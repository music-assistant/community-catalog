"""Tests for the admission checks of a submission, their result and their pull request."""

from __future__ import annotations

import http.client
import json
import re
from typing import TYPE_CHECKING, Any

import pytest

from registry.admission import (
    FORM_URL,
    MAX_COMMENT_SIZE,
    RULES_URL,
    Outcome,
    Proposal,
    admission_comment,
    check_admission,
    issue_digest,
    prepare_admission,
    proposal_fingerprint,
    propose_admission,
)
from registry.cli import main
from registry.edit import CommandError
from registry.files import load_model
from registry.formats import is_date
from registry.github import GitHub
from registry.models import AdmissionReport, ChannelResults, CheckPlan, Finding, ProviderFile
from registry.pipeline import PLAN_FILE, archive_path
from registry.registry import InvalidRegistryError, load_registry
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
from tests.test_submission import MOVED, body

if TYPE_CHECKING:
    from collections.abc import Callable
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
PULL = f"https://github.com/{REGISTRY}/pull/12"
MERGE = f"decides by merging [the pull request that adds it]({PULL});"


def _release(domain: str = DOMAIN, folder: str = DOMAIN, **changes: Any) -> bytes:
    """Return the archive of a release with one provider in a top-level folder."""
    content = json.dumps({**manifest(domain), **changes}).encode()
    return build_archive(
        {f"{folder}/manifest.json": content, f"{folder}/__init__.py": b"VALUE = 1\n"}
    )


@pytest.fixture
def github() -> FakeGitHub:
    """Return a GitHub with the sample repository, one release and its submission."""
    return _github()


def _github() -> FakeGitHub:
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


def _results(
    channel: str, passed: bool, version: str = "1.0.0", domain: str = DOMAIN
) -> ChannelResults:
    """Return the results file of a channel with one result for a provider."""
    reason = None if passed else "load: ModuleNotFoundError: No module named 'x'"
    return ChannelResults.from_dict(
        {
            "channel": channel,
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


def _moved(domain: str = "builtin_one") -> AdmissionReport:
    """Return the report of a move out of the server that passes every check of the prepare step."""
    report = _passed()
    assert report.provider is not None
    report.domain = report.provider.domain = report.provider.folder = domain
    report.name, report.moving = "Sample music", True
    return report


def _server_manifest(github: FakeGitHub, domain: str, codeowners: list[Any]) -> None:
    """Give the server's dev branch the manifest of a provider it ships."""
    content = json.dumps({**manifest(domain), "codeowners": codeowners}).encode()
    path = f"music_assistant/providers/{domain}/manifest.json"
    github.contents["music-assistant/server", "dev", path] = content


def _propose(
    files: Files,
    github: Transport,
    report: AdmissionReport | None,
    *,
    passed: bool = True,
    issue: int = ISSUE,
) -> Proposal:
    """Run the propose step for an issue with the results of the nightly channel."""
    domain = report.domain or DOMAIN if report else DOMAIN
    version = report.version or "1.0.0" if report else "1.0.0"
    return propose_admission(
        load_registry(files.root),
        report,
        [_results("nightly", passed, version, domain)],
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
    assert {
        channel: [(e.domain, e.version) for e in v] for channel, v in plan.channels.items()
    } == {
        "beta": [(DOMAIN, "1.0.0")],
        "nightly": [(DOMAIN, "1.0.0")],
    }

    passed, text = check_admission(
        registry,
        report,
        [_results("nightly", passed=True), _results("beta", passed=False)],
        proposal=Proposal(Outcome.OPENED, PULL),
    )

    assert passed
    assert text.startswith(
        f"## Admission checks for ` {DOMAIN} `\n\nChecked {report.checked}: repository "
        f"[` {REPOSITORY} `](https://github.com/{REPOSITORY}), release "
        f"[` v1.0.0 `](https://github.com/{REPOSITORY}/releases/tag/v1.0.0) (commit "
        f"[` {COMMIT} `](https://github.com/{REPOSITORY}/tree/{COMMIT}/{DOMAIN})).\n\n"
    )
    assert "- ✅ Passed on the nightly channel" in text
    assert (
        "- ❌ Failed on the beta channel: ` load: ModuleNotFoundError: No module named 'x' `"
        in text
    )
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


def test_the_release_must_pass_on_a_channel(files: Files, github: FakeGitHub) -> None:
    """A release that passes on no channel, or was not checked, fails the admission."""
    report = _prepare(files, github)
    registry = load_registry(files.root)

    failed, failed_text = check_admission(registry, report, [_results("nightly", passed=False)])
    other, _ = check_admission(
        registry, report, [_results("nightly", passed=True, version="0.1.0")]
    )

    assert not failed
    assert not other
    assert "- ⚪ Not checked on the beta channel" in failed_text
    assert "- ❌ Failed on the nightly channel:" in failed_text
    assert "Not every check passed. Fix what failed, then edit this issue" in failed_text
    assert "close and reopen it after you publish a new release" in failed_text


def test_a_channel_whose_checks_could_not_run_is_no_failure(
    files: Files, github: FakeGitHub
) -> None:
    """A check that could not run in the server image shows as not run, without its error."""
    report = _prepare(files, github)
    error = "load: the load script cannot start in this server image: cannot import name 'x'"
    unavailable = ChannelResults.from_dict(
        {
            "channel": "beta",
            "checked": "2026-11-15T03:00:00Z",
            "results": [
                {"domain": DOMAIN, "version": "1.0.0", "passed": False, "reason": error,
                 "output": f"Traceback\nImportError: {error}", "unavailable": True}
            ],
        }
    )  # fmt: skip

    passed, text = check_admission(
        load_registry(files.root), report, [_results("nightly", passed=True), unavailable]
    )

    assert passed
    assert "- ⚪ Not checked on the beta channel\n" in text
    assert "❌" not in text
    assert "- ✅ Passed on the nightly channel" in text
    assert "cannot import name" not in text


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
        ({"labels": [{"name": "report"}]}, f"#{ISSUE} is no issue with the label submission"),
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
    assert load_model(files.root / "work" / PLAN_FILE, CheckPlan).channels == {}
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
    assert load_model(files.root / "work" / PLAN_FILE, CheckPlan).channels == {}
    passed, text = check_admission(load_registry(files.root), report, [], run_url=RUN_URL)
    assert not passed
    assert text.startswith("## Admission checks\n\nChecked ")
    assert "This issue is not a valid submission, so nothing was checked:" in text
    assert f"The [form]({FORM_URL}) says what each field holds." in text


@pytest.mark.parametrize(
    ("tag", "commit", "release", "linked_commit"),
    [
        (
            "v1.0.0",
            COMMIT,
            f"[` v1.0.0 `](https://github.com/{REPOSITORY}/releases/tag/v1.0.0)",
            f"[` {COMMIT} `](https://github.com/{REPOSITORY}/commit/{COMMIT})",
        ),
        (
            "v1 ..//x @someone",
            "main",
            "` v1 ..//x @someone `",
            "` main `",
        ),
    ],
)
def test_what_was_checked_links_to_github(
    files: Files, tag: str, commit: str, release: str, linked_commit: str
) -> None:
    """The repository, release and commit link to GitHub, as far as their values are safe."""
    report = AdmissionReport(
        checked=CHECKED,
        findings=[],
        issue_digest="",
        repository=REPOSITORY,
        tag=tag,
        commit=commit,
        refusal="no provider folder",
    )

    _, text = check_admission(load_registry(files.root), report, [])

    assert text.startswith(
        f"## Admission checks\n\nChecked {CHECKED}: repository "
        f"[` {REPOSITORY} `](https://github.com/{REPOSITORY}), release {release} "
        f"(commit {linked_commit}).\n\n"
    )


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
    assert load_model(files.root / "work" / PLAN_FILE, CheckPlan).channels == {}
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
        (
            "The domain is a provider the server ships; only a code owner of it in the server "
            "moves it here, with the box Moving from the server: builtin_one"
        ),
        (
            "Not valid: providers/builtin_one/provider.json: domain: builtin_one is a provider "
            "the server ships"
        ),
    ]
    assert (report.provider, report.version, report.moving) == (None, None, False)


def test_a_moving_domain_without_the_box(files: Files, github: FakeGitHub) -> None:
    """A provider the server ships moves only with the box ticked, also one main lists as moving."""
    github.archives[REPOSITORY, COMMIT] = _release("moving_one", "moving_one")

    report = _prepare(files, github)

    assert _failed(report) == [
        (
            "The domain is a provider the server ships; only a code owner of it in the server "
            "moves it here, with the box Moving from the server: moving_one"
        )
    ]
    assert load_model(files.root / "work" / PLAN_FILE, CheckPlan).channels == {}


def test_a_move_by_a_code_owner(files: Files, github: FakeGitHub) -> None:
    """A code owner of a provider in the server moves it, and the check loads its release."""
    github.archives[REPOSITORY, COMMIT] = _release("builtin_one", "builtin_one")
    # GitHub handles are compared without case
    github.issue(REGISTRY, ISSUE, body(move=MOVED), user={"login": "SomeOne"})
    _server_manifest(github, "builtin_one", ["@another", "@someone"])

    report = _prepare(files, github)

    assert _failed(report) == []
    assert report.moving
    assert [(finding.text, finding.detail) for finding in report.findings][2:4] == [
        (
            "The domain is a provider the server ships, which this submission moves here",
            "builtin_one",
        ),
        ("The submitter is a code owner of the provider in the server", "SomeOne"),
    ]
    # the check step reads the registry of the checkout, which moves the domain
    assert load_registry(files.root).reserved.moving == ["builtin_one", "moving_one"]
    plan = load_model(files.root / "work" / PLAN_FILE, CheckPlan)
    assert [(e.domain, e.version) for e in plan.channels["nightly"]] == [("builtin_one", "1.0.0")]
    passed, text = check_admission(
        load_registry(files.root),
        report,
        [_results("nightly", passed=True, domain="builtin_one")],
        proposal=Proposal(Outcome.OPENED, PULL),
    )
    assert passed
    assert (
        "This submission moves ` builtin_one ` out of the Music Assistant server. Once its pull "
        "request is merged, the team removes the provider from the server in the release whose "
        "catalog index offers it; the server's CI refuses the removal before that.\n\n- ✅"
    ) in text


@pytest.mark.parametrize(
    "codeowners", [["@another"], ["someone-else", 7], None], ids=["another", "junk", "none"]
)
def test_a_move_by_someone_who_is_no_code_owner(
    files: Files, github: FakeGitHub, codeowners: list[Any] | None
) -> None:
    """Only a code owner of the provider in the server moves it; its release is not checked."""
    github.archives[REPOSITORY, COMMIT] = _release("builtin_one", "builtin_one")
    github.issue(REGISTRY, ISSUE, body(move=MOVED))
    if codeowners is not None:
        _server_manifest(github, "builtin_one", codeowners)

    report = _prepare(files, github)

    assert _failed(report) == [
        (
            "The submitter is no code owner of the provider in the server, and only a code owner "
            "moves it here: ask one of them to submit it: someone"
        )
    ]
    assert load_model(files.root / "work" / PLAN_FILE, CheckPlan).channels == {}


def test_a_move_of_a_domain_the_server_does_not_ship(files: Files, github: FakeGitHub) -> None:
    """A move needs a provider the server ships; the server is not asked about another."""
    github.issue(REGISTRY, ISSUE, body(move=MOVED))

    report = _prepare(files, github)

    assert _failed(report) == [
        (
            "The domain is no provider the server ships, so there is nothing to move: untick the "
            f"box Moving from the server: {DOMAIN}"
        )
    ]
    assert not report.moving
    assert load_registry(files.root).reserved.moving == ["moving_one"]
    assert not [r for r in github.requests if "music-assistant/server" in r.full_url]


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
    results = {"channel": "nightly", "checked": "2026-11-15T03:00:00Z", "results": [result]}

    _, refused = check_admission(load_registry(files.root), report, [])
    _, failed = check_admission(
        load_registry(files.root), _passed(), [ChannelResults.from_dict(results)]
    )

    assert "is refused:\n\n  `````\n  providers/sample_music/versions/1.0.0.json: manifest" in (
        refused
    )
    assert refused.count("  `````\n") == 2
    assert f"- ❌ Failed on the nightly channel: ``` {reason} ```" in failed


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
    assert "Edit the issue to correct it; the checks then run again." in text
    assert text.endswith(f"[The run of these checks]({RUN_URL})\n")


def test_checks_that_could_not_run(files: Files) -> None:
    """Without the report of the prepare step the result says the checks could not run."""
    passed, text = check_admission(load_registry(files.root), None, [], run_url=RUN_URL)

    assert not passed
    assert text.startswith("## Admission checks\n\nThe checks could not finish.")
    assert f"[The run of these checks]({RUN_URL})" in text


@pytest.mark.parametrize(
    ("proposal", "failed", "expected"),
    [
        (Proposal(Outcome.OPENED, PULL), False, MERGE),
        (Proposal(Outcome.STANDS, PULL), False, MERGE),
        (Proposal(Outcome.CLOSED), False, "but the issue was closed, so the submission is"),
        (Proposal(Outcome.CHANGED), False, "but the issue changed while it was checked, so"),
        (
            Proposal(Outcome.CONFLICT, PULL),
            False,
            f"a conflict with [another pull request]({PULL})",
        ),
        (None, True, "but a step after the checks could not finish"),
        (None, False, "A test run from a branch opens no pull request."),
    ],
    ids=["opened", "stands", "closed", "changed", "conflict", "failed", "not on main"],
)
def test_the_result_says_what_became_of_the_pull_request(
    files: Files, proposal: Proposal | None, failed: bool, expected: str
) -> None:
    """Only a pull request that holds what the run checked is offered for merging."""
    results = [_results("nightly", passed=True), _results("beta", passed=True)]

    passed, text = check_admission(
        load_registry(files.root),
        _passed(),
        results,
        run_url=RUN_URL,
        proposal=proposal,
        proposal_failed=failed,
    )

    assert passed
    assert expected in text
    if failed:
        # the pull request may have been opened before the step failed
        assert f"[the run]({RUN_URL}) shows why" in text
        assert "could not be opened" not in text
    holds = proposal is not None and proposal.outcome in {Outcome.OPENED, Outcome.STANDS}
    assert ("decides by merging" in text) == holds


def test_a_comment_holds_the_result_up_to_its_limit() -> None:
    """A result too long for a comment on GitHub is replaced by where to find it."""
    result = "## Admission checks for ` sample_music `\n\n- ✅ The domain is free\n"
    long = result + "x" * MAX_COMMENT_SIZE

    assert admission_comment(result, RUN_URL) == result
    assert admission_comment(long, RUN_URL) == (
        "## Admission checks\n\nThe result is too long for a comment; it is in the summary of "
        f"[the run of these checks]({RUN_URL}).\n"
    )


def _open(github: FakeGitHub, issue: int = ISSUE) -> list[dict[str, Any]]:
    """Return the open pull requests of the bot for an issue."""
    return [
        pull
        for pull in github.pulls.get(REGISTRY, [])
        if pull["state"] == "open" and pull["head"]["ref"].endswith(f"-{issue}")
    ]


def _consistent(github: FakeGitHub, pull: dict[str, Any]) -> bool:
    """Return whether a pull request holds the provider.json and release its text names."""
    branch = pull["head"]["ref"]
    domain = branch.removeprefix("admit/").rsplit("-", 1)[0]
    content = github.files.get((REGISTRY, branch, f"providers/{domain}/provider.json"))
    release = re.search(
        r"release \[` (\S+) `\]\(\S+\) \(commit \[` ([0-9a-f]{40}) `\]", pull["body"]
    )
    marker = re.search(r"<!-- proposal: ([0-9a-f]{64}) -->", pull["body"])
    if content is None or release is None or marker is None:
        return False
    provider = ProviderFile.from_dict(json.loads(content))
    checked = AdmissionReport(
        checked="", findings=[], issue_digest="", tag=release[1], commit=release[2]
    )
    return (
        marker[1] == proposal_fingerprint(provider, checked)
        and f"from [` {provider.repository} `](https://github.com/{provider.repository})."
        in pull["body"]
    )


def test_propose_opens_a_pull_request(files: Files, github: FakeGitHub) -> None:
    """A passed submission gets a branch of its own from main with its provider.json."""
    proposal = _propose(files, github, _passed())

    branch = f"admit/{DOMAIN}-{ISSUE}"
    assert proposal == Proposal(Outcome.OPENED, f"https://github.com/{REGISTRY}/pull/100")
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
    assert re.match(
        rf"<!-- proposal: [0-9a-f]{{64}} -->\nCloses #{ISSUE}\n\nAdds "
        rf"` providers/{DOMAIN}/provider.json `: `` Sample `music` @a-maintainer `` from "
        rf"\[` {REPOSITORY} `\]\(https://github\.com/{REPOSITORY}\)\.\n\n",
        pull["body"],
    )
    assert "decides by merging this pull request;" in pull["body"]
    assert _consistent(github, pull)


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


def test_a_pull_request_that_holds_the_same_stands(files: Files, github: FakeGitHub) -> None:
    """A later run that checked the same provider.json and release changes nothing."""
    first = _propose(files, github, _passed())
    report = _passed()
    report.checked = "2026-11-17T12:00:00Z"
    assert report.provider is not None
    report.provider.added = report.checked
    requests = len(github.requests)

    assert _propose(files, github, report) == Proposal(Outcome.STANDS, first.url)

    assert all(request.get_method() == "GET" for request in github.requests[requests:])
    [pull] = github.pulls[REGISTRY]
    assert pull["state"] == "open"


@pytest.mark.parametrize(
    "change",
    [{"commit": "5" * 40}, {"tag": "v1.0.1", "version": "1.0.1"}, {"repository": "someone/new"}],
    ids=["commit", "release", "provider.json"],
)
def test_a_pull_request_is_never_changed(
    files: Files, github: FakeGitHub, change: dict[str, str]
) -> None:
    """Another release or provider.json closes the pull request, and a new one follows."""
    first = _propose(files, github, _passed())
    report = _passed()
    for key, value in change.items():
        setattr(report.provider if key == "repository" else report, key, value)

    second = _propose(files, github, report)

    assert second.outcome == Outcome.OPENED
    assert second.url != first.url
    old, new = github.pulls[REGISTRY]
    assert (old["state"], new["state"]) == ("closed", "open")
    [comment] = github.comments[REGISTRY, old["number"]]
    assert comment["body"] == (
        f"Closed: [this run]({RUN_URL}) checked #{ISSUE} again, with another result."
    )
    assert _consistent(github, new)


def _interrupting(github: FakeGitHub, writes: int) -> Transport:
    """Return a transport that stops the run after a number of writes, as a cancellation does."""
    done = 0

    def transport(request: Request) -> tuple[int, BinaryIO]:
        nonlocal done
        if done >= writes:
            raise _InterruptedError
        answer = github(request)
        done += request.get_method() != "GET"
        return answer

    return transport


class _InterruptedError(Exception):
    """The run was cancelled."""


def _changed_release(report: AdmissionReport) -> None:
    report.commit = "5" * 40


def _changed_repository(report: AdmissionReport) -> None:
    assert report.provider is not None
    report.provider.repository = report.repository = "another/ma-sample"
    report.issue_digest = issue_digest(body("another/ma-sample"))


def _changed_domain(report: AdmissionReport) -> None:
    assert report.provider is not None
    report.domain = report.provider.domain = report.provider.folder = "other_music"


@pytest.mark.parametrize(
    "change",
    [None, _changed_release, _changed_repository, _changed_domain, "closed"],
    ids=["new", "another release", "another repository", "another domain", "withdrawn"],
)
def test_an_interrupted_run_leaves_no_pull_request_that_disagrees(
    files: Files, change: Callable[[AdmissionReport], None] | str | None
) -> None:
    """Stopped after each write in turn, no open pull request differs from its own text."""

    def run(writes: int | None) -> tuple[FakeGitHub, list[str], AdmissionReport | None]:
        github = _github()
        if change is not None:
            _propose(files, github, _passed())
        report: AdmissionReport | None = _passed()
        if change == "closed":
            github.issue_answers[REGISTRY, ISSUE]["state"] = "closed"
            report = None
        elif callable(change):
            assert report is not None
            change(report)
            github.issue_answers[REGISTRY, ISSUE]["body"] = body(report.repository or "")
        first = len(github.requests)
        transport = github if writes is None else _interrupting(github, writes)
        try:
            _propose(files, transport, report)
        except _InterruptedError:
            assert writes is not None
        done = [
            f"{request.get_method()} {request.full_url.split('/repos/', 1)[1]}"
            for request in github.requests[first:]
            if request.get_method() != "GET"
        ]
        return github, done, report

    _, all_writes, _ = run(None)
    assert len(all_writes) >= 3
    for writes in range(len(all_writes) + 1):
        github, done, report = run(writes)
        # the run stopped right after this many writes
        assert done == all_writes[:writes]
        open_pulls = _open(github)
        assert len(open_pulls) <= 1, done
        assert all(_consistent(github, pull) for pull in open_pulls), done
        # the next run finishes what this one began, and leaves no branch of the issue unused
        _propose(files, github, report)
        open_pulls = _open(github)
        assert len(open_pulls) == (change != "closed"), done
        assert all(_consistent(github, pull) for pull in open_pulls), done
        branches = {branch for owner, branch in github.branches if branch.endswith(f"-{ISSUE}")}
        assert branches == {pull["head"]["ref"] for pull in open_pulls}, done


def test_a_stand_cannot_be_forged_by_the_text_of_a_submission(
    files: Files, github: FakeGitHub
) -> None:
    """A manifest name that carries the fingerprint of another submission makes nothing stand."""
    later = _passed()
    _changed_repository(later)
    assert later.provider is not None
    first = _passed()
    first.name = f"<!-- proposal: {proposal_fingerprint(later.provider, later)} -->"
    _propose(files, github, first)
    github.issue_answers[REGISTRY, ISSUE]["body"] = body("another/ma-sample")

    proposal = _propose(files, github, later)

    assert proposal.outcome == Outcome.OPENED
    old, new = github.pulls[REGISTRY]
    assert (old["state"], new["state"]) == ("closed", "open")
    assert _consistent(github, new)


def test_unused_branches_of_the_issue_are_deleted_first(files: Files, github: FakeGitHub) -> None:
    """Only branches admit/<domain>-<issue> of this issue that no open pull request uses go."""
    left = [f"admit/{DOMAIN}-{ISSUE}", f"admit/other_music-{ISSUE}"]
    kept = [
        f"admit/{DOMAIN}-{ISSUE}7",
        f"admit/{DOMAIN}-1{ISSUE}",
        f"admit/{DOMAIN}-{ISSUE}-x",
        f"admit/Sample-{ISSUE}",
        f"admit/third_music-{ISSUE}",
        f"other/{DOMAIN}-{ISSUE}",
    ]
    for branch in left + kept:
        github.branches[REGISTRY, branch] = "6" * 40
    # a branch of the issue that a person's open pull request uses stays
    github.pull(REGISTRY, f"admit/third_music-{ISSUE}", "theirs", user={"login": "a-maintainer"})
    github.issue_answers[REGISTRY, ISSUE]["state"] = "closed"

    _propose(files, github, None)

    remaining = {branch for owner, branch in github.branches if owner == REGISTRY}
    assert remaining == {*kept, "main"}
    deleted = [r.full_url for r in github.requests if r.get_method() == "DELETE"]
    assert sorted(url.rsplit("/heads/", 1)[1] for url in deleted) == sorted(left)


def test_propose_closes_the_pull_request_of_another_domain(
    files: Files, github: FakeGitHub
) -> None:
    """When the issue names another provider, its earlier pull request is closed."""
    _propose(files, github, _passed())
    report = _passed()
    _changed_domain(report)

    url = _propose(files, github, report).url

    first, second = github.pulls[REGISTRY]
    assert (first["state"], second["state"]) == ("closed", "open")
    assert second["html_url"] == url
    assert (REGISTRY, f"admit/{DOMAIN}-{ISSUE}") not in github.branches


@pytest.mark.parametrize(
    ("change", "outcome", "why"),
    [
        (
            {},
            Outcome.NOT_PASSED,
            f"Closed: the checks of #{ISSUE} did not pass in [this run]({RUN_URL}).",
        ),
        ({"state": "closed"}, Outcome.CLOSED, f"Closed: #{ISSUE} is closed."),
        (
            {"labels": []},
            Outcome.CHANGED,
            f"Closed: #{ISSUE} changed while [this run]({RUN_URL}) checked it.",
        ),
        (
            {"body": body("someone/another")},
            Outcome.CHANGED,
            f"Closed: #{ISSUE} changed while [this run]({RUN_URL}) checked it.",
        ),
    ],
    ids=["failed", "closed", "unlabelled", "edited"],
)
def test_propose_closes_its_pull_request(
    files: Files, github: FakeGitHub, change: dict[str, Any], outcome: Outcome, why: str
) -> None:
    """A run that fails, or an issue closed or changed meanwhile, closes the pull request."""
    _propose(files, github, _passed())
    github.issue_answers[REGISTRY, ISSUE].update(change)

    assert _propose(files, github, _passed(), passed=bool(change)) == Proposal(outcome)

    [pull] = github.pulls[REGISTRY]
    assert pull["state"] == "closed"
    [comment] = github.comments[REGISTRY, pull["number"]]
    assert comment["body"] == why
    assert (REGISTRY, f"admit/{DOMAIN}-{ISSUE}") not in github.branches
    assert github.files == {}


def test_a_closed_issue_gets_no_pull_request(files: Files, github: FakeGitHub) -> None:
    """A submission closed while it was checked is not proposed, however it passed."""
    github.issue_answers[REGISTRY, ISSUE]["state"] = "closed"

    assert _propose(files, github, _passed()) == Proposal(Outcome.CLOSED)
    assert _propose(files, github, None) == Proposal(Outcome.CLOSED)

    assert github.pulls.get(REGISTRY, []) == []
    assert (REGISTRY, f"admit/{DOMAIN}-{ISSUE}") not in github.branches


def test_closing_leaves_a_merged_pull_request_alone(files: Files, github: FakeGitHub) -> None:
    """Merging closes the issue; the merged pull request gets no line, its unused branch goes."""
    _propose(files, github, _passed())
    merged = github.pulls[REGISTRY][0]
    merged.update(state="closed", merged=True)
    github.issue_answers[REGISTRY, ISSUE]["state"] = "closed"
    before = dict(merged)

    _propose(files, github, None)

    assert github.comments == {}
    assert merged == before
    assert (REGISTRY, f"admit/{DOMAIN}-{ISSUE}") not in github.branches


def test_a_branch_another_pull_request_uses_is_left_alone(files: Files, github: FakeGitHub) -> None:
    """Nothing is written to the branch of the submission while a person's pull request uses it."""
    branch = f"admit/{DOMAIN}-{ISSUE}"
    github.branches[REGISTRY, branch] = "6" * 40
    theirs = github.pull(REGISTRY, branch, "their work", user={"login": "a-maintainer"})
    before = json.dumps(github.pulls)
    requests = len(github.requests)

    proposal = _propose(files, github, _passed())

    assert proposal == Proposal(Outcome.CONFLICT, theirs["html_url"])
    assert all(request.get_method() == "GET" for request in github.requests[requests:])
    assert json.dumps(github.pulls) == before
    assert github.branches[REGISTRY, branch] == "6" * 40


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
        assert f"\nCloses #{number}\n" in pull["body"]
        assert _consistent(github, pull)
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
        proposal=second,
    )
    assert f"Another submission for this domain is open: [#100]({first.url})." in text


def test_propose_leaves_other_pull_requests_alone(files: Files, github: FakeGitHub) -> None:
    """A pull request of a person, a fork or another issue is never closed by the bot."""
    github.pull(REGISTRY, f"admit/{DOMAIN}-{ISSUE}", "x", user={"login": "a-stranger"})
    fork = {"ref": f"admit/{DOMAIN}-{ISSUE}", "repo": {"full_name": "someone/fork"}}
    github.pull(REGISTRY, "x", "x", head=fork)
    github.pull(REGISTRY, f"admit/{DOMAIN}-8", "Closes #8\n")
    before = json.dumps(github.pulls)

    assert _propose(files, github, _passed(), passed=False) == Proposal(Outcome.NOT_PASSED)

    assert json.dumps(github.pulls) == before
    assert github.comments == {}


def test_propose_moves_a_provider_out_of_the_server(files: Files, github: FakeGitHub) -> None:
    """The pull request of a move adds its provider.json and its domain to the moving ones."""
    reserved = files.root / "reserved_domains.json"
    github.contents[REGISTRY, MAIN, "reserved_domains.json"] = reserved.read_bytes()

    _propose(files, github, _moved())

    branch = f"admit/builtin_one-{ISSUE}"
    provider = (REGISTRY, branch, "providers/builtin_one/provider.json")
    moving = (REGISTRY, branch, "reserved_domains.json")
    assert list(github.files) == [provider, moving]
    assert json.loads(github.files[moving]) == {
        **json.loads(reserved.read_bytes()),
        "moving": ["builtin_one", "moving_one"],
    }
    [pull] = github.pulls[REGISTRY]
    assert pull["title"] == "Admit builtin_one, moved from the server"
    assert set(github.messages.values()) == {pull["title"]}
    assert (
        pull["body"]
        .split("\n", 2)[2]
        .startswith(
            "\nMoves ` builtin_one ` out of the Music Assistant server: adds it to ` moving ` in "
            "` reserved_domains.json `.\n\nAdds ` providers/builtin_one/provider.json `: "
            f"` Sample music ` from [` {REPOSITORY} `](https://github.com/{REPOSITORY}).\n\n"
            "## Admission checks"
        )
    )
    assert "This submission moves ` builtin_one ` out of the" in pull["body"]
    assert _consistent(github, pull)
    # what CI validates in the pull request: main with both files; without the move it is refused
    reserved.write_bytes(github.files[moving])
    files.write("providers/builtin_one/provider.json", github.files[provider].decode())
    assert set(load_registry(files.root).providers) == {"builtin_one"}
    reserved.write_bytes(github.contents[REGISTRY, MAIN, "reserved_domains.json"])
    with pytest.raises(InvalidRegistryError, match="builtin_one is a provider the server ships"):
        load_registry(files.root)


def test_propose_a_move_main_lists_already(files: Files, github: FakeGitHub) -> None:
    """A domain main lists as moving already leaves reserved_domains.json as it is."""
    github.contents[REGISTRY, MAIN, "reserved_domains.json"] = (
        files.root / "reserved_domains.json"
    ).read_bytes()

    _propose(files, github, _moved("moving_one"))

    assert list(github.files) == [
        (REGISTRY, f"admit/moving_one-{ISSUE}", "providers/moving_one/provider.json")
    ]


def test_propose_refuses_what_main_cannot_take(files: Files, github: FakeGitHub) -> None:
    """A provider main has already is not proposed."""
    files.provider()

    with pytest.raises(CommandError, match="is in the community catalog already"):
        _propose(files, github, _passed())

    assert github.files == {}


def test_propose_writes_the_outputs_of_its_job(
    files: Files, github: FakeGitHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The command adds its outcome, its pull request and those of the others to the outputs."""
    monkeypatch.setenv("GITHUB_TOKEN", "a-token")
    monkeypatch.setattr("registry.cli.GitHub", lambda token: GitHub(token, github))
    github.pull(REGISTRY, f"admit/{DOMAIN}-8", "Closes #8\n")
    files.write("work/admission.json", _passed().to_dict())
    entry = [{"domain": DOMAIN, "version": "1.0.0"}]
    files.write("work/plan.json", {"channels": {"nightly": entry}})
    files.write(f"results/nightly/{DOMAIN}-1.0.0.json", _results("nightly", passed=True).to_dict())
    output = files.root / "output"
    args = ["--root", str(files.root), "propose-admission", str(files.root / "results"),
            "--repository", REGISTRY, "--issue", str(ISSUE), "--author", BOT_LOGIN,
            "--run-url", RUN_URL, "--output", str(output)]  # fmt: skip

    assert main(args) == 0
    assert output.read_text() == (
        f"outcome=opened\nurl=https://github.com/{REGISTRY}/pull/101\n"
        f"others=https://github.com/{REGISTRY}/pull/100\n"
    )
    github.issue_answers[REGISTRY, ISSUE]["labels"] = []
    assert main(args) == 0
    assert output.read_text().endswith("outcome=changed\nurl=\nothers=\n")
    assert github.pulls[REGISTRY][1]["state"] == "closed"
