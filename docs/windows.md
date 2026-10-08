# Windows runners

Adds Windows PCs to the org's runner pool, controlled from the same `runner` CLI as the Macs.
It mirrors the Mac side: `win/winrunner.ps1` is `mac/gitrunner`, `win/build.sh` makes the
one-file installer, and `.github/workflows/admin-win.yml` is `admin.yml`.

## What the installer puts on a PC

| Piece | What it does |
| --- | --- |
| CI runner (`win-N`, label `win-ci`) | Registered on the org, so every example-org repo can use it. A Windows service that runs as a hidden, non-admin local user `_cirunner` with a random password the service alone holds. |
| Admin runner (`win-N-admin`, labels `win-admin`, `win-N`) | Registered on this repo; a service running as SYSTEM. It is how `runner` reaches the PC. |
| GitHub App key (or token) | `C:\ProgramData\win-runners\github-app-id` and `github-app.pem`, readable only by SYSTEM and Administrators; the PC mints one-hour installation tokens from them (cached beside them: `github-app-installation`, `github-app-token`, `github-app-token.expires`). Without an App key it uses the older `github-token` (a PAT) exactly as before. See "The GitHub App" below. |
| Power watch | A scheduled task at startup. Plugged in (or a desktop): CI on, no sleep, lid close does nothing. On battery: CI pauses once the running job finishes. Every 10 minutes it re-registers any runner GitHub dropped after 14 days offline. |
| `winrunner` | `C:\ProgramData\win-runners\winrunner.ps1`, in an administrator PowerShell: `winrunner help` (`& "$env:ProgramData\win-runners\winrunner.ps1" help`). |

Each PC names itself: the first becomes `win-1`, the next `win-2`. A reinstall keeps the name.

## Installing

**First install (empty org).** `publish-win` builds in Actions on a self-hosted `linux-ci` runner
(`build-win.yml`), and the first `linux-ci` runner is created by this very installer (its WSL step), so on an
empty org `publish-win` would queue forever. Build the first installer on the control machine instead (plain
bash; Git Bash is enough, no WSL):

```powershell
.\runner.cmd build-win       # App key (or token) from your secret manager (op-secrets.conf) or a hidden prompt; uploads to the private installer release
```

Install it on the first PC (below). Also set the repo variable `RUNNERPOOL_SELF_HOSTED=true` (README,
Configuration reference), or the Actions-based commands are skipped. After that, if you answered Y to the installer's WSL prompt (and WSL is installed), a `linux-ci` runner exists, and you
can rebuild in seconds in Actions whenever `win/` or the `CI_APP_ID` / `CI_APP_PRIVATE_KEY` (or `RUNNER_PAT`)
secrets change:

```powershell
.\runner.cmd publish-win     # on a self-hosted Linux runner, seconds; needs one online
```

On each PC, signed in to GitHub, download the file from
`https://github.com/example-org/runnerpool/releases/download/installer/win-runners.ps1`, then in PowerShell:

```powershell
Unblock-File .\win-runners.ps1
powershell -ExecutionPolicy Bypass -File .\win-runners.ps1
```

It asks for administrator rights, registers both runners and starts the power watch. Delete the
file afterwards: it holds the GitHub App key, or the token on an older build (the same trade-off as the Mac `.pkg`).

## The GitHub App

The PC holds the CI GitHub App's id and private key instead of a long-lived token ([dashboard-deploy.md](dashboard-deploy.md), "The CI GitHub App", lists its permissions; the README, step 2, says how to create it). Windows PowerShell 5.1 runs on .NET Framework, which has no
`ImportFromPem`, and the installer does not guarantee Git for Windows' `openssl.exe`. So `winrunner.ps1` parses the
PEM itself (`ConvertFrom-RsaPem`, about 40 lines: a PKCS#1 `BEGIN RSA PRIVATE KEY` as GitHub issues it, or a PKCS#8 key)
into `RSAParameters`, and `RSA.SignData` (SHA-256, PKCS#1 v1.5) signs the JWT. No new dependency. The tests check the
signature against the public key, and that it is byte-for-byte what `openssl dgst -sign` produces.

- Switch a PC: `.\runner.cmd set-app` (all devices), or `.\runner.cmd update win-1` then the Admin (Windows) workflow with action
  `set-app` on `win-1`. On the PC itself, as administrator, with `NEW_APP_ID` and `NEW_APP_KEY_B64` (the PEM, base64 on one
  line) in the environment: `winrunner.ps1 set-app example-org`. It checks a token can be minted and list the runners, deletes
  `github-token`, and puts the old credential back if anything fails.
- `winrunner status` shows `token:     GitHub App 12345, token valid until ...` (or the PAT line), `doctor` checks minting.
- `winrunner set-token` (or `runner rotate-token`) goes back to a PAT and removes the App key.

Then, from the control PC:

```powershell
.\runner.cmd list
.\runner.cmd doctor win-1
.\runner.cmd ci win-1 off      # stop taking jobs
.\runner.cmd battery win-1 run
.\runner.cmd cores win-1 8     # CI jobs may use 8 cores (CI_MAX_CORES, lower priority); `all` removes the limit
.\runner.cmd update win-1      # push a new winrunner.ps1
```

`postgres`, `tailscale` and `ssh` are Mac-only (see below). `ramdisk` works for the WSL runners (see [linux.md](linux.md)), not the native Windows ones.

## Linux runners on the same PC (WSL)

The installer asks "Also set up Linux (WSL) runners? [Y/n]". Yes adds two general Linux runners
for every repo in the org (label `linux-ci`; opt in with `runs-on: [self-hosted, linux, linux-ci]`), named
`win-1-wsl-1` and `-2` after the PC, plus a `wsl-N` admin runner, in a WSL distro called `gh-runner` that
systemd runs. Change the count with the
`WSL_SET` build setting (`wsl:4:linux-ci,docker`; the default is `wsl:2:linux-ci,docker`). They are managed as host `wsl-N` through the Admin (Linux) workflow, with
`linux/linuxrunner` (see [linux.md](linux.md)). A runner name already registered on GitHub (another PC's) is
skipped, never taken over.

A brand-new distro is provisioned first (`linux/linux-provision.sh`: git, python, a compiler and uv) and its
runners stay **off until that succeeds**, so no job lands on a box that cannot run it. A PC that already has a
`gh-runner` distro adopts it and leaves its runners alone. One without WSL gets WSL installed, which needs a
Windows restart, after which you run the installer again. Never `wsl --shutdown`, and `.wslconfig` is never touched.

Jobs that need Node are not covered by what the provisioning installs.

### Docker (label `docker`)

The WSL runners carry a second label, `docker`, so a job that builds x86-64 images can say
`runs-on: [self-hosted, linux, linux-ci, X64, docker]`. When `WSL_SET` has the `docker` label the installer runs
`linuxrunner install-docker` in the distro (after provisioning, so a failure keeps a new distro's runners off):
Docker Engine (`docker-ce`, `docker-ce-cli`, `containerd.io`, `docker-buildx-plugin`, `docker-compose-plugin`) from Docker's
official apt repo, the runners' users added to the `docker` group, and `docker.service` enabled. The distro runs systemd
(the installer turns it on in `/etc/wsl.conf`), so systemd starts dockerd with the distro, the same way it starts the runners;
nothing else needs launching. The Mac VMs get no Docker (they stay `linux-ci` only), and the command refuses on ARM64.
`linuxrunner add-runner` with `docker` among the labels installs it too if it is missing.

**Docker obeys the job slots.** dockerd, containerd and the containers run outside the runner services' cgroups, so the
runners' `CPUQuota`/`MemoryMax` drop-ins would not cover a job's `docker build`. `install-docker` therefore creates
`/etc/systemd/system/docker-ci.slice` with one slot's caps (`CPUQuota` = THREADS x 100%, `MemoryMax` = the distro's RAM / N slots;
no caps while slots are off and cores is `all`), puts `docker.service` and `containerd.service` in it (`Slice=` drop-ins), and sets
`"cgroup-parent": "docker-ci.slice"` in `/etc/docker/daemon.json` (merged into any existing JSON) so containers land there too.
`runner slots` (and, while slots are off, `runner cores`: `CPUQuota` = N x 100%, no RAM cap) rewrite the slice and reload systemd, so the caps follow without restarting Docker. The slice is one slot's
worth shared by everything Docker runs: two jobs building at once split it, they do not get one each. Docker is restarted once
by `install-docker` (running containers stop). Nothing here touches `.wslconfig` or runs `wsl --shutdown`.

Known gaps: whether BuildKit's build steps honour `cgroup-parent` is not something we could confirm (the daemon option is
documented for containers; BuildKit has its own worker `cgroupParent` setting, and `docker build --cgroup-parent` only applies to
the classic builder). The `Slice=` drop-ins are the fallback: BuildKit runs inside dockerd/containerd, so its steps are
normally in the slice either way, but this is untested on a real PC. Check once with `systemd-cgls /docker-ci.slice` during a
build. The cap also does not pin CPUs the way the slot hook's `taskset` does a runner, only limits total CPU time.

Docker Desktop's WSL integration is per distro and off by default for a distro other than the default one. If it is ever
switched on for `gh-runner` it puts its own `docker` into the distro and fights this engine: leave it off (Docker Desktop >
Settings > Resources > WSL integration). `install-docker` stops with that message when it finds Desktop's `docker` there.

Turning it on for an existing PC (new runners get the label at registration; the existing ones do not): `runner update win-1`,
`runner update wsl-1`, then install Docker in the distro and add the label to each runner, from PowerShell on the PC:

```powershell
wsl.exe -d gh-runner -u root -- /opt/git-runner/linuxrunner install-docker
gh api -X GET orgs/example-org/actions/runners --jq '.runners[] | select(.name | test("^win-1-wsl-")) | .id' |
  ForEach-Object { gh api -X POST "orgs/example-org/actions/runners/$_/labels" -f 'labels[]=docker' }
```

(Idle runners restart on their own to pick up the `docker` group; a runner in a job gets it at its next restart, so run
`runner restart wsl-1` once the jobs are done.)

A boot task keeps the distro running without anyone logged in. The Linux runners follow the Windows side:
on battery, or after `runner ci win-1 off`, a small service in the distro (`linuxrunner follow`, checking every 2 seconds) turns
Linux CI off too, and `runner cores win-1 N` and the job slots below carry over. `runner ci wsl-1 ...` still works on its own.

## Job slots: the PC's CPU cap

(Every device has a slot count you can change live, `runner slots HOST N`: PCs here, Macs in [mac-linux-vm.md](mac-linux-vm.md). `runner status HOST` shows "slots: k of N in use" for all of them.)

Both kinds of runner on one PC (Linux `linux-ci` in WSL, Windows `win-ci`) can share a hard cap, so CI
never takes more of the machine than you decide and the rest stays free for Windows and for you:

```powershell
.\runner.cmd slots win-1 3          # at most 3 CI jobs at once, each on 8 threads (3 x 8 = 24 threads)
.\runner.cmd slots win-1 3 6        # ... or 6 threads per job
.\runner.cmd slots win-1 off        # no cap (the default until you turn it on)
```

How it works (`winrunner slots`, `linuxrunner slots`, the hooks):

- **N slot folders**, `slot-1` .. `slot-N`, in `C:\ProgramData\win-runners-public\slots`, which the distro sees as
  `/mnt/c/ProgramData/win-runners-public/slots`. Each owner's name, side (`win` or `linux`), pid, run id and time
  are in an `owner` file inside.
- **Each runner's job-started hook takes a slot** with `mkdir`, which is atomic across Windows and WSL, so two
  runners can never hold the same one. If all N are taken the hook **waits** (polling every 3 seconds, logging
  "waiting for a PC job slot") until one frees: an extra job is delayed, never run alongside the others. The
  job-completed hook gives the slot back. **A waiting job shows as running on GitHub while it waits**, and its job
  timeout keeps counting. The hooks never fail a job: if one cannot run it lets the job through.
- **The slot picks the CPUs.** Slot k gets CPUs `(k-1)*T` .. `k*T-1` (wrapping on a smaller machine): the hook pins the
  runner's worker to that block (affinity, `taskset` in WSL, and below-normal priority on Windows), and the job's
  steps inherit it. `CI_MAX_CORES` is T in every runner's `.env`, so workflows can pass it to `pytest -n` and `make -j`.
  Linux services also get `CPUQuota=T*100%`. Windows and WSL see the same logical CPUs, so a Windows job and a Linux
  job in different slots never share CPUs.
- **The controller keeps idle runners from taking more work.** The power watch (Windows) and the follower service
  (the distro, `linuxrunner follow`) look every 2 seconds. While all N slots are held they stop every idle CI runner,
  on both sides; when a slot frees they start them again. A running job is never cut short. They also clear the slots
  of jobs that are gone (a cancelled job or a crash: the owner runner has no `Runner.Worker` any more; each side
  clears its own).
- **`ci off` and the battery rule still win.** They stop runners regardless; the controller never starts a runner
  that those rules have stopped.
- **Runners.** `slots N` adds the Windows runners `win-1-ci-2` .. `win-1-ci-N` next to `win-1` (copying its repo and
  labels). Linux runners need N as well: with `win-1-wsl-1` and `-2` there, add the third with
  `.\runner.cmd add wsl-1 example-org linux-ci win-1-wsl-3`. Having more runners than slots is fine, that is what lets
  any mix (3 Linux, 3 Windows, 2 and 1) fill the slots. Every non-admin runner in the distro counts, the old per-repo
  ones included.
- **Turning it on for an existing PC**: `runner update win-1` (new winrunner), `runner update wsl-1` (new linuxrunner;
  it also moves the distro's follower service onto the new code), add the missing runners (above), then
  `runner slots win-1 3`. The existing `win-1-wsl-1` / `-2` are adopted as they are, not re-registered: the new
  `.env` (hooks, `CI_MAX_CORES`) is written and idle runners restart once to pick it up; a runner in a job is caught up when it is idle.
  Check with `runner status win-1` (slots line) and `runner status wsl-1`.
- **The folder is open to every local user** (that is how a job running as `_cirunner`, or as the distro's user, can
  take a slot), so a malicious job could hold slots and starve CI; the repos are private, the same trust as running their jobs at all.
- **Changing N while jobs run is safe.** `runner slots win-1 N` applies within seconds and never stops a job.
  The hooks (Windows and Linux) re-read N on every poll, and a job takes a slot only when the number of held slots,
  counted over **all** `slot-*` folders and not only slot-1..N, is below N. So a raise lets a waiting job in at
  once, and a lowering keeps new jobs out until enough running ones have finished: with N lowered from 3 to 2 and
  `slot-1` and `slot-3` held, the free `slot-2` is not taken. The idle-runner pause and the hold helper count the
  same way. If two jobs race for the last slot, the one that sees too many folders after its `mkdir` removes its
  own and retries, so a job never starts beyond the cap.
- **Shrinking never unregisters a runner.** The extra Windows runners (`win-1-ci-2` ..) and WSL runners stay
  registered and simply wait for a free slot (idle ones are paused while every slot is held); nothing is removed
  mid-job or otherwise. Remove spares you no longer want yourself with `runner drop` once they are idle.
- **Holding a slot for something outside the runners** (a VM, a devcontainer): `winrunner.ps1 slots-hold` takes
  the highest free slot as a lease, and `slots-release` gives it back. Over the admin workflow's existing `slots`
  input (no new input): `.\runner.cmd slots win-1 hold [NAME [TIMEOUT]]` and `.\runner.cmd slots win-1 release [NAME]`.
  In an administrator PowerShell on the PC:

  ```powershell
  & "$env:ProgramData\win-runners\winrunner.ps1" slots-hold -Name claude-vm -LeaseMin 10 -TimeoutSec 600
  & "$env:ProgramData\win-runners\winrunner.ps1" slots-release -Name claude-vm
  ```

  `-Name` defaults to `claude-vm`; `-LeaseMin` (1 to 1440, default 10) is how long the slot is good for;
  `-TimeoutSec` (default 0 = wait forever) is how long to wait for a free slot. The owner file reads `side=hold`,
  `runner=<name>`, `lease=<minutes>`, `time=<now>`. **Run `slots-hold` again for the same name more often than its
  lease** (a scheduled task, or a loop in the VM's start script, every few minutes): holding again only refreshes `time`
  and keeps the same slot. The power loop clears a `side=hold` slot once now minus `time` is more than the lease, so a
  holder that dies frees its slot; the Linux side never clears it. Only the two numbers are read from the owner
  file, as digits, and nothing in it is ever run (the folder is writable by every local user). With slots off
  both commands succeed and do nothing. Exit codes: **0** held or released (or slots off), **3** timed out waiting for a
  free slot, **2** bad usage. Over `runner`, a wait that outlasts the workflow's 20 minute limit ends the job.
- **Limits.** If the power watch (or the distro's follower) is not running, nobody clears stale slots and idle
  runners are not paused: jobs still wait for a slot, but a slot leaked by a crash stays held until a controller runs
  again. Per-runner limits (`runner limit ... cores=`) still win over the slot's thread count for `CI_MAX_CORES`; the
  CPU block is the slot's. Not tested on a real PC yet: run `runner doctor win-1` and `runner status wsl-1` after turning it on.

## Things worth knowing

- **Core limit.** `runner cores HOST N` sets how many cores CI jobs may use. Jobs see `CI_MAX_CORES=N`
  (a workflow can pass it to `pytest -n`, `make -j`, and so on), and run at lower priority; on Windows
  they are also pinned to the first N cores (with job slots on, to the slot's block of CPUs instead, see below). The next job picks the
  limit up (a running job keeps its old `CI_MAX_CORES`). It is a cap you set, not something the machine works out from its specs.
  `runner limit HOST RUNNER cores=N|default ram=MB|default` sets one runner's own limits, which win over the device-wide one: cores are enforced as above for that runner, RAM only sets `CI_MAX_RAM_MB` (advisory: a Job Object memory cap would kill jobs mid-run, so none is set).

- **Windows jobs only.** A native Windows runner runs jobs written for Windows (`runs-on:
  [self-hosted, Windows, win-ci]`). Bash-based suites and the `steal` action are Linux/macOS
  tooling, so they will not run on the native Windows runner. That is what the Linux runners in WSL
  (above) are for.
- **Labels.** `win-ci` and `win-admin` never claim `linux`. If an org-wide label scheme is agreed
  (the "Macs on all org repos" work), change `CI_LABELS` in `win/build.sh` and in `admin-win.yml`,
  then run `runner publish-win`.
- **No Tailscale or SSH fallback.** Tailscale SSH has no Windows server, so the admin runner is the
  only control path. If a PC is off, asleep or offline, `runner` cancels its queued job after two minutes.
- **The dashboard** shows each PC's specs and health like a Mac's, from what the PC pushes every 30 s
  (see "PC health push" below). Controls go through the Admin workflow.
- **Untested on real Windows so far.** CI parses the PowerShell and tests the installer build; the
  first real install is the test. Run `runner doctor win-1` after it.

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
