"""Actions on one host run one at a time, in order; in-flight state is reported; no-op toggles skip dispatch."""

import importlib.util
import pathlib
import threading
import time

SERVER = pathlib.Path(__file__).parent.parent / "dashboard/server.py"


def load():
    spec = importlib.util.spec_from_file_location("dashboard_server_queue", SERVER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_second_action_waits_for_its_turn_in_order():
    s = load()
    st = s.State()
    order, errs = [], []
    assert st.acquire("win-1", "first") is None
    started = []

    def second(name):
        started.append(name)
        err = st.acquire("win-1", name, wait=10)
        errs.append(err)
        order.append(name)
        time.sleep(0.05)
        st.release("win-1")

    threads = []
    for name in ("second", "third"):
        t = threading.Thread(target=second, args=(name,))
        t.start()
        threads.append(t)
        while name not in started:
            time.sleep(0.01)
        time.sleep(0.1)  # let it queue before the next arrives
    assert order == []  # both still waiting behind "first"
    assert st.actions_in_flight()["win-1"]["action"] == "first"
    assert st.actions_in_flight()["win-1"]["queued"] == 2
    st.release("win-1")
    for t in threads:
        t.join(5)
    assert order == ["second", "third"] and errs == [None, None]
    assert st.actions_in_flight() == {} and st.queues == {}


def test_wait_runs_out_with_a_reason_naming_the_running_action():
    s = load()
    st = s.State()
    assert st.acquire("win-1", "battery-run") is None
    err = st.acquire("win-1", "ci-off", wait=0.05)
    assert "win-1 is busy running battery-run" in err and "s)" in err
    assert st.queues == {}
    assert list(st.inflight) == ["win-1"]


def test_queue_full_refuses_at_once():
    s = load()
    st = s.State()
    assert st.acquire("win-1", "a") is None
    waiters = [
        threading.Thread(target=st.acquire, args=("win-1", f"w{i}", "auto", 5)) for i in range(s.ACTION_QUEUE_MAX)
    ]
    for t in waiters:
        t.start()
    while len(st.queues.get("win-1", [])) < s.ACTION_QUEUE_MAX:
        time.sleep(0.01)
    t0 = time.time()
    err = st.acquire("win-1", "overflow", wait=5)
    assert time.time() - t0 < 1 and "busy running a" in err and "3 more waiting" in err
    for _ in waiters:
        st.release("win-1")
        time.sleep(0.1)
    for t in waiters:
        t.join(5)


def test_stale_record_is_dropped(monkeypatch):
    s = load()
    st = s.State()
    st.inflight["win-1"] = {"action": "doctor", "started": time.time() - s.ACTION_STALE - 1, "via": "auto"}
    assert st.actions_in_flight() == {}
    assert st.acquire("win-1", "ci-on", wait=0.05) is None
    assert st.inflight["win-1"]["action"] == "ci-on"


def test_hosts_do_not_block_each_other():
    s = load()
    st = s.State()
    assert st.acquire("win-1", "a") is None
    assert st.acquire("air-1", "b", wait=0.05) is None
    assert set(st.actions_in_flight()) == {"win-1", "air-1"}


def test_in_flight_is_in_runners_answer(monkeypatch):
    s = load()
    st = s.State()
    monkeypatch.setattr(st, "get_runners", lambda force=False: {"macs": [], "devices": []})
    monkeypatch.setattr(s, "load_limits", lambda: {})
    assert st.runners_answer()["actions_in_flight"] == {}
    st.acquire("win-1", "battery-run")
    got = st.runners_answer()["actions_in_flight"]
    assert got["win-1"]["action"] == "battery-run" and got["win-1"]["since_s"] >= 0


def test_already_set_toggles(monkeypatch):
    s = load()
    st = s.State()
    st.infos["air-1"] = {"ok": True, "info": {"settings": {"pause_on_battery": False, "ci_enabled": True}}}
    assert st.already_set("air-1", "battery-run") and not st.already_set("air-1", "battery-pause")
    assert st.already_set("air-1", "ci-on") and not st.already_set("air-1", "ci-off")
    assert not st.already_set("air-1", "cores") and not st.already_set("air-2", "ci-on")
    st.infos["air-1"]["ok"] = False  # stale answer: dispatch for real
    assert not st.already_set("air-1", "ci-on")
    st.pushed["win-1"] = {"at": time.time(), "iso": "now", "info": {"settings": {"pause_on_battery": True}}}
    assert st.already_set("win-1", "battery-pause") and not st.already_set("win-1", "battery-run")


def test_post_noop_skips_dispatch(monkeypatch):
    s = load()
    st = s.State()
    st.pushed["win-1"] = {"at": time.time(), "iso": "now", "info": {"settings": {"pause_on_battery": False}}}
    monkeypatch.setattr(s, "STATE", st)
    monkeypatch.setattr(st, "known_host", lambda h, a: True)
    monkeypatch.setattr(s, "mac_action", lambda *a: (_ for _ in ()).throw(AssertionError("dispatched")))
    sent = []

    class H(s.Handler):
        def __init__(self, body):
            self.body = body

        def send(self, code, body, *a):
            sent.append((code, body))

    # drive the POST branch through a minimal fake request
    import io
    import json

    h = H(None)
    h.headers = {"Host": "127.0.0.1:1", "Content-Length": "40"}
    h.rfile = io.BytesIO(json.dumps({"action": "battery-run"}).encode())
    h.headers["Content-Length"] = str(len(h.rfile.getvalue()))
    h.path = "/api/mac/win-1/action"
    h.allowed = lambda: True
    h.do_POST()
    assert sent == [(200, {"ok": True, "via": None, "output": "already set", "run_url": None, "error": None})]
    assert st.inflight == {}
