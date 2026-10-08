"""The dashboard's sign-in gate over HTTP, and how it hands work to the runner CLI."""

import http.client
import importlib.util
import json
import pathlib
import re
import threading

import pytest

ROOT = pathlib.Path(__file__).parent.parent
SERVER = ROOT / "dashboard/server.py"
TOKEN = "x" * 20


def load():
    spec = importlib.util.spec_from_file_location("dashboard_server_auth", SERVER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def server(monkeypatch):
    s = load()
    st = s.State()
    st.started = True  # an API read must not start the background poll (live gh calls, a thread per test)
    monkeypatch.setattr(st, "get_runners", lambda force=False: {"macs": ["air-1"], "devices": []})
    monkeypatch.setattr(s, "STATE", st)
    calls = []
    monkeypatch.setattr(s, "mac_action", lambda *a, **k: calls.append(a) or {"ok": True})
    s.Handler.token = TOKEN
    s.Handler.extra_hosts = ()
    httpd = s.http.server.ThreadingHTTPServer(("127.0.0.1", 0), s.Handler)
    s.Handler.port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
    yield s, httpd.server_address[1], calls
    httpd.shutdown()
    httpd.server_close()


def send(port, method, target, headers=None, body=None):
    """A raw request line, so the target goes out exactly as given (absolute-form included)."""
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    c.putrequest(method, target, skip_host=True, skip_accept_encoding=True)
    data = json.dumps(body).encode() if body is not None else b""
    for k, v in {"Host": f"127.0.0.1:{port}", "Content-Length": str(len(data)), **(headers or {})}.items():
        c.putheader(k, v)
    c.endheaders(data)
    r = c.getresponse()
    return r.status, r.read()


@pytest.mark.parametrize(
    "target", ["/api/runners", "/api/info", "/api/runners?x=1", "/api/runners;x", "/api/runners#f"]
)
def test_api_reads_need_the_login(server, target):
    _, port, _ = server
    assert send(port, "GET", target)[0] == 403
    assert send(port, "GET", target, {"X-Macs-Token": TOKEN})[0] == 200


@pytest.mark.parametrize("target", ["http://x/api/runners", "http://127.0.0.1/api/info", "https://x/api/ci"])
def test_an_absolute_form_target_is_refused_not_routed(server, target):
    _, port, _ = server
    status, body = send(port, "GET", target)
    assert status == 400 and b"macs" not in body
    assert send(port, "GET", target, {"X-Macs-Token": TOKEN})[0] == 400  # refused even when signed in


def test_an_absolute_form_post_never_reaches_an_action(server):
    _, port, calls = server
    for target in ("http://x/api/mac/air-1/action", "http://x/api/runner-limits"):
        assert send(port, "POST", target, {"Content-Type": "application/json"}, {"action": "ci-off"})[0] == 400
    assert (
        send(port, "POST", "/api/mac/air-1/action", {"Content-Type": "application/json"}, {"action": "ci-off"})[0]
        == 403
    )
    assert calls == []


def test_a_signed_in_action_still_goes_through(server, monkeypatch):
    s, port, calls = server
    monkeypatch.setattr(s.STATE, "known_host", lambda host, action: True)
    monkeypatch.setattr(s.STATE, "already_set", lambda host, action: False)
    headers = {"Content-Type": "application/json", "X-Macs-Token": TOKEN}
    assert send(port, "POST", "/api/mac/air-1/action", headers, {"action": "ci-off"})[0] == 200
    assert len(calls) == 1 and calls[0][:2] == ("air-1", "ci-off")


def test_a_double_slash_target_is_gated_on_the_path_it_is_routed_to():
    # urlparse reads "//x/api/runners" as host x, path /api/runners. Python 3.12+ folds the leading
    # slashes before the handler sees them, older ones do not, so check the gate itself.
    s = load()
    s.Handler.port = 1
    h = object.__new__(s.Handler)
    h.path, h.command, h.headers = "//x/api/runners", "GET", {"Host": "127.0.0.1:1"}
    sent = []
    h.send = lambda code, body, *a: sent.append(code)
    h.signed_in = lambda: False
    assert h.allowed() is False and sent == [403]
    assert h.url.path == "/api/runners"  # the same parse the route uses


def test_healthz_still_answers_without_a_login(server):
    _, port, _ = server
    assert send(port, "GET", "/healthz") == (200, b'{"ok": true}')


def test_runner_cli_gets_the_dashboards_org_and_admin_repo_in_the_container(monkeypatch):
    s = load()
    # The container sets MACS_ORG / MACS_ADMIN_REPO only; the CLI dies without GITRUNNER_ORG.
    monkeypatch.delenv("GITRUNNER_ORG", raising=False)
    monkeypatch.delenv("GITRUNNER_REPO", raising=False)
    monkeypatch.setattr(s, "ORG", "acme")
    monkeypatch.setattr(s, "ADMIN_REPO", "acme/fleet")
    monkeypatch.setattr(s, "find_bash", lambda: "/bin/bash")
    seen = {}
    monkeypatch.setattr(s, "run", lambda cmd, timeout, env=None: seen.update(cmd=cmd, env=env) or (0, "", ""))
    s.macs(["info", "air-1"], 5)
    assert seen["cmd"][2:] == ["info", "air-1"]
    assert seen["env"]["GITRUNNER_ORG"] == "acme" and seen["env"]["GITRUNNER_REPO"] == "acme/fleet"


def test_runner_dashboard_on_the_pc_keeps_the_callers_org_and_repo_default(monkeypatch):
    s = load()
    # `runner dashboard` exports GITRUNNER_ORG but not MACS_ORG: the CLI keeps its own $ORG/runnerpool.
    monkeypatch.setenv("GITRUNNER_ORG", "mine")
    monkeypatch.delenv("GITRUNNER_REPO", raising=False)
    monkeypatch.setattr(s, "find_bash", lambda: "/bin/bash")
    seen = {}
    monkeypatch.setattr(s, "run", lambda cmd, timeout, env=None: seen.update(env=env) or (0, "", ""))
    s.macs(["info", "air-1"], 5)
    assert seen["env"]["GITRUNNER_ORG"] == "mine" and "GITRUNNER_REPO" not in seen["env"]


def test_the_image_ships_the_runner_cli_not_the_old_name_shim():
    copies = re.findall(r"^COPY (\S+) \./runner$", (ROOT / "dashboard/Dockerfile").read_text(), re.M)
    assert copies == ["runner"]
    # `macs` execs "$(dirname "$0")/runner": copied as ./runner it would exec itself forever.
    assert "cmd_dashboard()" in (ROOT / copies[0]).read_text()


def test_a_repo_override_survives_when_only_the_org_is_filled_in(monkeypatch):
    s = load()
    monkeypatch.delenv("GITRUNNER_ORG", raising=False)
    monkeypatch.setenv("GITRUNNER_REPO", "acme/own-admin")
    monkeypatch.setattr(s, "ORG", "acme")
    monkeypatch.setattr(s, "ADMIN_REPO", "acme/fleet")
    monkeypatch.setattr(s, "find_bash", lambda: "/bin/bash")
    seen = {}
    monkeypatch.setattr(s, "run", lambda cmd, timeout, env=None: seen.update(env=env) or (0, "", ""))
    s.macs(["info", "air-1"], 5)
    assert seen["env"]["GITRUNNER_ORG"] == "acme" and seen["env"]["GITRUNNER_REPO"] == "acme/own-admin"
