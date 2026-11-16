"""Tests for the admission checks of a submission, their result and their pull request."""

from __future__ import annotations

import http.client
import json
from typing import TYPE_CHECKING, Any

import pytest

from registry.admission import (
    FORM_URL,
    MAX_COMMENT_SIZE,
    RULES_URL,
    Proposal,
    admission_comment,
    check_admission,
    issue_digest,
    prepare_admission,
    propose_admission,
)
from registry.cli import main
from registry.edit import CommandError
from registry.files import load_model
from registry.formats import is_date
from registry.github import GitHub
from registry.models import AdmissionReport, CheckPlan, Finding, LineResults, ProviderFile
from registry.pipeline import PLAN_FILE, archive_path
from registry.registry import load_registry
from tests.conftest import (
    BOT_LOGIN,
    DOMAIN,
    OWNER_ID,
    REPOSITORY_ID,
    FakeGitHub,
    build_archive,
    manifest,
    release,
)
from tests.test_submission import body

if TYPE_CHECKING:
    from typing import BinaryIO
    from urllib.request import Request

    from registry.github import Transport
    from tests.conftest import Files

REPOSITORY = "someone/ma-sample"
COMMIT = "1" * 40
# the registry, which holds the issue
REGISTRY = "music-assistant/community-catalog"
ISSUE = 7
MAIN = "2" * 40
RUN_URL = "https://github.com/music-assistant/community-catalog/actions/runs/1"
ISSUE_URL = f"https://api.github.com/repos/{REGISTRY}/issues/{ISSUE}"
CHECKED = "2026-11-16T12:00:00Z"


def _release(domain: str = DOMAIN, folder: str = DOMAIN, **changes: Any) -> bytes:
    """Return the archive of a release with one provider in a top-level folder."""
    content = json.dumps({**manifest(domain), **changes}).encode()
    return build_archive(
        {f"{folder}/manifest.json": content, f"{folder}/__init__.py": b"VALUE = 1\n"}
    )


@pytest.fixture
def github() -> FakeGitHub:
    """Return a GitHub with the sample repository, one release and its submission."""
    github = FakeGitHub()
    github.repository(REPOSITORY, [release("v1.0.0", COMMIT)])
    github.archives[REPOSITORY, COMMIT] = _release()
    github.issue(REGISTRY, ISSUE, body())
    github.branches[REGISTRY, "main"] = MAIN
    return github


def _prepare(files: Files, github: FakeGitHub) -> AdmissionReport:
    """Run the prepare step of the admission for the issue of the submission."""
    return prepare_admission(
        files.root,
        repository=REGISTRY,
        issue=ISSUE,
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


def _results(line: str, passed: bool, version: str = "1.0.0", domain: str = DOMAIN) -> LineResults:
    """Return the results file of a line with one result for a provider."""
    reason = None if passed else "load: ModuleNotFoundError: No module named 'x'"
    return LineResults.from_dict(
        {
            "line": line,
            "checked": "2026-11-15T03:00:00Z",
            "results": [{"domain": domain, "version": version, "passed": passed, "reason": reason}],
        }
    )


def _passed(issue: int = ISSUE, repository: str = REPOSITORY) -> AdmissionReport:
    """Return the report of a submission that passes every check of the prepare step."""
    candidate = ProviderFile(
        domain=DOMAIN,
        repository=repository,
        repository_id=REPOSITORY_ID + issue,
        owner_id=OWNER_ID,
        folder=DOMAIN,
        added=CHECKED,
    )
    return AdmissionReport(
        checked=CHECKED,
        findings=[Finding(passed=True, text="The domain is free", detail=DOMAIN)],
        issue_digest=issue_digest(body(repository)),
        repository=repository,
        domain=DOMAIN,
        tag="v1.0.0",
        commit=COMMIT,
        version="1.0.0",
        name="Sample `music`\n@a-maintainer",
        provider=candidate,
    )


def _propose(
    files: Files,
    github: Transport,
    report: AdmissionReport | None,
    *,
    passed: bool = True,
    issue: int = ISSUE,
) -> Proposal:
    """Run the propose step for an issue with the results of the nightly line."""
    domain = report.domain or DOMAIN if report else DOMAIN
    return propose_admission(
        load_registry(files.root),
        report,
        [_results("nightly", passed, domain=domain)],
        repository=REGISTRY,
        issue=issue,
        author=BOT_LOGIN,
        run_url=RUN_URL,
        github=GitHub("a-token", github),
    )


def test_a_submission_that_passes(files: Files, github: FakeGitHub) -> None:
    """Every check passes, and the latest release is a version file ready for the check step."""
    report = _prepare(files, github)

    assert _failed(report) == []
    assert [(finding.text, finding.detail) for finding in report.findings] == [
        ("The repository is public and not archived", REPOSITORY),
        ("A licence GitHub recognises", "mit"),
        ("The domain is free", DOMAIN),
        ("provider.json is valid", None),
        ("The latest release is version 1.0.0", "v1.0.0"),
        (
            (
                "The submitter owns the repository or is a code owner in the manifest of the "
                "latest release"
            ),
            "someone",
        ),
    ]
    assert (report.repository, report.domain, report.tag, report.commit) == (
        REPOSITORY,
        DOMAIN,
        "v1.0.0",
        COMMIT,
    )
    assert (report.version, report.name, report.refusal) == ("1.0.0", "Sample music", None)
    assert is_date(report.checked)
    # the ids come from GitHub, the rest from the form and the release
    assert report.provider == ProviderFile(
        domain=DOMAIN,
        repository=REPOSITORY,
        repository_id=REPOSITORY_ID,
        owner_id=OWNER_ID,
        folder=DOMAIN,
        added=report.checked,
    )
    registry = load_registry(files.root)
    assert set(registry.providers[DOMAIN].versions) == {"1.0.0"}
    assert archive_path(files.root / "work", DOMAIN, "1.0.0").is_file()
    plan = load_model(files.root / "work" / PLAN_FILE, CheckPlan)
    assert {line: [(e.domain, e.version) for e in v] for line, v in plan.lines.items()} == {
        "beta": [(DOMAIN, "1.0.0")],
        "nightly": [(DOMAIN, "1.0.0")],
    }

    passed, text = check_admission(
        registry, report, [_results("nightly", passed=True), _results("beta", passed=False)]
    )

    assert passed
    assert text.startswith(
        f"## Admission checks for ` {DOMAIN} `\n\nChecked {report.checked}: repository "
        f"` {REPOSITORY} `, release ` v1.0.0 ` (commit ` {COMMIT} `).\n\n"
    )
    assert "- ✅ nightly: passed" in text
    assert "- ❌ beta: failed: ` load: ModuleNotFoundError: No module named 'x' `" in text
    assert "Every check passed. A registry maintainer reviews the provider against" in text
    assert f"[rules of the community catalog]({RULES_URL})" in text


def test_the_folder_and_tag_prefix_of_the_form(files: Files, github: FakeGitHub) -> None:
    """A repository with several providers names the folder and the tag prefix."""
    github.repository(
        REPOSITORY,
        [release("other_music-v2.0.0", "3" * 40), release("sample_music-v1.0.0", COMMIT)],
    )
    github.archives[REPOSITORY, COMMIT] = _release(folder=f"providers/{DOMAIN}")
    github.issue(
        REGISTRY,
        ISSUE,
        body("https://github.com/someone/ma-sample", f"providers/{DOMAIN}", f"{DOMAIN}-"),
    )

    report = _prepare(files, github)

    assert _failed(report) == []
    assert report.tag == "sample_music-v1.0.0"
    assert report.provider is not None
    assert (report.provider.folder, report.provider.tag_prefix) == (
        f"providers/{DOMAIN}",
        f"{DOMAIN}-",
    )


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
    assert "Not every check passed. Fix what failed, then run the checks again: edit" in failed_text
    assert "close and reopen it, for example after you published a new release" in failed_text


@pytest.mark.parametrize(
    ("node", "problem"),
    [
        ({"isArchived": True}, "The repository is public and archived: someone/ma-sample"),
        ({"licenseInfo": None}, "A licence GitHub recognises: none"),
        ({"licenseInfo": {"key": "other"}}, "A licence GitHub recognises: other"),
    ],
)
def test_the_repository_must_qualify(
    files: Files, github: FakeGitHub, node: dict[str, Any], problem: str
) -> None:
    """An archived repository or one without a licence fails."""
    github.repositories[REPOSITORY].update(node)

    assert _failed(_prepare(files, github)) == [problem]


@pytest.mark.parametrize(
    ("changes", "refusal"),
    [
        ({"labels": []}, f"#{ISSUE} is no issue with the label submission"),
        ({"pull_request": {"url": "x"}}, f"#{ISSUE} is no issue with the label submission"),
        ({"state": "closed"}, f"#{ISSUE} is closed"),
        ({"body": "Please add my provider"}, "does not hold the fields of the form"),
        ({"body": None}, "does not hold the fields of the form"),
    ],
)
def test_an_issue_that_is_no_submission(
    files: Files, github: FakeGitHub, changes: dict[str, Any], refusal: str
) -> None:
    """Nothing but the issue is read when it is no valid submission."""
    github.issue_answers[REGISTRY, ISSUE].update(changes)

    report = _prepare(files, github)

    assert report.refusal is not None
    assert refusal in report.refusal
    assert (report.repository, report.domain, report.findings) == (None, None, [])
    assert load_model(files.root / "work" / PLAN_FILE, CheckPlan).lines == {}
    assert [request.full_url for request in github.requests] == [ISSUE_URL]


@pytest.mark.parametrize(
    ("node", "archive", "refusal"),
    [
        (None, None, "someone/ma-sample does not exist or is not public"),
        (
            {"releases": {"nodes": []}},
            None,
            "someone/ma-sample has no published release with a tag such as v1.0.0",
        ),
        (
            None,
            build_archive({"a/manifest.json": b"{}", "b/manifest.json": b"{}"}),
            "release v1.0.0: the release has 2 top-level folders with a manifest.json (a, b)",
        ),
    ],
    ids=["not public", "no release", "ambiguous"],
)
def test_a_submission_that_cannot_be_checked(
    files: Files,
    github: FakeGitHub,
    node: dict[str, Any] | None,
    archive: bytes | None,
    refusal: str,
) -> None:
    """Without the repository, a release or a provider folder nothing can be checked."""
    if node is None and archive is None:
        del github.repositories[REPOSITORY]
    elif node is not None:
        github.repositories[REPOSITORY].update(node)
    if archive is not None:
        github.archives[REPOSITORY, COMMIT] = archive

    report = _prepare(files, github)

    assert report.refusal is not None
    assert report.refusal.startswith(refusal)
    assert (report.domain, report.findings, report.provider) == (None, [], None)
    assert load_model(files.root / "work" / PLAN_FILE, CheckPlan).lines == {}
    passed, text = check_admission(load_registry(files.root), report, [], run_url=RUN_URL)
    assert not passed
    assert text.startswith("## Admission checks\n\nChecked ")
    assert "This issue is not a valid submission, so nothing was checked:" in text
    assert f"The [form]({FORM_URL}) says what each field holds." in text


def test_the_submitter_must_own_the_provider(files: Files, github: FakeGitHub) -> None:
    """The submitter owns the repository or is a code owner in the manifest of the release."""
    github.repositories[REPOSITORY]["owner"] = {"login": "an-organisation", "databaseId": OWNER_ID}
    github.archives[REPOSITORY, COMMIT] = _release(codeowners=["@another"])

    assert _failed(_prepare(files, github)) == [
        (
            "The submitter owns the repository or is a code owner in the manifest of the latest "
            "release: someone"
        )
    ]


def test_a_submission_that_failed_a_requirement_is_not_checked(
    files: Files, github: FakeGitHub
) -> None:
    """The release of a submission by someone else is never checked, so its code does not run."""
    github.repositories[REPOSITORY]["owner"] = {"login": "an-organisation", "databaseId": OWNER_ID}
    github.archives[REPOSITORY, COMMIT] = _release(codeowners=["@another"])

    report = _prepare(files, github)

    assert report.version == "1.0.0"
    assert _failed(report)
    assert load_model(files.root / "work" / PLAN_FILE, CheckPlan).lines == {}
    passed, text = check_admission(load_registry(files.root), report, [])
    assert not passed
    assert "the checks did not run" not in text
    assert "nightly" not in text


def test_a_code_owner_of_an_organisation_repository(files: Files, github: FakeGitHub) -> None:
    """A code owner in the manifest of the latest release may submit instead of the owner."""
    github.repositories[REPOSITORY]["owner"] = {"login": "an-organisation", "databaseId": OWNER_ID}
    github.archives[REPOSITORY, COMMIT] = _release(codeowners=["@Another"])
    github.issue_answers[REGISTRY, ISSUE]["user"] = {"login": "another"}

    assert _failed(_prepare(files, github)) == []


def test_a_submitter_that_is_no_github_handle(files: Files, github: FakeGitHub) -> None:
    """An author GitHub would not give a person stops the checks."""
    github.issue_answers[REGISTRY, ISSUE]["user"] = {"login": "a bot[bot]"}

    with pytest.raises(CommandError, match="is not a GitHub handle"):
        _prepare(files, github)


def test_a_refused_release(files: Files, github: FakeGitHub) -> None:
    """A latest release that cannot become a version fails the admission with the reason."""
    github.archives[REPOSITORY, COMMIT] = _release(type="core")

    report = _prepare(files, github)

    [failed] = _failed(report)
    assert failed.startswith("The latest release, version 1.0.0, is refused: ")
    assert "manifest.type: 'core'" in failed
    assert report.version is None


def test_a_reserved_domain(files: Files, github: FakeGitHub) -> None:
    """A domain the server ships cannot be taken; the rules of the checkout decide."""
    github.archives[REPOSITORY, COMMIT] = _release("builtin_one", "builtin_one")

    report = _prepare(files, github)

    assert _failed(report) == [
        "The domain is a provider the server ships: builtin_one",
        (
            "Not valid: providers/builtin_one/provider.json: domain: builtin_one is a provider "
            "the server ships"
        ),
    ]
    assert (report.provider, report.version) == (None, None)


def test_a_domain_that_is_taken(files: Files, github: FakeGitHub) -> None:
    """A domain in the catalog cannot be submitted again; its provider.json stays as it is."""
    path = files.provider(repository="another/ma-sample", folder=DOMAIN)
    before = path.read_bytes()

    report = _prepare(files, github)

    assert _failed(report) == [
        f"The domain is taken by a provider in the community catalog: {DOMAIN}"
    ]
    assert path.read_bytes() == before
    assert (report.provider, report.version) == (None, None)


@pytest.mark.parametrize(
    "error", [http.client.IncompleteRead(b"partial", 100), TimeoutError("timed out")]
)
def test_github_failing_to_answer_is_no_refusal(
    files: Files, github: FakeGitHub, error: Exception
) -> None:
    """When GitHub cannot answer for the issue, the checks could not run."""
    github.broken[ISSUE_URL] = error

    with pytest.raises(OSError, match=r"the answer broke off|timed out"):
        _prepare(files, github)

    assert not (files.root / "work" / PLAN_FILE).exists()


def test_provider_text_is_shown_as_code(files: Files, github: FakeGitHub) -> None:
    """A refusal reason and a check reason cannot end their code or mention anyone."""
    github.archives[REPOSITORY, COMMIT] = _release(type="```` **Approved** @a-maintainer", name="")
    report = _prepare(files, github)
    reason = "load: `` **Every check passed** @a-maintainer"
    result = {"domain": DOMAIN, "version": "1.0.0", "passed": False, "reason": reason}
    results = {"line": "nightly", "checked": "2026-11-15T03:00:00Z", "results": [result]}

    _, refused = check_admission(load_registry(files.root), report, [])
    _, failed = check_admission(
        load_registry(files.root), _passed(), [LineResults.from_dict(results)]
    )

    assert "is refused:\n\n  `````\n  providers/sample_music/versions/1.0.0.json: manifest" in (
        refused
    )
    assert refused.count("  `````\n") == 2
    assert f"- ❌ nightly: failed: ``` {reason} ```" in failed


def test_a_refusal_is_quoted_as_code(files: Files) -> None:
    """The refusal is quoted as code, with what to do."""
    refusal = "Repository: '```` **Approved** @a-maintainer' is not a GitHub repository"
    report = AdmissionReport(checked=CHECKED, findings=[], issue_digest="", refusal=refusal)

    passed, text = check_admission(load_registry(files.root), report, [], run_url=RUN_URL)

    assert not passed
    assert text.startswith(
        f"## Admission checks\n\nChecked {CHECKED}: the issue.\n\n"
        "This issue is not a valid submission"
    )
    assert f"\n`````\n{refusal}\n`````\n" in text
    assert "Edit the issue to fix it, and the checks run again by themselves." in text
    assert text.endswith(f"[The run of these checks]({RUN_URL})\n")


def test_checks_that_could_not_run(files: Files) -> None:
    """Without the report of the prepare step the result says the checks could not run."""
    passed, text = check_admission(load_registry(files.root), None, [], run_url=RUN_URL)

    assert not passed
    assert text.startswith("## Admission checks\n\nThe admission checks could not run")
    assert f"[The run of these checks]({RUN_URL})" in text


def test_the_result_links_the_pull_request(files: Files) -> None:
    """A result that passed links the pull request, or says it could not be opened."""
    registry = load_registry(files.root)
    results = [_results("nightly", passed=True), _results("beta", passed=True)]
    url = f"https://github.com/{REGISTRY}/pull/12"

    _, linked = check_admission(registry, _passed(), results, pull_request=url)
    _, failed = check_admission(registry, _passed(), results, proposal_failed=True)

    assert f"decides by merging [the pull request that adds it]({url});" in linked
    assert "the pull request that adds the provider could not be opened" in failed


def test_a_comment_holds_the_result_up_to_its_limit() -> None:
    """A result too long for a comment on GitHub is replaced by where to find it."""
    result = "## Admission checks for ` sample_music `\n\n- ✅ The domain is free\n"
    long = result + "x" * MAX_COMMENT_SIZE

    assert admission_comment(result, RUN_URL) == result
    assert admission_comment(long, RUN_URL) == (
        "## Admission checks\n\nThe result is too long for a comment; it is in the summary of "
        f"[the run of these checks]({RUN_URL}).\n"
    )


def test_propose_opens_a_pull_request(files: Files, github: FakeGitHub) -> None:
    """A passed submission gets a branch of its own from main with its provider.json."""
    proposal = _propose(files, github, _passed())

    branch = f"admit/{DOMAIN}-{ISSUE}"
    assert proposal == Proposal(url=f"https://github.com/{REGISTRY}/pull/100", others=[])
    assert github.branches[REGISTRY, branch] == MAIN
    key = (REGISTRY, branch, f"providers/{DOMAIN}/provider.json")
    assert list(github.files) == [key]
    assert json.loads(github.files[key]) == {
        "added": CHECKED,
        "domain": DOMAIN,
        "folder": DOMAIN,
        "owner_id": OWNER_ID,
        "repository": REPOSITORY,
        "repository_id": REPOSITORY_ID + ISSUE,
    }
    [pull] = github.pulls[REGISTRY]
    assert pull["head"]["ref"] == branch
    assert pull["body"].startswith(
        f"Closes #{ISSUE}\n\nAdds ` providers/{DOMAIN}/provider.json `: "
        f"`` Sample `music` @a-maintainer `` from ` {REPOSITORY} `.\n\n"
    )
    assert "Every check passed." in pull["body"]
    assert f"release ` v1.0.0 ` (commit ` {COMMIT} `)" in pull["body"]


def test_no_text_of_a_submission_reaches_a_title_or_a_commit(
    files: Files, github: FakeGitHub
) -> None:
    """GitHub reads keywords in titles and commit messages, so they hold the domain only."""
    report = _passed()
    report.name = "Sample. Closes #123 and fixes other/repo#456 @victim [skip ci]"

    _propose(files, github, report)

    [pull] = github.pulls[REGISTRY]
    assert pull["title"] == f"Admit {DOMAIN}"
    assert list(github.messages.values()) == [f"Admit {DOMAIN}"]
    assert f"`: ` {report.name} ` from" in pull["body"]


def test_propose_updates_its_pull_request(files: Files, github: FakeGitHub) -> None:
    """A later run makes the branch afresh and updates the pull request of the issue."""
    report = _passed()
    url = _propose(files, github, report).url
    github.branches[REGISTRY, "main"] = "4" * 40
    report.checked = "2026-11-17T12:00:00Z"

    assert _propose(files, github, report).url == url

    [pull] = github.pulls[REGISTRY]
    assert pull["state"] == "open"
    assert "Checked 2026-11-17T12:00:00Z" in pull["body"]
    assert github.branches[REGISTRY, f"admit/{DOMAIN}-{ISSUE}"] == "4" * 40
    assert len(github.files) == 1


@pytest.mark.parametrize(
    ("change", "why"),
    [
        ({}, f"Closed: the checks of #{ISSUE} did not pass in [this run]({RUN_URL})."),
        ({"state": "closed"}, f"Closed: #{ISSUE} is closed."),
        ({"labels": []}, f"Closed: #{ISSUE} changed while [this run]({RUN_URL}) checked it."),
        (
            {"body": body("someone/another")},
            f"Closed: #{ISSUE} changed while [this run]({RUN_URL}) checked it.",
        ),
    ],
    ids=["failed", "closed", "unlabelled", "edited"],
)
def test_propose_closes_its_pull_request(
    files: Files, github: FakeGitHub, change: dict[str, Any], why: str
) -> None:
    """A run that fails, or an issue closed or changed meanwhile, closes the pull request."""
    _propose(files, github, _passed())
    github.issue_answers[REGISTRY, ISSUE].update(change)

    assert _propose(files, github, _passed(), passed=bool(change)).url is None

    [pull] = github.pulls[REGISTRY]
    assert pull["state"] == "closed"
    [comment] = github.comments[REGISTRY, pull["number"]]
    assert comment["body"] == why
    assert (REGISTRY, f"admit/{DOMAIN}-{ISSUE}") not in github.branches
    assert github.files == {}


def test_a_closed_issue_gets_no_pull_request(files: Files, github: FakeGitHub) -> None:
    """A submission closed while it was checked is not proposed, however it passed."""
    github.issue_answers[REGISTRY, ISSUE]["state"] = "closed"

    assert _propose(files, github, _passed()).url is None
    assert _propose(files, github, None).url is None

    assert github.pulls.get(REGISTRY, []) == []
    assert (REGISTRY, f"admit/{DOMAIN}-{ISSUE}") not in github.branches


def test_closing_leaves_a_merged_pull_request_alone(files: Files, github: FakeGitHub) -> None:
    """Merging closes the issue; the merged pull request and its branch stay as they are."""
    _propose(files, github, _passed())
    github.pulls[REGISTRY][0].update(state="closed", merged=True)
    github.issue_answers[REGISTRY, ISSUE]["state"] = "closed"

    _propose(files, github, None)

    assert github.comments == {}
    assert (REGISTRY, f"admit/{DOMAIN}-{ISSUE}") in github.branches


def test_propose_closes_the_pull_request_of_another_domain(
    files: Files, github: FakeGitHub
) -> None:
    """When the issue names another provider, its earlier pull request is closed."""
    _propose(files, github, _passed())
    report = _passed()
    assert report.provider is not None
    report.domain = report.provider.domain = report.provider.folder = "other_music"

    url = _propose(files, github, report).url

    first, second = github.pulls[REGISTRY]
    assert (first["state"], second["state"]) == ("closed", "open")
    assert second["html_url"] == url
    [comment] = github.comments[REGISTRY, first["number"]]
    assert comment["body"] == f"Closed: #{ISSUE} names another provider, {url}."
    assert (REGISTRY, f"admit/{DOMAIN}-{ISSUE}") not in github.branches


def test_two_submissions_of_one_domain_racing(files: Files, github: FakeGitHub) -> None:
    """
    Two runs that both see no pull request still give two consistent pull requests.

    The second run commits and opens its pull request between the first run's commit and the
    first run's pull request.
    """
    other = 8
    github.issue(REGISTRY, other, body("another/ma-sample"))

    def interleaved(request: Request) -> tuple[int, BinaryIO]:
        opens = request.get_method() == "POST" and request.full_url.endswith("/pulls")
        if opens and not github.pulls.get(REGISTRY):
            _propose(files, github, _passed(other, "another/ma-sample"), issue=other)
        return github(request)

    first = _propose(files, interleaved, _passed())

    pulls = {pull["number"]: pull for pull in github.pulls[REGISTRY]}
    assert len(pulls) == 2
    for pull in pulls.values():
        number = int(pull["head"]["ref"].rsplit("-", 1)[1])
        content = github.files[REGISTRY, pull["head"]["ref"], f"providers/{DOMAIN}/provider.json"]
        expected = REPOSITORY if number == ISSUE else "another/ma-sample"
        assert json.loads(content)["repository"] == expected
        assert pull["body"].startswith(f"Closes #{number}\n")
        assert f"from ` {expected} `" in pull["body"]
    assert first.url is not None
    assert pulls[int(first.url.rsplit("/", 1)[1])]["head"]["ref"] == f"admit/{DOMAIN}-{ISSUE}"


def test_another_submission_of_the_domain_is_named(files: Files, github: FakeGitHub) -> None:
    """The second submission of a domain learns of the open pull request of the first."""
    other = 8
    github.issue(REGISTRY, other, body("another/ma-sample"))
    first = _propose(files, github, _passed())

    second = _propose(files, github, _passed(other, "another/ma-sample"), issue=other)

    assert first.url is not None
    assert second.others == [first.url]
    _, text = check_admission(
        load_registry(files.root),
        _passed(other, "another/ma-sample"),
        [_results("nightly", passed=True), _results("beta", passed=True)],
        others=second.others,
    )
    assert (
        f"Another submission for this domain has an open pull request: [#100]({first.url})." in text
    )


def test_propose_leaves_other_pull_requests_alone(files: Files, github: FakeGitHub) -> None:
    """A pull request of a person, a fork or another issue is never changed."""
    github.pull(REGISTRY, f"admit/{DOMAIN}-{ISSUE}", "x", user={"login": "a-stranger"})
    fork = {"ref": f"admit/{DOMAIN}-{ISSUE}", "repo": {"full_name": "someone/fork"}}
    github.pull(REGISTRY, "x", "x", head=fork)
    github.pull(REGISTRY, f"admit/{DOMAIN}-8", "Closes #8\n")
    before = json.dumps(github.pulls)

    assert _propose(files, github, _passed(), passed=False).url is None

    assert json.dumps(github.pulls) == before
    assert github.comments == {}


def test_propose_refuses_what_main_cannot_take(files: Files, github: FakeGitHub) -> None:
    """A provider main has already is not proposed."""
    files.provider()

    with pytest.raises(CommandError, match="is in the community catalog already"):
        _propose(files, github, _passed())

    assert github.files == {}


def test_propose_writes_the_outputs_of_its_job(
    files: Files, github: FakeGitHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The command adds the address of the pull request and of the others to the job outputs."""
    monkeypatch.setenv("GITHUB_TOKEN", "a-token")
    monkeypatch.setattr("registry.cli.GitHub", lambda token: GitHub(token, github))
    github.pull(REGISTRY, f"admit/{DOMAIN}-8", "Closes #8\n")
    files.write("work/admission.json", _passed().to_dict())
    entry = [{"domain": DOMAIN, "version": "1.0.0"}]
    files.write("work/plan.json", {"lines": {"nightly": entry}})
    files.write(f"results/nightly/{DOMAIN}-1.0.0.json", _results("nightly", passed=True).to_dict())
    output = files.root / "output"
    args = ["--root", str(files.root), "propose-admission", str(files.root / "results"),
            "--repository", REGISTRY, "--issue", str(ISSUE), "--author", BOT_LOGIN,
            "--run-url", RUN_URL, "--output", str(output)]  # fmt: skip

    assert main(args) == 0
    assert output.read_text() == (
        f"url=https://github.com/{REGISTRY}/pull/101\nothers=https://github.com/{REGISTRY}/pull/100\n"
    )
    (files.root / "work" / "admission.json").unlink()
    assert main(args) == 0
    assert output.read_text().endswith("url=\nothers=\n")
    assert github.pulls[REGISTRY][1]["state"] == "closed"
