"""PCs push their own health to POST /api/push-info; the dashboard keeps the latest and never polls them."""

import http.client
import importlib.util
import json
import pathlib
import threading

import pytest

SERVER = pathlib.Path(__file__).parent.parent / "dashboard/server.py"
TOKEN = "a" * 40
OTHER = "b" * 40


def load(tokens_file=None):
    spec = importlib.util.spec_from_file_location("dashboard_server_push", SERVER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    if tokens_file is not None:
        mod.PUSH_TOKENS_FILE = str(tokens_file)
    return mod


def info(host="win-1", **kw):
    return {
        "host": host,
        "platform": "windows",
        "cores": 16,
        "memory_gb": 32,
        "disk_total_gb": 1000,
        "disk_free_gb": 400,
        "settings": {"max_cores": 0, "ci_enabled": True},
        "battery": {"present": False},
        "runners": [{"name": host, "kind": "ci", "state": "running", "busy": False}],
        **kw,
    }


@pytest.fixture
def tokens(tmp_path):
    f = tmp_path / "push_tokens"
    f.write_text(f"# comment\nwin-1={TOKEN}\nwin-2 = {OTHER}\nair-1={TOKEN}\nwin-3=short\n\nnonsense\n")
    return f


def test_load_push_tokens_keeps_only_valid_pc_lines(tokens, tmp_path):
    s = load(tokens)
    assert s.load_push_tokens() == {"win-1": TOKEN, "win-2": OTHER}
    assert s.load_push_tokens(tmp_path / "missing") == {}
    assert s.load_push_tokens("") == {}


def test_host_for_token_matches_exactly():
    s = load()
    toks = {"win-1": TOKEN, "win-2": OTHER}
    assert s.host_for_token(TOKEN, toks) == "win-1" and s.host_for_token(OTHER, toks) == "win-2"
    assert s.host_for_token(TOKEN[:-1], toks) is None and s.host_for_token("", toks) is None


@pytest.mark.parametrize(
    "bad, fragment",
    [
        ([], "JSON info object"),
        (info("win-2"), "info.host must be win-1"),
        (info(platform="darwin"), "platform"),
        (info(cores="16"), "info.cores"),
        (info(memory_gb=True), "info.memory_gb"),
        (info(settings=[]), "info.settings"),
        (info(runners=["x"]), "info.runners"),
    ],
)
def test_validate_push_rejects(bad, fragment):
    s = load()
    assert fragment in s.validate_push(bad, "win-1")


def test_validate_push_accepts_a_windows_info_and_missing_fields():
    s = load()
    assert s.validate_push(info(), "win-1") is None
    assert s.validate_push({"host": "win-1", "platform": "windows"}, "win-1") is None


def test_push_answer_fresh_stale_and_never(tokens, monkeypatch):
    s = load(tokens)
    st = s.State()
    clock = [1000.0]
    monkeypatch.setattr(s.time, "time", lambda: clock[0])
    never = st.push_answer("win-1")
    assert not never["ok"] and never["info"] is None and "not reported yet" in never["reason"]
    assert "not set up" in st.push_answer("win-9")["reason"]
    assert st.push_info("win-1", info()) is None
    ok = st.push_answer("win-1")
    assert ok["ok"] and ok["via"] == "push" and ok["info"]["cores"] == 16 and ok["error"] is None
    clock[0] += s.PUSH_STALE + 31
    old = st.push_answer("win-1")
    assert not old["ok"] and old["info"]["cores"] == 16  # the last good info stays
    assert old["reason"] == f"PC has not reported for {s.PUSH_STALE + 31} s"
    assert old["info_at"] == ok["info_at"]


def test_push_rate_limit_per_host(monkeypatch):
    s = load()
    st = s.State()
    clock = [50.0]
    monkeypatch.setattr(s.time, "time", lambda: clock[0])
    assert st.push_info("win-1", info()) is None
    clock[0] += 2
    assert st.push_info("win-1", info()) == 3
    assert st.push_info("win-2", info("win-2")) is None
    clock[0] += 3
    assert st.push_info("win-1", info()) is None


def test_get_info_for_a_pc_never_polls(tokens, monkeypatch):
    s = load(tokens)
    monkeypatch.setattr(s, "mac_info", lambda *a: pytest.fail("a PC must not be polled"))
    monkeypatch.setattr(s, "ssh", lambda *a: pytest.fail("a PC must not be reached over SSH"))
    st = s.State()
    assert st.get_info("win-1")["via"] == "push" and st.get_info("win-1", fresh=True)["info"] is None
    # The background loop polls only `macs`, never the PC devices, so nothing else needs to be stubbed.
    st.runners = {"macs": [], "devices": [{"host": "win-1", "kind": "pc"}]}
    assert not hasattr(st, "info_hosts") and st.get_info("win-1")["via"] == "push"


def test_runners_answer_takes_a_pcs_size_from_its_push(tokens, monkeypatch, tmp_path):
    s = load(tokens)
    s.DATA_DIR = tmp_path
    st = s.State()
    runner = {"name": "win-1", "repo": "example-org", "kind": "ci", "status": "online", "busy": False, "labels": []}
    answer = {"macs": [], "devices": [{"host": "win-1", "kind": "pc", "runners": [runner]}], "other": []}
    monkeypatch.setattr(st, "get_runners", lambda force=False: answer)
    st.push_info("win-1", info(cores=20, memory_gb=32))
    dev = st.runners_answer()["devices"][0]
    assert dev["cores"] == 20 and dev["ram_mb"] == 32 * 1024
    assert dev["runners"][0]["default"] == {"cores": 16, "ram_mb": int(0.8 * 32 * 1024)}


def test_known_pc_needs_a_listed_win_device(monkeypatch):
    s = load()
    st = s.State()
    monkeypatch.setattr(
        st, "get_runners", lambda force=False: {"macs": [], "devices": [{"host": "win-1", "kind": "pc"}]}
    )
    assert st.known_pc("win-1") and not st.known_pc("win-2") and not st.known_pc("wsl-1") and not st.known_pc("air-1")


# --- over HTTP ------------------------------------------------------------------------------


@pytest.fixture
def server(tokens, tmp_path, monkeypatch):
    s = load(tokens)
    s.DATA_DIR = tmp_path
    monkeypatch.setattr(s, "STATE", s.State())
    s.Handler.token = "x" * 20
    s.Handler.extra_hosts = ()
    httpd = s.http.server.ThreadingHTTPServer(("127.0.0.1", 0), s.Handler)
    s.Handler.port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield s, httpd.server_address[1]
    httpd.shutdown()


def post(port, body, token=TOKEN, headers=None, raw=None):
    c = http.client.HTTPConnection("127.0.0.1", port)
    h = {"Content-Type": "application/json", "Host": f"127.0.0.1:{port}", **(headers or {})}
    if token:
        h["Authorization"] = f"Bearer {token}"
    c.request("POST", "/api/push-info", body=raw if raw is not None else json.dumps(body), headers=h)
    r = c.getresponse()
    return r.status, json.loads(r.read() or b"{}")


def test_push_accepted_without_a_login_cookie_or_origin(server):
    s, port = server
    assert post(port, info())[0] == 200
    assert s.STATE.push_answer("win-1")["info"]["memory_gb"] == 32


def test_push_auth_and_validation_over_http(server):
    s, port = server
    assert post(port, info(), token=None)[0] == 401
    assert post(port, info(), token="c" * 40)[0] == 401
    assert post(port, info("win-2"))[0] == 403  # win-1's token, win-2's info
    assert post(port, info(), headers={"Authorization": f"Basic {TOKEN}"}, token=None)[0] == 401
    assert post(port, None, raw="{not json")[0] == 400
    assert post(port, info(platform="linux"))[0] == 400
    assert post(port, None, raw="x" * (s.PUSH_MAX_BODY + 1))[0] == 413
    assert s.STATE.push_answer("win-1")["info"] is None  # nothing above was stored
    assert post(port, info())[0] == 200
    assert post(port, info())[0] == 429  # again within 5 s


def test_push_locks_out_after_many_bad_tokens(server):
    s, port = server
    for _ in range(s.PUSH_FAIL_LIMIT):
        assert post(port, info(), token="d" * 40)[0] == 401
    assert post(port, info(), token="d" * 40)[0] == 429
    assert post(port, info())[0] == 429  # even the right token, for a minute


def test_push_is_off_without_tokens(tmp_path, monkeypatch):
    empty = tmp_path / "empty"
    empty.write_text("")
    s = load(empty)
    monkeypatch.setattr(s, "STATE", s.State())
    s.Handler.token = "x" * 20
    s.Handler.extra_hosts = ()
    httpd = s.http.server.ThreadingHTTPServer(("127.0.0.1", 0), s.Handler)
    s.Handler.port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        assert post(s.Handler.port, info())[0] == 503
    finally:
        httpd.shutdown()


def test_other_api_calls_still_need_the_login(server):
    s, port = server
    c = http.client.HTTPConnection("127.0.0.1", port)
    c.request("GET", "/api/runners", headers={"Host": f"127.0.0.1:{port}", "Authorization": f"Bearer {TOKEN}"})
    assert c.getresponse().status == 403
    c = http.client.HTTPConnection("127.0.0.1", port)
    c.request(
        "POST",
        "/api/runner-limits",
        body="{}",
        headers={"Host": f"127.0.0.1:{port}", "Authorization": f"Bearer {TOKEN}"},
    )
    assert c.getresponse().status == 403


def test_pc_actions_go_through_github_only(monkeypatch):
    s = load()
    monkeypatch.setattr(s, "ssh", lambda *a: pytest.fail("PCs have no SSH"))
    seen = []
    monkeypatch.setattr(s, "macs", lambda args, t: seen.append(args) or (0, "ok", ""))
    for action in ("cores", "ci-off", "doctor"):
        assert s.mac_action("win-1", action, 4, "auto", 6)["via"] == "actions"
    assert seen == [["cores", "win-1", "6"], ["ci", "win-1", "off"], ["doctor", "win-1"]]
