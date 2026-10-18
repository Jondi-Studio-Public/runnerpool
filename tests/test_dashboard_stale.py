"""After the background loop idled, /api/runners answers from the last runner list and refreshes behind it."""

import importlib.util
import pathlib
import time

SERVER = pathlib.Path(__file__).parent.parent / "dashboard/server.py"


def load():
    spec = importlib.util.spec_from_file_location("dashboard_server_stale", SERVER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_stale_runner_list_is_served_without_calling_github(monkeypatch):
    s = load()
    calls = []
    monkeypatch.setattr(s, "run", lambda *a, **k: calls.append(a) or (1, "", "should not be called"))
    st = s.State()
    st.runners, st.runners_at = {"macs": ["air-1"], "devices": []}, time.time() - s.IDLE_AFTER - 60
    assert st.get_runners() is st.runners
    assert st.wake.is_set()  # the loop is told to refresh
    assert not calls


def test_runner_list_older_than_an_hour_is_refetched(monkeypatch):
    s = load()
    calls = []
    monkeypatch.setattr(s, "run", lambda cmd, *a, **k: calls.append(cmd) or (0, '{"runners": []}', ""))
    monkeypatch.setattr(s, "macrunner_sha", lambda: "abc1234")
    st = s.State()
    st.runners, st.runners_at = {"macs": ["old"], "devices": []}, time.time() - s.RUNNERS_STALE_OK - 1
    assert st.get_runners()["macs"] != ["old"]
    assert calls


def test_fresh_list_does_not_wake_the_loop_and_force_skips_the_stale_path(monkeypatch):
    s = load()
    st = s.State()
    st.runners, st.runners_at = {"macs": [], "devices": []}, time.time()
    assert st.get_runners() is st.runners
    assert not st.wake.is_set()


def test_unknown_host_is_looked_up_fresh_before_it_is_refused(monkeypatch):
    s = load()
    st = s.State()
    st.runners, st.runners_at = {"macs": [], "devices": []}, time.time() - s.IDLE_AFTER - 60
    seen = []

    def fake(force=False):
        seen.append(force)
        return {"macs": ["air-9"] if force else [], "devices": []}

    monkeypatch.setattr(st, "get_runners", fake)
    assert st.known_mac("air-9")
    assert seen == [False, True]
