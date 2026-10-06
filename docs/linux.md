# Linux boxes (WSL runners)

If you already have CI runners installed by hand in a WSL distro (here named `gh-runner`),
`linux/linuxrunner` brings them under the same `runner` commands as the Macs **without re-registering or
restarting them**: it finds the systemd units GitHub's `svc.sh` made (`actions.runner.*.service`) and
manages those. Labels, names and org registration are untouched, so existing workflows keep matching.

## Adopting (once, no CI downtime)

1. In the distro, as root, name the box and install the tool (copy `linux/linuxrunner` in):
   `bash linuxrunner bootstrap wsl-1`. This only writes `/opt/git-runner/` and a symlink.
2. Register the root admin runner that `runner` reaches the box through. From the control PC mint a
   token and run in the distro: `linuxrunner install-admin example-org/runnerpool <token> `
   (the token is a repo registration token from `gh api -X POST repos/example-org/runnerpool/actions/runners/registration-token`).
3. From the control PC: `runner doctor wsl-1`.

## Commands

`runner status|doctor|info|logs|restart|ci|cores|update wsl-1`, plus `runner add` / `runner drop` for
extra runners. `ci off` lets running jobs finish, then stops and disables the CI units (they stay
off across reboots); `ci on` re-enables them. `cores N` sets `CI_MAX_CORES=N` in each runner's `.env`
and restarts idle runners. `limit NAME cores=<N|default> ram=<MB|default>` sets one runner's own limits (they win over `cores`): `CI_MAX_CORES` / `CI_MAX_RAM_MB` in its `.env` plus a systemd `MemoryMax` drop-in (`<unit>.d/limits.conf`) that caps the whole service; a busy runner picks it up when it next restarts. The admin runner is never touched by these.

`slots N [THREADS] | off` is the PC's job cap (see "Job slots" in [windows.md](windows.md)): `slot-N` lock folders (default
`/opt/git-runner/slots.d`, on a PC the Windows folder `/mnt/c/ProgramData/win-runners-public/slots`), hooks in each runner's
`.env` (`ACTIONS_RUNNER_HOOK_JOB_STARTED` / `_COMPLETED`, `GIT_RUNNER_NAME`), `CI_MAX_CORES` = THREADS, and a `CPUQuota` in the
same drop-in as `MemoryMax`. A job waits for a free slot and is pinned (`taskset`) to the slot's CPU block. `slot-sync` clears slots
whose job is gone and stops idle runners while all are held (resuming them after); `follow STATEFILE` is the service that runs it every
2 seconds and applies the Windows side's `ci`, `cores` and `slots`; `install-follower` (re)creates it, and `self-update` does that for
an older one. On a PC set slots from Windows (`runner slots win-1 3`); `runner slots wsl-1 3` is for a Linux box on its own.

`install-docker` installs Docker Engine from Docker's apt repo (x86-64 only, so the PC's WSL and never a Mac VM), enables `docker.service`
and adds the CI runners' users to the `docker` group; see "Docker" in [windows.md](windows.md).

## RAM workspace (`ramdisk`)

`linuxrunner ramdisk on [MB]` (`runner ramdisk wsl-1 on [GB]` from the control PC) mounts a tmpfs on every CI runner's `_work`
folder, so checkouts and test temp files live in RAM and never fill the disk. `ramdisk off` puts it back. Details:

- The size is a **cap**, not a reservation: RAM is used only for what jobs write, and a full workspace fails the job (ENOSPC)
  instead of letting it eat the distro's memory. The default is half of one runner's share of RAM (RAM / slots, or / the number of
  CI runners). The distro's RAM ceiling is whatever WSL gives it (`.wslconfig`, which this tool never touches), so size to `free -m`
  inside the distro. With slots on, tmpfs pages count against the runner service's `MemoryMax`.
- **Emptied after every job.** `ramdisk on` wires the runners' job-completed hook, which deletes the checkout and the job's temp
  files. The `_tool` and `_actions` caches stay (the next job reuses them) until the distro stops, which empties everything.
- Persistent: an `/etc/fstab` line per runner (WSL2 mounts it at boot) and a `RequiresMountsFor` drop-in so the runner starts after the
  mount. `add-runner` mounts new runners too. It never interrupts a job: idle runners are stopped at once, busy ones are waited for (up to 30 minutes, then nothing is changed), and slot-sync leaves them alone meanwhile. It deletes what is on the disk under `_work` first.
- Not covered: Docker images and build caches (they live in Docker's own storage, not `_work`) and native Windows runners (`win-ci`).

Not covered: `reregister`/`remove` (the adopted runners keep their registration), `rotate-token`
(no token or App key is stored), `battery`, `postgres`, `tailscale`, `ssh`. The dashboard still only lists the PC's
runners; per-host controls for `wsl-N` are a follow-up.

WSL rules: never `wsl --shutdown` (it stops every runner) and never change `.wslconfig`.

The Macs run the same tool in a small ARM64 Colima VM (hosts `air-N-vm`); see [mac-linux-vm.md](mac-linux-vm.md). Their power rules are the Mac's, not this box's.
