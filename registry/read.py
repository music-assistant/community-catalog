"""The read step: each new version read against the rules of the community catalog, by Claude."""

from __future__ import annotations

import sys
import tarfile
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING

from registry.content import content_hash, unpack
from registry.edit import now, write_indexes, write_version
from registry.formats import format_date
from registry.models import ReadingResult, Verdict, VersionReading
from registry.pipeline import archive_path, versions_to_read, write_readings
from registry.reading import ReadingUnavailableError, Side, read_release

if TYPE_CHECKING:
    import anthropic

    from registry.models import Reading
    from registry.pipeline import PlannedReading
    from registry.registry import Provider, Registry


@dataclass
class ReadCounts:
    """How many versions a run of the read step passed, held, and left for another night."""

    passed: int = 0
    held: int = 0
    pending: int = 0


def read_versions(registry: Registry, client: anthropic.Anthropic, work: Path) -> ReadCounts:
    """
    Read the versions versions_to_read gives, and record the verdict of each in its version file.

    A version is read from its archive in the work folder, against the archive of its base. A
    version whose archives are missing or do not hold its content, or that the API could not
    read for now, gets no verdict and is read on another night. After each verdict, the readings
    of this run are written into the work folder for the report; at the end, the index files.

    :param registry: The registry.
    :param client: The client of the Claude API.
    :param work: The work folder, with the archives of the fetch step.
    :raises ReadingConfigurationError: The credentials or the configuration of the reading are
        wrong; the verdicts before are recorded.
    """
    counts = ReadCounts()
    results: list[ReadingResult] = []
    try:
        for planned in versions_to_read(registry):
            provider = registry.providers[planned.domain]
            if (reading := _read(provider, planned, client, work)) is None:
                counts.pending += 1
                continue
            file = provider.versions[planned.version]
            recorded = VersionReading(
                verdict=reading.verdict, read=format_date(now()), base=planned.base
            )
            write_version(registry, planned.domain, replace(file, reading=recorded))
            results.append(
                ReadingResult(
                    domain=planned.domain,
                    version=planned.version,
                    base=planned.base,
                    reading=reading,
                )
            )
            write_readings(results, work)
            if reading.verdict is Verdict.PASS:
                counts.passed += 1
            else:
                counts.held += 1
    finally:
        write_indexes(registry.root)
    return counts


def _read(
    provider: Provider, planned: PlannedReading, client: anthropic.Anthropic, work: Path
) -> Reading | None:
    """
    Return the reading of a version, or None after a warning when it cannot be read now.

    :param provider: The provider.
    :param planned: The version and its base.
    :param client: The client of the Claude API.
    :param work: The work folder, with the archives.
    :raises ReadingConfigurationError: The credentials or the configuration of the reading are
        wrong.
    """
    with tempfile.TemporaryDirectory() as folder:
        try:
            release = _side(provider, planned.version, work, Path(folder, "release"))
            base = None
            if planned.base is not None:
                base = _side(provider, planned.base, work, Path(folder, "base"))
            return read_release(client, release, base)
        except (ReadingUnavailableError, ValueError, OSError, tarfile.TarError, EOFError) as err:
            print(f"warning: {planned.domain} {planned.version}: not read: {err}", file=sys.stderr)
    return None


def _side(provider: Provider, version: str, work: Path, target: Path) -> Side:
    """
    Return a version of a provider, unpacked from its archive in the work folder.

    :param provider: The provider.
    :param version: The version, which has a version file.
    :param work: The work folder.
    :param target: The folder to unpack to, which must not exist.
    :raises FileNotFoundError: The archive is not in the work folder.
    :raises ValueError: The archive does not hold the content of the version file.
    :raises OSError: The archive cannot be read.
    """
    file = provider.versions[version]
    archive = archive_path(work, provider.file.domain, version)
    if not archive.is_file():
        msg = f"the archive of {version} is not in the work folder"
        raise FileNotFoundError(msg)
    unpack(archive, file.folder, target)
    if content_hash(target) != file.content_hash:
        msg = f"the archive of {version} does not hold the content of its version file"
        raise ValueError(msg)
    commit_url = f"https://github.com/{provider.file.repository}/commit/{file.commit}"
    return Side(target, version, commit_url)
