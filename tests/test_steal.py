"""The steal action's queue, against a stand-in for GitHub's git refs API.

Ref creation is the one thing the queue relies on: the server answers 201 to the first POST for a
ref and 422 to every later one. The stand-in does the same under a lock, so two machines racing
for chunks behave as they do against GitHub.
"""

import json
import os
import pathlib
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

STEAL = pathlib.Path(__file__).parent.parent / ".github/actions/steal/steal.py"
REPO, RUN = "org/repo", "42-1"
PREFIX = f"/repos/{REPO}/git"


@pytest.fixture
def github():
    refs, lock = set(), threading.Lock()
    # Responses to send instead of handling the next POSTs: (status, headers, body).
    failures = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, code, body=None, headers=()):
            data = json.dumps(body).encode() if body is not None else b""
            self.send_response(code)
            for name, value in headers:
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            with lock:
                if failures:
                    code, headers, message = failures.pop(0)
                    return self.reply(code, {"message": message}, headers)
                if body["ref"] in refs:
                    return self.reply(422, {"message": "Reference already exists"})
                refs.add(body["ref"])
            self.reply(201, {"ref": body["ref"]})

        def do_GET(self):
            prefix = "refs/" + self.path.removeprefix(PREFIX + "/matching-refs/")
            with lock:
                self.reply(200, [{"ref": r} for r in sorted(refs) if r.startswith(prefix)])

        def do_DELETE(self):
            with lock:
                refs.discard(self.path.removeprefix(PREFIX + "/"))
            self.reply(204)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}", refs, failures
    server.shutdown()


def steal(url, mode, chunks, command="", machine="m"):
    env = dict(
        os.environ,
        STEAL_API_URL=url,
        STEAL_REPO=REPO,
        STEAL_RUN=RUN,
        STEAL_SHA="abc",
        STEAL_TOKEN="t",
        STEAL_MODE=mode,
        STEAL_CHUNKS=str(chunks),
        STEAL_COMMAND=command,
        STEAL_MACHINE=machine,
        GITHUB_STEP_SUMMARY=os.devnull,
    )
    return subprocess.Popen(
        [sys.executable, str(STEAL)], env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT
    )


def test_two_machines_run_each_chunk_once_then_verify_clears_the_queue(github, tmp_path):
    url, refs, _ = github
    log = tmp_path / "ran"
    command = f"echo {{k}} >> {log}"
    machines = [steal(url, "run", 8, command, f"m{i}") for i in (1, 2)]
    assert [m.wait() for m in machines] == [0, 0]
    assert sorted(int(k) for k in log.read_text().split()) == list(range(1, 9))
    verify = steal(url, "verify", 8)
    assert verify.wait() == 0, verify.stdout.read()
    assert not refs


def test_a_failed_chunk_fails_its_machine_and_verify(github):
    url, _, _ = github
    assert steal(url, "run", 3, '[ "{k}" != 2 ]').wait() != 0
    verify = steal(url, "verify", 3)
    assert verify.wait() != 0
    assert "failed-2" in verify.stdout.read()


def test_a_chunk_claimed_by_a_machine_that_died_fails_verify(github):
    url, refs, _ = github
    refs.add(f"refs/claims/{RUN}/claim-1")
    assert steal(url, "run", 2, "true").wait() == 0
    verify = steal(url, "verify", 2)
    assert verify.wait() != 0
    assert "without a result: [1]" in verify.stdout.read()


@pytest.mark.parametrize(
    "code, headers",
    [
        (403, [("retry-after", "0")]),
        (429, [("retry-after", "0")]),
        (403, [("x-ratelimit-remaining", "0"), ("x-ratelimit-reset", "0")]),
    ],
)
def test_a_rate_limited_ref_is_retried_until_it_is_recorded(github, code, headers):
    url, refs, failures = github
    failures += [(code, headers, "You have exceeded a secondary rate limit")] * 2
    machine = steal(url, "run", 1, "true")
    assert machine.wait() == 0, machine.stdout.read()
    assert "rate-limited" in machine.stdout.read()
    assert {f"refs/claims/{RUN}/claim-1", f"refs/claims/{RUN}/done-1"} <= refs


def test_a_forbidden_ref_that_is_not_a_rate_limit_fails_with_github_s_reason(github):
    url, refs, failures = github
    failures.append((403, [], "Resource not accessible by integration"))
    machine = steal(url, "run", 1, "true")
    assert machine.wait() != 0
    assert "HTTP 403" in (out := machine.stdout.read()) and "not accessible by integration" in out
    assert not refs
