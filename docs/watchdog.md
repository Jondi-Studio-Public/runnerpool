# CI watchdog

A small service on your server (the same Docker host as the dashboard) that watches the org's self-hosted runners and
pushes phone alerts through ntfy. It is `watchdog/watchdog.py` (Python, standard library only),
built into the same image as the dashboard and run as a second container, `ci-watchdog`, from the
same `compose.yaml`. It reuses the dashboard's `gh_token` and `scripts/deploy.sh`.

Every minute it reads the GitHub REST API (org runners; queued and in-progress runs and their jobs
in every unarchived org repo; recently failed runs) and applies five rules:

| Rule | Fires when | Result |
|---|---|---|
| Pool offline | a queued job's labels are carried by no online runner, for 30 min | one alert naming each device carrying those labels and when it was last seen online; re-arms once the pool is served |
| Long queue | a job has been queued over 15 min | one alert per job: repo, job, labels |
| Lost runner | a job failed because its runner lost contact | the job is re-run once; an alert only if the re-run fails too |
| Stale runner | a runner has been offline 3 days | one alert so the registration can be removed |
| Daily digest | the configured hour, in the configured timezone | runs, median and max queue time, jobs per device over the last 24 h |

## How a lost runner is recognised

When a runner dies mid-job GitHub fails the job with an annotation on its check run (`GET
/repos/{repo}/check-runs/{job_id}/annotations`), "The self-hosted runner: NAME lost communication
with the server" or "The runner has received a shutdown signal". The watchdog matches those
messages. If annotations cannot be read it falls back to the step evidence: job `failure`, no step
failed, and a step that started but never completed. A job with a failed step, or cancelled by a
person, is a real failure and is never re-run.

The re-run uses `POST /repos/{repo}/actions/jobs/{job_id}/rerun`. Its record (`repo/run id/job name`)
is saved to `/data/state.json` before the request is sent, so a restart, a crash or a network error
can never lead to a second re-run; only a definite refusal (for example other jobs of the run still
running) clears it for a retry, up to 5 tries, then it alerts. A later failure of that same job in
the same run, after the re-run, sends "CI re-run also failed" once.

GitHub does not report when a runner was last online, so "last seen" and "offline for 3 days" come
from the watchdog's own sightings, kept in `state.json`. A runner it has never seen online counts
from the first time it saw it.

## Secrets and config

Secret names (values live in your secret manager; never printed or logged):

- `gh_app_id`, `gh_app_key`: the CI GitHub App (fields `app_id`, `private_key`),
  shared with the dashboard and copied by `scripts/provision-secrets.sh`. When both are non-empty the
  watchdog mints 1-hour installation tokens (refreshed about 10 minutes before expiry) and uses them
  instead of `gh_token`; the App needs the permissions in
  [dashboard-deploy.md](dashboard-deploy.md#the-ci-github-app), including Actions read and write
  for the job re-runs. If the App cannot mint a token it falls back to `gh_token`.
- `gh_token`: the dashboard's existing token (see dashboard-deploy.md). It already has Actions
  read and write on all repositories, Checks read (job annotations) and the Self-hosted runners
  read org permission, which is all the watchdog needs. Nothing new is required.
- `ntfy_topic`: the ntfy topic, from your secret manager, field `topic`. Treat it as a secret:
  on a public ntfy server anyone who knows the topic can read it. Without it, alerts are only logged.
- `ntfy_token`: optional, same item, field `token`, for a protected ntfy server.

Settings are environment variables on the `watchdog` service in `compose.yaml`:

| Variable | Default |
|---|---|
| `WATCHDOG_NTFY_URL` | `https://ntfy.sh` |
| `WATCHDOG_NTFY_TOPIC` | the `ntfy_topic` secret file (the variable wins if set) |
| `WATCHDOG_POLL_SECONDS` | `120` |
| `WATCHDOG_POOL_OFFLINE_MINUTES` | `30` |
| `WATCHDOG_QUEUE_MINUTES` | `15` |
| `WATCHDOG_STALE_DAYS` | `3` |
| `WATCHDOG_DIGEST_HOUR`, `WATCHDOG_TZ` | `7`, `UTC` |
| `WATCHDOG_RERUN_MAX_AGE_MINUTES` | `120`: a lost job older than this is left alone |
| `WATCHDOG_RERUN_MAX_TRIES` | `5` refused re-run requests before it alerts |
| `WATCHDOG_API_FAIL_ALERT_CYCLES` | `10` failed polls in a row before "CI watchdog is blind" |
| `CI_STORE` | `0`: poll GitHub as before. `1`: store mode, see below |
| `CI_DB` | `/ci/ci.db`: the shared SQLite store (store mode) |
| `WATCHDOG_RECONCILE_SECONDS` | `300`: how often store mode re-reads GitHub to catch missed webhooks |
| `WATCHDOG_STORE_MAX_AGE` | `900`: the store is trusted only if reconciled this recently, else the cycle polls |
| `WATCHDOG_ORG`, `WATCHDOG_REPOS` | `example-org`, every unarchived org repo (or a space-separated list) |

## Deploy

Git Bash with Windows OpenSSH first on PATH (`export PATH=/c/Windows/System32/OpenSSH:$PATH`), as in
[dashboard-deploy.md](dashboard-deploy.md). First put the ntfy topic (and token if the server needs
one) in your secret manager, then:

```bash
scripts/provision-watchdog.sh     # copies ntfy_topic and ntfy_token to the server, no value shown
scripts/deploy.sh                 # builds the image, recreates both containers
ssh root@192.0.2.10 docker logs --tail 20 ci-watchdog
```

The log should say `watchdog started` and, with a topic set, not `no ntfy topic configured`. In
PowerShell the same works through Git Bash: `& "$env:ProgramFiles\Git\bin\bash.exe" -c "scripts/deploy.sh"`.
`docker ps` shows `ci-watchdog` as healthy once a poll has completed. Subscribe a phone to the topic
in the ntfy app. To test the path end to end, run `scripts/deploy.sh` and look for the first digest
at the configured hour, or lower `WATCHDOG_QUEUE_MINUTES` temporarily and queue a job.

The state volume `watchdog-data` survives redeploys; deleting it forgets sightings, re-run records
and sent alerts, so do not delete it while a lost-runner failure is recent.

## Store mode (`CI_STORE=1`)

Unset or `0` the watchdog polls exactly as described above. With `CI_STORE=1` it opens the shared
SQLite store (`CI_DB`) that the webhook receiver fills, and the rules read from it:

- **Reads from the store:** queued jobs (long queue, pool offline) and failed or cancelled runs with
  their jobs (lost runner). No GitHub calls for them.
- **Reconcile** every `WATCHDOG_RECONCILE_SECONDS` (first cycle at once): per repo it lists live
  (queued, in progress) runs and the latest 30, fetches jobs for live runs, for failed or cancelled runs
  within the re-run window whose jobs are not held complete, and for runs whose jobs are still held as
  unfinished. A run the store holds as live that GitHub no longer lists as live is fetched by id and
  stored with its jobs (it finished while the event was missed). Then the store is marked reconciled.
  A GitHub error leaves it unmarked and the loop carries on.
- **Trust:** the store is used only while the last reconcile is younger than `WATCHDOG_STORE_MAX_AGE`;
  otherwise that cycle polls GitHub as before.
- **Still polled:** the org runner list (no webhook for runner status), the repo list, job annotations,
  the re-run POST and the daily digest.
- **ETags:** every GET sends `If-None-Match` from a bounded per-URL cache; a 304 costs no quota.
- **Rate limit:** `X-RateLimit-*` of every response is kept, a log line is written once each time
  remaining drops below 500, and the store meta keys `ratelimit` (JSON) and `reconcile_seconds` are
  written after every cycle for the dashboard. The store is pruned once per cycle.

With the GitHub App webhook on (`CI_STORE=1`) the watchdog reads runs and jobs from the shared store instead of polling GitHub every minute: see [webhook.md](webhook.md).
