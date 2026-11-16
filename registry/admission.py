"""The admission checks for a pull request that adds or changes a provider.json."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from registry import formats
from registry.checks import reserved_reason
from registry.edit import CommandError
from registry.fetch import newest_release, repository_state, take_release
from registry.files import parse_model
from registry.formats import release_version
from registry.github import GitHubError, NotAFileError
from registry.markdown import code_block, code_span
from registry.models import AdmissionReport, CheckPlan, Finding, ProviderFile
from registry.pipeline import make_plan, write_plan
from registry.registry import PROVIDER_FILE, PROVIDERS_FOLDER, InvalidRegistryError, load_registry

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from registry.github import GitHub
    from registry.models import LineResults, ReservedDomains
    from registry.registry import Registry

# the keys of an existing provider only a registry maintainer changes
MAINTAINER_KEYS = (
    "status",
    "reason",
    "reason_url",
    "blocked_versions",
    "repository",
    "repository_id",
    "owner_id",
    "maintainers",
)
# far more than a provider.json holds; a larger one is not read
MAX_PROVIDER_FILE_SIZE = 64 * 1024
# what the comment with the result on the pull request starts with, to find it again
COMMENT_MARKER = "<!-- admission-checks -->"
# well below the 65536 characters GitHub allows in a comment
MAX_COMMENT_SIZE = 60_000
RULES_URL = "https://github.com/music-assistant/.github/blob/main/COMMUNITY_CATALOG_RULES.md"
_PROVIDER_PATH = re.compile(rf"{PROVIDERS_FOLDER}/([^/]+)/{re.escape(PROVIDER_FILE)}")
_TITLE = "## Admission checks"


def prepare_admission(
    root: Path,
    changed: Sequence[str],
    *,
    repository: str,
    commit: str,
    author: str,
    github: GitHub,
    work: Path,
) -> AdmissionReport:
    """
    Run the admission checks that need GitHub, and add the latest release as a version file.

    The provider.json of the pull request is read from GitHub at its head commit and copied into
    the registry at root, a scratch checkout of the base branch, so the rules and the reserved
    domains of the base branch apply. A pull request that changes anything but the
    provider.json of one provider, or whose provider.json is no regular file, is no admission:
    its report holds the refusal and nothing was checked. The plan of the check step is written
    into the work folder.

    :param root: The root folder of the registry, a checkout of the base branch.
    :param changed: The files the pull request changes, as GitHub lists them.
    :param repository: The repository of the registry, as owner/name; the head commit of a
        pull request is in the repository it is opened on.
    :param commit: The full SHA of the head commit of the pull request.
    :param author: The GitHub handle of the author of the pull request.
    :param github: The GitHub client.
    :param work: The work folder, which keeps the archive of the release.
    :raises CommandError: The author is not valid.
    :raises GitHubError: GitHub refused to answer.
    """
    if not formats.is_github_handle(author):
        msg = f"{author!r} is not a GitHub handle"
        raise CommandError(msg)
    try:
        domain = _changed_domain(changed)
        relative = f"{PROVIDERS_FOLDER}/{domain}/{PROVIDER_FILE}"
        content = github.file(repository, relative, commit, MAX_PROVIDER_FILE_SIZE)
    except (CommandError, NotAFileError) as err:
        report = AdmissionReport(domain=None, findings=[], refusal=str(err))
    else:
        report = _prepare(root, domain, content, author=author, github=github, work=work)
    plan = make_plan(load_registry(root), work) if report.version else CheckPlan(lines={})
    write_plan(plan, work)
    return report


def check_admission(
    registry: Registry,
    report: AdmissionReport | None,
    results: list[LineResults],
    run_url: str | None = None,
) -> tuple[bool, str]:
    """
    Return whether every admission check passed, and the result in Markdown.

    The result is for the author of the pull request and for the person who decides: what was
    checked, and what happens next. The latest release passes when it passed on at least one
    server line that is tested. A line without a result in the results files takes the result
    the version file records, for a provider whose latest release is a version already.
    Everything the provider, its repository or the pull request holds is shown as code.

    :param registry: The registry of the base branch.
    :param report: The admission checks of the prepare step, None when they could not run.
    :param results: The results of the check step, as read_results returns them.
    :param run_url: The address of the workflow run, to link to.
    """
    link = f"\n[The run of these checks]({run_url})\n" if run_url else ""
    if report is None:
        text = (
            "The admission checks could not run to the end this time. The run shows why; a "
            "registry maintainer can start the checks again, and they run again when you push "
            "to this pull request."
        )
        return False, f"{_TITLE}\n\n{text}\n{link}"
    if report.refusal is not None or report.domain is None:
        rule = (
            "A pull request that adds a provider to the community catalog, or changes one, "
            f"changes one file, {code_span(f'{PROVIDERS_FOLDER}/<domain>/{PROVIDER_FILE}')}, "
            "and nothing else. Change the pull request so it holds only that file; the checks "
            "run again when you push to it. A change to anything else needs a pull request of "
            "its own."
        )
        refusal = code_block(report.refusal or "")
        intro = "This pull request is not an admission, so nothing was checked:"
        return False, f"{_TITLE}\n\n{intro}\n\n{refusal}\n{rule}\n{link}"
    findings = list(report.findings)
    passed_on_a_line = False
    if report.version is not None:
        known = registry.providers.get(report.domain)
        recorded = known.versions.get(report.version) if known else None
        by_line = {
            line: (check.passed, check.reason)
            for line, check in (recorded.checks.items() if recorded else [])
        }
        by_line |= {
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
    passed = (
        not report.maintainer_keys
        and passed_on_a_line
        and all(finding.passed for finding in report.findings)
    )
    rows = "".join(_row(finding) for finding in findings)
    if report.maintainer_keys:
        verdict = (
            "This change needs a registry maintainer, who decides on it: only a registry "
            "maintainer changes the status, reason, blocked versions, repository, ids or "
            "maintainers of a provider."
        )
    elif passed:
        verdict = (
            "Every check passed. A registry maintainer now reviews the provider against the "
            f"[rules of the community catalog]({RULES_URL}) and decides on the admission; "
            "there is nothing more to do for now."
        )
    else:
        verdict = (
            "Not every check passed. Fix what failed; the checks run again when you push to "
            "this pull request. When the fix is a new release of the provider, publish it, "
            "then close and reopen this pull request to check that release."
        )
    title = f"{_TITLE} for {code_span(report.domain)}"
    return passed, f"{title}\n\n{rows}\n{verdict}\n{link}"


def admission_comment(result: str, run_url: str | None = None) -> str:
    """
    Return the result of check_admission as the comment on the pull request.

    A result too long for a comment is replaced by where to find it.

    :param result: The result in Markdown.
    :param run_url: The address of the workflow run, whose summary holds the result.
    """
    if len(result) <= MAX_COMMENT_SIZE:
        return result
    run = f"[the run of these checks]({run_url})" if run_url else "the run of these checks"
    return f"{_TITLE}\n\nThe result is too long for a comment; it is in the summary of {run}.\n"


def _prepare(
    root: Path,
    domain: str,
    content: bytes,
    *,
    author: str,
    github: GitHub,
    work: Path,
) -> AdmissionReport:
    """Run the admission checks of the provider.json of a pull request; see prepare_admission."""
    relative = f"{PROVIDERS_FOLDER}/{domain}/{PROVIDER_FILE}"
    base_registry = load_registry(root)
    base = base_registry.providers.get(domain)
    try:
        proposed = parse_model(content, ProviderFile)
    except ValueError as err:
        finding = Finding(passed=False, text=f"{PROVIDER_FILE} cannot be read", detail=str(err))
        return AdmissionReport(domain=domain, findings=[finding])
    if base is not None and (
        keys := [
            key for key in MAINTAINER_KEYS if getattr(base.file, key) != getattr(proposed, key)
        ]
    ):
        text = "The pull request changes what only a registry maintainer changes"
        finding = Finding(passed=False, text=text, detail=", ".join(keys))
        return AdmissionReport(domain=domain, findings=[finding], maintainer_keys=keys)
    findings = [_domain_finding(base_registry.reserved, domain, taken=base is not None)]
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    try:
        registry = load_registry(root)
    except InvalidRegistryError as err:
        findings += [
            Finding(passed=False, text="Not valid", detail=problem) for problem in err.problems
        ]
        return AdmissionReport(domain=domain, findings=findings)
    findings.append(Finding(passed=True, text=f"{PROVIDER_FILE} is valid"))
    return _github_findings(
        registry, domain, author=author, github=github, work=work, findings=findings
    )


def _changed_domain(changed: Sequence[str]) -> str:
    """
    Return the domain of the one provider.json a pull request adds or changes.

    :param changed: The files the pull request changes.
    :raises CommandError: It changes anything else, or more than that file.
    """
    match = _PROVIDER_PATH.fullmatch(changed[0]) if len(changed) == 1 else None
    if match is None or not formats.is_domain(match[1]):
        listed = ", ".join(repr(path) for path in changed[:10])
        msg = (
            "a pull request for admission adds or changes the provider.json of one provider and "
            f"nothing else; this one changes {len(changed)} file(s): {listed}"
        )
        raise CommandError(msg)
    return match[1]


def _github_findings(
    registry: Registry,
    domain: str,
    *,
    author: str,
    github: GitHub,
    work: Path,
    findings: list[Finding],
) -> AdmissionReport:
    """
    Return the report with the checks of the repository and of its latest release added.

    :param registry: The registry, with the provider of the pull request.
    :param domain: The domain of the provider.
    :param author: The GitHub handle of the author of the pull request.
    :param github: The GitHub client.
    :param work: The work folder, which keeps the archive of the release.
    :param findings: The checks so far.
    :raises GitHubError: GitHub could not answer for the repository.
    """
    provider = registry.providers[domain]
    file = provider.file
    repository, prefix = file.repository, file.tag_prefix
    info = github.repositories([repository])[repository]
    if isinstance(info, GitHubError):
        raise info
    if info is None:
        text = "The repository does not exist or is not public"
        finding = Finding(passed=False, text=text, detail=repository)
        return AdmissionReport(domain=domain, findings=[*findings, finding])
    state = repository_state(info)
    findings.append(
        Finding(
            passed=not state.private and not state.archived,
            text="The repository is public and "
            + ("archived" if state.archived else "not archived"),
            detail=repository,
        )
    )
    ids = (info.repository_id, info.owner_id)
    findings.append(
        Finding(
            passed=ids == (file.repository_id, file.owner_id),
            text="provider.json has the ids GitHub gives the repository and its owner",
            detail=f"repository_id {ids[0]}, owner_id {ids[1]}",
        )
    )
    findings.append(
        Finding(
            passed=state.license not in {None, "other"},
            text="A licence GitHub recognises",
            detail=state.license or "none",
        )
    )
    version: str | None = None
    codeowners: list[str] = []
    if (release := newest_release(info.releases, prefix)) is None:
        text = "There is no release with a tag such as this one"
        findings.append(Finding(passed=False, text=text, detail=f"{prefix}v1.0.0"))
    else:
        try:
            take_release(registry, github, work, provider, release)
        except OSError as err:
            text = "The latest release cannot be downloaded"
            finding = Finding(passed=False, text=text, detail=f"{release.tag}: {err}")
            return AdmissionReport(domain=domain, findings=[*findings, finding])
        version = release_version(release.tag, prefix)
        assert version is not None
        if (refused := provider.refused.get(version)) is not None:
            text = f"The latest release, version {version}, is refused"
            findings.append(Finding(passed=False, text=text, detail=refused.reason))
            version = None
        else:
            text = f"The latest release is version {version}"
            findings.append(Finding(passed=True, text=text, detail=release.tag))
            codeowners = provider.versions[version].manifest.get("codeowners", [])
    owners = {state.owner.lower(), *(owner.removeprefix("@").lower() for owner in codeowners)}
    findings.append(
        Finding(
            passed=author.lower() in owners,
            text="The author owns the repository or is a code owner in the manifest of the "
            "latest release",
            detail=author,
        )
    )
    others = [handle for handle in file.maintainers if handle.lower() not in owners]
    findings.append(
        Finding(
            passed=not others,
            text="Every maintainer in provider.json owns the repository or is a code owner in "
            "the manifest of the latest release",
            detail=", ".join(others) or None,
        )
    )
    return AdmissionReport(domain=domain, findings=findings, version=version)


def _domain_finding(reserved: ReservedDomains, domain: str, *, taken: bool) -> Finding:
    """
    Return whether the domain is free for a new provider.

    :param reserved: The domains a community provider cannot take.
    :param domain: The domain.
    :param taken: Whether the base branch has a provider of this domain already.
    """
    if reason := reserved_reason(domain, reserved):
        return Finding(passed=False, text=f"The domain is {reason}", detail=domain)
    if taken:
        return Finding(
            passed=True, text="The pull request changes an existing provider", detail=domain
        )
    return Finding(passed=True, text="The domain is free", detail=domain)


def _row(finding: Finding) -> str:
    """Return a finding as an item of a Markdown list, with its detail as code."""
    mark = "✅" if finding.passed else "❌"
    if finding.detail is None:
        return f"- {mark} {finding.text}\n"
    if len(finding.detail.splitlines()) > 1:
        return f"- {mark} {finding.text}:\n\n{code_block(finding.detail, '  ')}\n"
    return f"- {mark} {finding.text}: {code_span(finding.detail)}\n"
