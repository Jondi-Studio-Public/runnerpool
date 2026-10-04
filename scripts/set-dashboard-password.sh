#!/usr/bin/env bash
# Choose the dashboard's password. Run this yourself in Git Bash (Windows OpenSSH first on PATH):
# it asks for the password at a hidden prompt, hashes it here (salted scrypt), writes only the
# hash to the server and restarts the dashboard. The password never leaves this terminal.
# 10 or more characters. Every signed-in device has to sign in again afterwards.
set -euo pipefail
cd "$(dirname "$0")/.."
SERVER="${DEPLOY_HOST:?set DEPLOY_HOST, the ssh target of the dashboard server, e.g. root@192.0.2.10}"
DIR=/root/macs-dashboard/secrets
PY=$(command -v python3 || command -v python || command -v py)

HASH=$("$PY" - <<'PYEOF'
import base64, getpass, hashlib, os, sys
pw = getpass.getpass("New dashboard password (10+ characters): ")
if pw != getpass.getpass("Again: "):
    sys.exit("passwords differ")
if len(pw) < 10:
    sys.exit("too short")
salt = os.urandom(16)
n, r, p = 2 ** 15, 8, 1
h = hashlib.scrypt(pw.encode(), salt=salt, n=n, r=r, p=p, maxmem=128 * 1024 * 1024, dklen=32)
print("scrypt$%d$%d$%d$%s$%s" % (n, r, p, base64.b64encode(salt).decode(), base64.b64encode(h).decode()))
PYEOF
)

printf '%s' "$HASH" | ssh -o BatchMode=yes "$SERVER" \
  "umask 077; cat > $DIR/dashboard_token.new && chown 1000:1000 $DIR/dashboard_token.new && chmod 400 $DIR/dashboard_token.new && mv -f $DIR/dashboard_token.new $DIR/dashboard_token"

docker --context "${REMOTE_CONTEXT:?set REMOTE_CONTEXT}" restart macs-dashboard >/dev/null
echo "password set; sign in at ${DASHBOARD_URL:-your dashboard URL}"
