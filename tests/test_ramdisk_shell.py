"""linuxrunner's `ramdisk` command (a tmpfs on each CI runner's _work), against stubbed systemd and mount tools."""

import os
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LINUXRUNNER = ROOT / "linux" / "linuxrunner"
STUBS = {
    "id": '#!/bin/sh\n[ "$1" = -u ] && echo 0 || /usr/bin/id "$@"\n',
    "free": "#!/bin/sh\nprintf '              total\\nMem:          16000\\n'\n",
    "pgrep": '#!/bin/sh\n[ -e "$STUB_BUSY" ]\n',
    "systemctl": """#!/bin/sh
echo "$@" >> "$STUB_LOG"
case "$1" in
  list-units) echo "actions.runner.o-r.ci-1.service loaded active running x"; echo "actions.runner.o-r.ci-2.service loaded active running x"; echo "actions.runner.o-r.wsl-1-admin.service loaded active running x" ;;
  show) case "$5" in *ci-1*) echo "$STUB_DIR/ci-1" ;; *ci-2*) echo "$STUB_DIR/ci-2" ;; *) echo "$STUB_DIR/admin" ;; esac ;;
  is-active) echo active ;;
esac
exit 0
""",
    # mount, umount and mountpoint keep their state in a file, so nothing is ever really mounted
    "mount": '#!/bin/sh\necho "mount $*" >> "$STUB_MOUNTLOG"\nfor a; do p=$a; done\ngrep -qxF "$p" "$STUB_MOUNTS" 2>/dev/null || echo "$p" >> "$STUB_MOUNTS"\n',
    "umount": '#!/bin/sh\necho "umount $*" >> "$STUB_MOUNTLOG"\ngrep -vxF "$1" "$STUB_MOUNTS" > "$STUB_MOUNTS.new"; mv "$STUB_MOUNTS.new" "$STUB_MOUNTS"\n',
    "mountpoint": '#!/bin/sh\ngrep -qxF "$2" "$STUB_MOUNTS" 2>/dev/null\n',
}


def setup(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name, body in STUBS.items():
        (bindir / name).write_text(body)
        (bindir / name).chmod(0o755)
    home, units, etc = tmp_path / "home", tmp_path / "systemd", tmp_path / "etc"
    for d in ("ci-1", "ci-2", "admin"):
        (tmp_path / d).mkdir()
        (tmp_path / d / ".runner").write_text('{"gitHubUrl": "https://github.com/o/r"}')
    home.mkdir()
    etc.mkdir()
    (etc / "fstab").write_text("LABEL=cloudimg-rootfs / ext4 defaults 0 1\n")
    env = {
        **os.environ,
        "PATH": f"{bindir}:{os.environ['PATH']}",
        "GIT_RUNNER_HOME": str(home),
        "SYSTEMD_DIR": str(units),
        "ETC_DIR": str(etc),
        "STUB_DIR": str(tmp_path),
        "STUB_LOG": str(tmp_path / "systemctl.log"),
        "STUB_BUSY": str(tmp_path / "busy"),
        "STUB_MOUNTS": str(tmp_path / "mounts"),
        "STUB_MOUNTLOG": str(tmp_path / "mount.log"),
    }
    return env, home, units, etc


def run(env, *args):
    return subprocess.run(
        ["bash", str(LINUXRUNNER), *args], env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=20
    )


def tmpfs_lines(etc):
    return [ln for ln in (etc / "fstab").read_text().splitlines() if ln.startswith("tmpfs ")]


def test_on_writes_fstab_dropin_and_mounts_every_ci_runner_but_not_the_admin(tmp_path):
    env, home, units, etc = setup(tmp_path)
    r = run(env, "ramdisk", "on", "2048")
    assert r.returncode == 0, r.stderr
    assert (home / "ramdisk").read_text().strip() == "2048"
    lines = tmpfs_lines(etc)
    assert len(lines) == 2
    assert lines[0].startswith(f"tmpfs {tmp_path}/ci-1/_work tmpfs size=2048m,mode=0755,uid=")
    assert "nosuid,nodev" in lines[0] and "noexec" not in lines[0]  # jobs run binaries from the workspace
    assert "LABEL=cloudimg-rootfs / ext4 defaults 0 1" in (etc / "fstab").read_text()  # other lines are kept
    unit = "actions.runner.o-r.ci-1.service"
    assert (units / f"{unit}.d/ramdisk.conf").read_text() == f"[Unit]\nRequiresMountsFor={tmp_path}/ci-1/_work\n"
    assert not (units / "actions.runner.o-r.wsl-1-admin.service.d").exists()
    assert not (tmp_path / "admin/_work").exists()
    assert set((tmp_path / "mounts").read_text().split()) == {f"{tmp_path}/ci-1/_work", f"{tmp_path}/ci-2/_work"}


def test_on_deletes_what_was_on_the_disk_before_mounting(tmp_path):
    env, home, units, etc = setup(tmp_path)
    old = tmp_path / "ci-1/_work/repo/repo"
    old.mkdir(parents=True)
    (old / "big.bin").write_text("x")
    assert run(env, "ramdisk", "on", "1024").returncode == 0
    assert list((tmp_path / "ci-1/_work").iterdir()) == []


def test_default_size_is_half_a_runners_share_of_ram(tmp_path):
    env, home, units, etc = setup(tmp_path)
    assert run(env, "ramdisk", "on").returncode == 0
    assert "size=4000m" in tmpfs_lines(etc)[0]  # 16000 MB / 2 runners / 2
    run(env, "slots", "4", "2")
    run(env, "ramdisk", "on")
    assert "size=2000m" in tmpfs_lines(etc)[0]  # slots on: 16000 / 4 slots / 2


def test_on_twice_is_idempotent_and_resizes(tmp_path):
    env, home, units, etc = setup(tmp_path)
    run(env, "ramdisk", "on", "1024")
    assert run(env, "ramdisk", "on", "3072").returncode == 0
    lines = tmpfs_lines(etc)
    assert len(lines) == 2 and all("size=3072m" in ln for ln in lines)
    assert "remount,size=3072m" in (tmp_path / "mount.log").read_text()


def test_a_job_that_outlasts_the_wait_stops_the_change_and_nothing_is_cut_short(tmp_path):
    env, home, units, etc = setup(tmp_path)
    (tmp_path / "busy").write_text("")
    r = run({**env, "RAMDISK_WAIT": "1", "RAMDISK_POLL": "0.2"}, "ramdisk", "on", "1024")
    assert r.returncode != 0 and "still running a job" in r.stderr
    assert tmpfs_lines(etc) == [] and not (home / "ramdisk").exists()
    assert not (home / "maintenance").exists()
    assert " stop " not in f" {(tmp_path / 'systemctl.log').read_text()} "  # a busy runner is never stopped


def test_it_waits_for_a_running_job_to_finish_then_stops_and_changes_the_runners(tmp_path):
    env, home, units, etc = setup(tmp_path)
    busy = tmp_path / "busy"
    busy.write_text("")
    p = subprocess.Popen(
        ["bash", str(LINUXRUNNER), "ramdisk", "on", "1024"],
        env={**env, "RAMDISK_POLL": "0.2"},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        stdin=subprocess.DEVNULL,
    )
    time.sleep(1)
    assert p.poll() is None and tmpfs_lines(etc) == []  # still waiting: the job is not interrupted
    assert (home / "maintenance").exists()
    busy.unlink()
    out, err = p.communicate(timeout=20)
    assert p.returncode == 0, err
    assert "waiting for running jobs to finish" in out
    log = (tmp_path / "systemctl.log").read_text()
    assert "stop actions.runner.o-r.ci-1.service" in log and "stop actions.runner.o-r.ci-2.service" in log
    assert len(tmpfs_lines(etc)) == 2 and not (home / "maintenance").exists()


def test_slot_sync_leaves_the_runners_alone_during_a_change(tmp_path):
    env, home, units, etc = setup(tmp_path)
    run(env, "slots", "1", "2")
    (home / "provisioned").write_text("")
    slotdir = home / "slots.d"
    slotdir.mkdir(exist_ok=True)
    (home / "maintenance").write_text("")
    (tmp_path / "systemctl.log").write_text("")
    assert run(env, "slot-sync").returncode == 0
    assert (tmp_path / "systemctl.log").read_text() == ""


def test_bad_sizes_are_refused(tmp_path):
    env, home, units, etc = setup(tmp_path)
    for bad in ("10", "99999", "lots"):
        assert run(env, "ramdisk", "on", bad).returncode != 0
    assert run(env, "ramdisk", "sideways").returncode != 0
    assert tmpfs_lines(etc) == []


def test_off_unmounts_and_removes_everything(tmp_path):
    env, home, units, etc = setup(tmp_path)
    run(env, "ramdisk", "on", "1024")
    r = run(env, "ramdisk", "off")
    assert r.returncode == 0, r.stderr
    assert tmpfs_lines(etc) == [] and not (home / "ramdisk").exists()
    assert (etc / "fstab").read_text() == "LABEL=cloudimg-rootfs / ext4 defaults 0 1\n"
    assert not list(units.glob("*/ramdisk.conf"))
    assert (tmp_path / "mounts").read_text() == ""


def test_status_says_whether_the_ram_workspace_is_on(tmp_path):
    env, home, units, etc = setup(tmp_path)
    assert "RAM workspace: off" in run(env, "status").stdout
    run(env, "ramdisk", "on", "1024")
    assert "RAM workspace: on (1024 MB per runner)" in run(env, "status").stdout


def test_on_wires_the_job_hooks_even_with_slots_off(tmp_path):
    env, home, units, etc = setup(tmp_path)
    run(env, "ramdisk", "on", "1024")
    e = (tmp_path / "ci-1/.env").read_text().split("\n")
    assert f"ACTIONS_RUNNER_HOOK_JOB_COMPLETED={home}/slot-done.sh" in e
    assert f"GIT_RUNNER_WORK={tmp_path}/ci-1/_work" in e
    assert "hook release" in (home / "slot-done.sh").read_text()
    assert not (tmp_path / "admin/.env").exists()
    run(env, "ramdisk", "off")
    assert not any(
        ln.startswith(("GIT_RUNNER_WORK=", "ACTIONS_RUNNER_HOOK"))
        for ln in (tmp_path / "ci-1/.env").read_text().split("\n")
    )


def test_release_hook_empties_the_workspace_but_keeps_tool_caches(tmp_path):
    env, home, units, etc = setup(tmp_path)
    work = tmp_path / "ci-1/_work"
    for d in ("repo/repo", "scratch", "_tool/node", "_actions/checkout", "_temp/pytest-1"):
        (work / d).mkdir(parents=True)
        (work / d / "f").write_text("x")
    (work / "_temp/_runner_file_commands").mkdir()
    for d in ("_scratch", "_PipelineMapping", "_temp/_leftover"):
        (work / d).mkdir(parents=True, exist_ok=True)
    hook_env = {**env, "GIT_RUNNER_NAME": "ci-1", "GIT_RUNNER_WORK": str(work)}
    r = subprocess.run(
        ["bash", str(LINUXRUNNER), "hook", "release"], env=hook_env, capture_output=True, text=True, timeout=20
    )
    assert r.returncode == 0, r.stderr
    assert sorted(p.name for p in work.iterdir()) == ["_PipelineMapping", "_actions", "_temp", "_tool"]
    assert [p.name for p in (work / "_temp").iterdir()] == ["_runner_file_commands"]


def test_release_hook_ignores_a_work_path_that_is_not_a_work_folder(tmp_path):
    env, home, units, etc = setup(tmp_path)
    keep = tmp_path / "precious"
    keep.mkdir()
    (keep / "f").write_text("x")
    hook_env = {**env, "GIT_RUNNER_NAME": "ci-1", "GIT_RUNNER_WORK": str(keep)}
    subprocess.run(
        ["bash", str(LINUXRUNNER), "hook", "release"], env=hook_env, capture_output=True, text=True, timeout=20
    )
    assert (keep / "f").exists()


def test_a_runner_added_while_ram_mode_is_on_is_mounted_and_gets_the_wipe_hook(tmp_path):
    env, home, units, etc = setup(tmp_path)
    run(env, "ramdisk", "on", "1024")
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
    # the new runner's unit shows up in systemd, with its folder under the tool's runners dir
    (bindir / "systemctl").write_text(
        (bindir / "systemctl")
        .read_text()
        .replace(
            "list-units) echo",
            'list-units) echo "actions.runner.o-r.new-1.service loaded active running x"; echo',
        )
        .replace('show) case "$5" in', 'show) case "$5" in *new-1*) echo "$STUB_HOME/runners/new-1" ;;')
        .replace(
            "is-active) echo active ;;",
            'is-active) case "$2" in *new-1*) [ -e "$STUB_DIR/started.new-1" ] && echo active || echo inactive ;; *) echo active ;; esac ;;',
        )
    )
    env["STUB_HOME"] = str(home)
    r = run(env, "add-runner", "o/r", "new-1", "linux-ci", "TOKEN")
    assert r.returncode == 0, r.stderr
    work = f"{home}/runners/new-1/_work"
    assert f"tmpfs {work} tmpfs size=1024m" in (etc / "fstab").read_text()
    e = (home / "runners/new-1/.env").read_text().split("\n")
    assert f"GIT_RUNNER_WORK={work}" in e
    assert f"ACTIONS_RUNNER_HOOK_JOB_COMPLETED={home}/slot-done.sh" in e
    # the workspace is mounted before the service starts, so the runner never holds a job while it is being changed
    order = [ln for ln in (tmp_path / "mount.log").read_text().splitlines() if ln.startswith(("svc ", f"mount {work}"))]
    assert order == ["svc install", f"mount {work}", "svc start"]
    assert "stop actions.runner.o-r.new-1.service" not in (tmp_path / "systemctl.log").read_text()


def test_a_failure_part_way_restarts_the_runners_it_stopped(tmp_path):
    env, home, units, etc = setup(tmp_path)
    bindir = tmp_path / "bin"
    (bindir / "mount").write_text("#!/bin/sh\nexit 1\n")  # the mount fails after the runners were drained
    sc = (bindir / "systemctl").read_text()  # stop and start now change what is-active reports
    sc = sc.replace(
        "is-active) echo active ;;", 'is-active) [ -e "$STUB_DIR/stopped.$2" ] && echo inactive || echo active ;;'
    )
    hooks = 'case "$1" in\n  stop) touch "$STUB_DIR/stopped.$2" ;;\n  start) rm -f "$STUB_DIR/stopped.$2" ;;'
    (bindir / "systemctl").write_text(sc.replace('case "$1" in', hooks, 1))
    r = run(env, "ramdisk", "on", "1024")
    assert r.returncode != 0
    log = (tmp_path / "systemctl.log").read_text().splitlines()
    for unit in ("actions.runner.o-r.ci-1.service", "actions.runner.o-r.ci-2.service"):
        assert f"stop {unit}" in log and f"start {unit}" in log
    assert log.index("stop actions.runner.o-r.ci-1.service") < log.index("start actions.runner.o-r.ci-1.service")
    assert not (home / "maintenance").exists()


def test_runners_that_were_paused_stay_stopped_through_a_change(tmp_path):
    env, home, units, etc = setup(tmp_path)
    bindir = tmp_path / "bin"
    sc = (bindir / "systemctl").read_text()
    sc = sc.replace("is-active) echo active ;;", "is-active) echo inactive ;;")  # ci off: every runner is stopped
    (bindir / "systemctl").write_text(sc)
    (home / "ci-off").write_text("")
    for action in (("on", "1024"), ("off",)):
        (tmp_path / "systemctl.log").write_text("")
        assert run(env, "ramdisk", *action).returncode == 0
        log = (tmp_path / "systemctl.log").read_text().splitlines()
        assert not [ln for ln in log if ln.startswith(("start ", "restart ", "stop "))]


def test_a_cancelled_change_clears_the_marker_and_restarts_what_it_stopped(tmp_path):
    env, home, units, etc = setup(tmp_path)
    (tmp_path / "busy").write_text("")
    p = subprocess.Popen(
        ["bash", str(LINUXRUNNER), "ramdisk", "on", "1024"],
        env={**env, "RAMDISK_POLL": "0.2"},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
    )
    time.sleep(1)
    assert (home / "maintenance").exists()
    p.terminate()  # what a cancelled workflow sends
    p.wait(timeout=10)
    assert not (home / "maintenance").exists()
    assert tmpfs_lines(etc) == []


def test_the_default_wait_fits_inside_the_admin_workflows_timeout():
    text = LINUXRUNNER.read_text()
    assert "RAMDISK_WAIT:-900" in text and "RAMDISK_WAIT:-1800" not in text
    assert "timeout-minutes: 20" in (ROOT / ".github/workflows/admin-wsl.yml").read_text()


def sent_to_workflow(tmp_path, *args):
    """Runs `runner ramdisk ...` against a fake gh and returns the `-f key=value` fields it dispatched."""
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
    assert r.returncode == 0, r.stderr
    dispatch = next(ln for ln in log.read_text().splitlines() if ln.startswith("workflow run"))
    return dispatch


def test_wsl_ramdisk_without_a_size_leaves_it_to_linuxrunner(tmp_path):
    dispatch = sent_to_workflow(tmp_path, "wsl-1", "on")
    assert "admin-wsl.yml" in dispatch and "action=ramdisk-on" in dispatch
    assert (
        "slots=" not in dispatch and "size=" not in dispatch
    )  # no fixed per-runner size that could exhaust the distro's RAM


def test_wsl_ramdisk_with_a_size_sends_it_and_a_mac_keeps_its_four_gb_default(tmp_path):
    assert "slots=2" in sent_to_workflow(tmp_path, "wsl-1", "on", "2")
    assert "size=4" in sent_to_workflow(tmp_path, "air-1", "on")
