#!/bin/sh
# Container entrypoint: secrets arrive as files; the GitHub token is exported for the watchdog.
if [ -r /run/secrets/gh_token ]; then
  GH_TOKEN=$(cat /run/secrets/gh_token)
  export GH_TOKEN
fi
# GitHub App credentials (Dev/CI GitHub App), when provisioned: the server then mints installation
# tokens (dashboard/gh_app_token.py) and uses them instead of GH_TOKEN, which stays as the fallback.
if [ -s /run/secrets/gh_app_id ] && [ -s /run/secrets/gh_app_key ]; then
  export GH_APP_ID_FILE=/run/secrets/gh_app_id GH_APP_KEY_FILE=/run/secrets/gh_app_key
fi
export WATCHDOG_NTFY_TOPIC_FILE=/run/secrets/ntfy_topic WATCHDOG_NTFY_TOKEN_FILE=/run/secrets/ntfy_token
exec python3 /app/watchdog/watchdog.py
