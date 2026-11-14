"""The admission checks for a pull request that adds or changes a provider.json."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from registry import formats
from registry.checks import reserved_reason
from registry.edit import CommandError
from registry.fetch import newest_release, repository_state, take_release
from registry.files import load_model
from registry.formats import release_version
from registry.models import AdmissionReport, Finding, ProviderFile, ProviderStatus
from registry.registry import PROVIDER_FILE, PROVIDERS_FOLDER, InvalidRegistryError, load_registry

if TYPE_CHECKING:
    from pathlib import Path

    from registry.github import GitHub
    from registry.models import LineResults, ReservedDomains
    from registry.registry import Registry


def prepare_admission(
    root: Path, pull_request: Path, domain: str, *, author: str, github: GitHub, work: Path
) -> AdmissionReport:
    """
    Run the admission checks that need GitHub, and add the latest release as a version file.

    The provider.json of the pull request is copied into the registry at root, a scratch
    checkout of the base branch, so the rules and the reserved domains of the base branch apply.

    :param root: The root folder of the registry, a checkout of the base branch.
    :param pull_request: A checkout of the pull request, which is only read.
    :param domain: The domain of the provider the pull request adds or changes.
    :param author: The GitHub handle of the author of the pull request.
    :param github: The GitHub client.
    :param work: The work folder, which keeps the archive of the release.
    :raises CommandError: The domain or the author is not valid, or the provider.json of the
        pull request is a symbolic link.
    :raises GitHubError: GitHub refused to answer.
    """
    if not formats.is_domain(domain) or not formats.is_github_handle(author):
        msg = f"{domain!r} is not a domain or {author!r} is not a GitHub handle"
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
        return _report(domain, [Finding(passed=False, text=f"{PROVIDER_FILE}: {err}")])
    if base is not None and _without_status(proposed) == _without_status(base.file):
        text = "Only the status or the blocked versions change: no admission decision is needed"
        return AdmissionReport(
            domain=domain, findings=[Finding(passed=True, text=text)], needed=False
        )
    findings = [_domain_finding(base_registry.reserved, domain, taken=base is not None)]
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes((pull_request / relative).read_bytes())
    try:
        registry = load_registry(root)
    except InvalidRegistryError as err:
        findings += [Finding(passed=False, text=problem) for problem in err.problems]
        return _report(domain, findings)
    findings.append(Finding(passed=True, text=f"{PROVIDER_FILE} is valid"))
    return _github_findings(
        registry, domain, author=author, github=github, work=work, findings=findings
    )


def check_admission(
    registry: Registry, report: AdmissionReport, results: list[LineResults]
) -> tuple[bool, str]:
    """
    Return whether every admission check passed, and the result in Markdown for a person.

    The latest release passes when it passed on at least one server line that is tested.

    :param registry: The registry.
    :param report: The admission checks of the prepare step.
    :param results: The results files of the check step.
    """
    findings = list(report.findings)
    passed_on_a_line = False
    if report.version is not None:
        by_line = {
            file.line: result
            for file in results
            for result in file.results
            if (result.domain, result.version) == (report.domain, report.version)
        }
        for line, config in sorted(registry.server_lines.items()):
            if config.frozen:
                continue
            if (result := by_line.get(line)) is None:
                findings.append(Finding(passed=False, text=f"{line}: the checks did not run"))
                continue
            findings.append(
                Finding(passed=result.passed, text=f"{line}: {result.reason or 'passed'}")
            )
            passed_on_a_line = passed_on_a_line or result.passed
    passed = not report.needed or (
        passed_on_a_line and all(finding.passed for finding in report.findings)
    )
    rows = "\n".join(f"- {'✅' if f.passed else '❌'} {f.text}" for f in findings)
    if not report.needed:
        verdict = "Nothing to admit."
    elif passed:
        verdict = (
            "Every check passed. A person decides whether what the provider does is acceptable."
        )
    else:
        verdict = "Not every check passed."
    return passed, f"## Admission checks for `{report.domain}`\n\n{rows}\n\n{verdict}\n"


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
    """
    provider = registry.providers[domain]
    repository, prefix = provider.file.repository, provider.file.tag_prefix
    if (info := github.repositories([repository])[repository]) is None:
        text = f"The repository {repository} does not exist or is not public"
        return _report(domain, [*findings, Finding(passed=False, text=text)])
    state = repository_state(info)
    findings.append(
        Finding(
            passed=not state.private and not state.archived,
            text=f"The repository {repository} is public and "
            + ("archived" if state.archived else "not archived"),
        )
    )
    findings.append(
        Finding(
            passed=state.license not in {None, "other"},
            text=f"A licence GitHub recognises: {state.license or 'none'}",
        )
    )
    version: str | None = None
    codeowners: list[str] = []
    if (release := newest_release(info.releases, prefix)) is None:
        text = f"There is no release with a tag such as {prefix}v1.0.0"
        findings.append(Finding(passed=False, text=text))
    else:
        try:
            take_release(registry, github, work, provider, release)
        except OSError as err:
            text = f"The latest release {release.tag} cannot be downloaded: {err}"
            return _report(domain, [*findings, Finding(passed=False, text=text)])
        version = release_version(release.tag, prefix)
        assert version is not None
        if (refused := provider.refused.get(version)) is not None:
            text = f"The latest release {release.tag} is refused: {refused.reason}"
            findings.append(Finding(passed=False, text=text))
            version = None
        else:
            text = f"The latest release {release.tag} is version {version}"
            findings.append(Finding(passed=True, text=text))
            codeowners = provider.versions[version].manifest.get("codeowners", [])
    owners = {state.owner.lower(), *(owner.removeprefix("@").lower() for owner in codeowners)}
    findings.append(
        Finding(
            passed=author.lower() in owners,
            text=f"The author {author} owns {repository} or is a code owner in the manifest of "
            "the latest release",
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
        return Finding(passed=False, text=f"The domain {domain} is {reason}")
    if taken:
        return Finding(passed=True, text=f"The pull request changes the existing provider {domain}")
    return Finding(passed=True, text=f"The domain {domain} is free")


def _without_status(file: ProviderFile) -> ProviderFile:
    """Return a provider.json without what a maintainer of the registry changes by command."""
    return replace(
        file, status=ProviderStatus.LISTED, reason=None, reason_url=None, blocked_versions={}
    )


def _report(domain: str, findings: list[Finding]) -> AdmissionReport:
    """Return the report of admission checks that could not all run."""
    return AdmissionReport(domain=domain, findings=findings)
