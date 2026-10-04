"""The dashboard keeps Mac info warm so a page load never waits on SSH."""

import importlib.util
import pathlib

SERVER = pathlib.Path(__file__).parent.parent / "dashboard/server.py"


def load():
    spec = importlib.util.spec_from_file_location("dashboard_server_info", SERVER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_cached_info_then_fresh_then_failure_keeps_last_good(monkeypatch):
    server = load()
    answers = [
        {"host": "air-1", "via": "ssh", "at": "t1", "run_url": None, "ok": True, "info": {"n": 1}, "error": None},
        {"host": "air-1", "via": "ssh", "at": "t2", "run_url": None, "ok": True, "info": {"n": 2}, "error": None},
        {"host": "air-1", "via": "ssh", "at": "t3", "run_url": None, "ok": False, "info": None, "error": "down"},
    ]
    calls = []
    monkeypatch.setattr(server, "mac_info", lambda host, via: calls.append(host) or dict(answers[len(calls) - 1]))
    st = server.State()
    assert st.get_info("air-1")["info"] == {"n": 1}
    assert st.get_info("air-1")["info"] == {"n": 1} and len(calls) == 1  # served from the cache
    assert st.get_info("air-1", fresh=True)["info"] == {"n": 2}
    bad = st.get_info("air-1", fresh=True)  # SSH failed: last good info stays
    assert not bad["ok"] and bad["error"] == "down"
    assert bad["info"] == {"n": 2} and bad["info_at"] == "t2"
    assert st.get_info("air-1")["info"] == {"n": 2}


def test_offline_reason_from_ssh_error():
    server = load()
    r = server.offline_reason
    assert "Tailscale" in r(255, "ssh: connect to host air-2 port 22: Connection timed out")
    assert "Tailscale" in r(124, "timed out after 60s")
    assert "SSH is off" in r(255, "Connection refused")
    assert "key" in r(255, "Permission denied (publickey)")
    assert "extra_hosts" in r(255, "Could not resolve hostname air-9")
    assert r(1, "something else") is None


def test_macrunner_sha_follows_main_not_the_image(monkeypatch):
    import subprocess

    server = load()
    monkeypatch.setattr(server, "baked_macrunner_sha", lambda: "baked00")

    def fake(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 0, stdout=b"echo new\r\n", stderr=b"")

    monkeypatch.setattr(server.subprocess, "run", fake)
    assert server.macrunner_sha() == server.sha_of(b"echo new\n")
    server._MAIN_SHA.update(at=0.0, sha="")
    monkeypatch.setattr(
        server.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 1, stdout=b"", stderr=b"x")
    )
    assert server.macrunner_sha() == "baked00"
