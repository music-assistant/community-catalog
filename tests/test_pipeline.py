"""Tests for what the steps after the checks do: record the results, and report failures."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest

from registry.cli import main
from registry.edit import refuse_release
from registry.files import load_model
from registry.github import post_issue
from registry.models import FetchReport, Issue, LineResults, RefusedRelease, RepositoryState
from registry.pipeline import versions_to_check
from registry.registry import load_registry
from registry.report import REGISTRY_REPOSITORY, build_issues
from tests.conftest import CHECKED, COMMIT, DOMAIN, RELEASED, FakeGitHub

if TYPE_CHECKING:
    from tests.conftest import Files

REPOSITORY = "someone/ma-sample"


def _results(line: str, *results: dict[str, Any]) -> dict[str, Any]:
    """Return the content of a results file."""
    return {"line": line, "checked": "2026-11-15T03:00:00Z", "results": list(results)}


def _result(version: str, reason: str | None = None, **changes: Any) -> dict[str, Any]:
    """Return the result of a version of the sample provider; passed without a reason."""
    return {"domain": DOMAIN, "version": version, "passed": reason is None, "reason": reason,
            **changes}  # fmt: skip


def _run(files: Files, *args: str) -> int:
    """Run a command on the registry of the fixture."""
    return main(["--root", str(files.root), *args])


def _index(files: Files, line: str) -> dict[str, Any]:
    """Return the index file of a line."""
    index: dict[str, Any] = json.loads((files.root / f"site/v1/{line}.json").read_text())
    return index


def test_versions_to_check(files: Files) -> None:
    """Versions without a result and each latest are checked; nothing blocked or frozen."""
    files.write(
        "server_lines.json",
        {
            "lines": {
                "nightly": {"image": "ghcr.io/music-assistant/server:nightly"},
                "2.11": {"image": "ghcr.io/music-assistant/server:2.11", "frozen": True},
            }
        },
    )
    files.provider(blocked_versions={"1.3.0": "Sends the library elsewhere"})
    files.version("1.0.0", lines={"nightly": True})
    files.version("1.1.0", lines={"nightly": True})
    files.version("1.2.0", lines={"nightly": False})
    files.version("1.3.0", lines={})
    files.version("1.4.0", lines={})
    files.provider("blocked_music", status="blocked", reason="Sends the library elsewhere")
    files.version("1.0.0", domain="blocked_music", lines={})
    registry = load_registry(files.root)

    assert versions_to_check(registry, "nightly") == [(DOMAIN, "1.1.0"), (DOMAIN, "1.4.0")]
    assert versions_to_check(registry, "2.11") == []


def test_apply_results(files: Files) -> None:
    """The results of every line are recorded and the index files written."""
    files.provider()
    files.version("1.0.0", lines={})
    files.write("results/nightly.json", _results("nightly", _result("1.0.0")))
    files.write("results/beta.json", _results("beta", _result("1.0.0", "load: no module")))

    assert _run(files, "apply-results", str(files.root / "results")) == 0

    checks = load_registry(files.root).providers[DOMAIN].versions["1.0.0"].checks
    assert (checks["nightly"].passed, checks["nightly"].checked) == (True, "2026-11-15T03:00:00Z")
    assert (checks["beta"].passed, checks["beta"].reason) == (False, "load: no module")
    assert _index(files, "nightly")["providers"][DOMAIN]["latest"] == "1.0.0"
    assert DOMAIN in _index(files, "beta")["providers"]
    assert _index(files, "beta")["providers"][DOMAIN]["latest"] is None
    assert _run(files, "validate") == 0


def test_a_version_that_stops_passing_falls_back(files: Files) -> None:
    """When the latest version stops passing, the newest version that still passes is latest."""
    files.provider()
    files.version("1.0.0", lines={"nightly": True})
    files.version("1.1.0", lines={"nightly": False})
    files.version("1.2.0", lines={"nightly": True})
    assert _run(files, "build") == 0
    assert _index(files, "nightly")["providers"][DOMAIN]["latest"] == "1.2.0"
    files.write("results/nightly.json", _results("nightly", _result("1.2.0", "load: broken")))

    assert _run(files, "apply-results", str(files.root / "results")) == 0

    provider = _index(files, "nightly")["providers"][DOMAIN]
    assert provider["latest"] == "1.0.0"
    assert set(provider["versions"]) == {"1.0.0"}


@pytest.mark.parametrize(
    ("content", "problem"),
    [
        (_results("2.10", _result("1.0.0")), "'2.10' is not a server line that is tested"),
        (_results("nightly", _result("9.0.0")), "sample_music has no version 9.0.0"),
        (_results("nightly", {**_result("1.0.0"), "domain": "x"}), "there is no provider x"),
        (_results("nightly", _result("1.0.0", "")), "a version that did not pass needs a reason"),
        (
            {**_results("nightly", _result("1.0.0")), "checked": "today"},
            "checks.nightly.checked: 'today' is not a date",
        ),
        ({**_results("nightly"), "extra": 1}, "unknown key extra"),
        ({"line": "nightly", "checked": CHECKED, "results": "all"}, '"results" must be a list'),
    ],
)
def test_results_are_checked_like_any_change(
    files: Files, capsys: pytest.CaptureFixture[str], content: dict[str, Any], problem: str
) -> None:
    """Results come from a step that ran provider code; what is not valid changes nothing."""
    files.provider()
    path = files.version("1.0.0", lines={})
    before = path.read_bytes()
    files.write("results/nightly.json", content)

    assert _run(files, "apply-results", str(files.root / "results")) == 1

    assert problem in capsys.readouterr().err
    assert path.read_bytes() == before


def test_a_frozen_line_takes_no_results(files: Files, capsys: pytest.CaptureFixture[str]) -> None:
    """Results for a frozen line are refused."""
    files.write(
        "server_lines.json",
        {"lines": {"nightly": {"image": "ghcr.io/music-assistant/server:nightly", "frozen": True}}},
    )
    files.provider()
    files.version("1.0.0", lines={})
    files.write("results/nightly.json", _results("nightly", _result("1.0.0")))

    assert _run(files, "apply-results", str(files.root / "results")) == 1
    assert "'nightly' is not a server line that is tested" in capsys.readouterr().err


def _fetched(*, has_issues: bool = True, refused: bool = False) -> FetchReport:
    """Return the report of a fetch step that read the sample repository."""
    state = RepositoryState(
        owner="someone", private=False, archived=False, has_issues=has_issues, license="mit"
    )
    return FetchReport.from_dict(
        {
            "repositories": {REPOSITORY: state.to_dict()},
            "refused": [{"domain": DOMAIN, "version": "2.0.0"}] if refused else [],
        }
    )


def test_issues_for_failures_and_refusals(files: Files) -> None:
    """Each failed check and each release refused in this run gets an issue on the repository."""
    files.provider()
    files.version("1.0.0", lines={})
    files.version("1.1.0", lines={})
    registry = load_registry(files.root)
    reason = "unknown key 'x'\n```\ninjected"
    refuse_release(
        registry,
        DOMAIN,
        RefusedRelease(
            version="2.0.0",
            tag="v2.0.0",
            commit=COMMIT,
            released=RELEASED,
            refused=CHECKED,
            reason=reason,
        ),
    )
    results = [
        load_model(
            files.write(
                "results/nightly.json",
                _results(
                    "nightly",
                    _result("1.0.0", "load: broken", output="Traceback\n````\nload: broken"),
                    _result("1.1.0", "load: broken", passed_before=True),
                    {**_result("1.1.0"), "domain": DOMAIN},
                ),
            ),
            LineResults,
        )
    ]

    issues = build_issues(load_registry(files.root), _fetched(refused=True), results)

    assert [issue.title for issue in issues] == [
        "Release v2.0.0 of sample_music was refused by the community catalog",
        "sample_music 1.0.0 does not pass the community catalog checks on nightly",
        "sample_music 1.1.0 no longer passes the community catalog checks on nightly",
    ]
    assert {issue.repository for issue in issues} == {REPOSITORY}
    refused, failed, stopped = issues
    assert "````\nunknown key 'x'\n```\ninjected\n````" in refused.body
    assert "<summary>Output</summary>\n\n`````\nTraceback\n````\nload: broken\n`````" in failed.body
    assert "It passed before" in stopped.body
    assert f"<!-- community-catalog: {failed.key} -->" in failed.body
    assert len({issue.key for issue in issues}) == 3
    # the same failure always has the same key
    again = build_issues(load_registry(files.root), _fetched(refused=True), results)
    assert [issue.key for issue in again] == [issue.key for issue in issues]


def test_issue_goes_to_the_registry_without_issues(files: Files) -> None:
    """When the provider's repository has issues switched off, the registry gets the issue."""
    files.provider(maintainers=["someone", "another"])
    files.version("1.0.0", lines={})
    results = [
        load_model(
            files.write("results/nightly.json", _results("nightly", _result("1.0.0", "load: x"))),
            LineResults,
        )
    ]

    [issue] = build_issues(load_registry(files.root), _fetched(has_issues=False), results)

    assert issue.repository == REGISTRY_REPOSITORY
    assert f"`{REPOSITORY}` has issues switched off. @someone @another" in issue.body


def test_report_writes_the_issues(files: Files) -> None:
    """The report command writes each issue as a file named after its key."""
    files.provider()
    files.version("1.0.0", lines={})
    files.write("results/nightly.json", _results("nightly", _result("1.0.0", "load: x")))
    files.write("work/fetch.json", _fetched().to_dict())

    assert _run(files, "report", str(files.root / "results")) == 0

    [path] = (files.root / "work" / "issues").iterdir()
    issue = load_model(path, Issue)
    assert path.name == f"{issue.key}.json"
    assert issue.repository == REPOSITORY


def test_post_issue_opens_an_issue_once() -> None:
    """An issue is opened unless an issue with its key exists."""
    github = FakeGitHub()
    issue = Issue(key="0123456789abcdef", repository=REPOSITORY, title="A title", body="A body")

    assert post_issue(issue, "a-token", github) == f"https://github.com/{REPOSITORY}/issues/1"
    github.issue_keys[REPOSITORY] = {issue.key}
    assert post_issue(issue, "a-token", github) is None

    assert github.created == [(REPOSITORY, {"title": "A title", "body": "A body"})]
    assert all(r.get_header("Authorization") == "Bearer a-token" for r in github.requests)
