"""linuxrunner's `limit` command, run against a stubbed systemd: env file, drop-in, limits file, info."""

import json
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LINUXRUNNER = ROOT / "linux" / "linuxrunner"
STUBS = {
    "id": '#!/bin/sh\n[ "$1" = -u ] && echo 0 || /usr/bin/id "$@"\n',
    "nproc": "#!/bin/sh\necho 8\n",
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
    env = {
        **os.environ,
        "PATH": f"{bindir}:{os.environ['PATH']}",
        "GIT_RUNNER_HOME": str(home),
        "SYSTEMD_DIR": str(units),
        "STUB_DIR": str(tmp_path),
        "STUB_LOG": str(tmp_path / "systemctl.log"),
        "STUB_BUSY": str(tmp_path / "busy"),
    }
    return env, home, units


def run(env, *args):
    return subprocess.run(
        ["bash", str(LINUXRUNNER), *args], env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL
    )


def test_limit_writes_env_dropin_and_info(tmp_path):
    env, home, units = setup(tmp_path)
    r = run(env, "limit", "ci-1", "cores=3", "ram=4096")
    assert r.returncode == 0, r.stderr
    assert (home / "conf/ci-1.limits").read_text().split() == ["cores=3", "ram=4096"]
    assert (tmp_path / "ci-1/.env").read_text().split() == ["CI_MAX_CORES=3", "CI_MAX_RAM_MB=4096"]
    dropin = units / "actions.runner.o-r.ci-1.service.d/limits.conf"
    assert dropin.read_text() == "[Service]\nMemoryMax=4096M\n"
    assert not (tmp_path / "ci-2/.env").exists()  # other runners untouched: absent = device-wide behaviour
    assert "restart actions.runner.o-r.ci-1.service" in (tmp_path / "systemctl.log").read_text()

    info = json.loads(run(env, "info").stdout)
    assert info["cores"] == 8 and info["ram_mb"] == 16000 and info["memory_gb"] == 16
    by = {x["name"]: x for x in info["runners"]}
    assert by["ci-1"]["limit"] == {"cores": 3, "ram_mb": 4096} and by["ci-1"]["kind"] == "ci"
    assert by["ci-2"]["limit"] == {"cores": None, "ram_mb": None}
    assert by["wsl-1-admin"]["kind"] == "admin"


def test_per_runner_cores_win_over_device_wide_and_default_restores_it(tmp_path):
    env, home, units = setup(tmp_path)
    assert run(env, "cores", "6").returncode == 0
    assert "CI_MAX_CORES=6" in (tmp_path / "ci-1/.env").read_text()
    assert run(env, "limit", "ci-1", "cores=2").returncode == 0
    assert (tmp_path / "ci-1/.env").read_text() == "CI_MAX_CORES=2\n"
    assert (tmp_path / "ci-2/.env").read_text() == "CI_MAX_CORES=6\n"
    assert run(env, "cores", "5").returncode == 0  # a device-wide change keeps the override
    assert (tmp_path / "ci-1/.env").read_text() == "CI_MAX_CORES=2\n"
    assert (tmp_path / "ci-2/.env").read_text() == "CI_MAX_CORES=5\n"
    assert run(env, "limit", "ci-1", "cores=default").returncode == 0
    assert (tmp_path / "ci-1/.env").read_text() == "CI_MAX_CORES=5\n"
    assert not (home / "conf/ci-1.limits").exists()


def test_default_ram_removes_the_dropin_and_one_key_leaves_the_other(tmp_path):
    env, home, units = setup(tmp_path)
    run(env, "limit", "ci-1", "cores=2", "ram=2048")
    run(env, "limit", "ci-1", "ram=default")
    assert (home / "conf/ci-1.limits").read_text() == "cores=2\n"
    assert not (units / "actions.runner.o-r.ci-1.service.d").exists()
    assert (tmp_path / "ci-1/.env").read_text() == "CI_MAX_CORES=2\n"


def test_busy_runner_is_not_restarted(tmp_path):
    env, home, units = setup(tmp_path)
    (tmp_path / "busy").write_text("")
    r = run(env, "limit", "ci-1", "cores=2")
    assert r.returncode == 0
    assert "restart" not in (tmp_path / "systemctl.log").read_text()
    assert "CI_MAX_CORES=2" in (tmp_path / "ci-1/.env").read_text()  # written, applies on the next restart


def test_limit_rejects_bad_input(tmp_path):
    env, home, units = setup(tmp_path)
    for args in (
        ["ci-1"],
        ["ci-1", "cores=0"],
        ["ci-1", "cores=9"],
        ["ci-1", "cores=x"],
        ["ci-1", "ram=100"],
        ["ci-1", "ram=99999"],
        ["ci-1", "mem=1"],
        ["ci-1", "cores"],
        ["nope", "cores=1"],
        ["wsl-1-admin", "cores=1"],
    ):
        r = run(env, "limit", *args)
        assert r.returncode != 0, args
    assert not (home / "conf").exists() or not list((home / "conf").iterdir())


GH_STUB = r"""#!/bin/bash
# stub gh: one workflow run (id 123) whose jobs come from $STUB_JOBS, filtered by the caller's --jq
case "$1 $2" in
  "run list") echo 123 ;;
  "run view")
    expr=""
    while [ $# -gt 0 ]; do [ "$1" = --jq ] && expr=$2; shift; done
    [ -n "$expr" ] && printf '%s' "$STUB_JOBS" | jq -r "$expr" ;;
  "secret list") echo RUNNER_PAT ;;
esac
exit 0
"""


def run_runner(tmp_path, jobs, *args):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "gh").write_text(GH_STUB)
    (bindir / "gh").chmod(0o755)
    env = {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}", "GITRUNNER_ORG": "o", "STUB_JOBS": json.dumps(jobs)}
    return subprocess.run(
        ["bash", str(ROOT / "runner"), *args], env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL
    )


def test_a_skipped_job_is_an_error_not_a_success(tmp_path):
    skipped = {"jobs": [{"status": "completed", "conclusion": "skipped"}]}
    for args in (("publish-win",), ("status", "air-1")):
        d = tmp_path / args[0]
        d.mkdir()
        r = run_runner(d, skipped, *args)
        assert r.returncode == 1 and "skipped" in r.stderr + r.stdout, (args, r.stderr)
        assert "RUNNERPOOL_SELF_HOSTED" in r.stderr


def test_success_and_no_jobs_are_not_reported_as_skipped(tmp_path):
    ok = {"jobs": [{"status": "completed", "conclusion": "success"}]}
    (tmp_path / "a").mkdir()
    r = run_runner(tmp_path / "a", ok, "status", "air-1")
    assert r.returncode == 0 and "skipped" not in r.stderr, r.stderr
    (tmp_path / "b").mkdir()
    r = run_runner(tmp_path / "b", {"jobs": []}, "publish-win")
    assert r.returncode == 0 and "skipped" not in r.stderr, r.stderr
