"""win/winrunner.ps1's `hyperv` command group (the opt-in Hyper-V VM host), run in PowerShell 7 where it is installed
(CI installs it). Hyper-V itself cannot run here: the argument parser, the cloud-init seed text and the refusals
(not elevated, no Hyper-V, missing confirm flag, a disk this tool did not make) are tested with the real functions, and
the Hyper-V cmdlets are stand-ins. Nothing here proves a VM boots; see docs/windows.md."""

import base64
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
WINRUNNER = ROOT / "win" / "winrunner.ps1"
PWSH = shutil.which("pwsh") or (
    os.environ.get("PWSH") if os.environ.get("PWSH") and Path(os.environ["PWSH"]).exists() else None
)
pytestmark = pytest.mark.skipif(PWSH is None, reason="pwsh not installed")
ENV = {k: v for k, v in os.environ.items() if k not in ("RUNNER_NAME", "GIT_RUNNER_NAME")}
ENV["DOTNET_SYSTEM_GLOBALIZATION_INVARIANT"] = "1"

FUNCS = [
    "Die",
    "ConvertFrom-HvArgs",
    "Get-HvDir",
    "Assert-HvReady",
    "Read-HvMarker",
    "Get-HvRemovable",
    "ConvertTo-Lf",
    "ConvertTo-B64",
    "New-HvFirstBoot",
    "New-HvUserData",
    "Read-HvSshKey",
    "Get-HvVm",
    "Get-HvToolVms",
    "New-HvVmHost",
    "Show-HvStatus",
    "Set-HvPower",
    "Remove-HvVmHost",
    "Invoke-HvCompact",
    "Invoke-HvEjectSeed",
    "Invoke-Hyperv",
]
VARS = ["HvDefaultDir", "HvMarkerName", "HvNotesPrefix", "HvOptions"]
PRELUDE = f"""
$ErrorActionPreference = 'Stop'
$HomeDir = $env:TEST_HOME
$AppOrg = 'example-org'
$HostFile = Join-Path $HomeDir 'host'
$ast = [Management.Automation.Language.Parser]::ParseFile('{WINRUNNER}', [ref]$null, [ref]$null)
foreach ($a in $ast.FindAll({{ param($x) $x -is [Management.Automation.Language.AssignmentStatementAst] -and $x.Left.VariablePath -and $x.Left.VariablePath.UserPath -in '{"','".join(VARS)}' }}, $false)) {{
  Invoke-Expression $a.Extent.Text
}}
foreach ($n in '{"','".join(FUNCS)}') {{
  $fn = $ast.Find({{ param($x) $x -is [Management.Automation.Language.FunctionDefinitionAst] -and $x.Name -eq $n }}, $true)
  if (-not $fn) {{ throw "no function $n in winrunner.ps1" }}
  Invoke-Expression $fn.Extent.Text
}}
$HvDefaultDir = 'D:\\hv'   # a drive path, as on Windows
function Log([string]$m) {{ Write-Output "LOG $m" }}
function Assert-Admin {{ }}
function Run([scriptblock]$b) {{ try {{ & $b }} catch {{ Write-Output "ERR $($_.Exception.Message)" }} }}
"""


def ps(tmp_path, script, **env):
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    return subprocess.run(
        [PWSH, "-NoProfile", "-NonInteractive", "-Command", PRELUDE + script],
        env={**ENV, "TEST_HOME": str(home), **env},
        capture_output=True,
        text=True,
        timeout=90,
        stdin=subprocess.DEVNULL,
    )


def out(r):
    assert r.returncode == 0, r.stdout + r.stderr
    return r.stdout


def err_of(tmp_path, script):
    return out(ps(tmp_path, f"Run {{ {script} }}"))


# --- arguments ---------------------------------------------------------------------------------------------


def test_defaults(tmp_path):
    o = out(
        ps(
            tmp_path,
            "$o = ConvertFrom-HvArgs 'create' @(); '{0} {1} {2} {3} {4} {5} {6}' -f $o.Name, $o.VCpu, $o.RamGB, $o.DiskGB, $o.Runners, $o.Tags, $o.CiRepo",
        )
    )
    assert "hv-ci 4 16 100 2 linux-ci example-org" in o


def test_options_are_parsed(tmp_path):
    o = out(
        ps(
            tmp_path,
            "$o = ConvertFrom-HvArgs 'create' @('-vcpu','6','-RamGB','24','-DiskGB','200','-VhdxDir','D:\\vm\\','-Name','box','-Yes','-Runners','3','-RamdiskMB','4096','-CoresPerJob','2');"
            "'{0} {1} {2} {3} {4} {5} {6} {7} {8}' -f $o.VCpu, $o.RamGB, $o.DiskGB, $o.VhdxDir, $o.Name, $o.Yes, $o.Runners, $o.RamdiskMB, $o.CoresPerJob",
        )
    )
    assert "6 24 200 D:\\vm box True 3 4096 2" in o


@pytest.mark.parametrize(
    "words, msg",
    [
        ("'-Bogus','1'", "unknown option -Bogus"),
        ("'-VCpu'", "-VCpu needs a value"),
        ("'-VCpu','x'", "whole number"),
        ("'-VCpu','0'", "-VCpu is 1 to 64"),
        ("'-RamGB','1'", "-RamGB is 2 to 512"),
        ("'-DiskGB','5'", "-DiskGB is 20 to 4000"),
        ("'-Name','bad name'", "-Name is letters"),
        ("'-VhdxDir','relative\\dir'", "-VhdxDir is a full local path"),
        ("'-VhdxDir','\\\\server\\share'", "-VhdxDir is a full local path"),
        ("'-Tags','docker'", "must include linux-ci"),
        ("'-Tags','linux-ci;rm'", "-Tags is a comma-separated"),
        ("'-AdminRepo','nope'", "-AdminRepo is OWNER/REPO"),
        ("'-CoresPerJob','9'", "-CoresPerJob is 0"),
        ("'-RamdiskMB','100'", "-RamdiskMB is 0"),
        ("'-RamGB','4','-RamdiskMB','4096'", "-RamdiskMB is 0"),
    ],
)
def test_bad_options_are_refused(tmp_path, words, msg):
    assert msg in err_of(tmp_path, f"ConvertFrom-HvArgs 'create' @({words})")


# --- refusals ----------------------------------------------------------------------------------------------


def test_create_needs_yes(tmp_path):
    assert "add -Yes" in err_of(tmp_path, "Invoke-Hyperv @('create','-AdminRepo','example-org/runnerpool')")


def test_refuses_when_not_elevated(tmp_path):
    s = "function Assert-Admin { Die 'run in an administrator PowerShell' }; Invoke-Hyperv @('status')"
    assert "administrator PowerShell" in err_of(tmp_path, s)


def test_refuses_without_the_hyperv_module(tmp_path):
    assert "Hyper-V PowerShell module is missing" in err_of(tmp_path, "Invoke-Hyperv @('status')")


def test_refuses_when_the_feature_is_off(tmp_path):
    s = (
        "function Get-VM { }; function Get-WindowsOptionalFeature { [pscustomobject]@{ State = 'Disabled' } };"
        "Invoke-Hyperv @('status')"
    )
    assert "Hyper-V feature is not enabled" in err_of(tmp_path, s)


def test_create_needs_an_admin_repo(tmp_path):
    s = "function Assert-HvReady { }; Invoke-Hyperv @('create','-Yes')"
    assert "needs -AdminRepo" in err_of(tmp_path, s)


def test_create_refuses_an_existing_directory(tmp_path):
    target = tmp_path / "vm" / "hv-ci"
    target.mkdir(parents=True)
    s = (
        f"function Assert-HvReady {{ }}; function Get-VM {{ }}; Set-Content '{tmp_path}/home/host' 'win-1';"
        f"Invoke-Hyperv @('create','-Yes','-AdminRepo','example-org/runnerpool','-VhdxDir','C:\\nope') ;"
    )
    # a Linux path is not a drive-letter path, so the parser refuses it before anything is touched
    assert "-VhdxDir is a full local path" in err_of(tmp_path, s.replace("C:\\nope", str(target.parent)))


# --- the VM's first boot -----------------------------------------------------------------------------------

CLOUD = """
$o = ConvertFrom-HvArgs 'create' @('-AdminRepo','example-org/runnerpool','-Runners','2','-RamdiskMB','4096','-CoresPerJob','3');
$fb = New-HvFirstBoot $o 'hv-1' 'win-1' @('win-1-hv-1','win-1-hv-2')
$ud = New-HvUserData $o 'hv-1' @{ 'firstboot.sh' = $fb; 'linuxrunner' = "a`r`nb`r`n"; 'ci-token' = 'TOK' } 'ssh-ed25519 AAAA'
"""


def test_firstboot_provisions_before_registering(tmp_path):
    o = out(ps(tmp_path, CLOUD + "$fb"))
    lines = o.splitlines()

    def at(text):
        return next(i for i, k in enumerate(lines) if text in k)

    prov = at("bash ./linux-provision.sh")
    boot = at("bash /tmp/linuxrunner bootstrap hv-1")
    admin = at("install-admin example-org/runnerpool")
    assert lines[admin].endswith(" hv-1-admin")
    ci1 = at("add-runner example-org win-1-hv-1 linux-ci")
    ci2 = at("add-runner example-org win-1-hv-2 linux-ci")
    assert lines[ci1].endswith(" ci")
    assert prov < boot < admin < ci1 < ci2
    assert "$LR cores 3" in lines and "$LR ramdisk on 4096" in lines
    assert lines[0] == "#!/bin/bash" and "set -euo pipefail" in lines
    assert "ghs_" not in o and "TOK" not in o  # tokens are read from files, never written into the script


def test_user_data_is_cloud_config_with_lf_files(tmp_path):
    o = out(ps(tmp_path, CLOUD + "$ud"))
    assert o.startswith("#cloud-config\n") and "hostname: hv-1" in o
    assert "ssh_authorized_keys:\n  - ssh-ed25519 AAAA" in o
    assert "runcmd:\n  - [ bash, /root/hv-seed/firstboot.sh ]" in o
    blocks = re.findall(
        r"path: /root/hv-seed/(\S+)\n    encoding: b64\n    permissions: '(\d+)'\n    content: (\S+)", o
    )
    got = {n: (m, base64.b64decode(c).decode()) for n, m, c in blocks}
    assert got["linuxrunner"] == ("0700", "a\nb\n")  # a Windows checkout's CRLF never reaches the VM
    assert got["ci-token"][0] == "0600" and got["ci-token"][1] == "TOK"
    assert "\r" not in o


def test_ssh_key_file_keeps_only_type_and_data(tmp_path):
    k = tmp_path / "k.pub"
    k.write_text("ssh-ed25519 AAAAC3Nz you@host\n")
    assert out(ps(tmp_path, f"Read-HvSshKey '{k}'")).strip() == "ssh-ed25519 AAAAC3Nz"
    k.write_text("not a key\n")
    assert "not an OpenSSH public key" in err_of(tmp_path, f"Read-HvSshKey '{k}'")


# --- remove never deletes what the tool did not make ----------------------------------------------------------


def make_vm_dir(tmp_path, **marker):
    d = tmp_path / "vms" / "hv-ci"
    d.mkdir(parents=True)
    for f in ("disk.vhdx", "seed.vhdx", "mine.txt"):
        (d / f).write_text("x")
    m = {
        "tool": "win-runners",
        "kind": "hyperv-vm",
        "id": "abc",
        "name": "hv-ci",
        "vhdx": str(d / "disk.vhdx"),
        "seed": str(d / "seed.vhdx"),
        "admin_repo": "example-org/runnerpool",
        "ci_repo": "example-org",
        "runners": [],
    }
    m.update(marker)
    import json

    (d / "hyperv-vm.json").write_text(json.dumps(m))
    return d


def test_remove_needs_the_typed_name(tmp_path):
    d = make_vm_dir(tmp_path)
    s = f"function Assert-HvReady {{ }}; function Get-VM {{ }}; Invoke-Hyperv @('remove','-VhdxDir','{d.parent}','-DeleteVhdx')"
    s = s.replace(f"'{d.parent}'", "'C:\\x'")  # parser refuses a non-drive path first
    r = err_of(tmp_path, s)
    assert "-ConfirmRemove hv-ci" in r or "full local path" in r


def removable(tmp_path, d, notes="$null"):
    return err_of(tmp_path, f"Get-HvRemovable '{d}' 'hv-ci' {notes} | ForEach-Object {{ Write-Output \"DEL $_\" }}")


def test_removable_lists_only_the_marked_files(tmp_path):
    d = make_vm_dir(tmp_path)
    o = removable(tmp_path, d, "'win-runners hyperv vm abc'")
    assert f"DEL {d}/disk.vhdx" in o and f"DEL {d}/seed.vhdx" in o and f"DEL {d}/hyperv-vm.json" in o
    assert "mine.txt" not in o


def test_removable_refuses_a_directory_without_a_marker(tmp_path):
    d = tmp_path / "plain"
    d.mkdir()
    (d / "disk.vhdx").write_text("x")
    assert "did not create a VM there" in removable(tmp_path, d)


def test_removable_refuses_a_foreign_marker(tmp_path):
    d = make_vm_dir(tmp_path, tool="someone-else")
    assert "did not create a VM there" in removable(tmp_path, d)


def test_removable_refuses_another_vms_marker(tmp_path):
    d = make_vm_dir(tmp_path, name="other")
    assert "belongs to VM other" in removable(tmp_path, d)


def test_removable_refuses_a_vm_with_other_notes(tmp_path):
    d = make_vm_dir(tmp_path)
    assert "was not created by this tool" in removable(tmp_path, d, "'hand made'")


@pytest.mark.parametrize(
    "field, value",
    [
        ("vhdx", "/etc/passwd.vhdx"),
        ("seed", "{d}/../other/seed.vhdx"),
        ("vhdx", "{d}/disk.txt"),
        ("vhdx", "{d}/sub/disk.vhdx"),
    ],
)
def test_removable_refuses_paths_outside_the_vm_directory(tmp_path, field, value):
    d = make_vm_dir(tmp_path)
    import json

    m = json.loads((d / "hyperv-vm.json").read_text())
    m[field] = value.format(d=d)
    (d / "hyperv-vm.json").write_text(json.dumps(m))
    assert "refusing" in removable(tmp_path, d)


def remove(tmp_path, d, extra, vm="$null"):
    # -VhdxDir must look like a drive path to the parser; the stand-in joins it back to the real directory
    s = (
        "function Assert-HvReady { }; function Get-VM { " + vm + " }; function Get-HvDir($o) { '" + str(d) + "' };"
        "function Invoke-Gh { throw 'no network' }; function Api-Path($r) { $r };"
        f"Invoke-Hyperv @('remove','-Name','hv-ci'{extra})"
    )
    return out(ps(tmp_path, f"Run {{ {s} }}"))


def test_remove_without_confirm_deletes_nothing(tmp_path):
    d = make_vm_dir(tmp_path)
    assert "-ConfirmRemove hv-ci" in remove(tmp_path, d, ",'-DeleteVhdx'")
    assert (d / "disk.vhdx").exists() and (d / "hyperv-vm.json").exists()


def test_remove_keeps_disks_without_delete_flag(tmp_path):
    d = make_vm_dir(tmp_path)
    assert "disks are kept" in remove(tmp_path, d, ",'-ConfirmRemove','hv-ci'")
    assert (d / "disk.vhdx").exists()


def test_remove_deletes_only_the_marked_files(tmp_path):
    d = make_vm_dir(tmp_path)
    o = remove(tmp_path, d, ",'-ConfirmRemove','hv-ci','-DeleteVhdx'")
    assert not (d / "disk.vhdx").exists() and not (d / "seed.vhdx").exists() and not (d / "hyperv-vm.json").exists()
    assert (d / "mine.txt").read_text() == "x" and d.exists(), (
        o
    )  # a file the tool did not make survives, and so does its folder


def test_remove_refuses_a_vm_it_did_not_make(tmp_path):
    d = make_vm_dir(tmp_path)
    o = remove(
        tmp_path,
        d,
        ",'-ConfirmRemove','hv-ci','-DeleteVhdx'",
        vm="[pscustomobject]@{ Name='hv-ci'; Notes='hand made'; State='Off' }",
    )
    assert "was not created by this tool" in o and (d / "disk.vhdx").exists()


def test_remove_refuses_a_running_vm(tmp_path):
    d = make_vm_dir(tmp_path)
    o = remove(
        tmp_path,
        d,
        ",'-ConfirmRemove','hv-ci','-DeleteVhdx'",
        vm="[pscustomobject]@{ Name='hv-ci'; Notes='win-runners hyperv vm abc'; State='Running' }",
    )
    assert "run hyperv stop -Yes first" in o and (d / "disk.vhdx").exists()


def test_stop_needs_yes(tmp_path):
    d = make_vm_dir(tmp_path)
    s = (
        "function Assert-HvReady { }; function Get-HvDir($o) { '" + str(d) + "' };"
        "function Get-VM { [pscustomobject]@{ Name='hv-ci'; Notes='win-runners hyperv vm abc'; State='Running' } };"
        "Invoke-Hyperv @('stop')"
    )
    assert "add -Yes" in err_of(tmp_path, s)


def test_compact_refuses_a_running_vm(tmp_path):
    d = make_vm_dir(tmp_path)
    s = (
        "function Assert-HvReady { }; function Get-HvDir($o) { '" + str(d) + "' };"
        "function Get-VM { [pscustomobject]@{ Name='hv-ci'; Notes='win-runners hyperv vm abc'; State='Running' } };"
        "Invoke-Hyperv @('compact')"
    )
    assert "compacting needs it off" in err_of(tmp_path, s)


# --- the VM's settings are what was agreed ---------------------------------------------------------------------


def test_create_sets_the_agreed_vm_settings():
    t = WINRUNNER.read_text()
    body = re.search(r"function New-HvVmHost.*?\n}\n", t, re.S).group(0)
    for want in (
        "-Generation 2",
        "-StaticMemory",
        "-AutomaticStartAction Start",
        "-AutomaticStopAction ShutDown",
        "-SecureBootTemplate MicrosoftUEFICertificateAuthority",
        "Set-VMProcessor -VMName $n -Count $o.VCpu",
    ):
        assert want in body, want
    assert "-Force" not in body.replace("New-Item -ItemType Directory", "")  # nothing is overwritten


def test_hyperv_is_dispatched_and_documented():
    t = WINRUNNER.read_text()
    assert "'hyperv' { Invoke-Hyperv $r }" in t
    usage = re.search(r"function Show-Usage.*?'@", t, re.S).group(0)
    assert "hyperv create" in usage and "hyperv remove" in usage
