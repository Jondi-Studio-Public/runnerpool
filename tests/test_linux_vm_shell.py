"""`gitrunner linux-vm` (mac/gitrunner) run on Linux against stubbed macOS and Colima tools.

Nothing here boots a VM: sudo, ssh, colima, launchctl, pmset, sysctl and curl are stand-ins that log
what they were asked, so these tests check the arguments, the scripts sent into the VM, the generated
launchd plist and the power rules, not Colima itself.
"""

import hashlib
import io
import json
import os
import plistlib
import subprocess
import tarfile
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
GITRUNNER = ROOT / "mac" / "gitrunner"
LINUXRUNNER = ROOT / "linux" / "linuxrunner"

FAKE_COLIMA = """#!/bin/bash
echo "colima $*" >> "$STUB_DIR/colima.log"
case "$1" in
  status) [ -e "$STUB_DIR/running" ]; exit $? ;;
  start) touch "$STUB_DIR/running" ;;
  stop) rm -f "$STUB_DIR/running" ;;
  ssh-config) printf 'Host lima-colima-gitrunner\\n  HostName 127.0.0.1\\n' ;;
esac
exit 0
"""

STUBS = {
    "id": '#!/bin/sh\ncase "$1" in -u) echo 0 ;; _*) exit 0 ;; *) /usr/bin/id "$@" ;; esac\n',
    "sudo": '#!/bin/sh\n[ "$1" = -u ] && shift 2\nexec "$@"\n',
    "uname": '#!/bin/sh\n[ "$1" = -m ] && echo "${STUB_ARCH:-arm64}" || /usr/bin/uname "$@"\n',
    "sysctl": '#!/bin/sh\ncase "$2" in hw.ncpu) echo 8 ;; hw.memsize) echo 17179869184 ;; esac\n',
    "chown": "#!/bin/sh\nexit 0\n",
    # One loaded/not-loaded flag per launchd label, named after the last argument (a label or a plist path).
    "launchctl": """#!/bin/sh
echo "launchctl $*" >> "$STUB_DIR/launchctl.log"
for last; do :; done
key=${last##*/}; key=${key%.plist}
case "$1" in
  bootstrap) touch "$STUB_DIR/loaded-$key"; case "$key" in *linux-vm) touch "$STUB_DIR/running" ;; esac ;;
  bootout) rm -f "$STUB_DIR/loaded-$key" ;;
  print) [ -e "$STUB_DIR/loaded-$key" ] ;;
esac
""",
    "stat": "#!/bin/sh\necho _cirunner\n",
    "pgrep": '#!/bin/sh\ncase "$*" in *Runner.Worker*) [ -e "$STUB_DIR/native_busy" ] ;; *) exit 1 ;; esac\n',
    "pmset": """#!/bin/sh
case "$*" in
  "-g batt") [ -e "$STUB_DIR/battery" ] && echo "Now drawing from 'Battery Power'" || echo "Now drawing from 'AC Power'" ;;
  *) echo "pmset $*" >> "$STUB_DIR/pmset.log" ;;
esac
""",
    # The power loop's `sleep 30` fails, so it ends after one pass; the VM loop's `sleep 4` sends its parent
    # SIGTERM, as launchd does when the daemon is stopped. `on` waits with `sleep 5` for the VM: by then the
    # daemon would have started it.
    "sleep": '#!/bin/sh\n[ "$1" = 5 ] && { touch "$STUB_DIR/running"; exit 0; }\n[ "$1" = 4 ] && { kill -TERM "$PPID"; exit 0; }\nexit 1\n',
    "ssh": """#!/bin/bash
script=$(cat)
{ echo "---- ssh $*"; printf '%s\\n' "$script"; } >> "$STUB_DIR/ssh.log"
if [ -e "$STUB_DIR/ssh_fail" ]; then exit 255; fi
if [ -e "$STUB_DIR/unprovisioned" ] && [[ "$script" == *"test -e /opt/git-runner/provisioned"* ]]; then exit 1; fi
if [ -e "$STUB_DIR/prov_fail" ] && [[ "$script" == *"apt-get install"* ]]; then exit 1; fi
if [[ "$script" == *"Runner.Worker"* ]]; then
  n=$(cat "$STUB_DIR/busy_count" 2>/dev/null || echo 0)
  if [ -e "$STUB_DIR/busy" ]; then echo busy
  elif [ "$n" -gt 0 ]; then echo $((n - 1)) > "$STUB_DIR/busy_count"; echo busy
  elif [ -e "$STUB_DIR/probe_unknown" ]; then exit 255
  else echo idle; fi
fi
exit 0
""",
    # Answers the GitHub API calls gitrunner makes with curl, and serves release downloads from $STUB_DIR/serve.
    "curl": """#!/bin/bash
out=""; url=""
while [ $# -gt 0 ]; do
  case "$1" in -o) out=$2; shift 2 ;; -H|-m|-X|--data) shift 2 ;; -*) shift ;; *) url=$1; shift ;; esac
done
echo "curl $url" >> "$STUB_DIR/curl.log"
case "$url" in
  *registration-token) echo '{"token": "REGTOKEN"}' ;;
  *remove-token) echo '{"token": "RMTOKEN"}' ;;
  *actions/runners?per_page*) n=$(cat "$STUB_DIR/gh_runners" 2>/dev/null)
    echo "{\\"total_count\\": 1, \\"runners\\": [$n]}" ;;
  https://github.com/*/releases/download/*) f="$STUB_DIR/serve/${url##*/}"; [ -f "$f" ] || exit 22; cp "$f" "$out" ;;
  *) exit 22 ;;
esac
""",
}


class Box:
    def __init__(self, tmp_path):
        self.t = tmp_path
        self.bin = tmp_path / "bin"
        self.bin.mkdir()
        for name, body in STUBS.items():
            (self.bin / name).write_text(body)
            (self.bin / name).chmod(0o755)
        self.home = tmp_path / "home"
        self.vm = self.home / "vm"
        (self.vm / "files").mkdir(parents=True)
        (self.vm / "bin").mkdir()
        (self.vm / "lima/bin").mkdir(parents=True)
        (tmp_path / "vmhome").mkdir()
        (tmp_path / "launchd").mkdir()
        (tmp_path / "serve").mkdir()
        (tmp_path / "slot").mkdir()
        (self.home / "runners/air-1").mkdir(parents=True)
        (self.home / "host").write_text("air-1\n")
        (self.home / "github-auth.header").write_text("Authorization: Bearer x\n")
        (self.vm / "files/linuxrunner").write_bytes(LINUXRUNNER.read_bytes())
        (self.vm / "files/linux-provision.sh").write_bytes((ROOT / "linux/linux-provision.sh").read_bytes())
        (self.vm / "ssh.config").write_text("Host lima-colima-gitrunner\n")
        (self.vm / "files/slot-hook.sh").write_bytes((ROOT / "linux/slot-hook.sh").read_bytes())
        self.env = {
            **os.environ,
            "PATH": f"{self.bin}:{os.environ['PATH']}",
            "GITRUNNER_HOME": str(self.home),
            "GITRUNNER_LOGS": str(tmp_path / "logs"),
            "GITRUNNER_VM_HOME": str(tmp_path / "vmhome"),
            "GITRUNNER_LAUNCHD_DIR": str(tmp_path / "launchd"),
            "GITRUNNER_SSH": str(self.bin / "ssh"),
            "GITRUNNER_SLOT_DIR": str(tmp_path / "slot"),
            "STUB_DIR": str(tmp_path),
        }

    def preinstall_tools(self):
        """Skip the download: Colima and Lima are 'already installed' at the pinned versions."""
        (self.vm / "bin/colima").write_text(FAKE_COLIMA)
        (self.vm / "bin/colima").chmod(0o755)
        (self.vm / "lima/bin/limactl").write_text("#!/bin/sh\necho limactl\n")
        (self.vm / "lima/bin/limactl").chmod(0o755)
        (self.vm / "versions").write_text("colima=0.9.1 lima=1.2.1\n")

    def serve_release(self, good=True):
        colima = FAKE_COLIMA.encode()
        (self.t / "serve/colima-Darwin-arm64").write_bytes(colima)
        digest = hashlib.sha256(colima).hexdigest() if good else "0" * 64
        (self.t / "serve/colima-Darwin-arm64.sha256sum").write_text(f"{digest}  colima-Darwin-arm64\n")
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            body = b"#!/bin/sh\necho limactl\n"
            info = tarfile.TarInfo("bin/limactl")
            info.size, info.mode = len(body), 0o755
            tar.addfile(info, io.BytesIO(body))
        lima = buf.getvalue()
        name = "lima-1.2.1-Darwin-arm64.tar.gz"
        (self.t / f"serve/{name}").write_bytes(lima)
        (self.t / "serve/SHA256SUMS").write_text(f"{hashlib.sha256(lima).hexdigest()}  {name}\n")

    def run(self, *args, **env):
        return subprocess.run(
            ["bash", str(GITRUNNER), *args],
            env={**self.env, **env},
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=60,
        )

    def flag(self, name, on=True):
        p = self.t / name
        p.touch() if on else p.unlink(missing_ok=True)

    def log(self, name):
        p = self.t / name
        return p.read_text() if p.exists() else ""

    def ssh_scripts(self):
        return self.log("ssh.log")


@pytest.fixture
def box(tmp_path):
    return Box(tmp_path)


def test_on_registers_one_ci_runner_and_the_admin_runner(box):
    box.preinstall_tools()
    box.flag("unprovisioned")
    r = box.run("linux-vm", "on")
    assert r.returncode == 0, r.stdout + r.stderr
    s = box.ssh_scripts()
    # the shared provisioning script ran, and linuxrunner was installed and named air-1-vm
    assert "apt-get install" in s and "bash /tmp/linuxrunner bootstrap air-1-vm" in s
    # token-bearing commands travel on stdin, never on the ssh command line
    assert "REGTOKEN" in s
    assert not any("REGTOKEN" in line for line in s.splitlines() if line.startswith("---- ssh"))
    assert "add-runner 'example-org' 'air-1-vm-1' 'linux-ci' 'REGTOKEN' ci" in s
    assert "air-1-vm-2" not in s  # one VM runner per Mac: it takes turns with the native one
    assert "add-runner 'example-org/runnerpool' 'air-1-vm-admin' 'linux-admin,air-1-vm' 'REGTOKEN' root" in s
    assert sorted(p.name for p in (box.vm / "conf").iterdir()) == ["air-1-vm-1", "air-1-vm-admin"]
    assert (box.vm / "enabled").exists()
    # defaults on a 16 GB, 8-core Mac
    assert (box.vm / "settings").read_text() == "CPUS=4\nMEMORY=4\nDISK=40\nADMIN=1\n"
    started = box.log("colima.log")
    assert "start" not in started  # the launchd daemon starts the VM, not `on`
    assert "bootstrap system" in box.log("launchctl.log")


def test_on_without_files_or_token_or_apple_silicon_fails_early(box):
    box.preinstall_tools()
    (box.vm / "files/linuxrunner").unlink()
    r = box.run("linux-vm", "on")
    assert r.returncode != 0 and "runner update air-1" in r.stderr
    (box.vm / "files/linuxrunner").write_bytes(LINUXRUNNER.read_bytes())
    (box.home / "github-auth.header").write_text("")
    assert box.run("linux-vm", "on").returncode != 0
    (box.home / "github-auth.header").write_text("Authorization: Bearer x\n")
    r = box.run("linux-vm", "on", STUB_ARCH="x86_64")
    assert r.returncode != 0 and "Apple Silicon" in r.stderr
    assert box.ssh_scripts() == ""


def test_provisioning_failure_registers_nothing(box):
    box.preinstall_tools()
    box.flag("unprovisioned")
    box.flag("prov_fail")
    r = box.run("linux-vm", "on")
    assert r.returncode != 0 and "provisioning failed" in r.stderr
    assert "add-runner" not in box.ssh_scripts() and not (box.vm / "enabled").exists()
    box.flag("prov_fail", on=False)  # run it again: provisioning is retried and the runners follow
    assert box.run("linux-vm", "on").returncode == 0
    assert "add-runner" in box.ssh_scripts()


def test_on_rejects_bad_arguments_before_touching_anything(box):
    box.preinstall_tools()
    for args in (
        ["abc"],
        ["2"],  # no COUNT any more: one VM runner per Mac
        ["1", "2"],
        ["--cpus"],
        ["--cpus", "99"],
        ["--cpus", "x"],
        ["--memory", "1"],
        ["--memory", "13"],  # a 16 GB Mac keeps 4 GB for macOS
        ["--disk", "5"],
        ["--bogus"],
    ):
        r = box.run("linux-vm", "on", *args)
        assert r.returncode != 0, args
    assert not (box.vm / "settings").exists()
    assert box.ssh_scripts() == "" and box.log("launchctl.log") == ""
    assert box.run("linux-vm", "nonsense").returncode != 0
    assert box.run("linux-vm").returncode != 0
    assert box.run("linux-vm", "off", "--bogus").returncode != 0


def test_settings_are_kept_and_overridable(box):
    box.preinstall_tools()
    r = box.run("linux-vm", "on", "--cpus", "2", "--memory", "6", "--disk", "60", "--no-admin")
    assert r.returncode == 0, r.stderr
    assert (box.vm / "settings").read_text() == "CPUS=2\nMEMORY=6\nDISK=60\nADMIN=0\n"
    assert "air-1-vm-admin" not in box.ssh_scripts()
    # running `on` again with no options keeps them
    box.run("linux-vm", "on")
    assert (box.vm / "settings").read_text() == "CPUS=2\nMEMORY=6\nDISK=60\nADMIN=0\n"


def test_resizing_a_running_vm_waits_for_an_idle_vm(box):
    box.preinstall_tools()
    assert box.run("linux-vm", "on").returncode == 0
    box.flag("busy")
    r = box.run("linux-vm", "on", "--cpus", "2")
    assert r.returncode != 0 and "job is running" in r.stderr
    box.flag("busy", on=False)
    r = box.run("linux-vm", "on", "--cpus", "2")
    assert r.returncode == 0, r.stderr
    assert "colima stop" in box.log("colima.log")


def test_on_battery_the_vm_runners_are_turned_off_then_back_on_with_ac(box):
    box.preinstall_tools()
    (box.home / "pause-on-battery").touch()
    box.flag("battery")
    assert box.run("linux-vm", "on").returncode == 0
    assert (box.vm / "applied-ci").read_text().strip() == "off"
    assert box.ssh_scripts().count("linuxrunner ci off") == 1
    # unchanged: no second call
    assert box.run("linux-vm", "sync").returncode == 0
    assert box.ssh_scripts().count("linuxrunner ci off") == 1
    # plugged in
    box.flag("battery", on=False)
    assert box.run("linux-vm", "sync").returncode == 0
    assert (box.vm / "applied-ci").read_text().strip() == "on"
    assert box.ssh_scripts().count("linuxrunner ci on") == 1
    # `gitrunner ci off` pauses the VM too
    (box.home / "ci-off").touch()
    assert box.run("linux-vm", "sync").returncode == 0
    assert (box.vm / "applied-ci").read_text().strip() == "off"
    # battery without the pause-on-battery rule does not pause
    (box.home / "ci-off").unlink()
    (box.home / "pause-on-battery").unlink()
    box.flag("battery")
    box.run("linux-vm", "sync")
    assert (box.vm / "applied-ci").read_text().strip() == "on"


def test_a_failed_sync_is_retried_and_a_stopped_vm_is_left_alone(box):
    box.preinstall_tools()
    assert box.run("linux-vm", "on").returncode == 0
    (box.vm / "applied-ci").unlink(missing_ok=True)
    box.flag("running", on=False)
    before = box.ssh_scripts()
    assert box.run("linux-vm", "sync").returncode == 0
    assert box.ssh_scripts() == before  # nothing sent to a VM that is not running
    box.flag("running")
    box.flag("ssh_fail")
    assert box.run("linux-vm", "sync").returncode != 0
    assert not (box.vm / "applied-ci").exists()  # not recorded: the next pass tries again
    box.flag("ssh_fail", on=False)
    assert box.run("linux-vm", "sync").returncode == 0
    assert (box.vm / "applied-ci").read_text().strip() == "on"


def test_power_watch_follows_the_battery_through_a_background_sync(box):
    box.preinstall_tools()
    (box.home / "pause-on-battery").touch()
    assert box.run("linux-vm", "on").returncode == 0  # AC: CI on
    (box.vm / "applied-ci").write_text("on\n")
    box.flag("battery")
    r = box.run("power-watch")  # the stubbed sleep ends the loop after one pass
    assert r.returncode != 0
    for _ in range(50):
        if (box.vm / "applied-ci").read_text().strip() == "off":
            break
        time.sleep(0.1)
    assert (box.vm / "applied-ci").read_text().strip() == "off"
    assert box.ssh_scripts().count("linuxrunner ci off") >= 1


def test_heal_registers_a_vm_runner_github_dropped(box):
    box.preinstall_tools()
    assert box.run("linux-vm", "on").returncode == 0
    (box.t / "gh_runners").write_text('{"name": "air-1-vm-admin"}')  # air-1-vm-1 is gone
    box.run("power-watch")
    for _ in range(50):
        if box.ssh_scripts().count("add-runner 'example-org' 'air-1-vm-1'") >= 2:
            break
        time.sleep(0.1)
    s = box.ssh_scripts()
    assert s.count("add-runner 'example-org' 'air-1-vm-1'") == 2  # once by `on`, once by heal
    assert s.count("add-runner 'example-org/runnerpool' 'air-1-vm-admin'") == 1
    assert "./svc.sh uninstall" in s  # the stale local registration is cleared first


def test_plist_is_valid_and_runs_the_daemon_loop(box):
    r = box.run("linux-vm", "plist")
    assert r.returncode == 0, r.stderr
    plist = plistlib.loads(r.stdout.encode())
    assert plist["Label"] == "io.github.git-runner.mac-runners.linux-vm"
    assert plist["ProgramArguments"] == [str(box.home / "gitrunner"), "linux-vm", "run"]
    assert plist["RunAtLoad"] is True and plist["KeepAlive"] is True
    assert plist["ExitTimeOut"] >= 120  # time for colima stop at shutdown
    box.preinstall_tools()
    assert box.run("linux-vm", "on").returncode == 0
    assert plistlib.loads((box.t / "launchd/io.github.git-runner.mac-runners.linux-vm.plist").read_bytes()) == plist


def test_run_loop_starts_a_stopped_vm_with_the_saved_size(box):
    box.preinstall_tools()
    (box.vm / "settings").write_text("CPUS=3\nMEMORY=5\nDISK=30\nADMIN=1\n")
    (box.vm / "applied-ci").write_text("on\n")
    r = box.run("linux-vm", "run")
    assert r.returncode == 0, r.stderr  # SIGTERM from the stubbed sleep: the trap stops the VM and exits
    c = box.log("colima.log")
    assert c.rstrip().endswith("stop -p gitrunner")
    assert (
        f"start -p gitrunner --arch aarch64 --vm-type vz --runtime none --mount-type virtiofs --mount {box.t}/slot:w "
        "--cpu 3 --memory 5 --disk 30" in c
    )
    assert not (box.vm / "applied-ci").exists()  # a fresh boot forgets what CI was told
    assert "ssh-config -p gitrunner" in c


def test_download_is_checked_against_the_release_checksums(box):
    box.serve_release(good=False)
    r = box.run("linux-vm", "on")
    assert r.returncode != 0 and "does not match its sha256" in r.stderr
    assert not (box.vm / "bin/colima").exists()
    box.serve_release(good=True)
    r = box.run("linux-vm", "on")
    assert r.returncode == 0, r.stdout + r.stderr
    assert (box.vm / "bin/colima").exists() and (box.vm / "lima/bin/limactl").exists()
    assert (box.vm / "versions").read_text().strip() == "colima=0.9.1 lima=1.2.1"
    # a pinned hash wins over the release's own checksum file
    (box.vm / "versions").unlink()
    r = box.run("linux-vm", "on", LINUX_VM_COLIMA_SHA256="1" * 64)
    assert r.returncode != 0 and "does not match its sha256" in r.stderr


def test_off_stops_the_daemon_and_the_vm_and_keeps_the_runners(box):
    box.preinstall_tools()
    assert box.run("linux-vm", "on").returncode == 0
    r = box.run("linux-vm", "off")
    assert r.returncode == 0, r.stderr
    assert "linuxrunner ci off" in box.ssh_scripts()
    assert "colima stop -p gitrunner" in box.log("colima.log")
    assert (
        not (box.vm / "enabled").exists()
        and not (box.t / "launchd/io.github.git-runner.mac-runners.linux-vm.plist").exists()
    )
    assert (box.vm / "conf/air-1-vm-1").exists()
    # off means the power watch no longer touches it
    before = box.ssh_scripts()
    box.run("linux-vm", "sync")
    assert box.ssh_scripts() == before


def test_off_purge_removes_runners_and_files(box):
    box.preinstall_tools()
    assert box.run("linux-vm", "on").returncode == 0
    r = box.run("linux-vm", "off", "--purge")
    assert r.returncode == 0, r.stderr
    s = box.ssh_scripts()
    assert "remove-runner 'air-1-vm-1' 'RMTOKEN'" in s and "remove-runner 'air-1-vm-admin' 'RMTOKEN'" in s
    assert "colima delete -p gitrunner" in box.log("colima.log")
    assert not box.vm.exists()


def test_files_keeps_copies_and_updates_a_running_vm(box):
    box.preinstall_tools()
    src = box.t / "repo/linux"
    src.mkdir(parents=True)
    (src / "linuxrunner").write_text("#!/bin/bash\necho new\n")
    (src / "linux-provision.sh").write_text("#!/bin/bash\necho prov\n")
    (src / "slot-hook.sh").write_text("#!/bin/bash\necho hook\n")
    assert box.run("linux-vm", "on").returncode == 0
    r = box.run("linux-vm", "files", str(box.t / "repo"))
    assert r.returncode == 0, r.stderr
    assert "echo new" in (box.vm / "files/linuxrunner").read_text()
    assert "linuxrunner self-update /tmp/linuxrunner" in box.ssh_scripts()
    assert "echo hook" in (box.home / "hooks/slot-hook.sh").read_text()  # native copy, and the two names:
    assert (box.home / "hooks/job-started.sh").resolve() == (box.home / "hooks/slot-hook.sh").resolve()
    (src / "linuxrunner").write_text("if then\n")
    assert box.run("linux-vm", "files", str(box.t / "repo")).returncode != 0
    assert box.run("linux-vm", "files", str(box.t / "nowhere")).returncode != 0


def test_status_and_info_show_the_vm(box):
    box.preinstall_tools()
    assert "linux-vm: off" in box.run("linux-vm", "status").stdout
    assert box.run("linux-vm", "on").returncode == 0
    out = box.run("linux-vm", "status").stdout
    assert "linux-vm: running, 4 CPUs, 4 GB" in out and "CI on" in out
    assert "linuxrunner status" in box.ssh_scripts()


def test_linuxrunner_downloads_the_runner_for_its_cpu(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    stubs = {
        "id": '#!/bin/sh\n[ "$1" = -u ] && echo 0 || /usr/bin/id "$@"\n',
        "uname": '#!/bin/sh\n[ "$1" = -m ] && echo "$STUB_ARCH" || /usr/bin/uname "$@"\n',
        "curl": '#!/bin/sh\necho "curl $*" >> "$STUB_LOG"\ncase "$*" in *-w*) echo https://github.com/actions/runner/releases/tag/v2.9.9 ;; esac\n',
        "tar": '#!/bin/sh\ncat > /dev/null\nwhile [ $# -gt 0 ]; do [ "$1" = -C ] && d=$2; shift; done\nprintf "#!/bin/sh\\nexit 0\\n" > "$d/svc.sh"; chmod +x "$d/svc.sh"\n',
        "chown": "#!/bin/sh\nexit 0\n",
        "sudo": "#!/bin/sh\nexit 0\n",
    }
    for name, body in stubs.items():
        (bindir / name).write_text(body)
        (bindir / name).chmod(0o755)
    for arch, want in (("aarch64", "arm64"), ("x86_64", "x64")):
        log = tmp_path / f"{arch}.log"
        home = tmp_path / f"home-{arch}"
        env = {
            **os.environ,
            "PATH": f"{bindir}:{os.environ['PATH']}",
            "GIT_RUNNER_HOME": str(home),
            "CI_USER": "root",
            "STUB_ARCH": arch,
            "STUB_LOG": str(log),
        }
        r = subprocess.run(
            ["bash", str(LINUXRUNNER), "add-runner", "example-org", "air-1-vm-1", "linux-ci", "TOK", "ci"],
            env=env,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
        )
        assert r.returncode == 0, r.stderr
        assert f"actions-runner-linux-{want}-2.9.9.tar.gz" in log.read_text()


def test_install_docker_is_for_x86_64_only_and_installs_the_engine_packages(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "log"
    # Every command that would touch the box just logs; `systemctl is-system-running` says systemd is up.
    stubs = {
        "id": '#!/bin/sh\n[ "$1" = -u ] && echo 0 || echo "id $*" >> "$STUB_LOG"\n',
        "uname": '#!/bin/sh\n[ "$1" = -m ] && echo "$STUB_ARCH" || /usr/bin/uname "$@"\n',
        "systemctl": '#!/bin/sh\necho "systemctl $*" >> "$STUB_LOG"\n[ "$1" = is-system-running ] && echo running\nexit 0\n',
        "docker": '#!/bin/sh\necho "docker $*" >> "$STUB_LOG"\necho "Docker version 27.0.0, build abc"\n',
    }
    for cmd in ("apt-get", "curl", "chmod", "usermod"):
        stubs[cmd] = f'#!/bin/sh\necho "{cmd} $*" >> "$STUB_LOG"\n'
    for name, body in stubs.items():
        (bindir / name).write_text(body)
        (bindir / name).chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{bindir}:{os.environ['PATH']}",
        "GIT_RUNNER_HOME": str(tmp_path / "home"),
        "SYSTEMD_DIR": str(tmp_path / "systemd"),
        "ETC_DIR": str(tmp_path / "etc"),
        "CI_USER": "runner",
        "STUB_LOG": str(log),
    }
    (tmp_path / "etc/docker").mkdir(parents=True)
    (tmp_path / "etc/docker/daemon.json").write_text('{"log-driver": "local"}')
    (tmp_path / "home").mkdir()
    (tmp_path / "home/slots").write_text("slots=3\nthreads=6\n")

    def run(arch):
        return subprocess.run(
            ["bash", str(LINUXRUNNER), "install-docker"],
            env={**env, "STUB_ARCH": arch},
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
        )

    r = run("aarch64")
    assert r.returncode != 0 and "x86-64" in r.stderr
    assert not log.exists()
    r = run("x86_64")
    assert r.returncode == 0, r.stderr
    out = log.read_text()
    assert "docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin" in out
    assert "systemctl enable docker.service containerd.service" in out
    assert "usermod -aG docker runner" in out
    # Docker sits under one slot's caps: 6 threads, and RAM / 3 slots.
    mem_mb = int(
        next(
            ln
            for ln in subprocess.run(["free", "-m"], capture_output=True, text=True).stdout.splitlines()
            if ln.startswith("Mem:")
        ).split()[1]
    )
    slice_conf = (tmp_path / "systemd/docker-ci.slice").read_text()
    assert "CPUQuota=600%" in slice_conf and f"MemoryMax={mem_mb // 3}M" in slice_conf
    for unit in ("docker", "containerd"):
        assert "Slice=docker-ci.slice" in (tmp_path / f"systemd/{unit}.service.d/slice.conf").read_text()
    daemon = json.loads((tmp_path / "etc/docker/daemon.json").read_text())
    assert daemon == {"log-driver": "local", "cgroup-parent": "docker-ci.slice"}  # merged, not replaced
    assert "systemctl restart containerd.service docker.service" in out
    # a later `slots` change rewrites the slice
    subprocess.run(
        ["bash", str(LINUXRUNNER), "slots", "off"], env={**env, "STUB_ARCH": "x86_64"}, check=True, capture_output=True
    )
    assert "CPUQuota" not in (tmp_path / "systemd/docker-ci.slice").read_text()
    # slots off: the device-wide cores cap applies to Docker too, and `cores all` lifts it
    subprocess.run(
        ["bash", str(LINUXRUNNER), "cores", "4"], env={**env, "STUB_ARCH": "x86_64"}, check=True, capture_output=True
    )
    assert "CPUQuota=400%" in (tmp_path / "systemd/docker-ci.slice").read_text()
    subprocess.run(
        ["bash", str(LINUXRUNNER), "cores", "all"], env={**env, "STUB_ARCH": "x86_64"}, check=True, capture_output=True
    )
    assert "CPUQuota" not in (tmp_path / "systemd/docker-ci.slice").read_text()


# --- one job at a time: the slot hook, and the native runner and the VM taking turns ------------------


def hook(tmp_path, name, owner, slot=None, **env):
    """Run linux/slot-hook.sh as the runner would: through a link named job-started.sh / job-completed.sh."""
    link = tmp_path / name
    if not link.exists():
        link.symlink_to(ROOT / "linux/slot-hook.sh")
    return subprocess.run(
        ["bash", str(link)],
        env={
            **os.environ,
            "GITRUNNER_SLOT_DIR": str(slot or tmp_path / "slot"),
            "SLOT_OWNER": owner,
            "SLOT_POLL": "0.1",
            **env,
        },
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        timeout=30,
    )


def test_hook_acquires_and_releases_the_slot(tmp_path):
    (tmp_path / "slot").mkdir()
    r = hook(tmp_path, "job-started.sh", "air-1")
    assert r.returncode == 0, r.stderr
    owner = (tmp_path / "slot/slot-1/owner").read_text()
    assert owner.startswith("runner=air-1\n") and "pid=" in owner and "time=" in owner and "side=" in owner
    # someone else's completed hook does not free it
    assert hook(tmp_path, "job-completed.sh", "air-1-vm-1").returncode == 0
    assert (tmp_path / "slot/slot-1").exists()
    assert hook(tmp_path, "job-completed.sh", "air-1").returncode == 0
    assert not (tmp_path / "slot/slot-1").exists()
    assert hook(tmp_path, "job-completed.sh", "air-1").returncode == 0  # nothing held: fine


def test_second_job_waits_for_the_slot_instead_of_running_beside_the_first(tmp_path):
    (tmp_path / "slot").mkdir()
    assert hook(tmp_path, "job-started.sh", "air-1").returncode == 0
    waiter = subprocess.Popen(
        ["bash", str(tmp_path / "job-started.sh")],
        env={
            **os.environ,
            "GITRUNNER_SLOT_DIR": str(tmp_path / "slot"),
            "SLOT_OWNER": "air-1-vm-1",
            "SLOT_POLL": "0.1",
        },
        stdout=subprocess.PIPE,
        text=True,
    )
    time.sleep(1)
    assert waiter.poll() is None  # still waiting: delayed, never concurrent
    assert "runner=air-1\n" in (tmp_path / "slot/slot-1/owner").read_text()
    assert hook(tmp_path, "job-completed.sh", "air-1").returncode == 0
    out, _ = waiter.communicate(timeout=10)
    assert waiter.returncode == 0
    assert "waiting for the Mac's job slot (1 of 1 in use)" in out and "got the Mac's job slot 1 of 1" in out
    assert "runner=air-1-vm-1\n" in (tmp_path / "slot/slot-1/owner").read_text()


def test_simultaneous_jobs_only_one_gets_the_slot(tmp_path):
    (tmp_path / "slot").mkdir()
    (tmp_path / "job-started.sh").symlink_to(ROOT / "linux/slot-hook.sh")
    procs = [
        subprocess.Popen(
            ["bash", str(tmp_path / "job-started.sh")],
            env={**os.environ, "GITRUNNER_SLOT_DIR": str(tmp_path / "slot"), "SLOT_OWNER": n, "SLOT_POLL": "0.1"},
            stdout=subprocess.PIPE,
            text=True,
        )
        for n in ("air-1", "air-1-vm-1")
    ]
    time.sleep(1.5)
    done = [p for p in procs if p.poll() is not None]
    assert len(done) == 1
    for p in procs:
        p.kill()
        p.communicate()


def test_hook_gives_up_after_the_wait_limit_and_ignores_a_missing_slot_folder(tmp_path):
    (tmp_path / "slot").mkdir()
    hook(tmp_path, "job-started.sh", "air-1")
    r = hook(tmp_path, "job-started.sh", "air-1-vm-1", SLOT_MAX_WAIT="1")
    assert r.returncode != 0 and "giving up" in r.stdout
    r = hook(tmp_path, "job-started.sh", "x", slot=tmp_path / "nowhere")  # no mount: do not gate (and do not fail)
    assert r.returncode == 0 and "not gating" in r.stdout


def test_a_lock_left_by_the_same_runner_is_taken_over(tmp_path):
    (tmp_path / "slot").mkdir()
    hook(tmp_path, "job-started.sh", "air-1")  # its completed hook never ran (crash)
    r = hook(tmp_path, "job-started.sh", "air-1")
    assert r.returncode == 0 and "got the Mac's job slot" in r.stdout
    assert len(list((tmp_path / "slot").glob("slot-*"))) == 1


def write_lock(box, who, age=60, num=1):
    lock = box.t / f"slot/slot-{num}"
    lock.mkdir(exist_ok=True)
    (lock / "owner").write_text(
        f"runner={who}\nside=mac\njob=1/a\npid=1\ntime={int(time.time()) - age}\nblock={num - 1}\n"
    )
    return lock


def test_watcher_clears_a_lock_whose_holder_has_no_job(box):
    box.preinstall_tools()
    assert box.run("linux-vm", "on").returncode == 0
    lock = write_lock(box, "air-1")  # native holder, no Runner.Worker
    assert box.run("linux-vm", "turns").returncode == 0
    assert not lock.exists()
    lock = write_lock(box, "air-1")
    box.flag("native_busy")  # it is in its job: the lock stands
    box.run("linux-vm", "turns")
    assert lock.exists()
    box.flag("native_busy", on=False)
    lock = write_lock(box, "air-1", age=5)  # just taken: leave it alone
    box.run("linux-vm", "turns")
    assert lock.exists()
    # the VM runner as holder: cleared only when the VM says idle, not when busy or unreachable
    lock = write_lock(box, "air-1-vm-1")
    box.flag("busy")
    box.run("linux-vm", "turns")
    assert lock.exists()
    box.flag("busy", on=False)
    box.flag("probe_unknown")
    box.run("linux-vm", "turns")
    assert lock.exists()
    box.flag("probe_unknown", on=False)
    box.run("linux-vm", "turns")
    assert not lock.exists()


def test_watcher_clears_a_bare_lock_only_after_a_grace_period(box):
    box.preinstall_tools()
    assert box.run("linux-vm", "on").returncode == 0
    lock = box.t / "slot/slot-1"
    lock.mkdir()
    box.run("linux-vm", "turns")
    assert lock.exists() and (box.vm / "slot-seen-1").exists()
    (box.vm / "slot-seen-1").write_text(f"{int(time.time()) - 60}\n")
    box.run("linux-vm", "turns")
    assert not lock.exists()


def loaded(box):
    return (box.t / "loaded-io.github.git-runner.mac-runners.runner.air-1").exists()


def test_native_runner_yields_while_the_vm_is_in_a_job_and_returns_after(box):
    box.preinstall_tools()
    assert box.run("linux-vm", "on").returncode == 0
    box.run("linux-vm", "turns")  # idle VM: the native runner is (re)started
    assert loaded(box)
    box.flag("busy")
    assert box.run("linux-vm", "turns").returncode == 0
    assert not loaded(box) and (box.vm / "vm-busy").exists()
    box.run("linux-vm", "turns")
    assert not loaded(box)  # stays stopped while the VM is in its job
    box.flag("busy", on=False)
    box.run("linux-vm", "turns")
    assert loaded(box) and not (box.vm / "vm-busy").exists()


def test_native_runner_in_a_job_is_never_stopped(box):
    box.preinstall_tools()
    assert box.run("linux-vm", "on").returncode == 0
    box.run("linux-vm", "turns")
    box.flag("native_busy")
    box.flag("busy")  # both took a job: neither is cut short
    box.run("linux-vm", "turns")
    assert loaded(box)
    assert "bootout system/io.github.git-runner.mac-runners.runner.air-1" not in box.log("launchctl.log")


def test_a_stale_busy_flag_expires_and_an_unreachable_vm_changes_nothing(box):
    box.preinstall_tools()
    assert box.run("linux-vm", "on").returncode == 0
    (box.vm / "vm-busy").write_text(f"{int(time.time()) - 120}\n")  # the loop died long ago
    box.flag("probe_unknown")
    box.run("linux-vm", "turns")
    assert loaded(box)
    (box.vm / "vm-busy").write_text(f"{int(time.time())}\n")  # seen busy a moment ago, now unreachable: keep yielding
    box.run("linux-vm", "turns")
    assert not loaded(box)


def test_battery_wins_over_the_turns(box):
    box.preinstall_tools()
    (box.home / "pause-on-battery").touch()
    box.flag("battery")
    assert box.run("linux-vm", "on").returncode == 0
    box.run("linux-vm", "turns")
    assert not loaded(box)  # idle VM, but on battery: the native runner is not started
    # and the power watch does not start it while the VM is busy either
    box.flag("battery", on=False)
    box.flag("busy")
    box.run("linux-vm", "turns")
    box.run("power-watch")
    assert not loaded(box)
    box.flag("busy", on=False)
    box.run("linux-vm", "turns")
    box.run("power-watch")
    assert loaded(box)


def test_vm_ci_is_off_while_the_native_runner_is_in_a_job(box):
    box.preinstall_tools()
    assert box.run("linux-vm", "on").returncode == 0
    box.run("linux-vm", "sync")
    assert (box.vm / "applied-ci").read_text().strip() == "on"
    box.flag("native_busy")
    assert box.run("linux-vm", "sync").returncode == 0
    assert (box.vm / "applied-ci").read_text().strip() == "off"
    # the race: the VM is in a job as well. Its CI stays on, so the job is not cut off
    box.flag("native_busy")
    (box.vm / "applied-ci").write_text("on\n")
    (box.vm / "vm-busy").write_text(f"{int(time.time())}\n")
    before = box.ssh_scripts().count("linuxrunner ci off")
    box.run("linux-vm", "sync")
    assert box.ssh_scripts().count("linuxrunner ci off") == before
    # the native job ends: back on
    box.flag("native_busy", on=False)
    (box.vm / "applied-ci").write_text("off\n")
    box.run("linux-vm", "sync")
    assert (box.vm / "applied-ci").read_text().strip() == "on"


def test_turning_vm_ci_off_waits_for_a_running_job_to_finish(box):
    box.preinstall_tools()
    assert box.run("linux-vm", "on").returncode == 0
    box.run("linux-vm", "sync")
    box.flag("battery")
    (box.home / "pause-on-battery").touch()
    (box.t / "busy_count").write_text("3\n")  # busy for three looks, then idle
    r = box.run("linux-vm", "sync")
    assert r.returncode == 0, r.stderr
    s = box.ssh_scripts()
    assert s.count("pgrep -f Runner.Worker") >= 4
    assert s.rindex("linuxrunner ci off") > s.rindex("pgrep -f Runner.Worker")  # off only after the last busy look
    assert "turning CI off when it finishes" in r.stdout
    # AC returns while it waits: nothing is turned off
    (box.vm / "applied-ci").write_text("on\n")
    (box.t / "busy_count").write_text("3\n")
    box.flag("battery", on=False)
    before = box.ssh_scripts().count("linuxrunner ci off")
    box.run("linux-vm", "sync")
    assert box.ssh_scripts().count("linuxrunner ci off") == before


def test_native_service_file_gets_the_hooks_and_the_vm_runner_gets_them_in_its_env(box):
    box.preinstall_tools()
    assert box.run("linux-vm", "on").returncode == 0
    plist = plistlib.loads((box.t / "launchd/io.github.git-runner.mac-runners.runner.air-1.plist").read_bytes())
    env = plist["EnvironmentVariables"]
    assert env["ACTIONS_RUNNER_HOOK_JOB_STARTED"] == str(box.home / "hooks/job-started.sh")
    assert env["ACTIONS_RUNNER_HOOK_JOB_COMPLETED"] == str(box.home / "hooks/job-completed.sh")
    assert env["SLOT_OWNER"] == "air-1" and env["GITRUNNER_SLOT_DIR"] == str(box.t / "slot")
    assert (box.home / "hooks/job-started.sh").resolve() == (box.home / "hooks/slot-hook.sh").resolve()
    assert oct((box.t / "slot").stat().st_mode & 0o7777) == "0o1777"
    s = box.ssh_scripts()
    assert "/opt/git-runner/hooks/slot-hook.sh" in s and "ln -sf slot-hook.sh /opt/git-runner/hooks/job-started.sh" in s
    assert "ACTIONS_RUNNER_HOOK_JOB_STARTED=/opt/git-runner/hooks/job-started.sh" in s and "SLOT_OWNER=air-1-vm-1" in s
    assert f"GITRUNNER_SLOT_DIR={box.t}/slot" in s
    # the hooks go in only for a CI runner: the admin runner's script carries the same guard, false for root
    assert "if [ 'root' = ci ]" in s and "if [ 'ci' = ci ]" in s


def test_off_waits_for_the_vm_job_before_stopping(box):
    box.preinstall_tools()
    assert box.run("linux-vm", "on").returncode == 0
    (box.t / "busy_count").write_text("2\n")
    r = box.run("linux-vm", "off")
    assert r.returncode == 0, r.stderr
    s = box.ssh_scripts()
    assert s.rindex("linuxrunner ci off") > s.rindex("pgrep -f Runner.Worker")
    assert "waiting for it to finish" in r.stdout


# --- N jobs at once on a Mac (`slots N`), changed while jobs run ---------------------------------------------


def set_slots(box, n):
    (box.t / "slot/slots.conf").write_text(f"slots={n}\n")


def hook_popen(tmp_path, name, owner):
    link = tmp_path / name
    if not link.exists():
        link.symlink_to(ROOT / "linux/slot-hook.sh")
    return subprocess.Popen(
        ["bash", str(link)],
        env={**os.environ, "GITRUNNER_SLOT_DIR": str(tmp_path / "slot"), "SLOT_OWNER": owner, "SLOT_POLL": "0.1"},
        stdout=subprocess.PIPE,
        text=True,
        stdin=subprocess.DEVNULL,
    )


def test_two_slots_let_the_native_and_vm_runner_both_run_and_a_third_wait(tmp_path):
    (tmp_path / "slot").mkdir()
    (tmp_path / "slot/slots.conf").write_text("slots=2\n")
    assert hook(tmp_path, "job-started.sh", "air-1").returncode == 0
    assert hook(tmp_path, "job-started.sh", "air-1-vm-1").returncode == 0  # both hold a slot: they run together
    assert sorted(p.name for p in (tmp_path / "slot").glob("slot-*")) == ["slot-1", "slot-2"]
    third = hook_popen(tmp_path, "job-started.sh", "air-1-b")
    time.sleep(0.8)
    assert third.poll() is None
    (tmp_path / "slot/slots.conf").write_text("slots=3\n")  # raised live: it is in at once
    out, _ = third.communicate(timeout=10)
    assert "got the Mac's job slot 3 of 3" in out


def test_lowering_slots_keeps_running_jobs_and_counts_every_folder(tmp_path):
    (tmp_path / "slot").mkdir()
    (tmp_path / "slot/slots.conf").write_text("slots=3\n")
    for who in ("a", "b", "c"):
        assert hook(tmp_path, "job-started.sh", who).returncode == 0
    (tmp_path / "slot/slots.conf").write_text("slots=2\n")
    assert len(list((tmp_path / "slot").glob("slot-*"))) == 3  # nothing is evicted
    waiter = hook_popen(tmp_path, "job-started.sh", "d")
    hook(tmp_path, "job-completed.sh", "c")  # slot-3 frees, but a and b still fill N=2
    time.sleep(0.8)
    assert waiter.poll() is None and not (tmp_path / "slot/slot-3").exists()
    hook(tmp_path, "job-completed.sh", "a")
    out, _ = waiter.communicate(timeout=10)
    assert "got the Mac's job slot 1 of 2" in out


def test_slots_off_means_no_gating_and_a_missing_setting_means_one(tmp_path):
    (tmp_path / "slot").mkdir()
    (tmp_path / "slot/slots.conf").write_text("slots=off\n")
    for who in ("a", "b", "c"):
        r = hook(tmp_path, "job-started.sh", who)
        assert r.returncode == 0 and "not gating" in r.stdout
    assert not list((tmp_path / "slot").glob("slot-*"))
    (tmp_path / "slot/slots.conf").write_text("slots=bogus\n")  # odd file: the safe default
    hook(tmp_path, "job-started.sh", "a")
    assert hook(tmp_path, "job-started.sh", "b", SLOT_MAX_WAIT="1").returncode != 0


def test_an_old_lock_folder_becomes_slot_1_and_keeps_its_holder(tmp_path):
    (tmp_path / "slot/lock").mkdir(parents=True)
    (tmp_path / "slot/lock/owner").write_text(f"runner=air-1 job=1/a pid=1 time={int(time.time())}\n")
    r = hook(tmp_path, "job-started.sh", "air-1-vm-1", SLOT_MAX_WAIT="1")  # N=1: the migrated slot is still held
    assert r.returncode != 0 and not (tmp_path / "slot/lock").exists() and (tmp_path / "slot/slot-1").exists()
    assert hook(tmp_path, "job-completed.sh", "air-1").returncode == 0  # its completed hook finds the renamed slot
    assert not (tmp_path / "slot/slot-1").exists()


def test_the_watcher_migrates_an_old_lock_and_vm_on_writes_the_safe_default(box):
    box.preinstall_tools()
    assert box.run("linux-vm", "on").returncode == 0
    assert (box.t / "slot/slots.conf").read_text().strip() == "slots=1"
    (box.t / "slot/lock").mkdir()
    (box.t / "slot/lock/owner").write_text(f"runner=air-1 job=1/a pid=1 time={int(time.time())}\n")
    box.flag("native_busy")
    box.run("linux-vm", "turns")
    assert not (box.t / "slot/lock").exists() and (box.t / "slot/slot-1").exists()


def test_slots_command_sets_the_count_and_rejects_bad_input(box):
    box.preinstall_tools()
    assert box.run("linux-vm", "on").returncode == 0
    r = box.run("slots", "2", "4")
    assert r.returncode == 0, r.stderr
    assert (box.t / "slot/slots.conf").read_text().split() == ["slots=2", "threads=4"]
    assert box.run("slots", "3").returncode == 0  # threads kept
    assert (box.t / "slot/slots.conf").read_text().split() == ["slots=3", "threads=4"]
    assert box.run("slots", "off").returncode == 0
    assert (box.t / "slot/slots.conf").read_text().split()[0] == "slots=off"
    for args in (["0"], ["17"], ["x"], [], ["2", "0"], ["2", "99"], ["2", "x"], ["2", "3", "4"]):
        assert box.run("slots", *args).returncode != 0, args
    assert (box.t / "slot/slots.conf").read_text().split()[0] == "slots=off"


def test_status_shows_slots_used_and_free(box):
    box.preinstall_tools()
    assert "not used" in box.run("status").stdout
    assert box.run("linux-vm", "on").returncode == 0
    write_lock(box, "air-1")
    assert "slots:     1 of 1 in use" in box.run("status").stdout
    box.run("slots", "3")
    assert "slots:     1 of 3 in use" in box.run("status").stdout
    box.run("slots", "off")
    assert "slots:     off" in box.run("status").stdout


def test_with_two_slots_the_native_runner_keeps_going_while_the_vm_is_busy(box):
    box.preinstall_tools()
    assert box.run("linux-vm", "on").returncode == 0
    set_slots(box, 2)
    box.run("linux-vm", "turns")
    assert loaded(box)
    box.flag("busy")
    box.run("linux-vm", "turns")
    assert loaded(box)  # one of two slots is free: both can run
    set_slots(box, 1)  # lowered live: now they take turns again, but nothing running is touched
    box.run("linux-vm", "turns")
    assert not loaded(box)
    set_slots(box, "off")
    box.run("linux-vm", "turns")
    assert loaded(box)  # off: no gating at all


def test_two_slots_keep_vm_ci_on_while_the_native_runner_is_busy(box):
    box.preinstall_tools()
    assert box.run("linux-vm", "on").returncode == 0
    set_slots(box, 2)
    box.run("linux-vm", "sync")
    box.flag("native_busy")
    box.run("linux-vm", "sync")
    assert (box.vm / "applied-ci").read_text().strip() == "on"  # a second slot is free for the VM
    set_slots(box, 1)
    box.run("linux-vm", "sync")
    assert (box.vm / "applied-ci").read_text().strip() == "off"


def test_ci_off_stops_an_idle_runner_at_once(box):
    box.preinstall_tools()
    assert box.run("linux-vm", "on").returncode == 0
    box.run("linux-vm", "turns")
    assert loaded(box)
    r = box.run("ci", "off")
    assert r.returncode == 0, r.stdout + r.stderr
    assert not loaded(box) and (box.home / "ci-off").exists()  # no wait for the 30 s power watch


def test_ci_off_leaves_a_busy_runner_alone_then_stops_it_when_its_job_ends(box):
    box.preinstall_tools()
    assert box.run("linux-vm", "on").returncode == 0
    box.run("linux-vm", "turns")
    box.flag("native_busy")
    assert box.run("ci", "off").returncode == 0
    assert loaded(box)  # the running job is never cut short
    box.flag("native_busy", on=False)
    deadline = time.time() + 10
    while loaded(box) and time.time() < deadline:
        time.sleep(0.2)
    assert not loaded(box)  # the drain stops it the moment the job ends
    deadline = time.time() + 10
    while (box.home / "ci-drain.pid").exists() and time.time() < deadline:
        time.sleep(0.2)
    assert not (box.home / "ci-drain.pid").exists()
