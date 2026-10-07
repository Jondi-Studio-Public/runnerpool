"""linuxrunner push-setup / push / push-test, against a stubbed systemd and a stubbed curl."""

import json
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LINUXRUNNER = ROOT / "linux" / "linuxrunner"
TOKEN = "t" * 40
STUBS = {
    "id": '#!/bin/sh\n[ "$1" = -u ] && echo 0 || /usr/bin/id "$@"\n',
    "nproc": "#!/bin/sh\necho 8\n",
    "free": "#!/bin/sh\nprintf '              total\\nMem:          16000\\n'\n",
    "pgrep": "#!/bin/sh\nexit 1\n",
    "systemctl": '#!/bin/sh\necho "$@" >> "$STUB_LOG"\ncase "$1" in list-units) exit 0 ;; esac\nexit 0\n',
    # records every argument, the config file's contents and stdin; prints the status in $STUB_CODE
    "curl": """#!/bin/bash
{
  printf 'ARGS %s\\n' "$*"
  while [ $# -gt 0 ]; do
    [ "$1" = -K ] && { printf 'CFG '; tr -d '\\n' < "$2"; echo; }
    shift
  done
  printf 'BODY '; cat; echo
} >> "$STUB_CURL"
if [ "${STUB_CODE:-200}" = fail ]; then exit 7; fi
printf '%s' "${STUB_CODE:-200}"
""",
}


def setup(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name, body in STUBS.items():
        (bindir / name).write_text(body)
        (bindir / name).chmod(0o755)
    home, units = tmp_path / "home", tmp_path / "systemd"
    home.mkdir()
    units.mkdir()
    (home / "host").write_text("wsl-1\n")
    tok = tmp_path / "tok"
    tok.write_text(TOKEN + "\n")
    env = {
        **os.environ,
        "PATH": f"{bindir}:{os.environ['PATH']}",
        "GIT_RUNNER_HOME": str(home),
        "SYSTEMD_DIR": str(units),
        "STUB_LOG": str(tmp_path / "systemctl.log"),
        "STUB_CURL": str(tmp_path / "curl.log"),
    }
    return env, home, units, tok


def run(env, *args):
    return subprocess.run(
        ["bash", str(LINUXRUNNER), *args], env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL
    )


def test_push_setup_stores_config_token_and_service(tmp_path):
    env, home, units, tok = setup(tmp_path)
    r = run(env, "push-setup", "https://runners.example.com/", str(tok))
    assert r.returncode == 0, r.stderr
    conf = json.loads((home / "dashboard-push.json").read_text())
    assert conf == {"url": "https://runners.example.com/api/push-info", "host": "wsl-1"}
    assert (home / "dashboard-push-token").read_text() == TOKEN
    assert oct((home / "dashboard-push-token").stat().st_mode & 0o777) == "0o600"
    assert "push-loop" in (units / "linuxrunner-push.service").read_text()
    assert "enable linuxrunner-push.service" in (tmp_path / "systemctl.log").read_text()
    assert TOKEN not in r.stdout + r.stderr


def test_push_setup_takes_a_url_that_already_ends_in_the_endpoint(tmp_path):
    env, home, _, tok = setup(tmp_path)
    assert run(env, "push-setup", "http://100.64.0.1:8765/api/push-info", str(tok)).returncode == 0
    assert json.loads((home / "dashboard-push.json").read_text())["url"] == "http://100.64.0.1:8765/api/push-info"


def test_push_setup_refuses_bad_input(tmp_path):
    env, home, _, tok = setup(tmp_path)
    assert run(env, "push-setup", "ftp://x", str(tok)).returncode != 0
    assert run(env, "push-setup", "https://x.example.com", str(tmp_path / "nope")).returncode != 0
    (home / "host").write_text("laptop\n")
    r = run(env, "push-setup", "https://x.example.com", str(tok))
    assert r.returncode != 0 and "wsl-N" in r.stderr
    assert not (home / "dashboard-push.json").exists()


def test_push_sends_info_with_the_token_only_in_a_private_config(tmp_path):
    env, home, _, tok = setup(tmp_path)
    assert run(env, "push-setup", "https://runners.example.com", str(tok)).returncode == 0
    r = run(env, "push")
    assert r.returncode == 0, r.stderr
    log = (tmp_path / "curl.log").read_text()
    args = next(line for line in log.splitlines() if line.startswith("ARGS"))
    assert TOKEN not in args  # never on a command line
    assert "https://runners.example.com/api/push-info" in args
    assert f'CFG header = "Authorization: Bearer {TOKEN}"' in log
    body = json.loads(next(line for line in log.splitlines() if line.startswith("BODY "))[5:])
    assert body["host"] == "wsl-1" and body["platform"] == "linux" and body["cores"] == 8


def test_push_test_explains_the_dashboards_answer(tmp_path):
    env, _, _, tok = setup(tmp_path)
    assert run(env, "push-setup", "https://runners.example.com", str(tok)).returncode == 0
    assert "accepted" in run(env, "push-test").stdout
    for code, words in (("401", "does not know this token"), ("429", "too many"), ("503", "no push tokens")):
        r = run({**env, "STUB_CODE": code}, "push-test")
        assert r.returncode == 1 and words in r.stdout
    assert run({**env, "STUB_CODE": "fail"}, "push-test").returncode == 1


def test_push_setup_rejects_a_malformed_token_or_url(tmp_path):
    env, home, _, tok = setup(tmp_path)
    tok.write_text('short"token\n')
    assert run(env, "push-setup", "https://x.example.com", str(tok)).returncode != 0
    tok.write_text("  \n")
    assert run(env, "push-setup", "https://x.example.com", str(tok)).returncode != 0
    tok.write_text(TOKEN)
    assert run(env, "push-setup", 'https://x.example.com/"x', str(tok)).returncode != 0
    assert not (home / "dashboard-push.json").exists()


def test_push_leaves_no_token_file_behind(tmp_path):
    env, _, _, tok = setup(tmp_path)
    tmp = tmp_path / "tmp"
    tmp.mkdir()
    env["TMPDIR"] = str(tmp)
    assert run(env, "push-setup", "https://runners.example.com", str(tok)).returncode == 0
    assert run(env, "push").returncode == 0
    assert list(tmp.iterdir()) == []


def test_push_before_setup_says_how_to_set_it_up(tmp_path):
    env, *_ = setup(tmp_path)
    r = run(env, "push")
    assert r.returncode != 0 and "push-setup" in r.stderr
