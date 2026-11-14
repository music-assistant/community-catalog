"""The admission checks for a pull request that adds or changes a provider.json."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from registry import formats
from registry.checks import reserved_reason
from registry.edit import CommandError
from registry.fetch import newest_release, repository_state, take_release
from registry.files import load_model
from registry.formats import release_version
from registry.github import GitHubError
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
_PROVIDER_PATH = re.compile(rf"{PROVIDERS_FOLDER}/([^/]+)/{re.escape(PROVIDER_FILE)}")


def prepare_admission(
    root: Path,
    pull_request: Path,
    changed: Sequence[str],
    *,
    author: str,
    github: GitHub,
    work: Path,
) -> AdmissionReport:
    """
    Run the admission checks that need GitHub, and add the latest release as a version file.

    The provider.json of the pull request is copied into the registry at root, a scratch
    checkout of the base branch, so the rules and the reserved domains of the base branch apply.
    The plan of the check step is written into the work folder.

    :param root: The root folder of the registry, a checkout of the base branch.
    :param pull_request: A checkout of the pull request, which is only read.
    :param changed: The files the pull request changes, as GitHub lists them.
    :param author: The GitHub handle of the author of the pull request.
    :param github: The GitHub client.
    :param work: The work folder, which keeps the archive of the release.
    :raises CommandError: The pull request changes anything but the provider.json of one
        provider, the author is not valid, or that provider.json is a symbolic link.
    :raises GitHubError: GitHub refused to answer.
    """
    report = _prepare(root, pull_request, changed, author=author, github=github, work=work)
    plan = make_plan(load_registry(root), work) if report.version else CheckPlan(lines={})
    write_plan(plan, work)
    return report


def check_admission(
    registry: Registry, report: AdmissionReport, results: list[LineResults]
) -> tuple[bool, str]:
    """
    Return whether every admission check passed, and the result in Markdown for a person.

    The latest release passes when it passed on at least one server line that is tested. A line
    without a result in the results files takes the result the version file records, for a
    provider whose latest release is a version already. Everything the provider or its
    repository holds is shown as code.

    :param registry: The registry of the base branch.
    :param report: The admission checks of the prepare step.
    :param results: The results of the check step, as read_results returns them.
    """
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
        verdict = "This change needs a registry maintainer, who decides on it."
    elif passed:
        verdict = (
            "Every check passed. A person decides whether what the provider does is acceptable."
        )
    else:
        verdict = "Not every check passed."
    title = f"## Admission checks for {code_span(report.domain)}"
    return passed, f"{title}\n\n{rows}\n{verdict}\n"


def _prepare(
    root: Path,
    pull_request: Path,
    changed: Sequence[str],
    *,
    author: str,
    github: GitHub,
    work: Path,
) -> AdmissionReport:
    """Run the admission checks; see prepare_admission."""
    domain = _changed_domain(changed)
    if not formats.is_github_handle(author):
        msg = f"{author!r} is not a GitHub handle"
        raise CommandError(msg)
    folder = f"{PROVIDERS_FOLDER}/{domain}"
    relative = f"{folder}/{PROVIDER_FILE}"
    if any((pull_request / path).is_symlink() for path in (PROVIDERS_FOLDER, folder, relative)):
        msg = f"{relative} of the pull request lies behind a symbolic link"
        raise CommandError(msg)
    base_registry = load_registry(root)
    base = base_registry.providers.get(domain)
    try:
        proposed = load_model(pull_request / relative, ProviderFile)
    except (ValueError, OSError) as err:
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
    target.write_bytes((pull_request / relative).read_bytes())
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
