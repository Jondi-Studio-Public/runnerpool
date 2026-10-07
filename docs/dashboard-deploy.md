# Dashboard on your server

`dashboard/` runs as the `macs-dashboard` container on a Docker host you control (example: `192.0.2.10`,
port `8765`), behind a reverse proxy (Caddy, nginx or similar) at `https://runners.example.com`, reachable
only on your LAN and tailnet. It is protected only by a password and can change the Macs, so it must
not be public.

## Required configuration

Nothing here has a built-in value for your org or server: set these (shell environment for the scripts,
`.env` next to `compose.yaml` or the repo's Actions secrets and variables for deploys).

| Name | Where | Meaning |
|---|---|---|
| `GITRUNNER_ORG` | `compose.yaml`, `scripts/deploy.sh`, `runner`, installers | Your GitHub org (also the GHCR namespace of the image; use lowercase) |
| `DEPLOY_HOST` | `scripts/*.sh` | ssh target of the dashboard server, e.g. `root@192.0.2.10` |
| `REMOTE_CONTEXT` | `scripts/deploy.sh`, `set-dashboard-password.sh` | Docker context that points at that server |
| `MACS_ALLOWED_HOSTS` | `compose.yaml` | Host names the dashboard answers to, e.g. `runners.example.com,192.0.2.10` |
| `MAC_1_IP`, `MAC_2_IP` | `compose.yaml` | Tailnet IPs for `air-1`, `air-2` (placeholder defaults) |
| `DASHBOARD_URL` | `set-dashboard-password.sh` (optional) | Only used in the message it prints |
| `DEPLOY_SSH_KEY`, `DEPLOY_HOST` | Actions **secrets** of `deploy.yml` | Private key and ssh target for the automated deploy |
| `DEPLOY_HEALTH_URL`, `MACS_ALLOWED_HOSTS` | Actions **variables** of `deploy.yml` | Health URL (e.g. `http://192.0.2.10:8765/healthz`) and the host list above |

## Signing in

Open `https://runners.example.com` and type the password; the browser can remember it, so it
is a one-time step per device. A successful login sets an HttpOnly, SameSite=Strict cookie
(30 days) and "Sign out" clears it. Five wrong tries in a minute lock the login for a minute.
Choose or change the password yourself, in your own terminal (Git Bash, Windows OpenSSH first
on PATH):

```bash
scripts/set-dashboard-password.sh
```

It asks at a hidden prompt, stores only a salted scrypt hash on the server and restarts the
dashboard, which signs every device out. Until you run it, the generated token in
`dashboard_token` still works as the password (and as `/?t=<token>`). Once a hash is stored,
`?t=` and the `X-Macs-Token` header stop working. The server redacts `?t=` from its log, and
the reverse proxy should have no access log for it.

## Secrets

They live as files on the server in `/root/macs-dashboard/secrets/` (owner uid 1000, mode 400),
mounted into the container as `/run/secrets/gh_token` (exported as `GH_TOKEN` for `gh`) and
`/run/secrets/dashboard_token`, plus `gh_token_personal` (below) and `push_tokens` (PC health push, below). `scripts/provision-secrets.sh`
creates them with no value shown: `gh_token` is piped from your secret manager (for example `op read op://Example-Vault/Dashboard GitHub/token`), `gh_token_personal` likewise from a second item when it exists (else an empty file), and `dashboard_token` is
generated on the server (later replaced by the password hash). `scripts/deploy.sh` creates an
empty `gh_token_personal` if it is missing, since compose needs the file.

The GitHub token is a fine-grained PAT owned by example-org with:

- Repository access: **All repositories**, so a new org repo shows up in the CI panel by itself.
- Repository permissions: Actions read and write (runs panel, Admin-workflow fallback; write is
  only used on runnerpool), Administration read (repo runner lists), **Checks read** and
  **Pull requests read** (CI panel), Metadata read.
- Organization permission: Self-hosted runners read (org runner list; `MACS_ORG`, default
  example-org).

### The CI GitHub App

A GitHub App owned by example-org replaces the org PAT above (and `RUNNER_STATUS_TOKEN` in
[ci-plan.md](ci-plan.md)): its installation tokens last an hour, belong to no person and need no
rotation. It is created by an org owner, installed on the org with **All repositories**, and needs:

- Organization permission: **Self-hosted runners** read and write.
- Repository permissions: **Administration** read and write, **Actions** read and write (the
  watchdog re-runs lost jobs), **Checks** read, **Pull requests** read, **Contents** read,
  **Metadata** read.

Its id and private key (the `.pem`) live in your secret manager (for example an item `CI GitHub App` with fields `app_id` and
`private_key`); `scripts/provision-secrets.sh` (`--gh` to re-copy) writes them to the server as
`gh_app_id` and `gh_app_key`, no value shown, and empty files when the item is not there yet. GitHub
Actions get them as secrets `CI_APP_ID` and `CI_APP_PRIVATE_KEY` (org secrets, visible to all org
repos; a caller of ci-plan passes them explicitly in its `secrets:` block).

With both files non-empty the dashboard and the watchdog sign a JWT (RS256, 9 minutes, PyJWT in
`dashboard/requirements.txt`), look the installation up with `GET /orgs/example-org/installation`,
mint a token, cache it and mint a new one about 10 minutes before it expires
(`dashboard/gh_app_token.py`, one helper for both). Without them, or when minting fails, both keep
using `gh_token`, so the PAT stays valid until the App is proven; delete it afterwards.
The App needs `GITRUNNER_ORG` (compose passes it on as `GH_APP_OWNER`); without an org the dashboard and watchdog log a warning and keep using `gh_token`.
`gh_token_personal` is not affected: an org App cannot reach repos owned by a personal account.

A fine-grained token belongs to one account, so it cannot see another account's private repos.
`gh_token_personal` is an optional second fine-grained PAT owned by that account, on the listed
repos only, with Actions read, Checks read, Pull requests read and Metadata read. Without it the CI panel
still reads public repos with the org token, and shows private ones as "not visible to the dashboard's
GitHub token". Moving a repo into the org removes the need for it.

## Deploy

### Normal path

Merging to `main` deploys: once CI is green, `.github/workflows/deploy.yml` builds the image on a
self-hosted `linux-ci` runner (it needs Docker), pushes `ghcr.io/example-org/macs-dashboard:<12-char sha>`, copies the compose files to
`/root/macs-dashboard` and recreates the dashboard and `ci-watchdog` there. It runs on every green
`main`, not only dashboard changes (`workflow_run` has no path filter), so a runner-tooling merge
also restarts both containers. To redeploy or roll back, use Actions > Deploy > Run workflow with
`tag` set to an earlier 12-character commit sha, or in PowerShell:

```powershell
gh workflow run deploy.yml -R example-org/runnerpool -f tag=<12-char sha>
```

The compose project is `macs-dashboard` (`name:` in `compose.yaml`; `docker compose ls` on the server
shows it). Provisioning the secrets (below) is still done by hand.

### Manual fallback

Only when GitHub Actions or the runners are down. Git Bash with Windows OpenSSH first on PATH (`export PATH=/c/Windows/System32/OpenSSH:$PATH`):
`scripts/provision-secrets.sh` (first time), then `scripts/deploy.sh`.
After editing or replacing a GitHub token in your secret manager: `scripts/provision-secrets.sh --gh`
(copies both GitHub tokens again, leaves the password alone), then `scripts/deploy.sh`.
"Bad credentials (HTTP 401)" on every panel means the server's `gh_token` is revoked or expired.
`scripts/deploy.sh --rollback <sha>` undoes a deploy (the sha is the 12-character image tag).

## What the page shows

Online/offline state and controls for each Mac, PC runners (read-only), stats tiles (success
rate, average duration, running and queued runs, runners online, runs per day) and the 30
latest workflow runs across the repos. Runs come from one `gh api .../actions/runs` call per
repo, cached for 60 seconds on the server however many devices are watching.

The **CI** panel has a row per live repo: every unarchived example-org repo the token can see,
plus `MACS_CI_EXTRA`. Each row shows the repo's `ci-ok` check (your CI's required
check; change it with `MACS_CI_CHECK`) on its default branch, the same check on up to 10 open PRs ("no ci-ok" means the
commit has no such check), and its running and queued workflow runs. A refresh is about
three `gh` calls per repo plus one per open PR, cached for 120 seconds.

## Settings (environment, all opt-in)

| Variable | Meaning | Default |
|---|---|---|
| `MACS_BIND` | address to listen on | `127.0.0.1` (the image sets `0.0.0.0`) |
| `MACS_ORG` | the GitHub org whose runners and repos are shown (compose sets it from `GITRUNNER_ORG`) | `example-org` (placeholder) |
| `MACS_ADMIN_REPO` | the repo holding the Admin workflows | `<MACS_ORG>/runnerpool` |
| `MACS_REPOS` | space-separated `owner/repo` list for the runs panel | the admin repo |
| `MACS_ALLOWED_HOSTS` | Host names accepted, comma-separated. **Required by `compose.yaml`** (e.g. `runners.example.com,192.0.2.10`) | none |
| `MACS_TOKEN` / `MACS_TOKEN_FILE` | fixed access token or password hash (10+ chars) | random per start |
| `MACS_CI_EXTRA` | repos outside `MACS_ORG` for the CI panel, space-separated | none (example: `some-user/some-repo`) |
| `MACS_CI_REPOS` | the CI panel's whole repo list, replacing the org listing and `MACS_CI_EXTRA` | none |
| `MACS_CI_CHECK` | the check each CI row reads | `ci-ok` |
| `MACS_CI_TOKEN_FILES` | `owner=/path` pairs: a token file for that owner's repos (empty file = use `GH_TOKEN`) | none (compose passes your `MACS_CI_TOKEN_FILES` through, e.g. `some-user=/run/secrets/gh_token_personal`) |
| `MACS_PUSH_TOKENS_FILE` | file of `win-N=<token>` lines: the PCs allowed to push health to `/api/push-info` (empty or absent = push off) | none (compose sets `/run/secrets/push_tokens`) |

## Reaching the Macs

The server (a VM or box) is on the tailnet under its own name, joined with `tailscale up
--accept-dns=false` (`scripts/join-tailnet.sh` does the same with an auth key from your secret manager).
The container's traffic leaves through the host's Tailscale, so Tailscale SSH sees the server
and `root@air-1` works with no key in the container. `compose.yaml` maps `air-1` and `air-2`
to the tailnet IPs in `MAC_1_IP` and `MAC_2_IP` (placeholder defaults; set your own) because the server does not use MagicDNS. A third Mac needs an
`extra_hosts` line. If SSH fails, changes and refreshes fall back to the Admin workflow.
A Tailscale subnet router does not help here: it carries tailnet-to-LAN traffic only.

## PC health push

A Windows PC cannot be reached over Tailscale SSH, so it reports to the dashboard instead: the power
watch POSTs `winrunner info` (specs, memory, disk, battery, settings, runner states) to
`POST /api/push-info` every 30 s with a per-PC bearer token. The card shows "Reported N s ago", and
"PC has not reported for N s" (last good data kept) after 90 s of silence. The dashboard never polls a
PC. Changes (CI on/off, cores, limits, restart, doctor, logs) still go through the Admin workflow.

The token is yours to make; nothing below prints it. In Windows PowerShell on the PC, as administrator
(`win-1` is the PC's runner name):

```powershell
$b = New-Object byte[] 32; [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($b)
$tok = ($b | ForEach-Object { $_.ToString('x2') }) -join ''
Set-Content -NoNewline -Path "$env:TEMP\push-token.txt" -Value $tok
Set-Clipboard "win-1=$tok"      # the line to paste into your secret manager, below
```

1. In your secret manager create an item (for example **Dashboard PC push tokens**) with a field `token`
   holding `win-1=<token>` (paste from the clipboard; one line per PC). Then clear the clipboard.
2. On the server: `scripts/provision-secrets.sh --push`, then `scripts/deploy.sh` (or restart the
   container). The tokens file is the `push_tokens` secret, `MACS_PUSH_TOKENS_FILE`.
3. On the PC, give winrunner the dashboard's address and the token file, then delete the file:

```powershell
& "$env:ProgramData\win-runners\winrunner.ps1" push-setup https://runners.example.com "$env:TEMP\push-token.txt"
Remove-Item "$env:TEMP\push-token.txt"
& "$env:ProgramData\win-runners\winrunner.ps1" push-test     # HTTP 200 = accepted
```

`push-setup` writes `C:\ProgramData\win-runners\dashboard-push.json` (url, host) and the token to
`dashboard-push-token` (SYSTEM and Administrators only). Push only starts once the PC runs this
version of winrunner: `.\runner.cmd update win-1` from the control PC. Without the json file nothing
is sent. The server answers 401 for an unknown token, 403 if the info names another host, 429 when a PC
pushes more than once per 5 s (or after 20 bad tokens in a minute), and 503 while the secret is empty.

## Webhook receiver

A third container, `ci-webhook`, takes the GitHub App's `workflow_job` and `workflow_run` webhooks into a shared store so the dashboard stops polling GitHub: setup, Funnel and rollback are in [webhook.md](webhook.md).
