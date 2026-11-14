"""The fetch step: the newest release of every provider becomes a version file, or is refused."""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

from registry.checks import MAX_REFUSAL
from registry.edit import CommandError, add_version, now, refuse_release
from registry.files import dumps
from registry.formats import format_date, is_date, release_version
from registry.github import ArchiveTooLargeError, GitHubError
from registry.models import (
    FetchReport,
    ProviderStatus,
    ProviderVersion,
    RefusedRelease,
    RepositoryState,
)
from registry.pipeline import (
    FETCH_REPORT,
    archive_path,
    make_plan,
    versions_to_check,
    write_plan,
)
from registry.registry import InvalidRegistryError, load_registry

if TYPE_CHECKING:
    from collections.abc import Collection, Sequence
    from pathlib import Path

    from registry.github import GitHub, Release, RepositoryInfo
    from registry.registry import Provider, Registry


def fetch(registry: Registry, github: GitHub, work: Path) -> FetchReport:
    """
    Write a version file for the newest release of every provider that is not blocked.

    Only the most recently published release counts, and only when it has neither a version
    file nor a refusal yet; a release that cannot become a version is recorded as refused.
    Nothing is fetched of a provider whose repository is not the one that was admitted (its
    ids differ), or could not be read; the others are fetched all the same. The archives of
    every version the check step needs are downloaded into the work folder, the plan of the
    check step and what was found are written there.

    :param registry: The registry.
    :param github: The GitHub client.
    :param work: The work folder.
    """
    providers = [
        provider
        for provider in registry.providers.values()
        if provider.file.status != ProviderStatus.BLOCKED
    ]
    infos = github.repositories(sorted({provider.file.repository for provider in providers}))
    report = FetchReport(repositories={}, refused=[])
    admitted: set[str] = set()
    for provider in sorted(providers, key=lambda provider: provider.file.domain):
        domain, repository = provider.file.domain, provider.file.repository
        info = infos[repository]
        if info is None or isinstance(info, GitHubError):
            problem = info or f"{repository} does not exist or is not public"
            print(f"warning: {domain}: {problem}", file=sys.stderr)
            continue
        report.repositories[repository] = repository_state(info)
        ids = (info.repository_id, info.owner_id)
        if ids != (provider.file.repository_id, provider.file.owner_id):
            print(
                f"warning: {domain}: {repository} is not the repository that was admitted: "
                f"repository_id {ids[0]}, owner_id {ids[1]}",
                file=sys.stderr,
            )
            continue
        admitted.add(domain)
        if (release := newest_release(info.releases, provider.file.tag_prefix)) is None:
            continue
        try:
            refused = take_release(registry, github, work, provider, release)
        except OSError as err:
            # GitHub may answer again tomorrow, so this is no refusal
            print(f"warning: {domain} {release.tag}: {err}", file=sys.stderr)
            continue
        if refused is not None:
            report.refused.append(ProviderVersion(domain=domain, version=refused.version))
    registry = load_registry(registry.root)
    download_archives(registry, github, work, admitted)
    write_plan(make_plan(registry, work), work)
    path = work / FETCH_REPORT
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(dumps(report.to_dict()))
    return report


def newest_release(releases: Sequence[Release], tag_prefix: str) -> Release | None:
    """
    Return the most recently published release whose tag names a version of a provider.

    Pre-releases are ignored, as are tags of other providers in the same repository.

    :param releases: The published releases of the repository.
    :param tag_prefix: What the release tags of the provider start with.
    """
    candidates = [
        release
        for release in releases
        if not release.prerelease and release_version(release.tag, tag_prefix) is not None
    ]
    return max(candidates, key=lambda release: release.published, default=None)


def take_release(
    registry: Registry, github: GitHub, work: Path, provider: Provider, release: Release
) -> RefusedRelease | None:
    """
    Write the version file of a release, or record why it is refused.

    A release whose version has a version file or a refusal already is left alone. Returns
    the refusal, when the release was refused now.

    :param registry: The registry.
    :param github: The GitHub client.
    :param work: The work folder, which keeps the archive of the release.
    :param provider: The provider.
    :param release: A release whose tag names a version of the provider.
    :raises OSError: The archive could not be downloaded.
    """
    domain = provider.file.domain
    version = release_version(release.tag, provider.file.tag_prefix)
    assert version is not None
    if version in provider.versions or version in provider.refused:
        return None
    released = release.published if is_date(release.published) else format_date(now())
    archive = archive_path(work, domain, version)
    try:
        github.download_archive(provider.file.repository, release.commit, archive)
        add_version(
            registry,
            domain,
            version,
            archive,
            tag=release.tag,
            commit=release.commit,
            released=released,
        )
    except (ArchiveTooLargeError, CommandError, InvalidRegistryError) as err:
        problems = err.problems if isinstance(err, InvalidRegistryError) else [str(err)]
        # the path of the archive in the work folder means nothing to the maintainer
        problems = [problem.removeprefix(f"{archive}: ") for problem in problems]
        # a name in the archive can hold what plain JSON cannot
        reason = "\n".join(problems).encode(errors="replace").decode()
        if len(reason) > MAX_REFUSAL:
            reason = f"{reason[: MAX_REFUSAL - 1]}…"
        refused = RefusedRelease(
            version=version,
            tag=release.tag,
            commit=release.commit,
            released=released,
            refused=format_date(now()),
            reason=reason,
        )
        refuse_release(registry, domain, refused)
        # every line of the log starts with the domain, whatever the reason holds
        for line in refused.reason.splitlines():
            print(f"{domain} {release.tag}: refused: {line}")
        return refused
    print(f"{domain} {release.tag}: added version {version}")
    return None


def download_archives(
    registry: Registry, github: GitHub, work: Path, domains: Collection[str]
) -> None:
    """
    Download the archive of every version of some providers the check step needs.

    An archive in the work folder already is kept; one that cannot be downloaded is left out,
    and its version is checked on another night.

    :param registry: The registry.
    :param github: The GitHub client.
    :param work: The work folder.
    :param domains: The providers whose archives are downloaded.
    """
    needed = {
        (domain, version)
        for line in registry.server_lines
        for domain, version in versions_to_check(registry, line)
        if domain in domains
    }
    for domain, version in sorted(needed):
        if (path := archive_path(work, domain, version)).is_file():
            continue
        provider = registry.providers[domain]
        try:
            github.download_archive(
                provider.file.repository, provider.versions[version].commit, path
            )
        except OSError as err:
            print(f"warning: {domain} {version}: {err}", file=sys.stderr)


def repository_state(info: RepositoryInfo) -> RepositoryState:
    """
    Return what the report and the admission keep of a repository.

    :param info: The repository as GitHub answered it.
    """
    return RepositoryState(
        owner=info.owner,
        repository_id=info.repository_id,
        owner_id=info.owner_id,
        private=info.private,
        archived=info.archived,
        has_issues=info.has_issues,
        license=info.license,
    )
