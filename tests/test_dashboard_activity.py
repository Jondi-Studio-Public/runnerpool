"""The dashboard's runner activity: which job each busy runner is on."""

import importlib.util
import pathlib
import time

SERVER = pathlib.Path(__file__).parent.parent / "dashboard/server.py"


def load():
    spec = importlib.util.spec_from_file_location("dashboard_server_activity", SERVER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def run_(rid, name="CI", branch="main"):
    return {"id": rid, "name": name, "head_branch": branch, "html_url": f"https://github.com/o/r/actions/runs/{rid}"}


def job(name, runner, status="in_progress", at="2026-10-02T10:00:00Z"):
    return {
        "name": name,
        "runner_name": runner,
        "status": status,
        "started_at": at,
        "html_url": f"https://github.com/o/r/job/{name}",
    }


def fake(table, calls=None):
    def fetch(path, token=None):
        if calls is not None:
            calls.append(path)
        for key, val in table.items():
            if key in path:
                return (None, val) if isinstance(val, str) else (val, None)
        return {"workflow_runs": [], "jobs": []}, None

    return fetch


def test_maps_in_progress_job_to_runner_case_insensitively():
    s = load()
    table = {
        "o/r/actions/runs?status=in_progress": {"workflow_runs": [run_(7, "CI", "feat")]},
        "runs/7/jobs": {"jobs": [job("unit", "Air-1"), job("lint", "win-1-wsl-1", status="completed"), job("q", "")]},
    }
    found, errors = s.build_activity([("o/r", None)], fetch=fake(table))
    assert errors == {}
    assert list(found) == ["air-1"]
    e = found["air-1"]
    assert (e["repo"], e["workflow"], e["job"], e["branch"]) == ("o/r", "CI", "unit", "feat")
    assert e["run_url"].endswith("/runs/7") and e["job_url"].endswith("/job/unit")
    assert e["started_at"] == "2026-10-02T10:00:00Z"


def test_a_repo_that_returns_403_is_skipped():
    s = load()
    table = {
        "o/denied/actions/runs": "gh: Resource not accessible (HTTP 403)",
        "o/ok/actions/runs?status": {"workflow_runs": [run_(1)]},
        "runs/1/jobs": {"jobs": [job("unit", "air-2")]},
    }
    found, errors = s.build_activity([("o/denied", None), ("o/ok", None)], fetch=fake(table))
    assert list(found) == ["air-2"]
    assert list(errors) == ["o/denied"]


def test_stops_once_every_busy_runner_is_found_and_calls_are_serial():
    s = load()
    calls = []
    table = {
        "o/a/actions/runs?status": {"workflow_runs": [run_(1)]},
        "runs/1/jobs": {"jobs": [job("unit", "air-1")]},
    }
    found, _ = s.build_activity([("o/a", None), ("o/b", None)], want={"air-1"}, fetch=fake(table, calls))
    assert list(found) == ["air-1"]
    assert not any("o/b" in c for c in calls)


def test_newest_job_wins_when_a_runner_appears_twice():
    s = load()
    table = {
        "actions/runs?status": {"workflow_runs": [run_(1)]},
        "runs/1/jobs": {"jobs": [job("old", "air-1", at="2026-10-02T09:00:00Z"), job("new", "air-1")]},
    }
    found, _ = s.build_activity([("o/r", None)], fetch=fake(table))
    assert found["air-1"]["job"] == "new"


def state_with(s, busy):
    st = s.State()
    st.runners = {
        "repos": [{"repo": "o", "runners": [{"name": n, "status": "online", "busy": b} for n, b in busy.items()]}]
    }
    return st


def test_entries_expire_when_the_runner_is_no_longer_busy_or_the_read_is_old():
    s = load()
    st = state_with(s, {"air-1": True, "air-2": False})
    st.activity = {"air-1": {"job": "x"}, "air-2": {"job": "y"}}
    st.activity_at = time.time()
    assert list(st.current_activity()) == ["air-1"]
    st.activity_at = time.time() - s.ACTIVITY_MAX_AGE - 1
    assert st.current_activity() == {}


def test_nothing_is_fetched_while_no_runner_is_busy():
    s = load()
    st = state_with(s, {"air-1": False})
    st.activity = {"air-1": {"job": "x"}}
    s.build_activity = lambda *a, **k: (_ for _ in ()).throw(AssertionError("fetched"))
    st.get_activity()
    assert st.activity == {}
