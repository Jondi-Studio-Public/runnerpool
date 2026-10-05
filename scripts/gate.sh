#!/usr/bin/env bash
# The same checks as CI (.github/workflows/ci.yml), from Git Bash, WSL or a Mac:
#   scripts/gate.sh            everything
#   scripts/gate.sh shell      shellcheck and a parse of every shell script, and of win/*.ps1 (needs pwsh)
#   scripts/gate.sh python     ruff format check and lint, compile the Python, run tests/
# Needs shellcheck and uv (ruff runs through uvx; without uv, tests fall back to pytest on PATH). actionlint and the secret scan run only in CI.
set -euo pipefail
cd "$(dirname "$0")/.."

shell_checks() {
  local scripts=(runner macs rp mac/gitrunner mac/postinstall mac/build-local.sh linux/linuxrunner linux/slot-hook.sh linux/linux-provision.sh win/build.sh scripts/*.sh)
  for f in "${scripts[@]}"; do bash -n "$f"; done
  sh -n dashboard/entrypoint.sh
  sh -n watchdog/entrypoint.sh
  sh -n webhook/entrypoint.sh
  shellcheck -S warning "${scripts[@]}" dashboard/entrypoint.sh watchdog/entrypoint.sh webhook/entrypoint.sh
  echo "shell: ok"
  ps_checks
}

# Parses every PowerShell script (the Windows side) with pwsh. CI installs it (actions/setup-tools);
# elsewhere it is skipped with a note, so Git Bash without PowerShell 7 can still run the gate.
ps_checks() {
  if ! command -v pwsh >/dev/null; then echo "powershell: skipped (pwsh not installed)"; return; fi
  pwsh -NoProfile -Command '
    $bad = 0
    foreach ($f in Get-ChildItem -Path win -Filter *.ps1) {
      $e = $null
      [void][Management.Automation.Language.Parser]::ParseFile($f.FullName, [ref]$null, [ref]$e)
      foreach ($x in $e) { Write-Host "$($f.Name):$($x.Extent.StartLineNumber): $($x.Message)"; $bad++ }
    }
    if ($bad) { exit 1 }
    Write-Host "powershell: ok"'
}

python_checks() {
  # Pinned; keep in sync with the ruff version used elsewhere in your org.
  uvx ruff@0.15.20 format --check .
  uvx ruff@0.15.20 check .
  python3 -m py_compile ci/ci_shard.py .github/actions/steal/steal.py dashboard/server.py dashboard/gh_app_token.py dashboard/ci_store.py watchdog/watchdog.py webhook/receiver.py
  # PyJWT[crypto] is the version dashboard/requirements.txt installs into the image.
  if command -v uvx >/dev/null; then uvx --with "$(grep -i ^pyjwt dashboard/requirements.txt)" pytest -q tests; else python3 -m pytest -q tests; fi
}

case "${1:-all}" in
  shell) shell_checks ;;
  python) python_checks ;;
  all) shell_checks; python_checks ;;
  *) echo "usage: scripts/gate.sh [shell|python]"; exit 2 ;;
esac
