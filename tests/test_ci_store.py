"""The shared CI store: ordering, replays, retention and the trust rule."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "dashboard"))
import ci_store  # noqa: E402

T0 = 1_790_000_000.0


@pytest.fixture
def store(tmp_path):
    clock = {"t": T0}
    s = ci_store.Store(tmp_path / "ci.db", clock=lambda: clock["t"])
    s.clock = clock
    yield s
    s.close()


def job_payload(job_id=1, status="queued", conclusion=None, attempt=1, repo="o/r", **kw):
    job = {
        "id": job_id,
        "run_id": 10,
        "run_attempt": attempt,
        "status": status,
        "conclusion": conclusion,
        "created_at": "2026-10-04T10:00:00Z",
        "labels": ["self-hosted", "linux"],
        "name": "check",
    }
    job.update(kw)
    return {"action": status, "workflow_job": job, "repository": {"full_name": repo}}


def run_payload(run_id=10, status="queued", updated="2026-10-04T10:00:00Z", attempt=1, repo="o/r", **kw):
    run = {
        "id": run_id,
        "name": "ci",
        "status": status,
        "conclusion": kw.pop("conclusion", None),
        "run_attempt": attempt,
        "created_at": "2026-10-04T10:00:00Z",
        "updated_at": updated,
        "repository": {"big": "object"},
        "head_repository": {"big": "object"},
    }
    run.update(kw)
    return {"action": status, "workflow_run": run, "repository": {"full_name": repo}}


def test_job_lifecycle_and_order(store):
    assert store.apply_webhook("workflow_job", job_payload(status="queued"), "d1") == "stored"
    assert store.apply_webhook("workflow_job", job_payload(status="in_progress", runner_name="air-1"), "d2") == "stored"
    # a late "queued" delivery must not move the job backwards
    assert store.apply_webhook("workflow_job", job_payload(status="queued"), "d3") == "stale"
    assert store.apply_webhook("workflow_job", job_payload(status="completed", conclusion="success"), "d4") == "stored"
    [j] = store.jobs(repo="o/r")
    assert j["status"] == "completed" and j["conclusion"] == "success" and j["_repo"] == "o/r"
    assert store.jobs(status="queued") == []


def test_rerun_attempt_beats_old_attempt(store):
    store.apply_webhook("workflow_job", job_payload(status="completed", conclusion="failure", attempt=1))
    assert store.apply_webhook("workflow_job", job_payload(status="queued", attempt=2)) == "stored"
    assert store.jobs(status="queued")[0]["run_attempt"] == 2
    assert (
        store.apply_webhook("workflow_job", job_payload(status="completed", conclusion="failure", attempt=1)) == "stale"
    )


def test_replayed_delivery_is_a_duplicate(store):
    assert store.apply_webhook("workflow_job", job_payload(), "same") == "stored"
    assert (
        store.apply_webhook("workflow_job", job_payload(status="completed", conclusion="success"), "same")
        == "duplicate"
    )
    assert store.jobs()[0]["status"] == "queued"


def test_runs_keep_rest_shape_and_drop_big_objects(store):
    store.apply_webhook("workflow_run", run_payload(status="in_progress", updated="2026-10-04T10:01:00Z"), "a")
    [r] = store.runs("o/r")
    assert r["status"] == "in_progress" and "head_repository" not in r and "repository" not in r
    assert (
        store.apply_webhook("workflow_run", run_payload(status="queued", updated="2026-10-04T10:00:30Z"), "b")
        == "stale"
    )
    assert store.live_run_ids() == [("o/r", 10)]
    store.apply_webhook(
        "workflow_run", run_payload(status="completed", conclusion="failure", updated="2026-10-04T10:05:00Z"), "c"
    )
    assert store.live_run_ids() == []
    assert [r["id"] for r in store.runs_by_conclusion("o/r", ("failure", "cancelled"))] == [10]
    assert store.runs("o/r", status=("queued", "in_progress")) == []


def test_other_events_and_bad_shapes_are_ignored(store):
    assert store.apply_webhook("ping", {"zen": "x"}) == "ignored"
    assert store.apply_webhook("workflow_job", {"workflow_job": {"id": 1}}) == "ignored"  # no repository
    assert (
        store.apply_webhook("workflow_job", {"repository": {"full_name": "o/r"}, "workflow_job": {"id": "x"}})
        == "ignored"
    )
    assert store.apply_webhook("workflow_job", [1, 2]) == "ignored"


def test_prune_keeps_live_rows_and_recent_ones(store):
    store.apply_webhook("workflow_run", run_payload(run_id=1, status="completed", conclusion="success"), "x")
    store.apply_webhook("workflow_run", run_payload(run_id=2, status="in_progress"), "y")
    store.clock["t"] = T0 + 30 * 86400
    store.prune()
    assert [r["id"] for r in store.runs("o/r")] == [2]  # live stays however old; old completed goes
    assert store.db.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0] == 0


def test_trusted_needs_a_recent_reconcile(store):
    assert not store.trusted(900)
    store.mark_reconciled()
    assert store.trusted(900)
    store.clock["t"] += 1000
    assert not store.trusted(900)
    assert store.webhook_ago() is None
    store.apply_webhook("workflow_job", job_payload())
    assert store.webhook_ago() == 0


def test_upsert_many_rolls_back_on_error(store):
    bad = {"id": 5}  # no run_id is fine, but make json fail
    bad["x"] = object()
    with pytest.raises(TypeError):
        store.upsert_many("o/r", jobs=[{"id": 4, "run_id": 1, "status": "queued"}, bad])
    assert store.jobs() == []
