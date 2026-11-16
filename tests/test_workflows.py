"""Tests for the rules every workflow of the registry follows."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

import pytest

from tests.conftest import ROOT

if TYPE_CHECKING:
    from pathlib import Path

WORKFLOWS = sorted((ROOT / ".github" / "workflows").glob("*.yml"))
ADMISSION = (ROOT / ".github" / "workflows" / "admission.yml").read_text()
# what gives a job a token beyond its own read-only one, or lets it push
_POWERS = ("secrets.", "create-github-app-token", "git push")


def _jobs(text: str) -> dict[str, str]:
    """Return the text of each job of a workflow, by its id."""
    body = text.split("\njobs:\n", 1)[1]
    parts = re.split(r"^  ([a-z][a-z0-9_-]*):\n", body, flags=re.MULTILINE)
    return dict(zip(parts[1::2], parts[2::2], strict=True))


JOBS = [
    pytest.param(job, text, id=f"{path.name}:{job}")
    for path in WORKFLOWS
    for job, text in _jobs(path.read_text()).items()
]


@pytest.mark.parametrize(("job", "text"), JOBS)
def test_every_job_has_a_time_limit(job: str, text: str) -> None:
    """A job that hangs ends."""
    assert "\n    timeout-minutes: " in text, job


@pytest.mark.parametrize(("job", "text"), JOBS)
def test_no_job_uses_a_cache(job: str, text: str) -> None:
    """A cache is shared with other runs, so no job restores one."""
    assert "actions/cache" not in text
    for step in text.split("\n      - ")[1:]:
        if step.startswith("uses: astral-sh/setup-uv@"):
            assert "enable-cache: false" in step, job


@pytest.mark.parametrize(("job", "text"), JOBS)
def test_only_main_holds_a_token_or_pushes(job: str, text: str) -> None:
    """A job with a secret or a push runs only on main, in the environment of the releases."""
    if any(power in text for power in _POWERS):
        assert "github.ref == 'refs/heads/main'" in text.split("\n    steps:", 1)[0], job
        assert "\n    environment: releases\n" in text, job


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda path: path.name)
def test_no_expression_in_a_script(path: Path) -> None:
    """Values reach a script through its environment, so no value can become shell code."""
    for script in _scripts(path.read_text()):
        assert "${{" not in script, script


def test_admission_runs_the_workflow_of_main() -> None:
    """A pull request, also from a fork, cannot change the workflow that checks it."""
    trigger = ADMISSION.split("\non:\n", 1)[1].split("\npermissions: {}\n", 1)[0]

    assert trigger.startswith("  pull_request_target:\n")
    assert "\n  pull_request:" not in trigger
    assert "\n    branches: [main]\n" in trigger


def test_admission_checks_out_only_the_base() -> None:
    """The pull request is never checked out; its provider.json is read through the API."""
    steps = [step for text in _jobs(ADMISSION).values() for step in text.split("\n      - ")]
    checkouts = [step for step in steps if step.startswith("uses: actions/checkout@")]

    assert checkouts
    for step in checkouts:
        assert "\n          ref: ${{ github.event.pull_request.base.sha }}\n" in step
        assert "\n          persist-credentials: false" in step
        assert "repository:" not in step
        assert "head" not in step


def test_admission_writes_only_its_comment() -> None:
    """Only the job that writes the comment can write anything, and only to pull requests."""
    writes = {
        job: [grant for grant in _permissions(text) if not grant.endswith(": read")]
        for job, text in _jobs(ADMISSION).items()
    }

    assert "\npermissions: {}\n" in ADMISSION
    assert writes == {"prepare": [], "check": [], "result": ["pull-requests: write"]}


def test_admission_uses_no_secret() -> None:
    """The admission needs nothing beyond the token of its own jobs."""
    assert not any(power in ADMISSION for power in _POWERS)
    assert "environment:" not in ADMISSION


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda path: path.name)
def test_provider_code_runs_in_a_job_without_permissions(path: Path) -> None:
    """The job that runs provider code has no token, no checkout and no cache."""
    for job, text in _jobs(path.read_text()).items():
        if "scripts/check-line.sh" in text:
            assert "\n    permissions: {}\n" in text, job
            assert "actions/checkout" not in text, job


def _permissions(job: str) -> list[str]:
    """Return the permissions a job sets, as `scope: access`; none for `permissions: {}`."""
    found = re.search(r"^    permissions:\n((?:      .*\n)+)", job, flags=re.MULTILINE)
    lines = found[1].splitlines() if found else []
    return [line.strip() for line in lines if not line.strip().startswith("#")]


def _scripts(text: str) -> list[str]:
    """Return the run scripts of the steps of a workflow."""
    pattern = r"^(?:      - |        )run:.*(?:\n(?:          .*| *))*"
    return re.findall(pattern, text, flags=re.MULTILINE)
