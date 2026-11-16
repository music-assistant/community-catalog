"""Tests for the admission checks of a pull request that adds or changes a provider."""

from __future__ import annotations

import http.client
import json
from typing import TYPE_CHECKING, Any

import pytest

from registry.admission import (
    MAX_COMMENT_SIZE,
    MAX_PROVIDER_FILE_SIZE,
    RULES_URL,
    admission_comment,
    check_admission,
    prepare_admission,
)
from registry.files import load_model
from registry.github import GitHub
from registry.models import AdmissionReport, CheckPlan, Finding, LineResults
from registry.pipeline import PLAN_FILE
from registry.registry import load_registry
from tests.conftest import (
    DOMAIN,
    OWNER_ID,
    REPOSITORY_ID,
    FakeGitHub,
    contents,
    provider_archive,
    provider_file,
    release,
)

if TYPE_CHECKING:
    from tests.conftest import Files

REPOSITORY = "someone/ma-sample"
COMMIT = "1" * 40
# the registry, which holds the head commit of the pull request
REGISTRY = "music-assistant/community-catalog"
HEAD = "2" * 40
RUN_URL = "https://github.com/music-assistant/community-catalog/actions/runs/1"
FILE_URL = f"https://api.github.com/repos/{REGISTRY}/contents/providers/{DOMAIN}/provider.json"


@pytest.fixture
def github() -> FakeGitHub:
    """Return a GitHub with the sample repository, one release, and a pull request that adds it."""
    github = FakeGitHub()
    github.repository(REPOSITORY, [release("v1.0.0", COMMIT)])
    github.archives[REPOSITORY, COMMIT] = provider_archive()
    _pull_request(github)
    return github


def _pull_request(github: FakeGitHub, domain: str = DOMAIN, **changes: Any) -> None:
    """Add the provider.json of a pull request to the head commit GitHub answers for."""
    path = f"providers/{domain}/provider.json"
    content = json.dumps(provider_file(domain, **changes)).encode()
    github.contents[REGISTRY, path, HEAD] = contents(path, content)


def _prepare(
    files: Files,
    github: FakeGitHub,
    domain: str = DOMAIN,
    author: str = "someone",
    changed: list[str] | None = None,
) -> AdmissionReport:
    """Run the prepare step of the admission for a pull request that changes one provider.json."""
    return prepare_admission(
        files.root,
        [f"providers/{domain}/provider.json"] if changed is None else changed,
        repository=REGISTRY,
        commit=HEAD,
        author=author,
        github=GitHub("a-token", github),
        work=files.root / "work",
    )


def _asked(github: FakeGitHub) -> list[str]:
    """Return the addresses GitHub was asked for."""
    return [request.full_url for request in github.requests]


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


def test_a_provider_that_passes(files: Files, github: FakeGitHub) -> None:
    """Every check passes, and the latest release is a version file ready for the check step."""
    report = _prepare(files, github)

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
    assert "Every check passed. A registry maintainer now reviews the provider against" in text
    assert f"[rules of the community catalog]({RULES_URL})" in text


def test_the_release_must_pass_on_a_line(files: Files, github: FakeGitHub) -> None:
    """A release that passes on no line, or was not checked, fails the admission."""
    report = _prepare(files, github)
    registry = load_registry(files.root)

    failed, failed_text = check_admission(registry, report, [_results("nightly", passed=False)])
    other, _ = check_admission(
        registry, report, [_results("nightly", passed=True, version="0.1.0")]
    )

    assert not failed
    assert not other
    assert "- ❌ beta: the checks did not run" in failed_text
    assert "- ❌ nightly: failed:" in failed_text
    assert "Not every check passed. Fix what failed" in failed_text
    assert "push to this pull request" in failed_text
    assert "close and reopen this pull request" in failed_text


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
    files: Files, github: FakeGitHub, node: dict[str, Any], problem: str
) -> None:
    """Archived, without a licence, without a release, or another repository fails."""
    github.repositories[REPOSITORY].update(node)

    assert _failed(_prepare(files, github)) == [problem]


def test_the_author_and_the_maintainers_must_own_the_provider(
    files: Files, github: FakeGitHub
) -> None:
    """The author and every maintainer own the repository or are a code owner in the manifest."""
    github.repositories[REPOSITORY]["owner"] = {"login": "an-organisation", "databaseId": OWNER_ID}
    github.archives[REPOSITORY, COMMIT] = provider_archive(codeowners=["@another"])
    _pull_request(github, maintainers=["another", "a-stranger", "someone"])

    report = _prepare(files, github)

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


def test_a_code_owner_of_an_organisation_repository(files: Files, github: FakeGitHub) -> None:
    """The author may be a code owner in the manifest of the latest release instead of the owner."""
    github.repositories[REPOSITORY]["owner"] = {"login": "an-organisation", "databaseId": OWNER_ID}
    github.archives[REPOSITORY, COMMIT] = provider_archive(codeowners=["@Another"])
    _pull_request(github, maintainers=["another"])

    report = _prepare(files, github, author="another")

    assert _failed(report) == []


def test_a_repository_that_is_not_public(files: Files) -> None:
    """GitHub answers nothing for a private repository."""
    github = FakeGitHub()
    _pull_request(github)

    report = _prepare(files, github)

    assert _failed(report) == ["The repository does not exist or is not public: someone/ma-sample"]
    assert report.version is None
    assert load_model(files.root / "work" / PLAN_FILE, CheckPlan).lines == {}


def test_a_refused_release(files: Files, github: FakeGitHub) -> None:
    """A latest release that cannot become a version fails the admission with the reason."""
    github.archives[REPOSITORY, COMMIT] = provider_archive(type="core")

    report = _prepare(files, github)

    [failed] = _failed(report)
    assert failed.startswith("The latest release, version 1.0.0, is refused: ")
    assert "manifest.type: 'core'" in failed
    assert report.version is None


def test_a_reserved_domain(files: Files, github: FakeGitHub) -> None:
    """A domain the server ships cannot be taken; the rules of the base branch decide."""
    _pull_request(github, "builtin_one", folder="builtin_one")

    report = _prepare(files, github, "builtin_one")

    assert _failed(report) == [
        "The domain is a provider the server ships: builtin_one",
        (
            "Not valid: providers/builtin_one/provider.json: domain: builtin_one is a provider "
            "the server ships"
        ),
    ]


def test_a_change_of_an_existing_provider(files: Files, github: FakeGitHub) -> None:
    """A new folder for an existing provider gets the same checks."""
    files.provider(folder=DOMAIN)
    files.version("1.0.0", folder=DOMAIN)

    report = _prepare(files, github)

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
    files: Files, github: FakeGitHub, changes: dict[str, Any]
) -> None:
    """A change of the status, blocks, repository or maintainers is never passed."""
    files.provider()
    files.version("1.0.0")

    _pull_request(github, **changes)

    report = _prepare(files, github)
    passed, text = check_admission(load_registry(files.root), report, [])

    assert report.maintainer_keys
    assert set(report.maintainer_keys) <= set(changes) | {"reason"}
    assert not passed
    assert "This change needs a registry maintainer" in text
    assert _asked(github) == [f"{FILE_URL}?ref={HEAD}"]


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
    files: Files, github: FakeGitHub, changed: list[str]
) -> None:
    """Anything besides the one provider.json is refused, before anything is read."""
    report = _prepare(files, github, changed=changed)

    assert report.refusal is not None
    assert "adds or changes the provider.json of one provider and nothing else" in report.refusal
    assert (report.domain, report.findings, report.version) == (None, [], None)
    assert load_model(files.root / "work" / PLAN_FILE, CheckPlan).lines == {}
    assert github.requests == []


@pytest.mark.parametrize(
    ("answer", "refusal"),
    [
        (None, f"providers/{DOMAIN}/provider.json does not exist at commit {HEAD}"),
        # a symbolic link to another file: the API answers that file with the id of the link
        (
            contents(f"providers/{DOMAIN}/provider.json", b"{}", sha="3" * 40),
            f"providers/{DOMAIN}/provider.json is a symbolic link at commit {HEAD}",
        ),
        (
            {"type": "symlink", "path": f"providers/{DOMAIN}/provider.json", "target": "/etc"},
            f"providers/{DOMAIN}/provider.json is no regular file at commit {HEAD}",
        ),
        (
            contents(f"providers/{DOMAIN}/other.json", b"{}"),
            f"providers/{DOMAIN}/provider.json is no regular file at commit {HEAD}",
        ),
        (
            [contents(f"providers/{DOMAIN}/provider.json/x", b"{}")],
            f"providers/{DOMAIN}/provider.json is no regular file at commit {HEAD}",
        ),
        (
            contents(f"providers/{DOMAIN}/provider.json", b" " * (MAX_PROVIDER_FILE_SIZE + 1)),
            (
                f"providers/{DOMAIN}/provider.json is larger than {MAX_PROVIDER_FILE_SIZE} "
                f"bytes at commit {HEAD}"
            ),
        ),
    ],
    ids=["missing", "link to a file", "link elsewhere", "another path", "folder", "too large"],
)
def test_a_provider_file_that_is_no_regular_file_is_refused(
    files: Files, github: FakeGitHub, answer: Any, refusal: str
) -> None:
    """The provider.json is read only when it is a regular file of a sensible size."""
    path = f"providers/{DOMAIN}/provider.json"
    del github.contents[REGISTRY, path, HEAD]
    if answer is not None:
        github.contents[REGISTRY, path, HEAD] = answer

    report = _prepare(files, github)

    assert report.refusal == refusal
    assert report.findings == []
    assert _asked(github) == [f"{FILE_URL}?ref={HEAD}"]


@pytest.mark.parametrize(
    "error", [http.client.IncompleteRead(b"partial", 100), TimeoutError("timed out")]
)
def test_github_failing_to_answer_the_provider_file_is_no_refusal(
    files: Files, github: FakeGitHub, error: Exception
) -> None:
    """When GitHub cannot answer for the provider.json, the checks could not run."""
    github.broken[f"{FILE_URL}?ref={HEAD}"] = error

    with pytest.raises(OSError, match=r"the answer broke off|timed out"):
        _prepare(files, github)

    assert not (files.root / "work" / PLAN_FILE).exists()


def test_provider_text_is_shown_as_code(files: Files, github: FakeGitHub) -> None:
    """A refusal reason and a check reason cannot end their code or mention anyone."""
    github.archives[REPOSITORY, COMMIT] = provider_archive(
        type="```` **Approved** @a-maintainer", name=""
    )
    report = _prepare(files, github)
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


def test_a_provider_file_that_is_not_valid(files: Files, github: FakeGitHub) -> None:
    """A provider.json that cannot be read is reported, and nothing else is asked of GitHub."""
    _pull_request(github, maintainers="someone")

    report = _prepare(files, github)

    [failed] = _failed(report)
    assert failed == 'provider.json cannot be read: Field "maintainers" must be a list'
    assert _asked(github) == [f"{FILE_URL}?ref={HEAD}"]


def test_a_refused_pull_request_says_what_an_admission_holds(files: Files) -> None:
    """The refusal is quoted as code, with what a pull request for admission changes."""
    refusal = "this one changes 2 file(s): '```` **Approved** @a-maintainer'"
    report = AdmissionReport(domain=None, findings=[], refusal=refusal)

    passed, text = check_admission(load_registry(files.root), report, [], RUN_URL)

    assert not passed
    assert text.startswith("## Admission checks\n\nThis pull request is not an admission")
    assert f"\n`````\n{refusal}\n`````\n" in text
    assert "changes one file, ` providers/<domain>/provider.json `, and nothing else" in text
    assert text.endswith(f"[The run of these checks]({RUN_URL})\n")


def test_checks_that_could_not_run(files: Files) -> None:
    """Without the report of the prepare step the result says the checks could not run."""
    passed, text = check_admission(load_registry(files.root), None, [], RUN_URL)

    assert not passed
    assert text.startswith("## Admission checks\n\nThe admission checks could not run")
    assert f"[The run of these checks]({RUN_URL})" in text


def test_a_change_for_a_registry_maintainer(files: Files) -> None:
    """A change only a registry maintainer makes says so instead of how to fix it."""
    finding = Finding(passed=False, text="The pull request changes it", detail="status")
    report = AdmissionReport(domain=DOMAIN, findings=[finding], maintainer_keys=["status"])

    passed, text = check_admission(load_registry(files.root), report, [])

    assert not passed
    assert "This change needs a registry maintainer, who decides on it" in text
    assert "Fix what failed" not in text
    assert "run of these checks" not in text


def test_a_comment_holds_the_result_up_to_its_limit() -> None:
    """A result too long for a comment on GitHub is replaced by where to find it."""
    result = "## Admission checks for ` sample_music `\n\n- ✅ The domain is free\n"
    long = result + "x" * MAX_COMMENT_SIZE

    assert admission_comment(result, RUN_URL) == result
    assert admission_comment(long, RUN_URL) == (
        "## Admission checks\n\nThe result is too long for a comment; it is in the summary of "
        f"[the run of these checks]({RUN_URL}).\n"
    )
