"""The GitHub App token helper: JWT claims, caching, refresh before expiry, PAT fallback.
The RSA key is generated here and thrown away; no real key is ever used."""

import importlib.util
import json
import pathlib
import urllib.error

import jwt

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

SRC = pathlib.Path(__file__).parent.parent / "dashboard/gh_app_token.py"
spec = importlib.util.spec_from_file_location("gh_app_token_mod", SRC)
G = importlib.util.module_from_spec(spec)
spec.loader.exec_module(G)

KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PEM = KEY.private_bytes(
    serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
).decode()
PUB = KEY.public_key()


class Clock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


class Resp:
    def __init__(self, body):
        self._b = json.dumps(body).encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._b


class FakeGitHub:
    """Records requests; mints token-1, token-2, ... and can be made to fail."""

    def __init__(self, installation_id=777):
        self.calls = []
        self.minted = 0
        self.installation_id = installation_id
        self.fail = None  # HTTP status to answer every call with

    def __call__(self, req, timeout=None):
        self.calls.append(
            (req.get_method(), req.full_url.replace("https://api.github.com", ""), req.get_header("Authorization"))
        )
        if self.fail:
            raise urllib.error.HTTPError(req.full_url, self.fail, "x", {}, None)
        if req.full_url.endswith("/installation"):
            return Resp({"id": self.installation_id})
        self.minted += 1
        return Resp({"token": f"token-{self.minted}", "expires_at": "2099-01-01T00:00:00Z"})


def source(gh, clock, **kw):
    kw.setdefault("owner", "example-org")
    return G.AppTokenSource("12345", PEM, opener=gh, clock=clock, **kw)


def test_jwt_claims_and_signature():
    clock = Clock()
    s = source(FakeGitHub(), clock)
    claims = jwt.decode(s.jwt(), PUB, algorithms=["RS256"], options={"verify_iat": False, "verify_exp": False})
    assert claims == {"iat": clock.t - 60, "exp": clock.t + 540, "iss": "12345"}
    assert claims["exp"] - claims["iat"] <= 600  # GitHub's limit


def test_mints_via_installation_lookup_with_the_jwt_as_bearer():
    gh, clock = FakeGitHub(), Clock()
    assert source(gh, clock).token() == "token-1"
    assert [(m, p) for m, p, _ in gh.calls] == [
        ("GET", "/orgs/example-org/installation"),
        ("POST", "/app/installations/777/access_tokens"),
    ]
    bearer = gh.calls[0][2].removeprefix("Bearer ")
    assert (
        jwt.decode(bearer, PUB, algorithms=["RS256"], options={"verify_exp": False, "verify_iat": False})["iss"]
        == "12345"
    )


def test_known_installation_id_skips_the_lookup():
    gh = FakeGitHub()
    source(gh, Clock(), installation_id="42").token()
    assert [p for _, p, _ in gh.calls] == ["/app/installations/42/access_tokens"]


def test_token_is_cached_and_refreshed_ten_minutes_before_expiry():
    gh, clock = FakeGitHub(), Clock()
    s = source(gh, clock)
    assert s.token() == "token-1"
    clock.t += 3600 - 601  # 10 minutes and 1 second left
    assert s.token() == "token-1" and gh.minted == 1
    clock.t += 2  # 9 minutes 59 seconds left
    assert s.token() == "token-2" and gh.minted == 2
    assert s.token() == "token-2" and gh.minted == 2
    # the installation is looked up once, not on every refresh
    assert sum(1 for _, p, _ in gh.calls if p.endswith("/installation")) == 1


def test_failed_refresh_keeps_the_token_until_it_really_expires():
    gh, clock = FakeGitHub(), Clock()
    s = source(gh, clock)
    s.token()
    gh.fail = 503
    clock.t += 3000  # inside the refresh window, still valid
    assert s.token() == "token-1"
    clock.t += 700  # expired
    with pytest.raises(G.AppTokenError, match="HTTP 503"):
        s.token()


def test_missing_installation_is_an_error():
    gh = FakeGitHub(installation_id="")
    with pytest.raises(G.AppTokenError, match="no installation"):
        source(gh, Clock()).token()


def test_from_env_needs_both_id_and_key(tmp_path):
    assert G.AppTokenSource.from_env({}) is None
    assert G.AppTokenSource.from_env({"GH_APP_ID": "1"}) is None
    idf, keyf = tmp_path / "id", tmp_path / "key"
    idf.write_text("99\n")
    keyf.write_text("")
    assert G.AppTokenSource.from_env({"GH_APP_ID_FILE": str(idf), "GH_APP_KEY_FILE": str(keyf)}) is None
    keyf.write_text(PEM.strip())  # a key file without a trailing newline still signs
    s = G.AppTokenSource.from_env(
        {"GH_APP_ID_FILE": str(idf), "GH_APP_KEY_FILE": str(keyf), "GH_APP_OWNER": "Org", "GH_APP_INSTALLATION_ID": "5"}
    )
    assert (s.app_id, s.owner, s.installation_id) == ("99", "Org", "5")
    assert s.jwt()


def app_env(tmp_path, **extra):
    idf, keyf = tmp_path / "id", tmp_path / "key"
    idf.write_text("99\n")
    keyf.write_text(PEM)
    return {"GH_APP_ID_FILE": str(idf), "GH_APP_KEY_FILE": str(keyf), **extra}


def test_owner_has_no_default_and_a_configured_app_without_an_org_is_an_error(tmp_path):
    with pytest.raises(G.AppTokenError, match="GH_APP_OWNER"):
        G.AppTokenSource.from_env(app_env(tmp_path))
    with pytest.raises(G.AppTokenError, match="GH_APP_OWNER"):
        G.TokenProvider.from_env(app_env(tmp_path), pat="p")


@pytest.mark.parametrize("var", ["GITRUNNER_ORG", "MACS_ORG", "WATCHDOG_ORG"])
def test_owner_falls_back_to_the_org_variables(tmp_path, var):
    assert G.AppTokenSource.from_env(app_env(tmp_path, **{var: "acme"})).owner == "acme"


def test_gh_app_owner_wins_over_the_org_variables(tmp_path):
    env = app_env(tmp_path, GH_APP_OWNER="a", GITRUNNER_ORG="b", MACS_ORG="c")
    assert G.AppTokenSource.from_env(env).owner == "a"


def test_compose_passes_the_org_to_the_dashboard_and_the_watchdog():
    text = (pathlib.Path(__file__).resolve().parent.parent / "compose.yaml").read_text()
    assert text.count('GH_APP_OWNER: "${GITRUNNER_ORG}"') == 2


def test_provider_without_app_returns_the_pat():
    p = G.TokenProvider.from_env({"GH_TOKEN": "pat-1"})
    assert p.source is None and p.token() == "pat-1" and p.configured()
    assert not G.TokenProvider.from_env({}).configured()


def test_provider_prefers_the_app_token():
    p = G.TokenProvider("pat-1", source(FakeGitHub(), Clock()))
    assert p.token() == "token-1"


def test_provider_falls_back_to_the_pat_when_minting_fails(capsys):
    gh = FakeGitHub()
    gh.fail = 401
    p = G.TokenProvider("pat-1", source(gh, Clock()))
    assert p.token() == "pat-1" and p.token() == "pat-1"
    err = capsys.readouterr().err
    assert err.count("using the personal access token") == 1  # warned once, not every call
    assert "pat-1" not in err and "BEGIN" not in err
    gh.fail = None
    assert p.token() == "token-1"  # recovers on its own


def test_provider_with_no_pat_raises_when_minting_fails():
    gh = FakeGitHub()
    gh.fail = 401
    with pytest.raises(G.AppTokenError):
        G.TokenProvider("", source(gh, Clock())).token()


def load_server(monkeypatch, **env):
    for k in ("GH_APP_ID", "GH_APP_PRIVATE_KEY", "GH_APP_ID_FILE", "GH_APP_KEY_FILE"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("GH_TOKEN", "pat-1")
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    s = importlib.util.spec_from_file_location("dashboard_server_app", SRC.parent / "server.py")
    mod = importlib.util.module_from_spec(s)
    s.loader.exec_module(mod)
    return mod


def test_dashboard_gh_calls_keep_the_pat_without_the_app(monkeypatch):
    mod = load_server(monkeypatch)
    assert mod.gh_env() is None  # inherit GH_TOKEN, exactly as before
    assert mod.gh_env({"A": "b"}) == {"A": "b"}


def test_dashboard_gh_calls_use_the_app_token_and_never_override_a_personal_one(monkeypatch):
    mod = load_server(monkeypatch, GH_APP_ID="1", GH_APP_PRIVATE_KEY=PEM)
    mod._TOKENS.source = source(FakeGitHub(), Clock())
    assert mod.gh_env()["GH_TOKEN"] == "token-1"
    seen = {}
    monkeypatch.setattr(
        mod.subprocess,
        "run",
        lambda cmd, **kw: seen.update(env=kw["env"]) or type("P", (), {"returncode": 0, "stdout": "", "stderr": ""})(),
    )
    mod.run(["gh", "api", "x"], 5)
    assert seen["env"]["GH_TOKEN"] == "token-1"
    mod.run(["gh", "api", "x"], 5, {"GH_TOKEN": "personal"})  # the example-user path brings its own
    assert seen["env"]["GH_TOKEN"] == "personal"
    mod.run(["bash", "x"], 5)
    assert seen["env"] is None
