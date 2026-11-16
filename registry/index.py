"""The index of each server line, built from the files of the registry."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from registry.files import dumps, read_object
from registry.formats import format_date, is_date, version_key
from registry.models import INDEX_SCHEMA, CatalogIndex, IndexProvider, IndexVersion
from registry.registry import INDEX_FOLDER, SITE_FOLDER, InvalidRegistryError

if TYPE_CHECKING:
    from datetime import datetime
    from pathlib import Path

    from registry.models import VersionFile
    from registry.registry import Provider, Registry

# how many versions of a provider the index of a line lists
LISTED_VERSIONS = 5
# a server gives up on an index it cannot download within 10 seconds, and keeps its old copy
MAX_INDEX_SIZE = 4 * 1024 * 1024


def build_index(registry: Registry, line: str, generated: str) -> CatalogIndex:
    """
    Return the index of a server line.

    Every provider with a version file is in it, also a blocked or withdrawn one, so a server
    still recognises what it has installed.

    :param registry: The registry.
    :param line: The server line.
    :param generated: When the index was generated.
    """
    return CatalogIndex(
        schema=INDEX_SCHEMA,
        server_line=line,
        generated=generated,
        providers={
            domain: _index_provider(provider, line)
            for domain, provider in sorted(registry.providers.items())
            if provider.versions
        },
    )


def render_indexes(registry: Registry, now: datetime) -> dict[Path, bytes]:
    """
    Return the content of the index file of every server line.

    An index whose providers are as in its current file keeps that file's generated date, so
    an unchanged registry gives the same bytes.

    :param registry: The registry.
    :param now: When a changed index is generated.
    :raises InvalidRegistryError: An index is not plain JSON the server reads, or is larger
        than a server downloads in time.
    """
    result: dict[Path, bytes] = {}
    for line in sorted(registry.server_lines):
        path = registry.root / INDEX_FOLDER / f"{line}.json"
        index = build_index(registry, line, format_date(now)).to_dict()
        current = _read_current(path)
        if current is not None and _without_generated(current) == _without_generated(index):
            index["generated"] = current["generated"]
        try:
            result[path] = dumps(index)
        except ValueError as err:
            raise InvalidRegistryError([f"{INDEX_FOLDER}/{line}.json: {err}"]) from err
        if (size := len(result[path])) > MAX_INDEX_SIZE:
            msg = (
                f"{INDEX_FOLDER}/{line}.json: {size} bytes, more than the {MAX_INDEX_SIZE} bytes "
                "a server is sure to download within its 10 seconds"
            )
            raise InvalidRegistryError([msg])
    return result


def stale_index_files(registry: Registry, rendered: dict[Path, bytes]) -> list[str]:
    """
    Return what differs between the published folder and the rendered index files.

    :param registry: The registry.
    :param rendered: The result of render_indexes.
    """
    problems: list[str] = []
    for path, content in rendered.items():
        where = path.relative_to(registry.root).as_posix()
        if not path.is_file():
            problems.append(f"{where}: is missing")
        elif path.read_bytes() != content:
            problems.append(f"{where}: differs from the registry")
    # everything in the site folder is published, so it holds the index files and nothing else
    site = registry.root / SITE_FOLDER
    index_folder = registry.root / INDEX_FOLDER
    problems += [
        f"{path.relative_to(registry.root).as_posix()}: is no index of a line in server_lines.json"
        for path in sorted(site.rglob("*") if site.is_dir() else [])
        if path != index_folder and not (path in rendered and path.is_file())
    ]
    return problems


def _index_provider(provider: Provider, line: str) -> IndexProvider:
    """
    Return a provider as the index of a line lists it.

    :param provider: The provider, with at least one version.
    :param line: The server line.
    """
    file = provider.file
    newest_first = sorted(
        provider.versions.values(), key=lambda version: version_key(version.version), reverse=True
    )
    allowed = [version for version in newest_first if version.version not in file.blocked_versions]
    passed = [
        version
        for version in allowed
        if (result := version.checks.get(line)) is not None and result.passed
    ]
    latest = passed[0] if passed else None
    # without a version for this line the app still shows the provider, by its newest version
    # that is not blocked; a provider with only blocked versions stays in the index all the same
    shown = latest or (allowed or newest_first)[0]
    return IndexProvider(
        status=file.status,
        repository=file.repository,
        manifest=shown.manifest,
        added=file.added,
        reason=file.reason,
        reason_url=file.reason_url,
        maintainers=provider.maintainers(),
        icon_svg=shown.icon_svg,
        latest=latest.version if latest else None,
        versions={version.version: _index_version(version) for version in passed[:LISTED_VERSIONS]},
        blocked_versions=file.blocked_versions,
    )


def _index_version(version: VersionFile) -> IndexVersion:
    """Return a version as the index lists it."""
    return IndexVersion(
        commit=version.commit,
        folder=version.folder,
        content_hash=version.content_hash,
        requirements=version.requirements,
        released=version.released,
    )


def _read_current(path: Path) -> dict[str, Any] | None:
    """Return the current index file, or None when there is none or it cannot be read."""
    try:
        current = read_object(path)
    except OSError, ValueError:
        return None
    generated = current.get("generated")
    return current if isinstance(generated, str) and is_date(generated) else None


def _without_generated(index: dict[str, Any]) -> dict[str, Any]:
    """Return an index without its generated date."""
    return {key: value for key, value in index.items() if key != "generated"}
