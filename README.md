# runnerpool

Pool your Macs, Linux and Windows machines as self-hosted runners for GitHub Actions.

- **`runner` CLI**: list, check, restart, re-register, limit and update every machine from a single control
  machine (`runner list`, `runner doctor air-1`, ...). `rp` is a short alias; `macs` is an old one.
- **Installers**: one credential-carrying installer per OS (a Mac `.pkg`, a Windows `.ps1`, a Linux/WSL
  script) that you build yourself. Each machine names itself (`air-1`, `win-1`, `wsl-1`).
- **Dashboard** (optional): a web control panel with each machine's state, specs and CI health.
- **Watchdog** (optional): phone alerts (ntfy) for offline pools and long queues, and one automatic re-run of
  a job whose runner lost contact.
- **CI sharding and work stealing**: a reusable workflow and action that split one big test suite across
  whichever machines are online, so a fast machine does more and a slow one never holds the run up.

## The idea: pooled machines

Spare laptops and desktops are free CI capacity, but only if they are easy to run. runnerpool makes each one
plug-and-play: plugged in, it takes jobs and never sleeps; on battery, it finishes its job and pauses. Runners
run as an unprivileged hidden user, credentials are short-lived GitHub App tokens, and a script re-registers any
runner GitHub drops. CI that needs more capacity than one machine borrows the others through work stealing.

## Status: what is and is not tested

Be honest with yourself about this before relying on it:

| Area | State |
| --- | --- |
| `runner` CLI, Mac tool (`mac/gitrunner`), Linux tool, dashboard, watchdog, sharding and `steal` | Unit and shell tests in CI; used on real Apple Silicon Macs and Linux boxes by the original author |
| Mac installer `.pkg` | Built and content-checked in CI (scripts, arm64 binaries, `bootstrap.env` mode); **not** installed end to end on a clean Mac by CI. It is unsigned (see below) |
| Windows (`win/`, [docs/windows.md](docs/windows.md)) | **Untested on real hardware.** Written and parse-checked without a Windows install to test on; the first real install is the test |
| WSL job slots, Docker label on WSL runners, Mac Linux VM | Lightly tested; some paths are documented as untested in their docs |

## SECURITY: read this first

Self-hosted runners execute whatever a workflow tells them to run, on your hardware and your network.

- **Never attach self-hosted runners to public repositories, and never run fork pull requests on them.**
  Anyone can open a pull request that edits a workflow and run arbitrary code as the runner. Use these runners
  only for private repositories whose contributors you trust. GitHub's own guidance says the same.
- **Use runner groups.** Put the pool in an organisation runner group restricted to the specific private repos
  that need it (organisation Settings > Actions > Runner groups). Do not leave it in the default group open to
  all repos. In workflows, require approval for outside collaborators and avoid `pull_request_target` with
  checkouts of untrusted code.
- **Keep runners clean.** The runners here are persistent, not ephemeral: a job can leave files, processes,
  caches and credentials for the next job. Treat anything a job can reach as readable by every other job in
  the pool. Prefer a dedicated machine or VM, do not put personal data on a runner, do not log in to personal
  accounts on it, and wipe `_work` and temp folders between jobs where you can. If you need isolation per job,
  use ephemeral runners (`--ephemeral`) or a VM per job instead.
- **Least-privilege tokens.** Prefer a **GitHub App** over a personal access token (below). The only
  permissions the fleet needs are: organisation **Self-hosted runners: read and write**, and repository
  **Administration: read and write** (for repo-level admin runners) plus **Metadata: read**. The dashboard
  and watchdog additionally want **Actions** (read; write for the watchdog's re-runs), **Checks: read**,
  **Pull requests: read**. Install the App only on the repositories that use the runners.
  If you use a PAT, make it fine-grained, owned by the organisation, with the same scopes and an expiry.
- **Never publish a credential-baked installer.** The Mac `.pkg` and Windows `.ps1` that `runner build-local`
  or `runner publish` produce embed your GitHub App key (or token) and a Tailscale auth key. Do not upload them
  to a public release, a public repo, a chat or a shared drive. Publish only to a private release or a private
  file store, delete the file from Downloads after installing, and if one ever leaks, rotate the App key,
  revoke the Tailscale auth key and rebuild. The `.gitignore` excludes `*.pkg`, `*.pem`, `*.key` and `.env`; keep it that way.
- **The dashboard can change your machines.** It is protected only by a password. Never expose it to the
  public internet; put it behind a VPN or tailnet (and a reverse proxy with TLS).
- **Tailscale access controls.** The installer joins each Mac to your tailnet with a tagged key. Restrict the
  tag with Tailscale ACLs so only your admin identities can SSH to it.
- The Mac installer is **unsigned and un-notarised**; macOS makes you click Open Anyway. If you distribute
  it beyond your own machines, sign and notarise it yourself.

Report vulnerabilities privately: see [SECURITY.md](SECURITY.md).

## Prerequisites

- A GitHub **organisation** you administer (examples below use `example-org`). Org-level runners need one.
- A **GitHub App** (preferred) or a fine-grained PAT, with the scopes above.
- A free Tailscale account (a tailnet) if you want the SSH control path and a private dashboard.
- A control machine with `bash` and a signed-in [`gh`](https://cli.github.com): Linux, macOS, or Windows with
  Git for Windows (`runner.cmd` wraps it for PowerShell). Python 3.8+ for the local dashboard.
- Devices: Apple Silicon Macs (macOS 13+ expected; developed on 15 and 26), Windows 10/11 PCs, or Linux/WSL2 boxes with systemd.
- To build the Mac installer (`mac/build-local.sh`): Linux or WSL (Ubuntu) with
  `sudo apt-get install -y build-essential cpio golang-go libxml2-dev libssl-dev zlib1g-dev autoconf`. The first
  build also compiles `mkbom` (bomutils) and `xar` into `~/pkgtools`. `golang-go` on Ubuntu 24.04 is Go 1.22; if a
  tool the build fetches needs a newer Go, install a newer toolchain. `runner build-local` is written for a
  Windows control machine with a WSL distro named `Ubuntu`; on Linux (or another distro name) run
  `GITRUNNER_ORG=<org> mac/build-local.sh out`, then upload `out/git-runner-mac.pkg` to the `installer` release
  of your repo with `gh release upload`.
- To build the Windows installer (`win/build.sh`): only `bash` (Git Bash is enough); no WSL or packaging tools.

## Quickstart

### 1. Get the code and name your org

Keep your copy **private**: the installers you build carry credentials. You cannot fork a public repository
into a private one, so create an empty private repo in your organisation (say `example-org/runnerpool`), clone
this repository, and push it there. Keep this repository as the `upstream` remote to pull updates later.
Push to `main` (the default branch), because `gh workflow run` finds the Admin workflows there.

PowerShell:

```powershell
gh repo create example-org/runnerpool --private
git clone https://github.com/<upstream-owner>/runnerpool.git
cd runnerpool
git remote rename origin upstream
git remote add origin https://github.com/example-org/runnerpool.git
git push -u origin main
```

bash:

```bash
gh repo create example-org/runnerpool --private
git clone https://github.com/<upstream-owner>/runnerpool.git && cd runnerpool
git remote rename origin upstream
git remote add origin https://github.com/example-org/runnerpool.git
git push -u origin main
# later: git fetch upstream && git merge upstream/main
```

Then set the values in [Configuration reference](#configuration-reference) below. The workflows contain no
org name, and the tools take the org from `GITRUNNER_ORG` (it is baked into the installers at build time), so
you set `GITRUNNER_ORG` rather than edit files. Unset required variables fail with a message that names them.
In `.github/ISSUE_TEMPLATE/config.yml`, point the security-advisory URL at your own repository.
Set the Actions variable that switches the self-hosted workflows on (next table, `RUNNERPOOL_SELF_HOSTED`) once
your runners exist (see [step 5](#5-build-and-install)):

```bash
gh variable set RUNNERPOOL_SELF_HOSTED -b true -R example-org/runnerpool
```

### Configuration reference

| Name | Used by | Meaning |
| --- | --- | --- |
| `GITRUNNER_ORG` (required) | `runner`/`rp`, `win/build.sh`, `mac/build-local.sh`, `scripts/deploy.sh`, `compose.yaml` | Your GitHub org. Baked into the Mac and Windows installers at build time (the Mac pkg writes it to `/usr/local/mac-runners/org`); `GITRUNNER_APP_ORG` overrides it for the App lookup |
| `RUNNERPOOL_SELF_HOSTED` (Actions variable) | `admin*.yml`, `build*.yml`, `deploy.yml` | Set to `true` in your own repo to enable the workflows that need your self-hosted runners and secrets: `admin*.yml` and `build*.yml` (what `runner` dispatches) and `deploy.yml`. Unset (forks, fresh clones) their jobs are skipped. `runner` now stops with a message when a dispatched job was skipped. **`deploy.yml` also runs after every green push to `main` once this is `true`**, and fails (no `DEPLOY_HOST`, `DEPLOY_SSH_KEY` or server) unless you run the dashboard; if you do not want the dashboard, leave the deploy secrets unset and ignore or disable that one workflow in the Actions tab. Set it when your first runners are installed or when you want to use `runner publish`/`test`/`publish-win` or the Admin fallback; `build-local`/`build-win` and the SSH path of `runner` do not need it. This repo's own `ci.yml` runs on GitHub-hosted `ubuntu-latest` and needs no setup |
| `GITRUNNER_REPO` | `runner` | Repo holding the Admin workflows; default `<org>/runnerpool` |
| `GITRUNNER_BUNDLE_ID` | Mac build, `mac/gitrunner` | launchd label prefix and pkg id; default `io.github.git-runner.mac-runners`. Also settable as an Actions variable for `build.yml` |
| `DEPLOY_HOST`, `REMOTE_CONTEXT` | `scripts/*.sh` | ssh target and Docker context of the dashboard server |
| `MACS_ALLOWED_HOSTS` (required), `MAC_1_IP`, `MAC_2_IP`, `MACS_CI_EXTRA`, `MACS_CI_TOKEN_FILES`, `WATCHDOG_TZ` (default `UTC`) | `compose.yaml` | Dashboard host names, the Macs' tailnet IPs, extra repos and their token, digest timezone |
| Secrets `CI_APP_ID`, `CI_APP_PRIVATE_KEY` (or `RUNNER_PAT`), `TS_AUTHKEY`, `DEPLOY_SSH_KEY`, `DEPLOY_HOST`; variables `DEPLOY_HEALTH_URL`, `MACS_ALLOWED_HOSTS`, `GITRUNNER_BUNDLE_ID` | Actions | Installers and the automated deploy; details in [docs/dashboard-deploy.md](docs/dashboard-deploy.md) |

`rp` is a short alias for `runner` (`./rp list`, `.\rp.cmd list`).

### 2. Create the credential

**GitHub App (preferred).** In your organisation: Settings > Developer settings > GitHub Apps > New. Give it
**Organization permissions: Self-hosted runners: Read and write** and **Repository permissions:
Administration: Read and write, Metadata: Read** (add Actions, Checks, Pull requests: read for the dashboard
and watchdog). Install it on the repos that will use the pool (at least this one). Generate a private key.
Save the App id and the `.pem` in your secret manager, then set repo secrets:

```bash
gh secret set CI_APP_ID -R example-org/runnerpool
gh secret set CI_APP_PRIVATE_KEY -R example-org/runnerpool < key.pem
```

Each device stores the key (root-only on Mac and Linux, SYSTEM-only on Windows) and mints one-hour
installation tokens from it, so a leaked token is good for an hour. The key is the only long-lived secret;
rotate it on the App's page.

**Fine-grained PAT (fallback).** Owned by `example-org`, **Self-hosted runners: Read and write** (organisation),
**Administration: Read and write** on this repo only, expiry up to a year. Store it as `RUNNER_PAT`.

### 3. Tailscale (optional but recommended)

Install Tailscale on the control machine and sign in. In the admin console add `"tag:ci-mac":
["autogroup:admin"]` to `tagOwners` and an SSH rule letting your members reach `tag:ci-mac`. Generate a
reusable, pre-approved auth key with that tag, store it in your secret manager and as `TS_AUTHKEY`.

### 4. Tell `runner` where your secrets are

`op-secrets.conf` holds only `op://vault/item/field` references for 1Password (`op read`), for example
`op://Example-Vault/CI GitHub App/app_id`. Edit them for your vault, or use any secret manager that can set the
same environment variables (`CI_APP_ID`, `CI_APP_PRIVATE_KEY`, `RUNNER_PAT`, `TS_AUTHKEY`). If nothing is
found, `build-local` asks for each value at a hidden prompt.

### 5. Build and install

The first installer has to be built on your control machine: `runner publish` and `runner publish-win` build in
Actions and need a self-hosted runner (`mac-ci` for the Mac pkg, `linux-ci` for the Windows installer) that does
not exist until an installer has run somewhere. So the order for an empty org is:

```bash
./runner build-local        # the Mac pkg only (git-runner-mac.pkg), built here with no Actions minutes
./runner build-win          # the Windows installer only (win-runners.ps1), plain bash, no WSL needed
```

Both upload the file to a private `installer` release in your repo (and delete the local copy);
`./runner link` prints the download links. Never put them anywhere public (see SECURITY). Install the first
machine from that link (below), set `RUNNERPOOL_SELF_HOSTED=true`, and from then on you can rebuild with
`./runner publish` (Mac) and `./runner publish-win` (Windows) in Actions, and use the Admin workflows. Use
`build-local`/`build-win` again any time no matching runner is online. Per OS:

- **Mac:** open the `.pkg`, allow it under System Settings > Privacy & Security > **Open Anyway**, Install, type
  the Mac's password, then delete the file. The Mac names itself `air-1`, `air-2`, ... A password is needed again
  after a reboot (FileVault).
- **Windows:** download `win-runners.ps1` (from `./runner link`) and run it in an administrator PowerShell on the PC. It names itself
  `win-1`, ... and can add WSL Linux runners. See [docs/windows.md](docs/windows.md); **untested on real hardware**.
- **Linux / WSL:** (the PC's WSL runners come with the Windows installer; this is for a box on its own) copy `linux/linuxrunner` to the box and run `bash linuxrunner bootstrap wsl-1`, then register the
  admin runner. See [docs/linux.md](docs/linux.md).

### 6. Check the fleet

```bash
./runner list
./runner doctor air-1       # 0 problems is healthy
```

### 7. Dashboard and watchdog (optional)

- `./runner dashboard` serves a local control panel on `127.0.0.1` only.
- For an always-on one, run the `dashboard/` and `watchdog/` containers with `compose.yaml` behind a private
  reverse proxy: [docs/dashboard-deploy.md](docs/dashboard-deploy.md), [docs/watchdog.md](docs/watchdog.md).

Before `docker compose up` the nine secret files must exist (two are required, see
[docs/dashboard-deploy.md](docs/dashboard-deploy.md)), and `compose.yaml` publishes port 8765 on every
interface, so keep the host behind your LAN/tailnet.

### 8. Use the runners from a workflow

```yaml
jobs:
  test:
    runs-on: [self-hosted, macOS, ARM64, mac-ci]
```

## Self-hosted runner labels

All of these are registered at the organisation level (so use a runner group, see SECURITY). Every job runs on
one of them; GitHub-hosted runners are never used, so a job whose runners are off waits in the queue. Pick the
label set that matches your own fleet; these are the defaults the installers create.

| Label | Runners | Used by |
| --- | --- | --- |
| `mac-ci` | `air-1`, `air-2`, ... (each Mac's `_cirunner`) | Any repo, opt in per job: `runs-on: [self-hosted, macOS, ARM64, mac-ci]`. For jobs that need only macOS with Node/Python/shell (no Postgres, Docker or `linux`). Queues if every Mac is off or on battery. |
| `mac-ci-heavy` (extra on `mac-ci`) | the same Macs | An optional second label the installers add (`CI_LABELS=mac-ci-heavy,mac-ci`), for a repo's heavier jobs that should target Macs specifically, e.g. as `mac-labels` for [ci-plan](docs/ci-plan.md). |
| any extra label (`CI_HOST_LABELS`) | the Macs you name | Optional per-host labels on a Mac's `_cirunner`, from the `CI_HOST_LABELS` repo variable, format `HOST=label[,label];HOST=...` (e.g. `host-a=gpu`). Read by `admin.yml` (`reregister-ci`), `build.yml` and `mac/build-local.sh` (written to the package's `bootstrap.env`); apply to an installed Mac with `runner reregister host-a`. Add the label to `.github/actionlint.yaml` so workflows can use it. |
| `mac-admin`, `air-N` | `air-1-admin`, ... (root) | This repo's Admin workflow only |
| `linux-admin`, `wsl-N`, `air-N-vm` | `wsl-1-admin` (root, in a PC's WSL distro), `air-1-vm-admin` (root, in a Mac's Linux VM) | This repo's Admin (Linux) workflow only |
| `linux-ci` | `win-1-wsl-1`, `-2`, ... (WSL, x86-64) and `air-1-vm-1`, ... (one per Mac, in its Linux VM, **ARM64**) | Any repo: `runs-on: [self-hosted, linux, linux-ci]`. Plain Ubuntu with git, Python, uv and Postgres 16 binaries (no Node; no Docker unless the `docker` label). Off while the host is on battery. The pool is mixed: add `X64` if the job needs x86-64. |
| `docker` (extra on `linux-ci`) | WSL runners only (not the Mac VMs) | Jobs that build or run containers: `runs-on: [self-hosted, linux, linux-ci, X64, docker]`. x86-64 only. |
| `win-ci` | `win-1`, `win-2`, ... (each PC's `_cirunner`) | Windows-native jobs: `runs-on: [self-hosted, Windows, X64, win-ci]` (PowerShell, no bash). With job slots on, `win-ci` and `linux-ci` on one PC share a cap. |
| `win-admin`, `win-N` | `win-1-admin`, ... (SYSTEM) | This repo's Admin (Windows) workflow only |

## Architecture (short)

```
 control machine                     your devices
 ┌──────────────┐  Tailscale SSH   ┌────────────────────────────┐
 │ ./runner CLI │ ───────────────▶ │ Mac / Windows PC / Linux   │
 │ dashboard    │  or Admin        │  - _cirunner  (CI jobs)    │
 └──────┬───────┘  workflow        │  - admin runner (root)     │
        │          (fallback)      │  - gitrunner / winrunner   │
        ▼                          │  - GitHub App key, power   │
   GitHub API  ◀───────────────────│    watch, re-register loop │
   (org runners, runs)  one-hour   └────────────────────────────┘
        ▲               tokens
        │
   watchdog + dashboard containers (optional)
```

Each device runs an unprivileged CI runner and a root admin runner. `runner` reaches a device over Tailscale
SSH and falls back to dispatching the Admin workflow onto that device's admin runner. Devices mint short-lived
App tokens and keep their own runners registered. The shared `ci-plan.yml` and `steal` action share a test
suite between machines. Details: [docs/architecture.md](docs/architecture.md), [docs/ci-plan.md](docs/ci-plan.md).

## More documentation

- [docs/architecture.md](docs/architecture.md): parts, per-device layout, how work stealing works, lessons learned
- [docs/ci-plan.md](docs/ci-plan.md): splitting another repo's CI across the pool
- [docs/windows.md](docs/windows.md), [docs/linux.md](docs/linux.md), [docs/mac-linux-vm.md](docs/mac-linux-vm.md)
- [docs/dashboard-deploy.md](docs/dashboard-deploy.md), [docs/watchdog.md](docs/watchdog.md)
- [CONTRIBUTING.md](CONTRIBUTING.md), [SECURITY.md](SECURITY.md), [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)

## Day to day

`./runner help` lists everything: `list`, `link`, `status`, `doctor`, `logs`, `restart`, `reregister`,
`remove`, `add`/`drop` (a runner for another repo), `update` (push a new on-device tool), `battery pause|run`,
`cores N|all`, `slots HOST N|off`, `limit HOST RUNNER cores=N|default ram=MB|default`, `ramdisk HOST on [GB]|off` (a Mac, or the PC's WSL runners),
`postgres`, `tailscale`, `rotate-token`, `set-app`, `ssh`, `ci`, `link`, `publish`, `publish-win`, `test`, `build-win`.

Rotating keys: for the App key, generate a new one, update your secret manager and `CI_APP_PRIVATE_KEY`, run
`./runner set-app`, then delete the old key. For a PAT, create a new one, update `RUNNER_PAT`, run
`./runner rotate-token`, revoke the old. For the Tailscale key, update `TS_AUTHKEY`, run
`./runner tailscale air-1` and rebuild the installers.

If `runner` says a device didn't pick up a job, it is off, asleep, offline, or (a Mac after reboot) waiting
for its password.

## Licence

MIT, see [LICENSE](LICENSE). Third-party components and their licences: [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

## Trademarks

runnerpool is the name of this project. GitHub, GitHub Actions, macOS, Windows, Linux, Tailscale and other product names are trademarks of their respective owners; this project is not affiliated with or endorsed by them.
