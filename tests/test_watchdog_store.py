"""Watchdog store mode (CI_STORE=1): ETags, rate-limit capture, reading from the store, reconcile."""

import json
import sys
import urllib.error
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_watchdog as T  # noqa: E402  (its FakeGH, Sink, Clock and helpers)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "dashboard"))
import ci_store  # noqa: E402

W, REPO, iso = T.W, T.REPO, T.iso


class Resp:
    def __init__(self, body, headers=None, status=200):
        self.status, self.headers, self._body = status, headers or {}, json.dumps(body).encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._body


# ---- GitHub client: ETag and rate limit


def test_etag_304_returns_cached_body_as_200():
    calls = []

    def opener(req, timeout=None):
        calls.append(req.get_header("If-none-match"))
        if len(calls) == 1:
            return Resp({"a": 1}, {"ETag": 'W/"x"', "X-RateLimit-Remaining": "4000"})
        raise urllib.error.HTTPError(req.full_url, 304, "Not Modified", {"X-RateLimit-Remaining": "4000"}, None)

    gh = W.GitHub("t", opener=opener)
    assert gh.request("GET", "/x", {"p": 1}) == (200, {"a": 1})
    assert gh.request("GET", "/x", {"p": 1}) == (200, {"a": 1})
    assert calls == [None, 'W/"x"']


def test_etag_is_per_full_url_get_only_and_bounded(monkeypatch):
    seen = []

    def opener(req, timeout=None):
        seen.append((req.get_method(), req.get_header("If-none-match")))
        return Resp({}, {"ETag": '"e"'})

    gh = W.GitHub("t", opener=opener)
    gh.request("GET", "/x", {"page": 1})
    gh.request("GET", "/x", {"page": 2})  # another URL: no validator
    gh.request("POST", "/x")
    assert seen == [("GET", None), ("GET", None), ("POST", None)]
    monkeypatch.setattr(W, "ETAG_CACHE_MAX", 3)
    for i in range(10):
        gh.request("GET", f"/y{i}")
    assert len(gh._etags) == 3


def test_ratelimit_captured_from_success_and_error_and_logged_once_per_crossing(capsys):
    hdr = {"X-RateLimit-Remaining": "900", "X-RateLimit-Limit": "5000", "X-RateLimit-Reset": "1790000000"}
    queue = [
        Resp({}, dict(hdr, **{"X-RateLimit-Resource": "core"})),
        Resp({}, dict(hdr, **{"X-RateLimit-Remaining": "400"})),
        Resp({}, dict(hdr, **{"X-RateLimit-Remaining": "300"})),
        "403",
        Resp({}, dict(hdr, **{"X-RateLimit-Remaining": "4000"})),
        Resp({}, dict(hdr, **{"X-RateLimit-Remaining": "100"})),
    ]

    def opener(req, timeout=None):
        r = queue.pop(0)
        if r == "403":
            raise urllib.error.HTTPError(req.full_url, 403, "no", {"X-RateLimit-Remaining": "0"}, None)
        return r

    gh = W.GitHub("t", opener=opener)
    gh.request("GET", "/a")
    assert gh.ratelimit == {"remaining": 900, "limit": 5000, "reset": 1790000000, "resource": "core"}
    for _ in range(2):
        gh.request("GET", "/a")
    assert gh.request("GET", "/a")[0] == 403
    assert gh.ratelimit["remaining"] == 0
    gh.request("GET", "/a")
    gh.request("GET", "/a")
    assert capsys.readouterr().out.count("rate limit low") == 2  # one per crossing: 400, then 100


def test_headerless_fake_responses_still_work():
    class Bare(Resp):
        headers = None

    gh = W.GitHub("t", opener=lambda req, timeout=None: Bare({"ok": 1}))
    assert gh.request("GET", "/x") == (200, {"ok": 1}) and gh.ratelimit == {}


# ---- store mode

NOW_ISO = lambda clock: iso(clock.t)  # noqa: E731


@pytest.fixture
def env(tmp_path):
    gh, sink, clock = T.FakeGH(), T.Sink(), T.Clock()
    gh.run_by_id = {}
    base = gh.request

    def request(method, path, params=None):
        params = params or {}
        if method == "GET" and path.endswith("/actions/runs") and "status" not in params and "created" not in params:
            return 200, {"workflow_runs": gh.latest}
        parts = path.split("/")
        if method == "GET" and len(parts) == 7 and parts[5] == "runs":
            run = gh.run_by_id.get(int(parts[6]))
            return (200, run) if run else (404, None)
        return base(method, path, params)

    gh.request = request
    gh.latest = []
    gh.ratelimit = {"remaining": 4000, "limit": 5000}
    store = ci_store.Store(tmp_path / "ci.db", clock=clock)
    cfg = W.Config({"WATCHDOG_DIGEST_HOUR": "24", "CI_STORE": "1", "WATCHDOG_REPOS": REPO})
    wd = W.Watchdog(cfg, gh, sink, W.State(None), clock, store=store)
    yield wd, gh, sink, clock, store
    store.close()


def test_config_defaults():
    c = W.Config({})
    assert (c.ci_store, c.ci_db, c.reconcile_seconds, c.store_max_age) == (False, "/ci/ci.db", 300, 900)
    assert W.Config({"CI_STORE": "1"}).ci_store is True


def put_queued(store, clock, minutes=20, run_id=1):
    store.upsert_many(
        REPO,
        [
            {
                "id": run_id,
                "name": "CI",
                "status": "in_progress",
                "created_at": iso(clock.t - 3600),
                "updated_at": iso(clock.t),
            }
        ],
        [T.job(11, "unit", run_id=run_id, created_at=iso(clock.t - minutes * 60))],
    )


def test_queued_jobs_from_store_has_the_polled_shape(env):
    wd, gh, sink, clock, store = env
    put_queued(store, clock)
    store.mark_reconciled()
    got = wd.queued_jobs()
    assert len(got) == 1 and got[0].keys() == {"repo", "job", "labels", "waited", "name", "run_name"}
    q = got[0]
    assert (q["repo"], q["name"], q["run_name"], q["labels"], q["waited"]) == (
        REPO,
        "unit",
        "CI",
        ["self-hosted", "linux-ci"],
        1200,
    )
    assert q["job"]["id"] == 11
    # the polled path gives the same shape
    T.queue_one(gh, clock, 20)
    store_mode = wd.store
    wd.store = None
    assert wd.queued_jobs()[0].keys() == got[0].keys()
    wd.store = store_mode


def test_stale_queued_job_of_a_finished_run_is_not_reported(env):
    wd, gh, sink, clock, store = env
    put_queued(store, clock)
    store.upsert_run(
        REPO, {"id": 1, "name": "CI", "status": "completed", "conclusion": "success", "updated_at": iso(clock.t + 5)}
    )
    store.mark_reconciled()
    assert wd.queued_jobs() == []


def test_untrusted_store_falls_back_to_polling(env):
    wd, gh, sink, clock, store = env
    put_queued(store, clock, minutes=99)  # in the store only
    T.queue_one(gh, clock, 5)  # GitHub says 5 minutes
    assert wd.queued_jobs()[0]["waited"] == 300  # never reconciled: polled
    store.mark_reconciled()
    clock.t += 901  # reconcile older than WATCHDOG_STORE_MAX_AGE
    assert wd.queued_jobs()[0]["waited"] == 300 + 901
    store.mark_reconciled()
    assert wd.queued_jobs()[0]["waited"] == 99 * 60 + 901


def test_reconcile_catches_run_that_finished_while_event_missed(env):
    wd, gh, sink, clock, store = env
    put_queued(store, clock)  # the store still thinks run 1 is in progress with a queued job
    gh.runs[(REPO, "in_progress")] = []
    done_run = {
        "id": 1, "name": "CI", "status": "completed", "conclusion": "failure", "run_attempt": 1,
        "created_at": iso(clock.t - 3600), "updated_at": iso(clock.t + 1),
    }  # fmt: skip
    gh.run_by_id[1] = done_run
    gh.jobs[1] = [T.job(11, "unit", "completed", "failure", run_id=1, created_at=iso(clock.t - 1200))]
    assert store.live_run_ids() == [(REPO, 1)] and not store.trusted(900)
    wd.cycle()
    assert store.live_run_ids() == []
    assert store.runs(REPO)[0]["conclusion"] == "failure"
    assert store.jobs(run_id=1)[0]["status"] == "completed"
    assert store.trusted(900)
    assert wd.queued_jobs() == []
    assert json.loads(store.get_meta("ratelimit")) == {"remaining": 4000, "limit": 5000}
    assert store.get_meta("reconcile_seconds") == "300"


def test_reconcile_stores_live_and_latest_runs_with_jobs(env):
    wd, gh, sink, clock, store = env
    gh.runs[(REPO, "queued")] = [
        {"id": 2, "name": "CI", "status": "queued", "created_at": iso(clock.t), "updated_at": iso(clock.t)}
    ]
    gh.latest = gh.runs[(REPO, "queued")] + [
        {
            "id": 3,
            "name": "CI",
            "status": "completed",
            "conclusion": "success",
            "created_at": iso(clock.t - 99),
            "updated_at": iso(clock.t - 90),
        }
    ]
    gh.jobs[2] = [T.job(21, "lint", run_id=2, created_at=iso(clock.t))]
    wd.cycle()
    assert sorted(r["id"] for r in store.runs(REPO)) == [2, 3]
    assert [j["id"] for j in store.jobs(run_id=2)] == [21] and store.jobs(run_id=3) == []
    assert store.trusted(900)


def test_reconcile_failure_does_not_mark_or_kill_the_loop(env):
    wd, gh, sink, clock, store = env
    gh.fail_all = True
    with pytest.raises(W.GitHubError):  # the cycle itself still fails (nothing can be read) ...
        wd.cycle()
    assert store.reconciled_ago() is None  # ... but the store is not marked
    assert wd._last_reconcile is None  # and the next cycle tries again

    # run_forever survives it
    class Stop:
        n = 0

        def is_set(self):
            self.n += 1
            return self.n > 1  # noqa: E702

        def wait(self, s):
            pass  # noqa: E704

    class HB:
        def write_text(self, *a, **k):
            pass  # noqa: E704

    wd.run_forever(Stop(), HB())


def test_reconcile_runs_when_due_only(env):
    wd, gh, sink, clock, store = env
    wd.cycle()
    first = store.get_meta("reconciled_at")
    clock.t += 100
    wd.cycle()
    assert store.get_meta("reconciled_at") == first
    clock.t += 250
    wd.cycle()
    assert store.get_meta("reconciled_at") != first


def test_failed_run_examined_from_store_and_rerun(env):
    wd, gh, sink, clock, store = env
    run = {
        "id": 5,
        "run_attempt": 1,
        "status": "completed",
        "conclusion": "failure",
        "created_at": iso(clock.t - 600),
        "updated_at": iso(clock.t - 60),
    }
    gh.latest = [run]
    gh.jobs[5] = [dict(T.lost_job(), run_id=5)]
    gh.annotations.update({50: T.LOST})
    wd.cycle()
    assert gh.posts == [f"/repos/{REPO}/actions/jobs/50/rerun"]
    assert [j["id"] for j in store.jobs(run_id=5)] == [50]  # reconcile fetched the failed run's jobs once


def test_store_off_ignores_a_store(tmp_path):
    store = ci_store.Store(tmp_path / "ci.db")
    gh, sink, clock = T.FakeGH(), T.Sink(), T.Clock()
    wd = W.Watchdog(W.Config({"WATCHDOG_DIGEST_HOUR": "24"}), gh, sink, W.State(None), clock, store=store)
    T.queue_one(gh, clock, 20)
    store.mark_reconciled()
    wd.cycle()
    assert wd.store is None and store.runs(REPO) == []
    assert [s for s in sink.sent if s[0] == "CI job queued too long"]
    store.close()
