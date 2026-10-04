#!/usr/bin/env python3
"""CI watchdog for the example-org self-hosted runners.

Runs on the home server next to the dashboard (same image, same GitHub token). Every minute it reads
the GitHub REST API (org runners, queued and in-progress runs and their jobs, recently failed runs)
and raises phone alerts through ntfy:

  1. pool offline   no online runner carries the labels a queued job needs, for 30 minutes
  2. long queue     a job queued for over 15 minutes (once per job)
  3. lost runner    a job that failed because its runner lost contact is re-run ONCE; alert only
                    if the re-run fails too. Any other failure is real and never re-run.
  4. stale runner   a runner offline for 3 days
  5. daily digest   one message each morning: runs, queue times, jobs per device

Config is environment variables (WATCHDOG_*, see docs/watchdog.md). Tokens are read from files or
the environment and are never printed or logged. Python 3.9 compatible, standard library only.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import statistics
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "dashboard"))
import gh_app_token  # noqa: E402  (shared with the dashboard; the image copies both directories)

API = "https://api.github.com"
# Failure annotations GitHub puts on a job whose runner went away mid-job. Matched case-insensitively.
LOST_PATTERNS = (
    "lost communication with the server",
    "received a shutdown signal",
    "runner has been shut down",
    "no longer running",
    "runner application exited unexpectedly",
)
FAILED = {"failure", "cancelled", "timed_out"}
KEEP_DAYS = 7


def log(msg):
    print(f"{datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')} {msg}", flush=True)


def _env_int(name, default):
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return int(default)


def read_secret(env_name, file_name):
    """A value from the environment, else from a secret file; empty string if neither is set."""
    v = os.environ.get(env_name, "").strip()
    if v:
        return v
    path = os.environ.get(file_name, "")
    if path:
        try:
            return Path(path).read_text(encoding="utf-8").strip()
        except OSError:
            return ""
    return ""


class Config:
    def __init__(self, env=None):
        e = env if env is not None else os.environ
        g = e.get
        self.org = g("WATCHDOG_ORG", "example-org")
        self.repos = g("WATCHDOG_REPOS", "").split()  # empty = every unarchived repo in the org
        self.poll_seconds = int(g("WATCHDOG_POLL_SECONDS", "60"))
        self.pool_offline_minutes = int(g("WATCHDOG_POOL_OFFLINE_MINUTES", "30"))
        self.queue_minutes = int(g("WATCHDOG_QUEUE_MINUTES", "15"))
        self.stale_days = float(g("WATCHDOG_STALE_DAYS", "3"))
        self.digest_hour = int(g("WATCHDOG_DIGEST_HOUR", "7"))
        self.timezone = g("WATCHDOG_TZ", "UTC")
        self.rerun_max_age_minutes = int(g("WATCHDOG_RERUN_MAX_AGE_MINUTES", "120"))
        self.rerun_max_tries = int(g("WATCHDOG_RERUN_MAX_TRIES", "5"))
        self.api_fail_alert_cycles = int(g("WATCHDOG_API_FAIL_ALERT_CYCLES", "10"))
        self.data_dir = Path(g("WATCHDOG_DATA_DIR", "/data"))
        self.ntfy_url = g("WATCHDOG_NTFY_URL", "https://ntfy.sh").rstrip("/")
        self.ntfy_topic = ""
        self.ntfy_token = ""
        self.gh_token = ""
        self.tokens = gh_app_token.TokenProvider()

    def load_secrets(self):
        self.gh_token = read_secret("GH_TOKEN", "WATCHDOG_GH_TOKEN_FILE")
        # The GitHub App's installation token when its id and key are provisioned, else gh_token.
        self.tokens = gh_app_token.TokenProvider.from_env(pat=self.gh_token)
        self.ntfy_topic = read_secret("WATCHDOG_NTFY_TOPIC", "WATCHDOG_NTFY_TOPIC_FILE")
        self.ntfy_token = read_secret("WATCHDOG_NTFY_TOKEN", "WATCHDOG_NTFY_TOKEN_FILE")
        return self


# ---------------------------------------------------------------- GitHub and ntfy clients


class GitHubError(Exception):
    pass


class GitHub:
    """Minimal REST client. request() -> (status, json or None); only the path is ever logged.
    token is a string or a zero-argument callable returning the current one."""

    def __init__(self, token, base=API, opener=None):
        self._token = token
        self.base = base
        self._open = opener or urllib.request.urlopen

    def request(self, method, path, params=None):
        url = self.base + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, method=method)
        try:
            token = self._token() if callable(self._token) else self._token
        except gh_app_token.AppTokenError as e:
            raise GitHubError(f"{method} {path}: no token ({e})") from None
        req.add_header("Authorization", "Bearer " + token)
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("X-GitHub-Api-Version", "2022-11-28")
        req.add_header("User-Agent", "git-runner-watchdog")
        try:
            with self._open(req, timeout=30) as r:
                body = r.read()
                return r.status, (json.loads(body) if body else None)
        except urllib.error.HTTPError as e:
            try:
                data = json.loads(e.read() or b"null")
            except ValueError:
                data = None
            return e.code, data
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise GitHubError(f"{method} {path}: {type(e).__name__}") from None


class Ntfy:
    def __init__(self, url, topic, token="", opener=None):
        self.url, self.topic, self.token = url, topic, token
        self._open = opener or urllib.request.urlopen

    def send(self, title, message, priority="default", tags=""):
        """True once the message is delivered (or when no topic is configured and it is only logged)."""
        log(f"ALERT [{priority}] {title}: {message.splitlines()[0] if message else ''}")
        if not self.topic:
            return True
        req = urllib.request.Request(
            f"{self.url}/{urllib.parse.quote(self.topic, safe='')}", data=message.encode("utf-8"), method="POST"
        )
        req.add_header("Title", title.encode("ascii", "replace").decode())
        req.add_header("Priority", priority)
        if tags:
            req.add_header("Tags", tags)
        if self.token:
            req.add_header("Authorization", "Bearer " + self.token)
        try:
            with self._open(req, timeout=15) as r:
                return 200 <= r.status < 300
        except (urllib.error.URLError, OSError) as e:
            log(f"ntfy send failed: {type(e).__name__}")
            return False


# ---------------------------------------------------------------- helpers


def parse_ts(v):
    if not v:
        return None
    try:
        return datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def device_of(runner_name):
    """air-1-vm-1 -> air-1, win-1-wsl-2 / wsl-1-admin / win-1-ci-2 -> win-1, examplepc-app-1 -> examplepc."""
    n = (runner_name or "").lower()
    m = re.match(r"^(air-\d+)(?:-|$)", n) or re.match(r"^(?:win|wsl)-(\d+)(?:-|$)", n)
    if m:
        return m.group(1) if m.group(1).startswith("air") else "win-" + m.group(1)
    return n.split("-")[0] or n


def labels_of(obj):
    out = []
    for lab in obj.get("labels") or []:
        name = lab.get("name") if isinstance(lab, dict) else lab
        out.append((name or "").lower())
    return [x for x in out if x]


def label_key(labels):
    return ",".join(sorted(labels))


def human(seconds):
    s = int(seconds)
    if s < 90:
        return f"{s}s"
    if s < 5400:
        return f"{round(s / 60)} min"
    if s < 172800:
        return f"{s / 3600:.1f} h"
    return f"{s / 86400:.1f} days"


def fmt_when(ts, tz):
    return datetime.fromtimestamp(ts, tz).strftime("%a %d %b %H:%M")


def is_lost_runner(job, annotations):
    """True when a failed job's runner went away rather than the job failing by itself.

    GitHub shows this as a failure annotation on the job's check run ("The self-hosted runner: NAME
    lost communication with the server..." or "The runner has received a shutdown signal..."). When
    annotations are unavailable, the same event leaves the job failed with no failed step and a step
    that was started but never finished (no completed_at, or cancelled). A job with any failed step
    is a real failure."""
    if job.get("conclusion") not in FAILED:
        return False
    for a in annotations or []:
        text = f"{a.get('message') or ''} {a.get('title') or ''}".lower()
        if any(p in text for p in LOST_PATTERNS):
            return True
    steps = job.get("steps") or []
    if job.get("conclusion") != "failure" or not steps or annotations:
        return False
    if any(s.get("conclusion") in ("failure", "timed_out") for s in steps):
        return False
    return any(s.get("status") != "completed" or not s.get("completed_at") for s in steps if s.get("started_at"))


# ---------------------------------------------------------------- state


class State:
    """Everything that must survive a restart, in one JSON file written atomically."""

    def __init__(self, path):
        self.path = Path(path) if path else None
        self.d = {}
        if self.path:
            try:
                self.d = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                self.d = {}
        for k in ("runners", "unserved", "queue_alerted", "reruns", "rerun_tries", "examined"):
            self.d.setdefault(k, {})
        self.d.setdefault("digest_date", "")
        self.d.setdefault("api_down_alerted", False)

    def save(self):
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.d, indent=1, sort_keys=True), encoding="utf-8")
        os.replace(tmp, self.path)

    def prune(self, now):
        cutoff = now - KEEP_DAYS * 86400
        for k in ("queue_alerted", "examined"):
            self.d[k] = {a: t for a, t in self.d[k].items() if t >= cutoff}
        self.d["reruns"] = {a: r for a, r in self.d["reruns"].items() if r.get("at", 0) >= cutoff}


# ---------------------------------------------------------------- the watchdog


class Watchdog:
    def __init__(self, cfg, gh, notify, state, clock=time.time):
        self.cfg, self.gh, self.notify, self.state, self.now = cfg, gh, notify, state, clock
        try:
            from zoneinfo import ZoneInfo

            self.tz = ZoneInfo(cfg.timezone)
        except Exception:  # missing tzdata: fall back to a fixed offset rather than not running
            log(f"timezone {cfg.timezone} unavailable, using UTC+10")
            self.tz = timezone(timedelta(hours=10))
        self._repos = ([], 0.0)

    # --- API reads

    def get(self, path, params=None):
        status, data = self.gh.request("GET", path, params)
        if status == 403 and isinstance(data, dict) and "rate limit" in str(data.get("message", "")).lower():
            raise GitHubError("rate limited")
        if status >= 400:
            raise GitHubError(f"GET {path}: HTTP {status}")
        return data

    def get_all(self, path, key, params=None, max_pages=10):
        out = []
        for page in range(1, max_pages + 1):
            data = self.get(path, dict(params or {}, per_page=100, page=page))
            items = data.get(key, []) if isinstance(data, dict) else data
            out += items
            if len(items) < 100:
                break
        return out

    def repos(self):
        if self.cfg.repos:
            return self.cfg.repos
        repos, at = self._repos
        if not repos or self.now() - at > 600:
            data = self.get_all(f"/orgs/{self.cfg.org}/repos", None, {"type": "all"})
            repos = sorted(r["full_name"] for r in data if not r.get("archived") and not r.get("disabled"))
            self._repos = (repos, self.now())
        return repos

    def runs(self, repo, **params):
        data = self.get(f"/repos/{repo}/actions/runs", dict(params, per_page=100))
        return data.get("workflow_runs", [])

    def jobs(self, repo, run_id):
        return self.get_all(f"/repos/{repo}/actions/runs/{run_id}/jobs", "jobs", {"filter": "latest"}, 3)

    # --- one cycle

    def cycle(self):
        now = self.now()
        runners = self.get_all(f"/orgs/{self.cfg.org}/actions/runners", "runners")
        self.track_runners(runners, now)
        queued = self.queued_jobs()
        self.check_pool(runners, queued, now)
        self.check_queue(queued, now)
        self.check_stale(runners, now)
        self.check_failures(now)
        self.maybe_digest(now)
        self.state.prune(now)
        self.state.save()

    def run_forever(self, stop, heartbeat):
        fails = 0
        while not stop.is_set():
            try:
                self.cycle()
                fails = 0
                self.state.d["api_down_alerted"] = False
            except GitHubError as e:
                fails += 1
                log(f"cycle failed ({fails}): {e}")
                if fails >= self.cfg.api_fail_alert_cycles and not self.state.d["api_down_alerted"]:
                    if self.notify.send(
                        "CI watchdog is blind",
                        f"The watchdog could not read GitHub for {fails} polls in a row ({e}). "
                        "Check the macs-dashboard host and the GH token.",
                        "high",
                        "warning",
                    ):
                        self.state.d["api_down_alerted"] = True
                        self.state.save()
            except Exception as e:  # a bug must not kill the loop
                log(f"cycle error: {type(e).__name__}: {e}")
            try:
                heartbeat.write_text(str(int(self.now())), encoding="utf-8")
            except OSError:
                pass
            stop.wait(self.cfg.poll_seconds)

    # --- runners and pools

    def track_runners(self, runners, now):
        known = self.state.d["runners"]
        for r in runners:
            name = r["name"]
            rec = known.setdefault(name, {"first_seen": now, "last_online": None, "stale_alerted": False})
            if r.get("status") == "online":
                rec["last_online"] = now
                rec["stale_alerted"] = False
        live = {r["name"] for r in runners}
        for name in [n for n in known if n not in live]:
            del known[name]  # deregistered: nothing to clean up any more

    def queued_jobs(self):
        """Every queued job across the org: [{repo, job, labels, waited_s}]."""
        seen, out = set(), []
        now = self.now()
        for repo in self.repos():
            for status in ("queued", "in_progress"):
                for run in self.runs(repo, status=status):
                    if run["id"] in seen:
                        continue
                    seen.add(run["id"])
                    for job in self.jobs(repo, run["id"]):
                        if job.get("status") == "queued":
                            t = parse_ts(job.get("created_at")) or now
                            out.append(
                                {
                                    "repo": repo,
                                    "job": job,
                                    "labels": labels_of(job),
                                    "waited": max(0, now - t),
                                    "name": job.get("name", ""),
                                    "run_name": run.get("name", ""),
                                }
                            )
        return out

    def check_pool(self, runners, queued, now):
        unserved = self.state.d["unserved"]
        needed = {}
        for q in queued:
            if not q["labels"]:
                continue
            if any(set(q["labels"]) <= set(labels_of(r)) for r in runners if r.get("status") == "online"):
                continue
            needed.setdefault(label_key(q["labels"]), []).append(q)
        for key in [k for k in unserved if k not in needed]:
            del unserved[key]  # served again or nothing waiting: re-arm
        for key, jobs in needed.items():
            rec = unserved.setdefault(key, {"since": now, "alerted": False})
            if rec["alerted"] or now - rec["since"] < self.cfg.pool_offline_minutes * 60:
                continue
            labels = set(key.split(","))
            carriers = [r for r in runners if labels <= set(labels_of(r))]
            by_dev = {}
            for r in carriers:
                by_dev.setdefault(device_of(r["name"]), []).append(r["name"])
            lines = []
            for dev, names in sorted(by_dev.items()):
                seen = [self.state.d["runners"].get(n, {}).get("last_online") for n in names]
                seen = [s for s in seen if s]
                when = f"last seen {fmt_when(max(seen), self.tz)}" if seen else "not seen since the watchdog started"
                lines.append(f"{dev} ({', '.join(sorted(names))}): {when}")
            if not lines:
                lines = ["no registered runner carries all of these labels"]
            msg = (
                f"No online runner for labels [{', '.join(sorted(labels))}] for {human(now - rec['since'])}; "
                f"{len(jobs)} job(s) waiting.\n" + "\n".join(lines)
            )
            if self.notify.send("CI pool offline", msg, "urgent", "rotating_light"):
                rec["alerted"] = True

    def check_queue(self, queued, now):
        alerted = self.state.d["queue_alerted"]
        for q in queued:
            key = f"{q['repo']}#{q['job'].get('id')}"
            if q["waited"] < self.cfg.queue_minutes * 60 or key in alerted:
                continue
            msg = (
                f"{q['repo']}: job '{q['name']}' has waited {human(q['waited'])} for a runner with labels "
                f"[{', '.join(q['labels'])}].\n{q['job'].get('html_url', '')}"
            )
            if self.notify.send("CI job queued too long", msg, "high", "hourglass"):
                alerted[key] = now

    def check_stale(self, runners, now):
        for r in runners:
            rec = self.state.d["runners"].get(r["name"])
            if not rec or r.get("status") == "online" or rec["stale_alerted"]:
                continue
            since = rec["last_online"] or rec["first_seen"]
            if now - since < self.cfg.stale_days * 86400:
                continue
            seen = (
                f"last seen online {fmt_when(rec['last_online'], self.tz)}"
                if rec["last_online"]
                else (f"offline since the watchdog first saw it, {fmt_when(rec['first_seen'], self.tz)}")
            )
            msg = f"Runner {r['name']} (device {device_of(r['name'])}) has been offline {human(now - since)}; {seen}. Clean up its registration if it is gone."
            if self.notify.send("Stale CI runner", msg, "default", "wastebasket"):
                rec["stale_alerted"] = True

    # --- failed jobs: lost runner

    def check_failures(self, now):
        s = self.state.d
        horizon = now - self.cfg.rerun_max_age_minutes * 60 * 2
        for repo in self.repos():
            for conclusion in ("failure", "cancelled"):
                for run in self.runs(repo, status=conclusion):
                    t = parse_ts(run.get("updated_at")) or 0
                    ekey = f"{repo}#{run['id']}#{run.get('run_attempt', 1)}"
                    if t < horizon or ekey in s["examined"]:
                        continue
                    if self.examine_run(repo, run, now):
                        s["examined"][ekey] = now
            self.state.save()

    def annotations(self, repo, job_id):
        try:
            data = self.get(f"/repos/{repo}/check-runs/{job_id}/annotations")
            return data if isinstance(data, list) else []
        except GitHubError:
            return None  # unknown; is_lost_runner falls back to the step evidence

    def examine_run(self, repo, run, now):
        """Look at one finished run's failed jobs. True when nothing is left to retry later."""
        done = True
        for job in self.jobs(repo, run["id"]):
            if job.get("conclusion") not in FAILED:
                continue
            key = f"{repo}/{run['id']}/{job.get('name')}"
            rec = self.state.d["reruns"].get(key)
            if rec:
                self.check_rerun_result(repo, job, key, rec)
                continue
            if not is_lost_runner(job, self.annotations(repo, job["id"])):
                continue  # a real failure: never re-run
            ended = parse_ts(job.get("completed_at")) or now
            if now - ended > self.cfg.rerun_max_age_minutes * 60:
                continue
            if not self.rerun(repo, job, key, now):
                done = False
        return done

    def rerun(self, repo, job, key, now):
        """Re-run a lost-runner job once. The record is written BEFORE the request, so a crash or an
        unclear network error can never lead to a second re-run; only a definite refusal clears it."""
        reruns, tries = self.state.d["reruns"], self.state.d["rerun_tries"]
        reruns[key] = {"at": now, "job_id": job["id"], "repo": repo, "name": job.get("name"), "alerted": False}
        self.state.save()
        try:
            status, data = self.gh.request("POST", f"/repos/{repo}/actions/jobs/{job['id']}/rerun")
        except GitHubError as e:
            log(f"re-run of {key} unclear ({e}); not retrying")
            return True
        if 200 <= status < 300:
            log(f"re-ran lost-runner job {key}")
            reruns[key]["accepted"] = True
            tries.pop(key, None)
            self.state.save()
            return True
        del reruns[key]  # refused (for example other jobs of the run still running): try again next poll
        n = tries[key] = tries.get(key, 0) + 1
        self.state.save()
        if n >= self.cfg.rerun_max_tries:
            reruns[key] = {"at": now, "job_id": job["id"], "repo": repo, "name": job.get("name"), "alerted": True}
            reason = (data or {}).get("message", f"HTTP {status}") if isinstance(data, dict) else f"HTTP {status}"
            self.notify.send(
                "CI re-run refused",
                f"{repo}: job '{job.get('name')}' lost its runner but GitHub refused the re-run ({reason}).\n{job.get('html_url', '')}",
                "high",
                "warning",
            )
            return True
        return False

    def check_rerun_result(self, repo, job, key, rec):
        """The job in this run is not the one we re-ran: the re-run itself ended badly. Alert once."""
        if rec.get("alerted") or job["id"] == rec.get("job_id"):
            return
        why = "lost its runner again" if is_lost_runner(job, self.annotations(repo, job["id"])) else "failed"
        msg = f"{repo}: job '{job.get('name')}' was re-run after losing its runner, and the re-run {why} ({job.get('conclusion')}).\n{job.get('html_url', '')}"
        if self.notify.send("CI re-run also failed", msg, "high", "x"):
            rec["alerted"] = True

    # --- daily digest

    def maybe_digest(self, now):
        local = datetime.fromtimestamp(now, self.tz)
        today = local.strftime("%Y-%m-%d")
        if local.hour < self.cfg.digest_hour or self.state.d["digest_date"] == today:
            return
        text = self.digest_text(now)
        if self.notify.send("CI daily digest", text, "low", "bar_chart"):
            self.state.d["digest_date"] = today

    def digest_text(self, now):
        since = now - 86400
        stamp = datetime.fromtimestamp(since, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        runs = {}
        waits, per_device = [], {}
        for repo in self.repos():
            rs = []
            for page in range(1, 6):
                data = self.get(f"/repos/{repo}/actions/runs", {"created": f">={stamp}", "per_page": 100, "page": page})
                batch = data.get("workflow_runs", [])
                rs += batch
                if len(batch) < 100:
                    break
            for run in rs:
                runs[run.get("conclusion") or run.get("status")] = (
                    runs.get(run.get("conclusion") or run.get("status"), 0) + 1
                )
                for job in self.jobs(repo, run["id"]):
                    a, b = parse_ts(job.get("created_at")), parse_ts(job.get("started_at"))
                    if a and b and b >= a:
                        waits.append(b - a)
                    dev = device_of(job["runner_name"]) if job.get("runner_name") else None
                    if dev:
                        per_device[dev] = per_device.get(dev, 0) + 1
        total = sum(runs.values())
        lines = [
            f"Last 24 hours: {total} run(s)"
            + (": " + ", ".join(f"{n} {k}" for k, n in sorted(runs.items())) if runs else "")
        ]
        if waits:
            lines.append(
                f"Queue time: median {human(statistics.median(waits))}, max {human(max(waits))} over {len(waits)} job(s)"
            )
        else:
            lines.append("Queue time: no jobs started")
        if per_device:
            lines.append("Jobs per device: " + ", ".join(f"{d} {n}" for d, n in sorted(per_device.items())))
        reran = sum(1 for r in self.state.d["reruns"].values() if r.get("at", 0) >= since and r.get("accepted"))
        if reran:
            lines.append(f"Lost-runner re-runs: {reran}")
        return "\n".join(lines)


# ---------------------------------------------------------------- entry point


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--healthcheck", action="store_true", help="exit 0 if the last poll was recent")
    ap.add_argument("--once", action="store_true", help="run one poll and exit")
    args = ap.parse_args(argv)
    cfg = Config().load_secrets()
    beat = cfg.data_dir / "heartbeat"
    if args.healthcheck:
        try:
            return 0 if time.time() - int(beat.read_text()) < max(300, cfg.poll_seconds * 5) else 1
        except (OSError, ValueError):
            return 1
    if not cfg.tokens.configured():
        log("no GitHub token (GH_TOKEN) or GitHub App credentials: cannot start")
        return 2
    if not cfg.ntfy_topic:
        log("no ntfy topic configured: alerts are only written to this log")
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    wd = Watchdog(
        cfg,
        GitHub(cfg.tokens.token),
        Ntfy(cfg.ntfy_url, cfg.ntfy_topic, cfg.ntfy_token),
        State(cfg.data_dir / "state.json"),
    )
    if args.once:
        wd.cycle()
        return 0
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    log(f"watchdog started: org {cfg.org}, every {cfg.poll_seconds}s")
    wd.run_forever(stop, beat)
    return 0


if __name__ == "__main__":
    sys.exit(main())
