#!/usr/bin/env bash
# MANUAL FALLBACK. The normal path is .github/workflows/deploy.yml: merging to main deploys after CI
# (docs/dashboard-deploy.md). Use this only when GitHub Actions or the runners are down.
#
# Deploy the runner dashboard to the home server. Run from Git Bash at the repo root.
# Builds locally (the server can't build), ships the image over SSH, recreates the
# container, verifies health.
#
#   scripts/deploy.sh                  deploy HEAD
#   scripts/deploy.sh --force          deploy with a dirty working tree
#   scripts/deploy.sh --rollback <sha> re-point at an already shipped image
#
# First deploy needs two files on the server (see docs/dashboard-deploy.md):
#   /root/macs-dashboard/secrets/gh_token         a GitHub token for gh
#   /root/macs-dashboard/secrets/dashboard_token  the dashboard's access token
set -euo pipefail
cd "$(dirname "$0")/.."

IMAGE=ghcr.io/${GITRUNNER_ORG:?set GITRUNNER_ORG}/macs-dashboard
CONTAINER=macs-dashboard
LOCAL_CTX="${BUILD_CONTEXT:-desktop-linux}"
REMOTE_CTX="${REMOTE_CONTEXT:?set REMOTE_CONTEXT, the docker context of the dashboard server}"
PORT="${MACS_DASHBOARD_PORT:-8765}"
SERVER="${DEPLOY_HOST:?set DEPLOY_HOST, the ssh target of the dashboard server, e.g. root@192.0.2.10}"
HEALTH_URL="${HEALTH_URL:-http://${SERVER#*@}:$PORT/healthz}"
COMPOSE=(docker --context "$REMOTE_CTX" compose -f compose.yaml -f compose.server.yaml)

FORCE=0; ROLLBACK=""
while [ $# -gt 0 ]; do
  case "$1" in
    --force) FORCE=1; shift ;;
    --rollback) ROLLBACK="${2:?--rollback needs a sha}"; shift 2 ;;
    -h|--help) sed -n '5,14p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 1 ;;
  esac
done

log() { printf '\n==> %s\n' "$*"; }

poll_health() {
  log "polling $HEALTH_URL"
  local deadline=$(( $(date +%s) + 60 ))
  while [ "$(date +%s)" -lt "$deadline" ]; do
    curl -fsS -m 5 -o /dev/null "$HEALTH_URL" 2>/dev/null && { echo "    healthy"; return 0; }
    sleep 2
  done
  echo "ERROR: not healthy within 60s: docker --context $REMOTE_CTX logs $CONTAINER" >&2
  return 1
}

# The personal-repo token, the PC push tokens, the GitHub App id and key and the watchdog's ntfy topic and token are optional, but compose needs the files: create them empty if absent.
ensure_personal_token() {
  ssh -o BatchMode=yes "$SERVER" 'for n in gh_token_personal push_tokens gh_app_id gh_app_key ntfy_topic ntfy_token; do f=/root/macs-dashboard/secrets/$n; [ -e "$f" ] ||
    { umask 077; : > "$f"; chown 1000:1000 "$f"; chmod 400 "$f"; }; done'
}

check_secrets() {
  ssh -o BatchMode=yes "$SERVER" 'test -s /root/macs-dashboard/secrets/gh_token && test -s /root/macs-dashboard/secrets/dashboard_token' ||
    { echo "ERROR: /root/macs-dashboard/secrets/{gh_token,dashboard_token} missing on the server: see docs/dashboard-deploy.md" >&2; exit 1; }
}

PREV="$(docker --context "$REMOTE_CTX" inspect "$CONTAINER" --format '{{.Config.Image}}' 2>/dev/null | sed -n "s|^$IMAGE:||p" || true)"

ensure_personal_token

if [ -n "$ROLLBACK" ]; then
  docker --context "$REMOTE_CTX" image inspect "$IMAGE:$ROLLBACK" >/dev/null
  IMAGE_TAG="$ROLLBACK" "${COMPOSE[@]}" up -d --no-build --force-recreate
  poll_health
  echo "Undo: scripts/deploy.sh --rollback ${PREV:-<sha>}"; exit 0
fi

if [ "$FORCE" -ne 1 ] && [ -n "$(git status --porcelain)" ]; then
  echo "ERROR: dirty working tree. Commit first, or pass --force." >&2; exit 1
fi

check_secrets

LOCAL_ARCH="$(docker --context "$LOCAL_CTX" info --format '{{.Architecture}}')"
REMOTE_ARCH="$(docker --context "$REMOTE_CTX" info --format '{{.Architecture}}')"
[ "$LOCAL_ARCH" = "$REMOTE_ARCH" ] || { echo "ERROR: arch $LOCAL_ARCH != $REMOTE_ARCH" >&2; exit 1; }

SHA="$(git rev-parse --short=12 HEAD)"
log "building $IMAGE:$SHA on $LOCAL_CTX"
docker --context "$LOCAL_CTX" build --provenance=false --sbom=false -f dashboard/Dockerfile -t "$IMAGE:$SHA" -t "$IMAGE:dev" .

log "shipping to $REMOTE_CTX"
docker --context "$LOCAL_CTX" save "$IMAGE:$SHA" "$IMAGE:dev" | docker --context "$REMOTE_CTX" load

log "recreating with IMAGE_TAG=$SHA"
IMAGE_TAG="$SHA" "${COMPOSE[@]}" up -d --no-build --force-recreate
poll_health

log "pruning old $IMAGE images (keeping 2 newest + dev)"
docker --context "$REMOTE_CTX" images "$IMAGE" --format '{{.Tag}}\t{{.CreatedAt}}' \
  | sort -t$'\t' -k2 -r | awk -F'\t' '$1!="dev" && $1!="<none>" {print $1}' | tail -n +3 \
  | while read -r t; do docker --context "$REMOTE_CTX" rmi "$IMAGE:$t" >/dev/null || true; done

echo; echo "Rollback: scripts/deploy.sh --rollback ${PREV:-<sha>}"
