"""Runners grouped by device, the default per-runner limits, and the saved overrides."""

import importlib.util
import json
import pathlib

import pytest

SERVER = pathlib.Path(__file__).parent.parent / "dashboard/server.py"


def load(monkeypatch=None, tmp=None):
    spec = importlib.util.spec_from_file_location("dashboard_server_devices", SERVER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    if tmp is not None:
        mod.DATA_DIR = tmp
    return mod


def r(name, labels=(), status="online", busy=False):
    return {"name": name, "os": "", "status": status, "busy": busy, "labels": list(labels)}


REPOS = [
    {
        "repo": "example-org",
        "error": None,
        "runners": [
            r("air-1", ["mac-ci"]),
            r("WIN-1", ["win-ci"]),
            r("win-1-wsl-1", ["linux-ci"], busy=True),
            r("win-1-wsl-2", ["linux-ci"]),
            r("win-2", ["win-ci"]),
            r("stray-box", ["x"]),
        ],
    },
    {
        "repo": "example-org/runnerpool",
        "error": None,
        "runners": [
            r("air-1-admin", ["mac-admin", "air-1"]),
            r("AIR-2-ADMIN", ["Mac-Admin"]),
            r("air-2", ["mac-ci"]),
            r("win-1-admin", ["win-admin"]),
            r("wsl-1-admin", ["linux-admin"]),
            r("win-1", ["win-ci"]),  # also listed at the org: shown once
        ],
    },
]


def test_groups_macs_and_pcs_case_insensitively():
    s = load()
    devices, other = s.device_groups(REPOS, ["air-1", "AIR-2"])
    assert [(d["host"], d["kind"]) for d in devices] == [
        ("air-1", "mac"),
        ("air-2", "mac"),
        ("win-1", "pc"),
        ("win-2", "pc"),
    ]
    by = {d["host"]: [(x["name"], x["kind"]) for x in d["runners"]] for d in devices}
    assert by["air-1"] == [("air-1", "ci"), ("air-1-admin", "admin")]
    assert by["air-2"] == [("air-2", "ci"), ("AIR-2-ADMIN", "admin")]
    assert by["win-1"] == [
        ("WIN-1", "ci"),
        ("win-1-admin", "admin"),
        ("win-1-wsl-1", "ci"),
        ("win-1-wsl-2", "ci"),
        ("wsl-1-admin", "admin"),
    ]
    assert by["win-2"] == [("win-2", "ci")]
    assert [x["name"] for x in other] == ["stray-box"]


def test_runner_fields_and_dedupe():
    s = load()
    devices, _ = s.device_groups(REPOS, ["air-1", "air-2"])
    win1 = next(d for d in devices if d["host"] == "win-1")
    first = next(x for x in win1["runners"] if x["name"].lower() == "win-1")
    assert first["repo"] == "example-org"  # first scope wins
    assert set(first) == {"name", "repo", "kind", "status", "busy", "labels"}
    wsl = next(x for x in win1["runners"] if x["name"] == "win-1-wsl-1")
    assert wsl["busy"] is True and wsl["labels"] == ["linux-ci"]


def test_mac_prefix_does_not_swallow_longer_host():
    s = load()
    repos = [{"repo": "o", "runners": [r("air-1-admin"), r("air-10-admin"), r("air-10")]}]
    devices, other = s.device_groups(repos, ["air-1", "air-10"])
    assert {d["host"]: [x["name"] for x in d["runners"]] for d in devices} == {
        "air-1": ["air-1-admin"],
        "air-10": ["air-10", "air-10-admin"],
    }
    assert other == []


def test_default_limits():
    s = load()
    assert s.default_limits(8, 16384, 2) == {"cores": 3, "ram_mb": 6553}  # floor(.8*8/2)=3, .8*16384/2
    assert s.default_limits(10, 32768, 1) == {"cores": 8, "ram_mb": 26214}
    assert s.default_limits(1, 1024, 4)["cores"] == 1  # never below 1 core
    assert s.default_limits(4, 512, 8)["ram_mb"] == 256  # never below the 256 MB minimum
    assert s.default_limits(None, None, 2) == {"cores": None, "ram_mb": None}
    assert s.default_limits(8, None, 0)["cores"] == 6  # zero CI runners counts as one


def test_device_size_from_info():
    s = load()
    assert s.device_size({"cores": 8, "memory_gb": 16}) == (8, 16384)
    assert s.device_size({"cores": 4, "ram_mb": 8000, "memory_gb": 99}) == (4, 8000)
    assert s.device_size(None) == (None, None)


def test_with_limits_attaches_defaults_and_overrides():
    s = load()
    devices, _ = s.device_groups(REPOS, ["air-1", "air-2"])
    infos = {"air-1": {"info": {"cores": 8, "memory_gb": 16}}}
    out = s.with_limits(devices, infos, {"air-1": {"air-1": {"cores": 2}}})
    a1 = next(d for d in out if d["host"] == "air-1")
    assert (a1["cores"], a1["ram_mb"]) == (8, 16384)
    ci = next(x for x in a1["runners"] if x["kind"] == "ci")
    assert ci["default"] == {"cores": 6, "ram_mb": 13107}
    assert ci["override"] == {"cores": 2}
    assert ci["limits"] == {"cores": 2, "ram_mb": 13107}
    admin = next(x for x in a1["runners"] if x["kind"] == "admin")
    assert "limits" not in admin
    a2 = next(d for d in out if d["host"] == "air-2")
    assert a2["cores"] is None and a2["runners"][0]["limits"] == {"cores": None, "ram_mb": None}


@pytest.mark.parametrize(
    "body",
    [
        [],
        {"runner": "air-1", "cores": 2},
        {"host": "Air 1", "runner": "air-1", "cores": 2},
        {"host": "air-1", "runner": "../etc", "cores": 2},
        {"host": "air-1", "runner": "air-1"},
        {"host": "air-1", "runner": "air-1", "cores": 0},
        {"host": "air-1", "runner": "air-1", "cores": 257},
        {"host": "air-1", "runner": "air-1", "cores": True},
        {"host": "air-1", "runner": "air-1", "cores": "4"},
        {"host": "air-1", "runner": "air-1", "cores": 2.5},
        {"host": "air-1", "runner": "air-1", "ram_mb": 255},
        {"host": "air-1", "runner": "air-1", "ram_mb": 1048577},
    ],
)
def test_validate_rejects(body):
    s = load()
    assert s.validate_limits(body)[3]


def test_validate_accepts_bounds_and_lowercases():
    s = load()
    assert s.validate_limits({"host": "AIR-1", "runner": "Air-1", "cores": 256, "ram_mb": 256}) == (
        "air-1",
        "air-1",
        {"cores": 256, "ram_mb": 256},
        None,
    )
    assert s.validate_limits({"host": "a", "runner": "b", "ram_mb": 1048576})[2] == {"ram_mb": 1048576}
    assert s.validate_limits({"host": "a", "runner": "b", "cores": None})[2] == {"cores": None}
    assert s.validate_limits({"host": "a", "runner": "b", "reset": True})[2] == {"cores": None, "ram_mb": None}


def test_save_persists_merges_resets_atomically(tmp_path):
    s = load(tmp=tmp_path)
    assert s.load_limits() == {}
    assert s.save_limit("air-1", "air-1", {"cores": 2}) == {"cores": 2}
    assert s.save_limit("air-1", "air-1", {"ram_mb": 4096}) == {"cores": 2, "ram_mb": 4096}
    s.save_limit("air-1", "air-2", {"cores": 3})
    assert json.loads((tmp_path / "runner-limits.json").read_text()) == {
        "air-1": {"air-1": {"cores": 2, "ram_mb": 4096}, "air-2": {"cores": 3}}
    }
    assert s.save_limit("air-1", "air-1", {"cores": None}) == {"ram_mb": 4096}
    assert s.save_limit("air-1", "air-1", {"cores": None, "ram_mb": None}) == {}
    assert s.load_limits() == {"air-1": {"air-2": {"cores": 3}}}
    s.save_limit("air-1", "air-2", {"cores": None})
    assert s.load_limits() == {}
    assert [p.name for p in tmp_path.iterdir()] == ["runner-limits.json"]  # no temp files left


def test_load_limits_survives_garbage(tmp_path):
    s = load(tmp=tmp_path)
    (tmp_path / "runner-limits.json").write_text("{not json")
    assert s.load_limits() == {}
    (tmp_path / "runner-limits.json").write_text("[1]")
    assert s.load_limits() == {}


def test_data_dir_env(monkeypatch, tmp_path):
    monkeypatch.setenv("MACS_DATA_DIR", str(tmp_path))
    s = load()
    assert s.limits_path() == tmp_path / "runner-limits.json"
    monkeypatch.delenv("MACS_DATA_DIR")
    assert load().limits_path() == SERVER.parent / "runner-limits.json"
