#!/usr/bin/env bash
# build-local.sh: builds git-runner-mac.pkg on Linux (the PC's Ubuntu WSL) without a Mac or any
# GitHub Actions minutes. `runner build-local` runs it and uploads the result; it is the local
# twin of .github/workflows/build.yml, so keep the two in step.
#
#   build-local.sh OUTDIR [--dry]
#
# Needs (once): sudo apt-get install -y build-essential cpio golang-go libxml2-dev libssl-dev
# zlib1g-dev autoconf. Builds mkbom (bomutils) and xar into ~/pkgtools on first use.
# The GitHub credential is the CI GitHub App (CI_APP_ID and CI_APP_PRIVATE_KEY, or CI_APP_KEY_B64 for the key
# already base64-encoded on one line) when those are in the environment; otherwise a token from RUNNER_PAT. The
# Tailscale key comes from TS_AUTHKEY. Anything missing is asked for with a hidden prompt; --dry puts placeholders
# in instead, to check the build itself.
set -euo pipefail

OUT=${1:?usage: build-local.sh OUTDIR [--dry]}
DRY=${2:-}
HERE=$(cd "$(dirname "$0")" && pwd)
TOOLS=$HOME/pkgtools
ORG=${GITRUNNER_ORG:?set GITRUNNER_ORG to your GitHub org}
CI_REPO=$ORG  # org-level CI runners
CI_LABELS=mac-ci-heavy,mac-ci
CI_HOST_LABELS=${CI_HOST_LABELS:-}  # extra labels per Mac, HOST=label[,label];...  e.g. host-a=gpu
ADMIN_REPO=${ADMIN_REPO:-$ORG/runnerpool}
IDENT=${GITRUNNER_BUNDLE_ID:-io.github.git-runner.mac-runners}   # reverse-DNS bundle id; set GITRUNNER_BUNDLE_ID for your own

log() { printf '== %s\n' "$*"; }

latest_tag() {  # owner/repo -> its latest release tag, from the redirect (no API rate limit)
  curl -fsSI "https://github.com/$1/releases/latest" |
    sed -n 's|^[Ll]ocation: .*/tag/\([^[:space:]]*\).*|\1|p' | tr -d '\r' | head -1
}

tools() {
  mkdir -p "$TOOLS"
  if [ ! -x "$TOOLS/bomutils/build/bin/mkbom" ]; then
    log "building mkbom"
    [ -d "$TOOLS/bomutils" ] || git clone -q https://github.com/hogliux/bomutils.git "$TOOLS/bomutils"
    make -s -C "$TOOLS/bomutils"
  fi
  if [ ! -x "$TOOLS/local/bin/xar" ]; then
    log "building xar"
    [ -d "$TOOLS/xar" ] || git clone -q https://github.com/mackyle/xar.git "$TOOLS/xar"
    ( cd "$TOOLS/xar/xar"
      # 2014 code: OpenSSL 3 dropped this symbol, and newer compilers reject its old C.
      sed -i 's/OpenSSL_add_all_ciphers/OPENSSL_init_crypto/' configure.ac
      ./autogen.sh --noconfigure >/dev/null
      ./configure --prefix="$TOOLS/local" >/dev/null \
        CFLAGS="-O2 -std=gnu17 -Wno-implicit-function-declaration -Wno-incompatible-pointer-types -Wno-int-conversion"
      make -s >/dev/null && make -s install >/dev/null )
  fi
}
mkbom() { "$TOOLS/bomutils/build/bin/mkbom" "$@"; }
xar() { LD_LIBRARY_PATH="$TOOLS/local/lib" "$TOOLS/local/bin/xar" "$@"; }

secrets() {
  if [ -z "${CI_APP_KEY_B64:-}" ] && [ -n "${CI_APP_PRIVATE_KEY:-}" ]; then
    CI_APP_KEY_B64=$(printf '%s' "$CI_APP_PRIVATE_KEY" | base64 | tr -d '\r\n')
  fi
  unset CI_APP_PRIVATE_KEY
  USE_APP=""
  if [ "$DRY" = --dry ]; then
    RUNNER_PAT=dry-run-placeholder; TS_AUTHKEY=""
    return
  fi
  if [ -n "${CI_APP_ID:-}" ] && [ -n "${CI_APP_KEY_B64:-}" ]; then
    USE_APP=1   # the App replaces the PAT: the package carries only one of them
  else
    if [ -z "${RUNNER_PAT:-}" ] && [ -t 0 ]; then
      read -rsp "GitHub token (RUNNER_PAT), hidden: " RUNNER_PAT; echo
    fi
    [ -n "${RUNNER_PAT:-}" ] || { echo "a GitHub App key (CI_APP_ID, CI_APP_PRIVATE_KEY) or a token (RUNNER_PAT) is required"; exit 1; }
  fi
  if [ -z "${TS_AUTHKEY:-}" ] && [ -t 0 ]; then
    read -rsp "Tailscale auth key (TS_AUTHKEY), hidden, Enter to skip: " TS_AUTHKEY; echo
  fi
  TS_AUTHKEY=${TS_AUTHKEY:-}
}

main() {
  tools
  secrets
  local ts_ver runner_ver version
  work=$(mktemp -d)  # global: the EXIT trap runs after main returns
  trap 'rm -rf "$work"' EXIT
  local root=$work/root scripts=$work/scripts flat=$work/flat
  mkdir -p "$root/usr/local/mac-runners/bin" "$scripts" "$flat" "$OUT"

  ts_ver=$(latest_tag tailscale/tailscale)
  runner_ver=$(latest_tag actions/runner); runner_ver=${runner_ver#v}
  [ -n "$ts_ver" ] && [ -n "$runner_ver" ] || { echo "could not look up release versions"; exit 1; }
  log "Tailscale $ts_ver (darwin/arm64), actions runner $runner_ver"
  GOPATH=$TOOLS/gopath GOBIN="" GOOS=darwin GOARCH=arm64 CGO_ENABLED=0 \
    go install "tailscale.com/cmd/tailscale@$ts_ver" "tailscale.com/cmd/tailscaled@$ts_ver"
  install -m 755 "$TOOLS/gopath/bin/darwin_arm64/tailscale" "$TOOLS/gopath/bin/darwin_arm64/tailscaled" \
    "$root/usr/local/mac-runners/bin/"

  # Tailscale is BSD-3-Clause and redistributed here: ship its licence text (see THIRD_PARTY_NOTICES.md).
  mkdir -p "$root/usr/local/mac-runners/LICENSES"
  curl -fsSL "https://raw.githubusercontent.com/tailscale/tailscale/$ts_ver/LICENSE" \
    -o "$root/usr/local/mac-runners/LICENSES/tailscale.txt" || { echo "could not fetch the Tailscale licence text"; exit 1; }
  [ -s "$root/usr/local/mac-runners/LICENSES/tailscale.txt" ] || { echo "Tailscale licence text is empty"; exit 1; }
  install -m 644 "$HERE/../THIRD_PARTY_NOTICES.md" "$root/usr/local/mac-runners/LICENSES/THIRD_PARTY_NOTICES.md"

  install -m 755 "$HERE/gitrunner" "$root/usr/local/mac-runners/gitrunner"
  install -m 755 "$HERE/postinstall" "$scripts/postinstall"
  local env_file=$root/usr/local/mac-runners/bootstrap.env
  ( umask 077
    { printf 'GITRUNNER_ORG=%q\n' "$ORG"
      printf 'CI_REPO=%q\nCI_LABELS=%q\nCI_HOST_LABELS=%q\n' "$CI_REPO" "$CI_LABELS" "$CI_HOST_LABELS"
      printf 'TS_AUTHKEY=%q\n' "$TS_AUTHKEY"
      if [ -n "$USE_APP" ]; then
        printf 'ADMIN_REPO=%q\nGITHUB_APP_ID=%q\nGITHUB_APP_KEY_B64=%q\n' "$ADMIN_REPO" "$CI_APP_ID" "$CI_APP_KEY_B64"
      else
        printf 'ADMIN_REPO=%q\nRUNNER_PAT=%q\n' "$ADMIN_REPO" "$RUNNER_PAT"
      fi
      printf 'RUNNER_VERSION=%q\n' "$runner_ver"
    } > "$env_file" )
  chmod 600 "$env_file"
  unset RUNNER_PAT TS_AUTHKEY CI_APP_ID CI_APP_KEY_B64

  # A component package, as pkgbuild makes one: a xar of Bom, PackageInfo, Payload, Scripts.
  log "assembling the package"
  version=1.$(date -u +%Y%m%d%H%M)
  ( cd "$root" && find . | LC_ALL=C sort | cpio -o --quiet --format odc --owner 0:0 ) | gzip -9n > "$flat/Payload"
  ( cd "$scripts" && find . | LC_ALL=C sort | cpio -o --quiet --format odc --owner 0:0 ) | gzip -9n > "$flat/Scripts"
  mkbom -u 0 -g 0 "$root" "$flat/Bom"
  local files kbytes
  files=$(find "$root" | wc -l)
  kbytes=$(du -sk "$root" | cut -f1)
  cat > "$flat/PackageInfo" <<EOF
<?xml version="1.0" encoding="utf-8"?>
<pkg-info overwrite-permissions="true" relocatable="false" identifier="$IDENT" postinstall-action="none" version="$version" format-version="2" generator-version="build-local" install-location="/" auth="root">
    <payload numberOfFiles="$files" installKBytes="$kbytes"/>
    <bundle-version/>
    <upgrade-bundle/>
    <update-bundle/>
    <atomic-update-bundle/>
    <strict-identifier/>
    <relocate/>
    <scripts>
        <postinstall file="./postinstall"/>
    </scripts>
</pkg-info>
EOF
  rm -f "$OUT/git-runner-mac.pkg"
  ( cd "$flat" && xar --compression none -cf "$OUT/git-runner-mac.pkg" Bom PackageInfo Payload Scripts )
  log "built $OUT/git-runner-mac.pkg ($(du -h "$OUT/git-runner-mac.pkg" | cut -f1), version $version)"
}

main
