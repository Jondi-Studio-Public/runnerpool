"""The dashboard's runner history: which runner ran each job and how fast."""

import importlib.util
import pathlib

SERVER = pathlib.Path(__file__).parent.parent / "dashboard/server.py"


def load():
    spec = importlib.util.spec_from_file_location("dashboard_server_jobs", SERVER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def job(runner, secs, minute, name="unit", repo="o/r", conclusion="success"):
    return {
        "repo": repo,
        "run_id": 1,
        "name": name,
        "runner": runner,
        "conclusion": conclusion,
        "started": f"2026-10-01T10:{minute:02d}:00Z",
        "completed": f"2026-10-01T10:{minute:02d}:{secs}Z" if secs < 60 else f"2026-10-01T11:{minute:02d}:00Z",
        "url": "https://github.com/x",
    }


def test_compares_the_same_job_across_runners():
    s = load()
    jobs = [job("pc-1", 30, 1), job("pc-1", 40, 2), job("air-1", 52, 3), job("air-1", 58, 4)]
    out = s.summarize_jobs(jobs)
    (row,) = out["compare"]
    assert row["job"] == "unit"
    assert row["runners"]["pc-1"]["median_s"] == 35
    assert row["runners"]["air-1"]["median_s"] == 55
    assert row["runners"]["air-1"]["trend"] == [52, 58]  # oldest first
    assert {r["runner"]: r["jobs"] for r in out["runners"]} == {"pc-1": 2, "air-1": 2}


def test_skips_jobs_with_no_runner_or_no_times_and_single_runner_jobs():
    s = load()
    jobs = [
        job("", 30, 1),
        dict(job("pc-1", 30, 2), completed=None),
        job("pc-1", 30, 3, name="only-pc"),
        job("pc-1", 30, 4, name="web"),
        job("air-1", 30, 5, name="web"),
    ]
    out = s.summarize_jobs(jobs)
    assert [c["job"] for c in out["compare"]] == ["web"]
    assert len(out["jobs"]) == 3


def test_failed_jobs_count_but_do_not_skew_the_comparison():
    s = load()
    jobs = [job("pc-1", 10, 1), job("air-1", 20, 2), job("air-1", 5, 3, conclusion="failure")]
    out = s.summarize_jobs(jobs)
    assert out["compare"][0]["runners"]["air-1"]["median_s"] == 20
    assert [r for r in out["runners"] if r["runner"] == "air-1"][0]["failed"] == 1
