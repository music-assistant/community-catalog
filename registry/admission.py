"""The admission checks of a provider submitted with the issue form, and its pull request."""

from __future__ import annotations

import hashlib
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from registry import formats
from registry.checks import check_provider, reserved_reason
from registry.edit import CommandError, now
from registry.fetch import newest_release, repository_state, take_release
from registry.files import dumps
from registry.formats import format_date, release_version
from registry.github import ArchiveTooLargeError, GitHubError
from registry.markdown import code_block, code_span
from registry.models import AdmissionReport, CheckPlan, Finding, ProviderFile
from registry.pipeline import make_plan, write_plan
from registry.registry import PROVIDER_FILE, PROVIDERS_FOLDER, InvalidRegistryError, load_registry
from registry.submission import (
    SUBMISSION_LABEL,
    Submission,
    SubmissionError,
    find_provider,
    parse_submission,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from registry.github import GitHub, IssueInfo, PullRequest, Release, RepositoryInfo
    from registry.models import LineResults
    from registry.registry import Registry


@dataclass(frozen=True, slots=True)
class Proposal:
    """The pull request of a submission."""

    # None when there is none
    url: str | None
    # the open pull requests of other submissions of the same domain
    others: list[str]


# what the comment with the result on the issue starts with, to find it again
COMMENT_MARKER = "<!-- admission-checks -->"
# well below the 65536 characters GitHub allows in a comment or the body of a pull request
MAX_COMMENT_SIZE = 60_000
RULES_URL = "https://github.com/music-assistant/.github/blob/main/COMMUNITY_CATALOG_RULES.md"
FORM_URL = (
    "https://github.com/music-assistant/community-catalog/issues/new?template=submit-provider.yml"
)
# the branch of the pull request of a submission is this, its domain and its issue; a domain
# holds no hyphen
BRANCH_PREFIX = "admit/"
BASE_BRANCH = "main"
_BRANCH = re.compile(rf"{BRANCH_PREFIX}([a-z][a-z0-9_]*)-([1-9][0-9]*)")
_TITLE = "## Admission checks"


def prepare_admission(
    root: Path, *, repository: str, issue: int, github: GitHub, work: Path
) -> AdmissionReport:
    """
    Run the admission checks of a submission that need GitHub, and take its latest release.

    The issue is read through the API, as data. The provider.json the submission leads to and
    the version file of the latest release are written into the registry at root, a scratch
    checkout, and the plan of the check step into the work folder. An issue that is no valid
    submission gets a report with the refusal, and nothing is checked.

    :param root: The root folder of the registry, a checkout of the commit the workflow runs.
    :param repository: The repository of the registry, which holds the issue, as owner/name.
    :param issue: The number of the issue.
    :param github: The GitHub client.
    :param work: The work folder, which keeps the archive of the release.
    :raises CommandError: The author of the issue is not a GitHub handle.
    :raises GitHubError: GitHub could not answer.
    """
    found = github.issue(repository, issue)
    report = AdmissionReport(
        checked=format_date(now()), findings=[], issue_digest=issue_digest(found.body)
    )
    if not formats.is_github_handle(found.author):
        msg = f"{found.author!r} is not a GitHub handle"
        raise CommandError(msg)
    try:
        report.findings = _prepare(root, report, found, issue=issue, github=github, work=work)
    except SubmissionError as err:
        report.refusal = str(err)
    # the release of a submission that failed a requirement is not checked: its code never runs
    admitted = report.version is not None and all(finding.passed for finding in report.findings)
    write_plan(make_plan(load_registry(root), work) if admitted else CheckPlan(lines={}), work)
    return report


def check_admission(
    registry: Registry,
    report: AdmissionReport | None,
    results: list[LineResults],
    *,
    run_url: str | None = None,
    pull_request: str | None = None,
    proposal_failed: bool = False,
    others: Sequence[str] = (),
) -> tuple[bool, str]:
    """
    Return whether every admission check passed, and the result in Markdown.

    The result is for the submitter and for the registry maintainer who decides: what was
    checked, and what happens next. The latest release passes when it passed on at least one
    server line that is tested. Everything the issue, the repository or the release holds is
    shown as code.

    :param registry: The registry, for its server lines.
    :param report: The admission checks of the prepare step, None when they could not run.
    :param results: The results of the check step, as read_results returns them.
    :param run_url: The address of the workflow run, to link to.
    :param pull_request: The address of the pull request that adds the provider, when there is
        one.
    :param proposal_failed: Whether opening that pull request failed.
    :param others: The open pull requests of other submissions of the same domain.
    """
    link = f"\n[The run of these checks]({run_url})\n" if run_url else ""
    if report is None:
        text = (
            "The admission checks could not run to the end this time. The run shows why; a "
            "registry maintainer starts them again."
        )
        return False, f"{_TITLE}\n\n{text}\n{link}"
    what = _what(report)
    if report.refusal is not None or report.domain is None:
        intro = "This issue is not a valid submission, so nothing was checked:"
        advice = (
            "Edit the issue to fix it, and the checks run again by themselves. The "
            f"[form]({FORM_URL}) says what each field holds."
        )
        refusal = code_block(report.refusal or "")
        return False, f"{_TITLE}\n\n{what}{intro}\n\n{refusal}\n{advice}\n{link}"
    findings = list(report.findings)
    passed_on_a_line = False
    # without every requirement met, the release is not checked on any line
    if report.version is not None and all(finding.passed for finding in report.findings):
        by_line = {
            file.line: (result.passed, result.reason)
            for file in results
            for result in file.results
            if (result.domain, result.version) == (report.domain, report.version)
        }
        for line, config in sorted(registry.server_lines.items()):
            if config.frozen:
                continue
            if line not in by_line:
                findings.append(Finding(passed=False, text=f"{line}: the checks did not run"))
                continue
            passed, reason = by_line[line]
            text = f"{line}: passed" if passed else f"{line}: failed"
            findings.append(Finding(passed=passed, text=text, detail=reason))
            passed_on_a_line = passed_on_a_line or passed
    passed = passed_on_a_line and all(finding.passed for finding in report.findings)
    rows = "".join(_row(finding) for finding in findings)
    if passed and proposal_failed:
        verdict = (
            "Every check passed, but the pull request that adds the provider could not be "
            "opened. The run shows why; a registry maintainer looks into it."
        )
    elif passed:
        adds = f"[the pull request that adds it]({pull_request})" if pull_request else None
        verdict = (
            "Every check passed. A registry maintainer reviews the provider against the "
            f"[rules of the community catalog]({RULES_URL}) and decides by merging "
            f"{adds or 'the pull request that adds it'}; there is nothing more for you to do."
        )
        if others:
            links = ", ".join(f"[#{url.rsplit('/', 1)[-1]}]({url})" for url in others)
            verdict += (
                f"\n\nAnother submission for this domain has an open pull request: {links}. The "
                "registry maintainer decides between them."
            )
    else:
        verdict = (
            "Not every check passed. Fix what failed, then run the checks again: edit this "
            "issue, or close and reopen it, for example after you published a new release."
        )
    title = f"{_TITLE} for {code_span(report.domain)}"
    return passed, f"{title}\n\n{what}{rows}\n{verdict}\n{link}"


def admission_comment(result: str, run_url: str | None = None) -> str:
    """
    Return the result of check_admission as a comment or as the body of a pull request.

    A result too long for GitHub is replaced by where to find it.

    :param result: The result in Markdown.
    :param run_url: The address of the workflow run, whose summary holds the result.
    """
    if len(result) <= MAX_COMMENT_SIZE:
        return result
    run = f"[the run of these checks]({run_url})" if run_url else "the run of these checks"
    return f"{_TITLE}\n\nThe result is too long for a comment; it is in the summary of {run}.\n"


def propose_admission(
    registry: Registry,
    report: AdmissionReport | None,
    results: list[LineResults],
    *,
    repository: str,
    issue: int,
    author: str,
    run_url: str,
    github: GitHub,
) -> Proposal:
    """
    Open or update the pull request that adds a provider whose checks passed, or close it.

    The pull request of an issue comes from its own branch, admit/<domain>-<issue>, made afresh
    from main with only the provider.json added, and closes the issue when it is merged. It is
    opened only while the issue is open, labelled and holds the text that was checked. Otherwise,
    and when the checks did not pass, the open pull request of the issue is closed with a line
    that says why, and its branch deleted.

    :param registry: The registry of main.
    :param report: The admission checks of the prepare step, None when they did not run.
    :param results: The results of the check step, as read_results returns them.
    :param repository: The repository of the registry, as owner/name.
    :param issue: The number of the issue of the submission.
    :param author: The account of the token, which opens the pull requests.
    :param run_url: The address of the workflow run.
    :param github: A client whose token may push branches, write pull requests and read issues.
    :raises CommandError: The provider cannot be added to main.
    :raises GitHubError: GitHub refused a request.
    """
    found = github.issue(repository, issue)
    pulls = [
        pull for pull in github.pull_requests(repository) if pull.head_repository == repository
    ]
    ours = [pull for pull in pulls if pull.author == author and _branch_issue(pull.branch) == issue]
    passed, result = check_admission(registry, report, results, run_url=run_url)
    unchanged = (
        report is not None
        and report.issue_digest == issue_digest(found.body)
        and SUBMISSION_LABEL in found.labels
        and not found.pull_request
    )
    if not found.open:
        why = f"Closed: #{issue} is closed."
    elif report is None:
        why = f"Closed: the checks of #{issue} could not run in [this run]({run_url})."
    elif not unchanged:
        why = f"Closed: #{issue} changed while [this run]({run_url}) checked it."
    else:
        why = f"Closed: the checks of #{issue} did not pass in [this run]({run_url})."
    if not (found.open and unchanged and passed) or report is None or report.provider is None:
        for pull in ours:
            _close(github, repository, pull, why)
        return Proposal(url=None, others=[])
    provider = report.provider
    _check_proposal(registry, provider)
    if (base := github.branch_commit(repository, BASE_BRANCH)) is None:
        msg = f"{repository} has no branch {BASE_BRANCH}"
        raise GitHubError(msg)
    # the domain is validated; nothing a submission writes reaches a title or a commit message
    title = f"Admit {provider.domain}"
    branch = f"{BRANCH_PREFIX}{provider.domain}-{issue}"
    path = f"{PROVIDERS_FOLDER}/{provider.domain}/{PROVIDER_FILE}"
    github.set_branch(repository, branch, base)
    github.put_file(repository, branch, path, dumps(provider.to_dict()), title)
    name = code_span(report.name or provider.domain)
    body = (
        f"Closes #{issue}\n\nAdds {code_span(path)}: {name} from "
        f"{code_span(provider.repository)}.\n\n{admission_comment(result, run_url)}"
    )
    current = next((pull for pull in ours if pull.branch == branch), None)
    if current is None:
        url = github.create_pull_request(
            repository, title=title, branch=branch, base=BASE_BRANCH, body=body
        )
    else:
        github.update_pull_request(repository, current.number, title=title, body=body)
        url = current.url
    for pull in ours:
        if pull is not current:
            _close(github, repository, pull, f"Closed: #{issue} names another provider, {url}.")
    others = [
        pull.url
        for pull in pulls
        if pull not in ours and _branch_domain(pull.branch) == provider.domain
    ]
    return Proposal(url=url, others=others)


def issue_digest(body: str) -> str:
    """
    Return the digest of the text of an issue, to tell whether it changed.

    :param body: The text of the issue.
    """
    return hashlib.sha256(body.encode()).hexdigest()


def _prepare(
    root: Path,
    report: AdmissionReport,
    found: IssueInfo,
    *,
    issue: int,
    github: GitHub,
    work: Path,
) -> list[Finding]:
    """
    Return the findings of a submission, and add what was checked to its report.

    :param root: The root folder of the registry, a scratch checkout.
    :param report: The report, which gets the repository, release and provider checked.
    :param found: The issue of the submission.
    :param issue: The number of the issue.
    :param github: The GitHub client.
    :param work: The work folder, which keeps the archive of the release.
    :raises SubmissionError: The issue is no valid submission.
    :raises GitHubError: GitHub could not answer.
    """
    info, release, submission, folder = _locate(report, found, issue=issue, github=github)
    assert report.domain is not None
    domain, prefix = report.domain, submission.tag_prefix
    version = release_version(release.tag, prefix)
    assert version is not None
    state = repository_state(info)
    findings = [
        Finding(
            passed=not state.private and not state.archived,
            text="The repository is public and "
            + ("archived" if state.archived else "not archived"),
            detail=info.name,
        ),
        Finding(
            passed=state.license not in {None, "other"},
            text="A licence GitHub recognises",
            detail=state.license or "none",
        ),
    ]
    registry = load_registry(root)
    if domain in registry.providers:
        text = "The domain is taken by a provider in the community catalog"
        return [*findings, Finding(passed=False, text=text, detail=domain)]
    if reason := reserved_reason(domain, registry.reserved):
        findings.append(Finding(passed=False, text=f"The domain is {reason}", detail=domain))
    else:
        findings.append(Finding(passed=True, text="The domain is free", detail=domain))
    candidate = ProviderFile(
        domain=domain,
        repository=info.name,
        repository_id=info.repository_id,
        owner_id=info.owner_id,
        folder=folder,
        added=report.checked,
        tag_prefix=prefix,
    )
    target = root / PROVIDERS_FOLDER / domain / PROVIDER_FILE
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(dumps(candidate.to_dict()))
    try:
        registry = load_registry(root)
    except InvalidRegistryError as err:
        return findings + [
            Finding(passed=False, text="Not valid", detail=problem) for problem in err.problems
        ]
    report.provider = candidate
    findings.append(Finding(passed=True, text=f"{PROVIDER_FILE} is valid"))
    provider = registry.providers[domain]
    take_release(registry, github, work, provider, release)
    codeowners: list[str] = []
    if (refused := provider.refused.get(version)) is not None:
        text = f"The latest release, version {version}, is refused"
        findings.append(Finding(passed=False, text=text, detail=refused.reason))
    else:
        text = f"The latest release is version {version}"
        findings.append(Finding(passed=True, text=text, detail=release.tag))
        manifest = provider.versions[version].manifest
        report.version, report.name = version, manifest["name"]
        codeowners = manifest.get("codeowners", [])
    owners = {state.owner.lower(), *(owner.removeprefix("@").lower() for owner in codeowners)}
    findings.append(
        Finding(
            passed=found.author.lower() in owners,
            text="The submitter owns the repository or is a code owner in the manifest of the "
            "latest release",
            detail=found.author,
        )
    )
    return findings


def _locate(
    report: AdmissionReport, found: IssueInfo, *, issue: int, github: GitHub
) -> tuple[RepositoryInfo, Release, Submission, str]:
    """
    Return the repository, latest release, form and provider folder of a submission.

    The report gets the repository, the release and the domain.

    :param report: The report of the submission.
    :param found: The issue of the submission.
    :param issue: The number of the issue.
    :param github: The GitHub client.
    :raises SubmissionError: The issue is no valid submission.
    :raises GitHubError: GitHub could not answer.
    """
    if SUBMISSION_LABEL not in found.labels or found.pull_request:
        msg = f"#{issue} is no issue with the label {SUBMISSION_LABEL}"
        raise SubmissionError(msg)
    if not found.open:
        msg = f"#{issue} is closed"
        raise SubmissionError(msg)
    submission = parse_submission(found.body)
    info = github.repositories([submission.repository])[submission.repository]
    if isinstance(info, GitHubError):
        raise info
    if info is None:
        msg = f"{submission.repository} does not exist or is not public"
        raise SubmissionError(msg)
    report.repository = info.name
    prefix = submission.tag_prefix
    if (release := newest_release(info.releases, prefix)) is None:
        msg = f"{info.name} has no published release with a tag such as {prefix}v1.0.0"
        raise SubmissionError(msg)
    report.tag, report.commit = release.tag, release.commit
    # the domain names the place of the archive in the work folder, so take_release downloads
    # it again there
    with tempfile.TemporaryDirectory() as scratch:
        try:
            downloaded = Path(scratch, "release.tar.gz")
            github.download_archive(info.name, release.commit, downloaded)
            folder, report.domain = find_provider(downloaded, submission.folder)
        except (ArchiveTooLargeError, SubmissionError) as err:
            msg = f"release {release.tag}: {err}"
            raise SubmissionError(msg) from err
    return info, release, submission, folder


def _check_proposal(registry: Registry, provider: ProviderFile) -> None:
    """
    Refuse to propose a provider main cannot take.

    :param registry: The registry of main.
    :param provider: The provider.json to add.
    :raises CommandError: The provider cannot be added.
    """
    problems = check_provider(provider, registry.reserved)
    if provider.domain in registry.providers:
        problems.append(f"domain: {provider.domain} is in the community catalog already")
    if problems:
        raise CommandError("; ".join(problems))


def _close(github: GitHub, repository: str, pull: PullRequest, why: str) -> None:
    """Close a pull request of the bot with a comment that says why, and delete its branch."""
    github.add_comment(repository, pull.number, why)
    github.update_pull_request(repository, pull.number, state="closed")
    github.delete_branch(repository, pull.branch)


def _branch_issue(branch: str) -> int | None:
    """Return the issue a branch of a pull request of the bot belongs to, None for another."""
    found = _BRANCH.fullmatch(branch)
    return int(found[2]) if found else None


def _branch_domain(branch: str) -> str | None:
    """Return the domain a branch of a pull request of the bot proposes, None for another."""
    found = _BRANCH.fullmatch(branch)
    return found[1] if found else None


def _what(report: AdmissionReport) -> str:
    """Return a paragraph that says what was checked and when, as far as the checks got."""
    parts = [f"repository {code_span(report.repository)}"] if report.repository else []
    if report.tag is not None and report.commit is not None:
        parts.append(f"release {code_span(report.tag)} (commit {code_span(report.commit)})")
    return f"Checked {report.checked}: {', '.join(parts) or 'the issue'}.\n\n"


def _row(finding: Finding) -> str:
    """Return a finding as an item of a Markdown list, with its detail as code."""
    mark = "✅" if finding.passed else "❌"
    if finding.detail is None:
        return f"- {mark} {finding.text}\n"
    if len(finding.detail.splitlines()) > 1:
        return f"- {mark} {finding.text}:\n\n{code_block(finding.detail, '  ')}\n"
    return f"- {mark} {finding.text}: {code_span(finding.detail)}\n"
