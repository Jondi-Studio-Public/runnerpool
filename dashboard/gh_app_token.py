"""GitHub App installation tokens for the dashboard and the CI watchdog (one shared helper).

The App's id and private key come from secret files (or the environment); a 1-hour installation
token is minted with a short-lived RS256 JWT, cached, and replaced about ten minutes before it
expires. Without App credentials the static personal access token (GH_TOKEN) is used unchanged,
and so it is if minting fails while a PAT exists. Only paths and status codes are ever logged.

  GH_APP_ID / GH_APP_ID_FILE               the App id (the `app_id` field of Dev/CI GitHub App)
  GH_APP_PRIVATE_KEY / GH_APP_KEY_FILE     the .pem (the `private_key` field)
  GH_APP_INSTALLATION_ID                   optional; else looked up with GET /orgs/{owner}/installation
  GH_APP_OWNER                             the org that installed the App (default: GITRUNNER_ORG, else MACS_ORG, else WATCHDOG_ORG; none of them set is an error)

Needs PyJWT with the crypto extra (dashboard/requirements.txt).
"""

import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

API = "https://api.github.com"
REFRESH_BEFORE = 600  # seconds before expiry at which a new token is minted
JWT_LIFETIME = 540  # GitHub allows 10 minutes at most
CLOCK_SKEW = 60  # iat is backdated this far, as GitHub recommends


def log(msg):
    print(msg, file=sys.stderr, flush=True)


def read_secret(env_name, file_env_name, env=None):
    """A value from the environment, else from the file the other variable names; '' if neither."""
    e = os.environ if env is None else env
    v = e.get(env_name, "").strip()
    if v:
        return v
    path = e.get(file_env_name, "")
    if path:
        try:
            return Path(path).read_text(encoding="utf-8").strip()
        except OSError:
            return ""
    return ""


OWNER_VARS = ("GH_APP_OWNER", "GITRUNNER_ORG", "MACS_ORG", "WATCHDOG_ORG")  # first one set names the org


class AppTokenError(Exception):
    pass


class AppTokenSource:
    """Mints and caches installation tokens. token() is safe to call from several threads."""

    def __init__(self, app_id, private_key, owner="", installation_id="", base=API, opener=None, clock=time.time):
        self.app_id = str(app_id)
        self._key = private_key
        self.owner = owner
        self.installation_id = str(installation_id or "")
        self.base = base
        self._open = opener or urllib.request.urlopen
        self._now = clock
        self._lock = threading.Lock()
        self._token = ""
        self._expires = 0.0

    def jwt(self):
        import jwt  # PyJWT; imported here so a PAT-only deployment never needs it

        now = int(self._now())
        claims = {"iat": now - CLOCK_SKEW, "exp": now + JWT_LIFETIME, "iss": self.app_id}
        return jwt.encode(claims, self._key, algorithm="RS256")

    def _call(self, method, path, bearer):
        req = urllib.request.Request(self.base + path, method=method, data=b"" if method == "POST" else None)
        req.add_header("Authorization", "Bearer " + bearer)
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("X-GitHub-Api-Version", "2022-11-28")
        req.add_header("User-Agent", "git-runner-gh-app-token")
        try:
            with self._open(req, timeout=30) as r:
                return json.loads(r.read() or b"null")
        except urllib.error.HTTPError as e:
            raise AppTokenError(f"{method} {path}: HTTP {e.code}") from None
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise AppTokenError(f"{method} {path}: {type(e).__name__}") from None

    def _mint(self):
        app_jwt = self.jwt()
        if not self.installation_id:
            data = self._call("GET", f"/orgs/{self.owner}/installation", app_jwt)
            self.installation_id = str((data or {}).get("id", ""))
            if not self.installation_id:
                raise AppTokenError(f"no installation of the App found for {self.owner}")
        data = self._call("POST", f"/app/installations/{self.installation_id}/access_tokens", app_jwt)
        token = (data or {}).get("token", "")
        if not token:
            raise AppTokenError("access_tokens returned no token")
        # expires_at is an ISO time; the lifetime is an hour, so trust that over parsing it.
        self._token, self._expires = token, self._now() + 3600

    def token(self, min_ttl=0):
        """The cached token, minted afresh when it has less than REFRESH_BEFORE (or min_ttl) s left."""
        with self._lock:
            if not self._token or self._now() >= self._expires - max(REFRESH_BEFORE, min_ttl):
                try:
                    self._mint()
                except AppTokenError:
                    if self._token and self._now() < self._expires:
                        log("GitHub App token refresh failed, keeping the current one")
                    else:
                        raise
            return self._token

    @classmethod
    def from_env(cls, env=None, **kw):
        """A source when the App id and key are provisioned, else None."""
        e = os.environ if env is None else env
        app_id = read_secret("GH_APP_ID", "GH_APP_ID_FILE", e)
        key = read_secret("GH_APP_PRIVATE_KEY", "GH_APP_KEY_FILE", e)
        if not (app_id and key):
            return None
        owner = next((e[k] for k in OWNER_VARS if e.get(k)), "")
        if not owner:
            raise AppTokenError(
                "the GitHub App is configured but no org is: set GH_APP_OWNER (compose passes it from GITRUNNER_ORG)"
            )
        return cls(
            app_id,
            key if key.endswith("\n") else key + "\n",
            owner=owner,
            installation_id=e.get("GH_APP_INSTALLATION_ID", ""),
            **kw,
        )


class TokenProvider:
    """token(): the App's installation token when configured, else the PAT (also if minting fails)."""

    def __init__(self, pat="", source=None):
        self.pat = pat
        self.source = source
        self._warned = False

    @classmethod
    def from_env(cls, env=None, pat=None, **kw):
        e = os.environ if env is None else env
        if pat is None:
            pat = read_secret("GH_TOKEN", "WATCHDOG_GH_TOKEN_FILE", e)
        try:
            source = AppTokenSource.from_env(e, **kw)
        except AppTokenError as err:  # a missing org must not crash the dashboard or watchdog at import
            print(
                f"GitHub App ignored, using the PAT only: {err} (set GH_APP_OWNER or GITRUNNER_ORG)",
                file=sys.stderr,
                flush=True,
            )
            source = None
        return cls(pat, source)

    def configured(self):
        return bool(self.pat or self.source)

    def token(self, min_ttl=0):
        if self.source:
            try:
                tok = self.source.token(min_ttl)
                self._warned = False
                return tok
            except AppTokenError as e:
                if not self.pat:
                    raise
                if not self._warned:
                    log(f"GitHub App token unavailable ({e}), using the personal access token")
                    self._warned = True
        return self.pat
