"""The rules every file of the registry follows, beyond its shape."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from registry import formats
from registry.icon import check_icon
from registry.models import ProviderStatus

if TYPE_CHECKING:
    from collections.abc import Mapping

    from registry.models import (
        Channel,
        Channels,
        CheckResult,
        ProviderFile,
        RefusedRelease,
        ReservedDomains,
        VersionFile,
    )

# the provider types of a manifest a community provider can have; core is for the server itself
PROVIDER_TYPES = frozenset({"music", "player", "metadata", "plugin", "audio_analysis"})
# every server downloads the manifest of each provider in its index, within a few seconds
MAX_MANIFEST_SIZE = 16 * 1024
MAX_REQUIREMENTS = 50
MAX_REQUIREMENT_LENGTH = 100
MAX_CODEOWNERS = 10
# the reason of a refused release goes into an issue for the maintainer
MAX_REFUSAL = 2000


def check_channels(channels: Channels) -> list[str]:
    """
    Return what is wrong in the list of channels.

    :param channels: The content of channels.json.
    """
    problems = [
        f"channels: {channel!r} is not a channel: nightly, beta or MAJOR.MINOR"
        for channel in channels.channels
        if not formats.is_channel(channel)
    ]
    problems += [
        f"channels.{channel}.image: {config.image!r} is not a container image with a tag"
        for channel, config in channels.channels.items()
        if not formats.is_image(config.image)
    ]
    if not channels.channels:
        problems.append("channels: there is no channel")
    return problems


def reserved_reason(domain: str, reserved: ReservedDomains) -> str | None:
    """
    Return why a community provider cannot take a domain, or None when it can.

    :param domain: The domain.
    :param reserved: The domains the server ships or reserves.
    """
    if domain in reserved.core_controllers:
        return "a core controller of the server"
    if domain in reserved.reserved_names:
        return "a name the server reserves"
    if domain in reserved.builtin_providers and domain not in reserved.moving:
        return "a provider the server ships"
    return None


def check_provider(provider: ProviderFile, reserved: ReservedDomains) -> list[str]:
    """
    Return what is wrong in a provider.json.

    :param provider: The content of the file.
    :param reserved: The domains the server ships or reserves.
    """
    problems: list[str] = []
    domain = provider.domain
    if not formats.is_domain(domain):
        problems.append(
            f"domain: {domain!r} is not a provider domain: lower case letters, digits and "
            "underscores, starting with a letter"
        )
    elif reason := reserved_reason(domain, reserved):
        problems.append(f"domain: {domain} is {reason}")
    if not formats.is_repository(provider.repository):
        problems.append(f"repository: {provider.repository!r} is not a GitHub owner/name")
    problems += [
        f"{key}: {value} is not an id GitHub gives"
        for key, value in (
            ("repository_id", provider.repository_id),
            ("owner_id", provider.owner_id),
        )
        if value < 1
    ]
    if not formats.is_provider_folder(provider.folder, domain):
        problems.append(_folder_problem(provider.folder, domain))
    if not formats.is_tag_prefix(provider.tag_prefix):
        problems.append(f"tag_prefix: {provider.tag_prefix!r} is not allowed in a tag")
    problems += _status_problems(provider)
    for version, reason in provider.blocked_versions.items():
        if not formats.is_version(version):
            problems.append(f"blocked_versions: {version!r} is not a version: MAJOR.MINOR.PATCH")
        if not reason.strip():
            problems.append(f"blocked_versions: {version} has no reason")
    problems += _date_problems("added", provider.added)
    return problems


def check_version(
    version: VersionFile, provider: ProviderFile, channels: Mapping[str, Channel]
) -> list[str]:
    """
    Return what is wrong in a version file.

    :param version: The content of the file.
    :param provider: The provider the version belongs to.
    :param channels: The channels an index is built for.
    """
    problems: list[str] = []
    domain = provider.domain
    if not formats.is_version(version.version):
        problems.append(f"version: {version.version!r} is not a version: MAJOR.MINOR.PATCH")
    tags = {f"{provider.tag_prefix}{version.version}", f"{provider.tag_prefix}v{version.version}"}
    if version.tag not in tags:
        problems.append(f"tag: {version.tag!r} is not one of {', '.join(sorted(tags))}")
    if not formats.is_commit(version.commit):
        problems.append(f"commit: {version.commit!r} is not a full commit SHA in lower case")
    if not formats.is_provider_folder(version.folder, domain):
        problems.append(_folder_problem(version.folder, domain))
    if not formats.is_content_hash(version.content_hash):
        problems.append(f"content_hash: {version.content_hash!r} is not a sha256 hex digest")
    problems += check_manifest(version.manifest, domain)
    problems += [
        f"requirements: {requirement!r} is not a package name with version specifiers"
        for requirement in version.requirements
        if not formats.is_requirement(requirement)
    ]
    if version.requirements != version.manifest.get("requirements", []):
        problems.append("requirements: differ from the requirements in the manifest")
    problems += _date_problems("released", version.released)
    if version.icon_svg is not None:
        try:
            check_icon(version.icon_svg)
        except ValueError as err:
            problems.append(f"icon_svg: {err}")
    for channel, result in version.checks.items():
        problems += _check_result_problems(channel, result, channels)
    return problems


def check_refused(release: RefusedRelease, provider: ProviderFile) -> list[str]:
    """
    Return what is wrong in the file of a refused release.

    :param release: The content of the file.
    :param provider: The provider the release belongs to.
    """
    problems: list[str] = []
    if formats.release_version(release.tag, provider.tag_prefix) != release.version:
        problems.append(f"tag: {release.tag!r} does not name the version {release.version}")
    if not formats.is_commit(release.commit):
        problems.append(f"commit: {release.commit!r} is not a full commit SHA in lower case")
    problems += _date_problems("released", release.released)
    problems += _date_problems("refused", release.refused)
    if not release.reason.strip():
        problems.append("reason: a refused release needs a reason")
    if len(release.reason) > MAX_REFUSAL:
        problems.append(f"reason: is longer than {MAX_REFUSAL} characters")
    return problems


def check_manifest(manifest: Mapping[str, Any], domain: str) -> list[str]:
    """
    Return what is wrong in the manifest of a provider, as far as the index relies on it.

    The full manifest is checked by the server the version is tested on.

    :param manifest: The content of manifest.json.
    :param domain: The domain of the provider.
    """
    problems: list[str] = []
    if manifest.get("domain") != domain:
        problems.append(f"manifest.domain: {manifest.get('domain')!r} is not {domain}")
    if manifest.get("type") not in PROVIDER_TYPES:
        types = ", ".join(sorted(PROVIDER_TYPES))
        problems.append(f"manifest.type: {manifest.get('type')!r} is not one of {types}")
    problems += [
        f"manifest.{key}: is missing or empty"
        for key in ("name", "description")
        if not isinstance(manifest.get(key), str) or not manifest[key].strip()
    ]
    for key, required, limit in (
        ("codeowners", True, MAX_CODEOWNERS),
        ("requirements", False, MAX_REQUIREMENTS),
    ):
        value = manifest.get(key, None if required else [])
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            problems.append(f"manifest.{key}: is not a list of text")
        elif len(value) > limit:
            problems.append(f"manifest.{key}: more than {limit}")
    requirements = manifest.get("requirements")
    if isinstance(requirements, list) and any(
        isinstance(requirement, str) and len(requirement) > MAX_REQUIREMENT_LENGTH
        for requirement in requirements
    ):
        problems.append(
            f"manifest.requirements: one is longer than {MAX_REQUIREMENT_LENGTH} characters"
        )
    size = len(json.dumps(manifest, ensure_ascii=False, separators=(",", ":")).encode())
    if size > MAX_MANIFEST_SIZE:
        problems.append(f"manifest: {size} bytes as JSON, more than {MAX_MANIFEST_SIZE}")
    return problems


def _check_result_problems(
    channel: str, result: CheckResult, channels: Mapping[str, Channel]
) -> list[str]:
    """Return what is wrong in the result of the checks of a version on a channel."""
    problems: list[str] = []
    if channel not in channels:
        problems.append(f"checks: {channel!r} is not a channel in channels.json")
    if result.passed and result.reason is not None:
        problems.append(f"checks.{channel}: a version that passed has no reason")
    if not result.passed and not (result.reason or "").strip():
        problems.append(f"checks.{channel}: a version that did not pass needs a reason")
    elif result.reason is not None and not formats.is_reason(result.reason):
        problems.append(
            f"checks.{channel}.reason: is not one line of at most {formats.MAX_REASON} printable "
            "characters"
        )
    problems += _date_problems(f"checks.{channel}.checked", result.checked)
    return problems


def _status_problems(provider: ProviderFile) -> list[str]:
    """Return what is wrong in the status of a provider and its reason."""
    problems: list[str] = []
    if provider.status == ProviderStatus.LISTED:
        if provider.reason is not None or provider.reason_url is not None:
            problems.append("reason: a listed provider has no reason or reason_url")
    elif not (provider.reason or "").strip():
        problems.append(f"reason: a {provider.status} provider needs a reason")
    if provider.reason_url is not None and not formats.is_https_url(provider.reason_url):
        problems.append(f"reason_url: {provider.reason_url!r} is not an https address")
    return problems


def _date_problems(key: str, value: str) -> list[str]:
    """Return what is wrong in a date."""
    if formats.is_date(value):
        return []
    return [f"{key}: {value!r} is not a date in UTC like 2026-11-14T08:00:00Z"]


def _folder_problem(folder: str, domain: str) -> str:
    """Return the problem of a provider folder that is not valid."""
    return (
        f"folder: {folder!r} is not a relative path in the repository whose last folder is "
        f"named {domain} or {formats.PROVIDER_FOLDER_NAME}"
    )
