"""Tests for the rules every workflow of the registry follows, read from the parsed workflows."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from typing import TYPE_CHECKING, Any

import pytest
import yaml

from tests.conftest import ROOT

if TYPE_CHECKING:
    from pathlib import Path

WORKFLOWS = sorted((ROOT / ".github" / "workflows").glob("*.yml"))
# the scopes of the token of a job
SCOPES = frozenset(
    {
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
    }
)
# the events that run a workflow for a pull request, which may come from a fork; a reusable
# workflow runs in the pull requests of the repositories that call it
PULL_REQUEST_EVENTS = frozenset(
    {"pull_request", "pull_request_target", "pull_request_review", "workflow_call"}
)
APP_TOKEN = "actions/create-github-app-token@"  # noqa: S105 - an action, not a token


def _load(path: Path) -> dict[Any, Any]:
    """Return a workflow as GitHub reads it."""
    workflow = yaml.safe_load(path.read_text())
    assert isinstance(workflow, dict)
    return workflow


ADMISSION: dict[str, Any] = _load(ROOT / ".github" / "workflows" / "admission.yml")
JOBS = [
    pytest.param(_load(path), job, id=f"{path.name}:{job}")
    for path in WORKFLOWS
    for job in _load(path)["jobs"]
]


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda path: path.name)
def test_permissions_are_explicit(path: Path) -> None:
    """Every workflow sets its permissions, each {} or a mapping of known scopes."""
    workflow = _load(path)

    assert "permissions" in workflow
    for value in [
        workflow["permissions"],
        *(job.get("permissions", {}) for job in _jobs(workflow)),
    ]:
        assert isinstance(value, dict), value
        for scope, access in value.items():
            assert scope in SCOPES, scope
            assert access in {"read", "write", "none"}, access


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda path: path.name)
def test_nothing_writes_on_a_pull_request(path: Path) -> None:
    """A pull request, also from a fork, never runs a job that can write."""
    workflow = _load(path)
    events = _events(workflow)

    assert "pull_request_target" not in events
    if events & PULL_REQUEST_EVENTS:
        for job in workflow["jobs"]:
            assert _writes(workflow, job) == set(), job
            assert not _app_tokens(workflow["jobs"][job]), job


@pytest.mark.parametrize(("workflow", "job"), JOBS)
def test_every_job_has_a_time_limit(workflow: dict[str, Any], job: str) -> None:
    """A job that hangs ends."""
    assert isinstance(workflow["jobs"][job].get("timeout-minutes"), int)


@pytest.mark.parametrize(("workflow", "job"), JOBS)
def test_no_job_uses_a_cache(workflow: dict[str, Any], job: str) -> None:
    """A cache is shared with other runs, so no job restores one."""
    for step in _steps(workflow["jobs"][job]):
        uses = step.get("uses", "")
        assert not uses.startswith("actions/cache"), job
        if uses.startswith("astral-sh/setup-uv@"):
            assert step["with"]["enable-cache"] is False, job


@pytest.mark.parametrize(("workflow", "job"), JOBS)
def test_only_main_holds_a_token_or_pushes(workflow: dict[str, Any], job: str) -> None:
    """A job with a secret, the app token or a push runs only on main, in its environment."""
    definition = workflow["jobs"][job]
    text = json.dumps(definition)
    if "secrets." in text or _app_tokens(definition) or "git push" in text:
        assert "github.ref == 'refs/heads/main'" in definition["if"], job
        assert definition["environment"] == "releases", job


@pytest.mark.parametrize(("workflow", "job"), JOBS)
def test_no_expression_in_a_script(workflow: dict[str, Any], job: str) -> None:
    """Values reach a script through its environment, so no value can become shell code."""
    for step in _steps(workflow["jobs"][job]):
        assert "${{" not in step.get("run", ""), step


@pytest.mark.parametrize(("workflow", "job"), JOBS)
def test_a_piped_step_fails_with_its_command(workflow: dict[str, Any], job: str) -> None:
    """A step that pipes through tee runs in bash, whose pipefail keeps the command's failure."""
    for step in _steps(workflow["jobs"][job]):
        if "| tee" in step.get("run", ""):
            assert step.get("shell") == "bash", step


@pytest.mark.parametrize(("workflow", "job"), JOBS)
def test_provider_code_runs_in_a_job_without_permissions(
    workflow: dict[str, Any], job: str
) -> None:
    """The job that runs provider code has no token, no checkout and no secret."""
    definition = workflow["jobs"][job]
    if "scripts/check-line.sh" in json.dumps(definition):
        assert definition["permissions"] == {}, job
        assert not any(
            step.get("uses", "").startswith("actions/checkout@") for step in _steps(definition)
        )
        assert "secrets." not in json.dumps(definition)


def test_admission_runs_on_submissions() -> None:
    """The admission starts from an issue, or by hand for one, never from a pull request."""
    triggers = _on(ADMISSION)

    assert set(triggers) == {"issues", "workflow_dispatch"}
    assert triggers["issues"] == {"types": ["opened", "edited", "reopened", "closed"]}
    prepare = ADMISSION["jobs"]["prepare"]["if"]
    assert "github.event.issue.state == 'open'" in prepare
    assert "contains(github.event.issue.labels.*.name, 'submission')" in prepare
    # closing a submission closes its pull request
    propose = ADMISSION["jobs"]["propose"]["if"]
    assert "github.event.action == 'closed'" in propose


def test_admission_checks_out_the_commit_of_the_run() -> None:
    """Every checkout takes the commit the workflow runs at, never one the event names."""
    checkouts = [
        step
        for job in ADMISSION["jobs"].values()
        for step in _steps(job)
        if step.get("uses", "").startswith("actions/checkout@")
    ]

    assert checkouts
    for step in checkouts:
        assert step["with"] == {"persist-credentials": False}


def test_admission_writes_only_its_comment_and_pull_request() -> None:
    """The result writes to issues; only propose holds the app token, and only for its work."""
    grants = {job: _effective(ADMISSION, job) for job in ADMISSION["jobs"]}

    assert ADMISSION["permissions"] == {}
    assert grants == {
        "prepare": {"contents": "read", "issues": "read"},
        "check": {},
        "propose": {"contents": "read"},
        "result": {"contents": "read", "issues": "write"},
    }
    tokens = {job: _app_tokens(definition) for job, definition in ADMISSION["jobs"].items()}
    assert {job for job, steps in tokens.items() if steps} == {"propose"}
    [token] = tokens["propose"]
    assert {key: value for key, value in token["with"].items() if key.startswith("permission")} == {
        "permission-contents": "write",
        "permission-issues": "read",
        "permission-pull-requests": "write",
    }


def test_admission_reports_also_after_a_failure() -> None:
    """The jobs that report run when an earlier job failed, but not when the run was cancelled."""
    jobs = ADMISSION["jobs"]

    for job in ("propose", "result"):
        assert "!cancelled()" in jobs[job]["if"], job
        assert "always()" not in json.dumps(jobs[job]), job
    assert jobs["result"]["needs"] == ["prepare", "check", "propose"]


@pytest.mark.skipif(shutil.which("jq") is None, reason="needs jq")
@pytest.mark.parametrize(
    ("plan", "lines"),
    [
        ({"lines": {}}, []),
        ({"lines": {"nightly": [], "beta": []}}, []),
        (
            {"lines": {"nightly": [{"domain": "x", "version": "1.0.0"}], "beta": []}},
            [{"line": "nightly", "image": "ghcr.io/music-assistant/server:nightly"}],
        ),
    ],
    ids=["no plan", "nothing planned", "one line"],
)
def test_admission_checks_only_the_lines_of_its_plan(
    tmp_path: Path, plan: dict[str, Any], lines: list[dict[str, str]]
) -> None:
    """A submission with nothing to check starts no check job, so its code never runs."""
    [step] = [step for step in _steps(ADMISSION["jobs"]["prepare"]) if step.get("id") == "lines"]
    (tmp_path / "work").mkdir()
    (tmp_path / "work" / "plan.json").write_text(json.dumps(plan))
    shutil.copy(ROOT / "server_lines.json", tmp_path)
    output = tmp_path / "output"
    bash = shutil.which("bash")
    assert bash is not None

    subprocess.run(
        [bash, "-e", "-c", step["run"]],
        cwd=tmp_path,
        env={**os.environ, "GITHUB_OUTPUT": str(output)},
        check=True,
    )

    assert json.loads(output.read_text().removeprefix("lines=")) == lines
    assert ADMISSION["jobs"]["check"]["if"] == "needs.prepare.outputs.lines != '[]'"


def _on(workflow: dict[Any, Any]) -> dict[str, Any]:
    """Return the triggers of a workflow, by event; YAML 1.1 reads the key on as true."""
    triggers = workflow.get("on", workflow.get(True))
    if isinstance(triggers, str):
        return {triggers: None}
    if isinstance(triggers, list):
        return dict.fromkeys(triggers)
    assert isinstance(triggers, dict)
    return triggers


def _events(workflow: dict[str, Any]) -> set[str]:
    """Return the events that run a workflow."""
    return set(_on(workflow))


def _jobs(workflow: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the jobs of a workflow."""
    return list(workflow["jobs"].values())


def _steps(job: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the steps of a job; none for a job that calls a workflow."""
    return list(job.get("steps", []))


def _effective(workflow: dict[str, Any], job: str) -> dict[str, str]:
    """Return the permissions the token of a job has, as test_permissions_are_explicit allows."""
    permissions = workflow["jobs"][job].get("permissions", workflow["permissions"])
    assert isinstance(permissions, dict)
    return {scope: access for scope, access in permissions.items() if access != "none"}


def _writes(workflow: dict[str, Any], job: str) -> set[str]:
    """Return the scopes the token of a job can write."""
    return {scope for scope, access in _effective(workflow, job).items() if access == "write"}


def _app_tokens(job: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the steps of a job that make a token of an app."""
    return [step for step in _steps(job) if step.get("uses", "").startswith(APP_TOKEN)]
