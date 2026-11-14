"""Tests for what the steps after the checks do: record the results, and report failures."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest

from registry.cli import main
from registry.edit import refuse_release
from registry.files import load_model
from registry.github import GitHub, post_issue
from registry.models import (
    FetchReport,
    Issue,
    LineResults,
    RefusedRelease,
    RepositoryState,
)
from registry.pipeline import versions_to_check
from registry.registry import load_registry
from registry.report import REGISTRY_REPOSITORY, build_issues
from tests.conftest import (
    CHECKED,
    COMMIT,
    DOMAIN,
    OWNER_ID,
    RELEASED,
    REPOSITORY_ID,
    FakeGitHub,
)

if TYPE_CHECKING:
    from pathlib import Path

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


def _plan(files: Files, lines: dict[str, list[str]], domain: str = DOMAIN) -> None:
    """Write the plan of the check step: the versions of a provider to check, by line."""
    plan = {
        line: [{"domain": domain, "version": version} for version in versions]
        for line, versions in lines.items()
    }
    files.write("work/plan.json", {"lines": plan})


def _write_result(files: Files, line: str, result: dict[str, Any], name: str = "") -> Path:
    """Write the results file of one version where the check step puts it, or at a name."""
    name = name or f"{result['domain']}-{result['version']}.json"
    return files.write(f"results/{line}/{name}", _results(line, result))


def _apply(files: Files) -> int:
    """Run apply-results on the results folder of the fixture."""
    return _run(files, "apply-results", str(files.root / "results"))


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

    assert versions_to_check(registry, "nightly") == [
        (DOMAIN, "1.1.0"),
        (DOMAIN, "1.2.0"),
        (DOMAIN, "1.4.0"),
    ]
    assert versions_to_check(registry, "2.11") == []


def test_a_version_that_failed_once_gets_another_chance(files: Files) -> None:
    """A latest version that failed on a broken night is checked again, and comes back."""
    files.provider()
    files.version("1.4.0", lines={"nightly": True})
    files.version("1.5.0", lines={"nightly": True})
    _plan(files, {"nightly": ["1.4.0", "1.5.0"]})
    _write_result(files, "nightly", _result("1.5.0", "load: ImportError in the server"))
    assert _apply(files) == 0
    assert _index(files, "nightly")["providers"][DOMAIN]["latest"] == "1.4.0"

    assert versions_to_check(load_registry(files.root), "nightly") == [
        (DOMAIN, "1.4.0"),
        (DOMAIN, "1.5.0"),
    ]
    _write_result(files, "nightly", _result("1.5.0"))
    assert _apply(files) == 0
    assert _index(files, "nightly")["providers"][DOMAIN]["latest"] == "1.5.0"


def test_only_the_newest_versions_above_the_latest_are_checked_again(files: Files) -> None:
    """Of the versions above the latest that failed, the three newest are checked again."""
    files.provider()
    files.version("1.0.0", lines={"nightly": True})
    for version in ("1.1.0", "1.2.0", "1.3.0", "1.4.0", "1.5.0"):
        files.version(version, lines={"nightly": False})

    assert versions_to_check(load_registry(files.root), "nightly") == [
        (DOMAIN, "1.0.0"),
        (DOMAIN, "1.3.0"),
        (DOMAIN, "1.4.0"),
        (DOMAIN, "1.5.0"),
    ]


def test_apply_results(files: Files) -> None:
    """The results of every line are recorded and the index files written."""
    files.provider()
    files.version("1.0.0", lines={})
    _plan(files, {"nightly": ["1.0.0"], "beta": ["1.0.0"]})
    _write_result(files, "nightly", _result("1.0.0"))
    _write_result(files, "beta", _result("1.0.0", "load: no module"))

    assert _apply(files) == 0

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
    _plan(files, {"nightly": ["1.2.0"]})
    _write_result(files, "nightly", _result("1.2.0", "load: broken"))

    assert _apply(files) == 0

    provider = _index(files, "nightly")["providers"][DOMAIN]
    assert provider["latest"] == "1.0.0"
    assert set(provider["versions"]) == {"1.0.0"}
    # the night after, the new latest and both versions above it are checked
    assert versions_to_check(load_registry(files.root), "nightly") == [
        (DOMAIN, "1.0.0"),
        (DOMAIN, "1.1.0"),
        (DOMAIN, "1.2.0"),
    ]


@pytest.mark.parametrize(
    ("content", "problem"),
    [
        (_results("nightly", _result("1.0.0", "")), "a failed check has one line of reason"),
        (_results("nightly", _result("1.0.0", "x" * 301)), "a failed check has one line"),
        (
            _results("nightly", _result("1.0.0", "load: forged " + "x" * 1_000_000)),
            "ignored, not a regular file of at most 200000 bytes",
        ),
        (_results("nightly", _result("1.0.0", "load: a\nb")), "a failed check has one line"),
        (
            _results("nightly", {**_result("1.0.0"), "reason": "load: x"}),
            "a failed check has one line of reason and a pass none",
        ),
        (
            {**_results("nightly", _result("1.0.0")), "checked": "today"},
            "checks.nightly.checked: 'today' is not a date",
        ),
        ({**_results("nightly"), "extra": 1}, "unknown key extra"),
        ({"line": "nightly", "checked": CHECKED, "results": "all"}, '"results" must be a list'),
    ],
)
def test_a_result_that_is_not_valid_is_skipped(
    files: Files, capsys: pytest.CaptureFixture[str], content: dict[str, Any], problem: str
) -> None:
    """Results come from containers that ran provider code; one that is not valid is skipped."""
    files.provider()
    path = files.version("1.0.0", lines={})
    before = path.read_bytes()
    files.version("1.1.0", lines={})
    _plan(files, {"nightly": ["1.0.0", "1.1.0"]})
    files.write(f"results/nightly/{DOMAIN}-1.0.0.json", content)
    _write_result(files, "nightly", _result("1.1.0"))

    assert _apply(files) == 0

    assert problem in capsys.readouterr().err
    assert path.read_bytes() == before
    versions = load_registry(files.root).providers[DOMAIN].versions
    assert versions["1.1.0"].checks["nightly"].passed
    assert _run(files, "validate") == 0


def test_a_result_counts_only_for_its_own_version(
    files: Files, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """A result is read only at the name the plan gives it, for exactly that version."""
    files.provider("aaa_evil", repository="someone/aaa-evil", folder="aaa_evil")
    files.version("1.0.0", "aaa_evil", folder="aaa_evil", lines={"nightly": True})
    files.provider()
    victim = files.version("1.0.0", lines={"nightly": True})
    files.version("1.1.0", lines={"nightly": True})
    before = victim.read_bytes()
    assert _run(files, "build") == 0
    _plan(files, {"nightly": ["1.0.0"], "beta": ["1.0.0"]}, domain="aaa_evil")
    forged = _result("1.0.0", "load: forged")
    # in the file of the evil provider, about the victim
    _write_result(files, "nightly", forged, name="aaa_evil-1.0.0.json")
    # in the file of the evil provider for beta, about nightly
    files.write("results/beta/aaa_evil-1.0.0.json", _results("nightly", _result("1.0.0")))
    # at the name of the victim, which is not in the plan
    _write_result(files, "nightly", forged)
    # at a name of its own, and behind a link
    _write_result(files, "nightly", forged, name="extra.json")
    (tmp_path / "secret").write_text("{}")
    (files.root / "results" / "2.11").symlink_to(tmp_path)

    assert _apply(files) == 0

    err = capsys.readouterr().err
    assert "aaa_evil-1.0.0.json: ignored, it does not hold the one result of aaa_evil 1.0.0" in err
    assert f"{DOMAIN}-1.0.0.json': ignored, not a results file of the plan" in err
    assert "extra.json': ignored, not a results file of the plan" in err
    assert victim.read_bytes() == before
    assert _index(files, "nightly")["providers"][DOMAIN]["latest"] == "1.1.0"
    assert _run(files, "validate") == 0


def test_a_results_file_behind_a_link_or_too_large_is_ignored(
    files: Files, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """Only a regular file of at most 200 kB at the name of the plan is read."""
    files.provider()
    files.version("1.0.0", lines={})
    files.version("1.1.0", lines={})
    _plan(files, {"nightly": ["1.0.0", "1.1.0"]})
    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps(_results("nightly", _result("1.0.0"))))
    (files.root / "results" / "nightly").mkdir(parents=True)
    (files.root / "results" / "nightly" / f"{DOMAIN}-1.0.0.json").symlink_to(outside)
    _write_result(files, "nightly", _result("1.1.0", output="x" * 200_000))

    assert _apply(files) == 0

    err = capsys.readouterr().err
    assert err.count("ignored, not a regular file of at most 200000 bytes") == 2
    assert all(not v.checks for v in load_registry(files.root).providers[DOMAIN].versions.values())


def test_a_missing_result_and_a_frozen_line(
    files: Files, capsys: pytest.CaptureFixture[str]
) -> None:
    """A version without a result keeps its results; a frozen line takes none."""
    files.write(
        "server_lines.json",
        {
            "lines": {
                "nightly": {"image": "ghcr.io/music-assistant/server:nightly"},
                "2.11": {"image": "ghcr.io/music-assistant/server:2.11", "frozen": True},
            }
        },
    )
    files.provider()
    files.version("1.0.0", lines={})
    _plan(files, {"nightly": ["1.0.0"], "2.11": ["1.0.0"]})
    _write_result(files, "2.11", _result("1.0.0"))

    assert _apply(files) == 0

    err = capsys.readouterr().err
    assert f"{DOMAIN} 1.0.0 on nightly: no result" in err
    assert "'2.11': skipped, not a server line that is tested" in err
    assert load_registry(files.root).providers[DOMAIN].versions["1.0.0"].checks == {}


def _fetched(*, has_issues: bool = True, refused: bool = False) -> FetchReport:
    """Return the report of a fetch step that read the sample repository."""
    state = RepositoryState(
        owner="someone",
        repository_id=REPOSITORY_ID,
        owner_id=OWNER_ID,
        private=False,
        archived=False,
        has_issues=has_issues,
        license="mit",
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


def test_the_output_in_an_issue_fits_github(files: Files) -> None:
    """GitHub takes 65536 characters in an issue: the end of a long output is kept."""
    files.provider()
    files.version("1.0.0", lines={})
    output = "start" + "x" * 60_000 + "load: the end"
    results = [
        LineResults.from_dict(_results("nightly", _result("1.0.0", "load: x", output=output)))
    ]

    [issue] = build_issues(load_registry(files.root), _fetched(), results)

    assert len(issue.body) < 45_000
    assert "load: the end\n" in issue.body
    assert "start" not in issue.body


def test_a_repository_that_is_not_the_admitted_one(files: Files) -> None:
    """Another repository under the name of a provider gets an issue on the registry, once."""
    files.provider(maintainers=["someone", "another"])
    files.version("1.0.0", lines={})
    fetched = _fetched()
    fetched.repositories[REPOSITORY].owner_id = 999

    [issue] = build_issues(load_registry(files.root), fetched, [])

    assert issue.repository == REGISTRY_REPOSITORY
    assert issue.title == "The repository of sample_music is not the one that was admitted"
    assert f"GitHub gives it repository_id {REPOSITORY_ID}, owner_id 999" in issue.body
    assert "\n@someone @another\n" in issue.body
    assert build_issues(load_registry(files.root), fetched, [])[0].key == issue.key


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
    _plan(files, {"nightly": ["1.0.0"]})
    _write_result(files, "nightly", _result("1.0.0", "load: x"))
    # a result the plan does not name is not reported
    _write_result(files, "nightly", _result("1.0.0", "load: forged"), name="other.json")
    files.write("work/fetch.json", _fetched().to_dict())

    assert _run(files, "report", str(files.root / "results")) == 0

    [path] = (files.root / "work" / "issues").iterdir()
    issue = load_model(path, Issue)
    assert path.name == f"{issue.key}.json"
    assert issue.repository == REPOSITORY


def test_post_issue_opens_an_issue_once() -> None:
    """An issue is opened unless the same account opened an issue with its key."""
    github = FakeGitHub()
    client = GitHub("a-token", github)
    issue = Issue(key="0123456789abcdef", repository=REPOSITORY, title="A title", body="A body")
    # a stranger who copies the key into an issue of their own does not stop ours
    github.issues.add((REPOSITORY, "a-stranger", issue.key))

    assert post_issue(issue, client) == f"https://github.com/{REPOSITORY}/issues/1"
    github.issues.add((REPOSITORY, FakeGitHub.LOGIN, issue.key))
    assert post_issue(issue, client) is None

    assert github.created == [(REPOSITORY, {"title": "A title", "body": "A body"})]
    assert all(r.get_header("Authorization") == "Bearer a-token" for r in github.requests)
    # the account is asked for once
    assert [r.full_url for r in github.requests].count("https://api.github.com/user") == 1


def test_post_issues_goes_on_after_a_refusal(
    files: Files, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An issue GitHub refuses is reported, and the other issues are opened all the same."""
    github = FakeGitHub()
    for key, repository in (("0000000000000001", "someone/gone"), ("0000000000000002", REPOSITORY)):
        issue = Issue(key=key, repository=repository, title=key, body="A body")
        files.write(f"work/issues/{key}.json", issue.to_dict())
    github.broken["https://api.github.com/repos/someone/gone/issues"] = OSError("refused")
    monkeypatch.setenv("GITHUB_TOKEN", "a-token")
    monkeypatch.setattr("registry.cli.GitHub", lambda token: GitHub(token, github))

    assert _run(files, "post-issues") == 1

    assert [repository for repository, _ in github.created] == ["someone/gone", REPOSITORY]
    assert "warning: someone/gone:" in capsys.readouterr().err
