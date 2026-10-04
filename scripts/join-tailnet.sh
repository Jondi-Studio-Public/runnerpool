#!/usr/bin/env bash
# Join the dashboard server ($DEPLOY_HOST) to the tailnet so the dashboard container can reach the
# Macs over Tailscale SSH. The auth key comes from 1Password (Example-Vault/Tailscale home server) through
# agent-run and is piped to the server, never shown. Run from Git Bash, Windows OpenSSH first on PATH.
set -euo pipefail
cd "$(dirname "$0")/.."
SERVER="${DEPLOY_HOST:?set DEPLOY_HOST, the ssh target of the dashboard server, e.g. root@192.0.2.10}"

ssh -o BatchMode=yes "$SERVER" 'command -v tailscale >/dev/null' || {
  echo "installing tailscale on the server"
  ssh -o BatchMode=yes "$SERVER" 'curl -fsSL https://tailscale.com/install.sh | sh'
}

if ssh -o BatchMode=yes "$SERVER" 'tailscale ip -4 2>/dev/null | grep -q ^100\.'; then
  echo "already on the tailnet"
else
  # accept-dns=false: leave the server's own DNS alone; compose.yaml maps air-1/air-2 by IP.
  agent-run --env-file dashboard/tailscale.env.op -- bash -c \
    "printf '%s' \"\$TS_AUTHKEY\" | ssh -o BatchMode=yes $SERVER 'read -r k; tailscale up --auth-key=\"\$k\" --hostname=${TS_HOSTNAME:-docker-host} --accept-dns=false'"
fi
ssh -o BatchMode=yes "$SERVER" 'tailscale ip -4; tailscale status | head -5'
