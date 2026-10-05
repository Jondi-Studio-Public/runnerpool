"""The shared CI state store: workflow runs and jobs in one SQLite file.

Written by the webhook receiver (webhook/receiver.py, from signed GitHub deliveries) and by the
watchdog's slow reconcile poll; read by the dashboard and the watchdog so they stop polling GitHub
for the same data. Rows keep the GitHub REST object as JSON, so a reader sees the same shape the
Actions API returns (a webhook's `workflow_run` / `workflow_job` is that same object).

Out-of-order and replayed deliveries are harmless: a write only lands if it is not older than what
is stored (run attempt, then status progress, then updated_at), and a delivery id is applied once.

Standard library only. The file lives on a volume shared by the containers; WAL keeps readers from
blocking the writer.
"""

import json
import sqlite3
import threading
import time
from datetime import datetime, timezone

STATUS_RANK = {"queued": 0, "waiting": 0, "requested": 0, "pending": 0, "in_progress": 1, "completed": 2}
LIVE = ("queued", "in_progress", "waiting", "requested", "pending")
RUN_KEEP_DAYS = 8  # the dashboard charts 7 days
JOB_KEEP_DAYS = 3
DELIVERY_KEEP_SECONDS = 24 * 3600
EVENTS = ("workflow_job", "workflow_run")

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY, repo TEXT NOT NULL, run_attempt INTEGER NOT NULL, status TEXT NOT NULL,
  conclusion TEXT, created_at TEXT, updated_at TEXT, data TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS runs_repo ON runs (repo, created_at);
CREATE INDEX IF NOT EXISTS runs_status ON runs (status);
CREATE TABLE IF NOT EXISTS jobs (
  id INTEGER PRIMARY KEY, repo TEXT NOT NULL, run_id INTEGER NOT NULL, run_attempt INTEGER NOT NULL,
  status TEXT NOT NULL, conclusion TEXT, created_at TEXT, completed_at TEXT, runner_name TEXT,
  data TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS jobs_run ON jobs (repo, run_id);
CREATE INDEX IF NOT EXISTS jobs_status ON jobs (status);
CREATE TABLE IF NOT EXISTS deliveries (id TEXT PRIMARY KEY, at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""

# The webhook payload carries whole repository objects inside a run; keep only what readers use.
RUN_DROP = ("repository", "head_repository", "pull_requests", "head_commit", "referenced_workflows")


def iso_to_epoch(v):
    if not v:
        return 0.0
    try:
        dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).timestamp()


def _rank(status):
    return STATUS_RANK.get(status, 0)


class Store:
    def __init__(self, path, clock=time.time):
        self.path, self.now = str(path), clock
        self._lock = threading.Lock()
        self.db = sqlite3.connect(self.path, timeout=15, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        try:
            self.db.execute("PRAGMA journal_mode=WAL")
        except sqlite3.DatabaseError:
            pass
        self.db.executescript(SCHEMA)

    def close(self):
        self.db.close()

    # --- writes

    def apply_webhook(self, event, payload, delivery=""):
        """One verified delivery -> 'stored', 'stale' (older than what is held), 'duplicate',
        'ignored' (an event or shape we do not keep). The caller has already verified the signature."""
        if event not in EVENTS or not isinstance(payload, dict):
            return "ignored"
        repo = (payload.get("repository") or {}).get("full_name")
        obj = payload.get(event)
        if not repo or not isinstance(obj, dict) or not isinstance(obj.get("id"), int):
            return "ignored"
        with self._lock:
            if delivery and not self._first_delivery(delivery):
                return "duplicate"
            applied = self.upsert_run(repo, obj) if event == "workflow_run" else self.upsert_job(repo, obj)
            self.set_meta("webhook_last", str(self.now()))
        return "stored" if applied else "stale"

    def _first_delivery(self, delivery):
        cur = self.db.execute("INSERT OR IGNORE INTO deliveries (id, at) VALUES (?, ?)", (delivery, self.now()))
        return cur.rowcount == 1

    def upsert_run(self, repo, run):
        """Store a workflow run (webhook or REST object). True if it was newer than the held copy."""
        run = {k: v for k, v in run.items() if k not in RUN_DROP}
        attempt = int(run.get("run_attempt") or 1)
        row = self.db.execute("SELECT run_attempt, status, updated_at FROM runs WHERE id = ?", (run["id"],)).fetchone()
        new_key = (attempt, _rank(run.get("status")), iso_to_epoch(run.get("updated_at")))
        if row and (row["run_attempt"], _rank(row["status"]), iso_to_epoch(row["updated_at"])) > new_key:
            return False
        self.db.execute(
            "INSERT OR REPLACE INTO runs (id, repo, run_attempt, status, conclusion, created_at, updated_at, data)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (
                run["id"],
                repo,
                attempt,
                run.get("status") or "queued",
                run.get("conclusion"),
                run.get("created_at"),
                run.get("updated_at"),
                json.dumps(run, separators=(",", ":")),
            ),
        )
        return True

    def upsert_job(self, repo, job):
        """Store a workflow job (webhook or REST object). True if it was newer than the held copy."""
        attempt = int(job.get("run_attempt") or 1)
        row = self.db.execute("SELECT run_attempt, status FROM jobs WHERE id = ?", (job["id"],)).fetchone()
        if row and (row["run_attempt"], _rank(row["status"])) > (attempt, _rank(job.get("status"))):
            return False
        self.db.execute(
            "INSERT OR REPLACE INTO jobs (id, repo, run_id, run_attempt, status, conclusion, created_at,"
            " completed_at, runner_name, data) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                job["id"],
                repo,
                int(job.get("run_id") or 0),
                attempt,
                job.get("status") or "queued",
                job.get("conclusion"),
                job.get("created_at"),
                job.get("completed_at"),
                job.get("runner_name"),
                json.dumps(job, separators=(",", ":")),
            ),
        )
        return True

    def upsert_many(self, repo, runs=(), jobs=()):
        """Reconcile helper: many REST objects in one transaction."""
        with self._lock:
            self.db.execute("BEGIN")
            try:
                for r in runs:
                    self.upsert_run(repo, r)
                for j in jobs:
                    self.upsert_job(repo, j)
            except BaseException:
                self.db.execute("ROLLBACK")
                raise
            self.db.execute("COMMIT")

    def set_meta(self, key, value):
        self.db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, str(value)))

    def prune(self):
        now = self.now()
        run_cut = datetime.fromtimestamp(now - RUN_KEEP_DAYS * 86400, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        job_cut = datetime.fromtimestamp(now - JOB_KEEP_DAYS * 86400, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with self._lock:
            self.db.execute("DELETE FROM deliveries WHERE at < ?", (now - DELIVERY_KEEP_SECONDS,))
            self.db.execute("DELETE FROM runs WHERE status = 'completed' AND created_at < ?", (run_cut,))
            self.db.execute("DELETE FROM jobs WHERE status = 'completed' AND created_at < ?", (job_cut,))

    # --- reads

    @staticmethod
    def _load(row):
        d = json.loads(row["data"])
        d["_repo"] = row["repo"]
        return d

    def runs(self, repo, status=None, limit=100):
        """Runs of one repo, newest created first; status is one value, a tuple of values or None."""
        sql, args = "SELECT repo, data FROM runs WHERE repo = ?", [repo]
        if status:
            statuses = (status,) if isinstance(status, str) else tuple(status)
            sql += " AND status IN (%s)" % ",".join("?" * len(statuses))
            args += statuses
        sql += " ORDER BY created_at DESC LIMIT ?"
        return [self._load(r) for r in self.db.execute(sql, args + [limit])]

    def runs_by_conclusion(self, repo, conclusions, limit=100):
        marks = ",".join("?" * len(conclusions))
        sql = f"SELECT repo, data FROM runs WHERE repo = ? AND conclusion IN ({marks}) ORDER BY updated_at DESC LIMIT ?"
        return [self._load(r) for r in self.db.execute(sql, [repo, *conclusions, limit])]

    def jobs(self, repo=None, status=None, run_id=None):
        sql, args, where = "SELECT repo, data FROM jobs", [], []
        if repo:
            where.append("repo = ?")
            args.append(repo)
        if run_id:
            where.append("run_id = ?")
            args.append(run_id)
        if status:
            statuses = (status,) if isinstance(status, str) else tuple(status)
            where.append("status IN (%s)" % ",".join("?" * len(statuses)))
            args += statuses
        if where:
            sql += " WHERE " + " AND ".join(where)
        return [self._load(r) for r in self.db.execute(sql + " ORDER BY created_at", args)]

    def live_run_ids(self):
        """[(repo, run_id)] of runs the store still holds as queued or in progress."""
        marks = ",".join("?" * len(LIVE))
        return [
            (r["repo"], r["id"]) for r in self.db.execute(f"SELECT repo, id FROM runs WHERE status IN ({marks})", LIVE)
        ]

    def get_meta(self, key, default=None):
        row = self.db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def reconciled_ago(self):
        """Seconds since the last full reconcile, or None if there never was one."""
        v = self.get_meta("reconciled_at")
        return None if v is None else max(0.0, self.now() - float(v))

    def mark_reconciled(self):
        self.set_meta("reconciled_at", self.now())

    def webhook_ago(self):
        v = self.get_meta("webhook_last")
        return None if v is None else max(0.0, self.now() - float(v))

    def trusted(self, max_age):
        """True when readers may use the store instead of polling GitHub: a reconcile finished
        within max_age seconds (it re-reads every live run, so what webhooks missed is caught up)."""
        ago = self.reconciled_ago()
        return ago is not None and ago <= max_age
