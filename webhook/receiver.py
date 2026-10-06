#!/usr/bin/env python3
"""GitHub App webhook receiver: the only internet-exposed piece (Tailscale Funnel, /webhook only).

Holds no GitHub credentials, only the webhook secret. A delivery is accepted only with a valid
`X-Hub-Signature-256` (HMAC-SHA256 of the raw body); `workflow_job` and `workflow_run` events are
written to the shared SQLite store (dashboard/ci_store.py) that the dashboard and watchdog read.
Anything else is dropped. See docs/webhook.md. Python 3.9 compatible, standard library only.

Config (environment): WEBHOOK_BIND (0.0.0.0), WEBHOOK_PORT (8766), WEBHOOK_SECRET_FILE or
WEBHOOK_SECRET, WEBHOOK_ORG (example-org), CI_DB (/ci/ci.db).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "dashboard"))
import ci_store  # noqa: E402  (shared with the dashboard; the image copies both directories)

PATH = "/webhook"
MAX_BODY = 1024 * 1024
READ_TIMEOUT = 10
PRUNE_EVERY = 3600


def log(*parts):
    print(time.strftime("%Y-%m-%dT%H:%M:%S"), *parts, flush=True)


def read_secret():
    """The webhook secret from WEBHOOK_SECRET_FILE, else WEBHOOK_SECRET. Empty string if neither."""
    path = os.environ.get("WEBHOOK_SECRET_FILE")
    if path:
        try:
            return Path(path).read_text(encoding="utf-8").strip()
        except OSError:
            return ""
    return os.environ.get("WEBHOOK_SECRET", "").strip()


def signature_ok(secret, body, header):
    want = "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(want.encode("ascii"), (header or "").encode("utf-8", "replace"))


class Handler(BaseHTTPRequestHandler):
    server_version = "webhook"
    sys_version = ""
    timeout = READ_TIMEOUT  # socket timeout for reading the request line, headers and body

    def log_message(self, *args):  # one line per delivery is logged by do_POST instead
        pass

    def _reply(self, code, text=""):
        data = text.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Connection", "close")
        self.end_headers()
        if data and self.command != "HEAD":
            self.wfile.write(data)

    def _other(self):
        self._reply(405 if self.path == PATH else 404)

    do_GET = do_HEAD = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = _other

    def do_POST(self):
        if self.path != PATH:
            return self._reply(404)
        length = self.headers.get("Content-Length")
        if length is None:
            return self._reply(411)
        if not length.isdigit():
            return self._reply(400)
        if int(length) > MAX_BODY:
            return self._reply(413)
        try:
            body = self.rfile.read(int(length))
        except (socket.timeout, OSError):
            return self._reply(408)
        if len(body) != int(length):
            return self._reply(400)
        delivery = (self.headers.get("X-GitHub-Delivery") or "")[:64]
        if not signature_ok(self.server.secret, body, self.headers.get("X-Hub-Signature-256")):
            log("rejected: bad or missing signature, delivery", delivery or "-")
            return self._reply(401)
        event = self.headers.get("X-GitHub-Event") or ""
        try:
            payload = json.loads(body)
        except ValueError:
            log("rejected: bad json, event", event[:40], "delivery", delivery or "-")
            return self._reply(400)
        action = str(payload.get("action", "")) if isinstance(payload, dict) else ""
        if event == "ping":
            code, result = 200, "pong"
        elif event not in ci_store.EVENTS:
            code, result = 202, "ignored event"
        else:
            owner = (
                ((payload.get("repository") or {}).get("owner") or {}).get("login")
                if isinstance(payload, dict)
                else None
            )
            if not isinstance(owner, str) or owner.casefold() != self.server.org.casefold():
                code, result = 202, "ignored org"
            else:
                try:
                    result, code = self.server.store.apply_webhook(event, payload, delivery), 200
                except Exception as e:  # a store failure: GitHub may redeliver, the reconcile poll covers it
                    code, result = 500, "store error " + type(e).__name__
        log("event", event[:40], "action", action[:40] or "-", "result", result, "delivery", delivery or "-")
        self._reply(code, result + "\n")


class Receiver(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, addr, store, secret, org):
        super().__init__(addr, Handler)
        self.store, self.secret, self.org = store, secret, org


def prune_loop(store, every=PRUNE_EVERY):
    while True:
        time.sleep(every)
        try:
            store.prune()
        except Exception as e:
            log("prune failed:", type(e).__name__)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    db = os.environ.get("CI_DB", "/ci/ci.db")
    if "--healthcheck" in argv:
        try:
            ci_store.Store(db).db.execute("SELECT 1 FROM deliveries LIMIT 1")
        except Exception:
            return 1
        return 0
    secret = read_secret()
    if not secret:
        print("refusing to start: no webhook secret (WEBHOOK_SECRET_FILE or WEBHOOK_SECRET is empty)", file=sys.stderr)
        return 1
    store = ci_store.Store(db)
    store.prune()
    threading.Thread(target=prune_loop, args=(store,), daemon=True).start()
    srv = Receiver(
        (os.environ.get("WEBHOOK_BIND", "0.0.0.0"), int(os.environ.get("WEBHOOK_PORT", "8766"))),
        store,
        secret,
        os.environ.get("WEBHOOK_ORG", "example-org"),
    )
    log("listening on", srv.server_address[0], srv.server_address[1], "db", db)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
