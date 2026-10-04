#!/usr/bin/env python3
"""runner dashboard: a control panel for the Mac runners, served on this PC only.

Started by `./runner dashboard [--port N] [--no-open]`. It reads:
  * GitHub's runner lists (gh api), for every runner's online/busy state, PC ones included;
  * each PC's specs and settings, pushed by the PC itself (see push_info);
  * each Mac's specs and settings (`macrunner info`) over Tailscale SSH, or on request
    through the Admin workflow (`runner info HOST`) when SSH can't reach it.
Setting changes go over SSH too, and through the Admin workflow (`runner ...`) when SSH can't
connect. A Windows PC (`win-N`) is not polled: it pushes its own health JSON (`winrunner info`) to
POST /api/push-info every 30 s with its own bearer token (MACS_PUSH_TOKENS_FILE), and the page shows the
latest push. Its changes go through the Admin workflow. Every action names a Mac found through its
`<host>-admin` runner or a listed PC, and nothing else.

Only this PC can reach it (127.0.0.1), and every API call must carry the random token printed
in the URL, so no other web page can drive it: the first visit with ?t=TOKEN sets an HttpOnly,
SameSite=Strict cookie and redirects to a clean URL, and the API accepts that cookie. Without
the cookie the page is a login form: the password is the same token, and a successful login
sets the same cookie. Failed logins are rate limited.
It also lists recent workflow runs and their stats (one gh call per repo, cached), and a CI
row per live repo: its `ci-ok` check on the default branch and on open PRs (cached).
Standard library only.

Hosted in a container (dashboard/Dockerfile) it is opt-in configured from the environment:
MACS_BIND (address to listen on), MACS_ALLOWED_HOSTS (comma-separated Host names to accept, on
top of 127.0.0.1 and localhost), and a fixed access token in MACS_TOKEN or the file named by
MACS_TOKEN_FILE. Without them nothing changes: 127.0.0.1, a random token per start.
"""

import argparse
import base64
import hashlib
import http.server
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gh_app_token  # noqa: E402  (shared with the watchdog)

# Everything org-specific comes from the environment; the defaults are obvious placeholders.
# Empty MACS_ORG = repos only. Org-level runners (the Macs' CI runners and the PC's) serve every
# repo in the org, so the runner list reads the org as well as each repo.
ORG = os.environ.get("MACS_ORG", "example-org")
ADMIN_REPO = os.environ.get("MACS_ADMIN_REPO", f"{ORG or 'example-org'}/runnerpool")
REPOS = os.environ.get("MACS_REPOS", ADMIN_REPO).split()
# The CI panel: one row per live repo, read from its `ci-ok` check (the CI contract). By default
# every unarchived repo the token can see in MACS_ORG, plus MACS_CI_EXTRA (space-separated
# OWNER/REPO outside the org). MACS_CI_REPOS, when set, replaces the whole list.
CI_EXTRA = os.environ.get("MACS_CI_EXTRA", "").split()
CI_REPOS = os.environ.get("MACS_CI_REPOS", "").split()
CI_CHECK = os.environ.get("MACS_CI_CHECK", "ci-ok")
# A fine-grained token belongs to one account, so repos owned elsewhere can use their own:
# MACS_CI_TOKEN_FILES="owner=/path owner2=/path2". A missing or empty file falls back to GH_TOKEN
# (which still reads public repos).
CI_TOKEN_FILES = {
    o.lower(): f for o, _, f in (p.partition("=") for p in os.environ.get("MACS_CI_TOKEN_FILES", "").split()) if f
}
MACRUNNER = "/usr/local/mac-runners/macrunner"
SSH_SOCKETS = os.path.join(tempfile.gettempdir(), "macs-ssh")
os.makedirs(SSH_SOCKETS, mode=0o700, exist_ok=True)
# Background refresh: Mac info every INFO_EVERY s (INFO_BACKOFF s for a Mac that failed), the runner
# list every RUNNERS_EVERY s. It pauses when nobody has asked for IDLE_AFTER s, so an unwatched
# dashboard makes no SSH or API calls.
INFO_EVERY = 10
INFO_BACKOFF = 45
RUNNERS_EVERY = 10
IDLE_AFTER = 300
# Which job each busy runner is on: read only while some runner is busy, every ACTIVITY_EVERY s, one gh
# call at a time. An entry older than ACTIVITY_MAX_AGE s is no longer shown.
ACTIVITY_EVERY = 30
ACTIVITY_MAX_AGE = 90
ACTIVITY_RUNS = 30  # in-progress runs read per repo
HERE = Path(__file__).resolve().parent
HOST_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,30}$")
PC_PUSH_RE = re.compile(r"^win-\d+$")  # PCs that push their own info (wsl-N is managed through GitHub only)
PUSH_TOKEN_RE = re.compile(r"^[A-Za-z0-9._~+/=-]{20,200}$")
PUSH_TOKENS_FILE = os.environ.get("MACS_PUSH_TOKENS_FILE", "")  # lines `host=token`; empty/absent = push is off
PUSH_STALE = 90  # a PC that has not reported for this many seconds is shown as not reporting
PUSH_MIN_GAP = 5  # seconds between accepted pushes from one PC
PUSH_MAX_BODY = 65536
PUSH_FAIL_LIMIT = 20  # failed pushes per minute (all hosts) before pushes are refused for a minute
RUN_URL_RE = re.compile(r"https://github\.com/\S+/actions/runs/\d+")
# Per-runner resource limits: saved here, then applied on the device by the `limit` action.
DATA_DIR = Path(os.environ.get("MACS_DATA_DIR") or HERE)
LIMITS_FILE = "runner-limits.json"
RUNNER_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
LIMITS_LOCK = threading.Lock()
LIMIT_SHARE = 0.8  # share of a device's cores and RAM that CI runners may use, split between them
CORES_RANGE = (1, 256)
RAM_MB_RANGE = (256, 1048576)

# action -> (macrunner arguments over SSH, macs arguments through GitHub). None = GitHub only.
ACTIONS = {
    "battery-pause": (["battery", "pause"], ["battery", "{host}", "pause"]),
    "battery-run": (["battery", "run"], ["battery", "{host}", "run"]),
    "ci-on": (["ci", "on"], ["ci", "{host}", "on"]),
    "ci-off": (["ci", "off"], ["ci", "{host}", "off"]),
    "ramdisk-on": (["ramdisk", "on", "{size}"], ["ramdisk", "{host}", "on", "{size}"]),
    "ramdisk-off": (["ramdisk", "off"], ["ramdisk", "{host}", "off"]),
    "cores": (["cores", "{cores}"], ["cores", "{host}", "{cores}"]),
    # {limits} becomes cores=<N|default> and/or ram=<MB|default>, whichever the request carries.
    "limit": (["limit", "{runner}", "{limits}"], ["limit", "{host}", "{runner}", "{limits}"]),
    "restart-ci": (["restart", "{host}"], ["restart", "{host}"]),
    "doctor": (["doctor"], ["doctor", "{host}"]),
    "logs": (["logs", "{host}", "80"], ["logs", "{host}"]),
    # These need the repo's macrunner or the TS_AUTHKEY secret, which only the workflow has.
    "update-macrunner": (None, ["update", "{host}"]),
    "tailscale-up": (None, ["tailscale", "{host}"]),
}

# The ACTIONS a win-N PC takes (through the Admin workflow); ramdisk, update and tailscale are Mac-only.
ACTION_WAIT = 900  # s a second action on one host waits for its turn before giving up (409)
ACTION_STALE = 960  # s after which an in-flight record is assumed dead and dropped
ACTION_QUEUE_MAX = 3  # actions that may wait behind the running one; more are refused at once
# Toggles whose wanted state may already hold: action -> (settings key, wanted value)
NOOP_TOGGLES = {
    "battery-pause": ("pause_on_battery", True),
    "battery-run": ("pause_on_battery", False),
    "ci-on": ("ci_enabled", True),
    "ci-off": ("ci_enabled", False),
}
PC_ACTIONS = {
    "battery-pause",
    "battery-run",
    "ci-on",
    "ci-off",
    "cores",
    "limit",
    "restart-ci",
    "doctor",
    "logs",
}


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


_TOKENS = gh_app_token.TokenProvider.from_env()


def gh_env(env=None):
    """The environment for a `gh` call: with the GitHub App configured, GH_TOKEN is its current
    installation token (minted and refreshed by gh_app_token). Otherwise env is returned as given
    (None = inherit), so the PAT from the entrypoint is used exactly as before. A caller that
    passes its own token (the personal-repo path) builds env itself and bypasses this."""
    if not _TOKENS.source:
        return env
    try:
        tok = _TOKENS.token()
    except gh_app_token.AppTokenError as e:
        gh_app_token.log(f"GitHub App token unavailable: {e}")
        return env
    return dict(os.environ if env is None else env, GH_TOKEN=tok)


# GitHub's hourly quota is shared by every user of one App installation (this dashboard, the watchdog,
# the runner CLI). After a "rate limit" error the dashboard sends no gh calls for RATE_LIMIT_BACKOFF s,
# so it neither hammers a spent quota nor delays its reset.
RATE_LIMIT_BACKOFF = 300
_gh_blocked_until = 0.0


def run(cmd, timeout, env=None):
    """-> (exit code, stdout, stderr); 124 on timeout, 127 when the program is missing.
    A `gh` call with no env of its own gets the GitHub App's token when one is configured."""
    global _gh_blocked_until
    is_gh = bool(cmd) and cmd[0] == "gh"
    if is_gh and time.time() < _gh_blocked_until:
        return 1, "", "gh: API rate limit exceeded (the dashboard is backing off)"
    if env is None and is_gh:
        env = gh_env()
    try:
        p = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            stdin=subprocess.DEVNULL,
            env=env,
        )
        if is_gh and p.returncode != 0 and "rate limit" in (p.stderr + p.stdout).lower():
            _gh_blocked_until = time.time() + RATE_LIMIT_BACKOFF
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        return 124, "", f"timed out after {timeout}s"
    except FileNotFoundError:
        return 127, "", f"{cmd[0]} not found"


def ssh(host, args, timeout):
    # Windows' OpenSSH has no connection sharing; elsewhere one master connection per Mac is
    # reused, which saves the handshake over Tailscale on every refresh.
    share = (
        []
        if os.name == "nt"
        else ["-o", "ControlMaster=auto", "-o", f"ControlPath={SSH_SOCKETS}/%C", "-o", "ControlPersist=300"]
    )
    return run(
        [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=8",
            "-o",
            "ServerAliveInterval=5",
            "-o",
            "StrictHostKeyChecking=accept-new",
            *share,
            f"root@{host}",
            MACRUNNER,
            *args,
        ],
        timeout,
    )


def find_bash():
    """bash for the macs script. On Windows that is Git's, never System32's (WSL's) bash."""
    if os.environ.get("MACS_BASH"):
        return os.environ["MACS_BASH"]
    if os.name != "nt":
        return shutil.which("bash") or "bash"
    places = []
    git = shutil.which("git")
    if git:  # ...\Git\cmd\git.exe -> ...\Git\bin\bash.exe
        places.append(Path(git).resolve().parent.parent / "bin" / "bash.exe")
    for root in (
        os.environ.get("ProgramFiles"),
        os.environ.get("LOCALAPPDATA") and os.path.join(os.environ["LOCALAPPDATA"], "Programs"),
    ):
        if root:
            places.append(Path(root) / "Git" / "bin" / "bash.exe")
    for p in places:
        if p.is_file():
            return str(p)
    return None


def macs(args, timeout):
    """The macs script itself, through the Admin workflow on the Mac's admin runner."""
    bash = find_bash()
    if not bash:
        return 127, "", "Git for Windows' bash not found (winget install Git.Git): it runs macs"
    script = os.environ.get("MACS_SCRIPT") or str(HERE.parent / "runner")
    return run([bash, script, *args], timeout)


_MAIN_SHA = {"at": 0.0, "sha": ""}
MAIN_SHA_TTL = 300


def sha_of(data):
    return hashlib.sha1(data.replace(b"\r\n", b"\n")).hexdigest()[:7]


def macrunner_sha():
    """Short sha1 of mac/gitrunner on main (what `update` installs), as `shasum` on the Mac prints it.

    Read from GitHub and cached, so a dashboard deployed before the last macrunner change still
    compares against the current one. Falls back to the copy baked into this image."""
    if time.time() - _MAIN_SHA["at"] < MAIN_SHA_TTL:
        return _MAIN_SHA["sha"] or baked_macrunner_sha()
    _MAIN_SHA["at"] = time.time()
    try:
        p = subprocess.run(
            [
                "gh",
                "api",
                "-H",
                "Accept: application/vnd.github.raw+json",
                f"repos/{ADMIN_REPO}/contents/mac/gitrunner?ref=main",
            ],
            env=gh_env(),
            capture_output=True,
            timeout=30,
            stdin=subprocess.DEVNULL,
        )
        _MAIN_SHA["sha"] = sha_of(p.stdout) if p.returncode == 0 and p.stdout else ""
    except (OSError, subprocess.TimeoutExpired):
        _MAIN_SHA["sha"] = ""
    return _MAIN_SHA["sha"] or baked_macrunner_sha()


def baked_macrunner_sha():
    try:
        return sha_of((HERE.parent / "mac" / "gitrunner").read_bytes())
    except OSError:
        return ""


def last_json(text):
    for line in reversed(text.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except ValueError:
                pass
    return None


def tail(text, n=4):
    lines = [line for line in text.strip().splitlines() if line.strip()]
    return "\n".join(lines[-n:])


def parse_ts(v):
    try:
        return datetime.strptime(v, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


RUNS_TTL = 120
RECENT = 30
GOOD = {"success"}
BAD = {"failure", "timed_out", "startup_failure"}
LIVE = {"queued", "in_progress", "waiting", "pending", "requested"}


def summarize_runs(results):
    """results: [(repo, error, [raw run, ...])] -> recent runs and stats, JSON-ready."""
    t_now = datetime.now(timezone.utc)
    flat, repos = [], []
    for repo, error, raw in results:
        for r in raw:
            started = parse_ts(r.get("run_started_at") or r.get("created_at"))
            ended = parse_ts(r.get("updated_at"))
            dur = None
            if started and ended and r.get("status") == "completed":
                dur = max(0, int((ended - started).total_seconds()))
            elif started and r.get("status") == "in_progress":
                dur = max(0, int((t_now - started).total_seconds()))
            flat.append(
                {
                    "repo": repo,
                    "id": r.get("id"),
                    "workflow": r.get("name") or "",
                    "title": r.get("display_title") or "",
                    "branch": r.get("head_branch") or "",
                    "event": r.get("event") or "",
                    "status": r.get("status") or "",
                    "conclusion": r.get("conclusion"),
                    "created": r.get("created_at"),
                    "duration_s": dur,
                    "url": r.get("html_url"),
                    "_t": parse_ts(r.get("created_at")),
                }
            )
        repos.append({"repo": repo, "error": error, "runs": len(raw)})

    def stats(rows):
        decided = [r for r in rows if r["conclusion"] in GOOD | BAD]
        good = sum(1 for r in decided if r["conclusion"] in GOOD)
        durs = [r["duration_s"] for r in decided if r["duration_s"] is not None]
        return {
            "runs": len(rows),
            "decided": len(decided),
            "success": good,
            "success_rate": round(100 * good / len(decided)) if decided else None,
            "avg_duration_s": round(sum(durs) / len(durs)) if durs else None,
        }

    flat.sort(key=lambda r: r["_t"] or t_now, reverse=True)
    days = [(t_now - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(6, -1, -1)]
    per_day = {d: 0 for d in days}
    for r in flat:
        d = (r["created"] or "")[:10]
        if d in per_day:
            per_day[d] += 1
    overall = stats(flat)
    overall.update(
        {
            "queued": sum(1 for r in flat if r["status"] in LIVE and r["status"] != "in_progress"),
            "running": sum(1 for r in flat if r["status"] == "in_progress"),
            "per_day": [{"day": d, "runs": per_day[d]} for d in days],
            "by_repo": [
                dict(stats([r for r in flat if r["repo"] == repo["repo"]]), repo=repo["repo"]) for repo in repos
            ],
        }
    )
    for r in flat:
        del r["_t"]
    return {"at": now(), "repos": repos, "stats": overall, "runs": flat[:RECENT]}


JOBS_TTL = 120
JOB_RUNS = 40  # newest finished runs per repo whose jobs are read
JOB_HISTORY = 300  # jobs kept per answer


def summarize_jobs(jobs):
    """jobs: [{repo, run_id, name, runner, conclusion, started, completed, url}] -> per-runner stats,
    a job-by-runner comparison and the newest jobs, JSON-ready. Only jobs that ran on a runner count."""
    rows = []
    for j in jobs:
        a, b = parse_ts(j.get("started")), parse_ts(j.get("completed"))
        if not j.get("runner") or not a or not b:
            continue
        rows.append(dict(j, duration_s=max(0, int((b - a).total_seconds())), _t=b))
    rows.sort(key=lambda r: r["_t"], reverse=True)

    def med(xs):
        xs = sorted(xs)
        n = len(xs)
        return xs[n // 2] if n % 2 else round((xs[n // 2 - 1] + xs[n // 2]) / 2)

    ok = [r for r in rows if r["conclusion"] in GOOD]  # only passing jobs are comparable
    by_runner = {}
    for r in rows:
        by_runner.setdefault(r["runner"], []).append(r)
    runners = [
        {
            "runner": n,
            "jobs": len(v),
            "failed": sum(1 for r in v if r["conclusion"] in BAD),
            "avg_duration_s": round(sum(r["duration_s"] for r in v) / len(v)),
        }
        for n, v in sorted(by_runner.items())
    ]
    groups = {}
    for r in ok:
        groups.setdefault((r["repo"], r["name"]), {}).setdefault(r["runner"], []).append(r)
    compare = []
    for (repo, name), per in groups.items():
        if len(per) < 2:  # nothing to compare against
            continue
        cells = {}
        for runner, v in per.items():
            # newest first; the trend is oldest to newest
            cells[runner] = {
                "n": len(v),
                "median_s": med([r["duration_s"] for r in v]),
                "last_s": v[0]["duration_s"],
                "trend": [r["duration_s"] for r in reversed(v[:10])],
            }
        compare.append({"repo": repo, "job": name, "runners": cells, "total": sum(c["n"] for c in cells.values())})
    compare.sort(key=lambda c: -c["total"])
    for r in rows:
        del r["_t"]
    return {"at": now(), "runners": runners, "compare": compare[:25], "jobs": rows[:JOB_HISTORY]}


CI_TTL = 300
CI_PRS = 10  # open PRs per repo whose check is read (newest first)


def gh_json(path, token=None):
    """gh api PATH -> (data, error). token replaces GH_TOKEN for this call."""
    env = dict(os.environ, GH_TOKEN=token) if token else None
    rc, out, err = run(["gh", "api", path], 30, env)
    if rc != 0:
        why = tail(err or out, 1) or f"gh exit {rc}"
        if "HTTP 404" in why:  # GitHub hides what a token may not see behind a 404
            why = "not visible to the dashboard's GitHub token (404): see docs/dashboard-deploy.md"
        return None, why
    try:
        return json.loads(out), None
    except ValueError:
        return None, "gh returned something that is not JSON"


def owner_token(repo):
    path = CI_TOKEN_FILES.get(repo.split("/")[0].lower())
    if not path:
        return None
    try:
        return Path(path).read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def check_state(runs):
    """The newest check run of one name on one commit -> {state, url, at}; state is
    passed, failed, running, queued, skipped, cancelled, other or missing (no such check)."""
    if not runs:
        return {"state": "missing", "url": None, "at": None}
    r = max(runs, key=lambda c: c.get("started_at") or c.get("completed_at") or "")
    status, concl = r.get("status"), r.get("conclusion")
    if status != "completed":
        state = "running" if status == "in_progress" else "queued"
    elif concl == "success":
        state = "passed"
    elif concl in ("failure", "timed_out", "startup_failure", "action_required"):
        state = "failed"
    elif concl in ("skipped", "neutral"):
        state = "skipped"
    elif concl in ("cancelled", "stale"):
        state = "cancelled"
    else:
        state = "other"
    return {"state": state, "url": r.get("html_url"), "at": r.get("completed_at") or r.get("started_at")}


def repo_ci(repo, branch, token):
    """One CI row: the check on the default branch, open PRs with theirs, and live workflow runs."""
    row = {
        "repo": repo,
        "branch": branch,
        "error": None,
        "main": None,
        "prs": [],
        "prs_open": 0,
        "running": None,
        "queued": None,
        "url": f"https://github.com/{repo}",
    }

    def check(ref):
        data, err = gh_json(f"repos/{repo}/commits/{ref}/check-runs?check_name={CI_CHECK}&per_page=20", token)
        if err:
            return {"state": "unknown", "url": None, "at": None, "error": err}
        return check_state(data.get("check_runs", []))

    row["main"] = check(branch)
    pulls, err = gh_json(f"repos/{repo}/pulls?state=open&sort=updated&direction=desc&per_page={CI_PRS}", token)
    if err:
        row["error"] = f"pull requests: {err}"
    else:
        for p in pulls:
            row["prs"].append(
                {
                    "number": p.get("number"),
                    "title": p.get("title") or "",
                    "draft": bool(p.get("draft")),
                    "url": p.get("html_url"),
                    "branch": (p.get("head") or {}).get("ref") or "",
                    "ci": check((p.get("head") or {}).get("sha") or ""),
                }
            )
        row["prs_open"] = len(pulls)
    runs, err = gh_json(f"repos/{repo}/actions/runs?per_page=30", token)
    if err:
        row["error"] = row["error"] or f"workflow runs: {err}"
    else:
        statuses = [r.get("status") for r in runs.get("workflow_runs", [])]
        row["running"] = statuses.count("in_progress")
        row["queued"] = sum(1 for s in statuses if s in LIVE and s != "in_progress")
    return row


def ci_repos():
    """[(repo, default branch)] for the CI panel, and an error from the org listing (or None)."""
    if CI_REPOS:
        return [(r, None) for r in CI_REPOS], None
    found, error = [], None
    if ORG:
        data, error = gh_json(f"orgs/{ORG}/repos?type=all&per_page=100")
        for r in sorted(data or [], key=lambda r: r["name"].lower()):
            if not r.get("archived") and not r.get("disabled"):
                found.append((r["full_name"], r.get("default_branch")))
    seen = {r.lower() for r, _ in found}
    return found + [(r, None) for r in CI_EXTRA if r.lower() not in seen], error


def build_activity(repos, want=None, fetch=None):
    """In-progress jobs -> ({runner name lowercased: what it is running}, {repo: error}).

    repos is [(repo, token)]. Calls are serial (one gh process at a time). A repo that cannot be read (403,
    404) is skipped and reported in the errors. want, when given, is the set of lowercased busy runner names:
    the walk stops once all are found."""
    fetch = fetch or gh_json
    found, errors = {}, {}
    for repo, token in repos:
        if want is not None and want <= set(found):
            break
        runs, err = fetch(f"repos/{repo}/actions/runs?status=in_progress&per_page={ACTIVITY_RUNS}", token)
        if err:
            errors[repo] = err
            continue
        for r in (runs or {}).get("workflow_runs", []):
            jobs, err = fetch(f"repos/{repo}/actions/runs/{r.get('id')}/jobs?per_page=100&filter=latest", token)
            if err:
                errors[repo] = err
                continue
            for j in (jobs or {}).get("jobs", []):
                name = j.get("runner_name") or ""
                if j.get("status") != "in_progress" or not name:
                    continue
                entry = {
                    "runner": name,
                    "repo": repo,
                    "workflow": r.get("name") or "",
                    "job": j.get("name") or "",
                    "branch": r.get("head_branch") or "",
                    "run_url": r.get("html_url"),
                    "job_url": j.get("html_url"),
                    "started_at": j.get("started_at") or r.get("run_started_at"),
                }
                have = found.get(name.lower())
                if not have or (entry["started_at"] or "") >= (have["started_at"] or ""):
                    found[name.lower()] = entry
    return found, errors


def natural_key(name):
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", name)]


PC_RE = re.compile(r"^(?:win|wsl)-(\d+)(?:-|$)")


def runner_entry(repo, r):
    name = r["name"]
    return {
        "name": name,
        "repo": repo,
        "kind": "admin" if name.lower().endswith("-admin") else "ci",
        "status": r.get("status", ""),
        "busy": bool(r.get("busy")),
        "labels": r.get("labels", []),
    }


def device_groups(repos, macs):
    """Group the runner lists by device -> (devices, other).

    A Mac is a host in `macs` (found through its `<host>-admin` runner with label mac-admin); its
    runners are the host itself and `<host>-...`. A PC is `win-N`: its `win-N`, `win-N-admin`,
    `win-N-wsl-M` and `wsl-N-admin` runners (the WSL distro's admin runner) all belong to it.
    Names match case-insensitively; a runner seen under several scopes is listed once.
    Anything else goes in `other`. Devices: [{host, kind, runners: [{name, repo, kind, status,
    busy, labels}]}], Macs first, each list in natural name order."""
    mac_hosts = sorted({m.lower() for m in macs}, key=natural_key)
    seen, entries = set(), []
    for repo in repos:
        for r in repo.get("runners", []):
            key = r["name"].lower()
            if key not in seen:
                seen.add(key)
                entries.append(runner_entry(repo["repo"], r))
    devices, other = {}, []

    def device(host, kind):
        return devices.setdefault(host, {"host": host, "kind": kind, "runners": []})

    for h in mac_hosts:
        device(h, "mac")
    for e in sorted(entries, key=lambda e: natural_key(e["name"].lower())):
        name = e["name"].lower()
        pc = PC_RE.match(name)
        mac = next((h for h in mac_hosts if name == h or name.startswith(h + "-")), None)
        if mac:
            device(mac, "mac")["runners"].append(e)
        elif pc:
            device("win-" + pc.group(1), "pc")["runners"].append(e)
        else:
            other.append(e)
    ordered = sorted(devices.values(), key=lambda d: (d["kind"] != "mac", natural_key(d["host"])))
    return ordered, other


def default_limits(cores, ram_mb, n_ci):
    """The default per-runner share: floor(0.8 * cores / n) cores (at least 1) and 0.8 * RAM / n MB.
    -> {"cores": int|None, "ram_mb": int|None}; None where the device's size is unknown."""
    n = max(1, int(n_ci))
    out = {"cores": None, "ram_mb": None}
    if type(cores) in (int, float) and cores > 0:
        out["cores"] = min(CORES_RANGE[1], max(CORES_RANGE[0], int(LIMIT_SHARE * cores / n)))
    if type(ram_mb) in (int, float) and ram_mb > 0:
        out["ram_mb"] = min(RAM_MB_RANGE[1], max(RAM_MB_RANGE[0], int(LIMIT_SHARE * ram_mb / n)))
    return out


def device_size(info):
    """(cores, ram_mb) from a device's info: cores, and ram_mb or memory_gb."""
    info = info if isinstance(info, dict) else {}
    cores = info.get("cores")
    ram = info.get("ram_mb")
    if not isinstance(ram, (int, float)) and isinstance(info.get("memory_gb"), (int, float)):
        ram = info["memory_gb"] * 1024
    return (
        cores if isinstance(cores, (int, float)) and not isinstance(cores, bool) else None,
        int(ram) if isinstance(ram, (int, float)) and not isinstance(ram, bool) else None,
    )


def with_limits(devices, infos, limits):
    """Copy of devices with each one's cores/ram_mb (from its info, or None) and, on every CI runner,
    its default, saved override and the effective limits."""
    out = []
    for d in devices:
        info = (infos.get(d["host"]) or {}).get("info")
        cores, ram_mb = device_size(info)
        ci = [r for r in d["runners"] if r["kind"] == "ci"]
        dflt = default_limits(cores, ram_mb, len(ci))
        saved = (limits.get(d["host"]) or {}) if isinstance(limits, dict) else {}
        runners = []
        for r in d["runners"]:
            if r["kind"] != "ci":
                runners.append(r)
                continue
            ov = saved.get(r["name"].lower()) or {}
            eff = {k: ov.get(k) if ov.get(k) is not None else dflt[k] for k in ("cores", "ram_mb")}
            runners.append({**r, "default": dflt, "override": ov, "limits": eff})
        out.append({**d, "cores": cores, "ram_mb": ram_mb, "runners": runners})
    return out


def limits_path():
    return DATA_DIR / LIMITS_FILE


def load_limits():
    """{host: {runner: {cores?, ram_mb?}}}; {} when the file is missing or unreadable."""
    try:
        data = json.loads(limits_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _int_in(v, lo, hi):
    return type(v) is int and lo <= v <= hi


def validate_limits(body):
    """A POST /api/runner-limits body -> (host, runner, changes, error). changes maps "cores" and/or
    "ram_mb" to an int, or to None to clear that override; "reset": true clears both."""
    if not isinstance(body, dict):
        return None, None, None, "body must be a JSON object"
    host, runner = body.get("host"), body.get("runner")
    if not isinstance(host, str) or not HOST_RE.match(host.lower()):
        return None, None, None, "host is not a valid device name"
    if not isinstance(runner, str) or not RUNNER_RE.match(runner.lower()):
        return None, None, None, "runner is not a valid runner name"
    changes = {}
    if body.get("reset") is True:
        changes = {"cores": None, "ram_mb": None}
    for key, (lo, hi), label in (("cores", CORES_RANGE, "cores"), ("ram_mb", RAM_MB_RANGE, "ram_mb")):
        if key in body and body[key] is not None:
            if not _int_in(body[key], lo, hi):
                return None, None, None, f"{label} is a whole number from {lo} to {hi}"
            changes[key] = body[key]
        elif key in body:
            changes[key] = None
    if not changes:
        return None, None, None, "nothing to change: send cores, ram_mb or reset"
    return host.lower(), runner.lower(), changes, None


def save_limit(host, runner, changes):
    """Apply validated changes and write the file atomically. -> the runner's override ({} if cleared)."""
    with LIMITS_LOCK:
        data = load_limits()
        hosts = {h: v for h, v in data.items() if isinstance(v, dict)}
        entry = dict(hosts.get(host, {}).get(runner) or {})
        for k, v in changes.items():
            if v is None:
                entry.pop(k, None)
            else:
                entry[k] = v
        per_host = dict(hosts.get(host, {}))
        if entry:
            per_host[runner] = entry
        else:
            per_host.pop(runner, None)
        if per_host:
            hosts[host] = per_host
        else:
            hosts.pop(host, None)
        path = limits_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".runner-limits-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(hosts, f, indent=1, sort_keys=True)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        return entry


class State:
    def __init__(self):
        self.lock = threading.Lock()
        self.runners = None  # last /api/runners answer
        self.runners_at = 0.0
        self.runs = None  # last /api/runs answer
        self.runs_at = 0.0
        self.jobs = None  # last /api/jobs answer
        self.jobs_at = 0.0
        self.job_cache = {}  # (repo, run id) -> its jobs; a finished run never changes
        self.jobs_lock = threading.Lock()
        self.ci = None  # last /api/ci answer
        self.ci_at = 0.0
        self.activity = {}  # runner (lowercase) -> job it is on, see build_activity
        self.activity_at = 0.0
        self.activity_errors = {}
        self.ci_lock = threading.Lock()  # one refresh at a time: it is many gh calls
        self.inflight = {}  # host -> {action, started, via} for the action running now
        self.queues = {}  # host -> [tickets] of actions waiting their turn (FIFO); the head goes next
        self.turn = threading.Condition(self.lock)
        self.limit_status = {}  # {host: {runner: {applied, error, via, run_url, at}}}, since this server started
        self.infos = {}  # host -> last mac_info answer (a failed one carries the last good info)
        self.info_at = {}  # host -> when it was last tried
        self.info_locks = {}
        self.pushed = {}  # win-N -> {"at": epoch, "iso": ..., "info": ...} from POST /api/push-info
        self.push_fails = []  # times of failed pushes, for the lockout
        self.last_seen = 0.0  # last time the browser asked for anything
        self.wake = threading.Event()
        self.running = set()  # background jobs in flight
        self.started = False

    # -- Mac info, kept warm in the background so a page load never waits on SSH --
    def get_info(self, host, fresh=False):
        if is_push_host(host):  # a PC reports on its own: never poll it
            return self.push_answer(host)
        with self.lock:
            lock = self.info_locks.setdefault(host, threading.Lock())
            have = self.infos.get(host)
        if have and not fresh:  # serve the cache without queueing behind a background SSH
            return have
        with lock:  # one SSH at a time per Mac; a waiting request then finds the new answer
            have = self.infos.get(host)
            if have and not fresh:
                return have
            res = mac_info(host, "ssh")
            if not res["ok"] and have:
                good = have if have["ok"] else {"info": have.get("info"), "at": have.get("info_at")}
                res = {**res, "info": good["info"], "info_at": good["at"]}
            elif res["ok"]:
                res["info_at"] = res["at"]
            with self.lock:
                self.infos[host] = res
                self.info_at[host] = time.time()
            return res

    # -- PC health, pushed by the PC (POST /api/push-info) --
    def push_answer(self, host):
        """A mac_info-shaped answer for a PC from its last push: ok while the push is fresh, else the last
        good info with a 'has not reported' error, or no info at all before the first push."""
        base = {"host": host, "via": "push", "run_url": None}
        with self.lock:
            have = self.pushed.get(host)
        if not have:
            known = host in load_push_tokens()
            why = (
                "PC has not reported yet (is winrunner updated and push-setup done on it?)"
                if known
                else "PC health push is not set up for this host (no token on the dashboard)"
            )
            return {**base, "at": now(), "ok": False, "info": None, "error": why, "reason": why}
        age = int(time.time() - have["at"])
        res = {**base, "at": have["iso"], "info": have["info"], "info_at": have["iso"], "age_s": age}
        if age > PUSH_STALE:
            why = f"PC has not reported for {age} s"
            return {**res, "ok": False, "error": why, "reason": why}
        return {**res, "ok": True, "error": None, "reason": None}

    def push_info(self, host, info):
        """Keep INFO as HOST's latest push. -> seconds to wait if this host pushed too recently, else None."""
        t = time.time()
        with self.lock:
            last = self.pushed.get(host)
            if last and t - last["at"] < PUSH_MIN_GAP:
                return max(1, int(PUSH_MIN_GAP - (t - last["at"]) + 0.999))
            self.pushed[host] = {"at": t, "iso": now(), "info": info}
        return None

    def push_blocked(self):
        t = time.time()
        with self.lock:
            self.push_fails[:] = [x for x in self.push_fails if t - x < 60]
            return len(self.push_fails) >= PUSH_FAIL_LIMIT

    def push_failed(self):
        with self.lock:
            self.push_fails.append(time.time())

    def touch(self):
        """Called on every API read: starts the background refresh and wakes it after idling."""
        self.last_seen = time.time()
        self.wake.set()
        with self.lock:
            if self.started:
                return
            self.started = True
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        pool = ThreadPoolExecutor(12)
        due = {}

        def job(key, fn):
            try:
                fn()
            except Exception as e:  # keep the loop alive; the next tick tries again
                print(f"dashboard: background {key} failed: {e}", flush=True)
            finally:
                with self.lock:
                    self.running.discard(key)

        def schedule(key, every, fn):
            t = time.time()
            with self.lock:
                if key in self.running or t < due.get(key, 0):
                    return
                self.running.add(key)
                due[key] = t + every
            pool.submit(job, key, fn)

        while True:
            self.wake.wait(2)
            self.wake.clear()
            if time.time() - self.last_seen > IDLE_AFTER:
                continue
            schedule("runners", RUNNERS_EVERY, lambda: self.get_runners(force=True))
            schedule("runs", RUNS_TTL, lambda: self.get_runs(force=True))
            schedule("ci", CI_TTL, lambda: self.get_ci(force=True))
            schedule("jobs", JOBS_TTL, lambda: self.get_jobs(force=True))
            schedule("activity", ACTIVITY_EVERY, self.get_activity)
            for host in list((self.runners or {}).get("macs", [])):
                if host in self.inflight:
                    continue  # an action is running; the page refreshes when it ends
                bad = host in self.infos and not self.infos[host]["ok"]
                schedule(
                    "mac:" + host,
                    INFO_BACKOFF if bad else INFO_EVERY,
                    lambda host=host: self.get_info(host, fresh=True),
                )

    def get_runners(self, force=False):
        with self.lock:
            if not force and self.runners and time.time() - self.runners_at < RUNNERS_EVERY:
                return self.runners

        def one(repo):
            path = f"repos/{repo}" if "/" in repo else f"orgs/{repo}"
            rc, out, err = run(["gh", "api", f"{path}/actions/runners?per_page=100"], 30)
            if rc != 0:
                return {"repo": repo, "error": tail(err or out, 1) or f"gh exit {rc}", "runners": []}
            try:
                data = json.loads(out)
            except ValueError:
                return {"repo": repo, "error": "gh returned something that is not JSON", "runners": []}
            return {
                "repo": repo,
                "error": None,
                "runners": [
                    {
                        "name": r["name"],
                        "os": r.get("os", ""),
                        "status": r.get("status", ""),
                        "busy": bool(r.get("busy")),
                        "labels": [label["name"] for label in r.get("labels", [])],
                    }
                    for r in sorted(data.get("runners", []), key=lambda r: r["name"])
                ],
            }

        scopes = ([ORG] if ORG else []) + REPOS
        with ThreadPoolExecutor(len(scopes)) as pool:
            repos = list(pool.map(one, scopes))
        macs_found = sorted(
            {
                r["name"][: -len("-admin")]
                for repo in repos
                if repo["repo"] == ADMIN_REPO
                for r in repo["runners"]
                if r["name"].endswith("-admin") and "mac-admin" in r["labels"]
            },
            key=natural_key,
        )
        answer = {"at": now(), "macs": macs_found, "macrunner_sha": macrunner_sha(), "repos": repos}
        with self.lock:
            # Keep the last known Macs when the admin repo could not be read this time.
            if self.runners and any(r["repo"] == ADMIN_REPO and r["error"] for r in repos):
                answer["macs"] = self.runners["macs"]
            answer["devices"], answer["other"] = device_groups(repos, answer["macs"])
            self.runners, self.runners_at = answer, time.time()
        return answer

    def get_runs(self, force=False):
        """Latest workflow runs of every repo (one Actions API call each, cached) plus stats."""
        with self.lock:
            if not force and self.runs and time.time() - self.runs_at < RUNS_TTL:
                return self.runs

        def one(repo):
            rc, out, err = run(["gh", "api", f"repos/{repo}/actions/runs?per_page=100"], 30)
            if rc != 0:
                return repo, tail(err or out, 1) or f"gh exit {rc}", []
            try:
                return repo, None, json.loads(out).get("workflow_runs", [])
            except (ValueError, AttributeError):
                return repo, "gh returned something that is not JSON", []

        with ThreadPoolExecutor(max(1, len(REPOS))) as pool:
            results = list(pool.map(one, REPOS))
        answer = summarize_runs(results)
        with self.lock:
            self.runs, self.runs_at = answer, time.time()
        return answer

    def get_jobs(self, force=False):
        """Which runner ran each job of the newest finished runs, cached; finished runs are read once."""
        with self.jobs_lock:
            with self.lock:
                if not force and self.jobs and time.time() - self.jobs_at < JOBS_TTL:
                    return self.jobs

            def runs_of(repo):
                rc, out, err = run(["gh", "api", f"repos/{repo}/actions/runs?per_page=100&status=completed"], 30)
                try:
                    if rc != 0:
                        raise ValueError(tail(err or out, 1) or f"gh exit {rc}")
                    return repo, None, [r["id"] for r in json.loads(out).get("workflow_runs", [])[:JOB_RUNS]]
                except (ValueError, AttributeError, KeyError) as e:
                    return repo, str(e), []

            def jobs_of(item):
                repo, rid = item
                if item in self.job_cache:
                    return None
                rc, out, err = run(
                    ["gh", "api", f"repos/{repo}/actions/runs/{rid}/jobs?per_page=100&filter=latest"], 30
                )
                if rc != 0:
                    return None  # retried on the next refresh
                try:
                    data = json.loads(out).get("jobs", [])
                except (ValueError, AttributeError):
                    return None
                self.job_cache[item] = [
                    {
                        "repo": repo,
                        "run_id": rid,
                        "name": j.get("name") or "",
                        "runner": j.get("runner_name") or "",
                        "conclusion": j.get("conclusion"),
                        "started": j.get("started_at"),
                        "completed": j.get("completed_at"),
                        "url": j.get("html_url"),
                    }
                    for j in data
                ]

            with ThreadPoolExecutor(max(1, len(REPOS))) as pool:
                found = list(pool.map(runs_of, REPOS))
            todo = [(repo, rid) for repo, _, ids in found for rid in ids]
            with ThreadPoolExecutor(8) as pool:
                list(pool.map(jobs_of, todo))
            jobs = [j for item in todo for j in self.job_cache.get(item, [])]
            answer = summarize_jobs(jobs)
            answer["repos"] = [{"repo": repo, "error": err} for repo, err, _ in found]
            if len(self.job_cache) > 2000:  # drop the oldest-inserted half
                for k in list(self.job_cache)[:1000]:
                    del self.job_cache[k]
            with self.lock:
                self.jobs, self.jobs_at = answer, time.time()
            return answer

    def busy_runners(self):
        """Lowercased names of the online runners the last runner list says are busy."""
        with self.lock:
            repos = (self.runners or {}).get("repos", [])
        return {r["name"].lower() for repo in repos for r in repo["runners"] if r["busy"] and r["status"] == "online"}

    def get_activity(self):
        """Refresh runner_activity, but only while a runner is busy; the repos are those of the CI panel."""
        want = self.busy_runners()
        if not want:
            with self.lock:
                self.activity, self.activity_errors = {}, {}
            return
        with self.lock:
            names = [r["repo"] for r in (self.ci or {}).get("repos", [])]
        names += [r for r in REPOS if r.lower() not in {n.lower() for n in names}]
        found, errors = build_activity([(r, owner_token(r)) for r in names], want)
        with self.lock:
            self.activity, self.activity_errors, self.activity_at = found, errors, time.time()

    def current_activity(self):
        """The activity of runners that are busy right now and were read recently enough."""
        want = self.busy_runners()
        with self.lock:
            fresh = time.time() - self.activity_at <= ACTIVITY_MAX_AGE
            return {k: v for k, v in self.activity.items() if fresh and k in want}

    def get_ci(self, force=False):
        """The CI panel (see repo_ci), cached for CI_TTL seconds."""
        with self.ci_lock:
            with self.lock:
                if not force and self.ci and time.time() - self.ci_at < CI_TTL:
                    return self.ci
            repos, error = ci_repos()

            def one(item):
                repo, branch = item
                token = owner_token(repo)
                if not branch:
                    info, err = gh_json(f"repos/{repo}", token)
                    if err:
                        return {
                            "repo": repo,
                            "error": err,
                            "main": None,
                            "prs": [],
                            "prs_open": 0,
                            "running": None,
                            "queued": None,
                            "url": f"https://github.com/{repo}",
                        }
                    branch = info.get("default_branch") or "main"
                return repo_ci(repo, branch, token)

            with ThreadPoolExecutor(max(1, min(8, len(repos)))) as pool:
                rows = list(pool.map(one, repos))
            answer = {"at": now(), "check": CI_CHECK, "error": error, "repos": rows}
            with self.lock:
                self.ci, self.ci_at = answer, time.time()
            return answer

    def runners_answer(self):
        """/api/runners: the cached answer with each device's limits (cheap, so never cached)."""
        answer = self.get_runners()
        with self.lock:
            infos = dict(self.infos)
            pushed = list(self.pushed)
        for host in pushed:  # a PC's size (for the 80% defaults) comes from its last push, stale or not
            infos[host] = self.push_answer(host)
        limits = load_limits()
        with self.lock:
            status = {h: dict(v) for h, v in self.limit_status.items()}
        return {
            **answer,
            "devices": with_limits(answer.get("devices", []), infos, limits),
            "limits": limits,
            "limit_status": status,
            "actions_in_flight": self.actions_in_flight(),
            "runner_activity": self.current_activity(),
        }

    def known_mac(self, host):
        if not HOST_RE.match(host or ""):
            return False
        return host in self.get_runners()["macs"]

    def known_pc(self, host):
        """A win-N PC the runner lists show."""
        if not is_push_host(host):
            return False
        return any(d["host"] == host for d in self.get_runners().get("devices", []))

    def known_host(self, host, action):
        """A Mac; or a listed win-N PC for the actions it takes (PC_ACTIONS); or for `limit` also the wsl-N
        host of a listed PC (its Linux runners)."""
        if self.known_mac(host):
            return True
        if not is_pc_host(host):
            return False
        n = host.split("-", 1)[1]
        listed = any(d["host"] == f"win-{n}" for d in self.get_runners().get("devices", []))
        if host.startswith("wsl-"):
            return action == "limit" and listed
        return listed and action in PC_ACTIONS

    def _live(self, host):
        """The in-flight record of HOST, dropping one older than ACTION_STALE (caller holds self.lock)."""
        rec = self.inflight.get(host)
        if rec and time.time() - rec["started"] > ACTION_STALE:
            del self.inflight[host]
            rec = None
        return rec

    def acquire(self, host, action, via="auto", wait=None):
        """Take HOST's turn to run ACTION. Actions on one host run one at a time, in arrival order: a
        second one waits (up to WAIT s) for the first. -> None once it is ours (call release), else a
        reason string (queue full, or the wait ran out) that names what is running and for how long."""
        ticket = object()
        deadline = time.time() + (ACTION_WAIT if wait is None else wait)
        with self.turn:
            queue = self.queues.setdefault(host, [])
            if self._live(host) and len(queue) >= ACTION_QUEUE_MAX:
                if not queue:
                    self.queues.pop(host, None)
                return self.busy_reason(host, len(queue))
            queue.append(ticket)
            try:
                while not (queue[0] is ticket and not self._live(host)):
                    left = deadline - time.time()
                    if left <= 0:
                        return self.busy_reason(host, len(queue) - 1)
                    self.turn.wait(min(left, 5))  # wakes on release; the cap re-checks for a stale record
                self.inflight[host] = {"action": action, "started": time.time(), "via": via}
                return None
            finally:
                queue.remove(ticket)
                if not queue:
                    self.queues.pop(host, None)
                self.turn.notify_all()

    def busy_reason(self, host, waiting=0):
        """Plain words for what HOST is doing (caller holds self.lock)."""
        rec = self._live(host)
        if not rec:
            return f"{host} is busy; try again in a moment"
        return (
            f"{host} is busy running {rec['action']} ({int(time.time() - rec['started'])} s)"
            + (f" with {waiting} more waiting" if waiting else "")
            + "; try again when it finishes"
        )

    def release(self, host):
        with self.turn:
            self.inflight.pop(host, None)
            self.runners_at = 0  # the next poll shows the new runner state
            self.turn.notify_all()

    def actions_in_flight(self):
        """{host: {action, since_s, queued}} for every host running an action now (cheap, for the page)."""
        out = {}
        with self.lock:
            for host in list(self.inflight):
                rec = self._live(host)
                if rec:
                    out[host] = {
                        "action": rec["action"],
                        "since_s": int(time.time() - rec["started"]),
                        "queued": len(self.queues.get(host, ())),
                    }
        return out

    def current_settings(self, host):
        """The device's settings from its last fresh info answer, or None when unknown or stale."""
        res = self.push_answer(host) if is_push_host(host) else self.infos.get(host)
        info = res.get("info") if res and res.get("ok") else None
        s = info.get("settings") if isinstance(info, dict) else None
        return s if isinstance(s, dict) else None

    def already_set(self, host, action):
        """True when a battery/CI toggle asks for what the device last reported (no dispatch needed)."""
        key, want = NOOP_TOGGLES.get(action, (None, None))
        s = self.current_settings(host) if key else None
        return bool(s) and isinstance(s.get(key), bool) and s[key] is want

    def apply_limit(self, host, runner, changes, via="auto"):
        """Push a saved override to the device. `changes` is the validated {cores?, ram_mb?} (None =
        back to default). -> {applied, error, via, run_url}; the outcome is remembered for the page."""
        target = admin_host(host, runner)
        cores = changes.get("cores", ...)
        ram = changes.get("ram_mb", ...)
        args = {
            "cores": None if cores is ... else "default" if cores is None else cores,
            "ram": None if ram is ... else "default" if ram is None else ram,
        }
        busy = self.acquire(target, f"limit {runner}", via)
        if busy:
            res = {"applied": False, "error": busy}
        else:
            try:
                print(f"{time.strftime('%H:%M:%S')} limit {runner} on {target} ({via})", file=sys.stderr)
                r = mac_action(target, "limit", 4, via, args["cores"], runner, args["ram"])
                res = {"applied": r["ok"], "error": r["error"], "via": r["via"], "run_url": r["run_url"]}
            except Exception as e:  # keep the saved override and report, whatever went wrong
                res = {"applied": False, "error": f"{type(e).__name__}: {e}"}
            finally:
                self.release(target)
        res["at"] = now()
        with self.lock:
            self.limit_status.setdefault(host, {})[runner] = res
        return res


STATE = State()


def offline_reason(rc, text):
    """Plain-English guess at why a Mac did not answer over SSH, from ssh's own error."""
    t = (text or "").lower()
    if "could not resolve" in t or "name or service not known" in t:
        return "Dashboard doesn't know this Mac's address (missing extra_hosts line)"
    if "permission denied" in t or "host key verification" in t:
        return "SSH key not accepted"
    if "connection refused" in t:
        return "Mac is on but SSH is off"
    if rc == 124 or any(
        k in t
        for k in ("timed out", "no route to host", "network is unreachable", "connection closed", "connection reset")
    ):
        return "Not reachable over Tailscale: asleep, powered off or offline"
    return None


def is_push_host(host):
    """win-N: a Windows PC that pushes its own info."""
    return bool(PC_PUSH_RE.match(host or ""))


def load_push_tokens(path=None):
    """{host: token} from the push tokens file (`host=token` lines; # comments and blanks skipped). Bad
    lines are ignored; a missing or empty file means push is off."""
    path = PUSH_TOKENS_FILE if path is None else path
    out = {}
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace") if path else ""
    except OSError:
        return out
    for line in text.splitlines():
        host, sep, token = line.strip().partition("=")
        host, token = host.strip().lower(), token.strip()
        if sep and is_push_host(host) and PUSH_TOKEN_RE.match(token):
            out[host] = token
    return out


def host_for_token(given, tokens):
    """The host whose token is GIVEN, comparing against every token in constant time; None if none."""
    found = None
    for host, token in tokens.items():
        if secrets.compare_digest(given.encode(), token.encode()):
            found = host
    return found


def _num(v):
    return v is None or (isinstance(v, (int, float)) and not isinstance(v, bool))


def validate_push(info, host):
    """Why INFO is not a Windows info object from HOST, or None. `winrunner info` prints it."""
    if not isinstance(info, dict):
        return "body must be the JSON info object"
    if info.get("host") != host or not HOST_RE.match(str(info.get("host"))):
        return f"info.host must be {host}"
    if info.get("platform") != "windows":
        return "info.platform must be windows"
    for key in ("cores", "memory_gb", "uptime_s", "disk_total_gb", "disk_free_gb", "memory_free_pct"):
        if not _num(info.get(key)):
            return f"info.{key} must be a number"
    for key in ("settings", "battery"):
        if info.get(key) is not None and not isinstance(info[key], dict):
            return f"info.{key} must be an object"
    runners = info.get("runners")
    if runners is not None and (not isinstance(runners, list) or not all(isinstance(r, dict) for r in runners)):
        return "info.runners must be a list of objects"
    return None


def mac_info(host, via):
    base = {"host": host, "via": via, "at": now(), "run_url": None}
    if via == "ssh":
        rc, out, err = ssh(host, ["info"], 60)
    else:
        rc, out, err = macs(["info", host], 300)
        m = RUN_URL_RE.search(out + err)
        base["run_url"] = m.group(0) if m else None
    info = last_json(out) if rc == 0 else None
    if info is None:
        why = tail(err or out, 3) or f"exit {rc}"
        if rc == 0:
            why = "no info came back (is macrunner on the Mac older than this dashboard? try Update)"
        return {
            **base,
            "ok": False,
            "info": None,
            "error": why,
            "reason": offline_reason(rc, err or out) if via == "ssh" else None,
        }
    return {**base, "ok": True, "info": info, "error": None}


def valid_cores(cores):
    """The max-cores value a request may carry: "all" or a whole number from 1 to 256."""
    return cores == "all" or (type(cores) is int and 1 <= cores <= 256)


PC_HOST_RE = re.compile(r"^(?:win|wsl)-\d+$")
WSL_RUNNER_RE = re.compile(r"^win-(\d+)-wsl-\d+$")


def is_pc_host(host):
    """win-N (the Windows PC) or wsl-N (its WSL distro): reached through the Admin workflow only."""
    return bool(PC_HOST_RE.match(host or ""))


def admin_host(device_host, runner):
    """The host whose Admin workflow manages RUNNER on a device. A Mac is its own host. On a PC the
    Windows runners (win-N) belong to win-N and the WSL runners (win-N-wsl-M) to wsl-N."""
    m = WSL_RUNNER_RE.match(runner.lower())
    if m and is_pc_host(device_host):
        return f"wsl-{m.group(1)}"
    return device_host


def limit_pairs(cores=None, ram_mb=None):
    """The `limit` arguments: cores=<N|default>, ram=<MB|default>. None leaves that one out."""
    out = []
    for key, v in (("cores", cores), ("ram", ram_mb)):
        if v is not None:
            out.append(f"{key}={'default' if v == 'default' else v}")
    return out


def parse_limit_action(body):
    """The `limit` action's body -> (cores, ram, runner, error). cores and ram_mb are each "default", a
    whole number in range, or absent (left alone); at least one is needed, and runner names the runner."""
    runner = body.get("runner")
    if not isinstance(runner, str) or not RUNNER_RE.match(runner.lower()):
        return None, None, None, "runner is not a valid runner name"
    out = []
    for key, (lo, hi) in (("cores", CORES_RANGE), ("ram_mb", RAM_MB_RANGE)):
        v = body.get(key)
        if v is None or v == "default" or _int_in(v, lo, hi):
            out.append(v)
        else:
            return None, None, None, f"{key} is default or a whole number from {lo} to {hi}"
    if out == [None, None]:
        return None, None, None, "nothing to change: send cores and/or ram_mb"
    return out[0], out[1], runner.lower(), None


def mac_action(host, action, size, via, cores="all", runner=None, ram=None):
    """Run ACTION on HOST: over SSH (Macs), else through the Admin workflow. For `limit`, cores and ram
    are each "default", a number, or None (left alone). win-N and wsl-N hosts have no SSH."""
    over_ssh, over_github = ACTIONS[action]
    if is_pc_host(host):
        over_ssh = None
    pairs = limit_pairs(cores, ram) if action == "limit" else []

    def fill(args):
        out = []
        for a in args:
            if a == "{limits}":
                out.extend(pairs)
            else:
                out.append(a.format(host=host, size=size, cores=cores, runner=runner))
        return out

    if over_ssh and via != "actions":
        rc, out, err = ssh(host, fill(over_ssh), 900)
        # 255 = ssh could not connect: try the workflow. Anything else (a timeout included,
        # when the action may have half run) is the answer, so it stands.
        if rc != 255:
            text = (out + ("\n" + err if err.strip() else "")).strip()
            why = f"{rc} problem(s) found" if action == "doctor" else tail(err or out, 2) or f"exit {rc}"
            return {"ok": rc == 0, "via": "ssh", "output": text, "run_url": None, "error": None if rc == 0 else why}
        ssh_error = tail(err, 1)
    else:
        ssh_error = None
    rc, out, err = macs(fill(over_github), 900)
    text = (out + ("\n" + err if err.strip() else "")).strip()
    if ssh_error:
        text = f"(SSH failed: {ssh_error}; used GitHub instead)\n{text}"
    m = RUN_URL_RE.search(text)
    return {
        "ok": rc == 0,
        "via": "actions",
        "output": text,
        "run_url": m.group(0) if m else None,
        "error": None if rc == 0 else tail(err or out, 2) or f"exit {rc}",
    }


LOGIN_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark"><link rel="icon" href="data:,"><title>runnerpool: sign in</title>
<style>
:root { --bg:#f4f5f7; --card:#fff; --text:#1c2129; --muted:#667085; --line:#e3e6ec; --accent:#2f6fed; --red:#b42318; --red-bg:#fde4e1; }
@media (prefers-color-scheme: dark) { :root { --bg:#12151a; --card:#1a1e25; --text:#e6e9ef; --muted:#97a0af; --line:#2b313b; --accent:#6b9bff; --red:#ff8f84; --red-bg:#431a17; } }
* { box-sizing: border-box; }
body { margin:0; min-height:100vh; display:grid; place-items:center; padding:16px; background:var(--bg); color:var(--text);
  font:15px/1.45 system-ui,-apple-system,"Segoe UI",Roboto,Arial,sans-serif; }
form { width:100%; max-width:340px; background:var(--card); border:1px solid var(--line); border-radius:14px; padding:22px; }
h1 { font-size:20px; margin:0 0 4px; } p { margin:0 0 16px; color:var(--muted); font-size:13px; }
label { display:block; font-size:12px; color:var(--muted); margin-bottom:4px; }
input[type=password] { width:100%; font:inherit; color:var(--text); background:var(--bg); border:1px solid var(--line); border-radius:8px; padding:9px 11px; }
button { width:100%; margin-top:14px; font:inherit; font-weight:600; color:#fff; background:var(--accent); border:0; border-radius:8px; padding:10px; cursor:pointer; }
input:focus-visible, button:focus-visible { outline:2px solid var(--accent); outline-offset:2px; }
.err { background:var(--red-bg); color:var(--red); border-radius:8px; padding:8px 10px; margin-bottom:14px; font-size:13px; }
</style></head><body>
<form method="post" action="/login">
<h1>runnerpool</h1><p>Sign in to see the runners and control the Macs.</p>
@@ERROR@@
<input type="text" name="username" value="dashboard" autocomplete="username" hidden>
<label for="pw">Password</label>
<input id="pw" type="password" name="password" autocomplete="current-password" autofocus required>
<button type="submit">Sign in</button>
</form></body></html>"""
LOGIN_FAIL_LIMIT = 5  # failed logins per minute before a login lockout
FAILS = []
FAILS_LOCK = threading.Lock()


def check_password(given, stored):
    """stored is a plain token, or 'scrypt$N$r$p$salt$hash' (base64) made by scripts/set-dashboard-password.sh."""
    if not stored.startswith("scrypt$"):
        return secrets.compare_digest(given.encode(), stored.encode())
    try:
        _, n, r, p, salt, want = stored.split("$")
        got = hashlib.scrypt(
            given.encode(),
            salt=base64.b64decode(salt),
            n=int(n),
            r=int(r),
            p=int(p),
            maxmem=128 * 1024 * 1024,
            dklen=32,
        )
        return secrets.compare_digest(got, base64.b64decode(want))
    except (ValueError, TypeError):
        return False


def login_blocked():
    cutoff = time.time() - 60
    with FAILS_LOCK:
        FAILS[:] = [t for t in FAILS if t > cutoff]
        return len(FAILS) >= LOGIN_FAIL_LIMIT


def fixed_token():
    """The access token set in the environment (MACS_TOKEN, or the file MACS_TOKEN_FILE), else ''."""
    path = os.environ.get("MACS_TOKEN_FILE")
    if path:
        try:
            value = Path(path).read_text(encoding="utf-8").strip()
        except OSError as e:
            sys.exit(f"macs dashboard: cannot read MACS_TOKEN_FILE: {e.strerror}")
        if not value:
            sys.exit("macs dashboard: MACS_TOKEN_FILE is empty")
        return value
    return os.environ.get("MACS_TOKEN", "").strip()


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "macs-dashboard"
    token = ""
    port = 0
    extra_hosts = ()  # Host names accepted besides 127.0.0.1 and localhost (MACS_ALLOWED_HOSTS)

    def log_message(self, fmt, *args):
        if (
            not self.path.startswith(("/api/runners", "/api/runs", "/api/jobs", "/api/ci", "/healthz"))
            and "/info" not in self.path
        ):
            line = re.sub(r"([?&]t=)[^\s&\"]*", r"\1REDACTED", fmt % args)  # never log the token
            sys.stderr.write("%s %s\n" % (time.strftime("%H:%M:%S"), line))

    def session(self):
        """The cookie value that proves a sign-in: derived from the token, never the token itself."""
        return hashlib.sha256(b"macs-dashboard-session:" + self.token.encode()).hexdigest()

    def signed_in(self):
        if not self.token.startswith("scrypt$") and secrets.compare_digest(
            self.headers.get("X-Macs-Token", ""), self.token
        ):
            return True
        for part in (self.headers.get("Cookie") or "").split(";"):
            name, _, value = part.strip().partition("=")
            if name == "macs_session" and secrets.compare_digest(value, self.session()):
                return True
        return False

    def redirect_home(self, cookie=False, to="/", clear=False):
        self.send_response(303)
        self.send_header("Location", to)
        if clear:
            self.send_header("Set-Cookie", "macs_session=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0")
        if cookie:
            secure = "; Secure" if self.headers.get("X-Forwarded-Proto") == "https" else ""
            self.send_header(
                "Set-Cookie",
                f"macs_session={self.session()}; Path=/; HttpOnly; SameSite=Strict; Max-Age=2592000{secure}",
            )
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
            "img-src 'self' data:; frame-ancestors 'none'",
        )
        self.end_headers()
        self.wfile.write(data)

    def allowed(self):
        # Host check stops DNS rebinding; the token (a custom header, so a cross-site page can't
        # send it without a CORS preflight this server never answers) stops everything else.
        host = self.headers.get("Host") or ""
        if host not in (f"127.0.0.1:{self.port}", f"localhost:{self.port}") and not (
            host.lower() in self.extra_hosts or host.lower().rsplit(":", 1)[0] in self.extra_hosts
        ):
            self.send(403, {"error": "wrong host"})
            return False
        if self.path == "/healthz":  # for the container's health check; reveals nothing
            self.send(200, {"ok": True})
            return False
        if self.command == "POST" and self.path == "/api/push-info":  # bearer token, no cookie, no Origin
            return True
        if self.path.startswith("/api/") and not self.signed_in():
            self.send(403, {"error": "not signed in: open the dashboard link with ?t=TOKEN"})
            return False
        if self.command == "POST":  # SameSite already blocks cross-site cookies; belt and braces
            origin = self.headers.get("Origin")
            # no-referrer makes browsers send "Origin: null" on same-origin form posts, so also
            # trust Sec-Fetch-Site, which a web page cannot set.
            if origin and urlparse(origin).netloc != host and self.headers.get("Sec-Fetch-Site") != "same-origin":
                self.send(403, {"error": "cross-origin request refused"})
                return False
        return True

    def do_GET(self):
        if not self.allowed():
            return
        url = urlparse(self.path)
        if url.path in ("/", "/index.html"):
            if "t" in parse_qs(url.query):  # sign in, then drop the token from the address bar
                given = parse_qs(url.query)["t"][0]
                return self.redirect_home(
                    cookie=not self.token.startswith("scrypt$") and secrets.compare_digest(given, self.token)
                )
            if not self.signed_in():
                err = '<div class="err" role="alert">Wrong password.</div>' if "e" in parse_qs(url.query) else ""
                if "e" in parse_qs(url.query) and login_blocked():
                    err = '<div class="err" role="alert">Too many tries. Wait a minute.</div>'
                return self.send(200, LOGIN_PAGE.replace("@@ERROR@@", err).encode(), "text/html; charset=utf-8")
            return self.send(200, (HERE / "index.html").read_bytes(), "text/html; charset=utf-8")
        if url.path.startswith("/api/"):
            STATE.touch()
        if url.path == "/api/runners":
            return self.send(200, STATE.runners_answer())
        if url.path == "/api/runs":
            return self.send(200, STATE.get_runs())
        if url.path == "/api/jobs":
            return self.send(200, STATE.get_jobs())
        if url.path == "/api/ci":
            return self.send(200, STATE.get_ci())
        m = re.fullmatch(r"/api/mac/([^/]+)/info", url.path)
        if m:
            host = m.group(1)
            via = parse_qs(url.query).get("via", ["ssh"])[0]
            if via not in ("ssh", "actions"):
                return self.send(400, {"error": "via is ssh or actions"})
            if not (STATE.known_mac(host) or STATE.known_pc(host)):
                return self.send(404, {"error": f"{host} is not a Mac or PC runner"})
            if via == "actions":
                return self.send(200, mac_info(host, via))
            return self.send(200, STATE.get_info(host, fresh="fresh" in parse_qs(url.query)))
        self.send(404, {"error": "not found"})

    def push_info(self):
        """POST /api/push-info: a PC reports its health. Auth is the PC's own bearer token (never a cookie)."""
        if STATE.push_blocked():
            return self.send(429, {"error": "too many bad pushes; wait a minute"})
        tokens = load_push_tokens()
        if not tokens:
            return self.send(503, {"error": "PC health push is not set up on the dashboard"})
        auth = self.headers.get("Authorization") or ""
        host = host_for_token(auth[7:].strip(), tokens) if auth.lower().startswith("bearer ") else None
        if not host:
            STATE.push_failed()
            return self.send(401, {"error": "bad or missing token"})
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if length < 0:
            return self.send(400, {"error": "bad Content-Length"})
        if length > PUSH_MAX_BODY:
            return self.send(413, {"error": f"body is over {PUSH_MAX_BODY} bytes"})
        try:
            info = json.loads(self.rfile.read(length) or b"null")
        except ValueError:
            return self.send(400, {"error": "body is not JSON"})
        error = validate_push(info, host)
        if error:
            return self.send(403 if "info.host" in error else 400, {"error": error})
        wait = STATE.push_info(host, info)
        if wait:
            return self.send(429, {"error": f"{host} pushed too recently; wait {wait} s"})
        self.send(200, {"ok": True, "host": host, "stale_after_s": PUSH_STALE})

    def set_runner_limits(self, body):
        host, runner, changes, error = validate_limits(body)
        if error:
            return self.send(400, {"error": error})
        via = body.get("via", "auto")
        if via not in ("auto", "actions"):
            return self.send(400, {"error": "via is auto or actions"})
        devices = STATE.get_runners().get("devices", [])
        dev = next((d for d in devices if d["host"] == host), None)
        if not dev or not any(r["kind"] == "ci" and r["name"].lower() == runner for r in dev["runners"]):
            return self.send(404, {"error": f"{runner} is not a CI runner on {host}"})
        try:
            override = save_limit(host, runner, changes)
        except OSError as e:
            return self.send(500, {"error": f"could not save: {e.strerror or e}"})
        # Saved either way; applying can fail (device asleep, offline), and the page offers a retry.
        res = STATE.apply_limit(host, runner, changes, via)
        self.send(
            200,
            {
                "ok": True,
                "host": host,
                "runner": runner,
                "override": override,
                "applied": res["applied"],
                "error": res["error"],
                "via": res.get("via"),
                "run_url": res.get("run_url"),
            },
        )

    def do_POST(self):
        if not self.allowed():
            return
        path = urlparse(self.path).path
        if path == "/api/push-info":
            return self.push_info()
        if path == "/logout":
            return self.redirect_home(clear=True)
        if path == "/login":
            if login_blocked():
                return self.redirect_home(to="/?e=1")
            raw = self.rfile.read(min(4096, max(0, int(self.headers.get("Content-Length") or 0))))
            given = parse_qs(raw.decode("utf-8", "replace")).get("password", [""])[0]
            if check_password(given, self.token):
                return self.redirect_home(cookie=True)
            with FAILS_LOCK:
                FAILS.append(time.time())
            time.sleep(1)  # slows guessing a little more
            return self.redirect_home(to="/?e=1")
        m = re.fullmatch(r"/api/mac/([^/]+)/action", path)
        if not m and path != "/api/runner-limits":
            return self.send(404, {"error": "not found"})
        try:
            length = max(0, int(self.headers.get("Content-Length") or 0))
            body = json.loads(self.rfile.read(min(length, 65536)) or b"{}")
        except ValueError:
            return self.send(400, {"error": "body is not JSON"})
        if not isinstance(body, dict):
            return self.send(400, {"error": "body must be a JSON object"})
        if path == "/api/runner-limits":
            return self.set_runner_limits(body)
        host = m.group(1)
        action, via = body.get("action"), body.get("via", "auto")
        size = body.get("size", 4)
        cores = body.get("cores", "all")
        if action not in ACTIONS or via not in ("auto", "actions"):
            return self.send(400, {"error": f"unknown action {action!r}"})
        if type(size) is not int or not 1 <= size <= 8:
            return self.send(400, {"error": "size is 1 to 8 GB"})
        runner = ram = None
        if action == "limit":
            cores, ram, runner, error = parse_limit_action(body)
            if error:
                return self.send(400, {"error": error})
        elif not valid_cores(cores):
            return self.send(400, {"error": "cores is a number or all"})
        if not STATE.known_host(host, action):
            return self.send(404, {"error": f"{host} is not a Mac runner or a PC that supports {action}"})
        if STATE.already_set(host, action):
            return self.send(200, {"ok": True, "via": None, "output": "already set", "run_url": None, "error": None})
        busy = STATE.acquire(host, action, via)
        if busy:
            return self.send(409, {"error": busy})
        try:
            print(f"{time.strftime('%H:%M:%S')} {action} on {host} ({via})", file=sys.stderr)
            result = mac_action(host, action, size, via, cores, runner, ram)
        finally:
            STATE.release(host)
        self.send(200, result)


def main():
    ap = argparse.ArgumentParser(prog="macs dashboard")
    ap.add_argument("--port", type=int, default=int(os.environ.get("MACS_PORT", 8765)))
    ap.add_argument("--no-open", action="store_true", help="don't open the browser")
    opts = ap.parse_args()
    if not shutil.which("gh"):
        sys.exit("macs dashboard: gh not found (it reads the runner lists)")
    Handler.token = fixed_token() or secrets.token_urlsafe(24)
    if not Handler.token.startswith("scrypt$") and len(Handler.token) < 10:
        sys.exit("macs dashboard: the access token / password must be at least 10 characters")
    Handler.extra_hosts = tuple(
        h.strip().lower() for h in os.environ.get("MACS_ALLOWED_HOSTS", "").split(",") if h.strip()
    )
    bind = os.environ.get("MACS_BIND", "127.0.0.1")
    server = None
    for port in range(opts.port, opts.port + 10):
        try:
            server = http.server.ThreadingHTTPServer((bind, port), Handler)
            break
        except OSError:
            continue
    if server is None:
        sys.exit(f"macs dashboard: ports {opts.port}-{opts.port + 9} are all in use")
    Handler.port = server.server_address[1]
    server.daemon_threads = True
    url = f"http://127.0.0.1:{Handler.port}/?t={Handler.token}"
    if os.environ.get("MACS_TOKEN") or os.environ.get("MACS_TOKEN_FILE"):  # never print a fixed token
        print(f"runnerpool dashboard listening on {bind}:{Handler.port} (sign in with the password)", flush=True)
    else:
        print(f"runnerpool dashboard: {url}\nCtrl+C stops it.", flush=True)
    if not opts.no_open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
