"""The admission checks of a provider submitted with the issue form, and its pull request."""

from __future__ import annotations

import hashlib
import re
import tempfile
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

from registry import formats
from registry.checks import check_provider, reserved_reason
from registry.content import MANIFEST_FILE
from registry.edit import CommandError, now
from registry.fetch import newest_release, repository_state, take_release
from registry.files import dumps, loads, parse_model
from registry.formats import format_date, is_commit, release_version
from registry.github import ArchiveTooLargeError, GitHubError
from registry.markdown import code_block, code_span, github_link
from registry.models import AdmissionReport, CheckPlan, Finding, ProviderFile, ReservedDomains
from registry.pipeline import make_plan, write_plan
from registry.registry import (
    PROVIDER_FILE,
    PROVIDERS_FOLDER,
    RESERVED_DOMAINS_FILE,
    InvalidRegistryError,
    load_registry,
)
from registry.reserved import add_moving
from registry.submission import (
    MOVE_FIELD,
    SUBMISSION_LABEL,
    Submission,
    SubmissionError,
    find_provider,
    parse_submission,
)

if TYPE_CHECKING:
    from registry.github import GitHub, IssueInfo, PullRequest, Release, RepositoryInfo
    from registry.models import ChannelResults
    from registry.registry import Registry


class Outcome(StrEnum):
    """What became of the pull request of a submission in a run."""

    # a new pull request holds what this run checked
    OPENED = "opened"
    # the open pull request holds what this run checked already
    STANDS = "stands"
    # the checks did not pass, so nothing is proposed
    NOT_PASSED = "not-passed"
    # the issue was closed, or its text or label changed, while it was checked
    CLOSED = "closed"
    CHANGED = "changed"
    # another pull request uses the branch of the submission
    CONFLICT = "conflict"


@dataclass(frozen=True, slots=True)
class Proposal:
    """The pull request of a submission after a run."""

    outcome: Outcome
    # the pull request that holds what this run checked, or for a conflict the other one
    url: str | None = None
    # the open pull requests of other submissions of the same domain
    others: list[str] = field(default_factory=list)


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
# where the code owners of a provider the server ships are read, for a move out of the server
SERVER_REPOSITORY = "music-assistant/server"
SERVER_BRANCH = "dev"
_BRANCH = re.compile(rf"{BRANCH_PREFIX}([a-z][a-z0-9_]*)-([1-9][0-9]*)")
_TITLE = "## Admission checks"
# what happens next for a submission whose checks passed and that gets no pull request
_NOT_PROPOSED = {
    Outcome.CLOSED: (
        "Every check passed, but the issue was closed, so the submission is withdrawn."
    ),
    Outcome.CHANGED: (
        "Every check passed, but the issue changed while it was checked, so this result does "
        "not count."
    ),
}


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
    write_plan(make_plan(load_registry(root), work) if admitted else CheckPlan(channels={}), work)
    return report


def check_admission(
    registry: Registry,
    report: AdmissionReport | None,
    results: list[ChannelResults],
    *,
    run_url: str | None = None,
    proposal: Proposal | None = None,
    proposal_failed: bool = False,
) -> tuple[bool, str]:
    """
    Return whether every admission check passed, and the result in Markdown.

    The result is for the submitter and for the registry maintainer who decides: what was
    checked, and what happens next. The latest release passes when it passed on at least one
    channel that is tested. Everything the issue, the repository or the release holds is
    shown as code; the repository, the release and the commit link to GitHub.

    :param registry: The registry, for its channels.
    :param report: The admission checks of the prepare step, None when they could not run.
    :param results: The results of the check step, as read_results returns them.
    :param run_url: The address of the workflow run, to link to.
    :param proposal: What became of the pull request of the submission in this run; None when
        no run on main took care of it.
    :param proposal_failed: Whether the job that takes care of the pull request failed.
    """
    link = f"\n[The run of these checks]({run_url})\n" if run_url else ""
    if report is None:
        text = (
            "The checks could not finish. A registry maintainer runs them again; there is "
            "nothing for you to do."
        )
        return False, f"{_TITLE}\n\n{text}\n{link}"
    what = _what(report)
    if report.refusal is not None or report.domain is None:
        intro = "This issue is not a valid submission, so nothing was checked:"
        advice = (
            "Edit the issue to correct it; the checks then run again. The "
            f"[form]({FORM_URL}) says what each field holds."
        )
        refusal = code_block(report.refusal or "")
        return False, f"{_TITLE}\n\n{what}{intro}\n\n{refusal}\n{advice}\n{link}"
    rows = [_row(finding) for finding in report.findings]
    passed_on_a_channel = False
    # without every requirement met, the release is not checked on any channel
    if report.version is not None and all(finding.passed for finding in report.findings):
        by_channel = {
            file.channel: (result.passed, result.reason)
            for file in results
            for result in file.results
            # a check that could not run in the server image is no verdict on the release
            if (result.domain, result.version) == (report.domain, report.version)
            and not result.unavailable
        }
        for channel, config in sorted(registry.channels.items()):
            if config.frozen:
                continue
            name = formats.channel_name(channel)
            if channel not in by_channel:
                rows.append(f"- ⚪ Not checked on {name}\n")
                continue
            passed, reason = by_channel[channel]
            text = f"Passed on {name}" if passed else f"Failed on {name}"
            rows.append(_row(Finding(passed=passed, text=text, detail=reason)))
            passed_on_a_channel = passed_on_a_channel or passed
    passed = passed_on_a_channel and all(finding.passed for finding in report.findings)
    if passed:
        verdict = _proposal_verdict(proposal, proposal_failed=proposal_failed, run_url=run_url)
    else:
        verdict = (
            "Not every check passed. Fix what failed, then edit this issue, or close and reopen "
            "it after you publish a new release; the checks then run again."
        )
    title = f"{_TITLE} for {code_span(report.domain)}"
    if report.moving:
        what += f"{_move_note(report.domain)}\n\n"
    return passed, f"{title}\n\n{what}{''.join(rows)}\n{verdict}\n{link}"


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
    results: list[ChannelResults],
    *,
    repository: str,
    issue: int,
    author: str,
    run_url: str,
    github: GitHub,
) -> Proposal:
    """
    Open the pull request that adds a provider whose checks passed, or close it.

    A pull request is never changed: it holds what one run checked. Its branch,
    admit/<domain>-<issue>, is made from main with only the provider.json, and the pull request
    is opened last, so an interrupted run leaves no pull request whose file and text disagree.
    An open pull request of the issue that holds the same provider.json and release stands;
    any other is closed with a line that says why and its branch deleted, before a new one is
    made. A pull request is proposed only while the issue is open, labelled and holds the text
    that was checked, and never on a branch another pull request uses. A branch of the issue
    that no open pull request uses is deleted first.

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
    _delete_unused_branches(github, repository, issue, pulls)
    passed = check_admission(registry, report, results)[0]
    unchanged = (
        report is not None
        and report.issue_digest == issue_digest(found.body)
        and SUBMISSION_LABEL in found.labels
        and not found.pull_request
    )
    if not found.open:
        outcome, why = Outcome.CLOSED, f"Closed: #{issue} is closed."
    elif report is None:
        outcome = Outcome.NOT_PASSED
        why = f"Closed: the checks of #{issue} could not run in [this run]({run_url})."
    elif not unchanged:
        outcome = Outcome.CHANGED
        why = f"Closed: #{issue} changed while [this run]({run_url}) checked it."
    else:
        outcome = Outcome.NOT_PASSED
        why = f"Closed: the checks of #{issue} did not pass in [this run]({run_url})."
    if not (found.open and unchanged and passed) or report is None or report.provider is None:
        for pull in ours:
            _close(github, repository, pull, why)
        return Proposal(outcome)
    provider = report.provider
    _check_proposal(registry, provider, moving=report.moving)
    branch = f"{BRANCH_PREFIX}{provider.domain}-{issue}"
    others = [
        pull.url
        for pull in pulls
        if pull not in ours
        and pull.branch != branch
        and _branch_domain(pull.branch) == provider.domain
    ]
    # nothing is written to a branch another pull request uses
    if taken := [pull for pull in pulls if pull.branch == branch and pull not in ours]:
        return Proposal(Outcome.CONFLICT, taken[0].url, others)
    marker = f"<!-- proposal: {proposal_fingerprint(provider, report)} -->"
    # the body quotes text of the submission, so only its first line tells what it holds
    standing = next(
        (p for p in ours if p.branch == branch and p.body.split("\n", 1)[0] == marker), None
    )
    for pull in ours:
        if pull is not standing:
            why = f"Closed: [this run]({run_url}) checked #{issue} again, with another result."
            _close(github, repository, pull, why)
    if standing is not None:
        return Proposal(Outcome.STANDS, standing.url, others)
    if (base := github.branch_commit(repository, BASE_BRANCH)) is None:
        msg = f"{repository} has no branch {BASE_BRANCH}"
        raise GitHubError(msg)
    # the domain is validated; nothing a submission writes reaches a title or a commit message
    title = f"Admit {provider.domain}" + (", moved from the server" if report.moving else "")
    path = f"{PROVIDERS_FOLDER}/{provider.domain}/{PROVIDER_FILE}"
    github.set_branch(repository, branch, base)
    github.put_file(repository, branch, path, dumps(provider.to_dict()), title)
    if report.moving:
        _propose_moving(github, repository, branch, base, provider.domain, title)
    result = check_admission(
        registry, report, results, run_url=run_url, proposal=Proposal(Outcome.OPENED)
    )[1]
    what = _proposed(report, provider)
    body = f"{marker}\nCloses #{issue}\n\n{what}\n\n{admission_comment(result, run_url)}"
    url = github.create_pull_request(
        repository, title=title, branch=branch, base=BASE_BRANCH, body=body
    )
    return Proposal(Outcome.OPENED, url, others)


def proposal_fingerprint(provider: ProviderFile, report: AdmissionReport) -> str:
    """
    Return what tells whether a pull request holds a provider.json and the release checked.

    The date the provider.json gives as added does not count, since every run sets it.

    :param provider: The provider.json the pull request adds.
    :param report: The report of the run, with the release it checked.
    """
    held = {key: value for key, value in provider.to_dict().items() if key != "added"}
    content = dumps({"provider": held, "tag": report.tag, "commit": report.commit})
    return hashlib.sha256(content).hexdigest()


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
    findings += _domain_findings(
        root, registry, report, move=submission.move, author=found.author, github=github
    )
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


def _domain_findings(
    root: Path,
    registry: Registry,
    report: AdmissionReport,
    *,
    move: bool,
    author: str,
    github: GitHub,
) -> list[Finding]:
    """
    Return whether a submission may take its domain, and mark a move out of the server.

    A move is written into the reserved domains of the registry at root.

    :param root: The root folder of the registry, a scratch checkout.
    :param registry: The registry at root.
    :param report: The report, with the domain; it gets whether the submission is a move.
    :param move: Whether the box of a move out of the server is ticked.
    :param author: The submitter, a GitHub handle.
    :param github: The GitHub client.
    :raises GitHubError: GitHub could not answer.
    """
    domain = report.domain
    assert domain is not None
    builtin = domain in registry.reserved.builtin_providers
    if builtin and move:
        report.moving = True
        # the registry the provider.json is validated in, and the check step loads from, holds
        # the move as its pull request does
        reserved = add_moving(registry.reserved, domain)
        (root / RESERVED_DOMAINS_FILE).write_bytes(dumps(reserved.to_dict()))
        text = "The domain is a provider the server ships, which this submission moves here"
        return [
            Finding(passed=True, text=text, detail=domain),
            _code_owner_finding(github, domain, author),
        ]
    if builtin:
        text = (
            "The domain is a provider the server ships; only a code owner of it in the server "
            f"moves it here, with the box {MOVE_FIELD}"
        )
        return [Finding(passed=False, text=text, detail=domain)]
    if move:
        text = (
            "The domain is no provider the server ships, so there is nothing to move: untick "
            f"the box {MOVE_FIELD}"
        )
        return [Finding(passed=False, text=text, detail=domain)]
    if reason := reserved_reason(domain, registry.reserved):
        return [Finding(passed=False, text=f"The domain is {reason}", detail=domain)]
    return [Finding(passed=True, text="The domain is free", detail=domain)]


def _code_owner_finding(github: GitHub, domain: str, author: str) -> Finding:
    """
    Return whether the submitter of a move is a code owner of the provider in the server.

    :param github: The GitHub client.
    :param domain: The domain of the provider the server ships.
    :param author: The submitter, a GitHub handle.
    :raises GitHubError: GitHub could not answer.
    """
    path = f"music_assistant/providers/{domain}/{MANIFEST_FILE}"
    codeowners: list[object] = []
    if (found := github.file(SERVER_REPOSITORY, path, SERVER_BRANCH)) is not None:
        try:
            manifest = loads(found[0])
        except ValueError:
            manifest = None
        if isinstance(manifest, dict) and isinstance(manifest.get("codeowners"), list):
            codeowners = manifest["codeowners"]
    owners = {owner.removeprefix("@").lower() for owner in codeowners if isinstance(owner, str)}
    if author.lower() in owners:
        text = "The submitter is a code owner of the provider in the server"
        return Finding(passed=True, text=text, detail=author)
    text = (
        "The submitter is no code owner of the provider in the server, and only a code owner "
        "moves it here: ask one of them to submit it"
    )
    return Finding(passed=False, text=text, detail=author)


def _check_proposal(registry: Registry, provider: ProviderFile, *, moving: bool) -> None:
    """
    Refuse to propose a provider main cannot take.

    :param registry: The registry of main.
    :param provider: The provider.json to add.
    :param moving: Whether it moves a provider the server ships.
    :raises CommandError: The provider cannot be added.
    """
    reserved = registry.reserved
    if moving:
        reserved = add_moving(reserved, provider.domain)
    problems = check_provider(provider, reserved)
    if moving and provider.domain not in reserved.builtin_providers:
        problems.append(f"domain: {provider.domain} is no provider the server ships")
    if provider.domain in registry.providers:
        problems.append(f"domain: {provider.domain} is in the community catalog already")
    if problems:
        raise CommandError("; ".join(problems))


def _propose_moving(
    github: GitHub, repository: str, branch: str, base: str, domain: str, message: str
) -> None:
    """
    Commit the domain of a provider the server ships as moving, unless main lists it already.

    :param github: The GitHub client.
    :param repository: The repository of the registry, as owner/name.
    :param branch: The branch of the pull request, made from base.
    :param base: The commit of main the branch is made from.
    :param domain: The domain of the provider.
    :param message: The commit message.
    :raises CommandError: Main holds no valid reserved_domains.json.
    :raises GitHubError: GitHub refused a request.
    """
    if (found := github.file(repository, RESERVED_DOMAINS_FILE, base)) is None:
        msg = f"{repository} has no {RESERVED_DOMAINS_FILE} at {base}"
        raise CommandError(msg)
    content, blob = found
    try:
        reserved = parse_model(content, ReservedDomains)
    except ValueError as err:
        msg = f"{RESERVED_DOMAINS_FILE} at {base}: {err}"
        raise CommandError(msg) from err
    if domain in reserved.moving:
        return
    content = dumps(add_moving(reserved, domain).to_dict())
    github.put_file(repository, branch, RESERVED_DOMAINS_FILE, content, message, replaces=blob)


def _delete_unused_branches(
    github: GitHub, repository: str, issue: int, pulls: list[PullRequest]
) -> None:
    """
    Delete the branches of an issue that no open pull request uses, left by an interrupted run.

    :param github: The GitHub client.
    :param repository: The repository of the registry, as owner/name.
    :param issue: The number of the issue; only branches admit/<domain>-<issue> are deleted.
    :param pulls: The open pull requests of the repository.
    """
    used = {pull.branch for pull in pulls}
    for branch in github.branches(repository, BRANCH_PREFIX):
        if _branch_issue(branch) == issue and branch not in used:
            github.delete_branch(repository, branch)


def _close(github: GitHub, repository: str, pull: PullRequest, why: str) -> None:
    """Close a pull request of the bot, say why, and delete its branch."""
    github.update_pull_request(repository, pull.number, state="closed")
    github.add_comment(repository, pull.number, why)
    github.delete_branch(repository, pull.branch)


def _branch_issue(branch: str) -> int | None:
    """Return the issue a branch of a pull request of the bot belongs to, None for another."""
    found = _BRANCH.fullmatch(branch)
    return int(found[2]) if found else None


def _branch_domain(branch: str) -> str | None:
    """Return the domain a branch of a pull request of the bot proposes, None for another."""
    found = _BRANCH.fullmatch(branch)
    return found[1] if found else None


def _proposal_verdict(
    proposal: Proposal | None, *, proposal_failed: bool, run_url: str | None
) -> str:
    """
    Return what happens next for a submission whose checks passed.

    :param proposal: What became of its pull request in this run, None when no run on main
        took care of it.
    :param proposal_failed: Whether the job that takes care of the pull request failed.
    :param run_url: The address of the workflow run.
    """
    if proposal_failed:
        run = f"[the run]({run_url})" if run_url else "the run"
        return (
            "Every check passed, but a step after the checks could not finish. A registry "
            f"maintainer looks into it; {run} shows why."
        )
    if proposal is None:
        return "Every check passed. A test run from a branch opens no pull request."
    outcome = proposal.outcome
    if outcome in {Outcome.OPENED, Outcome.STANDS}:
        adds = (
            f"[the pull request that adds it]({proposal.url})"
            if proposal.url
            else "this pull request"
        )
        verdict = (
            "Every check passed. A registry maintainer reviews the provider against the "
            f"[rules of the community catalog]({RULES_URL}) and decides by merging "
            f"{adds}; there is nothing more for you to do."
        )
        if proposal.others:
            links = ", ".join(f"[#{url.rsplit('/', 1)[-1]}]({url})" for url in proposal.others)
            verdict += (
                f"\n\nAnother submission for this domain is open: {links}. A registry "
                "maintainer decides between them."
            )
        return verdict
    if outcome == Outcome.CONFLICT:
        return (
            "Every check passed. Before the provider can be added, a registry maintainer "
            f"resolves a conflict with [another pull request]({proposal.url})."
        )
    return _NOT_PROPOSED.get(outcome, "Every check passed.")


def _proposed(report: AdmissionReport, provider: ProviderFile) -> str:
    """Return what the pull request of a submission adds, as its text starts."""
    path = f"{PROVIDERS_FOLDER}/{provider.domain}/{PROVIDER_FILE}"
    adds = (
        f"Adds {code_span(path)}: {code_span(report.name or provider.domain)} from "
        f"{github_link(provider.repository, provider.repository)}."
    )
    if not report.moving:
        return adds
    return (
        f"Moves {code_span(provider.domain)} out of the Music Assistant server: adds it to "
        f"{code_span('moving')} in {code_span(RESERVED_DOMAINS_FILE)}.\n\n{adds}"
    )


def _move_note(domain: str) -> str:
    """Return what a move of a provider out of the server means, and what happens after it."""
    return (
        f"This submission moves {code_span(domain)} out of the Music Assistant server. Once its "
        "pull request is merged, the team removes the provider from the server in the release "
        "whose catalog index offers it; the server's CI refuses the removal before that."
    )


def _what(report: AdmissionReport) -> str:
    """Return a paragraph that says what was checked and when, as far as the checks got."""
    repository = report.repository or ""
    parts = [f"repository {github_link(repository, repository)}"] if repository else []
    if report.tag is not None and report.commit is not None:
        release = github_link(report.tag, repository, f"releases/tag/{report.tag}")
        if not is_commit(report.commit):
            commit = code_span(report.commit)
        elif report.provider is not None:
            page = f"tree/{report.commit}/{report.provider.folder}"
            commit = github_link(report.commit, repository, page)
        else:
            commit = github_link(report.commit, repository, f"commit/{report.commit}")
        parts.append(f"release {release} (commit {commit})")
    return f"Checked {report.checked}: {', '.join(parts) or 'the issue'}.\n\n"


def _row(finding: Finding) -> str:
    """Return a finding as an item of a Markdown list, with its detail as code."""
    mark = "✅" if finding.passed else "❌"
    if finding.detail is None:
        return f"- {mark} {finding.text}\n"
    if len(finding.detail.splitlines()) > 1:
        return f"- {mark} {finding.text}:\n\n{code_block(finding.detail, '  ')}\n"
    return f"- {mark} {finding.text}: {code_span(finding.detail)}\n"
