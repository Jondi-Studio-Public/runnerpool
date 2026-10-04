"""Applying a saved per-runner limit on the device: which host, which command, what the page is told."""

import importlib.util
import pathlib

import pytest

SERVER = pathlib.Path(__file__).parent.parent / "dashboard/server.py"


def load(tmp=None):
    spec = importlib.util.spec_from_file_location("dashboard_server_limits", SERVER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    if tmp is not None:
        mod.DATA_DIR = tmp
    return mod


def test_admin_host_routes_wsl_runners_to_their_distro():
    s = load()
    assert s.admin_host("air-1", "air-1") == "air-1"
    assert s.admin_host("air-1", "air-1-extra") == "air-1"
    assert s.admin_host("win-1", "win-1") == "win-1"
    assert s.admin_host("win-1", "win-1-wsl-2") == "wsl-1"
    assert s.admin_host("win-12", "Win-12-WSL-1") == "wsl-12"
    assert s.admin_host("win-1", "win-1-extra") == "win-1"


def test_is_pc_host():
    s = load()
    assert s.is_pc_host("win-1") and s.is_pc_host("wsl-3")
    assert not s.is_pc_host("air-1") and not s.is_pc_host("win-1-wsl-1") and not s.is_pc_host(None)


def test_limit_pairs():
    s = load()
    assert s.limit_pairs() == []
    assert s.limit_pairs(2, None) == ["cores=2"]
    assert s.limit_pairs("default", 4096) == ["cores=default", "ram=4096"]
    assert s.limit_pairs(None, "default") == ["ram=default"]


def test_parse_limit_action():
    s = load()
    assert s.parse_limit_action({"runner": "Air-1", "cores": 2, "ram_mb": "default"}) == (2, "default", "air-1", None)
    assert s.parse_limit_action({"runner": "air-1", "ram_mb": 512}) == (None, 512, "air-1", None)
    for bad in (
        {"cores": 2},
        {"runner": "air-1"},
        {"runner": "air-1", "cores": 0},
        {"runner": "air-1", "cores": "all"},
        {"runner": "air-1", "ram_mb": 10},
        {"runner": "air-1", "cores": True},
        {"runner": "../x", "cores": 1},
    ):
        assert s.parse_limit_action(bad)[3], bad


def test_mac_limit_goes_over_ssh_with_only_the_given_limits(monkeypatch):
    s = load()
    calls = []
    monkeypatch.setattr(s, "ssh", lambda host, args, t: calls.append((host, args)) or (0, "ok", ""))
    monkeypatch.setattr(s, "macs", lambda *a: pytest.fail("no GitHub fallback expected"))
    r = s.mac_action("air-1", "limit", 4, "auto", 2, "air-1", None)
    assert r["ok"] and r["via"] == "ssh"
    assert calls == [("air-1", ["limit", "air-1", "cores=2"])]
    s.mac_action("air-1", "limit", 4, "auto", "default", "air-1", 4096)
    assert calls[-1][1] == ["limit", "air-1", "cores=default", "ram=4096"]


def test_mac_limit_falls_back_to_github_when_ssh_cannot_connect(monkeypatch):
    s = load()
    monkeypatch.setattr(s, "ssh", lambda *a: (255, "", "Connection timed out"))
    seen = []
    monkeypatch.setattr(
        s, "macs", lambda args, t: seen.append(args) or (0, "limit on air-1: https://github.com/o/r/actions/runs/7", "")
    )
    r = s.mac_action("air-1", "limit", 4, "auto", None, "air-1", 2048)
    assert r["ok"] and r["via"] == "actions" and r["run_url"].endswith("/runs/7")
    assert seen == [["limit", "air-1", "air-1", "ram=2048"]]


@pytest.mark.parametrize("host", ["win-1", "wsl-1"])
def test_pc_limit_never_uses_ssh(monkeypatch, host):
    s = load()
    monkeypatch.setattr(s, "ssh", lambda *a: pytest.fail("PCs have no SSH"))
    seen = []
    monkeypatch.setattr(s, "macs", lambda args, t: seen.append(args) or (1, "", "runner: boom\nlast line"))
    r = s.mac_action(host, "limit", 4, "auto", 3, "win-1-wsl-1", "default")
    assert not r["ok"] and r["via"] == "actions" and r["error"]
    assert seen == [["limit", host, "win-1-wsl-1", "cores=3", "ram=default"]]


def test_other_actions_keep_their_arguments(monkeypatch):
    s = load()
    calls = []
    monkeypatch.setattr(s, "ssh", lambda host, args, t: calls.append(args) or (0, "", ""))
    s.mac_action("air-1", "cores", 4, "auto", 6)
    s.mac_action("air-1", "ramdisk-on", 8, "auto")
    assert calls == [["cores", "6"], ["ramdisk", "on", "8"]]


def state_with(s, monkeypatch, devices, macs=("air-1",)):
    st = s.State()
    monkeypatch.setattr(st, "get_runners", lambda force=False: {"macs": list(macs), "devices": devices})
    return st


def test_known_host_accepts_pc_hosts_for_their_actions(monkeypatch):
    s = load()
    st = state_with(
        s,
        monkeypatch,
        [{"host": "air-1", "kind": "mac", "runners": []}, {"host": "win-1", "kind": "pc", "runners": []}],
    )
    assert st.known_host("air-1", "cores") and st.known_host("air-1", "limit")
    assert st.known_host("win-1", "limit") and st.known_host("wsl-1", "limit")
    assert st.known_host("win-1", "cores") and st.known_host("win-1", "ci-off") and st.known_host("win-1", "doctor")
    assert not st.known_host("win-1", "ramdisk-on") and not st.known_host("win-1", "tailscale-up")
    assert not st.known_host("wsl-1", "ci-off")
    assert not st.known_host("win-2", "limit") and not st.known_host("air-9", "limit")
    assert not st.known_mac("win-1")


def test_apply_limit_reports_and_remembers(monkeypatch):
    s = load()
    st = state_with(s, monkeypatch, [])
    calls = []

    def fake(host, action, size, via, cores, runner, ram):
        calls.append((host, action, via, cores, runner, ram))
        return {"ok": True, "via": "ssh", "output": "", "run_url": None, "error": None}

    monkeypatch.setattr(s, "mac_action", fake)
    res = st.apply_limit("air-1", "air-1", {"cores": 2})
    assert res["applied"] and res["error"] is None
    assert calls == [("air-1", "limit", "auto", 2, "air-1", None)]
    st.apply_limit("air-1", "air-1", {"cores": None, "ram_mb": None})  # reset = both back to default
    assert calls[-1][3:] == ("default", "air-1", "default")
    assert st.limit_status["air-1"]["air-1"]["applied"] is True
    assert st.inflight == {}


def test_apply_limit_wsl_runner_uses_the_wsl_host(monkeypatch):
    s = load()
    st = state_with(s, monkeypatch, [])
    hosts = []
    monkeypatch.setattr(
        s,
        "mac_action",
        lambda host, *a: hosts.append(host) or {"ok": True, "via": "actions", "run_url": "u", "error": None},
    )
    res = st.apply_limit("win-1", "win-1-wsl-2", {"ram_mb": 2048})
    assert hosts == ["wsl-1"] and res["run_url"] == "u"
    st.apply_limit("win-1", "win-1", {"cores": 4})
    assert hosts == ["wsl-1", "win-1"]


def test_apply_limit_failure_is_reported_not_raised(monkeypatch):
    s = load()
    st = state_with(s, monkeypatch, [])
    monkeypatch.setattr(
        s, "mac_action", lambda *a: {"ok": False, "via": "actions", "run_url": None, "error": "air-1 offline"}
    )
    res = st.apply_limit("air-1", "air-1", {"cores": 2})
    assert res["applied"] is False and res["error"] == "air-1 offline"
    assert st.limit_status["air-1"]["air-1"]["error"] == "air-1 offline"

    def boom(*a):
        raise RuntimeError("kaput")

    monkeypatch.setattr(s, "mac_action", boom)
    res = st.apply_limit("air-1", "air-1", {"cores": 2})
    assert res["applied"] is False and "kaput" in res["error"]
    assert st.inflight == {}  # released even after an exception


def test_apply_limit_when_host_is_busy(monkeypatch):
    s = load()
    st = state_with(s, monkeypatch, [])
    monkeypatch.setattr(s, "mac_action", lambda *a: pytest.fail("must not run"))
    monkeypatch.setattr(s, "ACTION_WAIT", 0.05)
    st.inflight["air-1"] = {"action": "doctor", "started": s.time.time(), "via": "auto"}
    res = st.apply_limit("air-1", "air-1", {"cores": 2})
    assert res["applied"] is False and "busy running doctor" in res["error"]
    assert list(st.inflight) == ["air-1"]  # not ours to release


class FakeHandler:
    """Just enough of Handler for set_runner_limits."""

    def __init__(self):
        self.sent = []

    def send(self, code, body, *a):
        self.sent.append((code, body))


def post(s, monkeypatch, body, devices, apply):
    st = state_with(s, monkeypatch, devices)
    monkeypatch.setattr(s, "STATE", st)
    monkeypatch.setattr(st, "apply_limit", apply)
    h = FakeHandler()
    s.Handler.set_runner_limits(h, body)
    return h.sent[-1]


DEVICES = [
    {
        "host": "air-1",
        "kind": "mac",
        "runners": [{"name": "air-1", "kind": "ci"}, {"name": "air-1-admin", "kind": "admin"}],
    }
]


def test_post_saves_then_applies(monkeypatch, tmp_path):
    s = load(tmp_path)
    seen = []

    def apply(host, runner, changes, via="auto"):
        seen.append((host, runner, changes, via))
        return {"applied": True, "error": None, "via": "ssh", "run_url": None}

    code, body = post(s, monkeypatch, {"host": "air-1", "runner": "air-1", "cores": 2}, DEVICES, apply)
    assert code == 200 and body["ok"] and body["applied"] is True and body["error"] is None
    assert body["override"] == {"cores": 2}
    assert seen == [("air-1", "air-1", {"cores": 2}, "auto")]
    assert s.load_limits() == {"air-1": {"air-1": {"cores": 2}}}


def test_post_keeps_the_override_when_applying_fails(monkeypatch, tmp_path):
    s = load(tmp_path)
    code, body = post(
        s,
        monkeypatch,
        {"host": "air-1", "runner": "air-1", "ram_mb": 4096, "via": "actions"},
        DEVICES,
        lambda *a, **k: {"applied": False, "error": "offline", "via": "actions", "run_url": None},
    )
    assert code == 200 and body["ok"] is True and body["applied"] is False and body["error"] == "offline"
    assert s.load_limits() == {"air-1": {"air-1": {"ram_mb": 4096}}}  # saved, so Retry can resend it


def test_post_rejects_bad_via_and_admin_runners(monkeypatch, tmp_path):
    s = load(tmp_path)
    never = lambda *a, **k: pytest.fail("must not apply")  # noqa: E731
    assert (
        post(s, monkeypatch, {"host": "air-1", "runner": "air-1", "cores": 2, "via": "ssh"}, DEVICES, never)[0] == 400
    )
    assert post(s, monkeypatch, {"host": "air-1", "runner": "air-1-admin", "cores": 2}, DEVICES, never)[0] == 404
    assert s.load_limits() == {}
