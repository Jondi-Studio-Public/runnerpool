"""win/winrunner.ps1's GitHub credential, run in PowerShell 7 where it is installed (CI installs it): the
PKCS#1 / PKCS#8 key reader that stands in for ImportFromPem (absent from Windows PowerShell 5.1's .NET Framework),
the RS256 JWT, installation lookup, token cache and refresh, the PAT fallback, and set-app / set-token with a way
back. Invoke-RestMethod is a stand-in that answers the GitHub API; the key is a throwaway made by openssl."""

import base64
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from appkeys import OPENSSL, b64url_decode, make_keys, run, verify_jwt

ROOT = Path(__file__).resolve().parent.parent
WINRUNNER = ROOT / "win" / "winrunner.ps1"
PWSH = shutil.which("pwsh") or (
    os.environ.get("PWSH") if os.environ.get("PWSH") and Path(os.environ["PWSH"]).exists() else None
)
pytestmark = [
    pytest.mark.skipif(PWSH is None, reason="pwsh not installed"),
    pytest.mark.skipif(OPENSSL is None, reason="openssl not installed"),
]
NOW = 1_800_000_000
ISO = "2027-01-15T08:59:50Z"
EXPIRES = 1_800_003_590

FUNCS = [
    "Log",
    "Die",
    "Assert-Admin",
    "Api-Path",
    "Protect-File",
    "Write-Private",
    "Save-Token",
    "Get-Now",
    "Test-AppConfigured",
    "Get-CredentialKind",
    "Test-HasCredential",
    "ConvertTo-Base64Url",
    "Get-DerElement",
    "Remove-LeadingZeros",
    "ConvertTo-FixedBytes",
    "ConvertFrom-RsaPem",
    "Get-AppJwt",
    "Invoke-GhApp",
    "Assert-AppOrg",
    "Get-AppInstallationId",
    "Update-AppToken",
    "Initialize-AppToken",
    "Get-GhBearer",
    "Invoke-Gh",
    "Get-TokenExpiry",
    "Get-CredentialLine",
    "Get-CredentialFiles",
    "Save-App",
    "Clear-App",
    "Get-CredentialSnapshot",
    "Restore-Credentials",
    "Set-NewToken",
    "Set-NewApp",
]
VARS = [
    "AuthFile",
    "AppIdFile",
    "AppKeyFile",
    "AppInstallFile",
    "AppTokenFile",
    "AppExpiryFile",
    "OrgFile",
    "AppOrg",
    "AppRefreshMargin",
]

# The real functions and variable definitions, cut out of winrunner.ps1, plus a GitHub that answers from the files
# in $env:STUB_DIR and logs every call as one JSON line: the method, the path and the bearer it was sent.
PRELUDE = f"""
$ErrorActionPreference = 'Stop'
$HomeDir = $env:TEST_HOME
$Command = 'test'
$ast = [Management.Automation.Language.Parser]::ParseFile('{WINRUNNER}', [ref]$null, [ref]$null)
foreach ($a in $ast.FindAll({{ param($x) $x -is [Management.Automation.Language.AssignmentStatementAst] -and $x.Left.VariablePath -and $x.Left.VariablePath.UserPath -in '{"','".join(VARS)}' }}, $false)) {{
  Invoke-Expression $a.Extent.Text
}}
foreach ($n in '{"','".join(FUNCS)}') {{
  $fn = $ast.Find({{ param($x) $x -is [Management.Automation.Language.FunctionDefinitionAst] -and $x.Name -eq $n }}, $true)
  if (-not $fn) {{ throw "no function $n in winrunner.ps1" }}
  Invoke-Expression $fn.Extent.Text
}}
function Log([string]$m) {{ Write-Output "LOG $m" }}
function Assert-Admin {{ }}
function Invoke-RestMethod {{
  param($Method, $Uri, $Headers, $TimeoutSec, $Body)
  $path = $Uri -replace '^https://api.github.com/', ''
  $bearer = ($Headers.Authorization -replace '^Bearer ', '')
  ([ordered]@{{ method = "$Method"; path = $path; bearer = $bearer }} | ConvertTo-Json -Compress) | Add-Content -Path (Join-Path $env:STUB_DIR 'calls.log')
  $sd = $env:STUB_DIR
  if ($Method -eq 'GET' -and $path -eq 'orgs/example-org/installation') {{
    if (Test-Path "$sd/no_installation") {{ throw 'HTTP 404' }}
    return [pscustomobject]@{{ id = 777 }}
  }}
  if ($Method -eq 'POST' -and $path -match '^app/installations/(\\d+)/access_tokens$') {{
    if ($Matches[1] -ne '777' -or (Test-Path "$sd/no_token")) {{ throw 'HTTP 404' }}
    $n = 1; if (Test-Path "$sd/minted") {{ $n = [int](Get-Content "$sd/minted") + 1 }}
    Set-Content -Path "$sd/minted" -Value $n
    $exp = if ($env:EXPIRES_AS_STRING) {{ '{ISO}' }} else {{ [datetime]::Parse('{ISO}', [Globalization.CultureInfo]::InvariantCulture, [Globalization.DateTimeStyles]::AdjustToUniversal) }}
    return [pscustomobject]@{{ token = "ghs_FAKE$n"; expires_at = $exp }}
  }}
  if ($Method -eq 'GET' -and $path -eq 'orgs/example-org/actions/runners?per_page=1') {{
    if (Test-Path "$sd/list_fails") {{ throw 'HTTP 403' }}
    return [pscustomobject]@{{ total_count = 0; runners = @() }}
  }}
  throw "unexpected call $Method $path"
}}
"""


@pytest.fixture(scope="session")
def keys(tmp_path_factory):
    pkcs1, pkcs8, pub = make_keys(tmp_path_factory.mktemp("winkeys"))
    return {"pkcs1": pkcs1, "pkcs8": pkcs8, "pub": pub}


class Box:
    def __init__(self, tmp_path, keys):
        self.t = tmp_path
        self.keys = keys
        self.home = tmp_path / "home"
        self.home.mkdir()
        self.env = {
            **os.environ,
            "DOTNET_SYSTEM_GLOBALIZATION_INVARIANT": "1",
            "TEST_HOME": str(self.home),
            "STUB_DIR": str(tmp_path),
            "GITRUNNER_NOW": str(NOW),
        }

    def ps(self, script, **env):
        return subprocess.run(
            [PWSH, "-NoProfile", "-NonInteractive", "-Command", PRELUDE + script],
            env={**self.env, **env},
            capture_output=True,
            text=True,
            timeout=120,
            stdin=subprocess.DEVNULL,
        )

    def install_app(self, app_id="12345", key="pkcs1"):
        (self.home / "github-app-id").write_text(app_id)
        shutil.copy(self.keys[key], self.home / "github-app.pem")

    def install_pat(self, token="pat123"):
        (self.home / "github-token").write_text(token)

    def flag(self, name):
        (self.t / name).write_text("")

    def calls(self):
        f = self.t / "calls.log"
        return [json.loads(x) for x in f.read_text().splitlines()] if f.exists() else []

    def minted(self):
        f = self.t / "minted"
        return int(f.read_text()) if f.exists() else 0

    def files(self):
        return {p.name: p.read_bytes() for p in self.home.iterdir()}

    def b64(self, key="pkcs1"):
        return run("base64", "-A", "-in", str(self.keys[key])).stdout.decode()


@pytest.fixture
def box(tmp_path, keys):
    return Box(tmp_path, keys)


@pytest.mark.parametrize("which", ["pkcs1", "pkcs8"])
def test_pem_reader_agrees_with_openssl_on_every_component(box, which):
    r = box.ps(
        f"$p = ConvertFrom-RsaPem (Get-Content '{box.keys[which]}' -Raw); "
        "'MOD ' + [BitConverter]::ToString($p.Modulus).Replace('-','') + \"`nEXP \" + [BitConverter]::ToString($p.Exponent).Replace('-','')"
        " + \"`nLEN \" + $p.D.Length + ' ' + $p.P.Length + ' ' + $p.Q.Length + ' ' + $p.DP.Length + ' ' + $p.DQ.Length + ' ' + $p.InverseQ.Length"
    )
    assert r.returncode == 0, r.stderr
    out = dict(line.split(" ", 1) for line in r.stdout.strip().splitlines())
    want = run("rsa", "-in", str(box.keys[which]), "-noout", "-modulus").stdout.decode().strip().split("=")[1]
    assert out["MOD"] == want
    assert out["EXP"] == "010001"
    assert out["LEN"] == "256 128 128 128 128 128"


def test_fixed_bytes_strip_the_der_sign_byte_and_pad_short_values(box):
    r = box.ps(
        "'A ' + [BitConverter]::ToString((ConvertTo-FixedBytes ([byte[]](0,0xFF,1)) 2)) + \"`n\" + "
        "'B ' + [BitConverter]::ToString((ConvertTo-FixedBytes ([byte[]](5)) 4)) + \"`n\" + "
        "'C ' + [BitConverter]::ToString((ConvertTo-FixedBytes ([byte[]](0,0,0)) 2))"
    )
    assert r.returncode == 0, r.stderr
    assert r.stdout.split("\n")[:3] == ["A FF-01", "B 00-00-00-05", "C 00-00"]


def test_garbage_pem_is_refused(box):
    for pem in ("hello", "-----BEGIN PUBLIC KEY-----\nAAAA\n-----END PUBLIC KEY-----"):
        r = box.ps(f"ConvertFrom-RsaPem @'\n{pem}\n'@")
        assert r.returncode != 0


@pytest.mark.parametrize("which", ["pkcs1", "pkcs8"])
def test_jwt_has_the_claims_verifies_and_matches_openssls_signature(box, which):
    box.install_app(key=which)
    r = box.ps("Get-AppJwt")
    assert r.returncode == 0, r.stderr
    jwt = r.stdout.strip()
    head, body, sig = jwt.split(".")
    assert json.loads(b64url_decode(head)) == {"alg": "RS256", "typ": "JWT"}
    assert json.loads(b64url_decode(body)) == {"iat": NOW - 60, "exp": NOW + 540, "iss": 12345}
    assert "=" not in jwt and "+" not in jwt and "/" not in jwt
    assert verify_jwt(jwt, box.keys["pub"], box.t)
    # RSASSA-PKCS1-v1_5 is deterministic: the same key and message give the very same bytes as `openssl dgst -sign`
    want = run("dgst", "-sha256", "-sign", str(box.keys[which]), stdin=f"{head}.{body}".encode()).stdout
    assert b64url_decode(sig) == want


def test_a_client_id_is_a_string_issuer(box):
    box.install_app(app_id="Iv1.abc123")
    jwt = box.ps("Get-AppJwt").stdout.strip()
    assert json.loads(b64url_decode(jwt.split(".")[1]))["iss"] == "Iv1.abc123"


@pytest.mark.parametrize("as_string", ["", "1"])  # PowerShell 7 parses the date, Windows PowerShell 5.1 leaves a string
def test_installation_looked_up_once_token_minted_once_and_reused(box, as_string):
    box.install_app()
    r = box.ps(
        "(Get-GhBearer); (Get-GhBearer); (Invoke-Gh 'GET' 'orgs/example-org/actions/runners?per_page=1') | Out-Null",
        EXPIRES_AS_STRING=as_string,
    )
    assert r.returncode == 0, r.stderr
    assert r.stdout.split() == ["ghs_FAKE1", "ghs_FAKE1"]
    calls = box.calls()
    assert [(c["method"], c["path"]) for c in calls] == [
        ("GET", "orgs/example-org/installation"),
        ("POST", "app/installations/777/access_tokens"),
        ("GET", "orgs/example-org/actions/runners?per_page=1"),
    ]
    assert verify_jwt(calls[0]["bearer"], box.keys["pub"], box.t) and verify_jwt(
        calls[1]["bearer"], box.keys["pub"], box.t
    )
    assert calls[2]["bearer"] == "ghs_FAKE1"
    assert (box.home / "github-app-installation").read_text() == "777"
    assert (box.home / "github-app-token.expires").read_text() == f"{EXPIRES} {ISO}"


def test_token_refreshes_ten_minutes_before_expiry(box):
    box.install_app()
    assert box.ps("Get-GhBearer").returncode == 0 and box.minted() == 1
    assert box.ps("Get-GhBearer", GITRUNNER_NOW=str(EXPIRES - 601)).stdout.strip() == "ghs_FAKE1" and box.minted() == 1
    assert box.ps("Get-GhBearer", GITRUNNER_NOW=str(EXPIRES - 599)).stdout.strip() == "ghs_FAKE2" and box.minted() == 2
    assert sum(1 for c in box.calls() if c["path"] == "orgs/example-org/installation") == 1


def test_a_stale_installation_id_is_looked_up_again(box):
    box.install_app()
    (box.home / "github-app-installation").write_text("111")
    r = box.ps("Get-GhBearer")
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "ghs_FAKE1"
    assert (box.home / "github-app-installation").read_text() == "777"


def test_a_failed_mint_throws_and_leaves_no_cache(box):
    box.install_app()
    box.flag("no_token")
    assert box.ps("Get-GhBearer").returncode != 0
    assert not (box.home / "github-app-token").exists()


def test_without_an_app_key_the_stored_pat_is_used_as_before(box):
    box.install_pat("pat123")
    r = box.ps("Get-CredentialKind; Invoke-Gh 'GET' 'orgs/example-org/actions/runners?per_page=1' | Out-Null")
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "pat"
    assert [(c["path"], c["bearer"]) for c in box.calls()] == [
        ("orgs/example-org/actions/runners?per_page=1", "pat123")
    ]


def test_with_both_the_app_wins_and_with_neither_it_throws(box):
    r = box.ps("Get-GhBearer")
    assert r.returncode != 0 and "no GitHub credential" in r.stderr
    box.install_pat("pat123")
    box.install_app()
    r = box.ps("Get-CredentialKind; Get-GhBearer")
    assert r.stdout.split() == ["app", "ghs_FAKE1"]


def test_status_line_names_the_credential(box):
    line = lambda: box.ps("Get-CredentialLine ''").stdout.strip()  # noqa: E731
    assert line().startswith("none")
    box.install_pat()
    assert line() == "GitHub token stored"
    box.install_app()
    assert line() == f"GitHub App 12345, token valid until {ISO}"
    for f in ("github-app-token", "github-app-token.expires"):
        (box.home / f).unlink()
    box.flag("no_token")
    assert "no token can be minted" in line()


def test_set_app_stores_checks_and_replaces_the_pat(box):
    box.install_pat("pat123")
    r = box.ps("Set-NewApp 'example-org'", NEW_APP_ID="12345", NEW_APP_KEY_B64=box.b64())
    assert r.returncode == 0, r.stdout + r.stderr
    assert "now mints GitHub App tokens (App 12345" in r.stdout
    assert not (box.home / "github-token").exists()
    assert (box.home / "github-app-id").read_text() == "12345"
    assert (box.home / "github-app.pem").read_text().strip() == box.keys["pkcs1"].read_text().strip()
    assert "BEGIN" not in r.stdout + r.stderr and "ghs_FAKE" not in r.stdout + r.stderr
    assert box.minted() == 1


def test_set_app_failure_brings_the_old_pat_back(box):
    box.install_pat("pat123")
    box.flag("list_fails")
    r = box.ps("Set-NewApp 'example-org'", NEW_APP_ID="12345", NEW_APP_KEY_B64=box.b64())
    assert r.returncode != 0 and "kept the old credential" in r.stderr
    assert box.files() == {"github-token": b"pat123"}


def test_set_app_failure_brings_the_old_app_back(box):
    box.install_app(app_id="111", key="pkcs8")
    assert box.ps("Get-GhBearer").returncode == 0
    before = box.files()
    box.flag("no_token")
    r = box.ps("Set-NewApp 'example-org'", NEW_APP_ID="222", NEW_APP_KEY_B64=box.b64("pkcs1"))
    assert r.returncode != 0
    assert box.files() == before


def test_set_app_rejects_a_key_that_is_not_pem(box):
    box.install_pat("pat123")
    r = box.ps("Set-NewApp 'example-org'", NEW_APP_ID="1", NEW_APP_KEY_B64=base64.b64encode(b"not a key").decode())
    assert r.returncode != 0
    assert box.files() == {"github-token": b"pat123"}
    assert box.ps("Set-NewApp 'example-org'").returncode != 0


def test_set_token_switches_an_app_device_back_to_a_pat_and_restores_on_failure(box):
    box.install_app()
    assert box.ps("Get-GhBearer").returncode == 0
    r = box.ps("Set-NewToken 'example-org'", NEW_GITHUB_TOKEN="newpat")
    assert r.returncode == 0, r.stdout + r.stderr
    assert box.files() == {"github-token": b"newpat"}
    assert box.calls()[-1]["bearer"] == "newpat"  # verified with the PAT, not the App

    (box.t / "second").mkdir()
    b = Box(box.t / "second", box.keys)
    b.install_app()
    assert b.ps("Get-GhBearer").returncode == 0
    before = b.files()
    b.flag("list_fails")
    r = b.ps("Set-NewToken 'example-org'", NEW_GITHUB_TOKEN="badpat")
    assert r.returncode != 0 and "kept the old credential" in r.stderr
    assert b.files() == before


def test_no_org_anywhere_fails_naming_gitrunner_org_and_asks_github_nothing(box):
    box.install_app()
    env = {k: v for k, v in box.env.items() if k not in ("GITRUNNER_ORG", "GITRUNNER_APP_ORG")}
    box.env = env
    r = box.ps("Get-AppInstallationId")
    assert r.returncode != 0 and "GITRUNNER_ORG" in r.stderr
    assert box.calls() == []


def test_the_org_the_installer_saved_is_used_when_the_environment_has_none(box):
    box.install_app()
    box.env = {k: v for k, v in box.env.items() if k not in ("GITRUNNER_ORG", "GITRUNNER_APP_ORG")}
    (box.home / "org").write_text("example-org\n")
    r = box.ps("Get-AppInstallationId")
    assert r.returncode == 0, r.stderr
    assert box.calls()[-1]["path"] == "orgs/example-org/installation"
