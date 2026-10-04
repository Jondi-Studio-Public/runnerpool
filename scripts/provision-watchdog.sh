#!/usr/bin/env bash
# Create the CI watchdog's two secret files on the home server without any value being shown:
#   ntfy_topic  the ntfy topic alerts are posted to, from 1Password (Example-Vault/CI Watchdog ntfy, field topic)
#   ntfy_token  an access token for a protected ntfy server, same item, field token (optional: an
#               empty file means no Authorization header is sent)
# The watchdog reuses the dashboard's gh_token and GitHub App id and key (scripts/provision-secrets.sh), so nothing else is needed.
# Run from Git Bash with Windows OpenSSH first on PATH, then scripts/deploy.sh. Existing files are
# replaced; a field that is empty or missing in 1Password leaves its server file as it was (or empty).
set -euo pipefail
cd "$(dirname "$0")/.."
SERVER="${DEPLOY_HOST:?set DEPLOY_HOST, the ssh target of the dashboard server, e.g. root@192.0.2.10}"
DIR=/root/macs-dashboard/secrets
remote() { ssh -o BatchMode=yes "$SERVER" "$1"; }

remote "mkdir -p $DIR && chmod 700 $DIR"

# copy ENV_FILE NAME: pipe the 1Password value to $DIR/NAME, never printing it. Written aside, moved
# into place only when non-empty.
copy() {
  local env_file=$1 name=$2
  agent-run --env-file "$env_file" -- bash -c \
    "test -n \"\$NTFY_VALUE\" && printf '%s' \"\$NTFY_VALUE\" | ssh -o BatchMode=yes $SERVER 'umask 077; cat > $DIR/$name.new && mv $DIR/$name.new $DIR/$name'" 2>/dev/null
}

if copy dashboard/ntfy-topic.env.op ntfy_topic; then echo "ntfy_topic: copied from 1Password"; else
  remote "rm -f $DIR/ntfy_topic.new; [ -e $DIR/ntfy_topic ] || { umask 077; : > $DIR/ntfy_topic; }"
  echo "ntfy_topic: no value in 1Password item 'Dev/CI Watchdog ntfy' (field topic) yet, left as it was: alerts only reach the log until it is set"
fi
if copy dashboard/ntfy-token.env.op ntfy_token; then echo "ntfy_token: copied from 1Password"; else
  remote "rm -f $DIR/ntfy_token.new; [ -e $DIR/ntfy_token ] || { umask 077; : > $DIR/ntfy_token; }"
  echo "ntfy_token: none in 1Password, left as it was (fine for an open ntfy server)"
fi
remote "chown 1000:1000 $DIR/ntfy_topic $DIR/ntfy_token && chmod 400 $DIR/ntfy_topic $DIR/ntfy_token"
echo "watchdog secrets in place: scripts/deploy.sh to apply"
