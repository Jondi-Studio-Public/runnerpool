# Architecture

How the parts fit, and what was learned running them. Setup steps are in the [README](../README.md).

## Parts

1. **Per-OS installer and on-machine tool.** `mac/gitrunner` (one bash script that does everything on a
   Mac), `mac/postinstall` and `mac/build-local.sh` (builds `git-runner-mac.pkg`, in WSL or Linux);
   `win/winrunner.ps1` and `win/build.sh` (one-file Windows installer); `linux/linuxrunner` (Linux and WSL
   boxes, and the Linux VM inside each Mac). Paths on a Mac use the `mac-runners` name
   (`/usr/local/mac-runners`, `/var/log/mac-runners`, launchd labels `io.github.git-runner.mac-runners.*`).
2. **`runner`** (`runner` for bash, `runner.cmd` for PowerShell and cmd): the control-machine command. It reaches
   a Mac over Tailscale SSH (for actions that need no secret; `RUNNER_VIA=auto|ssh|github`, stderr names the
   path that ran), or falls back to the **Admin workflow** (`admin*.yml`) running on that
   device's root admin runner. `./runner help` lists everything.
3. **Shared CI** (`.github/workflows/ci-plan.yml`, `.github/actions/steal/`, `ci/ci_shard.py`,
   [ci-plan.md](ci-plan.md)): splits a repo's test suite between its always-on machines and whichever
   others are online and plugged in.
4. **Dashboard** (`dashboard/`, [dashboard-deploy.md](dashboard-deploy.md)) and **watchdog**
   (`watchdog/`, [watchdog.md](watchdog.md)): a control panel and an alerting service, run as two containers
   from one image.

## Per-device layout

Each device runs a hidden, unprivileged `_cirunner` user for CI jobs (no login, no admin rights) and a
separate root or SYSTEM **admin runner** that `runner` uses as its remote hand. Plugged in: CI on, no sleep with
the lid closed. On battery: CI pauses once the running job finishes, so a laptop only helps while plugged in
and the shared plan counts only online machines.

A device holds a GitHub App id and private key (or, as a fallback, a PAT), and mints one-hour installation
tokens from it to register runners and to re-register any runner GitHub deleted (GitHub removes runners that
have been offline for 14 days). Credentials are root-only (Mac, Linux) or SYSTEM/Administrators-only (Windows).

## Shared CI: how it works

- **`ci-plan.yml`** (reusable workflow, runs no tests): lists the repo's and the org's runners with a
  status token (org Self-hosted runners: read), counts the online fixed machines and Macs, and returns
  `target`, `runner` and a matrix of machines. It uses Python 3 only, because the Linux runners have no `jq`.
  Its own job needs a self-hosted runner; there is no hosted fallback.
- **`steal` action** (work stealing): the machines in a run take chunks of the suite from a shared queue
  until none is left, so a fast machine does more. The queue is git refs under `refs/claims/<run>-<attempt>/`:
  creating a ref is atomic (201 for one machine, 422 for the rest). Each chunk records `done-K` or
  `failed-K`; a `verify` job fails the run if any chunk has no result and deletes the refs. It needs
  `contents: write` on the job.
- **`ci/ci_shard.py`**: the pytest plugin behind `--shard K/N` (round-robin over collected tests).

Measured on one project with about 103 000 tests: fixed weights 6/2/2 gave 11:26 on the PC and 4:55 and 3:57
on the Macs; weights 4/3/3 gave 12:10, 6:14 and 12:21, because a fixed split cannot cope with a slow machine.
That is why work stealing replaced fixed splits. Each chunk pays pytest's collection and worker start-up
again, so more chunks balance better but cost more (16 is the default; tune it from real logs).

## Lessons (things that bit us)

- **launchd opens a job's log as the job's user.** The CI runner's job exited 78 (EX_CONFIG) and never logged
  why until its log file existed and was owned by `_cirunner`. `write_plist` now creates it. "Loaded" is not
  "running": `runner status` and `doctor` say `failing (exit N)`.
- **Never start a CI runner by hand on a Mac.** A hand-started `runsvc.sh` outlived its terminal and became a
  second listener for the same runner; two jobs collided in one `_work` folder. Check `ps` for two
  `Runner.Listener` processes if a Mac behaves oddly (there is no doctor check for this yet).
- **uv's Python on macOS has no CA bundle**, so `urllib` fails TLS there. `steal.py` falls back to
  `/etc/ssl/cert.pem`.
- **A PR's CI runs the workflow from the base branch**, so a change to a shared workflow in an unmerged PR does
  nothing for other branches. `workflow_dispatch` with `--ref <branch>` runs a branch's own version.
- **Wall-clock tests are load-sensitive** on a shared machine. Never loosen the budget; run such gates when
  the machine is quiet.
- Clocks differ per machine; logs on a device are in local time, GitHub's are UTC.
- GitHub's OS label is `Linux` (capital); match labels case-insensitively.
- On a Mac, run `tailscaled install-system-daemon` from the installed binary directory, never from the target path.
- Git Bash: set `MSYS_NO_PATHCONV=1` before `wsl.exe`; no leading `/` on `gh api` paths. WSL `/tmp` is wiped,
  use `$HOME`. PowerShell mangles nested quotes in `runner ssh`; use Git Bash.
- Linux runners have no `jq`; use `python3` in workflows. A runner-management PAT cannot read workflow runs (403).
- Packaging the Mac pkg on Linux: use `dumpbom`, not `lsbom`. `xar` needs patches for OpenSSL 3.

## Known open items

- A `doctor` check for two `Runner.Listener` processes on one runner.
- CI-off gap after a reboot (FileVault password wait); the boot daemon can fail silently.
- Keys expire: the Tailscale auth key, and the GitHub token on any device still using a PAT (an App key does not).
- The Windows side (`win/`, [windows.md](windows.md)) was written without a real Windows install to test on.
