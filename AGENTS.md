# AGENTS.md

- This repo manages a fleet of self-hosted GitHub Actions runners (Mac, Windows, WSL/Linux): the `runner` CLI,
  the per-OS installers and on-machine tools (`mac/`, `win/`, `linux/`), the dashboard (`dashboard/`), the
  watchdog (`watchdog/`) and the shared CI sharding and work-stealing pieces (`ci/`, `.github/workflows/ci-plan.yml`,
  `.github/actions/steal`).
- Start with `README.md` (setup, labels, security) and `docs/architecture.md` (how the parts fit).
- **Before pushing:** run `scripts/gate.sh` (the same checks as CI; `scripts/gate.sh shell` or `python` runs one half).
  It needs `shellcheck` and `uv`; `pwsh` is optional.
- **Python style:** format and lint with ruff (`ruff.toml`): `uvx ruff@0.15.20 format .` before committing.
- Never commit secrets, tokens, private keys, built installers (`*.pkg`) or real hostnames and IP addresses.
  Use placeholders (`example-org`, `runners.example.com`, `192.0.2.10`).
- Never attach these runners to public repositories (see the SECURITY section of the README).
