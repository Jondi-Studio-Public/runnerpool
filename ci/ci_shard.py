"""Pytest plugin: run some of N shards of the suite (CI's split runs).

    python -m pytest tests -p ci_shard --shard 3/8 -n auto
    python -m pytest tests -p ci_shard --shard 1,2,3,4,5,6/10 -n 32

Shard K of N keeps every N-th collected test starting at K (1-based), so the
slow parametrized sweeps, which sit next to each other in collection order,
land on different shards instead of one. Collection order is deterministic, so
every xdist worker keeps the same items and the N shards together run each
test exactly once. Several shards, comma-separated, give one machine a bigger
share in one job: the PC takes 6 of 10 and each Mac 2 (docs/ci-plan.md).
Without `--shard` the plugin does nothing.

Round-robin by test, not by file, costs each shard every module's collection
and module-scoped setup; on GitHub's two-core machines that is cheaper than one
shard carrying all of `tests/test_mathgen.py`'s sweeps.
"""

from __future__ import annotations

import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--shard",
        default=None,
        metavar="K[,K...]/N",
        help="run only shards K of N (1-based, comma-separated), round-robin over collected tests",
    )


def _parse(spec: str) -> tuple[frozenset[int], int]:
    try:
        ks, n = spec.split("/")
        shards = frozenset(int(k) for k in ks.split(","))
        n = int(n)
    except ValueError:
        raise pytest.UsageError(f"--shard wants K/N or K,K,.../N, got {spec!r}") from None
    if not all(1 <= k <= n for k in shards):
        raise pytest.UsageError(f"--shard {spec}: every K must be between 1 and N")
    return shards, n


def pytest_configure(config: pytest.Config) -> None:
    spec = config.getoption("--shard")
    if spec:
        _parse(spec)


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    spec = config.getoption("--shard")
    if not spec:
        return
    shards, n = _parse(spec)
    keep = [item for i, item in enumerate(items) if i % n + 1 in shards]
    kept = {id(item) for item in keep}
    dropped = [item for item in items if id(item) not in kept]
    if dropped:
        config.hook.pytest_deselected(items=dropped)
    items[:] = keep
