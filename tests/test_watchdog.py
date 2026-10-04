"""CI watchdog rules, with the GitHub API and ntfy faked."""

import importlib.util
import pathlib
from datetime import datetime, timezone

SRC = pathlib.Path(__file__).parent.parent / "watchdog/watchdog.py"
spec = importlib.util.spec_from_file_location("watchdog_mod", SRC)
W = importlib.util.module_from_spec(spec)
spec.loader.exec_module(W)

T0 = datetime(2026, 10, 3, 12, 0, tzinfo=timezone.utc).timestamp()  # 23:00 in the fixture zone (Australia/Sydney)
REPO = "example-org/app-one"


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class FakeGH:
    """Routes GET paths to canned data and records every POST."""

    def __init__(self):
        self.runners = []
        self.runs = {}  # (repo, status) -> [run]
        self.jobs = {}  # run id -> [job]
        self.annotations = {}  # job id -> [annotation]
        self.all_runs = []  # for the digest (created filter)
        self.rerun_status = 201
        self.posts = []
        self.fail_all = False

    def request(self, method, path, params=None):
        if self.fail_all:
            raise W.GitHubError("down")
        params = params or {}
        if method == "POST":
            self.posts.append(path)
            return self.rerun_status, {"message": "Workflow is still running"} if self.rerun_status >= 400 else None
        if path == "/orgs/example-org/actions/runners":
            return 200, {"runners": self.runners}
        if path == "/orgs/example-org/repos":
            return 200, [{"full_name": REPO, "archived": False}, {"full_name": "example-org/old", "archived": True}]
        if path.endswith("/actions/runs") and "created" in params:
            return 200, {"workflow_runs": self.all_runs}
        if path.endswith("/actions/runs"):
            return 200, {
                "workflow_runs": self.runs.get((path.split("/")[2] + "/" + path.split("/")[3], params["status"]), [])
            }
        if "/actions/runs/" in path and path.endswith("/jobs"):
            return 200, {"jobs": self.jobs.get(int(path.split("/")[-2]), [])}
        if path.endswith("/annotations"):
            return 200, self.annotations.get(int(path.split("/")[-2]), [])
        return 404, None


class Sink:
    def __init__(self):
        self.sent = []
        self.ok = True

    def send(self, title, message, priority="default", tags=""):
        if self.ok:
            self.sent.append((title, message))
        return self.ok


class Clock:
    def __init__(self, t=T0):
        self.t = t

    def __call__(self):
        return self.t


def runner(name, labels, online=True):
    return {"name": name, "status": "online" if online else "offline", "labels": [{"name": x} for x in labels]}


def job(jid, name="test", status="queued", conclusion=None, labels=("self-hosted", "linux-ci"), **kw):
    return {
        "id": jid,
        "name": name,
        "status": status,
        "conclusion": conclusion,
        "labels": list(labels),
        "html_url": f"https://github.com/{REPO}/actions/runs/1/job/{jid}",
        **kw,
    }


def make(tmp_path=None, **env):
    gh, sink, clock = FakeGH(), Sink(), Clock()
    cfg = W.Config({"WATCHDOG_DIGEST_HOUR": "24", **env})
    state = W.State(tmp_path / "state.json" if tmp_path else None)
    return W.Watchdog(cfg, gh, sink, state, clock), gh, sink, clock


def queue_one(gh, clock, minutes, labels=("self-hosted", "linux-ci")):
    gh.runs[(REPO, "queued")] = [{"id": 1, "name": "CI"}]
    gh.jobs[1] = [job(11, "unit", created_at=iso(clock.t - minutes * 60), labels=labels)]


# ---- helpers


def test_device_names():
    assert W.device_of("air-1-vm-1") == "air-1"
    assert W.device_of("air-2") == "air-2"
    assert W.device_of("win-1-wsl-2") == "win-1"
    assert W.device_of("wsl-1-admin") == "win-1"
    assert W.device_of("win-1-ci-2") == "win-1"
    assert W.device_of("examplepc-app-1") == "examplepc"


# ---- rule 1: pool offline


def test_pool_offline_alerts_after_30_minutes_with_last_seen():
    wd, gh, sink, clock = make()
    gh.runners = [runner("air-1-vm-1", ["self-hosted", "Linux", "linux-ci"])]
    wd.cycle()  # seen online now
    gh.runners = [runner("air-1-vm-1", ["self-hosted", "Linux", "linux-ci"], online=False)]
    queue_one(gh, clock, 1)
    clock.t += 600
    wd.cycle()
    assert not [s for s in sink.sent if s[0] == "CI pool offline"]
    clock.t += 31 * 60  # 31 minutes since the pool went unserved
    wd.cycle()
    pool = [s for s in sink.sent if s[0] == "CI pool offline"]
    assert len(pool) == 1
    assert "air-1 (air-1-vm-1): last seen" in pool[0][1] and "linux-ci" in pool[0][1]
    clock.t += 300
    wd.cycle()
    assert len([s for s in sink.sent if s[0] == "CI pool offline"]) == 1  # once per episode


def test_pool_not_offline_when_a_runner_serves_the_labels():
    wd, gh, sink, clock = make()
    gh.runners = [runner("win-1-wsl-1", ["self-hosted", "Linux", "linux-ci"])]
    queue_one(gh, clock, 40)
    wd.cycle()
    clock.t += 3600
    queue_one(gh, clock, 100)
    wd.cycle()
    assert not [s for s in sink.sent if s[0] == "CI pool offline"]


def test_pool_episode_rearms_after_recovery():
    wd, gh, sink, clock = make()
    gh.runners = [runner("air-1", ["self-hosted", "mac-ci"], online=False)]
    queue_one(gh, clock, 1, ("self-hosted", "mac-ci"))
    wd.cycle()
    clock.t += 31 * 60
    wd.cycle()
    gh.runners = [runner("air-1", ["self-hosted", "mac-ci"])]
    wd.cycle()  # served: record cleared
    assert wd.state.d["unserved"] == {}
    gh.runners = [runner("air-1", ["self-hosted", "mac-ci"], online=False)]
    wd.cycle()
    clock.t += 31 * 60
    wd.cycle()
    assert len([s for s in sink.sent if s[0] == "CI pool offline"]) == 2


def test_failed_notification_is_retried_next_poll():
    wd, gh, sink, clock = make()
    gh.runners = [runner("air-1", ["self-hosted", "mac-ci"], online=False)]
    queue_one(gh, clock, 1, ("self-hosted", "mac-ci"))
    wd.cycle()
    clock.t += 31 * 60
    sink.ok = False
    wd.cycle()
    sink.ok = True
    wd.cycle()
    assert len([s for s in sink.sent if s[0] == "CI pool offline"]) == 1


# ---- rule 2: long queue


def test_long_queue_alerts_once_per_job_naming_repo_job_labels():
    wd, gh, sink, clock = make()
    gh.runners = [runner("win-1-wsl-1", ["self-hosted", "Linux", "linux-ci"])]
    queue_one(gh, clock, 10)
    wd.cycle()
    assert not sink.sent
    clock.t += 6 * 60
    wd.cycle()
    wd.cycle()
    q = [s for s in sink.sent if s[0] == "CI job queued too long"]
    assert len(q) == 1
    assert REPO in q[0][1] and "'unit'" in q[0][1] and "self-hosted, linux-ci" in q[0][1]


def test_in_progress_runs_with_queued_jobs_are_seen():
    wd, gh, sink, clock = make()
    gh.runners = [runner("win-1-wsl-1", ["self-hosted", "Linux", "linux-ci"])]
    gh.runs[(REPO, "in_progress")] = [{"id": 2, "name": "CI"}]
    gh.jobs[2] = [job(21, "lint", status="in_progress"), job(22, "slow", created_at=iso(clock.t - 1200))]
    wd.cycle()
    assert [s for s in sink.sent if s[0] == "CI job queued too long"]


# ---- rule 3: lost runner


def failed_run(gh, clock, jobs, run_id=5, attempt=1, ann=None):
    gh.runs[(REPO, "failure")] = [{"id": run_id, "run_attempt": attempt, "updated_at": iso(clock.t - 60)}]
    gh.jobs[run_id] = jobs
    gh.annotations.update(ann or {})


LOST = [
    {
        "message": "The self-hosted runner: air-1-vm-1 lost communication with the server. Verify the machine is running",
        "annotation_level": "failure",
    }
]


def lost_job(jid=50, name="unit"):
    return job(
        jid,
        name,
        "completed",
        "failure",
        completed_at="2026-10-03T11:59:00Z",
        steps=[{"name": "Run tests", "status": "in_progress", "conclusion": None, "started_at": "x"}],
    )


def test_lost_runner_job_is_rerun_once_and_never_again():
    wd, gh, sink, clock = make()
    failed_run(gh, clock, [lost_job()], ann={50: LOST})
    wd.cycle()
    assert gh.posts == [f"/repos/{REPO}/actions/jobs/50/rerun"]
    for _ in range(3):
        clock.t += 60
        wd.cycle()
    assert len(gh.posts) == 1
    assert not sink.sent  # a successful re-run alerts nobody


def test_rerun_not_repeated_after_restart(tmp_path):
    wd, gh, sink, clock = make(tmp_path)
    failed_run(gh, clock, [lost_job()], ann={50: LOST})
    wd.cycle()
    wd2 = W.Watchdog(wd.cfg, gh, sink, W.State(tmp_path / "state.json"), clock)
    wd2.state.d["examined"].clear()  # even if the examined list were lost
    wd2.cycle()
    assert len(gh.posts) == 1


def test_rerun_record_written_before_request_so_unclear_error_never_repeats():
    wd, gh, sink, clock = make()
    failed_run(gh, clock, [lost_job()], ann={50: LOST})

    def boom(method, path, params=None):
        if method == "POST":
            gh.posts.append(path)
            raise W.GitHubError("timeout")
        return FakeGH.request(gh, method, path, params)

    gh.request = boom
    wd.cycle()
    wd.state.d["examined"].clear()
    wd.cycle()
    assert len(gh.posts) == 1


def test_rerun_refused_is_retried_then_given_up_with_alert():
    wd, gh, sink, clock = make(WATCHDOG_RERUN_MAX_TRIES="3")
    failed_run(gh, clock, [lost_job()], ann={50: LOST})
    gh.rerun_status = 403
    for _ in range(2):
        wd.cycle()
        clock.t += 60
    assert len(gh.posts) == 2 and not sink.sent
    gh.rerun_status = 201
    wd.cycle()  # accepted on the third try
    assert len(gh.posts) == 3 and not sink.sent
    wd.cycle()
    assert len(gh.posts) == 3


def test_rerun_refused_every_time_alerts_once():
    wd, gh, sink, clock = make(WATCHDOG_RERUN_MAX_TRIES="2")
    failed_run(gh, clock, [lost_job()], ann={50: LOST})
    gh.rerun_status = 403
    for _ in range(5):
        wd.cycle()
        clock.t += 60
    assert len(gh.posts) == 2
    assert [s[0] for s in sink.sent] == ["CI re-run refused"]


def test_second_failure_of_the_rerun_alerts_and_does_not_rerun_again():
    wd, gh, sink, clock = make()
    failed_run(gh, clock, [lost_job()], ann={50: LOST})
    wd.cycle()
    assert len(gh.posts) == 1
    # attempt 2 of the same run: a new job id, lost again
    failed_run(gh, clock, [lost_job(jid=60)], attempt=2, ann={60: LOST})
    wd.cycle()
    wd.cycle()
    assert len(gh.posts) == 1
    assert [s[0] for s in sink.sent] == ["CI re-run also failed"]
    assert "lost its runner again" in sink.sent[0][1]


def test_rerun_failing_for_real_reason_alerts():
    wd, gh, sink, clock = make()
    failed_run(gh, clock, [lost_job()], ann={50: LOST})
    wd.cycle()
    real = job(
        61,
        "unit",
        "completed",
        "failure",
        steps=[{"name": "t", "status": "completed", "conclusion": "failure", "started_at": "x", "completed_at": "y"}],
    )
    failed_run(gh, clock, [real], attempt=2)
    wd.cycle()
    assert [s[0] for s in sink.sent] == ["CI re-run also failed"]
    assert len(gh.posts) == 1


def test_rerun_that_succeeds_is_silent():
    wd, gh, sink, clock = make()
    failed_run(gh, clock, [lost_job()], ann={50: LOST})
    wd.cycle()
    gh.runs[(REPO, "failure")] = []
    wd.cycle()
    assert not sink.sent


def test_real_failure_is_never_rerun():
    wd, gh, sink, clock = make()
    real = job(
        70,
        "unit",
        "completed",
        "failure",
        completed_at="2026-10-03T11:59:00Z",
        steps=[
            {"name": "pytest", "status": "completed", "conclusion": "failure", "started_at": "x", "completed_at": "y"}
        ],
    )
    failed_run(
        gh, clock, [real], ann={70: [{"message": "Process completed with exit code 1.", "annotation_level": "failure"}]}
    )
    wd.cycle()
    assert gh.posts == [] and not sink.sent


def test_user_cancel_is_not_a_lost_runner():
    wd, gh, sink, clock = make()
    c = job(
        71,
        "unit",
        "completed",
        "cancelled",
        steps=[{"name": "t", "status": "completed", "conclusion": "cancelled", "started_at": "x", "completed_at": "y"}],
    )
    failed_run(gh, clock, [c], ann={71: [{"message": "The operation was canceled.", "annotation_level": "failure"}]})
    wd.cycle()
    assert gh.posts == []


def test_shutdown_signal_annotation_counts_even_when_cancelled():
    wd, gh, sink, clock = make()
    c = job(72, "unit", "completed", "cancelled", completed_at="2026-10-03T11:59:00Z")
    failed_run(
        gh,
        clock,
        [c],
        ann={
            72: [
                {
                    "message": "The runner has received a shutdown signal. This can happen when the runner service is stopped",
                    "annotation_level": "failure",
                }
            ]
        },
    )
    wd.cycle()
    assert len(gh.posts) == 1


def test_unfinished_step_without_annotation_still_detected():
    assert W.is_lost_runner(lost_job(), [])
    assert not W.is_lost_runner(lost_job(), [{"message": "Process completed with exit code 2."}])


def test_old_failures_are_ignored():
    wd, gh, sink, clock = make()
    failed_run(gh, clock, [lost_job()], ann={50: LOST})
    gh.runs[(REPO, "failure")][0]["updated_at"] = iso(clock.t - 3 * 86400)
    wd.cycle()
    assert gh.posts == []


def test_job_that_finished_long_ago_is_not_rerun():
    wd, gh, sink, clock = make()
    old = lost_job()
    old["completed_at"] = iso(clock.t - 3 * 3600)
    failed_run(gh, clock, [old], ann={50: LOST})
    wd.cycle()
    assert gh.posts == []


# ---- rule 4: stale runner


def test_stale_runner_alerts_after_three_days_once():
    wd, gh, sink, clock = make()
    gh.runners = [runner("air-2", ["self-hosted", "mac-ci"])]
    wd.cycle()
    gh.runners = [runner("air-2", ["self-hosted", "mac-ci"], online=False)]
    clock.t += 2 * 86400
    wd.cycle()
    assert not sink.sent
    clock.t += 1.5 * 86400
    wd.cycle()
    wd.cycle()
    assert [s[0] for s in sink.sent] == ["Stale CI runner"]
    assert "air-2" in sink.sent[0][1]


def test_stale_clock_starts_at_first_sighting_and_survives_restart(tmp_path):
    wd, gh, sink, clock = make(tmp_path)
    gh.runners = [runner("win-1", ["self-hosted", "win-ci"], online=False)]
    wd.cycle()
    clock.t += 4 * 86400
    wd2 = W.Watchdog(wd.cfg, gh, sink, W.State(tmp_path / "state.json"), clock)
    wd2.cycle()
    assert [s[0] for s in sink.sent] == ["Stale CI runner"]


def test_runner_back_online_rearms_stale():
    wd, gh, sink, clock = make()
    gh.runners = [runner("air-2", ["x"], online=False)]
    wd.cycle()
    clock.t += 4 * 86400
    wd.cycle()
    gh.runners = [runner("air-2", ["x"])]
    wd.cycle()
    assert wd.state.d["runners"]["air-2"]["stale_alerted"] is False


# ---- rule 5: digest


def digest_data(gh, clock):
    base = clock.t - 3600
    gh.all_runs = [{"id": 1, "conclusion": "success"}, {"id": 2, "conclusion": "failure"}]
    gh.jobs[1] = [
        job(
            1,
            status="completed",
            conclusion="success",
            runner_name="air-1-vm-1",
            created_at=iso(base),
            started_at=iso(base + 60),
        )
    ]
    gh.jobs[2] = [
        job(
            2,
            status="completed",
            conclusion="failure",
            runner_name="win-1-wsl-1",
            created_at=iso(base),
            started_at=iso(base + 600),
        ),
        job(
            3,
            status="completed",
            conclusion="success",
            runner_name="air-1",
            created_at=iso(base),
            started_at=iso(base + 120),
        ),
    ]


def test_digest_waits_for_the_hour_then_sends_once():
    wd, gh, sink, clock = make(WATCHDOG_DIGEST_HOUR="9", WATCHDOG_TZ="Australia/Sydney")
    clock.t = datetime(
        2026, 10, 3, 20, 0, tzinfo=timezone.utc
    ).timestamp()  # 07:00 in the fixture zone (AEST+10 before DST starts on 4 Oct)
    digest_data(gh, clock)
    wd.cycle()
    assert not sink.sent
    clock.t += 3 * 3600  # 10:00 in the fixture zone
    digest_data(gh, clock)
    wd.cycle()
    wd.cycle()
    d = [s for s in sink.sent if s[0] == "CI daily digest"]
    assert len(d) == 1
    text = d[0][1]
    assert "2 run(s)" in text
    assert "1 success" in text and "1 failure" in text
    assert "median 2 min, max 10 min over 3 job(s)" in text
    assert "air-1 2" in text and "win-1 1" in text


def test_digest_with_no_runs():
    wd, gh, sink, clock = make()
    assert "0 run(s)" in wd.digest_text(clock.t)
    assert "no jobs started" in wd.digest_text(clock.t)


# ---- resilience, config, clients


def test_api_down_alerts_after_n_cycles_once():
    wd, gh, sink, clock = make(WATCHDOG_API_FAIL_ALERT_CYCLES="3", WATCHDOG_POLL_SECONDS="0")
    gh.fail_all = True

    class Stop:
        n = 0

        def is_set(self):
            self.n += 1
            return self.n > 6

        def wait(self, s):
            pass

    class Beat:
        def write_text(self, *a, **k):
            pass

    wd.run_forever(Stop(), Beat())
    assert [s[0] for s in sink.sent] == ["CI watchdog is blind"]


def test_archived_repos_are_skipped():
    wd, gh, sink, clock = make()
    assert wd.repos() == [REPO]


def test_config_defaults_and_overrides():
    c = W.Config({})
    assert (c.pool_offline_minutes, c.queue_minutes, c.stale_days, c.digest_hour) == (30, 15, 3.0, 7)
    c = W.Config({"WATCHDOG_QUEUE_MINUTES": "5", "WATCHDOG_REPOS": "a/b c/d"})
    assert c.queue_minutes == 5 and c.repos == ["a/b", "c/d"]


def test_secrets_come_from_files_and_are_never_logged(tmp_path, capsys):
    (tmp_path / "t").write_text("sekrit-topic\n")
    env = {"WATCHDOG_NTFY_TOPIC_FILE": str(tmp_path / "t")}
    assert W.read_secret("WATCHDOG_NTFY_TOPIC", "WATCHDOG_NTFY_TOPIC_FILE") == ""  # not in os.environ here
    import os

    old = dict(os.environ)
    os.environ.update(env, GH_TOKEN="ghp_supersecret")
    try:
        cfg = W.Config().load_secrets()
    finally:
        os.environ.clear()
        os.environ.update(old)
    assert cfg.ntfy_topic == "sekrit-topic"

    class Boom(Exception):
        pass

    def opener(req, timeout):
        raise W.urllib.error.URLError("nope")

    gh = W.GitHub("ghp_supersecret", opener=opener)
    try:
        gh.request("GET", "/x")
    except W.GitHubError as e:
        assert "ghp_" not in str(e)
    n = W.Ntfy("https://ntfy.example", "sekrit-topic", "tk_secret", opener=opener)
    assert n.send("t", "m") is False
    out = capsys.readouterr().out
    assert "ghp_supersecret" not in out and "tk_secret" not in out


def test_ntfy_request_shape():
    seen = {}

    class R:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

    def opener(req, timeout):
        seen["url"], seen["body"], seen["h"] = req.full_url, req.data, dict(req.header_items())
        return R()

    assert W.Ntfy("https://ntfy.example/", "ci-alerts", "tk", opener=opener).send(
        "CI pool offline", "body", "urgent", "x"
    )
    assert seen["url"] == "https://ntfy.example/ci-alerts" or seen["url"].startswith("https://ntfy.example")
    assert seen["body"] == b"body" and seen["h"]["Priority"] == "urgent" and seen["h"]["Authorization"] == "Bearer tk"


def test_no_topic_only_logs():
    assert W.Ntfy("https://ntfy.example", "").send("t", "m") is True


def test_state_round_trip_and_corrupt_file(tmp_path):
    s = W.State(tmp_path / "s.json")
    s.d["digest_date"] = "2026-10-03"
    s.save()
    assert W.State(tmp_path / "s.json").d["digest_date"] == "2026-10-03"
    (tmp_path / "s.json").write_text("{not json")
    assert W.State(tmp_path / "s.json").d["reruns"] == {}


def test_github_client_accepts_a_token_callable():
    seen = []

    class R:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b"{}"

    def opener(req, timeout=None):
        seen.append(req.get_header("Authorization"))
        return R()

    tokens = iter(["one", "two"])
    gh = W.GitHub(lambda: next(tokens), opener=opener)
    gh.request("GET", "/x")
    gh.request("GET", "/x")
    assert seen == ["Bearer one", "Bearer two"]


def test_github_client_turns_a_missing_token_into_github_error():
    def no_token():
        raise W.gh_app_token.AppTokenError("HTTP 401")

    try:
        W.GitHub(no_token).request("GET", "/x")
    except W.GitHubError as e:
        assert "HTTP 401" in str(e)
    else:
        raise AssertionError("expected GitHubError")
