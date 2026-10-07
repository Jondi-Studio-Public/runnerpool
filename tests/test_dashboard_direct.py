"""Hosts that report their own state (Mac SSH info, win-N and wsl-N pushes) let the dashboard read GitHub's runner list less often."""

import importlib.util
import pathlib

import pytest

SERVER = pathlib.Path(__file__).parent.parent / "dashboard/server.py"
TOKEN = "a" * 40


def load():
    spec = importlib.util.spec_from_file_location("dashboard_server_direct", SERVER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def runner(name, busy=False):
    return {"name": name, "repo": "example-org", "kind": "ci", "status": "online", "busy": busy, "labels": []}


def linux_info(host="wsl-1", busy=False):
    return {
        "host": host,
        "platform": "linux",
        "cores": 24,
        "memory_gb": 48,
        "runners": [{"name": "win-1-wsl-1", "kind": "ci", "state": "active", "busy": busy}],
    }


def win_info(host="win-1"):
    return {
        "host": host,
        "platform": "windows",
        "cores": 16,
        "memory_gb": 32,
        "runners": [{"name": host, "kind": "ci", "state": "running", "busy": False}],
    }


def device_list():
    return [
        {"host": "air-1", "kind": "mac", "runners": [runner("air-1-ci")]},
        {"host": "win-1", "kind": "pc", "runners": [runner("win-1"), runner("win-1-wsl-1")]},
    ]


@pytest.fixture
def state(monkeypatch):
    s = load()
    st = s.State()
    clock = [1000.0]
    monkeypatch.setattr(s.time, "time", lambda: clock[0])
    st.runners = {"macs": ["air-1"], "devices": device_list(), "other": []}
    st.clock = clock
    return s, st


def mac_ok(st, s):
    st.infos["air-1"] = {"ok": True, "info": {"host": "air-1", "runners": [{"name": "air-1-ci", "busy": False}]}}
    st.info_at["air-1"] = s.time.time()


def test_wsl_hosts_may_push_and_get_a_linux_platform_check():
    s = load()
    assert s.is_push_host("wsl-1") and s.is_push_host("win-1") and not s.is_push_host("air-1")
    assert s.load_push_tokens.__doc__
    assert s.validate_push(linux_info(), "wsl-1") is None
    assert "platform must be linux" in s.validate_push(win_info("wsl-1"), "wsl-1")
    assert "platform must be windows" in s.validate_push(linux_info("win-1"), "win-1")
    assert "info.cores" in s.validate_push({**linux_info(), "cores": "24"}, "wsl-1")
    assert "info.host must be wsl-1" in s.validate_push(linux_info("wsl-2"), "wsl-1")


def test_load_push_tokens_accepts_wsl_lines(tmp_path):
    s = load()
    f = tmp_path / "tokens"
    f.write_text(f"win-1={TOKEN}\nwsl-1={'b' * 40}\nair-1={TOKEN}\n")
    assert set(s.load_push_tokens(f)) == {"win-1", "wsl-1"}


def test_direct_covers_needs_every_device_and_a_pcs_wsl_side(state):
    s, st = state
    assert not st.direct_covers()
    mac_ok(st, s)
    st.push_info("win-1", win_info())
    assert not st.direct_covers()  # the PC has WSL runners but wsl-1 has not pushed
    st.clock[0] += s.PUSH_MIN_GAP + 1
    st.push_info("wsl-1", linux_info())
    assert st.direct_covers()


def test_direct_covers_drops_when_a_report_goes_stale_or_fails(state):
    s, st = state
    mac_ok(st, s)
    st.push_info("win-1", win_info())
    st.push_info("wsl-1", linux_info())
    assert st.direct_covers()
    st.infos["air-1"]["ok"] = False
    assert not st.direct_covers()
    mac_ok(st, s)
    assert st.direct_covers()
    st.clock[0] += s.PUSH_STALE + 1
    mac_ok(st, s)
    assert not st.direct_covers()  # both pushes are now stale


def test_a_pc_without_wsl_runners_needs_only_win_n(state):
    s, st = state
    st.runners["devices"][1]["runners"] = [runner("win-1")]
    mac_ok(st, s)
    st.push_info("win-1", win_info())
    assert st.direct_covers()


def test_no_devices_never_counts_as_covered(state):
    s, st = state
    st.runners = {"macs": [], "devices": [], "other": []}
    assert not st.direct_covers()


def test_overlay_busy_uses_the_devices_own_report():
    s = load()
    out = s.overlay_busy(device_list(), {"wsl-1": linux_info(busy=True)})
    assert [r["busy"] for r in out[1]["runners"]] == [False, True]
    assert [r["busy"] for r in out[0]["runners"]] == [False]
    assert device_list()[1]["runners"][1]["busy"] is False  # the input is not changed


def test_overlay_only_speaks_for_its_own_device_and_online_runners():
    s = load()
    devs = device_list()
    spoof = {"host": "wsl-1", "runners": [{"name": "air-1-ci", "busy": True}, {"name": "win-1", "busy": True}]}
    out = s.overlay_busy(devs, {"wsl-1": spoof})
    assert out[0]["runners"][0]["busy"] is False and out[1]["runners"][0]["busy"] is False  # not wsl-1's runners
    devs[1]["runners"][1]["status"] = "offline"
    out = s.overlay_busy(devs, {"wsl-1": linux_info(busy=True)})
    assert out[1]["runners"][1]["busy"] is False


def test_wsl_admin_runner_needs_wsl_report_and_runners_must_be_listed(state):
    s, st = state
    st.runners["devices"][1]["runners"] = [runner("win-1"), runner("wsl-1-admin")]
    mac_ok(st, s)
    st.push_info("win-1", win_info())
    assert not st.direct_covers()
    st.clock[0] += s.PUSH_MIN_GAP + 1
    st.push_info("wsl-1", {**linux_info(), "runners": [{"name": "wsl-1-admin", "busy": False}]})
    assert st.direct_covers()
    st.clock[0] += s.PUSH_MIN_GAP + 1
    st.push_info("wsl-1", {**linux_info(), "runners": []})  # a runner missing from the report
    assert not st.direct_covers()


def test_get_runners_cache_follows_coverage(state, monkeypatch):
    s, st = state
    mac_ok(st, s)
    st.push_info("win-1", win_info())
    st.push_info("wsl-1", linux_info())
    st.runners_at = s.time.time() - 20
    monkeypatch.setattr(s, "run", lambda *a, **k: pytest.fail("covered: the 20 s old list must be reused"))
    assert st.get_runners() is st.runners
    st.infos["air-1"]["ok"] = False  # no longer covered: 20 s is too old, so GitHub is read again
    with pytest.raises(BaseException):
        st.get_runners()


def test_overlay_ignores_malformed_runner_reports():
    s = load()
    bad = {"runners": [{"name": "win-1", "busy": "yes"}, {"busy": True}, "x", {"name": 3, "busy": True}]}
    out = s.overlay_busy(device_list(), {"win-1": bad, "wsl-1": None})
    assert [r["busy"] for r in out[1]["runners"]] == [False, False]
