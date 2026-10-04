#!/bin/bash
# linux-provision.sh: runs inside a brand-new Linux box (a WSL distro, or the Mac Colima VM; as root) and installs what general CI jobs
# need: git, python3 with venv and pip, a compiler, uv, and the Postgres 16 binaries (integration
# job starts its own throwaway cluster from them). Arch-neutral (x86-64 and ARM64). It marks the box provisioned only
# when every step worked; until then the Linux runners stay off, so no job lands on a box that
# cannot run it. Not run on an adopted distro (its runners are already set up by hand).
# Jobs that need Node are not covered: add a step here when one needs them. Docker is not here either: only the PC's
# WSL runners get it, from `linuxrunner install-docker` (the Mac VMs run this script too and stay without).
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq curl git python3 python3-venv python3-pip jq sudo ca-certificates tar unzip build-essential postgresql >/dev/null
# Only the binaries are wanted: the package's default cluster on 5432 must not run.
systemctl disable --now postgresql >/dev/null 2>&1 || true
test -x /usr/lib/postgresql/16/bin/initdb
curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin UV_NO_MODIFY_PATH=1 sh >/dev/null
/usr/local/bin/uv --version
mkdir -p /opt/git-runner
touch /opt/git-runner/provisioned
echo "provisioned"
