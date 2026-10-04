# Splitting a repo's CI between the PC and the Macs

`.github/workflows/ci-plan.yml` is a reusable workflow. It runs no tests. It looks at which of a
repo's self-hosted runners are online and returns a matrix: the PC takes most of a full run's
shards, and each Mac that is online (so plugged in: a Mac on battery stops its runner) takes a
few. The repo keeps its own install and test steps and feeds that matrix to them. This replaces
copying the routing code into every repo (it was first written for one repo's large test suite).

## Using it from a repo

1. **Register the Macs.** In example-org there is nothing to do: the Macs' CI runners are
   org-level (a repo-specific label such as `myrepo-mac`, and the generic `mac-ci` label that any repo can put in `runs-on`), so every repo in the org can use them. For a repo elsewhere, or
   a repo-specific label: `./runner add air-1 OWNER/REPO "self-hosted,macOS,ARM64,REPO-mac" REPO-mac`
   (or a bare ORG instead of OWNER/REPO) and the same for `air-2`. The Macs' GitHub token
   (the CI GitHub App's installation, or `RUNNER_PAT` on a device not yet switched) must cover that repo or org first.
2. **Let the repo call this one.** If your `git-runner` copy is private, then in its Settings > Actions >
   General > Access, allow repositories in the organisation to use its workflows
   (`gh api -X PUT repos/example-org/runnerpool/actions/permissions/access -f access_level=organization`).
3. **Give the repo a `RUNNER_STATUS_TOKEN` secret**: a fine-grained token owned by the org with
   *Self-hosted runners: read* (org runners) and *Administration: read* on the repo (its own
   runners), so the plan can list both. On GitHub Free, private repos
   can't read org secrets, so it must be a repo secret (`scripts/set-repo-secrets.ps1` sets it on the repos you list). Without it the plan counts no Macs
   and still targets the PC (the job queues until a PC runner is online; there is no hosted fallback).
   **Or use the CI GitHub App instead** (replaces the token): pass `CI_APP_ID` and `CI_APP_PRIVATE_KEY`
   next to `RUNNER_STATUS_TOKEN` in the caller's `secrets:` block. When both are set, the plan mints a
   1-hour installation token with `actions/create-github-app-token` (a node action, run by the
   runner's own node, so the python3-only images are fine) and uses it wherever
   `RUNNER_STATUS_TOKEN` was used; if either is missing, or the App cannot mint a token, the plan
   falls back to `RUNNER_STATUS_TOKEN` exactly as before, so both can be set during the move. The
   App's permissions are in [dashboard-deploy.md](dashboard-deploy.md#the-ci-github-app).
   `CI_APP_ID` and `CI_APP_PRIVATE_KEY` are org secrets (visible to all org repos); a caller must
   pass them explicitly in its `secrets:` block, as above.
4. **Call the plan and use its matrix:**

```yaml
jobs:
  plan:
    uses: example-org/runnerpool/.github/workflows/ci-plan.yml@main
    with:
      full: ${{ github.event_name != 'pull_request' }}   # or your own "whole suite?" test
      target: ${{ inputs.target || 'auto' }}
      pc-labels: '["self-hosted","linux","myrepo"]'
      mac-labels: '["self-hosted","macOS","ARM64","myrepo-mac"]'
    secrets:
      RUNNER_STATUS_TOKEN: ${{ secrets.RUNNER_STATUS_TOKEN }}
      CI_APP_ID: ${{ secrets.CI_APP_ID }}                      # optional: the GitHub App replaces the token
      CI_APP_PRIVATE_KEY: ${{ secrets.CI_APP_PRIVATE_KEY }}

  suite:
    needs: plan
    if: needs.plan.outputs.target == 'pc'
    runs-on: ${{ matrix.runs-on }}
    strategy:
      fail-fast: false
      matrix:
        include: ${{ fromJSON(needs.plan.outputs.matrix) }}
    name: suite (${{ matrix.name }})
    steps:
      - uses: actions/checkout@v5
      # ... install ...
      - run: python -m pytest tests -n ${{ matrix.workers }} ${{ matrix.shard && format('-p ci_shard --shard {0}', matrix.shard) || '' }}
```

Pin `@main` to a tag or commit if you want changes here to reach a repo only when you choose.

## What the plan decides

- **target**: always `pc`. The input accepts `auto` or `pc` (both mean the PC; anything else fails
  the plan), and a job queues while the PC is off. There is no hosted option and no fallback job
  to write. The output stays so callers can keep `if: needs.plan.outputs.target == 'pc'`.
- **matrix**: one entry for the PC and one per online Mac (up to `mac-max`). Each has `name`,
  `runs-on`, `workers` and `shard` (`""` means the whole suite, else `K,K/N`). With N = `pc-weight`
  + Macs x `mac-weight`: by default the PC runs 6 of 8 shards beside one Mac (6 + 2) and 6 of 10 beside two
  Macs (6 + 2 + 2). A run that is not `full`, or not on the PC, is never split.
- **light**: one runs-on label array per `light-slots` (default 4), for jobs that need no PC (a
  route step, `ci-ok`, a Node-only check). Each slot goes to an idle online Mac while one is
  free, then to the PC; with no PC online, to a Mac if one is up. Use it as
  `runs-on: ${{ fromJSON(needs.plan.outputs.light)[0] }}` (slot 0, 1, ... one per job so two jobs
  don't both pick the one idle Mac). It is a snapshot taken when the plan ran, so a Mac that
  went busy meanwhile only means the job queues there. Needs `RUNNER_STATUS_TOKEN`; without it
  every slot is the PC.
- **mac-first**: one runs-on label array for a job that can run on either machine and may queue: the Macs when one is online, else the PC. Use it as `runs-on: ${{ fromJSON(needs.plan.outputs.mac-first) }}`. Without `RUNNER_STATUS_TOKEN` it is the PC.
- **Macs alone never run a repo's CI**: they only join a run the PC is taking.

## Sharding pytest

`ci/ci_shard.py` is the pytest plugin behind `--shard K,K/N` (round-robin over the collected
tests, so slow parametrized sweeps spread across machines). Copy it into the repo next to its
tests, for example as `ci_shard.py` on the path pytest runs from, and pass `-p ci_shard`. For
another test runner, use the same `shard` value to pick your own slice.

## Work stealing (`.github/actions/steal`)

Fixed shard weights guess each machine's speed. The `steal` action lets the machines in a run
take chunks from a shared queue until none is left, so a fast machine does more and the run
ends when the work does. The queue is a set of git refs (`refs/claims/<run>/claim-K`) in the
repo: creating a ref is atomic (GitHub answers 201 to one machine and 422 to the others), so
no server is needed and the Macs need no route to the PC.

```yaml
  suite:                       # the matrix job from the plan, one per machine
    needs: [plan]
    permissions:
      contents: write          # to create and delete the claim refs
    steps:
      # checkout, install ...
      - uses: example-org/runnerpool/.github/actions/steal@main
        with:
          mode: run
          chunks: ${{ needs.plan.outputs.macs != '0' && 16 || 1 }}   # 1 = this machine runs it all
          command: python -m pytest tests -q -n ${{ matrix.workers }} -p ci_shard --shard {shard}

  suite-ok:                    # after all machines; fails if any chunk has no result
    needs: [plan, suite]
    if: always() && needs.suite.result != 'skipped'
    permissions:
      contents: write
    runs-on: [self-hosted, linux, myrepo]
    steps:
      - uses: example-org/runnerpool/.github/actions/steal@main
        with:
          mode: verify
          chunks: ${{ needs.plan.outputs.macs != '0' && 16 || 1 }}
```

`{shard}` in the command becomes `K/N` (`{k}` and `{n}` are also available). Each chunk's
result is recorded as a `done-K` or `failed-K` ref, and the verify job deletes all of them. A
chunk whose machine died has no result, so the run fails rather than passing with tests
unrun. Use **Re-run all jobs**, not re-run failed jobs, so every chunk is claimed afresh.

Trade-offs: each chunk pays pytest's collection and worker start-up again, so more chunks
balance better but cost more (16 is a start; tune it from the log). Balance is to within one
chunk at the end. The job needs `contents: write`, and GitHub gives read-only tokens to some
bot-opened runs (Dependabot), where this cannot work.

## Timing on the Macs

Measured on one project with a suite of about 100 000 tests: one eighth took 6 to 10 minutes on an
M3 Air with 4 workers, so the Macs alone would need roughly 30 to 35 minutes. They pay off as
extra capacity beside the PC, not as a replacement for it.
