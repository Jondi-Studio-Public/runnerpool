"""The dashboard's CI panel, against a stand-in `gh` that answers from a table of API paths.

The stand-in also records which GH_TOKEN each call carried, so the per-owner token (a repo
outside the org reads with its own token) is checked too.
"""

import importlib.util
import json
import os
import pathlib
import sys

import pytest

SERVER = pathlib.Path(__file__).parent.parent / "dashboard/server.py"

FAKE_GH = """#!/usr/bin/env python3
import json, os, sys
path = sys.argv[2]
with open(os.environ["FAKE_GH_LOG"], "a") as f:
    f.write(json.dumps([path, os.environ.get("GH_TOKEN", "")]) + "\\n")
answers = json.load(open(os.environ["FAKE_GH_ANSWERS"]))
if path not in answers:
    sys.stderr.write("gh: Not Found (HTTP 404)\\n")
    sys.exit(1)
print(json.dumps(answers[path]))
"""


def check(runs):
    return {
        "check_runs": [
            {
                "name": "ci-ok",
                "status": s,
                "conclusion": c,
                "started_at": t,
                "html_url": f"https://github.com/x/runs/{t}",
            }
            for s, c, t in runs
        ]
    }


def load_server(monkeypatch, tmp_path, answers, env):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    gh = bindir / "gh"
    gh.write_text(FAKE_GH)
    gh.chmod(0o755)
    (tmp_path / "answers.json").write_text(json.dumps(answers))
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_GH_ANSWERS", str(tmp_path / "answers.json"))
    monkeypatch.setenv("FAKE_GH_LOG", str(tmp_path / "log"))
    monkeypatch.setenv("GH_TOKEN", "org-token")
    for k in ("MACS_CI_REPOS", "MACS_CI_EXTRA", "MACS_CI_TOKEN_FILES", "MACS_ORG"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    spec = importlib.util.spec_from_file_location("dashboard_server", SERVER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def calls(tmp_path):
    return [json.loads(line) for line in (tmp_path / "log").read_text().splitlines()]


Q = "check-runs?check_name=ci-ok&per_page=20"
PULLS = "pulls?state=open&sort=updated&direction=desc&per_page=10"


@pytest.mark.skipif(sys.platform == "win32", reason="the stand-in gh is a shebang script")
def test_rows_for_org_and_extra_repos(monkeypatch, tmp_path):
    token = tmp_path / "personal"
    token.write_text("personal-token\n")
    answers = {
        "orgs/Org/repos?type=all&per_page=100": [
            {"name": "b", "full_name": "Org/b", "default_branch": "main"},
            {"name": "old", "full_name": "Org/old", "default_branch": "main", "archived": True},
            {"name": "A", "full_name": "Org/A", "default_branch": "trunk"},
        ],
        # Org/A: main failed after an earlier pass; one PR running, one with no ci-ok yet.
        f"repos/Org/A/commits/trunk/{Q}": check(
            [("completed", "success", "2026-10-01T08:00:00Z"), ("completed", "failure", "2026-10-01T09:00:00Z")]
        ),
        f"repos/Org/A/{PULLS}": [
            {
                "number": 7,
                "title": "Seven",
                "draft": False,
                "html_url": "https://github.com/Org/A/pull/7",
                "head": {"ref": "f7", "sha": "s7"},
            },
            {
                "number": 8,
                "title": "Eight",
                "draft": True,
                "html_url": "https://github.com/Org/A/pull/8",
                "head": {"ref": "f8", "sha": "s8"},
            },
        ],
        f"repos/Org/A/commits/s7/{Q}": check([("in_progress", None, "2026-10-01T09:10:00Z")]),
        f"repos/Org/A/commits/s8/{Q}": check([]),
        "repos/Org/A/actions/runs?per_page=30": {
            "workflow_runs": [{"status": "in_progress"}, {"status": "queued"}, {"status": "completed"}]
        },
        # Org/b: green, nothing open, workflow runs unreadable.
        f"repos/Org/b/commits/main/{Q}": check([("completed", "success", "2026-10-01T07:00:00Z")]),
        f"repos/Org/b/{PULLS}": [],
        # Me/fw: outside the org, read with its own token.
        "repos/Me/fw": {"default_branch": "main"},
        f"repos/Me/fw/commits/main/{Q}": check([("completed", "skipped", "2026-10-01T06:00:00Z")]),
        f"repos/Me/fw/{PULLS}": [],
        "repos/Me/fw/actions/runs?per_page=30": {"workflow_runs": []},
    }
    server = load_server(
        monkeypatch,
        tmp_path,
        answers,
        {"MACS_ORG": "Org", "MACS_CI_EXTRA": "Me/fw Org/b", "MACS_CI_TOKEN_FILES": f"me={token}"},
    )

    ci = server.STATE.get_ci()

    assert ci["error"] is None
    rows = {r["repo"]: r for r in ci["repos"]}
    assert [r["repo"] for r in ci["repos"]] == ["Org/A", "Org/b", "Me/fw"]  # archived gone, no duplicate
    a = rows["Org/A"]
    assert a["branch"] == "trunk" and a["main"]["state"] == "failed"
    assert [(p["number"], p["draft"], p["ci"]["state"]) for p in a["prs"]] == [
        (7, False, "running"),
        (8, True, "missing"),
    ]
    assert (a["running"], a["queued"], a["error"]) == (1, 1, None)
    b = rows["Org/b"]
    assert b["main"]["state"] == "passed" and b["prs"] == [] and b["running"] is None
    assert b["error"].startswith("workflow runs:")
    assert rows["Me/fw"]["main"]["state"] == "skipped"
    tokens = {tok for path, tok in calls(tmp_path) if "/Me/fw" in path}
    assert tokens == {"personal-token"}
    assert {tok for path, tok in calls(tmp_path) if "/Org/" in path} == {"org-token"}

    # Cached: a second call makes no gh calls.
    n = len(calls(tmp_path))
    assert server.STATE.get_ci() is ci and len(calls(tmp_path)) == n


@pytest.mark.skipif(sys.platform == "win32", reason="the stand-in gh is a shebang script")
def test_unreadable_repo_and_empty_token_file(monkeypatch, tmp_path):
    empty = tmp_path / "personal"
    empty.write_text("")
    server = load_server(
        monkeypatch, tmp_path, {}, {"MACS_CI_REPOS": "Me/private", "MACS_CI_TOKEN_FILES": f"Me={empty}"}
    )

    ci = server.STATE.get_ci()

    (row,) = ci["repos"]
    assert row["repo"] == "Me/private" and "404" in row["error"] and row["main"] is None
    assert {tok for _, tok in calls(tmp_path)} == {"org-token"}  # empty file: falls back to GH_TOKEN


@pytest.mark.parametrize("value", ["all", 1, 8, 256])
def test_cores_accepts_all_or_a_count(monkeypatch, tmp_path, value):
    assert load_server(monkeypatch, tmp_path, {}, {}).valid_cores(value)


@pytest.mark.parametrize("value", [0, -1, 257, 2.5, "4", "ALL", None, True, [], {}])
def test_cores_rejects_anything_else(monkeypatch, tmp_path, value):
    assert not load_server(monkeypatch, tmp_path, {}, {}).valid_cores(value)


def test_cores_is_a_known_action_and_fills_the_count(monkeypatch, tmp_path):
    mod = load_server(monkeypatch, tmp_path, {}, {})
    ssh_args, gh_args = mod.ACTIONS["cores"]
    assert [a.format(host="air-1", cores=4) for a in ssh_args] == ["cores", "4"]
    assert [a.format(host="air-1", cores="all") for a in gh_args] == ["cores", "air-1", "all"]
