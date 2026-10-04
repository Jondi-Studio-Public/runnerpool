# Contributing

Thanks for helping. Small, focused pull requests are easiest to review.

## Before you open a PR

1. Run the local gate, which runs the same checks as CI:

   ```bash
   scripts/gate.sh            # everything
   scripts/gate.sh shell      # shellcheck and a parse of every shell and PowerShell script
   scripts/gate.sh python     # ruff format check, lint, compile, tests
   ```

   It needs `shellcheck` and `uv` (`pwsh` is optional and checked when present).
2. Format Python with ruff, pinned in the gate: `uvx ruff@0.15.20 format .`.
3. Add or update tests in `tests/` for behaviour you change. Tests are pytest; the shell and PowerShell
   tools are tested through their pure functions and fake environments, so no real Mac, Windows PC or
   GitHub account is needed. Do not skip, disable or loosen a test to get green.
4. Keep real hostnames, IP addresses, org names, tokens and keys out of commits. Use `example-org`,
   `runners.example.com` and `192.0.2.10`.

## Pull request flow

Fork, branch, open a PR against `main`, and wait for CI (the single `ci-ok` check) to pass. Describe what
changed and how you tested it; say plainly if it was only build-tested or not run on real hardware (the
Windows side and the Mac installer package are in that state today, see the README).

## Security issues

Do not file them publicly: see [SECURITY.md](SECURITY.md).

## Licence

By contributing you agree your work is released under the [MIT licence](LICENSE).
