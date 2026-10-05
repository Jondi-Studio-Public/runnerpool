# GitHub webhook receiver

## What it does and why

The CI GitHub App sends `workflow_job` and `workflow_run` events to `ci-webhook` (`webhook/receiver.py`)
on the dashboard server. It writes them into one SQLite file (`/ci/ci.db`, `dashboard/ci_store.py`) on the
`ci-state` volume, shared by the dashboard and the watchdog. They read that instead of polling GitHub,
which saves API quota and makes the CI panel and queue alerts near real time.

- The receiver holds **no GitHub credentials**, only the webhook secret.
- The watchdog still runs a slow reconcile poll, so a missed delivery is filled in within minutes.
- Runner online/offline status has no webhook event, so it stays polled.
- `CI_STORE=0` (the default) keeps the dashboard and watchdog on direct polling; the receiver still
  fills the store, so it is warm when you switch on.

## Security

- Every delivery needs `X-Hub-Signature-256` (HMAC-SHA256 of the raw body, constant-time compare);
  a bad or missing one gets an empty 401 and nothing is parsed.
- Body capped at 1 MB (checked before reading), 10 s read timeout, only `POST /webhook` is served
  (everything else is an empty 404/405), payloads for another org are dropped.
- The container is read-only, no capabilities, 64 MB, published on host loopback only
  (`127.0.0.1:8766`).
- Tailscale Funnel exposes only the `/webhook` path of that port. The dashboard is never exposed.

## Setup, in order

Nothing here prints the secret. This ships off by default: until step 6 the dashboard and watchdog poll
GitHub exactly as before.

1. **Generate the secret (PowerShell).** Stores 64 hex characters straight into 1Password
   (item `CI GitHub App`, field `webhook_secret`, in the vault your `dashboard/*.env.op` files name); the
   value is never shown:

   ```powershell
   $b = [byte[]]::new(32)
   [System.Security.Cryptography.RandomNumberGenerator]::Fill($b)
   $s = -join ($b | ForEach-Object { '{0:x2}' -f $_ })
   op item edit "CI GitHub App" --vault Example-Vault "webhook_secret[concealed]=$s"
   Remove-Variable s, b
   ```

2. **Provision and deploy.** `scripts/provision-secrets.sh --webhook` copies the secret from 1Password
   (`dashboard/webhook-secret.env.op`) to `/root/macs-dashboard/secrets/webhook_secret` on the server
   through `agent-run`. Then deploy as in [dashboard-deploy.md](dashboard-deploy.md) (the deploy
   workflow starts `ci-webhook`, or `scripts/deploy.sh`). The store stays dark (`CI_STORE=0`): nothing
   reads it yet.
3. **Funnel (on the server).** Prerequisites: HTTPS certificates enabled in the tailnet, and the
   `funnel` nodeAttr for the host in the tailnet policy. Then:

   ```sh
   tailscale funnel --bg --set-path /webhook http://127.0.0.1:8766/webhook
   tailscale funnel status
   ```

   Turn it off with `tailscale funnel reset`. Any other way of publishing `127.0.0.1:8766/webhook` over
   HTTPS works too (a reverse proxy, say); expose only that path.
4. **GitHub App settings (github.com > Org settings > Developer settings > GitHub Apps > your CI App).**
   Webhook: Active; URL `https://runners.example.com/webhook` (your public HTTPS name); Secret: the same
   value (paste it from 1Password). Permissions: Actions read. Subscribe to events: Workflow job and
   Workflow run. The receiver drops deliveries for any owner other than `GITRUNNER_ORG` (its
   `WEBHOOK_ORG`).
5. **Verify.** App > Advanced > Recent Deliveries shows 200 (use Redeliver or the ping). On the host:
   `docker logs ci-webhook` shows one line per delivery (event, action, result, delivery id).
6. **Switch on.** Put `CI_STORE=1` in the `.env` file next to `compose.yaml` on the server
   (`/root/macs-dashboard/.env`; compose reads it, the deploy does not overwrite it) and redeploy. The
   dashboard footer line shows the store is live and how fresh it is.

## Rollback

Set `CI_STORE=0` (or remove it from `.env`) and redeploy (back to polling); `tailscale funnel reset`; untick Webhook Active in the
App settings. The store is disposable: delete the `ci-state` volume to start empty.

Until the secret is provisioned the `ci-webhook` container exits cleanly at start (compose restarts it only on failure); provision the secret and redeploy to start it.

## Troubleshooting

- A delivery shows 401 in the App's Recent Deliveries: the secret in the App differs from `webhook_secret` on the server. Set the App's secret from the 1Password value again and redeliver.
- `docker logs ci-webhook` prints one line per delivery (event, result, delivery id). `ignored org` means the repository owner is not `WEBHOOK_ORG`.
- The dashboard footer says "polling GitHub": `CI_STORE` is off, or no reconcile has finished in the last 15 minutes (check `docker logs ci-watchdog`).
