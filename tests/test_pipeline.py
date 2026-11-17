"""Tests for what the steps after the checks do: record the results, and report failures."""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any

import pytest

from registry.cli import main
from registry.edit import refuse_release
from registry.files import load_model
from registry.github import COMMENTS_PER_PAGE, GitHub
from registry.markdown import code_block
from registry.models import (
    ChannelResults,
    FetchReport,
    ProviderReport,
    RefusedRelease,
    RepositoryState,
)
from registry.pipeline import versions_to_check
from registry.registry import load_registry
from registry.report import (
    LABEL,
    build_channel_reports,
    build_reports,
    is_settled,
    open_channel_issues,
    open_issues,
    post_channel_report,
    post_report,
)
from tests.conftest import (
    CHECKED,
    COMMIT,
    DOMAIN,
    OWNER_ID,
    RELEASED,
    REPOSITORY_ID,
    FakeGitHub,
    manifest,
)

if TYPE_CHECKING:
    from pathlib import Path

    from tests.conftest import Files

REPOSITORY = "someone/ma-sample"
REGISTRY = "music-assistant/community-catalog"
# the reason of a check that could not run in the server image
UNAVAILABLE = "load: the load script cannot start in this server image: cannot import name 'x'"


def _results(channel: str, *results: dict[str, Any]) -> dict[str, Any]:
    """Return the content of a results file."""
    return {"channel": channel, "checked": "2026-11-15T03:00:00Z", "results": list(results)}


def _result(version: str, reason: str | None = None, **changes: Any) -> dict[str, Any]:
    """Return the result of a version of the sample provider; passed without a reason."""
    return {"domain": DOMAIN, "version": version, "passed": reason is None, "reason": reason,
            **changes}  # fmt: skip


def _unavailable(version: str) -> dict[str, Any]:
    """Return the result of a version of the sample provider whose check could not run."""
    output = "Traceback\nImportError: cannot import name 'x'"
    return _result(version, UNAVAILABLE, output=output, unavailable=True)


def _run(files: Files, *args: str) -> int:
    """Run a command on the registry of the fixture."""
    return main(["--root", str(files.root), *args])


def _plan(files: Files, channels: dict[str, list[str]], domain: str = DOMAIN) -> None:
    """Write the plan of the check step: the versions of a provider to check, by channel."""
    plan = {
        channel: [{"domain": domain, "version": version} for version in versions]
        for channel, versions in channels.items()
    }
    files.write("work/plan.json", {"channels": plan})


def _write_result(files: Files, channel: str, result: dict[str, Any], name: str = "") -> Path:
    """Write the results file of one version where the check step puts it, or at a name."""
    name = name or f"{result['domain']}-{result['version']}.json"
    return files.write(f"results/{channel}/{name}", _results(channel, result))


def _apply(files: Files) -> int:
    """Run apply-results on the results folder of the fixture."""
    return _run(files, "apply-results", str(files.root / "results"))


def _index(files: Files, channel: str) -> dict[str, Any]:
    """Return the index file of a channel."""
    index: dict[str, Any] = json.loads((files.root / f"site/v1/{channel}.json").read_text())
    return index


def test_versions_to_check(files: Files) -> None:
    """Versions without a result and each latest are checked; nothing blocked or frozen."""
    files.write(
        "channels.json",
        {
            "channels": {
                "nightly": {"image": "ghcr.io/music-assistant/server:nightly"},
                "2.11": {"image": "ghcr.io/music-assistant/server:2.11", "frozen": True},
            }
        },
    )
    files.provider(blocked_versions={"1.3.0": "Sends the library elsewhere"})
    files.version("1.0.0", channels={"nightly": True})
    files.version("1.1.0", channels={"nightly": True})
    files.version("1.2.0", channels={"nightly": False})
    files.version("1.3.0", channels={})
    files.version("1.4.0", channels={})
    files.provider("blocked_music", status="blocked", reason="Sends the library elsewhere")
    files.version("1.0.0", domain="blocked_music", channels={})
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
    files.version("1.4.0", channels={"nightly": True})
    files.version("1.5.0", channels={"nightly": True})
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
    files.version("1.0.0", channels={"nightly": True})
    for version in ("1.1.0", "1.2.0", "1.3.0", "1.4.0", "1.5.0"):
        files.version(version, channels={"nightly": False})

    assert versions_to_check(load_registry(files.root), "nightly") == [
        (DOMAIN, "1.0.0"),
        (DOMAIN, "1.3.0"),
        (DOMAIN, "1.4.0"),
        (DOMAIN, "1.5.0"),
    ]


def test_apply_results(files: Files) -> None:
    """The results of every channel are recorded and the index files written."""
    files.provider()
    files.version("1.0.0", channels={})
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
    files.version("1.0.0", channels={"nightly": True})
    files.version("1.1.0", channels={"nightly": False})
    files.version("1.2.0", channels={"nightly": True})
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
        ({"channel": "nightly", "checked": CHECKED, "results": "all"}, '"results" must be a list'),
    ],
)
def test_a_result_that_is_not_valid_is_skipped(
    files: Files, capsys: pytest.CaptureFixture[str], content: dict[str, Any], problem: str
) -> None:
    """Results come from containers that ran provider code; one that is not valid is skipped."""
    files.provider()
    path = files.version("1.0.0", channels={})
    before = path.read_bytes()
    files.version("1.1.0", channels={})
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
    files.version("1.0.0", "aaa_evil", folder="aaa_evil", channels={"nightly": True})
    files.provider()
    victim = files.version("1.0.0", channels={"nightly": True})
    files.version("1.1.0", channels={"nightly": True})
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
    files.version("1.0.0", channels={})
    files.version("1.1.0", channels={})
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


def test_a_missing_result_and_a_frozen_channel(
    files: Files, capsys: pytest.CaptureFixture[str]
) -> None:
    """A version without a result keeps its results; a frozen channel takes none."""
    files.write(
        "channels.json",
        {
            "channels": {
                "nightly": {"image": "ghcr.io/music-assistant/server:nightly"},
                "2.11": {"image": "ghcr.io/music-assistant/server:2.11", "frozen": True},
            }
        },
    )
    files.provider()
    files.version("1.0.0", channels={})
    _plan(files, {"nightly": ["1.0.0"], "2.11": ["1.0.0"]})
    _write_result(files, "2.11", _result("1.0.0"))

    assert _apply(files) == 0

    err = capsys.readouterr().err
    assert f"{DOMAIN} 1.0.0 on nightly: no result" in err
    assert "'2.11': skipped, not a channel that is tested" in err
    assert load_registry(files.root).providers[DOMAIN].versions["1.0.0"].checks == {}


def test_a_check_that_could_not_run_removes_a_failure(files: Files) -> None:
    """A check that could not run removes a failed result of its version there, not a pass."""
    files.provider()
    files.version("1.0.0", channels={"beta": True, "nightly": True})
    files.version("1.1.0", channels={"beta": False, "nightly": True})
    assert _run(files, "build") == 0
    _plan(files, {"beta": ["1.0.0", "1.1.0"]})
    _write_result(files, "beta", _unavailable("1.0.0"))
    _write_result(files, "beta", _unavailable("1.1.0"))

    assert _apply(files) == 0

    versions = load_registry(files.root).providers[DOMAIN].versions
    assert (versions["1.0.0"].checks["beta"].passed, versions["1.0.0"].checks["beta"].checked) == (
        True,
        CHECKED,
    )
    assert set(versions["1.1.0"].checks) == {"nightly"}
    beta = _index(files, "beta")["providers"][DOMAIN]
    assert (beta["latest"], set(beta["versions"])) == ("1.0.0", {"1.0.0"})
    assert _run(files, "validate") == 0


def _fetched(*, refused: bool = False, owner_id: int = OWNER_ID) -> FetchReport:
    """Return the report of a fetch step that read the sample repository."""
    state = RepositoryState(
        owner="someone",
        repository_id=REPOSITORY_ID,
        owner_id=owner_id,
        private=False,
        archived=False,
        has_issues=True,
        license="mit",
    )
    return FetchReport.from_dict(
        {
            "repositories": {REPOSITORY: state.to_dict()},
            "refused": [{"domain": DOMAIN, "version": "2.0.0"}] if refused else [],
        }
    )


def _report(
    files: Files, *results: tuple[str, dict[str, Any]], fetched: FetchReport | None = None
) -> ProviderReport:
    """Return the report of the sample provider after one result per (channel, result)."""
    files_results = [
        ChannelResults.from_dict(_results(channel, result)) for channel, result in results
    ]
    [report] = build_reports(load_registry(files.root), fetched or _fetched(), files_results)
    return report


def _night(
    files: Files, github: FakeGitHub, *results: tuple[str, dict[str, Any]], **fetched: Any
) -> str | None:
    """Run the report and the posting of one night for the sample provider; return what was done."""
    report = _report(files, *results, fetched=_fetched(**fetched))
    client = GitHub("a-token", github)
    issue = open_issues(client, REGISTRY).get(DOMAIN)
    assert not isinstance(issue, OSError)
    return post_report(report, issue, client, REGISTRY)


def _refuse(files: Files, version: str, reason: str) -> None:
    """Record a refused release of the sample provider."""
    release = RefusedRelease(
        version=version,
        tag=f"v{version}",
        commit=COMMIT,
        released=RELEASED,
        refused=CHECKED,
        reason=reason,
    )
    refuse_release(load_registry(files.root), DOMAIN, release)


def _mentions(markdown: str) -> list[str]:
    """Return the handles a text mentions outside code and comments, as GitHub reads it."""
    text = re.sub(r"(?ms)^(`{3,})\n.*?^\1$", "", markdown)
    text = re.sub(r"(?s)(?<!`)(`+)(?!`).*?(?<!`)\1(?!`)", "", text)
    text = re.sub(r"(?s)<!--.*?-->", "", text)
    return re.findall(r"(?<![\w`])@([A-Za-z0-9-]+)", text)


def _issues(github: FakeGitHub) -> list[dict[str, Any]]:
    """Return the issues on the registry, by number."""
    return [
        issue for (owner, _), issue in sorted(github.issue_answers.items()) if owner == REGISTRY
    ]


def test_reports_for_failures_and_refusals(files: Files) -> None:
    """A refused release and each failed check are failures, their text of the provider fenced."""
    files.provider()
    files.version("1.0.0", channels={})
    files.version("1.1.0", channels={})
    _refuse(files, "2.0.0", "unknown key 'x'\n```\ninjected")

    report = _report(
        files,
        ("nightly", _result("1.0.0", "load: broken", output="Traceback\n````\nload: broken")),
        ("nightly", _result("1.1.0", "load: broken", passed_before=True)),
        fetched=_fetched(refused=True),
    )

    assert report.title == "Sample music (sample_music): release checks failed"
    assert [failure.key for failure in report.failures] == [
        "refused 2.0.0",
        "check 1.0.0 nightly",
        "check 1.1.0 nightly",
    ]
    refused, failed, stopped = report.failures
    assert "````\nunknown key 'x'\n```\ninjected\n````" in refused.text
    assert "not tried again" in refused.text
    assert failed.output == "Traceback\n````\nload: broken"
    assert "It passed before" in stopped.text
    assert (report.newest, report.tested, report.passed, report.admitted) == (
        "2.0.0",
        ["beta", "nightly"],
        [],
        True,
    )
    assert report.maintainers == ["someone"]


def test_a_repository_that_is_not_the_admitted_one(files: Files) -> None:
    """Another repository under the name of a provider is a failure until GitHub gives it again."""
    files.provider()
    files.version("1.0.0", channels={"beta": True})

    report = _report(files, fetched=_fetched(owner_id=999))

    [failure] = report.failures
    assert failure.key == f"repository {REPOSITORY_ID} 999"
    assert "Report or change a provider" in failure.text
    assert not report.admitted
    assert is_settled(_report(files), failure.key)


@pytest.mark.parametrize(
    ("checks", "refused", "key", "settled"),
    [
        # the newest version passed on the channel of the failure
        (
            {"1.0.0": {"nightly": False}, "1.1.0": {"nightly": True}},
            None,
            "check 1.0.0 nightly",
            True,
        ),
        # it passed on another channel only
        (
            {"1.0.0": {"nightly": False}, "1.1.0": {"beta": True}},
            None,
            "check 1.0.0 nightly",
            False,
        ),
        # a version that failed and passed again
        ({"1.0.0": {"nightly": True}}, None, "check 1.0.0 nightly", True),
        # a channel that is no longer tested, while the newest version passes on another one
        ({"1.0.0": {"beta": True}}, None, "check 1.0.0 2.11", True),
        ({"1.0.0": {}}, None, "check 1.0.0 2.11", False),
        # a refused release, settled by a higher version that passed on a channel
        ({"2.1.0": {"beta": True}}, "2.0.0", "refused 2.0.0", True),
        ({"2.1.0": {"beta": False}}, "2.0.0", "refused 2.0.0", False),
        # a newest release that was refused settles nothing
        ({"1.0.0": {"nightly": True, "beta": True}}, "2.0.0", "check 0.9.0 nightly", False),
        ({"1.0.0": {"nightly": True}}, None, "repository 1 2", True),
        ({"1.0.0": {"nightly": True}}, None, "unknown", False),
    ],
)
def test_when_a_failure_is_settled(
    files: Files,
    checks: dict[str, dict[str, bool]],
    refused: str | None,
    key: str,
    settled: bool,
) -> None:
    """A failure is settled once the newest version passed where it failed, or anywhere at all."""
    files.provider()
    for version, channels in checks.items():
        files.version(version, channels=channels)
    if refused is not None:
        _refuse(files, refused, "refused")

    assert is_settled(_report(files), key) is settled


def test_the_report_leaves_out_what_is_settled(files: Files) -> None:
    """A failure of an older version on a channel where the newest passes needs nobody."""
    files.provider()
    files.version("1.0.0", channels={})
    files.version("1.1.0", channels={"nightly": True})

    report = _report(files, ("nightly", _result("1.0.0", "load: x")))

    assert report.failures == []


def test_a_first_failure_opens_one_issue(files: Files) -> None:
    """The first failure opens one labelled issue on the registry that mentions the maintainers."""
    files.provider()
    files.version("1.0.0", channels={})
    github = FakeGitHub()

    done = _night(files, github, ("nightly", _result("1.0.0", "load: broken")))

    [(repository, created)] = github.created
    assert (repository, done) == (REGISTRY, f"https://github.com/{REGISTRY}/issues/1")
    assert created["title"] == "Sample music (sample_music): release checks failed"
    assert created["labels"] == [LABEL]
    body = created["body"]
    assert body.startswith("<!-- release-check: sample_music; check 1.0.0 nightly -->\n")
    assert "### Version 1.0.0 does not pass the checks on the nightly channel" in body
    assert "```\nload: broken\n```" in body
    assert "#releasing-updates" in body
    assert "closes by itself" in body
    assert _mentions(body) == ["someone"]
    # the same failure the next night is told no more
    assert _night(files, github, ("nightly", _result("1.0.0", "load: broken"))) is None
    assert len(github.created) == 1
    assert github.comments.get((REGISTRY, 1), []) == []


def test_a_new_issue_mentions_at_most_ten(files: Files) -> None:
    """The owner and the code owners of the newest version that passed: at most ten of them."""
    owners = [f"@owner-{number}" for number in range(10)]
    files.provider()
    files.version(
        "1.0.0",
        channels={"beta": True},
        manifest={**manifest(requirements=["some-package==1.0"]), "codeowners": owners},
    )
    github = FakeGitHub()

    _night(files, github, ("nightly", _result("1.0.0", "load: broken")))

    [(_, created)] = github.created
    assert _mentions(created["body"]) == ["someone", *(f"owner-{n}" for n in range(9))]


def test_a_new_failure_while_open_is_a_comment(files: Files) -> None:
    """Another failure while the issue is open is one comment, without mentions, told once."""
    files.provider()
    files.version("1.0.0", channels={})
    github = FakeGitHub()
    _night(files, github, ("nightly", _result("1.0.0", "load: broken")))
    files.version("1.0.0", channels={"nightly": False})
    files.version("1.1.0", channels={})

    done = _night(files, github, ("beta", _result("1.1.0", "load: other")))
    again = _night(files, github, ("beta", _result("1.1.0", "load: other")))

    assert (done, again) == ("#1: 1 new failure(s)", None)
    assert len(github.created) == 1
    [comment] = github.comments[REGISTRY, 1]
    assert comment["body"].startswith("<!-- release-check: sample_music; check 1.1.0 beta -->\n")
    assert "### Version 1.1.0 does not pass the checks on the beta channel" in comment["body"]
    assert _mentions(comment["body"]) == []


def test_a_passing_release_closes_the_issue(files: Files) -> None:
    """When the newest version passed where the issue says it failed, it is closed, once."""
    files.provider()
    files.version("1.0.0", channels={})
    github = FakeGitHub()
    _night(files, github, ("nightly", _result("1.0.0", "load: broken")))
    files.version("1.0.0", channels={"nightly": False})
    files.version("1.1.0", channels={})

    done = _night(files, github, ("nightly", _result("1.1.0")))

    assert done == "#1: closed"
    [issue] = _issues(github)
    assert (issue["state"], issue["state_reason"]) == ("closed", "completed")
    [comment] = github.comments[REGISTRY, 1]
    assert comment["body"] == "Version 1.1.0 passed the checks, so this issue is closed."
    # a failure after that opens a new issue
    files.version("1.1.0", channels={"nightly": True})
    files.version("1.2.0", channels={})
    assert _night(files, github, ("nightly", _result("1.2.0", "load: again"))) == (
        f"https://github.com/{REGISTRY}/issues/2"
    )
    assert [issue["state"] for issue in _issues(github)] == ["closed", "open"]


def test_an_issue_stays_open_until_every_channel_passes(files: Files) -> None:
    """A newest version that passes on one channel of the issue only leaves it open."""
    files.provider()
    files.version("1.0.0", channels={})
    github = FakeGitHub()
    _night(files, github, ("nightly", _result("1.0.0", "load: x")), ("beta", _result("1.0.0", "y")))
    files.version("1.0.0", channels={"nightly": False, "beta": False})
    files.version("1.1.0", channels={"nightly": True})

    assert _night(files, github, ("beta", _result("1.1.0", "y"))) == "#1: 1 new failure(s)"
    files.version("1.1.0", channels={"nightly": True, "beta": False})
    assert _night(files, github) is None
    assert _issues(github)[0]["state"] == "open"


def test_a_key_written_by_someone_else_does_not_count(files: Files) -> None:
    """Only the issue and comments of the pipeline's account count, and only their first line."""
    files.provider()
    files.version("1.0.0", channels={})
    github = FakeGitHub()
    marker = "<!-- release-check: sample_music; check 1.0.0 nightly; check 1.0.0 beta -->"
    # an issue of a stranger with the label, and one of the pipeline that quotes a marker
    github.issue(REGISTRY, 1, f"{marker}\n", labels=[{"name": LABEL}])
    github.issue(
        REGISTRY,
        2,
        "<!-- release-check: sample_music; refused 0.1.0 -->\n```\n" + marker + "\n```\n",
        labels=[{"name": LABEL}],
        user={"login": "github-actions[bot]"},
    )
    github.comments[REGISTRY, 2] = [{"id": 7, "user": {"login": "a-stranger"}, "body": marker}]

    done = _night(files, github, ("nightly", _result("1.0.0", "load: x")))

    assert done == "#2: 1 new failure(s)"
    assert github.created == []


def test_text_of_a_provider_mentions_nobody(files: Files) -> None:
    """A mention in a manifest name, a reason or an output stays code and leaves the title."""
    evil = {**manifest(DOMAIN, ["some-package==1.0"]), "name": "@evil `` @evil", "codeowners": [
        "@someone", "@another"]}  # fmt: skip
    files.provider()
    files.version("1.0.0", channels={}, manifest=evil)
    _refuse(files, "2.0.0", "@evil ```` @evil\n@evil")
    github = FakeGitHub()

    _night(
        files,
        github,
        ("nightly", _result("1.0.0", "load: @evil", output="````\n@evil\n`` @evil")),
        refused=True,
    )

    [(_, created)] = github.created
    assert created["title"] == "sample_music: release checks failed"
    # the owner alone: no version passed, so the code owners of a manifest do not count
    assert _mentions(created["body"]) == ["someone"]
    # the reason of the refusal (3), of the check (1) and the output (2), all as code
    assert created["body"].count("@evil") == 6


def test_the_output_in_a_post_fits_github(files: Files) -> None:
    """GitHub takes 65536 characters in an issue: the failures share the end of their output."""
    files.provider()
    files.version("1.0.0", channels={})
    output = "start" + "x" * 60_000 + "load: the end"
    github = FakeGitHub()

    _night(
        files,
        github,
        ("nightly", _result("1.0.0", "load: x", output=output)),
        ("beta", _result("1.0.0", "load: x", output=output)),
    )

    [(_, created)] = github.created
    assert len(created["body"]) < 45_000
    assert created["body"].count("load: the end\n") == 2
    assert "start" not in created["body"]


@pytest.mark.parametrize(
    "output",
    ["`" * 40_000, "~" * 60_000, "\U0001f600" * 40_000],
    ids=["backticks", "tildes", "astral"],
)
@pytest.mark.parametrize("open_issue", [False, True], ids=["issue", "comment"])
def test_a_post_fits_github_whatever_the_output(
    files: Files,
    output: str,
    open_issue: bool,
) -> None:
    """The whole text of an issue or comment stays within 65536 characters, fences and all."""
    files.provider()
    files.version("1.0.0", channels={})
    github = FakeGitHub()
    if open_issue:
        github.issue(
            REGISTRY,
            1,
            "<!-- release-check: sample_music; refused 0.1.0 -->\n",
            labels=[{"name": LABEL}],
            user={"login": "github-actions[bot]"},
        )

    _night(files, github, ("nightly", _result("1.0.0", "load: x", output=output + "end")))

    if open_issue:
        [comment] = github.comments[REGISTRY, 1]
        body = comment["body"]
    else:
        [(_, created)] = github.created
        body = created["body"]
    assert len(body.encode("utf-16-le")) // 2 <= 65_536
    assert body.startswith("<!-- release-check: sample_music; check 1.0.0 nightly -->\n")
    found = re.search(r"Output, shortened to its last (\d+) characters", body)
    assert found is not None
    shown = (output + "end")[-int(found[1]) :]
    assert shown.endswith("end")
    assert code_block(shown) in body


def test_the_largest_comments_are_read() -> None:
    """A full page of comments of 65536 characters, each escaped in JSON, fits an answer."""
    github = FakeGitHub()
    body = "\x01" * 65_536
    github.comments[REGISTRY, 1] = [
        {"id": number, "user": {"login": "github-actions[bot]"}, "body": body}
        for number in range(100)
    ]

    comments = GitHub("a-token", github).comments(REGISTRY, 1)

    assert len(comments) == 100


def test_a_provider_whose_issue_cannot_be_read_is_skipped(
    files: Files, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The others are still reported, the skipped one gets no second issue, and the run fails."""
    github = FakeGitHub()
    for domain in ("other_music", DOMAIN, "third_music"):
        files.provider(domain)
        files.version("1.0.0", domain=domain, channels={})
    results = [
        ChannelResults.from_dict(
            _results(
                "nightly",
                *({**_result("1.0.0", "x"), "domain": d} for d in ("other_music", "third_music")),
                _result("1.0.0", "x"),
            )
        )
    ]
    for report in build_reports(load_registry(files.root), _fetched(), results):
        files.write(f"work/issues/{report.domain}.json", report.to_dict())
    # the comments of the open issue of sample_music cannot be read
    github.issue(
        REGISTRY,
        1,
        "<!-- release-check: sample_music; refused 0.1.0 -->\n",
        labels=[{"name": LABEL}],
        user={"login": "github-actions[bot]"},
    )
    comments = f"https://api.github.com/repos/{REGISTRY}/issues/1/comments?per_page={COMMENTS_PER_PAGE}&page=1"
    github.broken[comments] = OSError("too large")
    monkeypatch.setenv("GITHUB_TOKEN", "a-token")
    monkeypatch.setattr("registry.cli.GitHub", lambda token: GitHub(token, github))

    assert _run(files, "post-issues", "--repository", REGISTRY) == 1

    assert [data["title"] for _, data in github.created] == [
        "Sample music (other_music): release checks failed",
        "Sample music (third_music): release checks failed",
    ]
    assert "warning: sample_music: skipped, its issue cannot be read" in capsys.readouterr().err


def test_report_writes_the_reports(files: Files) -> None:
    """The report command writes the report of each provider, only from results of the plan."""
    files.provider()
    files.version("1.0.0", channels={})
    _plan(files, {"nightly": ["1.0.0"]})
    _write_result(files, "nightly", _result("1.0.0", "load: x"))
    # a result the plan does not name is not reported
    _write_result(files, "nightly", _result("1.0.0", "load: forged"), name="other.json")
    files.write("work/fetch.json", _fetched().to_dict())

    assert _run(files, "report", str(files.root / "results")) == 0

    [path] = (files.root / "work" / "issues").glob("*.json")
    report = load_model(path, ProviderReport)
    assert path.name == f"{DOMAIN}.json"
    assert [failure.text.count("load: x") for failure in report.failures] == [1]


def test_post_issues_goes_on_after_a_refusal(
    files: Files, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A provider whose issue GitHub refuses is reported, and the others are posted all the same."""
    github = FakeGitHub()
    files.provider()
    files.provider("other_music")
    files.version("1.0.0", channels={})
    files.version("1.0.0", domain="other_music", channels={})
    results = [
        ChannelResults.from_dict(
            _results(
                "nightly", _result("1.0.0", "x"), {**_result("1.0.0", "x"), "domain": "other_music"}
            )
        )
    ]
    for report in build_reports(load_registry(files.root), _fetched(), results):
        files.write(f"work/issues/{report.domain}.json", report.to_dict())
    # other_music has an open issue, on which GitHub refuses the comment
    github.issue(
        REGISTRY,
        1,
        "<!-- release-check: other_music; refused 0.1.0 -->\n",
        labels=[{"name": LABEL}],
        user={"login": "github-actions[bot]"},
    )
    github.broken[f"https://api.github.com/repos/{REGISTRY}/issues/1/comments"] = OSError("no")
    monkeypatch.setenv("GITHUB_TOKEN", "a-token")
    monkeypatch.setattr("registry.cli.GitHub", lambda token: GitHub(token, github))

    assert _run(files, "post-issues", "--repository", REGISTRY) == 1

    assert [data["title"] for _, data in github.created] == [
        "Sample music (sample_music): release checks failed"
    ]
    assert "warning: other_music: no" in capsys.readouterr().err
    assert all(r.get_header("Authorization") == "Bearer a-token" for r in github.requests)


def _channel_night(
    files: Files, github: FakeGitHub, *results: tuple[str, dict[str, Any]]
) -> str | None:
    """Run the report and the posting of one night for the one channel of the results."""
    files_results = [
        ChannelResults.from_dict(_results(channel, result)) for channel, result in results
    ]
    [report] = build_channel_reports(load_registry(files.root), files_results)
    client = GitHub("a-token", github)
    issue = open_channel_issues(client, REGISTRY).get(report.channel)
    return post_channel_report(report, issue, client, REGISTRY)


def test_a_check_that_could_not_run_fails_no_provider(files: Files) -> None:
    """The issue of a provider about a channel whose checks could not run closes, and none opens."""
    files.provider()
    files.version("1.0.0", channels={"nightly": True, "beta": False})
    github = FakeGitHub()
    github.issue(
        REGISTRY,
        1,
        "<!-- release-check: sample_music; check 1.0.0 beta -->\n",
        labels=[{"name": LABEL}],
        user={"login": "github-actions[bot]"},
    )
    results = (("beta", _unavailable("1.0.0")), ("nightly", _result("1.0.0")))

    report = _report(files, *results)
    done = _night(files, github, *results)

    assert (report.failures, report.tested, report.passed) == ([], ["nightly"], ["nightly"])
    assert done == "#1: closed"
    assert github.created == []
    [comment] = github.comments[REGISTRY, 1]
    assert comment["body"] == "Version 1.0.0 passed the checks, so this issue is closed."


def test_a_channel_whose_checks_cannot_run_has_one_issue(files: Files) -> None:
    """The maintainers get one issue per channel, kept up to date, closed once the checks run."""
    files.provider()
    files.version("1.0.0", channels={})
    github = FakeGitHub()
    # an issue of a stranger with the label and the first line of the pipeline does not count
    github.issue(REGISTRY, 1, "<!-- release-check channel: beta -->\n", labels=[{"name": LABEL}])

    opened = _channel_night(files, github, ("beta", _unavailable("1.0.0")))
    again = _channel_night(files, github, ("beta", _unavailable("1.0.0")))

    assert (opened, again) == (f"https://github.com/{REGISTRY}/issues/2", None)
    [(_, created)] = github.created
    assert created["title"] == "The release checks cannot run on the beta channel"
    assert created["labels"] == [LABEL]
    body = created["body"]
    assert body.startswith("<!-- release-check channel: beta -->\n")
    assert f"```\n{UNAVAILABLE}\n```" in body
    assert "ImportError: cannot import name 'x'" in body
    assert "closes by itself" in body
    assert _mentions(body) == []
    # another reason replaces the text of the issue
    other = _result("1.0.0", "load: other", unavailable=True)
    assert _channel_night(files, github, ("beta", other)) == "#2: updated"
    assert "```\nload: other\n```" in _issues(github)[1]["body"]
    # once the checks run, failed or not, the issue is closed, once
    assert _channel_night(files, github, ("beta", _result("1.0.0", "load: x"))) == "#2: closed"
    assert _channel_night(files, github, ("beta", _result("1.0.0", "load: x"))) is None
    assert [issue["state"] for issue in _issues(github)] == ["open", "closed"]
    [comment] = github.comments[REGISTRY, 2]
    assert comment["body"] == "The checks run on the beta channel again, so this issue is closed."
    assert len(github.created) == 1


def test_an_open_channel_issue_with_the_line_marker_is_updated(files: Files) -> None:
    """An open issue whose first line names the channel with "line" gets the new text, once."""
    files.provider()
    files.version("1.0.0", channels={})
    github = FakeGitHub()
    github.issue(
        REGISTRY,
        1,
        "<!-- release-check line: beta -->\nThe checks cannot run on the `beta` line.\n",
        user={"login": "github-actions[bot]"},
        labels=[{"name": LABEL}],
    )

    assert _channel_night(files, github, ("beta", _unavailable("1.0.0"))) == "#1: updated"
    assert _channel_night(files, github, ("beta", _unavailable("1.0.0"))) is None

    assert github.created == []
    [issue] = _issues(github)
    assert issue["body"].startswith("<!-- release-check channel: beta -->\n")
    assert "cannot check releases on the beta channel." in issue["body"]


def test_report_and_post_issues_for_a_channel_whose_checks_cannot_run(
    files: Files, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The report of each channel with results is written, and only the maintainers get an issue."""
    files.provider()
    files.version("1.0.0", channels={})
    _plan(files, {"beta": ["1.0.0"], "nightly": ["1.0.0"]})
    _write_result(files, "beta", _unavailable("1.0.0"))
    _write_result(files, "nightly", _result("1.0.0"))
    files.write("work/fetch.json", _fetched().to_dict())
    github = FakeGitHub()
    monkeypatch.setenv("GITHUB_TOKEN", "a-token")
    monkeypatch.setattr("registry.cli.GitHub", lambda token: GitHub(token, github))

    assert _run(files, "report", str(files.root / "results")) == 0
    assert _run(files, "post-issues", "--repository", REGISTRY) == 0

    channels = files.root / "work" / "issues" / "channels"
    assert sorted(path.name for path in channels.iterdir()) == ["beta.json", "nightly.json"]
    assert [data["title"] for _, data in github.created] == [
        "The release checks cannot run on the beta channel"
    ]
