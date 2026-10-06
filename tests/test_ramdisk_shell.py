"""linuxrunner's `ramdisk` command (a tmpfs on each CI runner's work folder), against stubbed systemd and mount tools."""

import fcntl
import os
import re
import subprocess
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
LINUXRUNNER = ROOT / "linux" / "linuxrunner"
UNIT1 = "actions.runner.o-r.ci-1.service"
UNIT2 = "actions.runner.o-r.ci-2.service"
STUBS = {
    "id": '#!/bin/sh\n[ "$1" = -u ] && [ $# -eq 1 ] && echo 0 || /usr/bin/id "$@"\n',
    "free": "#!/bin/sh\nprintf '              total\\nMem:          16000\\n'\n",
    "pgrep": '#!/bin/sh\n[ -e "$STUB_BUSY" ]\n',
    # stopped.<unit> files make is-active report a stopped runner; the log shows every call
    "systemctl": """#!/bin/sh
echo "$@" >> "$STUB_LOG"
case "$1" in
  list-units) echo "actions.runner.o-r.ci-1.service loaded active running x"; echo "actions.runner.o-r.ci-2.service loaded active running x"; echo "actions.runner.o-r.wsl-1-admin.service loaded active running x" ;;
  show) case "$3" in
          User) echo "$STUB_USER" ;;
          *) case "$5" in *ci-1*) echo "$STUB_DIR/ci-1" ;; *ci-2*) echo "$STUB_DIR/ci-2" ;; *) echo "$STUB_DIR/admin" ;; esac ;;
        esac ;;
  is-active) if [ -e "$STUB_DIR/stopped.$2" ] || [ -e "$STUB_DIR/alloff" ]; then echo inactive; else echo active; fi ;;
  stop) touch "$STUB_DIR/stopped.$2" ;;
  start) rm -f "$STUB_DIR/stopped.$2" ;;
esac
exit 0
""",
    # mount, umount and mountpoint keep their state in a file, so nothing is ever really mounted
    "mount": '#!/bin/sh\n[ -e "$STUB_MOUNT_FAIL" ] && exit 1\necho "mount $*" >> "$STUB_MOUNTLOG"\nfor a; do p=$a; done\ngrep -qxF "$p" "$STUB_MOUNTS" 2>/dev/null || echo "$p" >> "$STUB_MOUNTS"\n',
    "umount": '#!/bin/sh\n[ -e "$STUB_UMOUNT_FAIL" ] && exit 1\necho "umount $*" >> "$STUB_MOUNTLOG"\ngrep -vxF "$1" "$STUB_MOUNTS" > "$STUB_MOUNTS.new"; mv "$STUB_MOUNTS.new" "$STUB_MOUNTS"\n',
    "mountpoint": '#!/bin/sh\ngrep -qxF "$2" "$STUB_MOUNTS" 2>/dev/null\n',
    # rm refuses anything named "stuck" while STUB_RM_FAIL is set: a stand-in for root-owned files the runner's user cannot delete
    "rm": '#!/bin/sh\nif [ -n "${STUB_RM_FAIL:-}" ]; then for a; do case "$a" in *stuck*) exit 1 ;; esac; done; fi\nexec /bin/rm "$@"\n',
}


def setup(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name, body in STUBS.items():
        (bindir / name).write_text(body)
        (bindir / name).chmod(0o755)
    home, units = tmp_path / "home", tmp_path / "systemd"
    for d in ("ci-1", "ci-2", "admin"):
        (tmp_path / d).mkdir()
        (tmp_path / d / ".runner").write_text('{"gitHubUrl": "https://github.com/o/r"}')
    home.mkdir()
    (tmp_path / "mounts").write_text("")
    env = {
        **os.environ,
        "PATH": f"{bindir}:{os.environ['PATH']}",
        "GIT_RUNNER_HOME": str(home),
        "SYSTEMD_DIR": str(units),
        "STUB_DIR": str(tmp_path),
        "STUB_LOG": str(tmp_path / "systemctl.log"),
        "STUB_BUSY": str(tmp_path / "busy"),
        "STUB_MOUNTS": str(tmp_path / "mounts"),
        "STUB_MOUNTLOG": str(tmp_path / "mount.log"),
        "STUB_USER": subprocess.run(["whoami"], capture_output=True, text=True).stdout.strip(),
        "RAMDISK_RECHECK": "0",
        "RAMDISK_POLL": "0.1",
    }
    return env, home, units


def run(env, *args, **kw):
    return subprocess.run(
        ["bash", str(LINUXRUNNER), *args],
        env=env,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        timeout=30,
        **kw,
    )


def syslog(tmp_path):
    p = tmp_path / "systemctl.log"
    return p.read_text().splitlines() if p.exists() else []


def mounts(tmp_path):
    return set((tmp_path / "mounts").read_text().split())


def changes(tmp_path):
    return [ln for ln in syslog(tmp_path) if ln.startswith(("stop ", "start ", "restart "))]


def dropin(units, unit=UNIT1):
    return units / f"{unit}.d/ramdisk.conf"


# --- on and off ---------------------------------------------------------------------------------


def test_on_mounts_every_ci_runner_and_wires_a_root_mount_on_each_start_but_not_for_the_admin(tmp_path):
    env, home, units = setup(tmp_path)
    r = run(env, "ramdisk", "on", "2048")
    assert r.returncode == 0, r.stderr
    assert (home / "ramdisk").read_text().strip() == "2048"
    assert mounts(tmp_path) == {f"{tmp_path}/ci-1/_work", f"{tmp_path}/ci-2/_work"}
    text = dropin(units).read_text()
    assert "# size=2048\n" in text
    m = re.search(
        rf"ExecStartPre=\+{re.escape(str(home))}/linuxrunner ramdisk-mount {tmp_path}/ci-1/_work 2048 \d+ \d+\n", text
    )
    assert m, text
    assert (
        "nosuid,nodev" in (tmp_path / "mount.log").read_text() and "noexec" not in (tmp_path / "mount.log").read_text()
    )
    assert not (units / "actions.runner.o-r.wsl-1-admin.service.d").exists() and not (tmp_path / "admin/_work").exists()


def test_on_deletes_what_was_on_the_disk_before_mounting(tmp_path):
    env, home, units = setup(tmp_path)
    old = tmp_path / "ci-1/_work/repo/repo"
    old.mkdir(parents=True)
    (old / "big.bin").write_text("x")
    assert run(env, "ramdisk", "on", "1024").returncode == 0
    assert list((tmp_path / "ci-1/_work").iterdir()) == []


def test_default_size_is_half_a_runners_share_of_ram(tmp_path):
    env, home, units = setup(tmp_path)
    assert run(env, "ramdisk", "on").returncode == 0
    assert "# size=4000\n" in dropin(units).read_text()  # 16000 MB / 2 runners / 2
    run(env, "slots", "4", "2")
    run(env, "ramdisk", "on")
    assert "# size=2000\n" in dropin(units).read_text()  # slots on: 16000 / 4 slots / 2


def test_leading_zeros_are_decimal_not_octal(tmp_path):
    env, home, units = setup(tmp_path)
    assert run(env, "ramdisk", "on", "0512").returncode == 0
    assert "# size=512\n" in dropin(units).read_text()
    assert run(env, "ramdisk", "on", "08").returncode != 0  # 8 MB: below the minimum, not an octal error


def test_bad_sizes_and_arguments_are_refused(tmp_path):
    env, home, units = setup(tmp_path)
    for args in (("on", "10"), ("on", "99999"), ("on", "lots"), ("sideways",), ("on", "512", "extra"), ("off", "5")):
        assert run(env, "ramdisk", *args).returncode != 0, args
    assert not (home / "ramdisk").exists() and not dropin(units).exists()


def test_on_twice_at_the_same_size_changes_nothing_and_a_new_size_resizes(tmp_path):
    env, home, units = setup(tmp_path)
    run(env, "ramdisk", "on", "1024")
    (tmp_path / "systemctl.log").write_text("")
    assert run(env, "ramdisk", "on", "1024").returncode == 0
    assert changes(tmp_path) == []  # every runner was already as asked: none is stopped or restarted
    assert run(env, "ramdisk", "on", "3072").returncode == 0
    assert "remount,size=3072m" in (tmp_path / "mount.log").read_text()
    assert "# size=3072\n" in dropin(units).read_text()


def test_off_unmounts_and_removes_everything(tmp_path):
    env, home, units = setup(tmp_path)
    run(env, "ramdisk", "on", "1024")
    r = run(env, "ramdisk", "off")
    assert r.returncode == 0, r.stderr
    assert not (home / "ramdisk").exists() and mounts(tmp_path) == set()
    assert not list(units.glob("*/ramdisk.conf"))
    env_lines = (tmp_path / "ci-1/.env").read_text().split("\n")
    assert not any(ln.startswith(("GIT_RUNNER_WORK=", "ACTIONS_RUNNER_HOOK")) for ln in env_lines)


def test_off_when_it_was_never_on_changes_nothing(tmp_path):
    env, home, units = setup(tmp_path)
    assert run(env, "ramdisk", "off").returncode == 0
    assert changes(tmp_path) == []


def test_a_failed_unmount_stops_the_change_before_touching_state_and_is_safe_to_rerun(tmp_path):
    env, home, units = setup(tmp_path)
    run(env, "ramdisk", "on", "1024")
    (tmp_path / "um").write_text("")
    r = run({**env, "STUB_UMOUNT_FAIL": str(tmp_path / "um")}, "ramdisk", "off")
    assert r.returncode != 0 and "could not unmount" in r.stderr
    assert (home / "ramdisk").exists()  # the setting is still on: it is cleared only once every runner is done
    assert not (home / "maintenance").exists()
    assert run(env, "ramdisk", "off").returncode == 0 and not (home / "ramdisk").exists()


def test_status_says_whether_the_ram_workspace_is_on(tmp_path):
    env, home, units = setup(tmp_path)
    assert "RAM workspace: off" in run(env, "status").stdout
    run(env, "ramdisk", "on", "1024")
    assert "RAM workspace: on (1024 MB per runner)" in run(env, "status").stdout


# --- the mount step -----------------------------------------------------------------------------


def test_ramdisk_mount_refuses_a_symlinked_work_folder_and_bad_arguments(tmp_path):
    env, home, units = setup(tmp_path)
    target = tmp_path / "elsewhere"
    target.mkdir()
    (target / "keep").write_text("x")
    link = tmp_path / "ci-1/_work"
    link.symlink_to(target)
    r = run(env, "ramdisk-mount", str(link), "1024", "0", "0")
    assert r.returncode != 0 and "symlink" in r.stderr
    assert mounts(tmp_path) == set() and (target / "keep").exists()
    for args in (("relative/path", "1024", "0", "0"), (str(tmp_path / "ci-1"), "x", "0", "0"), ("/a b", "1", "0", "0")):
        assert run(env, "ramdisk-mount", *args).returncode != 0, args
    assert run(env, "ramdisk", "on", "1024").returncode != 0  # `on` refuses the symlinked runner too
    assert (target / "keep").exists()


def test_the_work_folder_and_owner_come_from_the_runners_own_config(tmp_path):
    env, home, units = setup(tmp_path)
    (tmp_path / "ci-2/.runner").write_text('{"gitHubUrl": "https://github.com/o/r", "workFolder": "_wk"}')
    assert run(env, "ramdisk", "on", "1024").returncode == 0
    assert f"{tmp_path}/ci-2/_wk" in mounts(tmp_path) and f"{tmp_path}/ci-2/_work" not in mounts(tmp_path)
    assert f"GIT_RUNNER_WORK={tmp_path}/ci-2/_wk" in (tmp_path / "ci-2/.env").read_text().split("\n")
    me = subprocess.run(["id", "-u"], capture_output=True, text=True).stdout.strip()
    assert f"uid={me}," in (tmp_path / "mount.log").read_text()  # the unit's User=, not the folder's owner


# --- draining: no job is ever cut short ---------------------------------------------------------


def test_a_job_that_outlasts_the_wait_stops_the_change_and_nothing_is_cut_short(tmp_path):
    env, home, units = setup(tmp_path)
    (tmp_path / "busy").write_text("")
    r = run({**env, "RAMDISK_WAIT": "1"}, "ramdisk", "on", "1024")
    assert r.returncode != 0 and "still running a job" in r.stderr
    assert not (home / "ramdisk").exists() and not (home / "maintenance").exists() and mounts(tmp_path) == set()
    assert not [ln for ln in syslog(tmp_path) if ln.startswith("stop ")]  # a busy runner is never stopped


def test_it_waits_for_a_running_job_to_finish_then_stops_and_changes_the_runners(tmp_path):
    env, home, units = setup(tmp_path)
    busy = tmp_path / "busy"
    busy.write_text("")
    p = subprocess.Popen(
        ["bash", str(LINUXRUNNER), "ramdisk", "on", "1024"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        stdin=subprocess.DEVNULL,
    )
    time.sleep(1)
    assert p.poll() is None and mounts(tmp_path) == set()  # still waiting: the job is not interrupted
    assert (home / "maintenance").read_text().split()[0].isdigit()
    busy.unlink()
    out, err = p.communicate(timeout=20)
    assert p.returncode == 0, err
    assert "waiting for running jobs to finish" in out
    assert f"stop {UNIT1}" in syslog(tmp_path) and f"stop {UNIT2}" in syslog(tmp_path)
    assert len(mounts(tmp_path)) == 2 and not (home / "maintenance").exists()


def test_a_cancelled_change_clears_the_marker_and_restarts_what_it_stopped(tmp_path):
    env, home, units = setup(tmp_path)
    (tmp_path / "busy").write_text("")
    p = subprocess.Popen(
        ["bash", str(LINUXRUNNER), "ramdisk", "on", "1024"],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
    )
    time.sleep(1)
    assert (home / "maintenance").exists()
    p.terminate()  # what a cancelled workflow sends
    p.wait(timeout=10)
    assert not (home / "maintenance").exists() and mounts(tmp_path) == set()


def test_a_failure_part_way_restarts_the_runners_it_stopped_and_records_nothing(tmp_path):
    env, home, units = setup(tmp_path)
    (tmp_path / "mf").write_text("")
    r = run({**env, "STUB_MOUNT_FAIL": str(tmp_path / "mf")}, "ramdisk", "on", "1024")
    assert r.returncode != 0 and "running it again completes it" in r.stdout + r.stderr
    log = syslog(tmp_path)
    for unit in (UNIT1, UNIT2):
        assert f"stop {unit}" in log and f"start {unit}" in log
    assert log.index(f"stop {UNIT1}") < log.index(f"start {UNIT1}")
    assert not (home / "maintenance").exists() and not (home / "ramdisk").exists()


def test_runners_that_were_paused_stay_stopped_through_a_change(tmp_path):
    env, home, units = setup(tmp_path)
    (tmp_path / "alloff").write_text("")  # ci off: every runner is stopped
    (home / "ci-off").write_text("")
    for action in (("on", "1024"), ("off",)):
        (tmp_path / "systemctl.log").write_text("")
        assert run(env, "ramdisk", *action).returncode == 0
        assert changes(tmp_path) == []


def test_a_stale_maintenance_marker_is_ignored_and_a_live_one_is_honoured(tmp_path):
    env, home, units = setup(tmp_path)
    (home / "ramdisk").write_text("1024\n")
    (home / "maintenance").write_text(f"99999999 {int(time.time())}\n")  # the process was killed: its marker stays
    assert run(env, "slot-sync").returncode == 0
    assert not (home / "maintenance").exists()
    (home / "maintenance").write_text(f"{os.getpid()} {int(time.time())}\n")
    assert run(env, "slot-sync").returncode == 0
    assert (home / "maintenance").exists()
    (home / "maintenance").write_text(f"{os.getpid()} 1\n")  # alive but far too old for any wait
    assert run(env, "slot-sync").returncode == 0
    assert not (home / "maintenance").exists()


def test_two_changes_never_run_at_once(tmp_path):
    env, home, units = setup(tmp_path)
    fd = os.open(home / "lock", os.O_CREAT | os.O_RDWR)
    fcntl.flock(fd, fcntl.LOCK_EX)  # another linuxrunner change holds the lock
    try:
        for args in (("ramdisk", "on", "1024"), ("slots", "2"), ("cores", "2"), ("ci", "off")):
            r = run({**env, "LOCK_WAIT": "1"}, *args)
            assert r.returncode != 0 and "in progress" in r.stderr, args
        assert not (home / "ramdisk").exists() and not (home / "slots").exists()
    finally:
        os.close(fd)
    assert run(env, "slots", "2").returncode == 0


# --- the job hooks ------------------------------------------------------------------------------


def hook(env, action, work, **extra):
    return subprocess.run(
        ["bash", str(LINUXRUNNER), "hook", action],
        env={**env, "GIT_RUNNER_NAME": "ci-1", "GIT_RUNNER_WORK": str(work), **extra},
        capture_output=True,
        text=True,
        timeout=30,
        stdin=subprocess.DEVNULL,
    )


def ram_work(tmp_path, name="ci-1/_work"):
    work = tmp_path / name
    work.mkdir(parents=True, exist_ok=True)
    with open(tmp_path / "mounts", "a") as f:
        f.write(f"{work}\n")
    return work


def test_on_wires_the_job_hooks_even_with_slots_off(tmp_path):
    env, home, units = setup(tmp_path)
    run(env, "ramdisk", "on", "1024")
    e = (tmp_path / "ci-1/.env").read_text().split("\n")
    assert f"ACTIONS_RUNNER_HOOK_JOB_COMPLETED={home}/slot-done.sh" in e
    assert f"GIT_RUNNER_WORK={tmp_path}/ci-1/_work" in e
    assert "hook release" in (home / "slot-done.sh").read_text()
    assert "hook acquire" in (home / "slot-start.sh").read_text()
    assert not (tmp_path / "admin/.env").exists()


def test_release_empties_the_workspace_completely_and_resets_the_runners_own_folders(tmp_path):
    env, home, units = setup(tmp_path)
    work = ram_work(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "precious").write_text("x")
    for d in (
        "repo/repo",
        "scratch",
        "_scratch",
        "_tool/node",
        "_actions/checkout",
        "_PipelineMapping/r",
        "with space/.hidden",
    ):
        (work / d).mkdir(parents=True)
        (work / d / "f").write_text("x")
    (work / ".dotfile").write_text("x")
    ro = work / "vendor/mod/pkg"  # read-only trees, as Go's module cache and cargo leave them
    ro.mkdir(parents=True)
    (ro / "f").write_text("x")
    (work / "vendor/mod/pkg").chmod(0o555)
    (work / "vendor/mod").chmod(0o555)
    (work / "link").symlink_to(outside)  # must be removed itself, never followed
    (work / "_temp/_runner_file_commands").mkdir(parents=True)
    (work / "_temp/_runner_file_commands/save_state_1").write_text("x")  # GITHUB_ENV and friends
    (work / "_temp/_github_workflow").mkdir()
    (work / "_temp/_github_workflow/event.json").write_text("{}")
    (work / "_temp/_github_home/.config").mkdir(parents=True)  # a Docker action's /github/home
    (work / "_temp/_github_home/.config/token").write_text("secret")
    (work / "_temp/pytest-1").mkdir()
    r = hook(env, "release", work)
    assert r.returncode == 0, r.stderr
    assert sorted(p.name for p in work.iterdir()) == ["_temp"]
    assert sorted(p.name for p in (work / "_temp").iterdir()) == ["_github_workflow", "_runner_file_commands"]
    assert list((work / "_temp/_runner_file_commands").iterdir()) == []
    assert list((work / "_temp/_github_workflow").iterdir()) == []
    assert (outside / "precious").exists()  # nothing outside the workspace was touched


def test_release_replaces_a_runner_folder_a_job_turned_into_a_symlink(tmp_path):
    env, home, units = setup(tmp_path)
    work = ram_work(tmp_path)
    outside = tmp_path / "outside"
    (outside / "inner").mkdir(parents=True)
    (outside / "inner/f").write_text("x")
    (work / "_temp").symlink_to(outside)
    assert hook(env, "release", work).returncode == 0
    assert (work / "_temp").is_dir() and not (work / "_temp").is_symlink()
    assert (outside / "inner/f").exists()
    (work / "_temp/_github_workflow").symlink_to(outside)
    assert hook(env, "release", work).returncode == 0
    assert (work / "_temp/_github_workflow").is_dir() and not (work / "_temp/_github_workflow").is_symlink()
    assert (outside / "inner/f").exists()


def test_the_hooks_only_ever_touch_a_real_ram_mount(tmp_path):
    env, home, units = setup(tmp_path)
    plain = tmp_path / "precious"  # a folder on the disk: RAM mode is off or the mount is missing
    plain.mkdir()
    (plain / "f").write_text("x")
    for action in ("release", "acquire"):
        assert hook(env, action, plain).returncode == 0
        assert (plain / "f").exists()
    target = tmp_path / "target"
    target.mkdir()
    (target / "f").write_text("x")
    (tmp_path / "linked").symlink_to(target)
    with open(tmp_path / "mounts", "a") as f:
        f.write(f"{tmp_path}/linked\n")
    assert hook(env, "release", tmp_path / "linked").returncode == 0
    assert (target / "f").exists()


def test_the_started_hook_never_touches_the_workspace(tmp_path):
    # By the time it runs, the runner has downloaded the job's actions and made its workspace: wiping there broke every job
    env, home, units = setup(tmp_path)
    work = ram_work(tmp_path)
    for d in ("_actions/actions/checkout/v5", "Priorities/Priorities", "_temp/_runner_file_commands"):
        (work / d).mkdir(parents=True)
    (work / "_actions/actions/checkout/v5/action.yml").write_text("name: checkout")
    r = hook(env, "acquire", work)
    assert r.returncode == 0, r.stderr
    assert (work / "_actions/actions/checkout/v5/action.yml").exists() and (work / "Priorities/Priorities").is_dir()


def test_files_the_user_cannot_delete_hold_the_finished_job_until_the_root_sweep_clears_them(tmp_path):
    env, home, units = setup(tmp_path)
    (home / "ramdisk").write_text("1024\n")
    work = ram_work(tmp_path)
    (work / "stuck").mkdir()
    (work / "stuck/f").write_text("x")
    p = subprocess.Popen(
        ["bash", str(LINUXRUNNER), "hook", "release"],
        env={
            **env,
            "STUB_RM_FAIL": "1",
            "GIT_RUNNER_NAME": "ci-1",
            "GIT_RUNNER_WORK": str(work),
            "RAMDISK_SWEEP_POLL": "0.2",
            "RAMDISK_SWEEP_WAIT": "20",
        },
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        stdin=subprocess.DEVNULL,
    )
    for _ in range(50):
        if (work / ".hook-waiting").exists():
            break
        time.sleep(0.1)
    assert (work / ".hook-waiting").exists() and p.poll() is None  # the runner stays busy: no new job meanwhile
    assert run(env, "ramdisk-sweep").returncode == 0  # what the path unit runs, as root (rm works for it)
    out, err = p.communicate(timeout=20)
    assert p.returncode == 0, err
    assert "the root sweep cleaned" in out
    assert not (work / "stuck").exists() and not (work / ".hook-waiting").exists()


def test_without_a_root_sweep_the_finished_job_still_succeeds_and_says_why_files_remain(tmp_path):
    env, home, units = setup(tmp_path)
    work = ram_work(tmp_path)
    (work / "stuck").mkdir()
    r = hook(env, "release", work, STUB_RM_FAIL="1", RAMDISK_SWEEP_POLL="0.1", RAMDISK_SWEEP_WAIT="1")
    assert r.returncode == 0  # a finished job is never failed by its cleanup
    assert "could not be deleted" in r.stdout + r.stderr and "ramdisk-sweep.path" in r.stdout + r.stderr
    assert (work / "stuck").exists() and not (work / ".hook-waiting").exists()


def test_the_hook_scripts_never_fail_a_job(tmp_path):
    env, home, units = setup(tmp_path)
    run(env, "ramdisk", "on", "1024")
    for name in ("slot-start.sh", "slot-done.sh"):
        text = (home / name).read_text()
        assert "|| true" in text and text.rstrip().endswith("exit 0") and "exit 1" not in text


def test_on_installs_a_root_sweep_path_unit_and_off_removes_it(tmp_path):
    env, home, units = setup(tmp_path)
    run(env, "ramdisk", "on", "1024")
    path = (units / "runnerpool-ramdisk-sweep.path").read_text()
    assert (
        f"PathExists={tmp_path}/ci-1/_work/.hook-waiting" in path
        and f"PathExists={tmp_path}/ci-2/_work/.hook-waiting" in path
    )
    assert "admin" not in path
    assert f"ExecStart={home}/linuxrunner ramdisk-sweep" in (units / "runnerpool-ramdisk-sweep.service").read_text()
    assert "enable --now runnerpool-ramdisk-sweep.path" in syslog(tmp_path)
    run(env, "ramdisk", "off")
    assert (
        not (units / "runnerpool-ramdisk-sweep.path").exists()
        and not (units / "runnerpool-ramdisk-sweep.service").exists()
    )
    assert "disable --now runnerpool-ramdisk-sweep.path" in syslog(tmp_path)


def test_the_sweep_removes_its_marker_so_the_path_unit_does_not_fire_again(tmp_path):
    env, home, units = setup(tmp_path)
    (home / "ramdisk").write_text("1024\n")
    work = ram_work(tmp_path)
    (work / "leftover").mkdir()
    (work / ".hook-waiting").write_text("")
    assert run(env, "ramdisk-sweep").returncode == 0
    assert not (work / "leftover").exists() and not (work / ".hook-waiting").exists()


def test_after_a_change_drained_runners_only_restart_into_free_slots(tmp_path):
    env, home, units = setup(tmp_path)
    run(env, "slots", "1", "2")
    held = home / "slots.d/slot-1"
    held.mkdir(parents=True)
    (held / "owner").write_text("runner=win-1\nside=win\ntime=1\n")  # the one slot is taken by a Windows job
    (tmp_path / "systemctl.log").write_text("")
    assert run(env, "ramdisk", "on", "1024").returncode == 0
    log = syslog(tmp_path)
    assert (
        f"stop {UNIT1}" in log and f"start {UNIT1}" not in log and f"start {UNIT2}" not in log
    )  # slot-sync resumes them later


def test_root_commands_from_a_wsl_session_rerun_in_the_services_mount_namespace():
    text = LINUXRUNNER.read_text()
    guard = text.split("\n# ---")[0]
    assert "nsenter -t 1 -m" in guard and "/proc/1/ns/mnt" in guard and '"$EUID" = 0' in guard
    assert "LINUXRUNNER_IN_NS" in guard  # never loops


def test_the_root_sweep_leaves_alone_a_workspace_nobody_is_waiting_on(tmp_path):
    env, home, units = setup(tmp_path)
    (home / "ramdisk").write_text("1024\n")
    work = ram_work(tmp_path)
    (work / "job-in-progress").mkdir()
    assert run(env, "slot-sync").returncode == 0
    assert (work / "job-in-progress").exists()  # no .hook-waiting: a running job's checkout is never swept


# --- add-runner and remove-runner ---------------------------------------------------------------


def add_runner_stubs(tmp_path, env, home):
    bindir = tmp_path / "bin"
    stubs = {
        "curl": '#!/bin/sh\ncase "$*" in *-w*) echo https://github.com/actions/runner/releases/tag/v2.9.9 ;; esac\n',
        # svc.sh logs "svc install" / "svc start" next to the mount log; `start` is what makes the unit active
        "tar": """#!/bin/sh
cat > /dev/null
while [ $# -gt 0 ]; do [ "$1" = -C ] && d=$2; shift; done
printf '#!/bin/sh\\nexit 0\\n' > "$d/config.sh"
printf '#!/bin/sh\\necho "svc $1" >> "$STUB_MOUNTLOG"\\n[ "$1" != start ] || touch "$STUB_DIR/started.new-1"\\n' > "$d/svc.sh"
chmod +x "$d/svc.sh" "$d/config.sh"
""",
        "sudo": '#!/bin/sh\n[ "$1" = -u ] && shift 2\nexec "$@"\n',
        "useradd": "#!/bin/sh\nexit 0\n",
        "chown": "#!/bin/sh\nexit 0\n",
    }
    for name, body in stubs.items():
        (bindir / name).write_text(body)
        (bindir / name).chmod(0o755)
    sc = (bindir / "systemctl").read_text()
    sc = sc.replace(
        "list-units) echo",
        'list-units) echo "actions.runner.o-r.new-1.service loaded active running x"; echo',
    )
    sc = sc.replace('*) case "$5" in', '*) case "$5" in *new-1*) echo "$STUB_HOME/runners/new-1" ;;')
    sc = sc.replace(
        'is-active) if [ -e "$STUB_DIR/stopped.$2" ]',
        'is-active) if { case "$2" in *new-1*) [ ! -e "$STUB_DIR/started.new-1" ] ;; *) false ;; esac; } || [ -e "$STUB_DIR/stopped.$2" ]',
    )
    (bindir / "systemctl").write_text(sc)
    return {**env, "STUB_HOME": str(home)}


@pytest.mark.parametrize("run_as", ["ci", "root"])  # a CI runner added as root is a CI runner too
def test_a_runner_added_while_ram_mode_is_on_is_mounted_and_gets_the_wipe_hook(tmp_path, run_as):
    env, home, units = setup(tmp_path)
    run(env, "ramdisk", "on", "1024")
    env = add_runner_stubs(tmp_path, env, home)
    r = run(env, "add-runner", "o/r", "new-1", "linux-ci", "TOKEN", run_as)
    assert r.returncode == 0, r.stderr
    work = f"{home}/runners/new-1/_work"
    e = (home / "runners/new-1/.env").read_text().split("\n")
    assert f"GIT_RUNNER_WORK={work}" in e
    assert f"ACTIONS_RUNNER_HOOK_JOB_COMPLETED={home}/slot-done.sh" in e
    assert work in (tmp_path / "mounts").read_text().split()
    assert f"ramdisk-mount {work} 1024 " in dropin(units, "actions.runner.o-r.new-1.service").read_text()
    # the workspace is mounted before the service starts, so the runner never holds a job while it is being changed
    order = [
        ln.split()[0] + " " + ln.split()[1]
        for ln in (tmp_path / "mount.log").read_text().splitlines()
        if ln.startswith(("svc ", "mount -t"))
    ]
    assert order[-3:] == ["svc install", "mount -t", "svc start"]
    assert "stop actions.runner.o-r.new-1.service" not in syslog(tmp_path)


def test_adding_a_runner_while_another_is_busy_still_mounts_the_new_one_only(tmp_path):
    env, home, units = setup(tmp_path)
    run(env, "ramdisk", "on", "1024")
    (tmp_path / "busy").write_text("")  # the other runners are running jobs
    (tmp_path / "systemctl.log").write_text("")
    env = add_runner_stubs(tmp_path, env, home)
    assert run(env, "add-runner", "o/r", "new-1", "linux-ci", "TOKEN").returncode == 0
    assert not [
        ln for ln in syslog(tmp_path) if ln.startswith("stop ") and "new-1" not in ln
    ]  # a busy runner is not touched


def test_removing_a_runner_unmounts_its_workspace_and_drops_its_wiring(tmp_path):
    env, home, units = setup(tmp_path)
    run(env, "ramdisk", "on", "1024")
    assert dropin(units).exists() and f"{tmp_path}/ci-1/_work" in mounts(tmp_path)
    r = run(env, "remove-runner", "ci-1")
    assert r.returncode == 0, r.stderr
    assert f"{tmp_path}/ci-1/_work" not in mounts(tmp_path) and not dropin(units).exists()
    assert f"{tmp_path}/ci-2/_work" in mounts(tmp_path)


# --- the runner command and the workflow --------------------------------------------------------


def run_runner(tmp_path, *args):
    """Runs `runner ramdisk ...` against a fake gh. Returns (exit code, stderr, the `workflow run` call or None)."""
    bindir = tmp_path / "gh-bin"
    bindir.mkdir(exist_ok=True)
    log = tmp_path / "gh.log"
    log.write_text("")
    (bindir / "gh").write_text(
        '#!/bin/sh\necho "$*" >> "$GH_LOG"\ncase "$1 $2" in\n  "run list") echo 42 ;;\n  "run view") echo completed ;;\nesac\nexit 0\n'
    )
    (bindir / "gh").chmod(0o755)
    env = {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}", "GH_LOG": str(log), "GITRUNNER_ORG": "example-org"}
    r = subprocess.run(
        [str(ROOT / "runner"), "ramdisk", *args],
        env=env,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        timeout=30,
    )
    call = next((ln for ln in log.read_text().splitlines() if ln.startswith("workflow run")), None)
    return r.returncode, r.stderr, call


def test_wsl_ramdisk_without_a_size_leaves_it_to_linuxrunner(tmp_path):
    rc, err, call = run_runner(tmp_path, "wsl-1", "on")
    assert rc == 0, err
    assert "admin-wsl.yml" in call and "action=ramdisk-on" in call
    assert "slots=" not in call and "size=" not in call  # no fixed per-runner size that could exhaust the distro's RAM


def test_wsl_ramdisk_with_a_size_sends_it_and_a_mac_keeps_its_four_gb_default(tmp_path):
    assert "slots=2" in run_runner(tmp_path, "wsl-1", "on", "2")[2]
    assert "size=4" in run_runner(tmp_path, "air-1", "on")[2]


@pytest.mark.parametrize(
    "args,msg",
    [
        (("wsl-1", "on", "abc"), "GB is a whole number"),
        (("wsl-1", "on", "12345"), "GB is a whole number"),
        (("wsl-1", "on", "2", "3"), "ramdisk HOST on"),
        (("wsl-1", "off", "foo"), "takes no size"),
        (("wsl-1", "off", "5"), "takes no size"),
        (("win-1", "on"), "native Windows"),
        (("air-1-vm", "on"), "Linux VM"),
        (("wsl-1", "sideways"), "ramdisk HOST on"),
    ],
)
def test_runner_ramdisk_checks_its_arguments_before_anything_is_sent(tmp_path, args, msg):
    rc, err, call = run_runner(tmp_path, *args)
    assert rc != 0 and msg in err and call is None


def test_the_drain_wait_fits_inside_the_admin_workflows_timeout_and_sizes_are_decimal():
    workflow = (ROOT / ".github/workflows/admin-wsl.yml").read_text()
    timeout_s = int(re.search(r"timeout-minutes:\s*(\d+)", workflow).group(1)) * 60
    default_wait = int(re.search(r"RAMDISK_WAIT:-(\d+)", LINUXRUNNER.read_text()).group(1))
    assert default_wait < timeout_s
    assert "10#$SLOTS" in workflow  # 08 and 010 are not octal


def test_on_completes_with_stopped_runners_whose_start_would_mount_under_the_lock(tmp_path):
    env, home, units = setup(tmp_path)
    (tmp_path / "alloff").write_text("")  # stopped, CI not off: ExecStartPre (ramdisk-mount) never takes the lock
    r = run({**env, "LOCK_WAIT": "1"}, "ramdisk", "on", "1024")
    assert r.returncode == 0, r.stderr
    assert len(mounts(tmp_path)) == 2
    assert run({**env, "LOCK_WAIT": "1"}, "ramdisk-mount", f"{tmp_path}/ci-1/_work", "1024", "0", "0").returncode == 0


def test_a_work_folder_under_a_symlinked_parent_is_refused(tmp_path):
    env, home, units = setup(tmp_path)
    real = tmp_path / "real"
    (real / "_work").mkdir(parents=True)
    (tmp_path / "via").symlink_to(real)
    r = run(env, "ramdisk-mount", f"{tmp_path}/via/_work", "1024", "0", "0")
    assert r.returncode != 0 and "symlink" in r.stderr and mounts(tmp_path) == set()
    assert run(env, "ramdisk-mount", f"{tmp_path}/ci-1/../ci-1", "1024", "0", "0").returncode != 0
