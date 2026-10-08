"""win/winrunner.ps1's `hyperv` command group (the opt-in Hyper-V VM host), run in PowerShell 7 where it is installed
(CI installs it). Hyper-V itself cannot run here: the argument parser, the cloud-init seed text and the refusals
(not elevated, no Hyper-V, missing confirm flag, a disk this tool did not make) are tested with the real functions, and
the Hyper-V cmdlets are stand-ins. Nothing here proves a VM boots; see docs/windows.md."""

import base64
import json
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
    "Api-Path",
    "Test-OnGitHub",
    "ConvertFrom-HvArgs",
    "Get-HvDir",
    "Assert-HvReady",
    "Read-HvMarker",
    "Get-HvToolVms",
    "Resolve-HvTarget",
    "Get-HvRemovable",
    "ConvertTo-Lf",
    "ConvertTo-B64",
    "New-HvFirstBoot",
    "New-HvUserData",
    "Read-HvSshKey",
    "Get-HvWslMemoryGB",
    "Get-HvGuestBusy",
    "Assert-HvSpace",
    "Test-HvPowerWatchConflict",
    "Get-HvPrefix",
    "Get-NextHvName",
    "Get-HvMissingRunners",
    "Get-HvHeartbeat",
    "Remove-HvSeed",
    "Wait-HvReady",
    "Undo-HvCreate",
    "New-HvVmHost",
    "Show-HvStatus",
    "Set-HvPower",
    "Remove-HvGhRunner",
    "Remove-HvVmHost",
    "Invoke-HvCompact",
    "Invoke-HvEjectSeed",
    "Invoke-Hyperv",
]
VARS = ["HvDefaultDir", "HvMarkerName", "HvNotesPrefix", "HvOptions", "HvFootprintGB", "HvHeadroomGB", "HvWarnFreeGB"]
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
    assert "hv-ci 4 16 16 2 linux-ci example-org" in o


def test_default_ramdisk_and_cache_follow_the_ram(tmp_path):
    s = (
        "foreach ($a in @('-RamGB','16'), @('-RamGB','4'), @('-RamGB','8'), @('-RamGB','8','-Runners','3'), @('-RamGB','4','-RamdiskMB','0'), @('-CacheMB','0'), @('-RamGB','16','-RamdiskMB','1024')) {"
        " $o = ConvertFrom-HvArgs 'create' $a; '{0}:{1}' -f $o.RamdiskMB, $o.CacheMB }"
    )
    # 16 GB: fits; 4 GB: 2 x 2048 + 1024 is over 65%, so off; 8 GB: fits; 8 GB x 3 runners: over, off; explicit 0 stays 0; explicit sizes are kept
    assert out(ps(tmp_path, s)).split() == [
        "2048:1024",
        "0:1024",
        "2048:1024",
        "0:1024",
        "0:1024",
        "2048:0",
        "1024:1024",
    ]


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
        ("'-DiskGB','5'", "-DiskGB is 16 to 4000"),
        ("'-Name','bad name'", "-Name is letters"),
        ("'-VhdxDir','relative\\dir'", "-VhdxDir is a full local path"),
        ("'-VhdxDir','\\\\server\\share'", "-VhdxDir is a full local path"),
        ("'-Tags','docker'", "must include linux-ci"),
        ("'-Tags','linux-ci;rm'", "-Tags is a comma-separated"),
        ("'-AdminRepo','nope'", "-AdminRepo is OWNER/REPO"),
        ("'-CoresPerJob','9'", "-CoresPerJob is 0"),
        ("'-RamdiskMB','100'", "-RamdiskMB is 0"),
        ("'-RamGB','4','-RamdiskMB','4096'", "over 65%"),
        ("'-RamdiskMB','6000'", "over 65%"),
        ("'-CacheMB','100'", "-CacheMB is 0"),
        ("'-RamGB','4','-CacheMB','4000'", "over 65%"),
        ("'-WaitMin','999'", "-WaitMin is 0"),
        ("'-VhdxDir','D:vm'", "-VhdxDir is a full local path"),
        ("'-VhdxDir','D:'", "-VhdxDir is a full local path"),
    ],
)
def test_bad_options_are_refused(tmp_path, words, msg):
    assert msg in err_of(tmp_path, f"ConvertFrom-HvArgs 'create' @({words})")


@pytest.mark.parametrize(
    "given, want",
    [("D:\\vm\\", "D:\\vm"), ("e:/vm/x//", "e:/vm/x"), ("C:\\Hyper-V\\win runners", "C:\\Hyper-V\\win runners")],
)
def test_windows_paths_are_accepted_and_trimmed(tmp_path, given, want):
    o = out(ps(tmp_path, f"(ConvertFrom-HvArgs 'create' @('-VhdxDir','{given}')).VhdxDir"))
    assert o.strip() == want


def test_ramdisk_within_65_percent_is_accepted(tmp_path):
    o = out(ps(tmp_path, "(ConvertFrom-HvArgs 'create' @('-RamGB','16','-Runners','2','-RamdiskMB','4500')).RamdiskMB"))
    assert o.strip() == "4500"


# --- the PC's other memory and power ----------------------------------------------------------------------------


def test_wsl_memory_cap(tmp_path):
    s = (
        "'{0} {1} {2} {3} {4}' -f (Get-HvWslMemoryGB $false @() 64), (Get-HvWslMemoryGB $true @() 64),"
        ' (Get-HvWslMemoryGB $true @("[wsl2]`nmemory=8GB") 64), (Get-HvWslMemoryGB $true @(\'memory = 4096MB\', "memory=12G") 64),'
        " (Get-HvWslMemoryGB $true @('# memory=99GB') 64)"
    )
    assert out(ps(tmp_path, s)).strip() == "0 32 8 12 32"


@pytest.mark.parametrize("task, battery, want", [(True, True, "True"), (True, False, "False"), (False, True, "False")])
def test_power_watch_conflict_needs_the_task_and_a_battery(tmp_path, task, battery, want):
    s = (
        f"function Get-ScheduledTask {{ param($TaskName, $ErrorAction) if ('{task}' -eq 'True') {{ 1 }} }};"
        f"function Get-CimInstance {{ param($c, $ErrorAction) if ('{battery}' -eq 'True') {{ 1 }} }};"
        "$TaskName = 'win-runners power watch'; Test-HvPowerWatchConflict"
    )
    assert out(ps(tmp_path, s)).strip() == want


# --- GitHub name lookups walk every page --------------------------------------------------------------------------

PAGES = """
function Invoke-Gh([string]$m, [string]$path) {
  $page = [int]($path -replace '.*page=', '')
  $total = %TOTAL%
  $n = [math]::Min(100, $total - ($page - 1) * 100)
  $names = if ($n -gt 0) { 1..$n | ForEach-Object { 'r' + (($page - 1) * 100 + $_) } } else { @() }
  [pscustomobject]@{ total_count = $total; runners = @($names | ForEach-Object { [pscustomobject]@{ name = $_ } }) }
}
"""


def test_name_on_a_later_page_is_found(tmp_path):
    s = (
        PAGES.replace("%TOTAL%", "250")
        + "'{0} {1}' -f (Test-OnGitHub 'example-org' 'r230'), (Test-OnGitHub 'example-org' 'nope')"
    )
    assert out(ps(tmp_path, s)).strip() == "True False"


def test_unreadable_listing_is_unknown_not_free(tmp_path):
    s = PAGES.replace("%TOTAL%", "999999") + "$null -eq (Test-OnGitHub 'example-org' 'nope')"
    assert out(ps(tmp_path, s)).strip() == "True"


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


# --- a VM of this tool: found by its notes, its folder by its own disk -------------------------------------------


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
        "runners": ["win-1-hv-1", "hv-1-admin"],
    }
    m.update(marker)
    (d / "hyperv-vm.json").write_text(json.dumps(m))
    return d


def vm(d, state="Off", notes="win-runners hyperv vm abc", disk=None, seed=True):
    """Hyper-V stand-ins for one VM whose disks live in D. Get-HvDir is NOT stubbed: the folder comes from the VM's own disk."""
    disk = disk or f"{d}/disk.vhdx"
    seed_drive = (
        f", [pscustomobject]@{{ Path = '{d}/seed.vhdx'; ControllerType = 'SCSI'; ControllerNumber = 0; ControllerLocation = 1 }}"
        if seed
        else ""
    )
    return (
        "function Assert-HvReady { };"
        f"function Get-VM {{ param($Name, $ErrorAction) $v = [pscustomobject]@{{ Name = 'hv-ci'; Notes = '{notes}'; State = '{state}' }}; if (-not $Name -or $Name -eq 'hv-ci') {{ $v }} }};"
        f"function Get-VMHardDiskDrive {{ param($VMName) [pscustomobject]@{{ Path = '{disk}'; ControllerType = 'SCSI'; ControllerNumber = 0; ControllerLocation = 0 }}{seed_drive} }};"
        'function Remove-VMHardDiskDrive { param($VMName, $ControllerType, $ControllerNumber, $ControllerLocation) Write-Output "DETACH $ControllerLocation" };'
        'function Remove-VM { param($Name, [switch]$Force) Write-Output "REMOVEVM $Name" };'
        "function Start-Sleep { param($Seconds) };"
        "function Invoke-Gh { throw 'no network' };"
    )


def run(tmp_path, d, args, **kw):
    return out(ps(tmp_path, f"Run {{ {vm(d, **kw)} Invoke-Hyperv @({args}) }}"))


def test_remove_needs_the_typed_name(tmp_path):
    d = make_vm_dir(tmp_path)
    o = run(tmp_path, d, "'remove','-DeleteVhdx'")  # no -VhdxDir, no -Name: found through the VM
    assert "-ConfirmRemove hv-ci" in o and "REMOVEVM" not in o
    assert (d / "disk.vhdx").exists() and (d / "hyperv-vm.json").exists()


def test_vm_is_found_by_notes_and_folder_from_its_own_disk(tmp_path):
    d = make_vm_dir(tmp_path)
    o = run(tmp_path, d, "'remove','-ConfirmRemove','hv-ci'")  # -VhdxDir would point at D:\hv, which does not exist
    assert "REMOVEVM hv-ci" in o and "disks are kept" in o and str(d) in o
    assert (d / "disk.vhdx").exists()


def test_remove_deletes_only_the_marked_files(tmp_path):
    d = make_vm_dir(tmp_path)
    o = run(tmp_path, d, "'remove','-ConfirmRemove','hv-ci','-DeleteVhdx'")
    assert not (d / "disk.vhdx").exists() and not (d / "seed.vhdx").exists() and not (d / "hyperv-vm.json").exists()
    assert (d / "mine.txt").read_text() == "x" and d.exists(), (
        o
    )  # a file the tool did not make survives, and so does its folder


def test_remove_after_the_vm_is_gone_uses_vhdxdir(tmp_path):
    d = make_vm_dir(tmp_path)
    s = (
        "function Assert-HvReady { }; function Get-VM { }; function Start-Sleep { param($Seconds) }; function Invoke-Gh { throw 'no network' };"
        f"function Get-HvDir($o) {{ '{d}' }};"  # only the Linux/Windows path spelling differs: -VhdxDir D:\\... cannot name a temp dir here
        "Invoke-Hyperv @('remove','-VhdxDir','D:\\hv','-ConfirmRemove','hv-ci','-DeleteVhdx')"
    )
    out(ps(tmp_path, f"Run {{ {s} }}"))
    assert not (d / "disk.vhdx").exists() and (d / "mine.txt").exists()


def test_remove_refuses_a_vm_it_did_not_make(tmp_path):
    d = make_vm_dir(tmp_path)
    o = run(tmp_path, d, "'remove','-Name','hv-ci','-ConfirmRemove','hv-ci','-DeleteVhdx'", notes="hand made")
    assert "was not created by this tool" in o
    assert (d / "disk.vhdx").exists()


def test_a_marker_for_another_disk_is_not_trusted(tmp_path):
    d = make_vm_dir(tmp_path)
    o = run(tmp_path, d, "'remove','-ConfirmRemove','hv-ci','-DeleteVhdx'", disk=f"{d}/elsewhere/disk.vhdx")
    assert "was not created by this tool" in o and (d / "disk.vhdx").exists()


def test_remove_refuses_a_running_vm(tmp_path):
    d = make_vm_dir(tmp_path)
    o = run(tmp_path, d, "'remove','-ConfirmRemove','hv-ci','-DeleteVhdx'", state="Running")
    assert "run hyperv stop -Yes first" in o and (d / "disk.vhdx").exists()


def test_stop_needs_yes(tmp_path):
    d = make_vm_dir(tmp_path)
    assert "add -Yes" in run(tmp_path, d, "'stop'", state="Running")


COMPACT = """
$global:hvstate = '%STATE%'
function Get-VM { param($Name, $ErrorAction) [pscustomobject]@{ Name = 'hv-ci'; Notes = 'win-runners hyperv vm abc'; State = $global:hvstate } }
function Stop-VM { param($Name)
  Write-Host 'STOP'
  if ('%STOP%' -eq 'fail') { throw 'stop failed' }
  if ('%STOP%' -eq 'failoff') { $global:hvstate = 'Off'; throw 'stop timed out' }
  if ('%STOP%' -ne 'stuck') { $global:hvstate = 'Off' } }
function Start-VM { param($Name) Write-Host 'START'; $global:hvstate = 'Running' }
function Get-VHD { [pscustomobject]@{ FileSize = 6GB } }
function Mount-VHD { Write-Host 'MOUNT' }
function Dismount-VHD { Write-Host 'DISMOUNT' }
function Optimize-VHD { param($Path, $Mode) Write-Host "OPT $Mode"; if ('%OPT%' -eq 'fail') { throw 'optimize failed' } }
function Get-VMIntegrationService { [pscustomobject]@{ PrimaryStatusDescription = '%HB%' } }
function Test-OnGitHub { %ON% }
function Get-HvGuestBusy($vm) { %BUSY% }
"""


def compact(
    tmp_path,
    args="'compact','-Yes'",
    state="Running",
    stop="ok",
    opt="ok",
    hb="OK",
    on="$true",
    busy="$false",
    seed=False,
):
    d = make_vm_dir(tmp_path)
    c = (
        COMPACT.replace("%STATE%", state)
        .replace("%STOP%", stop)
        .replace("%OPT%", opt)
        .replace("%HB%", hb)
        .replace("%ON%", on)
        .replace("%BUSY%", busy)
    )
    o = out(ps(tmp_path, f"Run {{ {vm(d, state=state, seed=seed)} {c} Invoke-Hyperv @({args}) }}"))
    return o, [k for k in o.splitlines() if k in ("STOP", "MOUNT", "OPT Full", "DISMOUNT", "START")]


def test_compact_with_yes_stops_compacts_and_restarts(tmp_path):
    o, steps = compact(tmp_path)
    assert steps == ["STOP", "MOUNT", "OPT Full", "DISMOUNT", "START"], o
    assert "started again" in o


def test_compact_restarts_the_vm_even_when_it_fails(tmp_path):
    o, steps = compact(tmp_path, opt="fail")
    assert "optimize failed" in o and steps == ["STOP", "MOUNT", "OPT Full", "DISMOUNT", "START"]


def test_a_failing_stop_vm_does_not_touch_the_disk_or_start_anything(tmp_path):
    o, steps = compact(tmp_path, stop="fail")
    assert "stop failed" in o and steps == ["STOP"]  # still running: nothing to start, nothing mounted


def test_a_stop_that_fails_after_the_vm_is_off_still_restarts_it(tmp_path):
    o, steps = compact(tmp_path, stop="failoff")
    assert "stop timed out" in o and steps == ["STOP", "START"]  # restart was decided before Stop-VM


def test_a_vm_that_is_not_off_is_never_mounted(tmp_path):
    o, steps = compact(tmp_path, stop="stuck")
    assert "did not shut down" in o and steps == ["STOP"]


@pytest.mark.parametrize(
    "kw, msg",
    [
        ({"hb": "No Contact"}, "no OK heartbeat"),
        ({"on": "$false"}, "runners not on GitHub yet"),
        ({"on": "$null"}, "could not ask GitHub"),
        ({"seed": True}, "seed disk is still attached"),
        ({"busy": "$true"}, "a job is running in the VM"),
        ({"args": "'compact'"}, "add -Yes to stop it"),
    ],
)
def test_compact_refuses_a_vm_that_is_not_ready(tmp_path, kw, msg):
    o, steps = compact(tmp_path, **kw)
    assert msg in o and steps == []


def test_a_busy_vm_is_refused_even_with_yes(tmp_path):
    _, steps = compact(tmp_path, busy="$true")
    assert steps == []


def test_an_unreachable_guest_is_warned_about_and_needs_yes(tmp_path):
    o, steps = compact(tmp_path, busy="$null")
    assert "cannot look inside the guest" in o and steps[0] == "STOP"


def test_compact_on_a_stopped_vm_needs_no_yes_and_does_not_start_it(tmp_path):
    o, steps = compact(tmp_path, args="'compact'", state="Off")
    assert steps == ["MOUNT", "OPT Full", "DISMOUNT"], o


def test_eject_seed_works_on_a_running_vm(tmp_path):
    d = make_vm_dir(tmp_path)
    o = run(tmp_path, d, "'eject-seed'", state="Running")
    assert "DETACH 1" in o and not (d / "seed.vhdx").exists() and (d / "disk.vhdx").exists()


# --- status: runners on GitHub, then the seed goes -----------------------------------------------------------------

STATUS = (
    "function Get-VMIntegrationService { [pscustomobject]@{ PrimaryStatusDescription = '%HB%' } };"
    "function Get-VMNetworkAdapter { [pscustomobject]@{ IPAddresses = @('192.0.2.5') } };"
    "function Get-VHD { [pscustomobject]@{ FileSize = 2GB; Size = 100GB } };"
    "function Test-OnGitHub { %ON% };"
)


def status(tmp_path, d, hb="OK", on="$true"):
    pre = STATUS.replace("%HB%", hb).replace("%ON%", on)
    return out(ps(tmp_path, f"Run {{ {vm(d, state='Running')} {pre} Invoke-Hyperv @('status') }}"))


def test_status_ejects_the_seed_once_up_and_registered(tmp_path):
    d = make_vm_dir(tmp_path)
    o = status(tmp_path, d)
    assert "all on GitHub" in o and "DETACH 1" in o and not (d / "seed.vhdx").exists()


def test_status_keeps_the_seed_while_a_runner_is_missing(tmp_path):
    d = make_vm_dir(tmp_path)
    o = status(tmp_path, d, on="$false")
    assert "not on GitHub yet: win-1-hv-1, hv-1-admin" in o and "DETACH" not in o and (d / "seed.vhdx").exists()


def test_status_keeps_the_seed_without_a_heartbeat(tmp_path):
    d = make_vm_dir(tmp_path)
    o = status(tmp_path, d, hb="No Contact")
    assert "DETACH" not in o and (d / "seed.vhdx").exists()


# --- unregistering retries while the runner is still online ---------------------------------------------------------


def test_unregister_waits_for_offline_then_deletes(tmp_path):
    s = (
        "function Start-Sleep { param($Seconds) }; function Api-Path($r) { $r }; $script:n = 0;"
        "function Invoke-Gh([string]$m, [string]$p) {"
        "  if ($m -eq 'DELETE') { Write-Host \"DELETE $p\"; return }"
        "  $script:n++; $st = if ($script:n -lt 3) { 'online' } else { 'offline' };"
        "  [pscustomobject]@{ runners = @([pscustomobject]@{ name = 'win-1-hv-1'; id = 7; status = $st }) } };"
        "Remove-HvGhRunner 'example-org' 'win-1-hv-1'"
    )
    o = out(ps(tmp_path, s))
    assert "DELETE example-org/actions/runners/7" in o and "unregistered win-1-hv-1" in o


def test_unregister_gives_up_with_a_message(tmp_path):
    s = (
        "function Start-Sleep { param($Seconds) }; function Api-Path($r) { $r };"
        "function Invoke-Gh([string]$m, [string]$p) { [pscustomobject]@{ runners = @([pscustomobject]@{ name = 'x'; id = 7; status = 'online' }) } };"
        "Remove-HvGhRunner 'example-org' 'x'"
    )
    assert "still online on GitHub" in out(ps(tmp_path, s))


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


def test_firstboot_removes_the_seed_files_even_on_failure_and_checks_hv_utils(tmp_path):
    o = out(ps(tmp_path, CLOUD + "$fb"))
    assert "trap 'cd /; rm -rf /root/hv-seed' EXIT" in o  # an EXIT trap: runs after a failed provision too
    assert o.index("trap 'cd /; rm -rf") < o.index("bash ./linux-provision.sh")
    assert (
        "modinfo hv_utils" in o
        and "linux-modules-extra" in o
        and "linux-azure" in o
        and "linux-cloud-tools-virtual" in o
    )
    assert "hv_utils" in o.split("modules-load.d")[0].splitlines()[-1] or "modules-load.d/hyperv.conf" in o


def test_firstboot_leaves_a_small_disk(tmp_path):
    o = out(ps(tmp_path, CLOUD + "$fb"))
    assert "swapoff -a" in o and "rm -f /swapfile" in o  # no swap file in the guest
    assert 'sed -i "/^[^#]*\\sswap\\s/d" /etc/fstab' in o  # only a live fstab line, not a comment
    assert o.index("swapoff -a") < o.index("bash ./linux-provision.sh")
    end = o[o.index("add-runner") :]
    assert (
        end.index("apt-get clean; rm -rf /var/lib/apt/lists/*")
        < end.index("fstrim -av")
        < end.index('echo "firstboot done"')
    )
    assert "systemctl enable --now fstrim.timer" in o
    assert o.index("rm -rf /root/hv-seed", o.index("add-runner")) < o.index("fstrim -av")


def test_tool_caches_live_on_a_capped_tmpfs_for_the_ci_runners_only(tmp_path):
    o = out(ps(tmp_path, CLOUD + "$fb"))
    assert "DefaultEnvironment" not in o and "system.conf.d" not in o and "daemon-reexec" not in o  # nothing global
    assert "XDG_CACHE_HOME" not in o
    mount = next(k for k in o.splitlines() if "mnt-cicache.mount" in k and "printf" in k)
    assert (
        "What=tmpfs" in mount
        and "Where=/mnt/cicache" in mount
        and "size=1024M" in mount
        and "uid=%s" in mount
        and "id -u runner" in mount
    )
    envs = [k for k in o.splitlines() if "/.env" in k]
    assert len(envs) == 2 and all(
        "UV_CACHE_DIR=/mnt/cicache/uv" in k and "PIP_CACHE_DIR=/mnt/cicache/pip" in k and "UV_LINK_MODE=copy" in k
        for k in envs
    )
    assert (
        "runners/win-1-hv-1/.env" in envs[0] and "runners/win-1-hv-2/.env" in envs[1] and "admin" not in "".join(envs)
    )
    assert o.index("enable --now mnt-cicache.mount") < o.index("add-runner") < o.index("/.env") < o.index("$LR restart")


def test_no_cache_disk_when_cachemb_is_zero(tmp_path):
    o = out(
        ps(
            tmp_path,
            "$o = ConvertFrom-HvArgs 'create' @('-CacheMB','0'); New-HvFirstBoot $o 'hv-1' 'win-1' @('win-1-hv-1')",
        )
    )
    assert "mnt-cicache" not in o and "UV_CACHE_DIR" not in o


def test_the_generated_first_boot_script_is_valid_bash(tmp_path):
    f = tmp_path / "firstboot.sh"
    f.write_text(out(ps(tmp_path, CLOUD + "$fb")))
    assert subprocess.run(["bash", "-n", str(f)], capture_output=True, text=True).returncode == 0
    sc = shutil.which("shellcheck")
    if sc is None:
        pytest.skip("shellcheck not installed")
    r = subprocess.run([sc, "-S", "warning", str(f)], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout


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


# --- removable: nothing outside the marker ------------------------------------------------------------------------


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
    assert "did not create a VM there" in removable(tmp_path, make_vm_dir(tmp_path, tool="someone-else"))


def test_removable_refuses_another_vms_marker(tmp_path):
    assert "belongs to VM other" in removable(tmp_path, make_vm_dir(tmp_path, name="other"))


def test_removable_refuses_a_vm_with_other_notes(tmp_path):
    assert "was not created by this tool" in removable(tmp_path, make_vm_dir(tmp_path), "'hand made'")


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
    m = json.loads((d / "hyperv-vm.json").read_text())
    m[field] = value.format(d=d)
    (d / "hyperv-vm.json").write_text(json.dumps(m))
    assert "refusing" in removable(tmp_path, d)


# --- create: order of checks, folder lock-down, rollback ---------------------------------------------------------

CREATE = """
$env:SystemDrive = '%SYS%'
$AppOrg = 'example-org'; $TaskName = 'win-runners power watch'
Set-Content -Path $HostFile -Value 'win-1'
Set-Content -Path (Join-Path $HomeDir 'linuxrunner') -Value '#!/bin/bash'
Set-Content -Path (Join-Path $HomeDir 'linux-provision.sh') -Value '#!/bin/bash'
$global:made = $false
function Assert-HvReady { }
function Test-HvPowerWatchConflict { %CONFLICT% }
function Get-VM { param($Name, $ErrorAction) if ($global:made -and (-not $Name -or $Name -eq 'hv-ci')) { [pscustomobject]@{ Name = 'hv-ci'; Notes = $global:notes; State = 'Running' } } }
function Get-HvDir($o) { '%DIR%' }
function Get-CimInstance { [pscustomobject]@{ TotalPhysicalMemory = 64GB } }
function Get-TotalCores { 16 }
function Get-WslConfigTexts { }
function Get-SlotConfig { $null }
function Get-PSDrive { [pscustomobject]@{ Free = %FREE%GB } }
function Get-VMSwitch { 1 }
function Resolve-HvImageSource($o) { Write-Host 'STEP image'; if ('%BADIMG%' -eq '1') { Die 'no such image' }; if ($o.Image) { return $o.Image }; $f = Join-Path '%DL%' 'dl.img'; Set-Content -Path $f -Value img; $f }
function Test-OnGitHub { $false }
function New-GhToken { Write-Host 'STEP token'; 'tok' }
function Protect-HvDir([string]$dir) { Write-Host "STEP acl dir=$(Test-Path $dir) marker=$(Test-Path (Join-Path $dir 'hyperv-vm.json')) seed=$(Test-Path (Join-Path $dir 'seed.vhdx'))" }
function New-HvDisk($o, $src, $dir) { Write-Host 'STEP disk'; Set-Content -Path (Join-Path $dir 'disk.vhdx') -Value d; Join-Path $dir 'disk.vhdx' }
function New-HvSeedDisk($path, $ud, $md) { Write-Host 'STEP seed'; if ('%FAIL%' -eq 'seed') { throw 'seed failed' }; Set-Content -Path $path -Value s }
function New-VM { param($Name, $Generation, $MemoryStartupBytes, $VHDPath, $SwitchName, $Path) Write-Host 'STEP newvm'; $global:made = $true; New-Item -ItemType Directory -Force -Path $Path | Out-Null }
function Set-VM { param($Name, [switch]$StaticMemory, $AutomaticStartAction, $AutomaticStartDelay, $AutomaticStopAction, $CheckpointType, $Notes)
  $global:notes = $Notes; Write-Output "SETVM $AutomaticStartAction/$AutomaticStartDelay/$AutomaticStopAction static=$StaticMemory $CheckpointType" }
function Set-VMProcessor { param($VMName, $Count) Write-Output "CPU $Count" }
function Set-VMFirmware { param($VMName, $EnableSecureBoot, $SecureBootTemplate) if ('%FAIL%' -eq 'firmware') { throw 'firmware failed' }; Write-Output "FW $EnableSecureBoot $SecureBootTemplate" }
function Add-VMHardDiskDrive { param($VMName, $Path) Write-Host 'STEP attach' }
function Start-VM { param($Name) Write-Host 'STEP start' }
function Stop-VM { param($Name, [switch]$TurnOff, [switch]$Force, $ErrorAction) Write-Output "STOPVM $Name" }
function Remove-VM { param($Name, [switch]$Force) Write-Output "REMOVEVM $Name"; $global:made = $false }
"""


def create(tmp_path, args="", sys="C:", conflict="$false", free=500, badimg="0", fail="", extra=""):
    base = tmp_path / "vmdir"
    base.mkdir(exist_ok=True)
    target = base / "hv-ci"
    (base / "precious.txt").write_text("keep")
    pre = (
        CREATE.replace("%SYS%", sys)
        .replace("%CONFLICT%", conflict)
        .replace("%DIR%", str(target))
        .replace("%FREE%", str(free))
        .replace("%BADIMG%", badimg)
        .replace("%FAIL%", fail)
        .replace("%DL%", str(tmp_path))
    )
    r = ps(
        tmp_path,
        pre
        + extra
        + f"Run {{ Invoke-Hyperv @('create','-Yes','-AdminRepo','example-org/runnerpool','-WaitMin','0'{args}) }}",
    )
    return out(r), base, target


def test_create_locks_the_folder_before_anything_is_written_and_sets_the_vm(tmp_path):
    o, base, target = create(tmp_path)
    assert "STEP acl dir=True marker=False seed=False" in o
    assert (
        o.index("STEP image")
        < o.index("STEP token")
        < o.index("STEP acl")
        < o.index("STEP disk")
        < o.index("STEP seed")
        < o.index("STEP start")
    )
    assert "SETVM StartIfRunning/60/ShutDown static=True Disabled" in o
    assert "FW On MicrosoftUEFICertificateAuthority" in o and "CPU 4" in o
    assert json.loads((target / "hyperv-vm.json").read_text())["name"] == "hv-ci"


def test_create_checks_ssh_key_and_image_before_minting_tokens(tmp_path):
    o, base, target = create(tmp_path, ",'-SshKeyFile','" + str(tmp_path / "missing.pub") + "'")
    assert "no such file" in o and "STEP token" not in o and not target.exists()
    o, base, target = create(tmp_path, badimg="1")
    assert "no such image" in o and "STEP token" not in o and not target.exists()


def test_failed_create_rolls_back_only_what_it_made(tmp_path):
    o, base, target = create(tmp_path, fail="seed")
    assert "seed failed" in o and "removed what this run made" in o
    assert not target.exists() and (base / "precious.txt").read_text() == "keep"
    assert "REMOVEVM" not in o  # no VM existed yet


def test_failed_create_after_the_vm_exists_removes_the_vm_too(tmp_path):
    o, base, target = create(tmp_path, fail="firmware")
    assert "firmware failed" in o and "STOPVM hv-ci" in o and "REMOVEVM hv-ci" in o
    assert not target.exists() and (base / "precious.txt").exists()


def test_create_refuses_an_existing_folder_and_leaves_it_alone(tmp_path):
    base = tmp_path / "vmdir" / "hv-ci"
    base.mkdir(parents=True)
    (base / "mine.txt").write_text("x")
    o, _, target = create(tmp_path)
    assert "already exists" in o and "STEP token" not in o and (target / "mine.txt").read_text() == "x"


def test_create_on_the_system_drive_is_the_normal_case(tmp_path):
    o, _, target = create(tmp_path, sys="C:")  # no flag, nothing about the system drive
    assert "STEP start" in o and "system drive" not in o and target.exists()


def test_create_refuses_too_little_disk_space(tmp_path):
    o, _, target = create(tmp_path, free=12)  # footprint 8 GB + headroom 8 GB = 16 GB
    assert "the VM needs about 8 GB plus 8 GB headroom (16 GB)" in o and "STEP token" not in o and not target.exists()


def test_default_create_passes_at_26_gb_free(tmp_path):
    o, _, target = create(
        tmp_path, free=26
    )  # the default -DiskGB 16 <= 26 - 8 headroom; 26 - 16 = 10 is not under the warning line
    assert "STEP start" in o and "could grow past" not in o and "warning: drive" not in o and target.exists()


def test_create_refuses_a_disk_that_could_outgrow_free_space(tmp_path):
    o, _, target = create(tmp_path, ",'-DiskGB','30'", free=26)  # 26 - 8 headroom = 18 GB at most
    assert "could grow past the 26 GB free" in o and "the most is 18 GB" in o and not target.exists()


@pytest.mark.parametrize(
    "free, disk, refused, warned",
    [
        (15, 20, "needs about 8 GB", False),
        (26, 20, "could grow past", False),
        (26, 18, None, False),  # 26 - 16 = 10: not under 10
        (25, 16, None, True),  # 25 - 16 = 9: warn, not refuse
        (20, 10, None, True),
        (60, 20, None, False),
    ],
)
def test_space_verdicts(tmp_path, free, disk, refused, warned):
    o = err_of(tmp_path, f"Assert-HvSpace {free} {disk} 'C'")
    assert (refused in o) if refused else ("ERR" not in o)
    assert ("warning:" in o) == warned


def test_create_deletes_the_downloaded_image_unless_kept_or_supplied(tmp_path):
    o, _, _ = create(tmp_path)
    assert "deleted the downloaded cloud image" in o and not (tmp_path / "dl.img").exists()


def test_it_does_not_claim_a_deletion_that_did_not_happen(tmp_path):
    o, _, _ = create(tmp_path, extra="function Remove-Item { };")  # the delete silently does nothing
    assert "could not delete the downloaded cloud image" in o and "deleted the downloaded cloud image" not in o


def test_keep_image_keeps_it(tmp_path):
    o, _, _ = create(tmp_path, ",'-KeepImage'")
    assert "deleted the downloaded" not in o and (tmp_path / "dl.img").exists()


def test_a_supplied_image_is_never_deleted(tmp_path):
    mine = tmp_path / "mine.vhdx"
    mine.write_text("x")
    o, _, _ = create(tmp_path, f",'-Image','{mine}'")
    assert "STEP start" in o and mine.exists()


def test_a_failed_create_keeps_the_image_for_the_retry(tmp_path):
    create(tmp_path, fail="seed")
    assert (tmp_path / "dl.img").exists()


def test_create_refuses_on_a_laptop_with_the_power_watch_unless_ignored(tmp_path):
    o, _, target = create(tmp_path, conflict="$true")
    assert "-IgnorePowerWatch" in o and "STEP token" not in o and not target.exists()
    o, _, target = create(tmp_path, ",'-IgnorePowerWatch'", conflict="$true")
    assert "STEP start" in o


def test_create_warns_when_slots_and_vcpus_exceed_the_cpus(tmp_path):
    base = tmp_path / "vmdir"
    base.mkdir()
    pre = (
        CREATE.replace("%SYS%", "C:")
        .replace("%CONFLICT%", "$false")
        .replace("%DIR%", str(base / "hv-ci"))
        .replace("%FREE%", "500")
        .replace("%BADIMG%", "0")
        .replace("%FAIL%", "")
        .replace("%DL%", str(tmp_path))
        + "function Get-SlotConfig { @{ slots = 2; threads = 8 } };"
    )
    o = out(
        ps(
            tmp_path,
            pre
            + "Run { Invoke-Hyperv @('create','-Yes','-AdminRepo','example-org/runnerpool','-WaitMin','0','-VCpu','4') }",
        )
    )
    assert (
        "warning: job slots (2 x 8 threads) plus the VM's 4 vCPUs is more than the 16 logical processors" in o
        and "STEP start" in o
    )


# --- the source says what was agreed ---------------------------------------------------------------------------


def test_source_has_the_agreed_settings():
    t = WINRUNNER.read_text()
    body = re.search(r"function New-HvVmHost.*?\n}\n", t, re.S).group(0)
    for want in (
        "-Generation 2",
        "-StaticMemory",
        "-AutomaticStartAction StartIfRunning",
        "-AutomaticStartDelay 60",
        "-AutomaticStopAction ShutDown",
        "-SecureBootTemplate MicrosoftUEFICertificateAuthority",
    ):
        assert want in body, want
    assert body.index("Protect-HvDir") < body.index("Set-Content -LiteralPath (Join-Path $dir $HvMarkerName)")
    seed = re.search(r"function New-HvSeedDisk.*?\n}\n", t, re.S).group(0)
    assert "128MB" in seed and "AssignDriveLetter" not in seed and "Add-PartitionAccessPath" in seed
    img = re.search(r"function Resolve-HvImageSource.*?\n}\n", t, re.S).group(0)
    assert "Tls12" in img and '"$src.part"' in img and "Move-Item" in img


def test_hyperv_is_dispatched_and_documented():
    t = WINRUNNER.read_text()
    assert "'hyperv' { Invoke-Hyperv $r }" in t
    usage = re.search(r"function Show-Usage.*?'@", t, re.S).group(0)
    assert (
        "hyperv create" in usage
        and "hyperv remove" in usage
        and "-IgnorePowerWatch" in usage
        and "-KeepImage" in usage
        and "AllowSystemDrive" not in usage
    )
