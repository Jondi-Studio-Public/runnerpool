"""With CI_STORE=1 the dashboard reads runs and jobs from the shared store instead of polling gh."""

import importlib.util
import json
import pathlib
import sys

SERVER = pathlib.Path(__file__).parent.parent / "dashboard/server.py"
sys.path.insert(0, str(SERVER.parent))
import ci_store  # noqa: E402


def load():
    spec = importlib.util.spec_from_file_location("dashboard_server_store", SERVER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


REPO = "example-org/maths"


def run_obj(rid, status="completed", conclusion="success", created="2026-10-04T10:00:00Z"):
    return {
        "id": rid,
        "name": "CI",
        "display_title": f"run {rid}",
        "head_branch": "main",
        "event": "push",
        "status": status,
        "conclusion": conclusion,
        "created_at": created,
        "updated_at": created,
        "html_url": f"https://github.com/{REPO}/actions/runs/{rid}",
    }


def job_obj(jid, run_id, runner, status="in_progress"):
    return {
        "id": jid,
        "run_id": run_id,
        "name": "unit",
        "status": status,
        "runner_name": runner,
        "started_at": "2026-10-04T10:01:00Z",
        "html_url": f"https://github.com/{REPO}/actions/runs/{run_id}/job/{jid}",
    }


def setup(monkeypatch, tmp_path, reconciled=True, mode="1"):
    s = load()
    db = tmp_path / "ci.db"
    st = ci_store.Store(db)
    st.upsert_many(
        REPO,
        runs=[run_obj(1), run_obj(2, "in_progress", None), run_obj(3, "queued", None)],
        jobs=[job_obj(9, 2, "Air-1")],
    )
    if reconciled:
        st.mark_reconciled()
    st.close()
    monkeypatch.setenv("CI_STORE", mode)
    monkeypatch.setenv("CI_DB", str(db))
    monkeypatch.setattr(s, "REPOS", [REPO])
    calls = []

    def fake_run(cmd, timeout, env=None):
        calls.append(cmd)
        return 1, "", "no gh in tests"

    monkeypatch.setattr(s, "run", fake_run)
    return s, calls


def test_store_serves_runs_and_activity_without_gh(monkeypatch, tmp_path):
    s, calls = setup(monkeypatch, tmp_path)
    ans = s.State().get_runs()
    assert ans["stats"]["running"] == 1 and ans["stats"]["queued"] == 1 and len(ans["runs"]) == 3
    assert ans["repos"] == [{"repo": REPO, "error": None, "runs": 3}]
    st = s.State()
    st.runners = {"repos": [{"runners": [{"name": "Air-1", "busy": True, "status": "online"}]}]}
    st.get_activity()
    act = st.activity["air-1"]
    assert act["repo"] == REPO and act["workflow"] == "CI" and act["branch"] == "main" and act["job"] == "unit"
    assert act["run_url"].endswith("/runs/2") and st.activity_errors == {}
    assert calls == []


def test_repo_ci_counts_come_from_the_store(monkeypatch, tmp_path):
    s, calls = setup(monkeypatch, tmp_path)
    monkeypatch.setattr(
        s, "gh_json", lambda path, token=None: ({"check_runs": []} if "check-runs" in path else [], None)
    )
    row = s.repo_ci(REPO, "main", None)
    assert (row["running"], row["queued"], row["error"]) == (1, 1, None) and calls == []


def test_repo_ci_polls_a_repo_the_store_has_not_seen(monkeypatch, tmp_path):
    s, _ = setup(monkeypatch, tmp_path)
    paths = []

    def gh(path, token=None):
        paths.append(path)
        return (
            ({"check_runs": []} if "check-runs" in path else {"workflow_runs": [{"status": "queued"}]}, None)
            if "pulls" not in path
            else ([], None)
        )

    monkeypatch.setattr(s, "gh_json", gh)
    row = s.repo_ci("someone/else", "main", None)
    assert row["queued"] == 1 and any("actions/runs" in p for p in paths)


def polled_runs(monkeypatch, s):
    calls = []

    def fake_run(cmd, timeout, env=None):
        calls.append(cmd)
        return 0, json.dumps({"workflow_runs": [run_obj(5)]}), ""

    monkeypatch.setattr(s, "run", fake_run)
    return calls


def test_untrusted_store_falls_back_to_polling(monkeypatch, tmp_path):
    s, _ = setup(monkeypatch, tmp_path, reconciled=False)
    calls = polled_runs(monkeypatch, s)
    ans = s.State().get_runs()
    assert len(calls) == 1 and ans["stats"]["runs"] == 1


def test_store_off_polls_as_before(monkeypatch, tmp_path):
    s, _ = setup(monkeypatch, tmp_path, mode="0")
    calls = polled_runs(monkeypatch, s)
    assert s.State().get_runs()["stats"]["runs"] == 1 and len(calls) == 1
    assert s.store_info()["mode"] == "off"


def test_broken_db_path_falls_back(monkeypatch, tmp_path):
    s, _ = setup(monkeypatch, tmp_path)
    monkeypatch.setenv("CI_DB", str(tmp_path / "missing-dir" / "ci.db"))
    calls = polled_runs(monkeypatch, s)
    assert s.State().get_runs()["stats"]["runs"] == 1 and len(calls) == 1
    info = s.store_info()
    assert info["mode"] == "on" and info["trusted"] is False


def test_store_read_error_falls_back(monkeypatch, tmp_path):
    s, _ = setup(monkeypatch, tmp_path)
    assert s.store_read(lambda st: 1 / 0) is None


def test_info_field(monkeypatch, tmp_path):
    s, _ = setup(monkeypatch, tmp_path)
    st = ci_store.Store(tmp_path / "ci.db")
    st.set_meta("ratelimit", json.dumps({"remaining": 4000, "limit": 5000}))
    st.set_meta("webhook_last", str(st.now() - 120))
    st.close()
    info = s.store_info()
    assert info["mode"] == "on" and info["trusted"] is True
    assert 118 <= info["webhook_ago_s"] <= 125 and info["reconciled_ago_s"] <= 5
    assert info["ratelimit"] == {"remaining": 4000, "limit": 5000}
