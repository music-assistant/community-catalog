"""Tests for the script of the workflow that closes an issue that is not a form."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from typing import TYPE_CHECKING, Any

import pytest
import yaml

from tests.conftest import ROOT

if TYPE_CHECKING:
    from pathlib import Path

WORKFLOW = ROOT / ".github" / "workflows" / "close-issues.yml"
[STEP] = yaml.safe_load(WORKFLOW.read_text())["jobs"]["close"]["steps"]
MARKER = STEP["env"]["MARKER"]
# stands in for gh: answers the comments of the issue from a file, and records every call
_FAKE_GH = """#!{python}
import json, os, sys

args = sys.argv[1:]
with open(os.environ["FAKE_GH_LOG"], "a") as log:
    log.write(json.dumps(args) + "\\n")
if args == ["api", "--paginate", "repos/someone/registry/issues/5/comments"]:
    with open(os.environ["FAKE_GH_COMMENTS"]) as comments:
        print(comments.read())
"""


def _run(tmp_path: Path, comments: list[dict[str, Any]]) -> list[list[str]]:
    """Run the step of the workflow with a gh that knows these comments; return its calls."""
    folder = tmp_path / "bin"
    folder.mkdir()
    gh = folder / "gh"
    gh.write_text(_FAKE_GH.format(python=sys.executable))
    gh.chmod(0o755)
    (tmp_path / "comments.json").write_text(json.dumps(comments))
    log = tmp_path / "log"
    log.touch()
    environment = {
        **os.environ,
        **STEP["env"],
        "PATH": f"{folder}{os.pathsep}{os.environ['PATH']}",
        "GH_TOKEN": "a-token",
        "NUMBER": "5",
        "REPOSITORY": "someone/registry",
        "FAKE_GH_LOG": str(log),
        "FAKE_GH_COMMENTS": str(tmp_path / "comments.json"),
    }
    bash = shutil.which("bash")
    assert bash is not None
    subprocess.run([bash, "-e", "-o", "pipefail", "-c", STEP["run"]], env=environment, check=True)
    return [json.loads(line) for line in log.read_text().splitlines()]


def _comment(login: str, body: str) -> dict[str, Any]:
    """Return a comment as the API answers it."""
    return {"id": 1, "user": {"login": login}, "body": body}


CLOSE = ["api", "-X", "PATCH", "repos/someone/registry/issues/5", "-f", "state=closed",
         "-f", "state_reason=not_planned"]  # fmt: skip


@pytest.mark.skipif(shutil.which("jq") is None, reason="needs jq")
def test_an_issue_gets_one_comment_and_is_closed(tmp_path: Path) -> None:
    """The fixed comment starts with the marker and says where to go; then the issue closes."""
    calls = _run(tmp_path, [_comment("someone", f"{MARKER} written by the author")])

    listing, comment, close = calls
    assert listing[:2] == ["api", "--paginate"]
    assert comment[:2] == ["api", "repos/someone/registry/issues/5/comments"]
    assert comment[2] == "-f"
    assert comment[3] == f"body={MARKER}\n{STEP['env']['COMMENT']}".rstrip("\n")
    for text in ("gives no support", "support/issues/new/choose", "discord.gg",
                 "template=submit-provider.yml", "template=report-provider.yml"):  # fmt: skip
        assert text in comment[3], text
    assert close == CLOSE


@pytest.mark.skipif(shutil.which("jq") is None, reason="needs jq")
def test_a_reopened_issue_is_closed_without_a_second_comment(tmp_path: Path) -> None:
    """The comment of an earlier run is found by its marker, so the issue is only closed."""
    calls = _run(tmp_path, [_comment("github-actions[bot]", f"{MARKER}\nThis repository")])

    assert calls[1:] == [CLOSE]
