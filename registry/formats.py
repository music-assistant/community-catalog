"""The formats of the values in the files of the registry."""

from __future__ import annotations

import keyword
import re
from datetime import UTC, datetime
from urllib.parse import urlsplit

DATE_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
# the longest reason of a failed check a version file keeps
MAX_REASON = 300
# the name a provider folder may have besides its domain
PROVIDER_FOLDER_NAME = "provider"

_DOMAIN = re.compile(r"[a-z][a-z0-9_]*")
_VERSION = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")
_SERVER_LINE = re.compile(r"nightly|beta|[0-9]+\.[0-9]+")
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")
_COMMIT = re.compile(r"[0-9a-f]{40}")
_CONTENT_HASH = re.compile(r"[0-9a-f]{64}")
# GitHub: letters, digits and single hyphens, not at the start or the end
_GITHUB_HANDLE = re.compile(r"[A-Za-z0-9](?:-?[A-Za-z0-9])*")
_GITHUB_HANDLE_MAX = 39
_REPOSITORY_NAME = re.compile(r"[A-Za-z0-9._-]+")
_PATH_PART = re.compile(r"[A-Za-z0-9._-]+")
_TAG_PREFIX = re.compile(r"[A-Za-z0-9._/-]*")
# a container image with a tag, such as ghcr.io/music-assistant/server:nightly
_IMAGE = re.compile(r"[a-z0-9]+(?:[._/-][a-z0-9]+)*:[A-Za-z0-9_][A-Za-z0-9._-]{0,127}")
# the same expression as _PLAIN_REQUIREMENT in dependencies.py of the server, which refuses
# anything else: a package name with optional extras and version specifiers, never a URL or an
# option for the resolver
_REQUIREMENT = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9._-]*(\[[A-Za-z0-9._, -]+\])?"
    r"(\s*(==|>=|<=|~=|!=|<|>)\s*[A-Za-z0-9.*+!_-]+"
    r"(\s*,\s*(==|>=|<=|~=|!=|<|>)\s*[A-Za-z0-9.*+!_-]+)*)?"
)


def is_domain(value: str) -> bool:
    """
    Return whether a value is a valid domain for a community provider.

    :param value: The value to check.
    """
    return bool(_DOMAIN.fullmatch(value)) and not keyword.iskeyword(value)


def is_version(value: str) -> bool:
    """
    Return whether a value is a version: MAJOR.MINOR.PATCH, numbers without leading zeros.

    :param value: The value to check.
    """
    return bool(_VERSION.fullmatch(value))


def version_key(version: str) -> tuple[int, ...]:
    """
    Return the key that orders versions as versions, not as text.

    :param version: A valid version.
    """
    return tuple(int(part) for part in version.split("."))


def is_server_line(value: str) -> bool:
    """
    Return whether a value names a server line: nightly, beta or MAJOR.MINOR.

    :param value: The value to check.
    """
    return bool(_SERVER_LINE.fullmatch(value))


def is_date(value: str) -> bool:
    """
    Return whether a value is a date and time in UTC, as 2026-11-14T08:00:00Z.

    :param value: The value to check.
    """
    if not _DATE.fullmatch(value):
        return False
    try:
        datetime.strptime(value, DATE_FORMAT).replace(tzinfo=UTC)
    except ValueError:
        return False
    return True


def format_date(value: datetime) -> str:
    """
    Return a moment in the format of the dates in the registry.

    :param value: An aware datetime.
    """
    return value.astimezone(UTC).strftime(DATE_FORMAT)


def is_commit(value: str) -> bool:
    """
    Return whether a value is a full commit SHA in lower case.

    :param value: The value to check.
    """
    return bool(_COMMIT.fullmatch(value))


def is_content_hash(value: str) -> bool:
    """
    Return whether a value is a sha256 hex digest in lower case.

    :param value: The value to check.
    """
    return bool(_CONTENT_HASH.fullmatch(value))


def is_github_handle(value: str) -> bool:
    """
    Return whether a value is a GitHub handle, without the @.

    :param value: The value to check.
    """
    return len(value) <= _GITHUB_HANDLE_MAX and bool(_GITHUB_HANDLE.fullmatch(value))


def is_repository(value: str) -> bool:
    """
    Return whether a value names a GitHub repository as owner/name.

    :param value: The value to check.
    """
    owner, _, name = value.partition("/")
    return (
        is_github_handle(owner)
        and bool(_REPOSITORY_NAME.fullmatch(name))
        and name not in {".", ".."}
    )


def is_provider_folder(value: str, domain: str) -> bool:
    """
    Return whether a value is a folder in a repository named after the domain or provider.

    :param value: A relative path with forward slashes, such as providers/foo.
    :param domain: The domain of the provider.
    """
    parts = value.split("/")
    valid = all(_PATH_PART.fullmatch(part) and part not in {".", ".."} for part in parts)
    return valid and parts[-1] in {domain, PROVIDER_FOLDER_NAME}


def is_tag_prefix(value: str) -> bool:
    """
    Return whether a value can start the release tags of a provider.

    :param value: The value to check.
    """
    return bool(_TAG_PREFIX.fullmatch(value))


def release_version(tag: str, tag_prefix: str) -> str | None:
    """
    Return the version a release tag names, or None when it names none for this provider.

    :param tag: The tag of the release: the tag prefix and the version, with or without v.
    :param tag_prefix: What the release tags of the provider start with.
    """
    if not tag.startswith(tag_prefix):
        return None
    version = tag.removeprefix(tag_prefix)
    version = version.removeprefix("v")
    return version if is_version(version) else None


def is_image(value: str) -> bool:
    """
    Return whether a value names a container image with a tag.

    :param value: The value to check.
    """
    return bool(_IMAGE.fullmatch(value))


def is_requirement(value: str) -> bool:
    """
    Return whether a value is a requirement the server installs: a package name with specifiers.

    A requirement is one line of printable characters.

    :param value: The value to check.
    """
    # \s in the expression of the server also matches line breaks and other control characters
    return value.isprintable() and bool(_REQUIREMENT.fullmatch(value))


def is_reason(value: str) -> bool:
    """
    Return whether a value is the reason of a check that failed: one line of printable text.

    :param value: The value to check.
    """
    return bool(value.strip()) and len(value) <= MAX_REASON and value.isprintable()


def is_https_url(value: str) -> bool:
    """
    Return whether a value is an https address.

    :param value: The value to check.
    """
    parts = urlsplit(value)
    return parts.scheme == "https" and bool(parts.hostname) and not any(c.isspace() for c in value)
