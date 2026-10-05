"""win/winrunner.ps1's job slots, run in PowerShell 7 where it is installed (CI installs it): the hook
(acquire, wait when full, release), the pure helpers, stale clearing and the pause or resume of idle runners."""

import os
import re
import shutil
import subprocess
import time
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


def pwsh(script, env=None, timeout=60):
    return subprocess.run(
        [PWSH, "-NoProfile", "-NonInteractive", "-Command", script],
        env={**ENV, **(env or {})},
        capture_output=True,
        text=True,
        timeout=timeout,
        stdin=subprocess.DEVNULL,
    )


def hook_text():
    return re.search(r"\$SlotHookScript = @'\n(.*?)\n'@", WINRUNNER.read_text(), re.S).group(1)


def pc(tmp_path, slots=2, threads=4):
    pub = tmp_path / "public"
    (pub / "slots").mkdir(parents=True)
    (pub / "slots.conf").write_text(f"slots={slots}\nthreads={threads}\n")
    (tmp_path / "hook.ps1").write_text(hook_text())
    return pub


def hook(tmp_path, pub, action, name):
    env = {"SLOT_PUBLIC": str(pub), "GIT_RUNNER_NAME": name, "SLOT_POLL": "0.2"}
    return pwsh(f"& '{tmp_path / 'hook.ps1'}' {action}", env)


def test_hook_acquires_numbered_slots_and_release_frees(tmp_path):
    pub = pc(tmp_path)
    r = hook(tmp_path, pub, "acquire", "win-1")
    assert r.returncode == 0 and "got PC job slot 1 of 2" in r.stdout, r.stdout + r.stderr
    assert "got PC job slot 2 of 2" in hook(tmp_path, pub, "acquire", "win-1-ci-2").stdout
    owner = (pub / "slots/slot-1/owner").read_text()
    assert "runner=win-1" in owner and "side=win" in owner and "block=0" in owner
    assert hook(tmp_path, pub, "release", "win-1").returncode == 0
    assert not (pub / "slots/slot-1").exists() and (pub / "slots/slot-2").exists()


def test_hook_waits_when_full_and_takes_the_freed_slot(tmp_path):
    pub = pc(tmp_path)
    hook(tmp_path, pub, "acquire", "a")
    hook(tmp_path, pub, "acquire", "b")
    env = {**ENV, "SLOT_PUBLIC": str(pub), "GIT_RUNNER_NAME": "c", "SLOT_POLL": "0.2"}
    p = subprocess.Popen(
        [PWSH, "-NoProfile", "-NonInteractive", "-Command", f"& '{tmp_path / 'hook.ps1'}' acquire"],
        env=env,
        stdout=subprocess.PIPE,
        text=True,
        stdin=subprocess.DEVNULL,
    )
    time.sleep(3)
    assert p.poll() is None
    assert len(list((pub / "slots").glob("slot-*"))) == 2
    hook(tmp_path, pub, "release", "b")
    out, _ = p.communicate(timeout=30)
    assert "waiting for a PC job slot" in out and "got PC job slot 2 of 2" in out
    assert "runner=c" in (pub / "slots/slot-2/owner").read_text()


def test_hook_never_fails_the_job(tmp_path):
    pub = tmp_path / "nothing-here"
    (tmp_path / "hook.ps1").write_text(hook_text())
    r = hook(tmp_path, pub, "acquire", "x")
    assert r.returncode == 0
    r = pwsh(f"& '{tmp_path / 'hook.ps1'}' acquire", {"SLOT_PUBLIC": str(pub)})  # no runner name
    assert r.returncode == 0


FUNCS = [
    "Get-CpuMask",
    "Get-SlotConfig",
    "Get-HeldSlots",
    "Test-SlotHold",
    "Clear-StaleSlots",
    "Sync-RunState",
    "Test-LeaseExpired",
    "New-SlotFolder",
    "Write-HoldOwner",
    "Invoke-SlotHold",
    "Invoke-SlotRelease",
    "Invoke-SlotVmCommand",
]
PRELUDE = f"""
$ast = [Management.Automation.Language.Parser]::ParseFile('{WINRUNNER}', [ref]$null, [ref]$null)
foreach ($n in '{"','".join(FUNCS)}') {{
  $fn = $ast.Find({{ param($x) $x -is [Management.Automation.Language.FunctionDefinitionAst] -and $x.Name -eq $n }}, $true)
  Invoke-Expression $fn.Extent.Text
}}
function Log($m) {{ Write-Output "LOG $m" }}
"""


def test_cpu_masks_are_distinct_blocks_and_wrap():
    r = pwsh(
        PRELUDE
        + "'{0} {1} {2} {3} {4}' -f (Get-CpuMask 0 8 24), (Get-CpuMask 8 8 24), (Get-CpuMask 16 8 24), (Get-CpuMask 16 8 20), (Get-CpuMask 0 8 8)"
    )
    assert "255 65280 16711680 983055 " in r.stdout, r.stdout + r.stderr


def controller(tmp_path, held, busy, running, hold_slots=2, reason="$null", extra="", tail=""):
    """Slots `held` ({num: (runner, side, age)}), runners `busy`, services `running`; prints what it did."""
    slots = tmp_path / "slots"
    slots.mkdir(exist_ok=True)
    for num, (runner, side, age) in held.items():
        d = slots / f"slot-{num}"
        d.mkdir()
        (d / "owner").write_text(f"runner={runner}\nside={side}\n")
        t = time.time() - age
        os.utime(d, (t, t))
    (tmp_path / "slots.cfg").write_text(f"slots={hold_slots}\nthreads=4\n")
    names = ",".join(f"'{n}'" for n in running)
    script = (
        PRELUDE
        + f"""
$SlotDir = '{slots}'; $SlotsFile = '{tmp_path / "slots.cfg"}'
$script:running = @({names})
function Get-RunnerNames {{ 'win-1','win-1-ci-2' }}
function Test-Ci($n) {{ $true }}
function Get-CiPauseReason {{ {reason} }}
function Get-Service-For($n) {{ [pscustomobject]@{{ Status = $(if ($script:running -contains $n) {{ 'Running' }} else {{ 'Stopped' }}) }} }}
function Start-Runner($n) {{ Write-Output "START $n" }}
function Stop-Runner($n) {{ Write-Output "STOP $n" }}
$StartRetry = 600; $script:StartFailedAt = @{{}}
{extra}
$busy = @({",".join(f"'{n}'" for n in busy)})
Clear-StaleSlots $busy
Sync-RunState $busy
{tail}
Write-Output ("HELD " + (@(Get-HeldSlots).Count))
"""
    )
    return pwsh(script).stdout


def test_stale_windows_slot_is_cleared_but_busy_fresh_and_linux_ones_stay(tmp_path):
    out = controller(
        tmp_path,
        {1: ("win-1", "win", 100), 2: ("win-1-ci-2", "win", 100)},
        busy=["win-1-ci-2"],
        running=["win-1", "win-1-ci-2"],
        hold_slots=3,
    )
    assert "cleared stale slot 1" in out and "stale slot 2" not in out and "HELD 1" in out
    other = tmp_path / "b"
    other.mkdir()
    out = controller(other, {1: ("win-1", "win", 3), 2: ("wsl", "linux", 500)}, busy=[], running=[], hold_slots=3)
    assert "cleared" not in out and "HELD 2" in out


def test_full_slots_pause_idle_runner_never_the_busy_one(tmp_path):
    out = controller(
        tmp_path, {1: ("win-1", "win", 1), 2: ("wsl-x", "linux", 1)}, busy=["win-1"], running=["win-1", "win-1-ci-2"]
    )
    lines = out.splitlines()
    assert "STOP win-1-ci-2" in lines and "STOP win-1" not in lines


def test_free_slot_resumes_a_stopped_runner(tmp_path):
    out = controller(tmp_path, {1: ("win-1", "win", 1)}, busy=["win-1"], running=["win-1"])
    assert "START win-1-ci-2" in out


def test_battery_or_ci_off_wins_over_a_free_slot(tmp_path):
    out = controller(tmp_path, {}, busy=[], running=[], reason="'battery'")
    assert "START" not in out


def test_a_service_that_fails_to_start_is_left_alone_until_the_retry_wait_passes(tmp_path):
    out = controller(
        tmp_path,
        {},
        busy=[],
        running=[],
        extra='function Start-Runner($n) { Write-Output "START $n"; throw "logon failure" }',
        tail="Sync-RunState $busy; $script:StartFailedAt.Clear(); Sync-RunState $busy",
    )
    lines = out.splitlines()
    # first pass tries both runners and survives the failures; the second skips them; after the wait it retries
    assert lines.count("START win-1") == 2 and lines.count("START win-1-ci-2") == 2, out
    assert sum("could not start win-1 " in line for line in lines) == 2, out


def test_a_service_started_by_hand_forgets_its_failure_so_a_later_stop_is_recovered(tmp_path):
    out = controller(
        tmp_path,
        {},
        busy=[],
        running=[],
        extra='function Start-Runner($n) { Write-Output "START $n"; throw "logon failure" }',
        # the operator starts both services (`winrunner restart`), a poll sees them running, then they stop again
        tail="$script:running = @('win-1','win-1-ci-2'); Sync-RunState $busy; $script:running = @(); Sync-RunState $busy",
    )
    lines = out.splitlines()
    assert lines.count("START win-1") == 2 and lines.count("START win-1-ci-2") == 2, out


# --- something outside the runners holding a slot as a lease (slots-hold / slots-release) -----------------


def make_slot(slotdir, num, runner, side="win", age=100):
    d = slotdir / f"slot-{num}"
    d.mkdir(parents=True)
    (d / "owner").write_text(f"runner={runner}\nside={side}\nblock={num - 1}\n")
    t = time.time() - age
    os.utime(d, (t, t))


def hold_run(tmp_path, body, slots=3, env=None):
    """Runs `body` with $SlotDir / $SlotsFile pointed at tmp_path and the slot controller faked."""
    (tmp_path / "slots").mkdir(parents=True, exist_ok=True)
    if slots:
        (tmp_path / "slots.cfg").write_text(f"slots={slots}\nthreads=4\n")
    script = (
        PRELUDE
        + f"""
$SlotDir = '{tmp_path / "slots"}'; $SlotsFile = '{tmp_path / "slots.cfg"}'
function Invoke-SlotSync {{ Write-Host 'SYNC' }}
function Log($m) {{ Write-Host "LOG $m" }}
{body}
"""
    )
    return pwsh(script, {"SLOT_POLL": "0.2", **(env or {})})


def make_hold(slotdir, num, runner, lease=None, since=0, side="hold"):
    """A slot owned by `runner`, with its lease clock `since` seconds old (the folder itself is old too)."""
    make_slot(slotdir, num, runner, side=side)
    with open(slotdir / f"slot-{num}/owner", "a") as f:
        if lease is not None:
            f.write(f"lease={lease}\n")
        f.write(f"time={int(time.time() - since)}\n")


def owner(tmp_path, k):
    return dict(line.split("=", 1) for line in (tmp_path / f"slots/slot-{k}/owner").read_text().split())


def test_hold_takes_the_highest_free_slot_as_a_lease(tmp_path):
    r = hold_run(tmp_path, "$rc = Invoke-SlotVmCommand 'hold' @('-Name','claude-vm','-LeaseMin','7'); \"RC $rc\"")
    assert "RC 0" in r.stdout and "SYNC" in r.stdout, r.stdout + r.stderr
    o = owner(tmp_path, 3)
    assert o["side"] == "hold" and o["runner"] == "claude-vm" and o["lease"] == "7" and o["block"] == "2"
    assert {"pid", "time"} <= o.keys()


def test_holding_again_only_refreshes_the_lease_clock(tmp_path):
    make_hold(tmp_path / "slots", 3, "claude-vm", lease=5, since=400)
    before = int(owner(tmp_path, 3)["time"])
    r = hold_run(tmp_path, "$rc = Invoke-SlotVmCommand 'hold' @(); \"RC $rc\"")
    assert "RC 0" in r.stdout and "still holds slot 3" in r.stdout, r.stdout + r.stderr
    assert len(list((tmp_path / "slots").glob("slot-*"))) == 1
    assert int(owner(tmp_path, 3)["time"]) >= before + 300 and owner(tmp_path, 3)["lease"] == "10"


def test_hold_leaves_lower_slots_to_the_runners_and_skips_taken_ones(tmp_path):
    (tmp_path / "slots/slot-3").mkdir(parents=True)  # a runner (or anything) already has the top slot
    hold_run(tmp_path, "Invoke-SlotVmCommand 'hold' @('-Name','a') | Out-Null")
    assert (tmp_path / "slots/slot-2").is_dir() and not (tmp_path / "slots/slot-1").exists()


def test_hold_times_out_with_3_then_waits_forever_and_takes_a_freed_slot(tmp_path):
    for k in (1, 2, 3):
        (tmp_path / f"slots/slot-{k}").mkdir(parents=True)
    r = hold_run(tmp_path, "$rc = Invoke-SlotVmCommand 'hold' @('-TimeoutSec','1'); \"RC $rc\"")
    assert "RC 3" in r.stdout and "waiting for a free PC job slot" in r.stdout
    assert not (tmp_path / "slots/slot-3/owner").exists()
    p = subprocess.Popen(
        [
            PWSH,
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            PRELUDE
            + f"""
$SlotDir = '{tmp_path / "slots"}'; $SlotsFile = '{tmp_path / "slots.cfg"}'
function Invoke-SlotSync {{}}
$rc = Invoke-SlotVmCommand 'hold' @(); "RC $rc" """,
        ],
        env={**ENV, "SLOT_POLL": "0.2"},
        stdout=subprocess.PIPE,
        text=True,
        stdin=subprocess.DEVNULL,
    )
    time.sleep(2)
    assert p.poll() is None  # forever means forever
    (tmp_path / "slots/slot-1").rmdir()
    out, _ = p.communicate(timeout=30)
    assert "RC 0" in out and owner(tmp_path, 1)["runner"] == "claude-vm"


def test_hold_with_slots_off_succeeds_and_does_nothing(tmp_path):
    r = hold_run(tmp_path, "$rc = Invoke-SlotVmCommand 'hold' @(); \"RC $rc\"", slots=0)
    assert "RC 0" in r.stdout and not list((tmp_path / "slots").glob("slot-*"))


def test_release_removes_only_this_holders_slots_and_is_idempotent(tmp_path):
    make_hold(tmp_path / "slots", 1, "claude-vm", lease=10)
    make_hold(tmp_path / "slots", 2, "other", lease=10)
    make_hold(tmp_path / "slots", 3, "claude-vm", side="win")  # a runner that happens to share the name
    r = hold_run(
        tmp_path,
        "$a = Invoke-SlotVmCommand 'release' @('-Name','claude-vm'); $b = Invoke-SlotVmCommand 'release' @(); \"RC $a $b\"",
    )
    assert "RC 0 0" in r.stdout, r.stdout + r.stderr
    assert not (tmp_path / "slots/slot-1").exists()
    assert (tmp_path / "slots/slot-2").exists() and (tmp_path / "slots/slot-3").exists()


def test_bad_usage_exits_2(tmp_path):
    for words in (
        "@('-Bogus','x')",
        "@('-Name')",
        "@('-Name','bad name!')",
        "@('-TimeoutSec','soon')",
        "@('-TimeoutSec','-4')",
        "@('-LeaseMin','0')",
        "@('-LeaseMin','1441')",
    ):
        r = hold_run(tmp_path, f"$rc = Invoke-SlotVmCommand 'hold' {words}; \"RC $rc\"")
        assert "RC 2" in r.stdout, (words, r.stdout, r.stderr)
    r = hold_run(tmp_path, "$rc = Invoke-SlotVmCommand 'release' @('-LeaseMin','5'); \"RC $rc\"")
    assert "RC 2" in r.stdout
    assert not list((tmp_path / "slots").glob("slot-*"))


def survives(tmp_path, lease, since, side="hold", extra=""):
    make_hold(tmp_path / "slots", 1, "claude-vm", lease=lease, since=since, side=side)
    if extra:
        with open(tmp_path / "slots/slot-1/owner", "a") as f:
            f.write(extra)
    hold_run(tmp_path, "Clear-StaleSlots @()")
    return (tmp_path / "slots/slot-1").exists()


def test_a_hold_slot_is_cleared_only_once_its_lease_has_run_out(tmp_path):
    assert survives(tmp_path / "a", 10, 100) is True  # inside the lease
    assert survives(tmp_path / "b", 10, 601) is False  # past 10 minutes
    assert survives(tmp_path / "c", 2, 100) is True  # inside a 2 minute lease, with a margin for slow starts
    assert survives(tmp_path / "d", 2, 121) is False
    assert survives(tmp_path / "e", None, 700) is False  # no lease written: the default is 10 minutes
    assert survives(tmp_path / "f", None, 500) is True


def test_a_runners_win_slot_is_not_treated_as_a_lease(tmp_path):
    assert survives(tmp_path / "a", 1, 5000, side="win") is False  # the runner rule: no worker, so stale
    # the lease rule never touches a slot owned by a runner that is busy
    make_hold(tmp_path / "b" / "slots", 1, "win-1", lease=1, since=5000, side="win")
    hold_run(tmp_path / "b", "Clear-StaleSlots @('win-1')")
    assert (tmp_path / "b/slots/slot-1").exists()


def test_owner_file_values_are_data_never_commands(tmp_path):
    marker = tmp_path / "pwned"
    evil = f"lease=$(New-Item {marker}); & New-Item {marker}\ntime=$(New-Item {marker})\n"
    survives(tmp_path / "a", 10, 5000, extra=evil)
    survives(tmp_path / "b", 99999, 5000, extra="lease=99999\n")
    assert not marker.exists()
    assert survives(tmp_path / "c", 99999, 90000) is False  # an absurd lease is capped at a day


def test_a_garbled_time_falls_back_to_the_slot_age(tmp_path):
    make_slot(tmp_path / "a" / "slots", 1, "claude-vm", side="hold", age=100)
    with open(tmp_path / "a/slots/slot-1/owner", "a") as f:
        f.write("lease=5\ntime=soon\n")
    hold_run(tmp_path / "a", "Clear-StaleSlots @()")
    assert (tmp_path / "a/slots/slot-1").exists()


# --- changing N while jobs run (the PowerShell hook and the hold helper) -------------------------------------


def write_conf(pub, n):
    (pub / "slots.conf").write_text(f"slots={n}\nthreads=4\n")


def start_ps_hook(tmp_path, pub, name):
    return subprocess.Popen(
        [PWSH, "-NoProfile", "-NonInteractive", "-Command", f"& '{tmp_path / 'hook.ps1'}' acquire"],
        env={**ENV, "SLOT_PUBLIC": str(pub), "GIT_RUNNER_NAME": name, "SLOT_POLL": "0.2"},
        stdout=subprocess.PIPE,
        text=True,
        stdin=subprocess.DEVNULL,
    )


def test_hook_raising_n_lets_a_waiting_job_in_at_once(tmp_path):
    pub = pc(tmp_path, slots=1)
    hook(tmp_path, pub, "acquire", "a")
    p = start_ps_hook(tmp_path, pub, "b")
    time.sleep(2)
    assert p.poll() is None
    write_conf(pub, 2)
    out, _ = p.communicate(timeout=30)
    assert "got PC job slot 2 of 2" in out


def test_hook_lowering_n_never_evicts_and_counts_every_slot_folder(tmp_path):
    pub = pc(tmp_path, slots=3)
    for n in ("a", "b", "c"):
        hook(tmp_path, pub, "acquire", n)
    write_conf(pub, 2)
    assert len(list((pub / "slots").glob("slot-*"))) == 3
    p = start_ps_hook(tmp_path, pub, "d")
    time.sleep(1.5)
    hook(tmp_path, pub, "release", "c")  # slot-3 gone, two held: still full at N=2
    time.sleep(1.5)
    assert p.poll() is None and sorted(x.name for x in (pub / "slots").glob("slot-*")) == ["slot-1", "slot-2"]
    hook(tmp_path, pub, "release", "a")
    out, _ = p.communicate(timeout=30)
    assert "got PC job slot 1 of 2" in out


def test_hold_waits_for_a_raise_and_does_not_take_a_hole_under_a_high_slot(tmp_path):
    for k in (1, 3):
        (tmp_path / f"slots/slot-{k}").mkdir(parents=True)
    (tmp_path / "slots.cfg").write_text("slots=2\nthreads=4\n")
    script = (
        PRELUDE
        + f"""
$SlotDir = '{tmp_path / "slots"}'; $SlotsFile = '{tmp_path / "slots.cfg"}'
function Invoke-SlotSync {{}}
$rc = Invoke-SlotVmCommand 'hold' @('-TimeoutSec','2'); "RC $rc" """
    )
    r = pwsh(script, {"SLOT_POLL": "0.2"})
    assert "RC 3" in r.stdout and not (tmp_path / "slots/slot-2").exists()  # held {1,3} = 2 = N: no free slot to take
    (tmp_path / "slots.cfg").write_text("slots=3\nthreads=4\n")
    r = pwsh(script, {"SLOT_POLL": "0.2"})
    assert "RC 0" in r.stdout and (tmp_path / "slots/slot-2").exists()


def test_controller_hold_counts_folders_not_numbers(tmp_path):
    (tmp_path / "slots/slot-3").mkdir(parents=True)
    (tmp_path / "slots.cfg").write_text("slots=2\nthreads=4\n")
    body = "Write-Output ('FULL ' + (Test-SlotHold))"

    def run():
        return pwsh(
            PRELUDE + f"$SlotDir = '{tmp_path / 'slots'}'; $SlotsFile = '{tmp_path / 'slots.cfg'}'\n{body}"
        ).stdout

    assert "FULL False" in run()  # slot-3 alone is one held slot of two
    (tmp_path / "slots/slot-2").mkdir()
    assert "FULL True" in run()  # {2,3} is two held: full, though slot-1 is free
    (tmp_path / "slots.cfg").write_text("slots=3\nthreads=4\n")
    assert "FULL False" in run()
