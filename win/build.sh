#!/usr/bin/env bash
# build.sh: builds win-runners.ps1, the one-file installer for Windows PCs, from
# win/install.template.ps1 and win/winrunner.ps1. Plain text, so it builds anywhere with bash
# (Git Bash included): no WSL, no packaging tools, no GitHub Actions minutes. The runner itself
# is downloaded by the PC at install time, so nothing else needs bundling.
#
#   build.sh OUTDIR [--dry]
#
# The GitHub credential is the CI GitHub App (CI_APP_ID and CI_APP_PRIVATE_KEY, or CI_APP_KEY_B64 for the key
# already base64-encoded on one line) when those are in the environment; otherwise a token from RUNNER_PAT (or a
# hidden prompt). The PC mints one-hour App tokens from the key; with a PAT it stores that as before. --dry puts a
# placeholder token in instead, to check the build itself. `runner build-win` runs it and uploads.
set -euo pipefail
shopt -u patsub_replacement 2>/dev/null || true   # bash 5.2: a & in a value must not mean "the match"

OUT=${1:?usage: build.sh OUTDIR [--dry]}
DRY=${2:-}
HERE=$(cd "$(dirname "$0")" && pwd)
ORG=${GITRUNNER_ORG:?set GITRUNNER_ORG to your GitHub org}
CI_REPO=${CI_REPO:-$ORG}   # org-level CI runners
CI_LABELS=${CI_LABELS:-win-ci}
ADMIN_REPO=${ADMIN_REPO:-$ORG/runnerpool}
# The WSL set the installer offers, as set:count:label-list;... (empty = no WSL step). It makes general
# runners for every repo in the org, named <win host>-wsl-N (e.g. win-1-wsl-1) and opted into with the
# linux-ci tag; a new Linux box is provisioned for them (linux/linux-provision.sh). The docker tag also gets
# Docker Engine installed in the distro (linuxrunner install-docker), for jobs that build x86-64 images.
WSL_SET=${WSL_SET-wsl:2:linux-ci,docker}

if [ -z "${CI_APP_KEY_B64:-}" ] && [ -n "${CI_APP_PRIVATE_KEY:-}" ]; then
  CI_APP_KEY_B64=$(printf '%s' "$CI_APP_PRIVATE_KEY" | base64 | tr -d '\r\n')
fi
unset CI_APP_PRIVATE_KEY
GITHUB_APP_ID=""; GITHUB_APP_KEY_B64=""
if [ "$DRY" = --dry ]; then
  RUNNER_PAT=dry-run-placeholder
elif [ -n "${CI_APP_ID:-}" ] && [ -n "${CI_APP_KEY_B64:-}" ]; then
  GITHUB_APP_ID=$CI_APP_ID; GITHUB_APP_KEY_B64=$CI_APP_KEY_B64
  RUNNER_PAT=""   # the App replaces the PAT: the installer carries only one of them
else
  if [ -z "${RUNNER_PAT:-}" ] && [ -t 0 ]; then
    read -rsp "GitHub token (RUNNER_PAT), hidden: " RUNNER_PAT; echo
  fi
  [ -n "${RUNNER_PAT:-}" ] || { echo "a GitHub App key (CI_APP_ID, CI_APP_PRIVATE_KEY) or a token (RUNNER_PAT) is required"; exit 1; }
fi

# A value inside a PowerShell single-quoted string: only ' needs doubling.
ps_quote() { printf '%s' "${1//\'/\'\'}"; }
latest_tag() {
  curl -fsSI "https://github.com/$1/releases/latest" |
    sed -n 's|^[Ll]ocation: .*/tag/v\{0,1\}\([^[:space:]]*\).*|\1|p' | tr -d '\r' | head -1
}

runner_ver=${RUNNER_VERSION:-}
[ "$DRY" = --dry ] && runner_ver=${runner_ver:-0.0.0}
[ -n "$runner_ver" ] || runner_ver=$(latest_tag actions/runner)
[ -n "$runner_ver" ] || { echo "could not look up the actions/runner version"; exit 1; }

payload=$(base64 < "$HERE/winrunner.ps1" | tr -d '\r\n')
linux_payload=$(base64 < "$HERE/../linux/linuxrunner" | tr -d '\r\n')
provision_payload=$(base64 < "$HERE/../linux/linux-provision.sh" | tr -d '\r\n')
mkdir -p "$OUT"
tmp=$(mktemp "$OUT/.win-runners.XXXXXX")
trap 'rm -f "$tmp"' EXIT
chmod 600 "$tmp"
while IFS= read -r line || [ -n "$line" ]; do
  line=${line%$'\r'}
  line=${line//@@CI_REPO@@/$(ps_quote "$CI_REPO")}
  line=${line//@@CI_LABELS@@/$(ps_quote "$CI_LABELS")}
  line=${line//@@ADMIN_REPO@@/$(ps_quote "$ADMIN_REPO")}
  line=${line//@@RUNNER_PAT@@/$(ps_quote "$RUNNER_PAT")}
  line=${line//@@GITHUB_APP_ID@@/$(ps_quote "$GITHUB_APP_ID")}
  line=${line//@@GITHUB_APP_KEY_B64@@/$GITHUB_APP_KEY_B64}
  line=${line//@@RUNNER_VERSION@@/$(ps_quote "$runner_ver")}
  line=${line//@@WSL_SET@@/$(ps_quote "$WSL_SET")}
  line=${line//@@LINUX_PAYLOAD@@/$linux_payload}
  line=${line//@@PROVISION_PAYLOAD@@/$provision_payload}
  line=${line//@@PAYLOAD@@/$payload}
  printf '%s\r\n' "$line"   # CRLF: Windows PowerShell 5.1 is happiest with it
done < "$HERE/install.template.ps1" > "$tmp"
unset RUNNER_PAT GITHUB_APP_KEY_B64 CI_APP_KEY_B64
grep -q '@@' "$tmp" && { echo "a placeholder was left unfilled"; exit 1; }
mv "$tmp" "$OUT/win-runners.ps1"
trap - EXIT
echo "built $OUT/win-runners.ps1 ($(wc -c < "$OUT/win-runners.ps1") bytes, actions runner $runner_ver)"
