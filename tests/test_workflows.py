"""Tests for the rules every workflow of the registry follows."""

from __future__ import annotations

import re

import pytest

from tests.conftest import ROOT

WORKFLOWS = sorted((ROOT / ".github" / "workflows").glob("*.yml"))
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
