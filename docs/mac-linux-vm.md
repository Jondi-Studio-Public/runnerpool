# Linux VM on each Mac

The PC's WSL distro (`gh-runner`) is the only host of the org-wide `linux-ci` pool, so Linux CI waits
whenever the PC is off. Each Apple Silicon Mac can add a small **ARM64** Ubuntu VM to that pool. The
Macs' own native macOS runners (`air-N`, `mac-ci`) are untouched.

## What it is

- **Colima** (a Lima VM on Apple's Virtualization.framework, `--vm-type vz`, no container runtime) with
  profile `gitrunner`: Ubuntu, aarch64, 4 CPUs (2 on a Mac with fewer than 8 cores), 4 GB RAM, 40 GB
  disk by default. Memory is capped at the Mac's RAM minus 4 GB.
- **Colima and Lima are pinned release binaries** in `/usr/local/mac-runners/vm/`, not Homebrew. Each
  download is checked against the checksum file its release publishes; set `COLIMA_SHA256` /
  `LIMA_SHA256` in `mac/gitrunner` (or the `LINUX_VM_*_SHA256` environment variables) to pin the hash
  itself too. Versions are `COLIMA_VERSION` / `LIMA_VERSION` in the same place.
- It runs as the hidden user `_gitrunnervm` (Lima refuses to run as root) from a launchd daemon
  (`io.github.git-runner.mac-runners.linux-vm`), so it comes up at boot with no login. Its disk lives under
  `/var/gitrunner-vm`. The VM mounts only the job-slot folder below, never the Mac's other files.
- The box is provisioned with the same `linux/linux-provision.sh` the WSL distro uses (git, python3, a
  compiler, uv, Postgres 16 binaries with the default cluster off), and managed inside by `linuxrunner`.
- Runners: exactly one CI runner, `air-N-vm-1`, at org level with label `linux-ci`, running as the
  non-root user `runner`; and a root admin runner `air-N-vm-admin` (labels `linux-admin`, `air-N-vm`) on
  this repo, which is how `runner status air-1-vm` and the Admin (Linux) workflow reach the box (it only
  manages; it runs no CI).
  Registration tokens are minted by the Mac with its GitHub credential (a GitHub App token, or the stored PAT on a Mac not yet switched) and sent into the VM on stdin; the VM stores no credential.
- **They are ARM64.** GitHub labels them `self-hosted, Linux, ARM64, linux-ci`. A job that needs x86-64
  must say so (`runs-on: [self-hosted, linux, linux-ci, X64]`), or it may land on the VM. The VMs have no Docker (a deliberate choice); a job that needs it must also carry the `docker` label, which only the PC's WSL runners have.

## Job slots (one at a time by default)

A MacBook Air has 8 cores (4 of them efficiency), so by default each Mac runs **one job at a time**,
whichever runner it comes to: the native macOS runner (`air-N`) or the VM's (`air-N-vm-1`). The number is a
setting, changed any time, with the same command as on the PC:

```powershell
.\runner.cmd slots air-1 2        # two jobs at once: the native runner and the VM each run a job
.\runner.cmd slots air-1 1        # back to taking turns (the default while the VM is on)
.\runner.cmd slots air-1 off      # no gating at all
.\runner.cmd slots air-1 2 4      # optional THREADS: CI_MAX_CORES for the native runner
.\runner.cmd status air-1         # "slots:     1 of 2 in use"
```

Changing it never stops a running job: lowering takes effect as jobs finish (a new job gets in only when the
number of held slots, whatever their numbers, is below N), raising lets a waiting job in at once. The Mac's
admin workflow has no spare input, so `runner slots air-1 ...` sends the value in its `size` input. Two things
enforce the count.

**The job slots (never more than N jobs at once).** Both runners run `linux/slot-hook.sh` as their
`ACTIONS_RUNNER_HOOK_JOB_STARTED` / `ACTIONS_RUNNER_HOOK_JOB_COMPLETED` hooks (the native one set in its
launchd service file, the VM's in its runner `.env`). The slots are the directories
`/private/var/gitrunner-slot/slot-1` .. `slot-N` on the Mac, mounted read-write into the VM (virtiofs, same path),
with the setting in `slots.conf` beside them (`slots=N` or `slots=off`; missing means 1). A job-started hook takes
a slot with `mkdir`, which is atomic on both sides, but only while the held `slot-*` folders number fewer than N,
and writes who holds it (runner name, side, job, pid, time) in an `owner` file inside; the completed hook removes
it. A job that finds every slot taken **waits in its hook** (logging "waiting for the Mac's job slot", polling every
3 s, re-reading N each time) until one is free, so even jobs started in the same instant are delayed, never
concurrent. While it waits, GitHub shows that job as *running*. A hook gives up (the job fails) after 2 hours of
waiting, and does nothing if the slot folder is missing or slots are off. A folder from before slots (`lock`) is
renamed to `slot-1` the first time anything looks, keeping its holder.
A slot whose holder has no job any more (cancelled job, crashed runner, so no completed hook) is cleared by the
watcher below, which checks that the holder's runner has no `Runner.Worker` (the VM's is asked over ssh); a
runner also clears a slot carrying its own name, which can only be left over from its own earlier job.

**Taking turns (so an idle runner is not even offered a job).** A loop in the VM daemon (every 4 s) looks at the
slots. While **every slot is held** (a job running on a side counts as holding one even before its hook has made
the folder):
- the VM's idle CI is turned off (`linuxrunner ci off`), and back on when a slot frees;
- the idle native runner is stopped, and started again when a slot frees.
With the default of 1 slot this is the old rule (the native runner in a job: the VM's CI is off; the VM in a job:
the native runner is stopped); with 2, both sides stay on and run together.

Neither side is ever stopped mid-job, and turning the VM's CI off first waits until no job is running there
(stopping a runner service during a job cancels it). Battery and `gitrunner ci off` (below) win over all of
it: nothing here starts a runner those rules say must be stopped. Everything is level-triggered, so a missed
step corrects itself on the next pass. If both runners take a job in the same few seconds before the loop
notices, the slots make the extra one wait.

## Power

The Mac's power watch (the loop that already pauses the native runners) also drives the VM: when the Mac
is on battery with pause-on-battery set, or after `gitrunner ci off`, it turns CI off in the VM, once no job is
running there; plugged in again, CI on. That runs in the
background so a long job does not hold up the loop. The heal pass re-registers the VM runner if GitHub has
dropped it (after 14 days offline), only while CI may run. The VM itself keeps running on battery (stopping
it would cut a job short); `linux-vm off` waits for a running job, then stops it.

## Turning it on

From the PC (PowerShell; each needs the Mac's admin runner online):

```powershell
runner update air-1                       # brings linuxrunner and linux-provision.sh to the Mac
runner linux-vm air-1 on                  # add --cpus 4 --memory 6 --disk 60 to size it
runner linux-vm air-1 status
runner doctor air-1-vm                    # linuxrunner doctor inside the VM, once its admin runner is up
```

The first `on` downloads Colima, Lima and an Ubuntu image (several minutes) and can outlast the Admin
workflow's 20-minute limit; if the job times out, the VM may still be coming up: run `runner linux-vm
air-1 status`, or finish over SSH: `runner ssh air-1 gitrunner linux-vm on`. `on` is safe to repeat; it
keeps what exists, resizes an idle VM and adds missing runners.

On the Mac itself: `sudo gitrunner linux-vm on|off|status`, `sudo gitrunner linux-vm off --purge`
(unregisters the runners, deletes the VM and its files), logs in `/var/log/mac-runners/linux-vm.log`.
`gitrunner status`, `doctor` and `info` include the VM.

Control the Mac's CI as a whole with `runner ci air-1 on|off`, not on `air-1-vm`: the Mac's power watch
owns the VM's CI state and would flip it back.

## Known limits

- Needs Apple Silicon and macOS 13 or later (Virtualization.framework). No Node or Docker in the VM, like
  the WSL distro.
- Whether Virtualization.framework runs from a launchd daemon with no logged-in user is the thing to
  confirm on the first Mac: `gitrunner linux-vm status` and `/var/log/mac-runners/linux-vm.log` show it.
- `runner add/drop air-1-vm ...` registers extra runners, which would run outside the turns and the slot
  (their jobs are not gated); do not add CI runners to the VM that way.
- The turns and the slot rely on the native runner's service file carrying the hooks, which the VM loop adds
  to an idle native runner (restarting it) once `linux-vm on` has run; a busy one picks them up on a later pass.
