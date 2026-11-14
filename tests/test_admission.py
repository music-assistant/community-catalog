"""Tests for the admission checks of a pull request that adds or changes a provider."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest

from registry.admission import check_admission, prepare_admission
from registry.edit import CommandError
from registry.files import load_model
from registry.github import GitHub
from registry.models import AdmissionReport, CheckPlan, LineResults
from registry.pipeline import PLAN_FILE
from registry.registry import load_registry
from tests.conftest import (
    DOMAIN,
    OWNER_ID,
    REPOSITORY_ID,
    FakeGitHub,
    provider_archive,
    provider_file,
    release,
)

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
    """Run the prepare step of the admission for a pull request that changes one provider.json."""
    return prepare_admission(
        files.root,
        pull_request,
        [f"providers/{domain}/provider.json"],
        author=author,
        github=GitHub("a-token", github),
        work=files.root / "work",
    )


def _failed(report: AdmissionReport) -> list[str]:
    """Return the checks that failed, each with its detail."""
    return [
        finding.text + (f": {finding.detail}" if finding.detail is not None else "")
        for finding in report.findings
        if not finding.passed
    ]


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
    assert [(finding.text, finding.detail) for finding in report.findings] == [
        ("The domain is free", DOMAIN),
        ("provider.json is valid", None),
        ("The repository is public and not archived", "someone/ma-sample"),
        (
            "provider.json has the ids GitHub gives the repository and its owner",
            f"repository_id {REPOSITORY_ID}, owner_id {OWNER_ID}",
        ),
        ("A licence GitHub recognises", "mit"),
        ("The latest release is version 1.0.0", "v1.0.0"),
        (
            (
                "The author owns the repository or is a code owner in the manifest of the "
                "latest release"
            ),
            "someone",
        ),
        (
            (
                "Every maintainer in provider.json owns the repository or is a code owner in "
                "the manifest of the latest release"
            ),
            None,
        ),
    ]
    registry = load_registry(files.root)
    assert set(registry.providers[DOMAIN].versions) == {"1.0.0"}
    assert (files.root / "work" / "archives" / DOMAIN / "1.0.0.tar.gz").is_file()
    plan = load_model(files.root / "work" / PLAN_FILE, CheckPlan)
    assert {line: [(e.domain, e.version) for e in v] for line, v in plan.lines.items()} == {
        "beta": [(DOMAIN, "1.0.0")],
        "nightly": [(DOMAIN, "1.0.0")],
    }

    passed, text = check_admission(
        registry, report, [_results("nightly", passed=True), _results("beta", passed=False)]
    )

    assert passed
    assert "- ✅ nightly: passed" in text
    assert "- ❌ beta: failed: ` load: ModuleNotFoundError: No module named 'x' `" in text
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
    assert "- ❌ nightly: failed:" in failed_text
    assert failed_text.endswith("Not every check passed.\n")


@pytest.mark.parametrize(
    ("node", "problem"),
    [
        ({"isArchived": True}, "The repository is public and archived: someone/ma-sample"),
        ({"licenseInfo": None}, "A licence GitHub recognises: none"),
        ({"licenseInfo": {"key": "other"}}, "A licence GitHub recognises: other"),
        ({"releases": {"nodes": []}}, "There is no release with a tag such as this one: v1.0.0"),
        (
            {"databaseId": 999},
            (
                "provider.json has the ids GitHub gives the repository and its owner: "
                f"repository_id 999, owner_id {OWNER_ID}"
            ),
        ),
        (
            {"owner": {"login": "someone", "databaseId": 998}},
            (
                "provider.json has the ids GitHub gives the repository and its owner: "
                f"repository_id {REPOSITORY_ID}, owner_id 998"
            ),
        ),
    ],
)
def test_the_repository_must_qualify(
    files: Files, github: FakeGitHub, tmp_path: Path, node: dict[str, Any], problem: str
) -> None:
    """Archived, without a licence, without a release, or another repository fails."""
    github.repositories[REPOSITORY].update(node)

    assert _failed(_prepare(files, github, _pull_request(tmp_path))) == [problem]


def test_the_author_and_the_maintainers_must_own_the_provider(
    files: Files, github: FakeGitHub, tmp_path: Path
) -> None:
    """The author and every maintainer own the repository or are a code owner in the manifest."""
    github.repositories[REPOSITORY]["owner"] = {"login": "an-organisation", "databaseId": OWNER_ID}
    github.archives[REPOSITORY, COMMIT] = provider_archive(codeowners=["@another"])
    pull_request = _pull_request(tmp_path, maintainers=["another", "a-stranger", "someone"])

    report = _prepare(files, github, pull_request)

    assert _failed(report) == [
        (
            "The author owns the repository or is a code owner in the manifest of the latest "
            "release: someone"
        ),
        (
            "Every maintainer in provider.json owns the repository or is a code owner in the "
            "manifest of the latest release: a-stranger, someone"
        ),
    ]


def test_a_code_owner_of_an_organisation_repository(
    files: Files, github: FakeGitHub, tmp_path: Path
) -> None:
    """The author may be a code owner in the manifest of the latest release instead of the owner."""
    github.repositories[REPOSITORY]["owner"] = {"login": "an-organisation", "databaseId": OWNER_ID}
    github.archives[REPOSITORY, COMMIT] = provider_archive(codeowners=["@Another"])
    pull_request = _pull_request(tmp_path, maintainers=["another"])

    report = _prepare(files, github, pull_request, author="another")

    assert _failed(report) == []


def test_a_repository_that_is_not_public(files: Files, tmp_path: Path) -> None:
    """GitHub answers nothing for a private repository."""
    report = _prepare(files, FakeGitHub(), _pull_request(tmp_path))

    assert _failed(report) == ["The repository does not exist or is not public: someone/ma-sample"]
    assert report.version is None
    assert load_model(files.root / "work" / PLAN_FILE, CheckPlan).lines == {}


def test_a_refused_release(files: Files, github: FakeGitHub, tmp_path: Path) -> None:
    """A latest release that cannot become a version fails the admission with the reason."""
    github.archives[REPOSITORY, COMMIT] = provider_archive(type="core")

    report = _prepare(files, github, _pull_request(tmp_path))

    [failed] = _failed(report)
    assert failed.startswith("The latest release, version 1.0.0, is refused: ")
    assert "manifest.type: 'core'" in failed
    assert report.version is None


def test_a_reserved_domain(files: Files, github: FakeGitHub, tmp_path: Path) -> None:
    """A domain the server ships cannot be taken; the rules of the base branch decide."""
    pull_request = _pull_request(tmp_path, "builtin_one", folder="builtin_one")
    # the pull request cannot free the domain itself
    (pull_request / "reserved_domains.json").write_text("{}")

    report = _prepare(files, github, pull_request, "builtin_one")

    assert _failed(report) == [
        "The domain is a provider the server ships: builtin_one",
        (
            "Not valid: providers/builtin_one/provider.json: domain: builtin_one is a provider "
            "the server ships"
        ),
    ]


def test_a_change_of_an_existing_provider(files: Files, github: FakeGitHub, tmp_path: Path) -> None:
    """A new folder for an existing provider gets the same checks."""
    files.provider(folder=DOMAIN)
    files.version("1.0.0", folder=DOMAIN)

    report = _prepare(files, github, _pull_request(tmp_path))

    assert report.maintainer_keys == []
    assert report.findings[0].text == "The pull request changes an existing provider"
    assert _failed(report) == []
    # the latest release is a version already: the results its version file records count
    passed, text = check_admission(load_registry(files.root), report, [])
    assert passed
    assert "- ✅ nightly: passed" in text


@pytest.mark.parametrize(
    "changes",
    [
        {"status": "blocked", "reason": "Sends the library elsewhere"},
        {"blocked_versions": {"1.0.0": "Sends the library elsewhere"}},
        {"repository": "someone/new-home"},
        {"repository_id": 102},
        {"owner_id": 202},
        {"maintainers": ["someone", "another"]},
    ],
)
def test_what_only_a_registry_maintainer_changes(
    files: Files, github: FakeGitHub, tmp_path: Path, changes: dict[str, Any]
) -> None:
    """A change of the status, blocks, repository or maintainers is never passed."""
    files.provider()
    files.version("1.0.0")

    report = _prepare(files, github, _pull_request(tmp_path, **changes))
    passed, text = check_admission(load_registry(files.root), report, [])

    assert report.maintainer_keys
    assert set(report.maintainer_keys) <= set(changes) | {"reason"}
    assert not passed
    assert "This change needs a registry maintainer" in text
    assert github.requests == []


@pytest.mark.parametrize(
    "changed",
    [
        [],
        ["providers/sample_music/provider.json", "providers/other_music/provider.json"],
        ["providers/sample_music/provider.json", "providers/sample_music/versions/1.0.0.json"],
        ["providers/sample_music/provider.json", ".github/workflows/releases.yml"],
        ["providers/Sample/provider.json"],
        ["providers/sample_music/versions/1.0.0.json"],
    ],
)
def test_a_pull_request_changes_one_provider_file_only(
    files: Files, github: FakeGitHub, tmp_path: Path, changed: list[str]
) -> None:
    """Anything besides the one provider.json of the pull request fails the admission."""
    with pytest.raises(CommandError, match=r"adds or changes the provider\.json of one provider"):
        prepare_admission(
            files.root,
            _pull_request(tmp_path),
            changed,
            author="someone",
            github=GitHub("a-token", github),
            work=files.root / "work",
        )

    assert github.requests == []


def test_provider_text_is_shown_as_code(files: Files, github: FakeGitHub, tmp_path: Path) -> None:
    """A refusal reason and a check reason cannot end their code or mention anyone."""
    github.archives[REPOSITORY, COMMIT] = provider_archive(
        type="```` **Approved** @a-maintainer", name=""
    )
    report = _prepare(files, github, _pull_request(tmp_path))
    files.provider("other_music", folder="other_music")
    files.version("1.0.0", domain="other_music", folder="other_music", lines={})
    other = AdmissionReport(domain="other_music", findings=[], version="1.0.0")
    reason = "load: `` **Every check passed** @a-maintainer"

    _, refused = check_admission(load_registry(files.root), report, [])
    _, failed = check_admission(
        load_registry(files.root),
        other,
        [
            LineResults.from_dict(
                {
                    "line": "nightly",
                    "checked": "2026-11-15T03:00:00Z",
                    "results": [
                        {"domain": "other_music", "version": "1.0.0", "passed": False,
                         "reason": reason}
                    ],
                }
            )
        ],
    )  # fmt: skip

    assert (
        "is refused:\n\n  `````\n  providers/sample_music/versions/1.0.0.json: manifest" in refused
    )
    assert refused.count("  `````\n") == 2
    assert f"- ❌ nightly: failed: ``` {reason} ```" in failed


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
    assert failed == 'provider.json cannot be read: Field "maintainers" must be a list'
    assert github.requests == []
