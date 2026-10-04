"""Work stealing over git refs: the queue behind the `steal` action.

mode run:    for k in 1..N, try to create refs/claims/<run>/claim-<k>. GitHub answers 201
             to exactly one machine and 422 (already exists) to the rest, so a claim is
             atomic with no server. A machine runs the command for each chunk it wins, then
             records done-<k> or failed-<k>, and goes on until every chunk is claimed.
mode verify: every done-1..done-N must exist and no failed-*; then the refs are deleted.
             A chunk claimed by a machine that died has no done ref, so the run fails.
Standard library only.
"""

import json
import os
import shutil
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request

E = os.environ
REPO, RUN, SHA, TOKEN = E["STEAL_REPO"], E["STEAL_RUN"], E["STEAL_SHA"], E["STEAL_TOKEN"]
N = int(E.get("STEAL_CHUNKS") or 16)
# STEAL_API_URL points the queue at a stand-in server in tests (tests/test_steal.py).
API = f"{E.get('STEAL_API_URL') or 'https://api.github.com'}/repos/{REPO}/git"


def _context():
    # A uv-installed Python on macOS ships no CA bundle; the system's is at /etc/ssl/cert.pem.
    for cafile in (E.get("SSL_CERT_FILE"), "/etc/ssl/cert.pem", "/etc/ssl/certs/ca-certificates.crt"):
        if cafile and os.path.exists(cafile):
            return ssl.create_default_context(cafile=cafile)
    return ssl.create_default_context()


CONTEXT = _context()


def rate_limit_wait(err, body, attempt):
    """Seconds to wait before retrying a rate-limited request, or None when `err` is not one.

    GitHub answers a primary or secondary rate limit with 403 or 429. It says how long to wait in
    retry-after, or in x-ratelimit-reset once x-ratelimit-remaining reaches 0; otherwise its advice
    is at least a minute, longer on each retry.
    """
    limited = (
        err.headers.get("retry-after") is not None
        or err.headers.get("x-ratelimit-remaining") == "0"
        or "rate limit" in body.lower()
    )
    if err.code not in (403, 429) or not limited:
        return None
    if err.headers.get("retry-after") is not None:
        return int(err.headers["retry-after"])
    if err.headers.get("x-ratelimit-remaining") == "0" and err.headers.get("x-ratelimit-reset"):
        return max(0, int(err.headers["x-ratelimit-reset"]) - int(time.time())) + 1
    return 60 * 2**attempt


def call(method, path, body=None):
    req = urllib.request.Request(
        API + path,
        method=method,
        data=json.dumps(body).encode() if body else None,
        headers={"Authorization": f"Bearer {TOKEN}", "Accept": "application/vnd.github+json"},
    )
    waited = 0
    for attempt in range(6):
        try:
            with urllib.request.urlopen(req, timeout=30, context=CONTEXT) as r:
                text = r.read()
                return r.status, json.loads(text) if text else None
        except urllib.error.HTTPError as err:
            if err.code == 422 or err.code == 404:
                return err.code, None
            text = err.read().decode(errors="replace")
            wait = rate_limit_wait(err, text, attempt)
            # A rate limit is retried for up to ten minutes in all; a run of chunks is worth that.
            if wait is not None and waited + wait <= 600:
                print(f"steal: {method} {path} rate-limited ({err.code}), retrying in {wait}s", flush=True)
                time.sleep(wait)
                waited += wait
                continue
            if err.code < 500 or attempt >= 3:
                sys.exit(f"steal: {method} {path} failed: HTTP {err.code} {text}")
        except urllib.error.URLError:
            if attempt >= 3:
                raise
        time.sleep(2**attempt)
    sys.exit(f"steal: {method} {path} still failing after {attempt + 1} attempts")


def ref(name):
    return f"claims/{RUN}/{name}"


def create(name):
    status, _ = call("POST", "/refs", {"ref": "refs/" + ref(name), "sha": SHA})
    return status == 201


def run_mode():
    cmd, machine = E["STEAL_COMMAND"], E["STEAL_MACHINE"]
    if not cmd:
        sys.exit("steal: mode run needs a command")
    worst, taken = 0, []
    for k in range(1, N + 1):
        if not create(f"claim-{k}"):
            continue
        shell_cmd = cmd.replace("{shard}", f"{k}/{N}").replace("{k}", str(k)).replace("{n}", str(N))
        print(f"::group::{machine} takes chunk {k}/{N}", flush=True)
        start = time.time()
        rc = subprocess.call(shell_cmd, shell=True, executable=shutil.which("bash") or "/bin/bash")
        took = time.time() - start
        print("::endgroup::")
        print(f"{machine}: chunk {k}/{N} {'done' if rc == 0 else f'FAILED ({rc})'} in {took:.0f}s", flush=True)
        if not create(f"{'done' if rc == 0 else 'failed'}-{k}"):
            print(f"warning: could not record chunk {k}")
        taken.append((k, rc, took))
        worst = worst or rc
    summary = f"{machine}: {len(taken)} of {N} chunks ({', '.join(str(k) for k, _, _ in taken) or 'none'})"
    print(summary)
    with open(E.get("GITHUB_STEP_SUMMARY", os.devnull), "a") as out:
        out.write(summary + "\n")
    sys.exit(worst)


def verify_mode():
    _, refs = call("GET", f"/matching-refs/claims/{RUN}/")
    names = {r["ref"].rsplit("/", 1)[1] for r in (refs or [])}
    done = {k for k in range(1, N + 1) if f"done-{k}" in names}
    failed = sorted(n for n in names if n.startswith("failed-"))
    claimed = {k for k in range(1, N + 1) if f"claim-{k}" in names}
    missing = [k for k in range(1, N + 1) if k not in done]
    for r in refs or []:
        call("DELETE", "/" + r["ref"])
    print(
        f"chunks done {len(done)}/{N}; failed {failed or 'none'}; "
        f"never claimed {sorted(set(range(1, N + 1)) - claimed) or 'none'}"
    )
    with open(E.get("GITHUB_STEP_SUMMARY", os.devnull), "a") as out:
        out.write(f"chunks done {len(done)}/{N}, failed {len(failed)}\n")
    if missing or failed:
        sys.exit(f"steal: chunks without a result: {missing}; failed: {failed}")


{"run": run_mode, "verify": verify_mode}[E["STEAL_MODE"]]()
