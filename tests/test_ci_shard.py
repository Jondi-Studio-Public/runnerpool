"""ci/ci_shard.py: every test lands in exactly one shard, and bad specs are refused."""

import pathlib

import pytest

CI_DIR = pathlib.Path(__file__).parent.parent / "ci"


@pytest.fixture
def suite(pytester):
    pytester.syspathinsert(CI_DIR)
    pytester.makepyfile(
        test_a="import pytest\n@pytest.mark.parametrize('i', range(7))\ndef test_a(i): pass",
        test_b="def test_b1(): pass\ndef test_b2(): pass\ndef test_b3(): pass",
    )
    return pytester


def collected(suite, *args):
    result = suite.runpytest("-p", "ci_shard", "--collect-only", "-q", *args)
    return {line for line in result.outlines if "::" in line}


def test_without_shard_the_plugin_does_nothing(suite):
    assert len(collected(suite)) == 10


def test_shards_partition_the_suite(suite):
    shards = [collected(suite, "--shard", f"{k}/3") for k in (1, 2, 3)]
    assert sorted(len(s) for s in shards) == [3, 3, 4]
    assert set().union(*shards) == collected(suite)
    assert not shards[0] & shards[1] and not shards[1] & shards[2]


def test_several_shards_in_one_job(suite):
    assert collected(suite, "--shard", "1,2/3") == (
        collected(suite, "--shard", "1/3") | collected(suite, "--shard", "2/3")
    )


@pytest.mark.parametrize("spec", ["4/3", "0/3", "x/3", "3"])
def test_bad_spec_is_a_usage_error(suite, spec):
    result = suite.runpytest("-p", "ci_shard", "--shard", spec)
    assert result.ret == pytest.ExitCode.USAGE_ERROR
