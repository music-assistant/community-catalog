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
# the scopes of the token of a job
_SCOPES = (
    "actions",
    "attestations",
    "checks",
    "contents",
    "deployments",
    "discussions",
    "id-token",
    "issues",
    "models",
    "packages",
    "pages",
    "pull-requests",
    "security-events",
    "statuses",
)


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


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda path: path.name)
def test_permissions_are_spelled_as_the_tests_read_them(path: Path) -> None:
    """Every permissions key is {} or a block of known scopes, so no grant hides from the tests."""
    lines = path.read_text().splitlines()
    for number, line in enumerate(lines):
        found = re.fullmatch(r"( *)permissions:(.*)", line)
        if found is None:
            continue
        value = found[2].split("#", 1)[0].strip()
        assert value in {"", "{}"}, line
        if value:
            continue
        indent = len(found[1])
        grants = []
        for following in lines[number + 1 :]:
            if following.strip() and len(following) - len(following.lstrip()) <= indent:
                break
            if following.strip() and not following.strip().startswith("#"):
                grants.append(following.strip())
        assert grants, line
        for grant in grants:
            assert re.fullmatch(rf"({'|'.join(_SCOPES)}): (read|write|none)", grant), grant


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda path: path.name)
def test_no_workflow_runs_privileged_on_pull_requests(path: Path) -> None:
    """A pull request never runs a workflow with more than a read-only token."""
    assert "pull_request_target" not in path.read_text()


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda path: path.name)
def test_a_piped_step_fails_with_its_command(path: Path) -> None:
    """A step that pipes through tee runs in bash, whose pipefail keeps the command's failure."""
    for text in _jobs(path.read_text()).values():
        for step in text.split("\n      - "):
            if "| tee" in step:
                assert "\n        shell: bash\n" in step, step


def test_admission_runs_on_submissions() -> None:
    """The admission starts from an issue, or by hand for one, never from a pull request."""
    trigger = ADMISSION.split("\non:\n", 1)[1].split("\npermissions: {}\n", 1)[0]

    assert trigger.startswith("  issues:\n    types: [opened, edited, reopened]\n")
    assert "\n  workflow_dispatch:\n" in trigger
    assert "pull_request" not in trigger
    assert "contains(github.event.issue.labels.*.name, 'submission')" in _jobs(ADMISSION)["prepare"]


def test_admission_checks_out_the_commit_of_the_run() -> None:
    """Every checkout takes the commit the workflow runs at, never one the event names."""
    steps = [step for text in _jobs(ADMISSION).values() for step in text.split("\n      - ")]
    checkouts = [step for step in steps if step.startswith("uses: actions/checkout@")]

    assert checkouts
    for step in checkouts:
        assert "\n          persist-credentials: false" in step
        assert "ref:" not in step
        assert "repository:" not in step


def test_admission_writes_only_its_comment_and_pull_request() -> None:
    """Only the result writes, and only to issues; the app token only in propose."""
    jobs = _jobs(ADMISSION)
    grants = {job: _permissions(text) for job, text in jobs.items()}

    assert "\npermissions: {}\n" in ADMISSION
    assert grants == {
        "prepare": ["contents: read", "issues: read"],
        "check": [],
        "propose": ["contents: read"],
        "result": ["contents: read", "issues: write"],
    }
    assert "\n    permissions: {}\n" in jobs["check"]
    assert [job for job, text in jobs.items() if any(p in text for p in _POWERS)] == ["propose"]
    token = re.findall(r"^          permission-.*$", jobs["propose"], flags=re.MULTILINE)
    assert token == [
        "          permission-contents: write",
        "          permission-pull-requests: write",
    ]


def test_admission_reports_also_after_a_failure() -> None:
    """The jobs that report run when an earlier job failed, but not when the run was cancelled."""
    jobs = _jobs(ADMISSION)

    for job in ("propose", "result"):
        condition = re.search(r"^    if: (.*)$", jobs[job], flags=re.MULTILINE)
        assert condition is not None, job
        assert "!cancelled()" in condition[1], job
        assert "always()" not in jobs[job], job
    assert "needs: [prepare, check, propose]" in jobs["result"]


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda path: path.name)
def test_provider_code_runs_in_a_job_without_permissions(path: Path) -> None:
    """The job that runs provider code has no token, no checkout and no cache."""
    for job, text in _jobs(path.read_text()).items():
        if "scripts/check-line.sh" in text:
            assert "\n    permissions: {}\n" in text, job
            assert "actions/checkout" not in text, job


def _permissions(job: str) -> list[str]:
    """
    Return the permissions a job sets, as `scope: access`; none for `permissions: {}`.

    Only the spellings test_permissions_are_spelled_as_the_tests_read_them allows are read.
    """
    found = re.search(r"^    permissions:\n((?:      .*\n)+)", job, flags=re.MULTILINE)
    lines = found[1].splitlines() if found else []
    return [line.strip() for line in lines if not line.strip().startswith("#")]


def _scripts(text: str) -> list[str]:
    """Return the run scripts of the steps of a workflow."""
    pattern = r"^(?:      - |        )run:.*(?:\n(?:          .*| *))*"
    return re.findall(pattern, text, flags=re.MULTILINE)
