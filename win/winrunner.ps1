# winrunner: installs and manages GitHub Actions runners on this Windows PC.
# Installed by win-runners.ps1 to C:\ProgramData\win-runners\winrunner.ps1 and run elevated
# (as SYSTEM) by the installer, by the admin workflow (its runner runs as SYSTEM), or by hand in
# an administrator PowerShell. It is the Windows twin of mac/gitrunner. `winrunner help` lists
# the commands. Written for Windows PowerShell 5.1, which every Windows 10/11 has.
param(
    [Parameter(Position = 0)][string]$Command = 'help',
    [Parameter(Position = 1, ValueFromRemainingArguments = $true)][string[]]$Rest = @()
)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'   # Invoke-WebRequest is far slower with the progress bar

$HomeDir = Join-Path $env:ProgramData 'win-runners'
$Runners = Join-Path $HomeDir 'runners'
$Cache = Join-Path $HomeDir 'cache'
$Logs = Join-Path $HomeDir 'logs'
$Conf = Join-Path $HomeDir 'conf'
$HostFile = Join-Path $HomeDir 'host'                 # this PC's runner name, e.g. win-1
$AuthFile = Join-Path $HomeDir 'github-token'         # the stored PAT, readable by SYSTEM and Administrators only (used when there is no App key)
$AppIdFile = Join-Path $HomeDir 'github-app-id'       # the GitHub App's id (or client id)
$AppKeyFile = Join-Path $HomeDir 'github-app.pem'     # its private key (PKCS#1 or PKCS#8 PEM), SYSTEM and Administrators only
$AppInstallFile = Join-Path $HomeDir 'github-app-installation'   # cached installation id on $AppOrg
$AppTokenFile = Join-Path $HomeDir 'github-app-token'            # cached installation token, SYSTEM and Administrators only
$AppExpiryFile = Join-Path $HomeDir 'github-app-token.expires'   # "EPOCH ISO8601" of that token's expires_at
$AppOrg = if ($env:GITRUNNER_APP_ORG) { $env:GITRUNNER_APP_ORG } elseif ($env:GITRUNNER_ORG) { $env:GITRUNNER_ORG } else { 'example-org' }   # the org the App is installed on
$AppRefreshMargin = 600   # seconds: mint a new installation token this long before the old one expires
$BatteryFlag = Join-Path $HomeDir 'pause-on-battery'  # present = CI pauses on battery (default)
$CiOff = Join-Path $HomeDir 'ci-off'                  # present = CI runners take no jobs (`ci off`)
$CoresFile = Join-Path $HomeDir 'max-cores'           # holds N = CI jobs may use N cores (`cores N`); absent = all
$PushConf = Join-Path $HomeDir 'dashboard-push.json'  # {url, host, token_file}; absent = no health push (`push-setup`)
$PushTokenFile = Join-Path $HomeDir 'dashboard-push-token'  # the dashboard's token for this PC, SYSTEM and Administrators only
$StateDir = Join-Path $env:ProgramData 'win-runners-public'   # world-readable: the distro and the job hooks read here
$StateFile = Join-Path $StateDir 'linux-state'
$SlotsFile = Join-Path $HomeDir 'slots'               # `slots=N` / `threads=T` lines: the PC's job slots (`slots N`); absent = no cap
$CiUser = '_cirunner'
$TaskName = 'win-runners power watch'
$PowerPoll = 30        # seconds between power checks
$HealEvery = 600       # seconds between checks that GitHub still has our runners
$PushEvery = 30        # seconds between health pushes to the dashboard
$PushTimeout = 5       # seconds a push may take: it runs inside the power loop, which must not stall
$SlotPoll = 2          # seconds between slot checks (clear dead slots, pause or resume idle runners)
$StartRetry = 600      # seconds before trying again to start a CI service that failed to start

function Log([string]$m) { Write-Host ("{0} {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $m) }
function Die([string]$m) { throw "error: $m" }

function Assert-Admin {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    if (-not (New-Object Security.Principal.WindowsPrincipal($id)).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        Die "run in an administrator PowerShell (winrunner $Command)"
    }
}

# --- GitHub (a GitHub App's short-lived tokens, or a long-lived PAT) --------------------

# This PC holds a GitHub App's id and private key (the pattern Actions Runner Controller uses) and mints
# one-hour installation tokens from them on demand: a JWT signed RS256 with the key (iat = now-60,
# exp = now+540, iss = the App id) is traded at POST /app/installations/ID/access_tokens. The installation id
# is looked up once (GET /orgs/ORG/installation) and cached; the token is cached with its expires_at and
# reused until $AppRefreshMargin seconds before it expires. Windows PowerShell 5.1 / .NET Framework cannot
# import a PEM key (no ImportFromPem), so ConvertFrom-RsaPem reads the PKCS#1 (or PKCS#8) DER into
# RSAParameters itself and .NET signs with it: no openssl, Git or other dependency. With no App key the old
# stored fine-grained PAT is used exactly as before (limited to the CI and admin repos: enough to register
# runners and nothing else). Every credential lives in a file only SYSTEM and Administrators can read and is
# sent as a header, never on a command line.
function Protect-File([string]$path) {  # SYSTEM and Administrators only (a no-op off Windows, where the tests run)
    if ([IO.Path]::DirectorySeparatorChar -eq '\') {
        & icacls.exe $path /inheritance:r /grant:r '*S-1-5-18:(F)' '*S-1-5-32-544:(F)' | Out-Null
    }
}

function Write-Private([string]$path, [string]$text) {  # replace PATH atomically, readable by SYSTEM and Administrators only
    New-Item -ItemType Directory -Force -Path (Split-Path $path) | Out-Null
    $tmp = "$path.new"
    Set-Content -Path $tmp -Value $text -NoNewline -Encoding ASCII
    Protect-File $tmp
    Move-Item -Force $tmp $path
}

function Save-Token([string]$token) { Write-Private $AuthFile $token }

function Get-Now { if ($env:GITRUNNER_NOW) { [long]$env:GITRUNNER_NOW } else { [DateTimeOffset]::UtcNow.ToUnixTimeSeconds() } }  # tests pin the clock

function Test-AppConfigured { (Test-Path $AppIdFile) -and (Test-Path $AppKeyFile) }
function Get-CredentialKind { if (Test-AppConfigured) { 'app' } elseif (Test-Path $AuthFile) { 'pat' } else { 'none' } }
function Test-HasCredential { (Get-CredentialKind) -ne 'none' }

function ConvertTo-Base64Url([byte[]]$bytes) { [Convert]::ToBase64String($bytes).TrimEnd('=').Replace('+', '-').Replace('/', '_') }

# One DER element at POS: its tag and where its content starts and ends.
function Get-DerElement([byte[]]$b, [int]$pos) {
    $len = [int]$b[$pos + 1]
    $start = $pos + 2
    if ($len -band 0x80) {
        $n = $len -band 0x7f
        $len = 0
        for ($i = 0; $i -lt $n; $i++) { $len = ($len -shl 8) -bor [int]$b[$pos + 2 + $i] }
        $start = $pos + 2 + $n
    }
    @{ Tag = [int]$b[$pos]; Start = $start; End = $start + $len }
}

function Remove-LeadingZeros([byte[]]$v) {
    $i = 0
    while ($i -lt $v.Length - 1 -and $v[$i] -eq 0) { $i++ }
    , ([byte[]]$v[$i..($v.Length - 1)])
}

function ConvertTo-FixedBytes([byte[]]$v, [int]$size) {  # strip the DER sign byte, left-pad to SIZE (what RSAParameters wants)
    $v = Remove-LeadingZeros $v
    if ($v.Length -gt $size) { throw 'RSA key component is longer than the modulus' }
    $out = New-Object byte[] $size
    [Array]::Copy($v, 0, $out, $size - $v.Length, $v.Length)
    , $out
}

# An RSA private key in PEM form ("BEGIN RSA PRIVATE KEY", PKCS#1, which is what GitHub issues; or "BEGIN
# PRIVATE KEY", PKCS#8) -> RSAParameters.
function ConvertFrom-RsaPem([string]$pem) {
    if (-not ($pem -match '-----BEGIN (RSA )?PRIVATE KEY-----([^-]+)-----END (RSA )?PRIVATE KEY-----')) { throw 'not an RSA private key in PEM form' }
    $pkcs8 = -not $Matches[1]
    $der = [byte[]][Convert]::FromBase64String(($Matches[2] -replace '\s', ''))
    if ($pkcs8) {  # PrivateKeyInfo { INTEGER version, SEQUENCE algorithm, OCTET STRING privateKey }
        $outer = Get-DerElement $der 0
        $ver = Get-DerElement $der $outer.Start
        $alg = Get-DerElement $der $ver.End
        $oct = Get-DerElement $der $alg.End
        if ($outer.Tag -ne 0x30 -or $oct.Tag -ne 4) { throw 'unreadable PKCS#8 key' }
        $der = [byte[]]$der[$oct.Start..($oct.End - 1)]
    }
    $seq = Get-DerElement $der 0   # RSAPrivateKey { version, n, e, d, p, q, dp, dq, qinv }
    if ($seq.Tag -ne 0x30) { throw 'unreadable RSA key' }
    $pos = $seq.Start
    $ints = @()
    for ($i = 0; $i -lt 9; $i++) {
        $e = Get-DerElement $der $pos
        if ($e.Tag -ne 2) { throw 'unreadable RSA key' }
        $ints += , ([byte[]]$der[$e.Start..($e.End - 1)])
        $pos = $e.End
    }
    $mod = Remove-LeadingZeros $ints[1]
    $size = $mod.Length
    $half = [int]($size / 2)
    $rp = New-Object Security.Cryptography.RSAParameters
    $rp.Modulus = $mod
    $rp.Exponent = Remove-LeadingZeros $ints[2]
    $rp.D = ConvertTo-FixedBytes $ints[3] $size
    $rp.P = ConvertTo-FixedBytes $ints[4] $half
    $rp.Q = ConvertTo-FixedBytes $ints[5] $half
    $rp.DP = ConvertTo-FixedBytes $ints[6] $half
    $rp.DQ = ConvertTo-FixedBytes $ints[7] $half
    $rp.InverseQ = ConvertTo-FixedBytes $ints[8] $half
    $rp
}

function Get-AppJwt {  # -> a JWT that authenticates as the App itself, valid ten minutes
    $id = (Get-Content $AppIdFile -Raw).Trim()
    $iss = if ($id -match '^\d+$') { $id } else { '"' + $id + '"' }   # a client id (Iv1...) is a string
    $iat = (Get-Now) - 60
    $head = ConvertTo-Base64Url ([Text.Encoding]::ASCII.GetBytes('{"alg":"RS256","typ":"JWT"}'))
    $body = ConvertTo-Base64Url ([Text.Encoding]::ASCII.GetBytes('{"iat":' + $iat + ',"exp":' + ($iat + 600) + ',"iss":' + $iss + '}'))
    $rsa = [Security.Cryptography.RSA]::Create()
    try {
        if ($rsa -is [Security.Cryptography.RSACryptoServiceProvider]) { $rsa.PersistKeyInCsp = $false }   # never leave a key in a container
        $rsa.ImportParameters((ConvertFrom-RsaPem (Get-Content $AppKeyFile -Raw)))
        $sig = $rsa.SignData([Text.Encoding]::ASCII.GetBytes("$head.$body"), [Security.Cryptography.HashAlgorithmName]::SHA256,
            [Security.Cryptography.RSASignaturePadding]::Pkcs1)
    } finally { $rsa.Dispose() }
    "$head.$body." + (ConvertTo-Base64Url $sig)
}

function Invoke-GhApp([string]$method, [string]$path) {  # called as the App (a JWT), not as an installation
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    $h = @{
        Authorization          = 'Bearer ' + (Get-AppJwt)
        Accept                 = 'application/vnd.github+json'
        'X-GitHub-Api-Version' = '2022-11-28'
    }
    Invoke-RestMethod -Method $method -Uri "https://api.github.com/$path" -Headers $h -TimeoutSec 30
}

function Get-AppInstallationId {  # the App's installation on $AppOrg, cached
    if (Test-Path $AppInstallFile) {
        $v = (Get-Content $AppInstallFile -Raw).Trim()
        if ($v) { return $v }
    }
    $r = Invoke-GhApp 'GET' "orgs/$AppOrg/installation"
    if (-not $r.id) { throw "the GitHub App has no installation on $AppOrg" }
    Write-Private $AppInstallFile ([string]$r.id)
    [string]$r.id
}

function Update-AppToken {  # mint a new installation token and cache it
    $id = Get-AppInstallationId
    try { $r = Invoke-GhApp 'POST' "app/installations/$id/access_tokens" }
    catch {  # a reinstalled App has a new installation id: forget the cached one and ask once more
        Remove-Item -Force $AppInstallFile -ErrorAction SilentlyContinue
        $id = Get-AppInstallationId
        $r = Invoke-GhApp 'POST' "app/installations/$id/access_tokens"
    }
    if (-not $r.token) { throw 'GitHub answered with no installation token' }
    $exp = $r.expires_at
    if ($exp -is [datetime]) { $dt = $exp.ToUniversalTime() }  # PowerShell 7 parses dates; 5.1 leaves a string
    else { $dt = [DateTime]::Parse([string]$exp, [Globalization.CultureInfo]::InvariantCulture, [Globalization.DateTimeStyles]::AssumeUniversal -bor [Globalization.DateTimeStyles]::AdjustToUniversal) }
    $epoch = (New-Object DateTimeOffset($dt)).ToUnixTimeSeconds()
    Write-Private $AppTokenFile ([string]$r.token)
    Write-Private $AppExpiryFile ("$epoch " + $dt.ToString('yyyy-MM-ddTHH:mm:ssZ'))
}

function Initialize-AppToken {  # a cached token good for another $AppRefreshMargin seconds, else a new one
    if ((Test-Path $AppTokenFile) -and (Test-Path $AppExpiryFile)) {
        $epoch = ((Get-Content $AppExpiryFile -Raw).Trim() -split ' ')[0]
        if ($epoch -match '^\d+$' -and ([long]$epoch - (Get-Now)) -gt $AppRefreshMargin) { return }
    }
    Update-AppToken
}

function Get-GhBearer {  # -> the token to send: a fresh App installation token, or the stored PAT
    if (Test-AppConfigured) { Initialize-AppToken; return (Get-Content $AppTokenFile -Raw).Trim() }
    if (Test-Path $AuthFile) { return (Get-Content $AuthFile -Raw).Trim() }
    throw 'no GitHub credential on this PC'
}

function Invoke-Gh([string]$method, [string]$path) {  # -> parsed JSON; throws on any HTTP error
    $bearer = Get-GhBearer
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    $h = @{
        Authorization          = 'Bearer ' + $bearer
        Accept                 = 'application/vnd.github+json'
        'X-GitHub-Api-Version' = '2022-11-28'
    }
    Invoke-RestMethod -Method $method -Uri "https://api.github.com/$path" -Headers $h -TimeoutSec 30
}

# A runner's REPO is OWNER/REPO for a repo-level runner, or just ORG for an org-level one that
# any repo in the org can use.
function Api-Path([string]$repo) { if ($repo.Contains('/')) { "repos/$repo" } else { "orgs/$repo" } }

function New-GhToken([string]$kind, [string]$repo) {  # registration|remove -> a one-hour token
    (Invoke-Gh 'POST' "$(Api-Path $repo)/actions/runners/$kind-token").token
}

# Whether GitHub lists runner NAME in REPO|ORG: $true, $false, or $null when it could not ask,
# so heal never re-registers a runner on a guess.
function Test-OnGitHub([string]$repo, [string]$name) {
    try { $r = Invoke-Gh 'GET' "$(Api-Path $repo)/actions/runners?per_page=100" } catch { return $null }
    if ($null -eq $r.total_count) { return $null }
    return [bool]($r.runners | Where-Object { $_.name -eq $name })
}

# The lowest win-N that neither repo has a runner for.
function Get-NextName([string]$ciRepo, [string]$adminRepo) {
    foreach ($n in 1..50) {
        $a = Test-OnGitHub $ciRepo "win-$n"
        if ($null -eq $a) { Die 'could not list runners on GitHub (token or network)' }
        if ($a) { continue }
        $b = Test-OnGitHub $adminRepo "win-$n-admin"
        if ($null -eq $b) { Die 'could not list runners on GitHub (token or network)' }
        if ($b) { continue }
        return "win-$n"
    }
    Die 'no free win-N name'
}

function Get-TokenExpiry([string]$repo) {  # the stored PAT's expiry as GitHub reports it, or "never"
    try {
        $h = @{ Authorization = 'Bearer ' + (Get-Content $AuthFile -Raw).Trim() }
        $r = Invoke-WebRequest -UseBasicParsing -Uri "https://api.github.com/$(Api-Path $repo)" -Headers $h
        $e = $r.Headers['github-authentication-token-expiration']
        if ($e) { return [string]$e }
    } catch { Write-Verbose $_.Exception.Message }
    'never'
}

function Get-CredentialLine([string]$repo) {  # what `status` prints after "token:"
    switch (Get-CredentialKind) {
        'app' {
            $id = (Get-Content $AppIdFile -Raw).Trim()
            try {
                Initialize-AppToken
                "GitHub App $id, token valid until $(((Get-Content $AppExpiryFile -Raw).Trim() -split ' ')[1])"
            } catch { "GitHub App $id, but no token can be minted (check the key, and that the App is installed on $AppOrg)" }
        }
        'pat' { if ($repo) { "GitHub token stored, expires $(Get-TokenExpiry $repo)" } else { 'GitHub token stored' } }
        default { 'none (runners cannot re-register themselves)' }
    }
}

# --- runners -----------------------------------------------------------------------------

function Get-RunnerDir([string]$name) { Join-Path $Runners $name }
function Get-Service-For([string]$name) {  # config.cmd names the service actions.runner.<scope>.<name>
    Get-Service -Name "actions.runner.*.$name" -ErrorAction SilentlyContinue | Select-Object -First 1
}
function Get-RunnerNames { if (Test-Path $Runners) { (Get-ChildItem $Runners -Directory).Name } else { @() } }
function Get-RunnerConf([string]$name) {
    $f = Join-Path $Conf "$name.json"
    if (Test-Path $f) { Get-Content $f -Raw | ConvertFrom-Json } else { $null }
}
function Test-Ci([string]$name) { $c = Get-RunnerConf $name; ($null -ne $c) -and ($c.as -eq 'ci') }
# A job is running when that runner's Runner.Worker process exists.
function Test-Busy([string]$name) {
    $dir = (Get-RunnerDir $name).ToLowerInvariant() + '\'   # the slash: win-1 must not match win-1-ci-2
    $w = Get-CimInstance Win32_Process -Filter "Name = 'Runner.Worker.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.ExecutablePath -and $_.ExecutablePath.ToLowerInvariant().StartsWith($dir) }
    return [bool]$w
}
function Get-RunnerState([string]$name) {  # running | stopped | missing
    $s = Get-Service-For $name
    if (-not $s) { return 'missing' }
    if ($s.Status -eq 'Running') { return 'running' }
    return 'stopped'
}

function Get-LatestRunnerVersion {
    try { $r = Invoke-Gh 'GET' 'repos/actions/runner/releases/latest' }
    catch { $r = Invoke-RestMethod -Uri 'https://api.github.com/repos/actions/runner/releases/latest' }
    return ([string]$r.tag_name).TrimStart('v')
}

function Get-RunnerZip([string]$ver) {  # -> path of the cached zip
    $zip = Join-Path $Cache "actions-runner-win-x64-$ver.zip"
    New-Item -ItemType Directory -Force -Path $Cache | Out-Null
    if (-not (Test-Path $zip)) {
        Log "downloading actions runner $ver"
        Invoke-WebRequest -UseBasicParsing -OutFile "$zip.part" `
            -Uri "https://github.com/actions/runner/releases/download/v$ver/actions-runner-win-x64-$ver.zip"
        Move-Item -Force "$zip.part" $zip
    }
    # Only the version in use is worth keeping; each old zip is another 100+ MB.
    Get-ChildItem $Cache -Filter 'actions-runner-win-x64-*.zip' | Where-Object { $_.Name -ne (Split-Path $zip -Leaf) } | Remove-Item -Force
    return $zip
}

function New-RandomPassword {
    $bytes = New-Object byte[] 24
    [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    # Upper, lower and digit suffixes keep it inside any local password policy.
    return ([Convert]::ToBase64String($bytes) + 'aA1!')
}

# A hidden service account that only runs the CI runner service: not an administrator, hidden
# from the sign-in screen, and given a fresh random password whenever a runner is registered
# (the password goes straight to the service, nowhere else).
function Initialize-CiUser {  # -> the password
    $pw = New-RandomPassword
    $sec = ConvertTo-SecureString $pw -AsPlainText -Force
    if (Get-LocalUser -Name $CiUser -ErrorAction SilentlyContinue) {
        Set-LocalUser -Name $CiUser -Password $sec
        # Failed service logons (a stale password) can trip Windows' account lockout, and a locked
        # account rejects even the new password, so config.cmd reports invalid credentials.
        $u = [ADSI]"WinNT://./$CiUser,user"
        if ($u.IsAccountLocked) { Log "unlocking $CiUser"; $u.IsAccountLocked = $false; $u.SetInfo() }
    } else {
        Log "creating service user $CiUser"
        New-LocalUser -Name $CiUser -Password $sec -PasswordNeverExpires -UserMayNotChangePassword `
            -AccountNeverExpires -FullName 'CI runner' -Description 'Runs the GitHub Actions CI runner service' | Out-Null
        Add-LocalGroupMember -SID 'S-1-5-32-545' -Member $CiUser -ErrorAction SilentlyContinue  # Users
        $key = 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon\SpecialAccounts\UserList'
        if (-not (Test-Path $key)) { New-Item -Path $key -Force | Out-Null }
        New-ItemProperty -Path $key -Name $CiUser -Value 0 -PropertyType DWord -Force | Out-Null
    }
    return $pw
}

# Every CI runner's service logs on as $CiUser, so a new password must reach all of them, not just
# the runner being registered: a sibling left with the old one can no longer start.
function Set-CiServicePassword([string]$pw) {
    foreach ($n in Get-RunnerNames) {
        if (-not (Test-Ci $n)) { continue }
        $s = Get-Service-For $n
        if (-not $s) { continue }
        $svc = Get-CimInstance Win32_Service -Filter "Name = '$($s.Name)'"
        $r = Invoke-CimMethod -InputObject $svc -MethodName Change -Arguments @{ StartName = ".\$CiUser"; StartPassword = $pw }
        if ($r.ReturnValue -ne 0) { Log "could not give $n the new $CiUser password (Win32_Service.Change returned $($r.ReturnValue))" }
    }
}

function Start-Runner([string]$name) {
    $s = Get-Service-For $name
    if ($s -and $s.Status -ne 'Running') { Start-Service -InputObject $s }
}
function Stop-Runner([string]$name) {
    $s = Get-Service-For $name
    if ($s -and $s.Status -ne 'Stopped') { Stop-Service -InputObject $s -Force }
}

# Runs config.cmd and fails on a non-zero exit, with the runner's own words in the log.
function Invoke-Config([string]$dir, [string[]]$arguments) {
    # Start-Process joins the array with spaces and quotes nothing, so 'NT AUTHORITY\SYSTEM' would split in two.
    $quoted = $arguments | ForEach-Object { if ($_ -match '[\s"]') { '"' + ($_ -replace '"', '\"') + '"' } else { $_ } }
    $p = Start-Process -FilePath (Join-Path $dir 'config.cmd') -ArgumentList $quoted -WorkingDirectory $dir `
        -NoNewWindow -Wait -PassThru
    if ($p.ExitCode -ne 0) { Die "config.cmd exited $($p.ExitCode)" }
}

# add-runner REPO|ORG NAME LABELS [TOKEN|-] [ci|admin] [-ephemeral]   (- = mint one with the stored token)
function Add-Runner([string]$repo, [string]$name, [string]$labels, [string]$token = '-', [string]$as = 'ci', [bool]$ephemeral = $false) {
    Assert-Admin
    if ($name -notmatch '^[A-Za-z0-9._-]+$') { Die "runner name '$name' has characters a service name cannot take" }
    if ($token -eq '-' -or [string]::IsNullOrEmpty($token)) {
        $token = New-GhToken 'registration' $repo
        if (-not $token) { Die "could not get a registration token for $repo" }
    }
    $dir = Get-RunnerDir $name
    if (Test-Path (Join-Path $dir '.runner')) {
        Log "$name is already configured; re-registering"
        Remove-RunnerDir $name   # --replace takes over the registration on GitHub
    }
    $ver = if ($env:RUNNER_VERSION) { $env:RUNNER_VERSION } else { Get-LatestRunnerVersion }
    if (-not $ver) { Die 'could not find the latest actions/runner version' }
    $zip = Get-RunnerZip $ver
    New-Item -ItemType Directory -Force -Path $dir | Out-Null
    Expand-Archive -Path $zip -DestinationPath $dir -Force
    New-Item -ItemType Directory -Force -Path $Logs | Out-Null

    $cfg = @('--unattended', '--replace', '--url', "https://github.com/$repo", '--token', $token,
        '--name', $name, '--labels', $labels, '--work', '_work', '--runasservice')
    if ($as -eq 'ci') {
        $pw = Initialize-CiUser
        Set-CiServicePassword $pw
        # CI runs as the service user, so it owns its folder; nothing else on the PC is writable to it.
        & icacls.exe $dir /grant "${CiUser}:(OI)(CI)M" /T /C /Q | Out-Null
        # The runner insists on reading every folder up the path, and $HomeDir is closed to everyone but
        # SYSTEM and Administrators. (RX) with no inheritance opens just these two folders for listing;
        # what is inside them (the token, the other runners) stays closed.
        foreach ($d in $HomeDir, $Runners) { & icacls.exe $d /grant "${CiUser}:(RX)" | Out-Null }
        $cfg += @('--windowslogonaccount', $CiUser, '--windowslogonpassword', $pw)
    } else {
        $cfg += @('--windowslogonaccount', 'NT AUTHORITY\SYSTEM')
    }
    if ($ephemeral) { $cfg += '--ephemeral' }
    Log "registering $name to $repo with labels $labels (as $(if ($as -eq 'ci') { $CiUser } else { 'SYSTEM' }))"
    Invoke-Config $dir $cfg

    New-Item -ItemType Directory -Force -Path $Conf | Out-Null
    if ($as -eq 'ci') { Write-CoresEnv $name }
    [pscustomobject]@{ repo = $repo; labels = $labels; as = $as; ephemeral = $ephemeral } |
        ConvertTo-Json | Set-Content -Path (Join-Path $Conf "$name.json") -Encoding UTF8
    if ((Test-Ci $name) -and -not (Test-RunnerShouldRun)) {
        Stop-Runner $name   # config.cmd starts the service; a paused PC (or one with every job slot taken) keeps it stopped
        Log "$name is registered; it stays stopped ($(if (Get-CiPauseReason) { Get-CiPauseReason } else { 'every job slot is taken' }))"
    } else {
        Start-Runner $name
        Log "$name is running"
    }
}

# Stops the runner's service and deletes its service and folder (not its entry on GitHub).
function Remove-RunnerDir([string]$name) {
    Stop-Runner $name
    $s = Get-Service-For $name
    if ($s) { & sc.exe delete $s.Name | Out-Null }
    $dir = Get-RunnerDir $name
    if (Test-Path $dir) { Remove-Item -Recurse -Force $dir }
    Remove-Item -Force (Join-Path $Conf "$name.json"), (Join-Path $Conf "$name.cores"), (Join-Path $Conf "$name.ram"), (Join-Path $Conf "$name.limits") -ErrorAction SilentlyContinue
}

# remove-runner NAME [TOKEN|-]   (TOKEN = a removal token; - = mint one with the stored token)
function Remove-RunnerFiles([string]$name, [string]$token = '-') {
    $dir = Get-RunnerDir $name
    $c = Get-RunnerConf $name
    Stop-Runner $name
    if (Test-Path (Join-Path $dir '.runner')) {
        if ($token -eq '-' -or [string]::IsNullOrEmpty($token)) {
            $token = ''
            if ($c) {
                try { $token = New-GhToken 'remove' $c.repo }
                catch { Log "could not get a removal token ($($_.Exception.Message)); the entry stays on GitHub" }
            }
        }
        if ($token) {
            try { Invoke-Config $dir @('remove', '--token', $token) } catch { Log "config.cmd remove failed: $($_.Exception.Message)" }
        }
    }
    Remove-RunnerDir $name
}
function Remove-Runner([string]$name, [string]$token = '-') {
    Assert-Admin
    if (-not (Test-Path (Get-RunnerDir $name))) { Die "no runner named $name here" }
    Remove-RunnerFiles $name $token
    Log "$name removed"
}

function Restart-Runners([string]$which = 'all') {
    Assert-Admin
    foreach ($n in Get-RunnerNames) {
        if ($which -ne 'all' -and $which -ne $n) { continue }
        Stop-Runner $n
        if ((Test-Ci $n) -and (Get-CiPauseReason)) { Log "$n stays stopped ($(Get-CiPauseReason))" }
        else { Start-Runner $n; Log "$n restarted" }
    }
}

# --- power -------------------------------------------------------------------------------

# True when a battery is discharging (a desktop has none, so it is never "on battery").
function Test-OnBattery {
    $b = Get-CimInstance Win32_Battery -ErrorAction SilentlyContinue
    if (-not $b) { return $false }
    return [bool]($b | Where-Object { $_.BatteryStatus -eq 1 })
}
# Why CI runners are stopped right now, or nothing when they should run.
function Get-CiPauseReason {
    if (Test-Path $CiOff) { return 'turned off' }
    if ((Test-Path $BatteryFlag) -and (Test-OnBattery)) { return 'battery' }
    return $null
}

# Plugged in: never sleep, and keep going with the lid closed. On battery: sleep as normal.
function Set-AcPower([bool]$awake) {
    $standby = if ($awake) { 0 } else { 30 }
    & powercfg.exe /change standby-timeout-ac $standby | Out-Null
    & powercfg.exe /change hibernate-timeout-ac 0 | Out-Null
    # SUB_BUTTONS LIDACTION: 0 = do nothing, 1 = sleep.
    & powercfg.exe /setacvalueindex SCHEME_CURRENT SUB_BUTTONS LIDACTION $(if ($awake) { 0 } else { 1 }) | Out-Null
    & powercfg.exe /setactive SCHEME_CURRENT | Out-Null
}

# GitHub deletes a self-hosted runner that has been offline for 14 days. Re-register any runner
# GitHub no longer lists (CI runners only while they may run).
function Invoke-Heal {
    foreach ($n in Get-RunnerNames) {
        $c = Get-RunnerConf $n
        if (-not $c -or $c.ephemeral) { continue }
        if ($c.as -eq 'ci' -and (Get-CiPauseReason)) { continue }
        if ((Test-OnGitHub $c.repo $n) -eq $false) {
            Log "$n is gone from $($c.repo) on GitHub; registering it again"
            try { Add-Runner $c.repo $n $c.labels '-' $c.as } catch { Log "could not re-register ${n}: $($_.Exception.Message)" }
        }
    }
}

# The power loop (the scheduled task runs this). Keeps CI to the times the PC is plugged in:
# each CI runner stops once it has no job running (a running job is never cut short).
function Invoke-PowerWatch {
    Assert-Admin
    $last = ''
    $lastHeal = [DateTime]::MinValue
    $lastPush = [DateTime]::MinValue
    $pushFailing = $false
    try { if (Get-SlotConfig) { Initialize-Slots } } catch { Log "slots: $($_.Exception.Message)" }
    while ($true) {
        if (((Get-Date) - $lastHeal).TotalSeconds -ge $HealEvery) {
            try { Invoke-Heal } catch { Log "heal: $($_.Exception.Message)" }
            $lastHeal = Get-Date
        }
        $state = if ((Test-Path $BatteryFlag) -and (Test-OnBattery)) { 'battery' } else { 'ac' }
        if ($state -ne $last) {
            Log "power: $state"
            try { Set-AcPower ($state -eq 'ac') } catch { Log "powercfg: $($_.Exception.Message)" }
            $last = $state
        }
        foreach ($n in Get-RunnerNames) { if (Test-Ci $n) { Sync-Cores $n } }
        try { Invoke-SlotSync } catch { Log "slot sync: $($_.Exception.Message)" }
        try { Set-JobLimits } catch { Log "limits: $($_.Exception.Message)" }
        try { Sync-WslCi } catch { Log "wsl: $($_.Exception.Message)" }
        if (((Get-Date) - $lastPush).TotalSeconds -ge $PushEvery) {
            $lastPush = Get-Date
            try {
                $res = Send-DashboardPush
                if ($res -and -not $res.ok -and -not $pushFailing) { Log "dashboard push failed (HTTP $($res.status)): $($res.message)" }
                if ($res -and $res.ok -and $pushFailing) { Log 'dashboard push works again' }
                if ($res) { $pushFailing = -not $res.ok }
            } catch { Log "push: $($_.Exception.Message)" }
        }
        # Between the full passes the slot controller looks every few seconds, so a freed slot is used at once.
        $until = (Get-Date).AddSeconds($PowerPoll)
        while ((Get-Date) -lt $until) {
            Start-Sleep -Seconds $SlotPoll
            try { Invoke-SlotSync } catch { Log "slot sync: $($_.Exception.Message)" }
        }
    }
}

function Install-PowerWatch {
    $ps = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
    $log = Join-Path $Logs 'power.log'
    $arg = "-NoProfile -ExecutionPolicy Bypass -Command `"& '$HomeDir\winrunner.ps1' power-watch *>> '$log'`""
    $action = New-ScheduledTaskAction -Execute $ps -Argument $arg
    $trigger = New-ScheduledTaskTrigger -AtStartup
    $principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
    $settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 `
        -RestartInterval (New-TimeSpan -Minutes 1) -Hidden -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Principal $principal `
        -Settings $settings -Force | Out-Null
    Start-ScheduledTask -TaskName $TaskName
}

# --- core limit --------------------------------------------------------------------------

# The limit as a number: 0 = no limit.
function Get-MaxCores {
    if (Test-Path $CoresFile) {
        $n = 0
        if ([int]::TryParse((Get-Content $CoresFile -Raw).Trim(), [ref]$n) -and $n -gt 0) { return $n }
    }
    return 0
}
function Get-TotalCores { [int](Get-CimInstance Win32_Processor | Measure-Object NumberOfLogicalProcessors -Sum).Sum }

# A runner's own limits live in $Conf\NAME.limits as `cores=N` / `ram=MB` lines; a missing line means the
# device-wide behaviour (`cores`, or nothing). Returns @{ cores = N|0; ram = MB|0 } (0 = none set).
function Get-RunnerLimit([string]$name) {
    $r = @{ cores = 0; ram = 0 }
    $f = Join-Path $Conf "$name.limits"
    if (Test-Path $f) {
        foreach ($line in Get-Content $f) {
            $n = 0
            if ($line -match '^(cores|ram)=(\d+)\s*$' -and [int]::TryParse($Matches[2], [ref]$n)) { $r[$Matches[1]] = $n }
        }
    }
    return $r
}
# The runner's own core limit, else the device-wide one: 0 = none.
# With job slots on, the default is the slot's thread count (still capped by the device-wide limit).
function Get-EffCores([string]$name) {
    $c = (Get-RunnerLimit $name).cores
    if ($c -gt 0) { return $c }
    $m = Get-MaxCores
    $slots = Get-SlotConfig
    if ($slots) { if ($m -gt 0 -and $m -lt $slots.threads) { return $m } else { return $slots.threads } }
    return $m
}

# A CI runner reads its .env file when it starts and passes it to every job: jobs see CI_MAX_CORES and
# CI_MAX_RAM_MB. Cores are also enforced (priority and affinity, Set-JobLimits). RAM is advisory only:
# a Windows Job Object memory cap would need native calls and would kill jobs mid-run, so it is not set.
# With job slots on, the runner also gets the slot hooks (they run when a job starts and ends).
function Get-RunnerEnvLines([string]$name) {
    $lines = @()
    $n = Get-EffCores $name
    $ram = (Get-RunnerLimit $name).ram
    if ($n -gt 0) { $lines += "CI_MAX_CORES=$n" }
    if ($ram -gt 0) { $lines += "CI_MAX_RAM_MB=$ram" }
    if (Get-SlotConfig) {
        $lines += "ACTIONS_RUNNER_HOOK_JOB_STARTED=$(Join-Path $StateDir 'slot-start.ps1')"
        $lines += "ACTIONS_RUNNER_HOOK_JOB_COMPLETED=$(Join-Path $StateDir 'slot-done.ps1')"
        $lines += "GIT_RUNNER_NAME=$name"
    }
    return $lines
}
function Write-CoresEnv([string]$name) {
    $envFile = Join-Path (Get-RunnerDir $name) '.env'
    $lines = @(Get-RunnerEnvLines $name)
    if ($lines.Count -gt 0) { Set-Content -Path $envFile -Value $lines -Encoding ASCII }
    else { Remove-Item -Force $envFile -ErrorAction SilentlyContinue }
}

# Brings CI runner NAME to its current .env (limits and slot hooks): rewrites it and restarts the runner.
# A runner with a job running is left alone and caught up by the power loop once it is idle.
function Sync-Cores([string]$name) {
    $want = @(Get-RunnerEnvLines $name)
    $envFile = Join-Path (Get-RunnerDir $name) '.env'
    $have = if (Test-Path $envFile) { @(Get-Content $envFile | Where-Object { $_.Trim() }) } else { @() }
    if (($want -join "`n") -eq ($have -join "`n") -or (Test-Busy $name)) { return }
    Log "${name}: env $(($have -join ' ')) -> $(($want -join ' '))"
    Stop-Runner $name
    Write-CoresEnv $name
    if (Test-RunnerShouldRun) { Start-Runner $name }
}

# The bit mask of T CPUs starting at CPU START, wrapping round TOTAL CPUs; $null = no pinning (it would be every CPU).
function Get-CpuMask([int]$start, [int]$threads, [int]$total) {
    if ($threads -ge $total -or $total -gt 63 -or $threads -lt 1) { return $null }
    $m = 0L
    for ($i = 0; $i -lt $threads; $i++) { $m = $m -bor (1L -shl (($start + $i) % $total)) }
    return [IntPtr]$m
}

# Keeps a running job to its runner's core limit: lowest-but-one priority and only its CPUs, for the
# runner's worker and everything it has started (children inherit, so new ones follow). With job slots
# on, the CPUs are the block of the slot the job holds (the hook sets them too; this catches the rest).
function Set-JobLimits {
    $all = $null
    $slots = Get-SlotConfig
    foreach ($name in Get-RunnerNames) {
        if (-not (Test-Ci $name)) { continue }
        $n = Get-EffCores $name
        if ($n -le 0) { continue }
        if (-not $all) { $all = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue }
        $total = Get-TotalCores
        $mask = $null
        if ($slots) {
            $held = Get-HeldSlots | Where-Object { $_.runner -eq $name } | Select-Object -First 1
            if ($held) { $mask = Get-CpuMask (([int]$held.num - 1) * $slots.threads % $total) $n $total }
        } elseif ($n -lt $total) { $mask = Get-CpuMask 0 $n $total }
        $dir = (Get-RunnerDir $name).ToLowerInvariant() + '\'
        $queue = New-Object System.Collections.Queue
        $all | Where-Object { $_.Name -eq 'Runner.Worker.exe' -and $_.ExecutablePath -and $_.ExecutablePath.ToLowerInvariant().StartsWith($dir) } |
            ForEach-Object { $queue.Enqueue([int]$_.ProcessId) }
        while ($queue.Count -gt 0) {
            $id = $queue.Dequeue()
            try {
                $p = [Diagnostics.Process]::GetProcessById($id)
                if ($p.PriorityClass -ne 'BelowNormal') { $p.PriorityClass = 'BelowNormal' }
                if ($mask -and $p.ProcessorAffinity -ne $mask) { $p.ProcessorAffinity = $mask }
            } catch { Write-Verbose $_.Exception.Message }
            $all | Where-Object { $_.ParentProcessId -eq $id } | ForEach-Object { $queue.Enqueue([int]$_.ProcessId) }
        }
    }
}

function Set-Cores([string]$value) {
    Assert-Admin
    $total = Get-TotalCores
    if ($value -eq 'all' -or $value -eq '0') {
        Remove-Item -Force $CoresFile -ErrorAction SilentlyContinue
        Log 'CI jobs may use every core'
    } else {
        $n = 0
        if (-not [int]::TryParse($value, [ref]$n) -or $n -lt 1 -or $n -gt $total) { Die "cores is 1 to $total, or all" }
        Set-Content -Path $CoresFile -Value $n -Encoding ASCII
        Log "CI jobs may use $n of $total cores (lower priority)"
    }
    foreach ($r in Get-RunnerNames) { if (Test-Ci $r) { Sync-Cores $r } }
    Set-JobLimits
    Sync-WslCi
}

# limit NAME cores=<N|default> ram=<MB|default>: one runner's own limits; either may be left out
# (unchanged). default drops the override, so the device-wide `cores` (or nothing) applies again.
function Set-RunnerLimit([string]$name, [string[]]$specs) {
    Assert-Admin
    if (-not (Test-Ci $name)) { Die "no CI runner named $name" }
    if (-not $specs -or $specs.Count -eq 0) { Die 'limit NAME cores=<N|default> ram=<MB|default>' }
    $total = Get-TotalCores
    $memMb = [int][math]::Floor((Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory / 1MB)
    $cur = Get-RunnerLimit $name
    foreach ($spec in $specs) {
        $k, $v = $spec -split '=', 2
        $n = 0
        switch ($k) {
            'cores' {
                if ($v -eq 'default') { $cur.cores = 0 }
                elseif ([int]::TryParse($v, [ref]$n) -and $n -ge 1 -and $n -le $total) { $cur.cores = $n }
                else { Die "cores is 1 to $total, or default" }
            }
            'ram' {
                if ($v -eq 'default') { $cur.ram = 0 }
                elseif ([int]::TryParse($v, [ref]$n) -and $n -ge 256 -and $n -le $memMb) { $cur.ram = $n }
                else { Die "ram is 256 to $memMb (MB), or default" }
            }
            default { Die "unknown limit $k (cores, ram)" }
        }
    }
    New-Item -ItemType Directory -Force -Path $Conf | Out-Null
    $lines = @()
    if ($cur.cores -gt 0) { $lines += "cores=$($cur.cores)" }
    if ($cur.ram -gt 0) { $lines += "ram=$($cur.ram)" }
    $f = Join-Path $Conf "$name.limits"
    if ($lines.Count -gt 0) { Set-Content -Path $f -Value $lines -Encoding ASCII } else { Remove-Item -Force $f -ErrorAction SilentlyContinue }
    Log "${name}: cores $(if ($cur.cores) { $cur.cores } else { 'default' }), ram $(if ($cur.ram) { $cur.ram } else { 'default' }) MB (RAM is advisory on Windows)"
    if (Test-Busy $name) { Log "$name is in a job: the limit applies when it is idle" }
    Sync-Cores $name
    Set-JobLimits
}

function Set-CiMode([string]$mode) {
    Assert-Admin
    switch ($mode) {
        'on' { Remove-Item -Force $CiOff -ErrorAction SilentlyContinue; Log 'CI is on' }
        'off' { New-Item -ItemType File -Force -Path $CiOff | Out-Null; Log 'CI is off: runners finish their job and stop' }
        default { Die 'ci on|off' }
    }
    Sync-CiRunners
    Sync-WslCi
}
function Set-BatteryMode([string]$mode) {
    Assert-Admin
    switch ($mode) {
        'pause' { New-Item -ItemType File -Force -Path $BatteryFlag | Out-Null; Log 'CI will pause on battery' }
        'run' { Remove-Item -Force $BatteryFlag -ErrorAction SilentlyContinue; Log 'CI will run on battery too' }
        default { Die 'battery pause|run' }
    }
    Sync-CiRunners
    Sync-WslCi
}
# Starts CI runners that may run now; stops idle ones that may not (the power loop finishes the rest).
function Sync-CiRunners { Sync-RunState @(Get-BusySet) }

# --- job slots -----------------------------------------------------------------------------
# A PC allows at most N CI jobs at once across its Windows and Linux (WSL) runners, each on T threads
# (CI never uses more than N x T of the CPUs; the rest stays free for Windows and for you). The cap is N
# lock folders, slot-1..slot-N, in $StateDir\slots, which the distro sees as /mnt/c/ProgramData/...:
#   * a runner's job-started hook takes a slot with mkdir (atomic, so two runners can never share one)
#     and WAITS while all N are taken, so an extra job is delayed, never run alongside; the job-completed
#     hook gives it back. The slot number picks the job's block of T CPUs.
#   * the power loop clears slots whose job has gone (cancelled, crashed) and, while every slot is held,
#     stops the idle runners so they take no more work; it starts them again when a slot frees.
# `ci off` and the battery rule still win: they stop runners whatever the slots say.
$SlotDir = Join-Path $StateDir 'slots'
$SlotConfPublic = Join-Path $StateDir 'slots.conf'   # slots=N / threads=T, copied where the hook (a plain user) can read it

# -> @{ slots; threads } when job slots are on, else $null.
function Get-SlotConfig {
    if (-not (Test-Path $SlotsFile)) { return $null }
    $r = @{ slots = 0; threads = 0 }
    foreach ($line in Get-Content $SlotsFile) {
        $n = 0
        if ($line -match '^(slots|threads)=(\d+)\s*$' -and [int]::TryParse($Matches[2], [ref]$n)) { $r[$Matches[1]] = $n }
    }
    if ($r.slots -lt 1 -or $r.threads -lt 1) { return $null }
    return $r
}

# The slots held right now: one object per slot folder, with the owner file's fields.
function Get-HeldSlots {
    if (-not (Test-Path $SlotDir)) { return @() }
    foreach ($d in Get-ChildItem $SlotDir -Directory -Filter 'slot-*' -ErrorAction SilentlyContinue) {
        $o = [ordered]@{ num = [int]($d.Name -replace '\D', ''); path = $d.FullName; runner = ''; side = ''; lease = ''; time = ''; age = ((Get-Date) - $d.LastWriteTime).TotalSeconds }
        $f = Join-Path $d.FullName 'owner'
        if (Test-Path $f) { foreach ($line in Get-Content $f) { if ($line -match '^(runner|side|lease|time)=(.*)$') { $o[$Matches[1]] = $Matches[2].Trim() } } }
        [pscustomobject]$o
    }
}

# True when every slot is held: idle runners should not take another job.
function Test-SlotHold {
    $c = Get-SlotConfig
    if (-not $c) { return $false }
    return (@(Get-HeldSlots).Count -ge $c.slots)
}

# Whether CI runners (the idle ones) may be running now: not paused (battery, `ci off`) and no slot free is missing.
function Test-RunnerShouldRun { return (-not (Get-CiPauseReason)) -and (-not (Test-SlotHold)) }

# The CI runners with a Runner.Worker process now, from one process query.
function Get-BusySet {
    $paths = @(Get-CimInstance Win32_Process -Filter "Name = 'Runner.Worker.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.ExecutablePath } | ForEach-Object { $_.ExecutablePath.ToLowerInvariant() })
    if ($paths.Count -eq 0) { return @() }
    return @(Get-RunnerNames | Where-Object {
        $dir = (Get-RunnerDir $_).ToLowerInvariant() + '\'
        $paths | Where-Object { $_.StartsWith($dir) }
    })
}

# A hold slot (slots-hold) is a lease: stale once now - time is more than lease minutes (10 by default, at most a
# day). Only the two numbers are read from the owner file, and only as digits; nothing in it is ever run.
function Test-LeaseExpired($h) {
    $lease = 10
    if ($h.lease -match '^\d{1,4}$') { $lease = [math]::Min([math]::Max([int]$h.lease, 1), 1440) }
    $since = $h.age
    if ($h.time -match '^\d{9,11}$') { $since = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds() - [int64]$h.time }
    return ($since -gt $lease * 60)
}

# Removes the slots of Windows jobs that are gone: the owner runner has no Runner.Worker. A slot with no
# owner file is given a minute (the hook writes it just after taking the slot); the Linux side clears its own.
function Clear-StaleSlots([string[]]$busy) {
    foreach ($h in Get-HeldSlots) {
        $stale = $false
        if (-not $h.side) { $stale = $h.age -gt 60 }
        elseif ($h.side -eq 'win' -and $h.age -gt 20) { $stale = -not ($busy -contains $h.runner) }
        elseif ($h.side -eq 'hold') { $stale = Test-LeaseExpired $h }
        if ($stale) {
            Remove-Item -Recurse -Force $h.path -ErrorAction SilentlyContinue
            Log "cleared stale slot $($h.num) ($(if ($h.runner) { $h.runner } else { 'no owner' }) has no job)"
        }
    }
}

# Stops idle CI runners that may not run (paused, or every slot held); starts stopped ones that may.
# A runner with a job is never stopped. A service that fails to start is left for $StartRetry seconds:
# retrying every poll is a failed logon each time, and enough of those lock $CiUser out.
$script:StartFailedAt = @{}
function Sync-RunState([string[]]$busy) {
    $reason = Get-CiPauseReason
    $hold = Test-SlotHold
    foreach ($n in Get-RunnerNames) {
        if (-not (Test-Ci $n)) { continue }
        $s = Get-Service-For $n
        if (-not $s) { continue }
        if ($s.Status -eq 'Running') { $script:StartFailedAt.Remove($n) }  # started by hand (winrunner restart): forget the failure
        $isBusy = $busy -contains $n
        if (-not $reason -and (-not $hold -or $isBusy)) {
            if ($s.Status -ne 'Running') {
                $failed = $script:StartFailedAt[$n]
                if ($failed -and ((Get-Date) - $failed).TotalSeconds -lt $StartRetry) { continue }
                Log "starting $n"
                try { Start-Runner $n; $script:StartFailedAt.Remove($n) }
                catch { $script:StartFailedAt[$n] = Get-Date; Log "could not start $n (next try in $StartRetry s): $($_.Exception.Message)" }
            }
        } elseif ($s.Status -eq 'Running' -and -not $isBusy) {
            Log "pausing $n ($(if ($reason) { $reason } else { 'every job slot is taken' }))"; Stop-Runner $n
        }
    }
}

# One pass of the slot controller; the power loop runs it every $SlotPoll seconds.
function Invoke-SlotSync {
    $busy = @(Get-BusySet)
    if (Get-SlotConfig) { Clear-StaleSlots $busy }
    Sync-RunState $busy
}

# The hook scripts the runners run (as _cirunner, so they live where it can read them). slot-hook.ps1 holds
# the logic; the two small wrappers exist because a runner hook takes no arguments, and never fail the job.
$SlotHookScript = @'
# Job slot hook, written by winrunner. acquire: take one of the PC's job slots (waiting while all are taken);
# release: give it back. Never fails the job. Settings come from slots.conf next to it.
param([string]$Action)
$ErrorActionPreference = 'Stop'
try {
    $pub = if ($env:SLOT_PUBLIC) { $env:SLOT_PUBLIC } else { Join-Path $env:ProgramData 'win-runners-public' }
    $dir = Join-Path $pub 'slots'
    $name = if ($env:GIT_RUNNER_NAME) { $env:GIT_RUNNER_NAME } else { $env:RUNNER_NAME }
    function Read-Conf { $c = @{}; foreach ($l in Get-Content (Join-Path $pub 'slots.conf') -ErrorAction SilentlyContinue) { if ($l -match '^(\w+)=(\d+)') { $c[$Matches[1]] = [int]$Matches[2] } }; $c }
    function Count-Held { @(Get-ChildItem $dir -Directory -Filter 'slot-*' -ErrorAction SilentlyContinue).Count }
    $conf = Read-Conf
    if (-not $name -or -not $conf.slots) { exit 0 }
    # A runner runs one job at a time: a slot it still holds is left over from a crash.
    foreach ($d in Get-ChildItem $dir -Directory -Filter 'slot-*' -ErrorAction SilentlyContinue) {
        $o = Join-Path $d.FullName 'owner'
        if ((Test-Path $o) -and (Select-String -Path $o -Pattern ('^runner=' + [regex]::Escape($name) + '\s*$') -Quiet)) { Remove-Item -Recurse -Force $d.FullName }
    }
    if ($Action -ne 'acquire') { exit 0 }
    New-Item -ItemType Directory -Force -Path $dir | Out-Null
    $poll = if ($env:SLOT_POLL) { [double]$env:SLOT_POLL } else { 3 }
    $lastMsg = [DateTime]::MinValue
    while ($true) {
        # The slot count is read on every pass, so `runner slots` changes apply at once: a raise lets a waiting job in,
        # a lowering keeps it out until the held slots (ALL slot-* folders, whatever their number) are fewer than N.
        $conf = Read-Conf
        if (-not $conf.slots) { exit 0 }
        $backoff = $false
        if ((Count-Held) -lt $conf.slots) { foreach ($i in 1..$conf.slots) {
            $p = Join-Path $dir "slot-$i"
            # mkdir fails when the folder exists, atomically, even against a Linux runner taking it at the same moment.
            if ($env:OS -eq 'Windows_NT') { & cmd.exe /c "mkdir `"$p`" >nul 2>&1" } else { & mkdir $p 2>$null }
            if ($LASTEXITCODE -ne 0) { continue }
            Set-Content -Path (Join-Path $p 'owner') -Encoding ASCII -Value @("runner=$name", 'side=win', "pid=$PID", "job=$($env:GITHUB_RUN_ID)",
                "time=$([DateTimeOffset]::UtcNow.ToUnixTimeSeconds())", "block=$($i - 1)")
            if ((Count-Held) -gt $conf.slots) {   # two jobs, or a lowered N, won the same instant: back out and retry
                Remove-Item -Recurse -Force $p -ErrorAction SilentlyContinue
                Start-Sleep -Milliseconds (Get-Random -Minimum 100 -Maximum 600)
                $backoff = $true
                break
            }
            Write-Host "got PC job slot $i of $($conf.slots)"
            $total = [Environment]::ProcessorCount
            $threads = [int]$conf.threads
            if ($env:OS -eq 'Windows_NT' -and $threads -gt 0 -and $threads -lt $total -and $total -le 63) {
                $mask = 0L
                for ($k = 0; $k -lt $threads; $k++) { $mask = $mask -bor (1L -shl ((($i - 1) * $threads + $k) % $total)) }
                # The runner's worker is an ancestor of this script: pin it, and the job steps it starts follow.
                $id = $PID
                foreach ($hop in 1..8) {
                    $c = Get-CimInstance Win32_Process -Filter "ProcessId = $id" -ErrorAction SilentlyContinue
                    if (-not $c) { break }
                    if ($c.Name -eq 'Runner.Worker.exe') {
                        $proc = Get-Process -Id $id
                        $proc.ProcessorAffinity = [IntPtr]$mask
                        $proc.PriorityClass = 'BelowNormal'
                        break
                    }
                    $id = $c.ParentProcessId
                }
            }
            exit 0
        } }
        if ($backoff) { continue }
        if (((Get-Date) - $lastMsg).TotalSeconds -ge 30) { Write-Host "waiting for a PC job slot ($(Count-Held) of $($conf.slots) in use)"; $lastMsg = Get-Date }
        Start-Sleep -Seconds $poll
    }
} catch { Write-Host "slot hook: $($_.Exception.Message)" }
exit 0
'@
$SlotWrapper = "try { & (Join-Path `$PSScriptRoot 'slot-hook.ps1') {0} } catch { Write-Host `"slot hook: `$(`$_.Exception.Message)`" }`r`nexit 0`r`n"

# Writes the slot folder (open to every local user, which is how a job running as _cirunner or the distro's
# user can take a slot), the hooks and the public copy of the settings.
function Initialize-Slots {
    $c = Get-SlotConfig
    New-Item -ItemType Directory -Force -Path $StateDir, $SlotDir | Out-Null
    & icacls.exe $SlotDir /grant '*S-1-5-32-545:(OI)(CI)M' | Out-Null
    Set-Content -Path (Join-Path $StateDir 'slot-hook.ps1') -Value $SlotHookScript -Encoding ASCII
    Set-Content -Path (Join-Path $StateDir 'slot-start.ps1') -Value ($SlotWrapper -replace '\{0\}', 'acquire') -Encoding ASCII
    Set-Content -Path (Join-Path $StateDir 'slot-done.ps1') -Value ($SlotWrapper -replace '\{0\}', 'release') -Encoding ASCII
    Set-Content -Path $SlotConfPublic -Value @("slots=$($c.slots)", "threads=$($c.threads)") -Encoding ASCII
}

# Something outside the runners (a Hyper-V VM, a devcontainer) holds one PC slot while it runs, as a lease:
# slots-hold takes the highest free slot (the runners take the lowest first) and must be run again more often
# than its -LeaseMin (default 10) to keep it; slots-release gives it back. Same atomic mkdir as the hooks. The owner
# file says side=hold, so the Linux side never clears it; the power loop clears it once the lease has run out.
function New-SlotFolder([string]$path) {
    if ($env:OS -eq 'Windows_NT') { & cmd.exe /c "mkdir `"$path`" >nul 2>&1" } else { & mkdir $path 2>$null }
    return ($LASTEXITCODE -eq 0)
}
function Write-HoldOwner([string]$path, [string]$name, [int]$lease, [int]$block) {
    Set-Content -Path (Join-Path $path 'owner') -Encoding ASCII -Value @("runner=$name", 'side=hold', "lease=$lease", "pid=$PID",
        "time=$([DateTimeOffset]::UtcNow.ToUnixTimeSeconds())", "block=$block")
}
# -> exit code: 0 held (or slots off), 3 timed out waiting for a free slot. Holding again refreshes the lease.
function Invoke-SlotHold([string]$name, [int]$lease, [int]$timeout) {
    $c = Get-SlotConfig
    if (-not $c) { Log 'job slots are off: nothing to hold'; return 0 }
    New-Item -ItemType Directory -Force -Path $SlotDir | Out-Null
    $poll = if ($env:SLOT_POLL) { [double]$env:SLOT_POLL } else { 3 }
    $start = Get-Date
    $noted = $false
    while ($true) {
        $mine = Get-HeldSlots | Where-Object { $_.side -eq 'hold' -and $_.runner -eq $name } | Select-Object -First 1
        if ($mine) { Write-HoldOwner $mine.path $name $lease ($mine.num - 1); Log "$name still holds slot $($mine.num) (lease $lease min)"; return 0 }
        $c = Get-SlotConfig   # live: `runner slots` may have changed N while this waits
        if (-not $c) { Log 'job slots are off: nothing to hold'; return 0 }
        $backoff = $false
        if (@(Get-HeldSlots).Count -lt $c.slots) { foreach ($k in $c.slots..1) {
            $p = Join-Path $SlotDir "slot-$k"
            if (-not (New-SlotFolder $p)) { continue }
            Write-HoldOwner $p $name $lease ($k - 1)
            if (@(Get-HeldSlots).Count -gt $c.slots) {   # won the same instant as a job, or N was lowered: back out
                Remove-Item -Recurse -Force $p -ErrorAction SilentlyContinue
                Start-Sleep -Milliseconds (Get-Random -Minimum 100 -Maximum 600)
                $backoff = $true
                break
            }
            Log "$name holds slot $k of $($c.slots) (CPU block $($k - 1), lease $lease min)"
            return 0
        } }
        if ($backoff) { continue }
        if (-not $noted) { Log "waiting for a free PC job slot for $name"; $noted = $true }
        if ($timeout -gt 0 -and ((Get-Date) - $start).TotalSeconds -ge $timeout) { Log "no slot freed within $timeout s"; return 3 }
        Start-Sleep -Seconds $poll
    }
}
# Removes every slot held by side=hold name NAME. Idempotent; always 0.
function Invoke-SlotRelease([string]$name) {
    $n = 0
    foreach ($h in @(Get-HeldSlots | Where-Object { $_.side -eq 'hold' -and $_.runner -eq $name })) {
        Remove-Item -Recurse -Force $h.path -ErrorAction SilentlyContinue
        $n++
    }
    Log "$name released $n slot(s)"
    return 0
}
# slots-hold / slots-release [-Name N] [-LeaseMin M] [-TimeoutSec S]: parses the words after the command; 2 = bad usage.
function Invoke-SlotVmCommand([string]$kind, [string[]]$words) {
    $name = 'claude-vm'; $lease = 10; $timeout = 0
    $usage = "usage: slots-$kind [-Name NAME]$(if ($kind -eq 'hold') { ' [-LeaseMin M (1-1440)] [-TimeoutSec S]' })"
    for ($i = 0; $i -lt $words.Count; $i += 2) {
        $v = if ($i + 1 -lt $words.Count) { $words[$i + 1] } else { $null }
        $n = 0
        $ok = $null -ne $v
        if ($ok) {
            switch ($words[$i]) {
                '-Name' { $name = $v; $ok = $v -match '^[A-Za-z0-9._-]+$' }
                '-LeaseMin' { $ok = ($kind -eq 'hold') -and [int]::TryParse($v, [ref]$n) -and $n -ge 1 -and $n -le 1440; $lease = $n }
                '-TimeoutSec' { $ok = ($kind -eq 'hold') -and [int]::TryParse($v, [ref]$n) -and $n -ge 0; $timeout = $n }
                default { $ok = $false }
            }
        }
        if (-not $ok) { [Console]::Error.WriteLine($usage); return 2 }
    }
    # [-1]: only the exit code, whatever else the helpers wrote to the pipeline
    if ($kind -eq 'hold') { $rc = @(Invoke-SlotHold $name $lease $timeout)[-1] } else { $rc = @(Invoke-SlotRelease $name)[-1] }
    # Nudge the controller now (it would anyway within seconds); it needs administrator rights, so never fail on it.
    try { Invoke-SlotSync | Out-Null } catch { Write-Verbose $_.Exception.Message }
    return $rc
}

# slots N [THREADS] | off: the PC's job slots. On: N slots of THREADS CPUs each (default 8), N Windows runners
# (win-1, win-1-ci-2, ...) plus the Linux ones, which follow through the state file. Off: back to no cap.
function Set-Slots([string]$value, [string]$threads = '') {
    Assert-Admin
    $total = Get-TotalCores
    if ($value -eq 'off') {
        Remove-Item -Force $SlotsFile, $SlotConfPublic -ErrorAction SilentlyContinue
        Get-HeldSlots | ForEach-Object { Remove-Item -Recurse -Force $_.path -ErrorAction SilentlyContinue }
        Log 'job slots are off'
    } else {
        $n = 0; $t = 0
        if (-not [int]::TryParse($value, [ref]$n) -or $n -lt 1 -or $n -gt 32) { Die 'slots is 1 to 32, or off' }
        if ($threads) {
            if (-not [int]::TryParse($threads, [ref]$t) -or $t -lt 1 -or $t -gt $total) { Die "threads is 1 to $total" }
        } else {
            $old = Get-SlotConfig
            $t = if ($old) { $old.threads } else { [math]::Min(8, $total) }
        }
        Set-Content -Path $SlotsFile -Value @("slots=$n", "threads=$t") -Encoding ASCII
        Initialize-Slots
        Log "job slots: at most $n CI jobs at once, $t threads each (of $total)"
        $host_ = (Get-Content $HostFile -Raw).Trim()
        $primary = Get-RunnerConf $host_
        if ($primary) {
            foreach ($i in 2..$n) {
                $rn = "$host_-ci-$i"
                if (Test-Path (Get-RunnerDir $rn)) { continue }
                try { Add-Runner $primary.repo $rn $primary.labels '-' 'ci' } catch { Log "could not add ${rn}: $($_.Exception.Message)" }
            }
        } else { Log "no CI runner named $host_ here to copy: add the extra Windows runners with add-runner" }
    }
    foreach ($r in Get-RunnerNames) { if (Test-Ci $r) { Sync-Cores $r } }
    Invoke-SlotSync
    Set-JobLimits
    Sync-WslCi
}

# --- status ------------------------------------------------------------------------------

function Get-Uptime { $o = Get-CimInstance Win32_OperatingSystem; [int]((Get-Date) - $o.LastBootUpTime).TotalSeconds }

function Show-Status {
    Write-Host "host:      $(if (Test-Path $HostFile) { (Get-Content $HostFile -Raw).Trim() } else { '(not installed)' })"
    Write-Host "power:     $(if (Test-OnBattery) { 'battery' } else { 'ac' })   pause on battery: $(Test-Path $BatteryFlag)   CI off: $(Test-Path $CiOff)"
    $reason = Get-CiPauseReason
    Write-Host "CI:        $(if ($reason) { "paused ($reason)" } else { 'taking jobs' })"
    $mc = Get-MaxCores
    Write-Host "cores:     $(if ($mc -gt 0) { "CI jobs limited to $mc of $(Get-TotalCores)" } else { 'no limit' })"
    $sc = Get-SlotConfig
    if ($sc) {
        $held = @(Get-HeldSlots)
        Write-Host "slots:     $($held.Count) of $($sc.slots) in use, $($sc.threads) threads each$(if (Test-SlotHold) { ' (full: idle runners are paused)' })"
        foreach ($h in $held) { Write-Host ("  slot-{0}  {1} ({2})" -f $h.num, $h.runner, $h.side) }
    } else { Write-Host 'slots:     off (winrunner slots N)' }
    $credRepo = ''
    foreach ($n in Get-RunnerNames) { $c = Get-RunnerConf $n; if ($c -and -not $credRepo) { $credRepo = $c.repo } }
    Write-Host "token:     $(Get-CredentialLine $credRepo)"
    Write-Host "uptime:    $([int]((Get-Uptime) / 3600)) h"
    Write-Host "dashboard: $(if (Test-Path $PushConf) { "pushing health every $PushEvery s" } else { 'health push not set up (winrunner push-setup)' })"
    Write-Host 'runners:'
    foreach ($n in Get-RunnerNames) {
        $c = Get-RunnerConf $n
        Write-Host ("  {0,-16} {1,-8} {2}  {3}" -f $n, (Get-RunnerState $n), $(if (Test-Busy $n) { 'busy' } else { 'idle' }),
            $(if ($c) { "$($c.repo) [$($c.labels)]" } else { '' }))
    }
    $t = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Write-Host "power watch: $(if ($t) { $t.State } else { 'not installed' })"
}

# One line of JSON, in the same shape macrunner info prints, so the dashboard can read both.
function Show-Info {
    $cs = Get-CimInstance Win32_ComputerSystem
    $os = Get-CimInstance Win32_OperatingSystem
    $cpu = Get-CimInstance Win32_Processor | Select-Object -First 1
    $disk = Get-CimInstance Win32_LogicalDisk -Filter "DeviceID = '$($env:SystemDrive)'"
    $bat = Get-CimInstance Win32_Battery -ErrorAction SilentlyContinue | Select-Object -First 1
    $reason = Get-CiPauseReason
    $runners = @(foreach ($n in Get-RunnerNames) {
        $c = Get-RunnerConf $n
        [ordered]@{
            name = $n; kind = $(if (Test-Ci $n) { 'ci' } elseif ($n -like '*-admin') { 'admin' } else { 'root' })
            state = Get-RunnerState $n; busy = (Test-Busy $n); repo = $(if ($c) { "https://github.com/$($c.repo)" } else { '' })
            limit = [ordered]@{ cores = $(if ((Get-RunnerLimit $n).cores -gt 0) { (Get-RunnerLimit $n).cores } else { $null }); ram_mb = $(if ((Get-RunnerLimit $n).ram -gt 0) { (Get-RunnerLimit $n).ram } else { $null }) }
        }
    })
    $info = [ordered]@{
        host = $(if (Test-Path $HostFile) { (Get-Content $HostFile -Raw).Trim() } else { '' })
        platform = 'windows'
        computer_name = $env:COMPUTERNAME
        model = "$($cs.Manufacturer) $($cs.Model)".Trim()
        chip = $cpu.Name.Trim()
        cores = [int]$cpu.NumberOfLogicalProcessors
        memory_gb = [int][math]::Round($cs.TotalPhysicalMemory / 1GB)
        windows = "$($os.Caption) $($os.Version)".Trim()
        uptime_s = Get-Uptime
        disk_total_gb = [int][math]::Round($disk.Size / 1GB)
        disk_free_gb = [int][math]::Round($disk.FreeSpace / 1GB)
        memory_free_pct = [int][math]::Round(100 * $os.FreePhysicalMemory * 1KB / $cs.TotalPhysicalMemory)
        battery = [ordered]@{
            present = [bool]$bat; percent = $(if ($bat) { [int]$bat.EstimatedChargeRemaining } else { $null })
            source = $(if (Test-OnBattery) { 'Battery' } else { 'AC' })
        }
        settings = [ordered]@{ max_cores = (Get-MaxCores); slots = $(if (Get-SlotConfig) { (Get-SlotConfig).slots } else { $null }); slot_threads = $(if (Get-SlotConfig) { (Get-SlotConfig).threads } else { $null }); slots_held = @(Get-HeldSlots).Count; pause_on_battery = (Test-Path $BatteryFlag); ci_enabled = -not (Test-Path $CiOff) }
        ci_paused = [bool]$reason; ci_paused_reason = [string]$reason
        token_stored = (Test-HasCredential)
        credential = (Get-CredentialKind)
        winrunner_sha = $(if (Test-Path "$HomeDir\winrunner.ps1") { (Get-FileHash "$HomeDir\winrunner.ps1" -Algorithm SHA1).Hash.Substring(0, 7).ToLower() } else { '' })
        runners = $runners
    }
    $info | ConvertTo-Json -Depth 5 -Compress
}

# --- health push to the dashboard ----------------------------------------------------------
# The dashboard cannot reach a PC (no Tailscale SSH on Windows), so the PC reports its own `info`
# JSON to POST <url> with a bearer token. Off unless `push-setup` wrote $PushConf. The token is only
# ever read from $PushTokenFile and sent in a header: never logged, never on a command line.

# -> @{ ok; status; message }, or $null when push is not set up. Never throws.
function Send-DashboardPush {
    if (-not (Test-Path $PushConf)) { return $null }
    try {
        # not $conf: PowerShell variables are case-insensitive and $Conf is the script's conf folder, which Show-Info reads
        $push = Get-Content $PushConf -Raw | ConvertFrom-Json
        $tokFile = if ($push.token_file) { [string]$push.token_file } else { $PushTokenFile }
        if (-not $push.url -or -not (Test-Path $tokFile)) { return @{ ok = $false; status = 0; message = 'push config or token file missing; run: winrunner push-setup' } }
        $token = (Get-Content $tokFile -Raw).Trim()
        $body = [Text.Encoding]::UTF8.GetBytes([string](Show-Info))
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        $r = Invoke-WebRequest -UseBasicParsing -Method Post -Uri ([string]$push.url) -Body $body -ContentType 'application/json' `
            -Headers @{ Authorization = "Bearer $token" } -TimeoutSec $PushTimeout
        return @{ ok = $true; status = [int]$r.StatusCode; message = 'ok' }
    } catch {
        $code = 0
        if ($_.Exception.Response) { $code = [int]$_.Exception.Response.StatusCode }
        # The message can name the URL but never the token (it is only in the header).
        return @{ ok = $false; status = $code; message = $_.Exception.Message }
    }
}

# push-setup URL TOKENFILE: store the dashboard's address and this PC's token. TOKENFILE holds just the
# token (the same value as this PC's line in the dashboard's push tokens); it is copied to a file only
# SYSTEM and Administrators can read, and the original is left for you to delete.
function Set-PushConfig([string]$url, [string]$tokenFile) {
    Assert-Admin
    if ($url -notmatch '^https?://[^\s/]+') { Die 'URL must look like https://runners.example.com' }
    $base = ($url -replace '/api/push-info/?$', '').TrimEnd('/')
    if (-not (Test-Path $tokenFile)) { Die "token file $tokenFile not found" }
    $token = (Get-Content $tokenFile -Raw).Trim()
    if ($token -notmatch '^[A-Za-z0-9._~+/=-]{20,200}$') { Die 'the token file must hold one token of 20 or more letters, digits and . _ ~ + / = -' }
    if (-not (Test-Path $HostFile)) { Die 'this PC has no host name yet: install the runners first' }
    New-Item -ItemType Directory -Force -Path $HomeDir | Out-Null
    $tmp = "$PushTokenFile.new"
    Set-Content -Path $tmp -Value $token -NoNewline -Encoding ASCII
    & icacls.exe $tmp /inheritance:r /grant:r '*S-1-5-18:(F)' '*S-1-5-32-544:(F)' | Out-Null
    Move-Item -Force $tmp $PushTokenFile
    [ordered]@{ url = "$base/api/push-info"; host = (Get-Content $HostFile -Raw).Trim(); token_file = $PushTokenFile } |
        ConvertTo-Json | Set-Content -Path $PushConf -Encoding ASCII
    Write-Host "health push set up: $base/api/push-info as $((Get-Content $HostFile -Raw).Trim()) (token stored in $PushTokenFile)"
    Write-Host "The power watch reports every $PushEvery s once it runs this version (winrunner self-update, or runner update HOST). Delete $tokenFile now."
}

# push-test: one push now, with the HTTP status.
function Invoke-PushTest {
    $res = Send-DashboardPush
    if ($null -eq $res) { Die 'push is not set up here: run winrunner push-setup URL TOKENFILE' }
    if ($res.ok) { Write-Host "HTTP $($res.status): the dashboard accepted this PC's info"; return }
    Write-Host "push failed$(if ($res.status) { " with HTTP $($res.status)" }): $($res.message)"
    switch ($res.status) {
        401 { Write-Host 'the dashboard does not know this token: check this PC''s line in its push tokens, then restart the dashboard' }
        403 { Write-Host 'the token belongs to another host than this PC reports' }
        429 { Write-Host 'too many pushes or bad tokens; wait a minute' }
        503 { Write-Host 'the dashboard has no push tokens yet (empty push_tokens secret)' }
    }
    exit 1
}

function Get-RunnerLogTail([string]$name, [int]$lines) {
    $dir = Join-Path (Get-RunnerDir $name) '_diag'
    if (-not (Test-Path $dir)) { Write-Host "(no diagnostics for $name)"; return }
    foreach ($kind in 'Runner_*.log', 'Worker_*.log') {
        $f = Get-ChildItem $dir -Filter $kind | Sort-Object LastWriteTime -Descending | Select-Object -First 1
        if ($f) { Write-Host "--- $($f.Name)"; Get-Content $f.FullName -Tail $lines }
    }
}
function Show-Logs([string]$what, [int]$lines = 60) {
    if ($what -eq 'power') { Get-Content (Join-Path $Logs 'power.log') -Tail $lines -ErrorAction SilentlyContinue; return }
    if (-not (Test-Path (Get-RunnerDir $what))) { Die "no runner named $what here" }
    Get-RunnerLogTail $what $lines
}

# Checks. The exit code is the number of problems found.
function Invoke-Doctor {
    Show-Status
    Write-Host 'checks:'
    $failed = New-Object System.Collections.ArrayList
    function Check([string]$what, [bool]$ok, [string]$hint) {
        if ($ok) { Write-Host "  ok    $what" } else { Write-Host "  FAIL  $what  ($hint)"; [void]$failed.Add($what) }
    }
    Check 'GitHub credential stored' (Test-HasCredential) 'run: winrunner set-app (or set-token)'
    if (Test-AppConfigured) {
        $minted = $false
        try { Initialize-AppToken; $minted = $true } catch { Write-Verbose $_.Exception.Message }
        Check "GitHub App token can be minted (installation on $AppOrg)" $minted 'bad key, App not installed on the org, or no network'
    }
    $api = $false
    if (Test-HasCredential) { try { Invoke-Gh 'GET' 'rate_limit' | Out-Null; $api = $true } catch { Write-Verbose $_.Exception.Message } }
    Check 'GitHub reachable with the credential' $api 'token expired or revoked, App key revoked, or no network'
    Check 'power watch task' ([bool](Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue)) 'run: winrunner install-power-watch'
    if (Get-SlotConfig) {
        Check 'slot hooks installed' ((Test-Path (Join-Path $StateDir 'slot-start.ps1')) -and (Test-Path (Join-Path $StateDir 'slot-hook.ps1'))) 'run: winrunner slots N'
    }
    foreach ($n in Get-RunnerNames) {
        $c = Get-RunnerConf $n
        $should = -not ((Test-Ci $n) -and -not (Test-RunnerShouldRun))
        $state = Get-RunnerState $n
        Check "$n service" ($state -eq 'running' -or (-not $should -and $state -eq 'stopped')) "state is $state; try: winrunner restart $n"
        if ($c -and -not $c.ephemeral -and $api) {
            Check "$n listed on GitHub" ((Test-OnGitHub $c.repo $n) -ne $false) 'GitHub dropped it; the power watch re-registers it'
        }
        $procs = @(Get-CimInstance Win32_Process -Filter "Name = 'Runner.Listener.exe'" -ErrorAction SilentlyContinue |
            Where-Object { $_.ExecutablePath -and $_.ExecutablePath.StartsWith((Get-RunnerDir $n) + '\', [StringComparison]::OrdinalIgnoreCase) })
        Check "$n has at most one listener" ($procs.Count -le 1) 'a second Runner.Listener is running; restart the runner'
    }
    Write-Host "$($failed.Count) problem(s)"
    return $failed.Count
}

# --- install and maintenance -------------------------------------------------------------

# bootstrap ENVFILE: first install. ENVFILE is a JSON file the installer writes (and this deletes).
function Invoke-Bootstrap([string]$envFile) {
    Assert-Admin
    if (-not (Test-Path $envFile)) { Die "no $envFile" }
    $e = Get-Content $envFile -Raw | ConvertFrom-Json
    Remove-Item -Force $envFile
    foreach ($d in $HomeDir, $Runners, $Logs, $Conf) { New-Item -ItemType Directory -Force -Path $d | Out-Null }
    if ($e.GITHUB_APP_ID -and $e.GITHUB_APP_KEY_B64) {
        try { Save-App ([string]$e.GITHUB_APP_ID) ([string]$e.GITHUB_APP_KEY_B64) } catch { Die 'the installer''s GitHub App key is not a PEM private key' }
        Remove-Item -Force $AuthFile -ErrorAction SilentlyContinue   # an older PAT is no longer needed
        try { Initialize-AppToken } catch { Die "the installer's GitHub App cannot mint a token (not installed on $AppOrg, or its key was revoked?)" }
        try { Invoke-Gh 'GET' "repos/$($e.ADMIN_REPO)" | Out-Null } catch { Die "the installer's GitHub App cannot reach $($e.ADMIN_REPO) (is the repo in the App's installation?)" }
    } else {
        if (-not $e.RUNNER_PAT) { Die 'the installer carries no GitHub App key or token' }
        Clear-App
        Save-Token $e.RUNNER_PAT
        try { Invoke-Gh 'GET' "repos/$($e.ADMIN_REPO)" | Out-Null } catch { Die 'the installer''s GitHub token does not work (expired or revoked?)' }
    }
    if ($e.HOST) { $name = $e.HOST }
    elseif (Test-Path $HostFile) { $name = (Get-Content $HostFile -Raw).Trim() }
    else { $name = Get-NextName $(if ($e.CI_REPO) { $e.CI_REPO } else { $e.ADMIN_REPO }) $e.ADMIN_REPO }
    Set-Content -Path $HostFile -Value $name -Encoding ASCII
    Log "this PC is $name"
    New-Item -ItemType File -Force -Path $BatteryFlag | Out-Null
    Add-Runner $e.ADMIN_REPO "$name-admin" "win-admin,$name$(if ($e.EXTRA_ADMIN_LABELS) { ',' + $e.EXTRA_ADMIN_LABELS })" '-' 'admin' ([bool]$e.ADMIN_EPHEMERAL)
    if ($e.CI_REPO) { Add-Runner $e.CI_REPO $name $e.CI_LABELS '-' 'ci' }
    Install-PowerWatch
    if ($e.WSL_SET) {
        try { Install-WslSet $e } catch { Log "WSL runners failed: $($_.Exception.Message) (the Windows runner is installed; rerun the installer to retry)" }
    }
    Log 'bootstrap done'
    Show-Status
}

# The Linux runners cannot see this PC's battery, and the SYSTEM-side tools cannot reach a distro that
# belongs to another Windows user. So this side only writes a small state file (readable by anyone),
# and a service inside the distro follows it: CI on/off (battery, `macs ci`), the core limit and the job
# slots (their lock folders are in this same folder, so both sides share them).
function Sync-WslCi {
    if (-not (Test-Path (Join-Path $HomeDir 'wsl-host'))) { return }
    New-Item -ItemType Directory -Force -Path $StateDir | Out-Null
    $cores = Get-MaxCores
    $ci = if (Get-CiPauseReason) { 'off' } else { 'on' }
    $sc = Get-SlotConfig
    $lines = @("ci=$ci", "cores=$(if ($cores -gt 0) { $cores } else { 'all' })")
    $lines += if ($sc) { "slots=$($sc.slots)", "threads=$($sc.threads)" } else { 'slots=off' }
    Set-Content -Path $StateFile -Value $lines -Encoding ASCII
}

# --- WSL set: Linux runners in a WSL distro (adopts one that is already there) -------------------
# Never `wsl --shutdown` and never .wslconfig: only the one distro is ever terminated, and only
# when it is brand new and needs systemd switched on.

function Invoke-WslIn([string]$distro, [string]$cmd) {  # run a bash command in DISTRO as root
    # Tools such as the uv installer print progress on stderr; under -Stop PowerShell turns every such
    # line into an error, so stderr is folded into the output and only the exit code decides.
    $old = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try { & wsl.exe -d $distro -u root -- bash -c $cmd 2>&1 | ForEach-Object { Write-Host "$_" } }
    finally { $ErrorActionPreference = $old }
    if ($LASTEXITCODE) { throw "wsl command failed (exit $LASTEXITCODE): $cmd" }
}

# Copies a Windows file into DISTRO as an executable, through the distro's own view of the drive (wslpath, so a custom
# automount root works too), never on the command line: Windows caps a command line at 32767 characters, and linuxrunner
# in base64 is well past that ("The filename or extension is too long"). Throws if the copy fails.
function Copy-ToWsl([string]$distro, [string]$file, [string]$dest) {
    Invoke-WslIn $distro ("install -m 755 `"`$(wslpath -u '" + $file + "')`" '" + $dest + "'")
}

function Test-WslDistro([string]$distro) {
    $l = (& wsl.exe -l -q 2>$null) -replace "`0", ''  # wsl.exe prints UTF-16
    [bool]($l -split "`r?`n" | Where-Object { $_.Trim() -eq $distro })
}

function Get-NextWslName([string]$adminRepo) {
    foreach ($n in 1..50) {
        $a = Test-OnGitHub $adminRepo "wsl-$n-admin"
        if ($null -eq $a) { Die 'could not list runners on GitHub (token or network)' }
        if (-not $a) { return "wsl-$n" }
    }
    Die 'no free wsl-N name'
}

# $e: the bootstrap settings. WSL_SET is "set:count:tags;..." and the default is "wsl:2:linux-ci,docker".
function Install-WslSet($e) {
    $distro = if ($e.WSL_DISTRO) { $e.WSL_DISTRO } else { 'gh-runner' }
    & wsl.exe --status *> $null
    if ($LASTEXITCODE) {
        Log 'WSL is not installed: installing it (this may need a restart)'
        & wsl.exe --install --no-distribution 2>&1 | Out-Null
        Log 'WSL is installed but Windows must restart before it works. Restart this PC, then run this installer again: it finishes the Linux runners. Skipping them for now.'
        return
    }
    $fresh = -not (Test-WslDistro $distro)
    if ($fresh) {
        Log "creating WSL distro $distro (Ubuntu 24.04)"
        & wsl.exe --install -d Ubuntu-24.04 --name $distro --no-launch
        if ($LASTEXITCODE) { Die "could not create the $distro distro (needs a current WSL: wsl --update)" }
    } else { Log "adopting existing WSL distro ${distro}: its runners are left running" }
    $conf = & wsl.exe -d $distro -u root -- bash -c 'cat /etc/wsl.conf 2>/dev/null'
    if (-not ($conf -match 'systemd\s*=\s*true')) {
        if (-not $fresh) { Die "$distro has no systemd (/etc/wsl.conf); not touching an existing distro that needs a restart" }
        Invoke-WslIn $distro "printf '[boot]\nsystemd=true\n' >> /etc/wsl.conf"
        & wsl.exe --terminate $distro   # this one distro only, never --shutdown
        Start-Sleep -Seconds 3
    }
    # A new distro is provisioned (git, python, uv) before any runner may take a job; an adopted one
    # already runs jobs, so it is only marked. If provisioning fails the runners stay off.
    $provisioned = $true
    if ($fresh) {
        try {
            Copy-ToWsl $distro (Join-Path $HomeDir 'linux-provision.sh') '/tmp/linux-provision.sh'
            Invoke-WslIn $distro 'bash /tmp/linux-provision.sh'
        }
        catch { $provisioned = $false; Log "provisioning the Linux box failed ($($_.Exception.Message)): its runners stay off until it works" }
    } else { Invoke-WslIn $distro 'mkdir -p /opt/git-runner && touch /opt/git-runner/provisioned' }
    Copy-ToWsl $distro (Join-Path $HomeDir 'linuxrunner') '/tmp/linuxrunner'
    # Runners labelled docker need Docker Engine (x86-64 only; the Mac VMs have none). On a new distro a failure keeps
    # the runners off like provisioning does; an adopted distro already runs jobs, so it only logs.
    if ($e.WSL_SET -match '(^|[:,;])docker([,;]|$)') {
        try { Invoke-WslIn $distro 'bash /tmp/linuxrunner install-docker' }
        catch {
            if ($fresh) { $provisioned = $false }
            Log "installing Docker failed ($($_.Exception.Message)): jobs labelled docker would fail$(if ($fresh) { ', so its runners stay off' })"
        }
    }
    $wname = Get-NextWslName $e.ADMIN_REPO
    if (Test-Path (Join-Path $HomeDir 'wsl-host')) { $wname = (Get-Content (Join-Path $HomeDir 'wsl-host') -Raw).Trim() }
    Set-Content -Path (Join-Path $HomeDir 'wsl-host') -Value $wname -Encoding ASCII
    Invoke-WslIn $distro "bash /tmp/linuxrunner bootstrap $wname && rm -f /tmp/linuxrunner"
    $lrBin = '/opt/git-runner/linuxrunner'
    $units = (& wsl.exe -d $distro -u root -- bash -c "$lrBin info") | ConvertFrom-Json
    $have = @($units.runners | ForEach-Object { $_.name })
    $token = New-GhToken 'registration' $e.ADMIN_REPO
    if ($have -notcontains "$wname-admin") { Invoke-WslIn $distro "$lrBin install-admin $($e.ADMIN_REPO) $token" }
    $prefix = (Get-Content $HostFile -Raw).Trim()   # this PC's name (win-1), so the runners read win-1-wsl-1
    $ciToken = New-GhToken 'registration' $e.CI_REPO
    foreach ($spec in ($e.WSL_SET -split ';' | Where-Object { $_ })) {
        $set, $count, $labels = $spec -split ':', 3
        foreach ($i in 1..[int]$count) {
            $rn = "$prefix-$set-$i"
            if ($have -contains $rn) { Log "$rn already here"; continue }
            if (Test-OnGitHub $e.CI_REPO $rn) { Log "$rn is already registered on GitHub (another box?): skipped"; continue }
            Invoke-WslIn $distro "$lrBin add-runner $($e.CI_REPO) $rn $labels $ciToken ci"
        }
    }
    if (-not $provisioned) { Invoke-WslIn $distro "$lrBin ci off" }
    Install-WslFollower $distro
    Install-WslBoot $distro
    Log "WSL runners done: $wname (macs doctor $wname)"
}

# A service in the distro that follows linux-state (see Sync-WslCi) every 2 seconds and keeps the job
# slots (linuxrunner follow). linuxrunner installs the service itself.
function Install-WslFollower([string]$distro) {
    Sync-WslCi
    $drive = $env:SystemDrive.Substring(0, 1).ToLower()
    $path = "/mnt/$drive/" + ($StateFile.Substring(3) -replace '\\', '/')
    Invoke-WslIn $distro "/opt/git-runner/linuxrunner install-follower '$path'"
}

# WSL stops a distro when nothing is running in it, and nothing starts it after a reboot. This
# task starts it at boot and keeps one process in it, so the runners (systemd services) come up
# with Windows, no login needed (S4U: runs as this user without a stored password).
function Install-WslBoot([string]$distro) {
    $user = [Security.Principal.WindowsIdentity]::GetCurrent().Name
    # powershell -WindowStyle Hidden, so no console window is ever shown for the long-running wsl.exe.
    $ps = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
    $action = New-ScheduledTaskAction -Execute $ps -Argument "-NoProfile -NonInteractive -WindowStyle Hidden -Command `"& wsl.exe -d $distro -u root --exec /bin/sh -c 'exec sleep infinity'`""
    $trigger = New-ScheduledTaskTrigger -AtStartup
    $principal = New-ScheduledTaskPrincipal -UserId $user -LogonType S4U -RunLevel Highest
    $settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 `
        -RestartInterval (New-TimeSpan -Minutes 1) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable
    Register-ScheduledTask -TaskName 'win-runners wsl boot' -Action $action -Trigger $trigger -Principal $principal `
        -Settings $settings -Force | Out-Null
    Start-ScheduledTask -TaskName 'win-runners wsl boot'
}

# add-wsl: set up (or adopt) the WSL runners on a PC that is already installed, from its settings.

# The credential files, for switching between the App and the PAT with a way back.
function Get-CredentialFiles { @($AuthFile, $AppIdFile, $AppKeyFile, $AppInstallFile, $AppTokenFile, $AppExpiryFile) }

function Save-App([string]$id, [string]$keyB64) {  # store the App's id and (decoded) private key; the cached installation and token go
    $pem = [Text.Encoding]::ASCII.GetString([Convert]::FromBase64String($keyB64.Trim()))
    [void](ConvertFrom-RsaPem $pem)   # throws unless it is a readable RSA key
    Write-Private $AppKeyFile $pem
    Write-Private $AppIdFile $id.Trim()
    Remove-Item -Force $AppInstallFile, $AppTokenFile, $AppExpiryFile -ErrorAction SilentlyContinue
}

function Clear-App { Remove-Item -Force $AppIdFile, $AppKeyFile, $AppInstallFile, $AppTokenFile, $AppExpiryFile -ErrorAction SilentlyContinue }

function Get-CredentialSnapshot {  # every credential file there is, name -> text
    $snap = @{}
    foreach ($f in Get-CredentialFiles) { if (Test-Path $f) { $snap[$f] = Get-Content $f -Raw } }
    $snap
}

function Restore-Credentials($snap) {  # put back exactly what Get-CredentialSnapshot saved
    Remove-Item -Force (Get-CredentialFiles) -ErrorAction SilentlyContinue
    foreach ($f in $snap.Keys) { Write-Private $f $snap[$f] }
}

# set-token REPO|ORG: replace the stored credential with the PAT in $env:NEW_GITHUB_TOKEN (dropping any App
# key, which would otherwise win), after checking it can list REPO's runners; on failure the old credential
# comes back. The admin workflow's rotate-token passes RUNNER_PAT.
function Set-NewToken([string]$repo) {
    Assert-Admin
    if (-not $env:NEW_GITHUB_TOKEN) { Die 'put the new token in NEW_GITHUB_TOKEN' }
    $keep = Get-CredentialSnapshot
    Clear-App
    Save-Token $env:NEW_GITHUB_TOKEN
    try {
        Invoke-Gh 'GET' "$(Api-Path $repo)/actions/runners?per_page=1" | Out-Null
        Log "GitHub token replaced; expires $(Get-TokenExpiry $repo)"
    } catch {
        Restore-Credentials $keep
        Die "the new token cannot list $repo's runners; kept the old credential"
    }
}

# set-app REPO|ORG: switch this PC to a GitHub App: store the id in $env:NEW_APP_ID and the private key in
# $env:NEW_APP_KEY_B64 (the PEM, base64 on one line: the admin workflow passes the CI_APP_ID and
# CI_APP_PRIVATE_KEY secrets that way), check that a token can be minted and can list REPO's runners, then
# delete the stored PAT. On failure the previous credential (PAT or older App key) comes back.
function Set-NewApp([string]$repo) {
    Assert-Admin
    if (-not $env:NEW_APP_ID -or -not $env:NEW_APP_KEY_B64) { Die 'put the App id in NEW_APP_ID and its base64 key in NEW_APP_KEY_B64' }
    $keep = Get-CredentialSnapshot
    try {
        Save-App $env:NEW_APP_ID $env:NEW_APP_KEY_B64
        Initialize-AppToken
        Invoke-Gh 'GET' "$(Api-Path $repo)/actions/runners?per_page=1" | Out-Null
    } catch {
        Restore-Credentials $keep
        Die "the GitHub App could not mint a token that lists $repo's runners (bad key, not installed on $AppOrg, or no network); kept the old credential"
    }
    Remove-Item -Force $AuthFile -ErrorAction SilentlyContinue
    Log "this PC now mints GitHub App tokens (App $((Get-Content $AppIdFile -Raw).Trim()), valid until $(((Get-Content $AppExpiryFile -Raw).Trim() -split ' ')[1])); the stored PAT is deleted"
}

# self-update FILE: replace this script (the admin workflow passes the repo's copy).
function Update-Self([string]$src) {
    Assert-Admin
    $errors = $null
    [void][Management.Automation.Language.Parser]::ParseFile($src, [ref]$null, [ref]$errors)
    if ($errors) { Die "$src does not parse: $($errors[0].Message)" }
    Copy-Item -Force $src "$HomeDir\winrunner.ps1.new"
    Move-Item -Force "$HomeDir\winrunner.ps1.new" "$HomeDir\winrunner.ps1"
    Log 'winrunner updated; restarting power watch'
    # The workflow job that runs this lives in a runner this task never touches, so a restart is safe.
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Start-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
}

function Invoke-Uninstall {
    Assert-Admin
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    foreach ($n in Get-RunnerNames) { try { Remove-RunnerFiles $n '-' } catch { Log "could not remove ${n}: $($_.Exception.Message)" } }
    try { Set-AcPower $false } catch { Write-Verbose $_.Exception.Message }
    if (Get-LocalUser -Name $CiUser -ErrorAction SilentlyContinue) { Remove-LocalUser -Name $CiUser }
    $profile = Join-Path $env:SystemDrive "Users\$CiUser"
    if (Test-Path $profile) { Remove-Item -Recurse -Force $profile -ErrorAction SilentlyContinue }
    # Removing the folder we run from is the last thing: a copy in %TEMP% finishes the job.
    $cmd = "Start-Sleep 3; Remove-Item -Recurse -Force '$HomeDir'"
    Start-Process (Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe') `
        -ArgumentList '-NoProfile', '-Command', $cmd -WindowStyle Hidden
    Write-Host "removed. Delete the runners' entries under each repo's Settings > Actions > Runners."
}

function Show-Usage {
    @'
winrunner: GitHub Actions runners on this Windows PC (run in an administrator PowerShell)

  status                          host, power and every runner
  doctor                          status plus checks; exit code = problems found
  logs [NAME|power] [LINES]       the runner's latest diagnostics, or the power watch log
  restart [NAME|all]              restart runners (CI stays paused on battery)
  add-runner REPO|ORG NAME LABELS [TOKEN|-] [ci|admin] [ephemeral]
                                  register a runner on OWNER/REPO, or on an ORG for all its
                                  repos (- mints a token with the stored one)
  remove-runner NAME [TOKEN|-]    unregister and delete
  set-token REPO|ORG              replace the GitHub token with $env:NEW_GITHUB_TOKEN (checked there; drops any App key)
  set-app REPO|ORG                switch to the GitHub App in $env:NEW_APP_ID and $env:NEW_APP_KEY_B64 (checked there; deletes the PAT)
  next-name CI_REPO|ORG ADMIN_REPO  the name a new PC would take
  battery pause|run               pause CI on battery (default) or run it anyway
  ci on|off                       CI runners take jobs (on) or finish their job and stop (off)
  cores N|all                     CI jobs may use N cores (CI_MAX_CORES, lower priority); all = no limit
  limit NAME cores=<N|default> ram=<MB|default>
                                  one runner's own limits (cores: CI_MAX_CORES, priority and affinity;
                                  ram: CI_MAX_RAM_MB, advisory); they win over `cores`; default = device-wide
  slots N [THREADS] | off         at most N CI jobs at once on this PC (Windows and WSL runners together), each on
                                  THREADS CPUs (default 8); adds win-N-ci-2.. runners; off = no cap
  slots-hold [-Name claude-vm] [-LeaseMin 10] [-TimeoutSec S]
                                  something outside the runners (a VM, a container) holds one PC job slot as a lease:
                                  waits for a free one (S 0 = forever); run it again within LeaseMin to keep it (heartbeat),
                                  else the power loop clears it. Exit 0 held (or slots off), 3 timed out, 2 bad usage
  slots-release [-Name claude-vm] release every slot that name holds (exit 0; idempotent)
  info                            specs, settings and runners as one line of JSON
  push-setup URL TOKENFILE        report this PC's info to the dashboard every 30 s (the power watch does it);
                                  TOKENFILE holds this PC's push token; URL like https://runners.example.com
  push-test                       send the info once and print the HTTP status
  self-update FILE                install a new winrunner
  uninstall                       remove runners, the service user and the files
  bootstrap ENVFILE               first install (win-runners.ps1 does this)
  power-watch                     the power loop (a scheduled task runs this)
  install-power-watch             (re)create that scheduled task
'@ | Write-Host
}

$r = $Rest
function Arg([int]$i, [string]$default = '') { if ($r.Count -gt $i) { $r[$i] } else { $default } }
function Need([int]$n, [string]$usage) { if ($r.Count -lt $n) { Die $usage } }

try {
    switch ($Command) {
        'status' { Show-Status }
        'doctor' { exit (Invoke-Doctor) }
        'info' { Show-Info }
        'logs' { Show-Logs (Arg 0 (Get-Content $HostFile -Raw).Trim()) ([int](Arg 1 '60')) }
        'restart' { Restart-Runners (Arg 0 'all') }
        'add-runner' { Need 3 'add-runner REPO NAME LABELS [TOKEN|-] [ci|admin] [ephemeral]'
            Add-Runner $r[0] $r[1] $r[2] (Arg 3 '-') (Arg 4 'ci') ((Arg 5) -eq 'ephemeral') }
        'remove-runner' { Need 1 'remove-runner NAME [TOKEN|-]'; Remove-Runner $r[0] (Arg 1 '-') }
        'set-token' { Need 1 'set-token REPO|ORG'; Set-NewToken $r[0] }
        'set-app' { Need 1 'set-app REPO|ORG'; Set-NewApp $r[0] }
        'next-name' { Need 2 'next-name CI_REPO ADMIN_REPO'; Get-NextName $r[0] $r[1] }
        'battery' { Need 1 'battery pause|run'; Set-BatteryMode $r[0] }
        'ci' { Need 1 'ci on|off'; Set-CiMode $r[0] }
        'cores' { Need 1 'cores N|all'; Set-Cores $r[0] }
        'limit' { Need 2 'limit NAME cores=<N|default> ram=<MB|default>'; Set-RunnerLimit $r[0] @($r[1..($r.Count - 1)]) }
        'slots' { Need 1 'slots N [THREADS] | off'; Set-Slots $r[0] (Arg 1) }
        'slots-hold' { exit (Invoke-SlotVmCommand 'hold' $r) }
        'slots-release' { exit (Invoke-SlotVmCommand 'release' $r) }
        'push-setup' { Need 2 'push-setup URL TOKENFILE'; Set-PushConfig $r[0] $r[1] }
        'push-test' { Invoke-PushTest }
        'self-update' { Need 1 'self-update FILE'; Update-Self $r[0] }
        'uninstall' { Invoke-Uninstall }
        'bootstrap' { Need 1 'bootstrap ENVFILE'; Invoke-Bootstrap $r[0] }
        'power-watch' { Invoke-PowerWatch }
        'install-power-watch' { Assert-Admin; Install-PowerWatch }
        { $_ -in 'help', '-h', '--help' } { Show-Usage }
        default { Show-Usage; exit 2 }
    }
} catch {
    [Console]::Error.WriteLine($_.Exception.Message)
    exit 1
}
