"""Tests for the admission checks of a pull request that adds or changes a provider."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest

from registry.admission import check_admission, prepare_admission
from registry.edit import CommandError
from registry.github import GitHub
from registry.models import AdmissionReport, LineResults
from registry.registry import load_registry
from tests.conftest import DOMAIN, FakeGitHub, provider_archive, provider_file, release

if TYPE_CHECKING:
    from pathlib import Path

    from tests.conftest import Files

REPOSITORY = "someone/ma-sample"
COMMIT = "1" * 40


@pytest.fixture
def github() -> FakeGitHub:
    """Return a GitHub with the sample repository and one release of its provider."""
    github = FakeGitHub()
    github.repository(REPOSITORY, [release("v1.0.0", COMMIT)])
    github.archives[REPOSITORY, COMMIT] = provider_archive()
    return github


def _pull_request(tmp_path: Path, domain: str = DOMAIN, **changes: Any) -> Path:
    """Return a checkout of a pull request that adds a provider.json."""
    folder = tmp_path / "pull-request"
    path = folder / "providers" / domain / "provider.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(provider_file(domain, **changes)))
    return folder


def _prepare(
    files: Files,
    github: FakeGitHub,
    pull_request: Path,
    domain: str = DOMAIN,
    author: str = "someone",
) -> AdmissionReport:
    """Run the prepare step of the admission."""
    return prepare_admission(
        files.root,
        pull_request,
        domain,
        author=author,
        github=GitHub("a-token", github),
        work=files.root / "work",
    )


def _failed(report: AdmissionReport) -> list[str]:
    """Return the checks that failed."""
    return [finding.text for finding in report.findings if not finding.passed]


def _results(line: str, passed: bool, version: str = "1.0.0") -> LineResults:
    """Return the results file of a line with one result for the sample provider."""
    reason = None if passed else "load: ModuleNotFoundError: No module named 'x'"
    return LineResults.from_dict(
        {
            "line": line,
            "checked": "2026-11-15T03:00:00Z",
            "results": [{"domain": DOMAIN, "version": version, "passed": passed, "reason": reason}],
        }
    )


def test_a_provider_that_passes(files: Files, github: FakeGitHub, tmp_path: Path) -> None:
    """Every check passes, and the latest release is a version file ready for the check step."""
    report = _prepare(files, github, _pull_request(tmp_path))

    assert _failed(report) == []
    assert report.version == "1.0.0"
    assert [finding.text for finding in report.findings] == [
        "The domain sample_music is free",
        "provider.json is valid",
        "The repository someone/ma-sample is public and not archived",
        "A licence GitHub recognises: mit",
        "The latest release v1.0.0 is version 1.0.0",
        (
            "The author someone owns someone/ma-sample or is a code owner in the manifest of the "
            "latest release"
        ),
    ]
    registry = load_registry(files.root)
    assert set(registry.providers[DOMAIN].versions) == {"1.0.0"}
    assert (files.root / "work" / "archives" / DOMAIN / "1.0.0.tar.gz").is_file()

    passed, text = check_admission(
        registry, report, [_results("nightly", passed=True), _results("beta", passed=False)]
    )

    assert passed
    assert "- ✅ nightly: passed" in text
    assert "- ❌ beta: load: ModuleNotFoundError: No module named 'x'" in text
    assert text.endswith("A person decides whether what the provider does is acceptable.\n")


def test_the_release_must_pass_on_a_line(files: Files, github: FakeGitHub, tmp_path: Path) -> None:
    """A release that passes on no line, or was not checked, fails the admission."""
    report = _prepare(files, github, _pull_request(tmp_path))
    registry = load_registry(files.root)

    failed, failed_text = check_admission(registry, report, [_results("nightly", passed=False)])
    other, _ = check_admission(
        registry, report, [_results("nightly", passed=True, version="0.1.0")]
    )

    assert not failed
    assert not other
    assert "- ❌ beta: the checks did not run" in failed_text
    assert failed_text.endswith("Not every check passed.\n")


@pytest.mark.parametrize(
    ("node", "problem"),
    [
        ({"isArchived": True}, "The repository someone/ma-sample is public and archived"),
        ({"licenseInfo": None}, "A licence GitHub recognises: none"),
        ({"licenseInfo": {"key": "other"}}, "A licence GitHub recognises: other"),
        (
            {"owner": {"login": "an-organisation"}},
            "The author someone owns someone/ma-sample or is a code owner",
        ),
        ({"releases": {"nodes": []}}, "There is no release with a tag such as v1.0.0"),
    ],
)
def test_the_repository_must_qualify(
    files: Files, github: FakeGitHub, tmp_path: Path, node: dict[str, Any], problem: str
) -> None:
    """Archived, without a licence, not the author's, or without a release fails."""
    github.repositories[REPOSITORY].update(node)
    if "owner" in node:
        # the manifest names another code owner
        github.archives[REPOSITORY, COMMIT] = provider_archive(codeowners=["@another"])

    [failed] = _failed(_prepare(files, github, _pull_request(tmp_path)))

    assert failed.startswith(problem)


def test_a_code_owner_of_an_organisation_repository(
    files: Files, github: FakeGitHub, tmp_path: Path
) -> None:
    """The author may be a code owner in the manifest of the latest release instead of the owner."""
    github.repositories[REPOSITORY]["owner"] = {"login": "an-organisation"}
    github.archives[REPOSITORY, COMMIT] = provider_archive(codeowners=["@Another"])

    report = _prepare(files, github, _pull_request(tmp_path), author="another")

    assert _failed(report) == []


def test_a_repository_that_is_not_public(files: Files, tmp_path: Path) -> None:
    """GitHub answers nothing for a private repository."""
    report = _prepare(files, FakeGitHub(), _pull_request(tmp_path))

    assert _failed(report) == ["The repository someone/ma-sample does not exist or is not public"]
    assert report.version is None


def test_a_refused_release(files: Files, github: FakeGitHub, tmp_path: Path) -> None:
    """A latest release that cannot become a version fails the admission with the reason."""
    github.archives[REPOSITORY, COMMIT] = provider_archive(type="core")

    report = _prepare(files, github, _pull_request(tmp_path))

    [failed] = _failed(report)
    assert failed.startswith("The latest release v1.0.0 is refused: ")
    assert "manifest.type: 'core'" in failed
    assert report.version is None


def test_a_reserved_domain(files: Files, github: FakeGitHub, tmp_path: Path) -> None:
    """A domain the server ships cannot be taken; the rules of the base branch decide."""
    pull_request = _pull_request(tmp_path, "builtin_one", folder="builtin_one")
    # the pull request cannot free the domain itself
    (pull_request / "reserved_domains.json").write_text("{}")

    report = _prepare(files, github, pull_request, "builtin_one")

    assert _failed(report) == [
        "The domain builtin_one is a provider the server ships",
        "providers/builtin_one/provider.json: domain: builtin_one is a provider the server ships",
    ]


def test_a_change_of_an_existing_provider(files: Files, github: FakeGitHub, tmp_path: Path) -> None:
    """A new repository for an existing provider gets the same checks."""
    files.provider()
    files.version("1.0.0")
    github.repository("someone/new-home", [release("v1.0.0", COMMIT)])
    github.archives["someone/new-home", COMMIT] = provider_archive()

    report = _prepare(files, github, _pull_request(tmp_path, repository="someone/new-home"))

    assert report.needed
    assert report.findings[0].text == "The pull request changes the existing provider sample_music"
    assert _failed(report) == []
    # the latest release is a version already: the results its version file records count
    passed, text = check_admission(load_registry(files.root), report, [])
    assert passed
    assert "- ✅ nightly: passed" in text


def test_only_a_status_change_needs_no_admission(
    files: Files, github: FakeGitHub, tmp_path: Path
) -> None:
    """Withdrawing, blocking or blocking a version is no admission."""
    files.provider()
    files.version("1.0.0")
    pull_request = _pull_request(
        tmp_path, status="blocked", reason="Sends the library elsewhere",
        blocked_versions={"1.0.0": "Sends the library elsewhere"},
    )  # fmt: skip

    report = _prepare(files, github, pull_request, author="a-maintainer")
    passed, text = check_admission(load_registry(files.root), report, [])

    assert not report.needed
    assert passed
    assert "Nothing to admit." in text
    assert github.requests == []


def test_a_provider_file_behind_a_link_is_refused(
    files: Files, github: FakeGitHub, tmp_path: Path
) -> None:
    """The provider.json of a pull request is read only when it is a file of the checkout."""
    pull_request = _pull_request(tmp_path)
    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps(provider_file()))
    path = pull_request / "providers" / DOMAIN / "provider.json"
    path.unlink()
    path.symlink_to(outside)

    with pytest.raises(CommandError, match="lies behind a symbolic link"):
        _prepare(files, github, pull_request)


def test_a_provider_file_that_is_not_valid(
    files: Files, github: FakeGitHub, tmp_path: Path
) -> None:
    """A provider.json that cannot be read is reported, and nothing is asked of GitHub."""
    report = _prepare(files, github, _pull_request(tmp_path, maintainers="someone"))

    [failed] = _failed(report)
    assert failed == 'provider.json: Field "maintainers" must be a list'
    assert github.requests == []
