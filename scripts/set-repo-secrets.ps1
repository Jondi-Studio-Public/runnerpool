# Copies RUNNER_PAT and RUNNER_STATUS_TOKEN onto each repo that runs the PC/Mac CI.
# On GitHub Free, private repos cannot read organization secrets, so the org copies are not enough.
# Paste each token from your password manager. The values are never written to disk or printed.
param(
  [Parameter(Mandatory)] [string] $Org,
  [Parameter(Mandatory)] [string[]] $Repos   # every repo that runs on the pool, including this one (runnerpool)
)
$ErrorActionPreference = 'Stop'
foreach ($name in @('RUNNER_PAT', 'RUNNER_STATUS_TOKEN')) {
  $secure = Read-Host "Paste $name (the new org-owned token), or press Enter to skip" -AsSecureString
  $plain = [Runtime.InteropServices.Marshal]::PtrToStringBSTR([Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure))
  if ($plain) {
    foreach ($r in $Repos) {
      gh secret set $name -R "$Org/$r" --body $plain
      if ($LASTEXITCODE -ne 0) { throw "setting $name on $r failed" }
      Write-Host "  set $name on $r"
    }
  }
  $plain = $null
}
Write-Host 'Done. Next: .\runner.cmd publish'
