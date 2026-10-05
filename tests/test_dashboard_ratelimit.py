"""The dashboard stops calling gh for a while after GitHub says the rate limit is spent."""

import importlib.util
import pathlib
import subprocess

SERVER = pathlib.Path(__file__).parent.parent / "dashboard/server.py"


def load():
    spec = importlib.util.spec_from_file_location("dashboard_server_ratelimit", SERVER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_rate_limit_error_pauses_gh_calls(monkeypatch):
    s = load()
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 1, "", "gh: API rate limit exceeded for installation ID 1 (HTTP 403)")

    monkeypatch.setattr(s.subprocess, "run", fake_run)
    rc, _, err = s.run(["gh", "api", "x"], 5)
    assert rc == 1 and "rate limit" in err
    rc, _, err = s.run(["gh", "api", "y"], 5)
    assert rc == 1 and "backing off" in err
    assert len(calls) == 1  # the second call never reached gh


def test_other_failures_and_other_programs_do_not_pause(monkeypatch):
    s = load()
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 1, "", "HTTP 404")

    monkeypatch.setattr(s.subprocess, "run", fake_run)
    s.run(["gh", "api", "x"], 5)
    s.run(["gh", "api", "y"], 5)
    s.run(["ssh", "host"], 5)
    assert len(calls) == 3
