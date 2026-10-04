"""linuxrunner's PC job slots: the hook (acquire, wait when full, release), stale clearing, pause and resume."""

import os
import subprocess
import time

from test_runner_limits_shell import LINUXRUNNER, STUBS, setup

PGREP = (
    '#!/bin/sh\ncase "$2" in *ci-1/bin*) [ -e "$STUB_BUSY1" ] ;; *ci-2/bin*) [ -e "$STUB_BUSY2" ] ;; *) false ;; esac\n'
)
# is-active reads a per-unit file so the tests can see pause (stop) and resume (start)
SYSTEMCTL = (
    STUBS["systemctl"]
    .replace("is-active) echo active ;;", 'is-active) [ -e "$STUB_DIR/stopped-$2" ] && echo inactive || echo active ;;')
    .replace(
        'echo "$@" >> "$STUB_LOG"',
        'echo "$@" >> "$STUB_LOG"\ncase "$1" in stop) : > "$STUB_DIR/stopped-$2" ;; start) rm -f "$STUB_DIR/stopped-$2" ;; esac',
    )
)
UNIT1, UNIT2 = "actions.runner.o-r.ci-1.service", "actions.runner.o-r.ci-2.service"


def slot_setup(tmp_path, slots="2", threads="4"):
    env, home, units = setup(tmp_path)
    # CI runs these tests inside a runner, whose own RUNNER_NAME must not leak into the hook
    env = {k: v for k, v in env.items() if k not in ("RUNNER_NAME", "GIT_RUNNER_NAME")}
    bindir = tmp_path / "bin"
    for name, body in (("pgrep", PGREP), ("systemctl", SYSTEMCTL), ("nproc", "#!/bin/sh\necho 16\n")):
        (bindir / name).write_text(body)
    (bindir / "taskset").write_text('#!/bin/sh\necho "$@" >> "$STUB_DIR/taskset.log"\n')
    (bindir / "taskset").chmod(0o755)
    slotdir = tmp_path / "pc" / "slots"
    env = {
        **env,
        "SLOT_DIR": str(slotdir),
        "SLOT_POLL": "0.1",
        "STUB_BUSY1": str(tmp_path / "busy1"),
        "STUB_BUSY2": str(tmp_path / "busy2"),
    }
    (home / "provisioned").write_text("")
    r = subprocess.run(["bash", str(LINUXRUNNER), "slots", slots, threads], env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return env, home, units, slotdir


def lr(env, *args):
    return subprocess.run(
        ["bash", str(LINUXRUNNER), *args], env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL
    )


def hook(env, action, name):
    env = {**env, "GIT_RUNNER_NAME": name}
    return subprocess.run(
        ["bash", str(LINUXRUNNER), "hook", action],
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
        stdin=subprocess.DEVNULL,
    )


def test_slots_writes_env_hooks_quota_and_cap_default(tmp_path):
    env, home, units, slotdir = slot_setup(tmp_path)
    assert (home / "slots").read_text() == "slots=2\nthreads=4\n"
    e = (tmp_path / "ci-1/.env").read_text().split("\n")
    assert "CI_MAX_CORES=4" in e and "GIT_RUNNER_NAME=ci-1" in e
    assert f"ACTIONS_RUNNER_HOOK_JOB_STARTED={home}/slot-start.sh" in e
    assert f"ACTIONS_RUNNER_HOOK_JOB_COMPLETED={home}/slot-done.sh" in e
    assert (units / f"{UNIT1}.d/limits.conf").read_text() == "[Service]\nCPUQuota=400%\n"
    assert (
        "hook acquire" in (home / "slot-start.sh").read_text() and "hook release" in (home / "slot-done.sh").read_text()
    )
    assert slotdir.is_dir()
    assert not (tmp_path / "admin/.env").exists()  # the admin runner is never touched


def test_hook_acquires_numbered_slots_pins_cpus_and_release_frees(tmp_path):
    env, home, units, slotdir = slot_setup(tmp_path)
    assert hook(env, "acquire", "ci-1").returncode == 0
    assert hook(env, "acquire", "ci-2").returncode == 0
    owner = (slotdir / "slot-1/owner").read_text()
    assert "runner=ci-1" in owner and "side=linux" in owner and "block=0" in owner
    assert "block=1" in (slotdir / "slot-2/owner").read_text()
    pins = (tmp_path / "taskset.log").read_text().split("\n")
    assert "-a -pc 0-3 " in pins[0] and "-a -pc 4-7 " in pins[1]
    assert hook(env, "release", "ci-1").returncode == 0
    assert not (slotdir / "slot-1").exists() and (slotdir / "slot-2").exists()


def test_hook_waits_when_full_then_takes_the_freed_slot(tmp_path):
    env, home, units, slotdir = slot_setup(tmp_path)
    hook(env, "acquire", "ci-1")
    hook(env, "acquire", "ci-2")
    p = subprocess.Popen(
        ["bash", str(LINUXRUNNER), "hook", "acquire"],
        env={**env, "GIT_RUNNER_NAME": "third"},
        stdout=subprocess.PIPE,
        text=True,
        stdin=subprocess.DEVNULL,
    )
    time.sleep(0.8)
    assert p.poll() is None  # still waiting: a third job is delayed, never concurrent
    assert len(list(slotdir.glob("slot-*"))) == 2
    hook(env, "release", "ci-2")
    out, _ = p.communicate(timeout=10)
    assert "waiting for a PC job slot" in out and "got PC job slot 2 of 2" in out
    assert "runner=third" in (slotdir / "slot-2/owner").read_text()


def test_hook_drops_a_slot_the_same_runner_left_behind(tmp_path):
    env, home, units, slotdir = slot_setup(tmp_path, slots="1")
    hook(env, "acquire", "ci-1")  # a job that never released (crash)
    assert hook(env, "acquire", "ci-1").returncode == 0  # its next job is not blocked by its own ghost
    assert "runner=ci-1" in (slotdir / "slot-1/owner").read_text()


def test_hook_without_slots_or_name_does_nothing(tmp_path):
    env, home, units, slotdir = slot_setup(tmp_path)
    assert (
        subprocess.run(
            ["bash", str(LINUXRUNNER), "hook", "acquire"], env=env, capture_output=True, text=True, timeout=10
        ).returncode
        == 0
    )
    assert not list(slotdir.glob("slot-*"))
    (home / "slots").unlink()
    assert hook(env, "acquire", "ci-1").returncode == 0
    assert not list(slotdir.glob("slot-*"))


def make_slot(slotdir, num, runner, side="linux", age=100):
    d = slotdir / f"slot-{num}"
    d.mkdir(parents=True)
    (d / "owner").write_text(f"runner={runner}\nside={side}\ntime=1\nblock={num - 1}\n")
    t = time.time() - age
    os.utime(d, (t, t))


def test_sync_clears_stale_slots_of_idle_runners_only(tmp_path):
    env, home, units, slotdir = slot_setup(tmp_path)
    make_slot(slotdir, 1, "ci-1")  # no worker: stale
    make_slot(slotdir, 2, "ci-2")  # busy
    (tmp_path / "busy2").write_text("")
    assert lr(env, "slot-sync").returncode == 0
    assert not (slotdir / "slot-1").exists() and (slotdir / "slot-2").exists()


def test_sync_leaves_fresh_and_windows_slots_alone(tmp_path):
    env, home, units, slotdir = slot_setup(tmp_path)
    make_slot(slotdir, 1, "ci-1", age=2)  # just taken: the worker may not show yet
    make_slot(slotdir, 2, "win-1", side="win")  # the Windows side clears its own
    lr(env, "slot-sync")
    assert (slotdir / "slot-1").exists() and (slotdir / "slot-2").exists()


def test_full_slots_pause_idle_runners_and_a_free_slot_resumes_them(tmp_path):
    env, home, units, slotdir = slot_setup(tmp_path)
    make_slot(slotdir, 1, "ci-1")
    make_slot(slotdir, 2, "win-1", side="win")
    (tmp_path / "busy1").write_text("")  # ci-1's job is real; win-1 is on the Windows side
    lr(env, "slot-sync")
    log = (tmp_path / "systemctl.log").read_text()
    assert f"stop {UNIT2}" in log and f"stop {UNIT1}" not in log  # the busy runner is never cut short
    assert (tmp_path / f"stopped-{UNIT2}").exists()
    (slotdir / "slot-2").rename(tmp_path / "gone")  # the Windows job finished
    lr(env, "slot-sync")
    assert not (tmp_path / f"stopped-{UNIT2}").exists()
    assert f"start {UNIT2}" in (tmp_path / "systemctl.log").read_text()


def test_ci_off_wins_over_slot_resume(tmp_path):
    env, home, units, slotdir = slot_setup(tmp_path)
    (tmp_path / f"stopped-{UNIT1}").write_text("")
    (home / "ci-off").write_text("")
    lr(env, "slot-sync")
    assert (tmp_path / f"stopped-{UNIT1}").exists()


def test_ci_off_stops_idle_runners_now_and_a_busy_one_when_its_job_ends(tmp_path):
    env, home, units, slotdir = slot_setup(tmp_path)
    (tmp_path / "busy1").write_text("")  # ci-1 is in a job, ci-2 is idle
    assert lr(env, "ci", "off").returncode == 0
    assert (tmp_path / f"stopped-{UNIT2}").exists() and not (tmp_path / f"stopped-{UNIT1}").exists()
    lr(env, "slot-sync")
    assert not (tmp_path / f"stopped-{UNIT1}").exists()  # still never cut short
    (tmp_path / "busy1").unlink()  # the job ended: the next follower pass stops it
    lr(env, "slot-sync")
    assert (tmp_path / f"stopped-{UNIT1}").exists()


def test_slots_off_removes_hooks_and_resumes_paused_runners(tmp_path):
    env, home, units, slotdir = slot_setup(tmp_path)
    (tmp_path / f"stopped-{UNIT1}").write_text("")
    assert lr(env, "slots", "off").returncode == 0
    assert not (home / "slots").exists()
    assert "HOOK" not in (tmp_path / "ci-1/.env").read_text() and not (tmp_path / "ci-1/.env").read_text().strip()
    assert not (units / f"{UNIT1}.d").exists()
    assert not (tmp_path / f"stopped-{UNIT1}").exists()


def test_slots_rejects_bad_input(tmp_path):
    env, home, units, slotdir = slot_setup(tmp_path)
    for args in (["0"], ["x"], ["3", "0"], ["3", "17"], ["33"]):
        assert lr(env, "slots", *args).returncode != 0, args


def test_follow_once_applies_windows_state_once(tmp_path):
    env, home, units, slotdir = slot_setup(tmp_path, slots="2")
    state = tmp_path / "pc" / "linux-state"
    state.write_text("ci=on\ncores=all\nslots=3\nthreads=6\n")
    env.pop("SLOT_DIR")
    assert lr(env, "follow-once", str(state)).returncode == 0
    assert (home / "slots").read_text() == "slots=3\nthreads=6\n"
    assert (home / "slot-dir").read_text().strip() == str(tmp_path / "pc" / "slots")
    n = (tmp_path / "systemctl.log").read_text().count("daemon-reload")
    lr(env, "follow-once", str(state))
    assert (tmp_path / "systemctl.log").read_text().count("daemon-reload") == n  # unchanged state: nothing re-applied
    state.write_text("ci=on\ncores=all\nslots=off\n")
    lr(env, "follow-once", str(state))
    assert not (home / "slots").exists()


def test_cpu_block_wraps_on_a_small_box(tmp_path):
    env, home, units, slotdir = slot_setup(tmp_path)
    (tmp_path / "bin" / "nproc").write_text("#!/bin/sh\necho 20\n")
    lr(env, "slots", "3", "8")
    hook(env, "acquire", "ci-1")
    hook(env, "acquire", "ci-2")
    hook(env, "acquire", "second")
    pins = (tmp_path / "taskset.log").read_text()
    assert "-a -pc 0-7 " in pins and "-a -pc 8-15 " in pins and "-a -pc 16-19,0-3 " in pins


def test_runner_slots_arguments_are_checked_before_anything_is_sent():
    runner = LINUXRUNNER.parent.parent / "runner"
    for args, msg in (
        (["win-1"], "slots HOST"),
        (["win-1", "abc"], "slots HOST"),
        (["win-1", "3", "x"], "THREADS"),
        (["air-1", "hold"], "is for the PC"),
        (["wsl-1", "release"], "is for the PC"),
        (["win-1", "hold", "bad name"], "NAME"),
        (["win-1", "hold", "a", "x"], "TIMEOUT"),
        (["air-1", "99x"], "slots HOST"),
    ):
        r = subprocess.run([str(runner), "slots", *args], capture_output=True, text=True, stdin=subprocess.DEVNULL)
        assert r.returncode != 0 and msg in r.stderr, (args, r.stderr)


def test_limits_keep_hooks_another_tool_wrote_and_work_without_slots(tmp_path):
    # The Mac's Linux VM puts its own hooks in the runner's .env and never turns slots on.
    env, home, units = setup(tmp_path)
    (tmp_path / "ci-1/.env").write_text(
        "ACTIONS_RUNNER_HOOK_JOB_STARTED=/opt/git-runner/hooks/job-started.sh\n"
        "ACTIONS_RUNNER_HOOK_JOB_COMPLETED=/opt/git-runner/hooks/job-completed.sh\nSLOT_OWNER=ci-1\nGITRUNNER_SLOT_DIR=/x\n"
    )
    assert lr(env, "cores", "2").returncode == 0
    assert lr(env, "limit", "ci-1", "ram=1024").returncode == 0
    e = (tmp_path / "ci-1/.env").read_text()
    for k in (
        "hooks/job-started.sh",
        "hooks/job-completed.sh",
        "SLOT_OWNER=ci-1",
        "GITRUNNER_SLOT_DIR=/x",
        "CI_MAX_CORES=2",
    ):
        assert k in e
    assert lr(env, "slot-sync").returncode == 0 and not (home / "slots").exists()
    assert "follow" not in (tmp_path / "systemctl.log").read_text()


def test_linux_sync_never_clears_a_hold_slot(tmp_path):
    env, home, units, slotdir = slot_setup(tmp_path)
    make_slot(slotdir, 2, "claude-vm", side="hold", age=500)
    make_slot(slotdir, 1, "ci-2")
    lr(env, "slot-sync")
    assert (slotdir / "slot-2").exists() and not (slotdir / "slot-1").exists()


# --- changing N while jobs run -----------------------------------------------------------------------------


def set_n(home, n, threads=4):
    (home / "slots").write_text(f"slots={n}\nthreads={threads}\n")


def start_hook(env, name):
    return subprocess.Popen(
        ["bash", str(LINUXRUNNER), "hook", "acquire"],
        env={**env, "GIT_RUNNER_NAME": name},
        stdout=subprocess.PIPE,
        text=True,
        stdin=subprocess.DEVNULL,
    )


def test_raising_n_lets_a_waiting_job_in_at_once(tmp_path):
    env, home, units, slotdir = slot_setup(tmp_path, slots="1")
    hook(env, "acquire", "a")
    p = start_hook(env, "b")
    time.sleep(0.6)
    assert p.poll() is None
    set_n(home, 2)
    out, _ = p.communicate(timeout=10)
    assert "got PC job slot 2 of 2" in out
    assert len(list(slotdir.glob("slot-*"))) == 2


def test_lowering_n_never_evicts_and_counts_every_slot_folder(tmp_path):
    env, home, units, slotdir = slot_setup(tmp_path, slots="3")
    for name in ("a", "b", "c"):
        hook(env, "acquire", name)
    set_n(home, 2)
    assert len(list(slotdir.glob("slot-*"))) == 3  # lowering changes nothing already running
    p = start_hook(env, "d")
    time.sleep(0.6)
    hook(env, "release", "c")  # slot-3 frees, but two are still held: the cap is 2
    time.sleep(0.6)
    assert p.poll() is None and sorted(x.name for x in slotdir.glob("slot-*")) == ["slot-1", "slot-2"]
    hook(env, "release", "a")  # now one is held: d gets the free slot
    out, _ = p.communicate(timeout=10)
    assert "got PC job slot 1 of 2" in out
    assert len(list(slotdir.glob("slot-*"))) == 2


def test_a_hole_below_n_is_not_taken_when_a_high_slot_still_counts(tmp_path):
    env, home, units, slotdir = slot_setup(tmp_path, slots="3")
    for name in ("a", "b", "c"):
        hook(env, "acquire", name)
    hook(env, "release", "b")  # held: slot-1 and slot-3
    set_n(home, 2)
    p = start_hook(env, "d")
    time.sleep(0.8)
    assert p.poll() is None and not (slotdir / "slot-2").exists()
    p.kill()
    p.communicate()


def test_simultaneous_jobs_never_exceed_n(tmp_path):
    env, home, units, slotdir = slot_setup(tmp_path, slots="2")
    procs = [start_hook(env, f"j{i}") for i in range(6)]
    time.sleep(2.5)
    assert len([p for p in procs if p.poll() is not None]) == 2
    assert len(list(slotdir.glob("slot-*"))) == 2
    for p in procs:
        p.kill()
        p.communicate()


def test_controller_follows_held_folders_not_slot_numbers(tmp_path):
    env, home, units, slotdir = slot_setup(tmp_path, slots="3")
    make_slot(slotdir, 3, "win-1", side="win", age=1)
    make_slot(slotdir, 2, "win-2", side="win", age=1)
    set_n(home, 2)
    lr(env, "slot-sync")  # two held, N=2: full, idle runners pause
    assert (tmp_path / f"stopped-{UNIT1}").exists() and (tmp_path / f"stopped-{UNIT2}").exists()
    (slotdir / "slot-2").rename(tmp_path / "gone")  # slot-3 alone is held, below N=2: resume
    lr(env, "slot-sync")
    assert not (tmp_path / f"stopped-{UNIT1}").exists() and not (tmp_path / f"stopped-{UNIT2}").exists()
    set_n(home, 4)
    make_slot(slotdir, 1, "win-3", side="win", age=1)
    lr(env, "slot-sync")
    assert not (tmp_path / f"stopped-{UNIT1}").exists()  # raised: two held of four stays open
