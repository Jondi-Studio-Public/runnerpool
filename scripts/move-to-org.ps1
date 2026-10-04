<#
Moves your live repos into a GitHub organisation and re-homes the PC's self-hosted runners.
See org-migration.md. Run one phase at a time, from PowerShell, with `gh` logged in as the repos' owner.

  .\move-to-org.ps1 -Org <org> -Owner <user> -Repos a,b,c -Phase transfer
  .\move-to-org.ps1 -Org <org> -Repos a,b,c -Phase runners
  .\move-to-org.ps1 -Org <org> -Phase protect -Repo <repo> [-Check ci-ok]
#>
param(
  [Parameter(Mandatory)] [string] $Org,
  [Parameter(Mandatory)] [ValidateSet('transfer', 'runners', 'protect')] [string] $Phase,
  [string] $Repo,
  [string] $Check = 'ci-ok',
  [string] $Distro = 'gh-runner',   # the WSL distro the PC runners live in
  [string] $Owner,   # the personal account the repos move from
  [string[]] $Repos,   # the repos to move
  [string] $PcPrefix = 'examplepc'   # name prefix of the PC runners to clean up when re-homing
)
$ErrorActionPreference = 'Stop'

function Invoke-Gh {
  $out = & gh @args
  if ($LASTEXITCODE -ne 0) { throw "gh $($args -join ' ') failed" }
  $out
}

# True when the repo exists. Windows PowerShell turns a native command's stderr into a terminating
# error under ErrorActionPreference Stop, so the expected 404 is checked with Continue.
function Test-Repo($fullName) {
  $ErrorActionPreference = 'Continue'
  & gh api "repos/$fullName" --jq '.full_name' 2>$null | Out-Null
  return ($LASTEXITCODE -eq 0)
}

function Confirm-Step($text) {
  if ((Read-Host "$text Type yes to continue") -ne 'yes') { Write-Host 'Stopped.'; exit 1 }
}

if ($Phase -eq 'transfer') {
  if (-not $Owner -or -not $Repos) { throw '-Owner and -Repos are required for -Phase transfer' }
  $plan = Invoke-Gh api "orgs/$Org" --jq '.plan.name'
  Write-Host "Org $Org is on plan: $plan"
  if ($plan -ne 'team') { Confirm-Step "The org is on $plan, not Team: runners will be shared, but required checks (-Phase protect) won't work on private repos until you upgrade." }

  $group = Invoke-Gh api "orgs/$Org/actions/runner-groups" --jq '.runner_groups[] | select(.default) | .visibility'
  Write-Host "Default runner group visibility: $group (want: all)"

  Confirm-Step "About to transfer $($Repos -join ', ') from $Owner to $Org."
  foreach ($r in $Repos) {
    if (Test-Repo "$Org/$r") { Write-Host "  $r already in $Org"; continue }
    Invoke-Gh api -X POST "repos/$Owner/$r/transfer" -f "new_owner=$Org" --jq '.full_name' | Out-Null
    Write-Host "  transferred $r"
  }

  # Transfers finish asynchronously; wait for runnerpool before changing its settings.
  for ($i = 0; $i -lt 30; $i++) {
    if (Test-Repo "$Org/runnerpool") { break }
    Start-Sleep -Seconds 2
  }
  # Let the org's repos call runnerpool' reusable workflows (ci-plan.yml) and composite actions.
  Invoke-Gh api -X PUT "repos/$Org/runnerpool/actions/permissions/access" -f access_level=organization | Out-Null
  Write-Host "runnerpool workflows are callable from every repo in $Org."
  Write-Host 'Next: tell Claude the transfer is done (step 4 PRs), then reissue the two tokens (step 5).'
}

if ($Phase -eq 'runners') {
  # Org secrets (private repos read them only on a paid plan; see set-repo-secrets.ps1). The values never touch disk.
  foreach ($name in @('RUNNER_PAT', 'RUNNER_STATUS_TOKEN')) {
    $secure = Read-Host "Paste the new $name (org-owned fine-grained token), or press Enter to skip" -AsSecureString
    $plain = [Runtime.InteropServices.Marshal]::PtrToStringBSTR([Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure))
    if ($plain) {
      gh secret set $name --org $Org --visibility private --body $plain
      if ($LASTEXITCODE -ne 0) { throw "setting $name failed" }
      Write-Host "  set org secret $name"
    }
    $plain = $null
  }

  # Private repos on GitHub Free cannot read org secrets, so each repo needs its own copy.
  # Run scripts/set-repo-secrets.ps1 to set them; the org copies serve once the org is on Team.

  # Registration token for the org, and a removal token per repo the PC runners belong to now.
  $reg = Invoke-Gh api -X POST "orgs/$Org/actions/runners/registration-token" --jq '.token'
  if (-not $Repos) { throw '-Repos is required for -Phase runners (the repos the PC runners belong to now)' }
  $rmPairs = @($Repos | ForEach-Object { '{0}={1}' -f $_.ToLower(), (Invoke-Gh api -X POST "repos/$Org/$_/actions/runners/remove-token" --jq '.token') })

  Confirm-Step 'About to stop the PC runners in WSL and re-register them at org level (CI pauses for a minute).'

  # Runs inside WSL. Finds every runner by its .runner file, keeps its name, work folder and
  # repo label, and moves it from repo level to org level.
  $bash = @'
set -euo pipefail
org="$1"; reg="$2"; shift 2
declare -A rm_tokens
for p in "$@"; do rm_tokens[${p%%=*}]=${p#*=}; done
found=0
while IFS= read -r cfg; do
  dir=$(dirname "$cfg")
  # A runner whose earlier move half-failed keeps its settings only in .runner_migrated.
  if [ -f "$dir/.runner" ]; then f="$dir/.runner"
  elif [ -f "$dir/.runner_migrated" ]; then f="$dir/.runner_migrated"
  else continue; fi
  name=$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1],encoding="utf-8-sig"))["agentName"])' "$f")
  url=$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1],encoding="utf-8-sig"))["gitHubUrl"])' "$f")
  work=$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1],encoding="utf-8-sig")).get("workFolder","_work"))' "$f")
  repo=$(basename "${url,,}")
  if [ "${url,,}" = "https://github.com/${org,,}" ]; then echo "$name: already at org level, skipped"; continue; fi
  if [ -z "${rm_tokens[$repo]:-}" ]; then echo "$name: $url is not one of the listed repos, skipped"; continue; fi
  label=$repo; rm="${rm_tokens[$repo]}"
  found=$((found+1))
  echo "== $name ($label) in $dir, work folder $work"
  cd "$dir"
  # config.sh refuses to run as root, so it runs as the folder's owner (the runner user).
  owner=$(stat -c %U "$dir")
  as_owner() { runuser -u "$owner" -- "$@"; }
  svc=0; [ -f .service ] && svc=1   # installed as a systemd service by svc.sh
  # Keep the unit's drop-ins (cpu.conf limits) for the new unit name svc.sh install picks.
  dropins=""
  if [ "$svc" = 1 ] && [ -d "/etc/systemd/system/$(cat .service).d" ]; then
    dropins=$(mktemp -d); cp -a "/etc/systemd/system/$(cat .service).d/." "$dropins/"
  fi
  # A runner whose service an earlier run already removed: take the drop-ins left under its old name.
  if [ -z "$dropins" ]; then
    for d in /etc/systemd/system/actions.runner.*."$name".service.d; do
      [ -d "$d" ] && { dropins=$(mktemp -d); cp -a "$d/." "$dropins/"; break; }
    done
  fi
  if [ "$svc" = 1 ]; then ./svc.sh stop || true; ./svc.sh uninstall || true
  else pkill -f "$dir/bin/Runner.Listener" || true; sleep 2; fi
  # The runner's saved URL is the old example-user/<repo> one, which GitHub's runner API does not
  # redirect, so the removal can 404. Then clear the local config and drop the stale GitHub
  # entry afterwards (below, from PowerShell).
  if ! as_owner ./config.sh remove --token "$rm"; then
    echo "  GitHub would not unregister $name at its old URL; clearing its local config instead"
    # Without credentials, config.sh remove only clears the local settings (all of them, including
    # the .runner_migrated copy newer runners keep), which is what lets it register again.
    rm -f .credentials .credentials_rsaparams .credentials_migrated .credentials_rsaparams_migrated
    as_owner ./config.sh remove --token none || true
    rm -f .runner .runner_migrated
  fi
  as_owner ./config.sh --unattended --url "https://github.com/$org" --token "$reg" \
    --name "$name" --labels "$label" --work "$work" --replace
  # Every PC runner runs as a systemd service; this also restores one a failed earlier run uninstalled.
  ./svc.sh install "$owner"
  if [ -n "$dropins" ]; then
    mkdir -p "/etc/systemd/system/$(cat .service).d"
    cp -a "$dropins/." "/etc/systemd/system/$(cat .service).d/"
    systemctl daemon-reload
  fi
  ./svc.sh start
done < <(find / -xdev -maxdepth 6 -name config.sh -path "*runner*" -not -path "/proc/*" -not -path "/mnt/*" 2>/dev/null)
echo "re-registered $found runner(s)"
'@ -replace "`r", ''

  $bash | wsl -d $Distro -e bash -s -- $Org $reg @rmPairs
  if ($LASTEXITCODE -ne 0) { throw 'WSL re-registration failed; check the output above.' }

  # Old repo-level entries for the PC runners that could not be unregistered are now offline
  # duplicates; delete them so only the org-level runners remain.
  foreach ($r in $Repos) {
    $old = ((Invoke-Gh api "repos/$Org/$r/actions/runners?per_page=100") -join "`n" | ConvertFrom-Json).runners |
      Where-Object { $_.name -like "$PcPrefix-*" -and $_.status -eq 'offline' }
    foreach ($x in $old) {
      Invoke-Gh api -X DELETE "repos/$Org/$r/actions/runners/$($x.id)" | Out-Null
      Write-Host "  deleted old $r entry $($x.name)"
    }
  }
  # Formatted here rather than in --jq: Windows PowerShell strips double quotes inside native arguments.
  ((Invoke-Gh api "orgs/$Org/actions/runners") -join "`n" | ConvertFrom-Json).runners |
    ForEach-Object { '{0} {1} {2}' -f $_.name, $_.status, (($_.labels | ForEach-Object name) -join ',') }
  Write-Host 'Next: runner publish, then re-register each Mac (step 7).'
}

if ($Phase -eq 'protect') {
  if (-not $Repo) { throw 'Name the repo with -Repo.' }
  # Ruleset on main: changes arrive by PR, the aggregate check must pass, no force-push or delete.
  # No bypass list, so it binds you and every Claude session alike.
  $ruleset = @{
    name        = 'main'
    target      = 'branch'
    enforcement = 'active'
    conditions  = @{ ref_name = @{ include = @('~DEFAULT_BRANCH'); exclude = @() } }
    rules       = @(
      @{ type = 'deletion' },
      @{ type = 'non_fast_forward' },
      @{ type = 'pull_request'; parameters = @{
          required_approving_review_count = 0; dismiss_stale_reviews_on_push = $false
          require_code_owner_review = $false; require_last_push_approval = $false
          required_review_thread_resolution = $false } },
      @{ type = 'required_status_checks'; parameters = @{
          strict_required_status_checks_policy = $false
          required_status_checks = @(@{ context = $Check }) } }
    )
  } | ConvertTo-Json -Depth 10
  Confirm-Step "About to require '$Check' and PRs on $Org/$Repo main."
  $created = $ruleset | gh api -X POST "repos/$Org/$Repo/rulesets" --input -
  if ($LASTEXITCODE -ne 0) { throw 'creating the ruleset failed' }
  Write-Host "Ruleset $((($created -join "`n") | ConvertFrom-Json).id) active on $Org/$Repo main."
}
