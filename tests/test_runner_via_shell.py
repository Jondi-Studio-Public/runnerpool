"""`runner` reaches a Mac over Tailscale SSH first and falls back to the GitHub Admin workflow (fake ssh and gh on PATH)."""

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RUNNER = ROOT / "runner"
SSH = '#!/bin/sh\necho "$@" >> "$STUB_LOG.ssh"\nif [ "$STUB_SSH_RC" = 255 ]; then echo "ssh: connect to host: Connection timed out" >&2; exit 255; fi\necho ssh-output\nexit "${STUB_SSH_RC:-0}"\n'
GH = """#!/bin/sh
echo "$@" >> "$STUB_LOG.gh"
case "$1 $2" in
  "run list") echo 42 ;;
  "run view") echo completed ;;
esac
exit 0
"""


def run(tmp_path, *args, ssh_rc="0", via=None):
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    for name, body in (("ssh", SSH), ("gh", GH), ("sleep", "#!/bin/sh\n")):
        (bindir / name).write_text(body)
        (bindir / name).chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{bindir}:{os.environ['PATH']}",
        "GITRUNNER_ORG": "example-org",
        "STUB_LOG": str(tmp_path / "log"),
        "STUB_SSH_RC": ssh_rc,
    }
    env.pop("RUNNER_VIA", None)
    if via:
        env["RUNNER_VIA"] = via
    r = subprocess.run(["bash", str(RUNNER), *args], env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL)

    def calls(kind):
        f = tmp_path / f"log.{kind}"
        return f.read_text().splitlines() if f.exists() else []

    return r, calls("ssh"), calls("gh")


def test_ssh_success_skips_github(tmp_path):
    r, ssh, gh = run(tmp_path, "ci", "air-1", "off")
    assert r.returncode == 0, r.stderr
    assert "runner: via tailscale ssh" in r.stderr
    assert "ssh-output" in r.stdout
    assert gh == []
    assert len(ssh) == 1 and ssh[0].endswith("root@air-1 /usr/local/mac-runners/macrunner ci off")
    assert "BatchMode=yes" in ssh[0] and "ConnectTimeout=8" in ssh[0] and "StrictHostKeyChecking=accept-new" in ssh[0]


def test_ssh_255_falls_back_to_github(tmp_path):
    r, ssh, gh = run(tmp_path, "ci", "air-1", "off", ssh_rc="255")
    assert r.returncode == 0, r.stderr
    assert "runner: via github actions (ssh to air-1 failed: ssh: connect to host: Connection timed out)" in r.stderr
    assert "ssh-output" not in r.stdout
    assert len(ssh) == 1
    assert any(c.startswith("workflow run admin.yml") and "action=ci-off" in c for c in gh)
    assert "ci-off on air-1: https://github.com/example-org/runnerpool/actions/runs/42" in r.stdout


def test_other_ssh_exit_stands(tmp_path):
    r, ssh, gh = run(tmp_path, "restart", "air-1", ssh_rc="1")
    assert r.returncode == 1
    assert len(ssh) == 1 and ssh[0].endswith("macrunner restart air-1")
    assert gh == []
    assert "via github" not in r.stderr


def test_github_only_actions_skip_ssh(tmp_path):
    r, ssh, gh = run(tmp_path, "update", "air-1")
    assert r.returncode == 0, r.stderr
    assert ssh == [] and gh
    assert "via github actions" not in r.stderr  # nothing was tried over ssh, so nothing fell back


def test_runner_via_github_never_calls_ssh(tmp_path):
    r, ssh, gh = run(tmp_path, "ci", "air-1", "on", via="github")
    assert r.returncode == 0, r.stderr
    assert "runner: via github actions (RUNNER_VIA=github)" in r.stderr
    assert ssh == [] and gh


def test_runner_via_ssh_does_not_fall_back(tmp_path):
    r, ssh, gh = run(tmp_path, "ci", "air-1", "on", ssh_rc="255", via="ssh")
    assert r.returncode == 1
    assert "ssh to air-1 failed" in r.stderr
    assert len(ssh) == 1 and gh == []


def test_runner_via_ssh_refuses_github_only_action(tmp_path):
    r, ssh, gh = run(tmp_path, "update", "air-1", via="ssh")
    assert r.returncode == 1 and ssh == [] and gh == []


def test_bad_runner_via_is_rejected(tmp_path):
    r, ssh, gh = run(tmp_path, "ci", "air-1", "on", via="carrier-pigeon")
    assert r.returncode == 1 and "RUNNER_VIA" in r.stderr
    assert ssh == [] and gh == []


def test_unsafe_value_never_reaches_ssh(tmp_path):
    r, ssh, gh = run(tmp_path, "logs", "air-1", "x; touch /tmp/x")
    assert ssh == []
    assert "not safe to send over ssh" in r.stderr


def test_cores_must_be_a_number_or_all(tmp_path):
    r, ssh, gh = run(tmp_path, "cores", "air-1", "4; touch /tmp/x")
    assert r.returncode != 0 and ssh == [] and gh == []


def test_limit_on_an_admin_runner_skips_ssh(tmp_path):
    r, ssh, gh = run(tmp_path, "limit", "air-1", "air-1-admin", "cores=2")
    assert ssh == []


def test_non_mac_hosts_never_use_ssh(tmp_path):
    for host in ("win-1", "wsl-1", "air-1-vm"):
        r, ssh, gh = run(tmp_path, "restart", host)
        assert r.returncode == 0, r.stderr
        assert ssh == [] and gh, host
        assert "runner: via" not in r.stderr
