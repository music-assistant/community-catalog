"""The changes the commands make to the files of the registry, each checked before it is written."""

from __future__ import annotations

import sys
from dataclasses import replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from registry.checks import check_provider, check_refused, check_version
from registry.content import ProviderContent, read_archive, read_folder
from registry.files import dumps
from registry.formats import format_date
from registry.icon import check_icon
from registry.index import render_indexes
from registry.models import CheckResult, ProviderStatus, RefusedRelease, VersionFile
from registry.registry import PROVIDER_FILE, PROVIDERS_FOLDER, InvalidRegistryError, load_registry

if TYPE_CHECKING:
    from pathlib import Path

    from registry.models import ProviderFile
    from registry.registry import Provider, Registry


class CommandError(Exception):
    """A command cannot be carried out; the message says why."""


def add_version(
    registry: Registry,
    domain: str,
    version: str,
    source: Path,
    *,
    tag: str,
    commit: str,
    released: str | None = None,
) -> VersionFile:
    """
    Write the version file of a release, from its provider folder or its GitHub archive.

    :param registry: The registry.
    :param domain: The domain of the provider.
    :param version: The version the release names.
    :param source: The downloaded GitHub archive of the commit, or a local provider folder.
    :param tag: The tag of the release.
    :param commit: The full SHA of the tagged commit.
    :param released: When it was released; now when omitted.
    :raises CommandError: There is no such provider, the version exists already, or the source
        holds what a server would not install.
    :raises InvalidRegistryError: The version file would not be valid.
    """
    provider = get_provider(registry, domain)
    if registry.version_path(domain, version).exists():
        msg = f"{domain} {version} exists already"
        raise CommandError(msg)
    content = read_source(source, provider.file.folder)
    icon_svg = content.icon_svg
    if icon_svg is not None:
        try:
            check_icon(icon_svg)
        except ValueError as err:
            print(f"warning: the icon is left out: {err}", file=sys.stderr)
            icon_svg = None
    requirements = content.manifest.get("requirements", [])
    file = VersionFile(
        version=version,
        tag=tag,
        commit=commit,
        folder=provider.file.folder,
        content_hash=content.content_hash,
        manifest=content.manifest,
        requirements=requirements if isinstance(requirements, list) else [],
        released=released or format_date(now()),
        icon_svg=icon_svg,
    )
    write_version(registry, domain, file)
    return file


def refuse_release(registry: Registry, domain: str, release: RefusedRelease) -> None:
    """
    Record a release that did not become a version, so it is not tried again.

    :param registry: The registry.
    :param domain: The domain of the provider.
    :param release: The release and why it was refused.
    :raises InvalidRegistryError: The file would not be valid.
    """
    provider = get_provider(registry, domain)
    path = registry.refused_path(domain, release.version)
    _write(registry, path, release.to_dict(), check_refused(release, provider.file))
    provider.refused[release.version] = release


def record_check(
    registry: Registry,
    domain: str,
    version: str,
    channel: str,
    *,
    reason: str | None,
    checked: str | None = None,
) -> None:
    """
    Record the result of the checks of a version on a channel.

    :param registry: The registry.
    :param domain: The domain of the provider.
    :param version: The version.
    :param channel: The channel.
    :param reason: Why the version did not pass; None when it passed.
    :param checked: When it was checked; now when omitted.
    :raises CommandError: There is no such provider or version.
    :raises InvalidRegistryError: The version file would not be valid.
    """
    provider = get_provider(registry, domain)
    if (file := provider.versions.get(version)) is None:
        msg = f"{domain} has no version {version}"
        raise CommandError(msg)
    file.checks[channel] = CheckResult(
        passed=reason is None, checked=checked or format_date(now()), reason=reason
    )
    write_version(registry, domain, file)


def clear_failed_check(registry: Registry, domain: str, version: str, channel: str) -> None:
    """
    Remove a failed result of the checks of a version on a channel; a passed one stays.

    :param registry: The registry.
    :param domain: The domain of the provider.
    :param version: The version.
    :param channel: The channel.
    :raises CommandError: There is no such provider or version.
    :raises InvalidRegistryError: The version file would not be valid.
    """
    provider = get_provider(registry, domain)
    if (file := provider.versions.get(version)) is None:
        msg = f"{domain} has no version {version}"
        raise CommandError(msg)
    if (check := file.checks.get(channel)) is not None and not check.passed:
        del file.checks[channel]
        write_version(registry, domain, file)


def set_status(
    registry: Registry,
    domain: str,
    status: ProviderStatus,
    reason: str | None = None,
    reason_url: str | None = None,
) -> None:
    """
    Change whether the catalog offers a provider.

    :param registry: The registry.
    :param domain: The domain of the provider.
    :param status: The new status.
    :param reason: Why the provider is withdrawn or blocked; required for those.
    :param reason_url: An https link with more about the reason.
    :raises CommandError: There is no such provider.
    :raises InvalidRegistryError: The provider would not be valid, such as a block without reason.
    """
    provider = get_provider(registry, domain)
    _write_provider(
        registry,
        provider,
        replace(provider.file, status=status, reason=reason, reason_url=reason_url),
    )


def block_version(registry: Registry, domain: str, version: str, reason: str | None) -> None:
    """
    Block a version of a provider, or lift its block, so no server loads it.

    :param registry: The registry.
    :param domain: The domain of the provider.
    :param version: The version.
    :param reason: Why the version is blocked; None lifts the block.
    :raises CommandError: There is no such provider, or the version is not blocked when lifting.
    :raises InvalidRegistryError: The provider would not be valid, such as a version without
        a version file.
    """
    provider = get_provider(registry, domain)
    blocked = dict(provider.file.blocked_versions)
    if reason is not None:
        blocked[version] = reason
    elif blocked.pop(version, None) is None:
        msg = f"{domain} {version} is not blocked"
        raise CommandError(msg)
    _write_provider(registry, provider, replace(provider.file, blocked_versions=blocked))


def clear_version(registry: Registry, domain: str, version: str, reason_url: str) -> None:
    """
    Accept a version of a provider that the reading did not pass, after a person read it.

    :param registry: The registry.
    :param domain: The domain of the provider.
    :param version: The version.
    :param reason_url: An https link to why the version is accepted, such as its issue.
    :raises CommandError: There is no such provider.
    :raises InvalidRegistryError: The provider would not be valid, such as a version without
        a version file or a link that is not https.
    """
    provider = get_provider(registry, domain)
    cleared = {**provider.file.cleared_versions, version: reason_url}
    _write_provider(registry, provider, replace(provider.file, cleared_versions=cleared))


def write_indexes(root: Path) -> None:
    """
    Check every file of the registry and write the index file of every channel that changed.

    :param root: The root folder of the registry.
    :raises InvalidRegistryError: A file of the registry is not valid.
    """
    registry = load_registry(root)
    for path, content in render_indexes(registry, now()).items():
        if path.is_file() and path.read_bytes() == content:
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        print(f"wrote {path.relative_to(registry.root).as_posix()}")


def write_version(registry: Registry, domain: str, version: VersionFile) -> None:
    """
    Write a version file after checking it.

    :param registry: The registry.
    :param domain: The domain of the provider.
    :param version: The version.
    :raises InvalidRegistryError: The version is not valid.
    """
    provider = registry.providers[domain]
    problems = check_version(version, provider.file, registry.channels)
    _write(registry, registry.version_path(domain, version.version), version.to_dict(), problems)
    provider.versions[version.version] = version


def get_provider(registry: Registry, domain: str) -> Provider:
    """
    Return a provider of the registry.

    :param registry: The registry.
    :param domain: The domain of the provider.
    :raises CommandError: There is no such provider.
    """
    if (provider := registry.providers.get(domain)) is None:
        msg = f"there is no provider {domain}"
        raise CommandError(msg)
    return provider


def read_source(source: Path, folder: str | None) -> ProviderContent:
    """
    Return the content of a provider folder, or of a folder in a GitHub archive.

    :param source: A provider folder, or a downloaded archive of a commit.
    :param folder: The folder of the provider in the repository, needed for an archive.
    :raises CommandError: The source cannot be read or holds what a server would not install.
    """
    if source.is_file() and folder is None:
        raise CommandError("--folder is needed to read an archive")
    try:
        if source.is_dir():
            return read_folder(source)
        if source.is_file() and folder is not None:
            return read_archive(source, folder)
    except (ValueError, OSError) as err:
        msg = f"{source}: {err}"
        raise CommandError(msg) from err
    msg = f"{source} is no folder or archive"
    raise CommandError(msg)


def now() -> datetime:
    """Return the current moment."""
    return datetime.now(UTC)


def _write_provider(registry: Registry, provider: Provider, file: ProviderFile) -> None:
    """
    Write a provider.json after checking it.

    :param registry: The registry.
    :param provider: The provider as it is.
    :param file: The new content of its provider.json.
    :raises InvalidRegistryError: The provider is not valid.
    """
    problems = check_provider(file, registry.reserved)
    problems += [
        f"{key}: {version} has no version file"
        for key, kept in (
            ("blocked_versions", file.blocked_versions),
            ("cleared_versions", file.cleared_versions),
        )
        for version in kept
        if version not in provider.versions
    ]
    path = registry.root / PROVIDERS_FOLDER / file.domain / PROVIDER_FILE
    _write(registry, path, file.to_dict(), problems)
    provider.file = file


def _write(registry: Registry, path: Path, data: dict[str, object], problems: list[str]) -> None:
    """
    Write a file of the registry, unless it has problems.

    :param registry: The registry.
    :param path: The file.
    :param data: Its content.
    :param problems: What is wrong in the content.
    :raises InvalidRegistryError: The content has problems or is not plain JSON.
    """
    where = path.relative_to(registry.root).as_posix()
    if problems:
        raise InvalidRegistryError([f"{where}: {problem}" for problem in problems])
    try:
        content = dumps(data)
    except ValueError as err:
        raise InvalidRegistryError([f"{where}: {err}"]) from err
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    print(f"wrote {where}")
