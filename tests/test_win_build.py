"""win/build.sh assembles the Windows installer: check what lands in it."""

import base64
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def build(tmp_path, **env):
    full = {**os.environ, "RUNNER_VERSION": "2.999.0", **env}
    out = tmp_path / "out"
    subprocess.run(
        ["bash", str(ROOT / "win" / "build.sh"), str(out)],
        check=True,
        env=full,
        stdin=subprocess.DEVNULL,
        capture_output=True,
    )
    return (out / "win-runners.ps1").read_bytes().decode("utf-8")


def test_dry_build_fills_every_placeholder(tmp_path):
    out = tmp_path / "out"
    subprocess.run(
        ["bash", str(ROOT / "win" / "build.sh"), str(out), "--dry"],
        check=True,
        stdin=subprocess.DEVNULL,
        capture_output=True,
    )
    text = (out / "win-runners.ps1").read_text()
    assert "@@" not in text
    assert "RUNNER_PAT     = 'dry-run-placeholder'" in text
    assert "CI_LABELS      = 'win-ci'" in text


def test_payload_is_winrunner_and_lines_are_crlf(tmp_path):
    text = build(tmp_path, RUNNER_PAT="t")
    payload = re.search(r"\$Payload = '([^']+)'", text).group(1)
    assert base64.b64decode(payload) == (ROOT / "win" / "winrunner.ps1").read_bytes()
    assert all(line.endswith("\r") for line in text.split("\n")[:-1])


def test_token_with_quote_and_ampersand_survives(tmp_path):
    text = build(tmp_path, RUNNER_PAT="a'b&c$d")
    # PowerShell single quotes double the quote; & and $ stay literal.
    assert "RUNNER_PAT     = 'a''b&c$d'" in text


def test_win_labels_never_claim_linux():
    for f in ("win/build.sh", ".github/workflows/admin-win.yml", "win/winrunner.ps1"):
        assert not re.search(r"labels?[^\n]*linux", (ROOT / f).read_text(), re.I)


def test_linux_tool_and_wsl_set_are_embedded(tmp_path):
    text = build(tmp_path, RUNNER_PAT="t")
    payload = re.search(r"\$LinuxPayload = '([^']+)'", text).group(1)
    assert base64.b64decode(payload) == (ROOT / "linux" / "linuxrunner").read_bytes()
    assert "WSL_SET        = 'wsl:2:linux-ci,docker'" in text
    provision = re.search(r"\$ProvisionPayload = '([^']+)'", text).group(1)
    assert base64.b64decode(provision) == (ROOT / "linux" / "linux-provision.sh").read_bytes()


def test_wsl_paths_match_linuxrunner_home():
    home = re.search(r"^HOME_DIR=(\S+)", (ROOT / "linux/linuxrunner").read_text(), re.M).group(1)
    for name in ("win/winrunner.ps1", "linux/linux-provision.sh"):
        paths = set(re.findall(r"/opt/git-runners?\b", (ROOT / name).read_text()))
        assert paths <= {home}, f"{name} uses {paths}, linuxrunner uses {home}"


def app_build(tmp_path, **env):
    # a build that does not inherit anything from the caller's environment
    base = {
        k: v
        for k, v in os.environ.items()
        if k not in ("RUNNER_PAT", "CI_APP_ID", "CI_APP_PRIVATE_KEY", "CI_APP_KEY_B64")
    }
    out = tmp_path / "out"
    subprocess.run(
        ["bash", str(ROOT / "win" / "build.sh"), str(out)],
        check=True,
        env={**base, "RUNNER_VERSION": "2.999.0", **env},
        stdin=subprocess.DEVNULL,
        capture_output=True,
    )
    return (out / "win-runners.ps1").read_bytes().decode("utf-8")


def test_github_app_credentials_are_embedded_instead_of_the_pat(tmp_path):
    # assembled at run time so no key-shaped block sits in the source (the secret scan would rightly flag it)
    pem = "-----" + "BEGIN RSA PRIVATE KEY" + "-----\nTEST+KEY/DATA==\n-----" + "END RSA PRIVATE KEY" + "-----\n"
    text = app_build(tmp_path, CI_APP_ID="12345", CI_APP_PRIVATE_KEY=pem, RUNNER_PAT="should-not-be-embedded")
    assert "GITHUB_APP_ID  = '12345'" in text
    key = re.search(r"GITHUB_APP_KEY_B64 = '([^']*)'", text).group(1)
    assert base64.b64decode(key).decode() == pem
    assert "RUNNER_PAT     = ''" in text
    assert "should-not-be-embedded" not in text and "@@" not in text


def test_an_already_encoded_key_is_taken_as_is(tmp_path):
    text = app_build(tmp_path, CI_APP_ID="7", CI_APP_KEY_B64="QUJD")
    assert "GITHUB_APP_KEY_B64 = 'QUJD'" in text and "RUNNER_PAT     = ''" in text


def test_without_app_secrets_the_pat_is_embedded_and_the_app_fields_are_empty(tmp_path):
    text = app_build(tmp_path, RUNNER_PAT="tok")
    assert "RUNNER_PAT     = 'tok'" in text
    assert "GITHUB_APP_ID  = ''" in text and "GITHUB_APP_KEY_B64 = ''" in text


def test_a_half_set_app_falls_back_to_the_pat_and_nothing_at_all_fails(tmp_path):
    text = app_build(tmp_path, CI_APP_ID="12345", RUNNER_PAT="tok")
    assert "RUNNER_PAT     = 'tok'" in text and "GITHUB_APP_ID  = ''" in text
    r = subprocess.run(
        ["bash", str(ROOT / "win" / "build.sh"), str(tmp_path / "o2")],
        env={k: v for k, v in os.environ.items() if k not in ("RUNNER_PAT", "CI_APP_ID", "CI_APP_PRIVATE_KEY")}
        | {"RUNNER_VERSION": "2.999.0"},
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
    )
    assert r.returncode != 0 and "required" in r.stdout


def test_the_org_must_be_configured(tmp_path):
    env = {k: v for k, v in os.environ.items() if k != "GITRUNNER_ORG"}
    r = subprocess.run(
        ["bash", str(ROOT / "win" / "build.sh"), str(tmp_path / "out")],
        env={**env, "RUNNER_VERSION": "2.999.0"},
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
    )
    assert r.returncode != 0 and "GITRUNNER_ORG" in r.stderr


def test_scripts_reach_the_distro_as_files_never_on_the_command_line():
    # Windows caps a command line at 32767 characters; linuxrunner in base64 is far past that
    # ("The filename or extension is too long"), so the installer copies it through the distro's view of the drive.
    ps = (ROOT / "win" / "winrunner.ps1").read_text()
    body = ps.split("function Install-WslSet")[1].split("\nfunction ")[0]
    assert "ToBase64String" not in body and "base64 -d" not in body
    assert "Copy-ToWsl $distro (Join-Path $HomeDir 'linuxrunner') '/tmp/linuxrunner'" in body
    assert "Copy-ToWsl $distro (Join-Path $HomeDir 'linux-provision.sh') '/tmp/linux-provision.sh'" in body
    assert "wslpath -u" in ps  # the distro maps the path itself: a custom automount root works too
    assert len(base64.b64encode((ROOT / "linux" / "linuxrunner").read_bytes())) > 32767  # why it matters


def test_copy_to_wsl_builds_a_quoted_wslpath_copy():
    if not shutil.which("pwsh"):
        pytest.skip("pwsh not installed")
    ps = (ROOT / "win" / "winrunner.ps1").read_text()
    fn = re.search(r"function Copy-ToWsl.*?\n}\n", ps, re.S).group(0)
    stub = 'function Invoke-WslIn($d, $c) { Write-Output "$d|$c" }\n'
    src = r"C:\ProgramData\win-runners\linuxrunner"
    call = f"Copy-ToWsl gh-runner '{src}' '/tmp/linuxrunner'"
    out = subprocess.run(
        ["pwsh", "-NoProfile", "-Command", stub + fn + call], capture_output=True, text=True, check=True
    ).stdout.strip()
    assert out == f"gh-runner|install -m 755 \"$(wslpath -u '{src}')\" '/tmp/linuxrunner'"


def test_the_org_is_baked_into_the_installer_settings(tmp_path):
    text = build(tmp_path, RUNNER_PAT="t", GITRUNNER_ORG="my-org'x")
    assert "GITRUNNER_ORG  = 'my-org''x'" in text and "@@" not in text


def test_winrunner_has_no_default_org_and_bootstrap_saves_and_checks_it():
    src = (ROOT / "win" / "winrunner.ps1").read_text()
    assert "example-org" not in src
    assert "GITRUNNER_ORG is not set" in src
    boot = src[src.index("$e.GITRUNNER_ORG") :]
    assert boot.index("Write-Private $OrgFile") < boot.index("Initialize-AppToken")
    assert boot.index("Assert-AppOrg") < boot.index("Save-App")
    assert "@@GITRUNNER_ORG@@" in (ROOT / "win" / "install.template.ps1").read_text()


def test_no_leftover_old_names_in_the_windows_installer_and_docs():
    assert "macs doctor" not in (ROOT / "win" / "winrunner.ps1").read_text()
    win_doc = (ROOT / "docs" / "windows.md").read_text()
    assert not re.search(r"`macs (ci|cores)", win_doc)
    assert "git-runner" not in (ROOT / "docs" / "ci-plan.md").read_text()
    assert "Jondi" not in (ROOT / ".github" / "ISSUE_TEMPLATE" / "config.yml").read_text()
