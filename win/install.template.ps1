# win-runners.ps1: turns this Windows PC into a GitHub Actions runner for your GitHub org.
# One file for every PC: the first PC installed becomes win-1, the next win-2. Built by
# win/build.sh, which fills in the settings below and embeds winrunner.ps1. It holds a GitHub
# GitHub App private key (or, on an older build, a token), so keep it off shared drives and delete it after installing.
#
# Run it from any PowerShell (it asks for administrator rights itself):
#   Unblock-File .\win-runners.ps1
#   powershell -ExecutionPolicy Bypass -File .\win-runners.ps1
$ErrorActionPreference = 'Stop'

$Settings = [ordered]@{
    GITRUNNER_ORG  = '@@GITRUNNER_ORG@@'
    CI_REPO        = '@@CI_REPO@@'
    CI_LABELS      = '@@CI_LABELS@@'
    ADMIN_REPO     = '@@ADMIN_REPO@@'
    RUNNER_PAT     = '@@RUNNER_PAT@@'
    GITHUB_APP_ID  = '@@GITHUB_APP_ID@@'
    GITHUB_APP_KEY_B64 = '@@GITHUB_APP_KEY_B64@@'
    RUNNER_VERSION = '@@RUNNER_VERSION@@'
    WSL_SET        = '@@WSL_SET@@'
    WSL_DISTRO     = 'gh-runner'
}
$Payload = '@@PAYLOAD@@'   # winrunner.ps1, base64
$LinuxPayload = '@@LINUX_PAYLOAD@@'   # linux/linuxrunner, base64 (for the WSL runners)
$ProvisionPayload = '@@PROVISION_PAYLOAD@@'   # linux/linux-provision.sh, base64

$id = [Security.Principal.WindowsIdentity]::GetCurrent()
if (-not (New-Object Security.Principal.WindowsPrincipal($id)).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host 'Asking for administrator rights...'
    $ps = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
    Start-Process $ps -Verb RunAs -Wait -ArgumentList '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', "`"$PSCommandPath`""
    exit
}

# The WSL set (general Linux runners for every repo, in a WSL distro) is optional.
if ($Settings.WSL_SET) {
    $ans = Read-Host "Also set up Linux (WSL) runners: $($Settings.WSL_SET -replace ';', ' + ')? An existing '$($Settings.WSL_DISTRO)' distro is adopted, not reinstalled. [Y/n]"
    if ($ans -match '^[nN]') { $Settings.WSL_SET = '' }
}

try {
    $home_ = Join-Path $env:ProgramData 'win-runners'
    $logs = Join-Path $home_ 'logs'
    New-Item -ItemType Directory -Force -Path $home_, $logs | Out-Null
    # Only SYSTEM and Administrators may read what is in here (the credential ends up in it).
    & icacls.exe $home_ /inheritance:r /grant:r '*S-1-5-18:(OI)(CI)(F)' '*S-1-5-32-544:(OI)(CI)(F)' | Out-Null

    $script = Join-Path $home_ 'winrunner.ps1'
    [IO.File]::WriteAllBytes($script, [Convert]::FromBase64String($Payload))
    if ($Settings.WSL_SET) {
        [IO.File]::WriteAllBytes((Join-Path $home_ 'linuxrunner'), [Convert]::FromBase64String($LinuxPayload))
        [IO.File]::WriteAllBytes((Join-Path $home_ 'linux-provision.sh'), [Convert]::FromBase64String($ProvisionPayload))
    }
    $envFile = Join-Path $home_ 'bootstrap.json'
    [pscustomobject]$Settings | ConvertTo-Json | Set-Content -Path $envFile -Encoding UTF8

    $ps = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
    & $ps -NoProfile -ExecutionPolicy Bypass -File $script bootstrap $envFile 2>&1 | Tee-Object -FilePath (Join-Path $logs 'install.log') -Append
    if ($LASTEXITCODE -ne 0) { throw "winrunner bootstrap failed (exit $LASTEXITCODE); see $logs\install.log" }
    Write-Host ''
    Write-Host 'Installed. Delete this file now: it holds the GitHub App key (or token).'
    Write-Host 'From the PC you manage them from:  .\runner.cmd list   (this one shows up as win-N)'
} catch {
    Write-Host "Install failed: $($_.Exception.Message)" -ForegroundColor Red
    Remove-Item -Force (Join-Path $env:ProgramData 'win-runners\bootstrap.json') -ErrorAction SilentlyContinue
    $global:LASTEXITCODE = 1
}
Read-Host 'Press Enter to close'
