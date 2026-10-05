#!/usr/bin/env bash
# Create the dashboard's secret files on the home server without any value being shown.
#   gh_token         copied from 1Password (Example-Vault/Mac Dashboard GitHub) through agent-run
#   gh_token_personal  copied from 1Password (Example-Vault/Mac Dashboard GitHub personal) when that item
#                    exists, else left empty (the CI panel then can't read example-user' private repos)
#   gh_app_id, gh_app_key  the CI GitHub App's id and private key, copied from 1Password (Example-Vault/CI GitHub
#                    App, fields app_id and private_key) when that item exists, else empty files. With
#                    both set, the dashboard and watchdog mint 1-hour installation tokens and use them
#                    instead of gh_token (docs/dashboard-deploy.md); gh_token_personal stays a PAT.
#   push_tokens      the PCs' health-push tokens (lines `win-1=<token>`), copied from 1Password (Example-Vault/Mac
#                    Dashboard PC push tokens, field token or credential) when that item exists, else
#                    an empty file (push off). Only with --push, or when the file is still missing.
#   webhook_secret   the CI GitHub App's webhook secret, copied from 1Password (Example-Vault/CI GitHub App,
#                    field webhook_secret) when that field exists, else an empty file (the webhook receiver
#                    then refuses to start; docs/webhook.md). Only with --webhook, or when the file is still empty.
#   dashboard_token  generated on the server; read it there once to sign in:
#                    ssh "$DEPLOY_HOST" cat /root/macs-dashboard/secrets/dashboard_token
# Run from Git Bash with Windows OpenSSH first on PATH. Existing files are kept, unless:
#   --push    re-copy push_tokens from 1Password (after adding a PC or changing a token); restart after.
#   --webhook re-copy webhook_secret from 1Password (after rotating it); restart the webhook container after.
#   --gh      re-copy both GitHub tokens and the GitHub App id and key from 1Password (after a token is edited or replaced);
#             the dashboard password is untouched. Restart the container (or deploy) after.
#   --rotate  replace everything, the dashboard password included.
set -euo pipefail
cd "$(dirname "$0")/.."
SERVER="${DEPLOY_HOST:?set DEPLOY_HOST, the ssh target of the dashboard server, e.g. root@192.0.2.10}"
DIR=/root/macs-dashboard/secrets
ROTATE=0; GH=0; PUSH=0; WEBHOOK=0
case "${1:-}" in
  --rotate) ROTATE=1; GH=1 ;;
  --gh) GH=1 ;;
  --push) PUSH=1 ;;
  --webhook) WEBHOOK=1 ;;
  "") ;;
  *) echo "usage: scripts/provision-secrets.sh [--gh|--push|--webhook|--rotate]" >&2; exit 2 ;;
esac
remote() { ssh -o BatchMode=yes "$SERVER" "$1"; }

remote "mkdir -p $DIR && chmod 700 $DIR"

if [ "$ROTATE" = 1 ] || ! remote "test -s $DIR/dashboard_token"; then
  remote "umask 077; head -c 24 /dev/urandom | base64 | tr '+/' '-_' | tr -d '=\n' > $DIR/dashboard_token"
  echo "dashboard_token: generated"
fi

# copy_gh ENV_FILE NAME: pipe the 1Password value named in ENV_FILE to $DIR/NAME, never printing it.
# The item's field may be called `token` (as ENV_FILE says) or `credential`: the first one that
# exists and is non-empty wins. A blank value never replaces a working token: it is written aside
# and moved into place only when non-empty.
copy_gh() {
  local env_file=$1 name=$2 field tmp
  tmp=$(mktemp)
  for field in token credential; do
    sed "s#/token\$#/$field#" "$env_file" > "$tmp"
    # agent-run puts GH_TOKEN in this child's environment only; it is piped, never printed.
    if agent-run --env-file "$tmp" -- bash -c \
        "test -n \"\$GH_TOKEN\" && printf '%s' \"\$GH_TOKEN\" | ssh -o BatchMode=yes $SERVER 'umask 077; cat > $DIR/$name.new && mv $DIR/$name.new $DIR/$name'" 2>/dev/null; then
      rm -f "$tmp"; echo "$name: copied from 1Password (field $field)"; return 0
    fi
  done
  rm -f "$tmp"; remote "rm -f $DIR/$name.new"
  return 1
}

if [ "$GH" = 1 ] || ! remote "test -s $DIR/gh_token"; then
  copy_gh dashboard/secrets.env.op gh_token ||
    { echo "ERROR: 1Password item Dev/Mac Dashboard GitHub has no value in a token or credential field" >&2; exit 1; }
fi

if [ "$GH" = 1 ] || ! remote "test -s $DIR/gh_token_personal"; then
  if ! copy_gh dashboard/secrets-personal.env.op gh_token_personal; then
    remote "[ -e $DIR/gh_token_personal ] || { umask 077; : > $DIR/gh_token_personal; }"
    echo "gh_token_personal: no value in 1Password item 'Dev/Mac Dashboard GitHub personal' yet, left as it was"
  fi
fi

# copy_app: the App's id and key from one 1Password item, written aside and moved into place only when
# both are non-empty, so a half-filled item never replaces a working pair. Never printed.
copy_app() {
  agent-run --env-file dashboard/github-app.env.op -- bash -c \
    "test -n \"\$GH_APP_ID\" && test -n \"\$GH_APP_PRIVATE_KEY\" &&
     printf '%s' \"\$GH_APP_ID\" | ssh -o BatchMode=yes $SERVER 'umask 077; cat > $DIR/gh_app_id.new' &&
     printf '%s\n' \"\$GH_APP_PRIVATE_KEY\" | ssh -o BatchMode=yes $SERVER 'umask 077; cat > $DIR/gh_app_key.new' &&
     ssh -o BatchMode=yes $SERVER 'mv $DIR/gh_app_id.new $DIR/gh_app_id && mv $DIR/gh_app_key.new $DIR/gh_app_key'" 2>/dev/null
}

if [ "$GH" = 1 ] || ! remote "test -s $DIR/gh_app_key"; then
  if copy_app; then echo "gh_app_id, gh_app_key: copied from 1Password (Example-Vault/CI GitHub App)"; else
    remote "rm -f $DIR/gh_app_id.new $DIR/gh_app_key.new; for n in gh_app_id gh_app_key; do [ -e $DIR/\$n ] || { umask 077; : > $DIR/\$n; }; done"
    echo "gh_app_id, gh_app_key: no complete 'Dev/CI GitHub App' item (fields app_id, private_key) yet, left as it was: the PAT is used"
  fi
fi

if [ "$PUSH" = 1 ] || ! remote "test -e $DIR/push_tokens"; then
  if ! copy_gh dashboard/push-tokens.env.op push_tokens; then
    remote "[ -e $DIR/push_tokens ] || { umask 077; : > $DIR/push_tokens; }"
    echo "push_tokens: no value in 1Password item 'Dev/Mac Dashboard PC push tokens' yet, left as it was"
  fi
fi

# copy_webhook: the webhook secret, piped from 1Password, never printed; moved into place only when non-empty.
copy_webhook() {
  agent-run --env-file dashboard/webhook-secret.env.op -- bash -c \
    "test -n \"\$WEBHOOK_SECRET\" && printf '%s' \"\$WEBHOOK_SECRET\" | ssh -o BatchMode=yes $SERVER 'umask 077; cat > $DIR/webhook_secret.new && mv $DIR/webhook_secret.new $DIR/webhook_secret'" 2>/dev/null
}

if [ "$WEBHOOK" = 1 ] || ! remote "test -s $DIR/webhook_secret"; then
  if copy_webhook; then echo "webhook_secret: copied from 1Password (Example-Vault/CI GitHub App)"; else
    remote "rm -f $DIR/webhook_secret.new; [ -e $DIR/webhook_secret ] || { umask 077; : > $DIR/webhook_secret; }"
    echo "webhook_secret: no value in 1Password item 'Example-Vault/CI GitHub App' (field webhook_secret) yet, left as it was: the webhook receiver will not start"
  fi
fi

SECRET_FILES="gh_token dashboard_token gh_token_personal push_tokens gh_app_id gh_app_key webhook_secret"
remote "cd $DIR && chown 1000:1000 $SECRET_FILES && chmod 400 $SECRET_FILES"
for f in gh_token dashboard_token; do
  remote "test -s $DIR/$f" || { echo "ERROR: $DIR/$f on the server is empty" >&2; exit 1; }
done
echo "secrets in place"
