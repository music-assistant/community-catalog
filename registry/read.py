"""The read step: each new version read against the rules of the community catalog, by Claude."""

from __future__ import annotations

import sys
import tarfile
import tempfile
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING

from registry.content import content_hash, unpack
from registry.edit import now, write_indexes, write_version
from registry.formats import format_date
from registry.models import ReadingResult, Verdict, VersionReading
from registry.pipeline import archive_path, reading_base, versions_to_read, write_readings
from registry.reading import (
    ReadingConfigurationError,
    ReadingRejectedError,
    ReadingUnavailableError,
    Side,
    TokenUsage,
    read_release,
)

if TYPE_CHECKING:
    import anthropic

    from registry.models import Reading
    from registry.registry import Provider, Registry

# the most providers whose request the API refuses as not valid in one run before the reading
# stops: then the request of the reading is wrong, rather than those releases
MAX_REJECTED = 3


@dataclass
class ReadCounts:
    """How many versions a run of the read step passed, held, and left for another night."""

    passed: int = 0
    held: int = 0
    pending: int = 0
    # the tokens of all its requests
    usage: TokenUsage = field(default_factory=TokenUsage)


def read_versions(registry: Registry, client: anthropic.Anthropic, work: Path) -> ReadCounts:
    """
    Read the versions versions_to_read gives, and record the verdict of each in its version file.

    A version is read from its archive in the work folder, against the archive of the base
    reading_base gives when its turn comes, so one that passed earlier in the run can be it. A
    version whose archives are missing or do not hold its content, or that the API could not
    read for now, gets no verdict and is read on another night. So does one whose request the
    API refused as not valid, and the later versions of its provider in this run; once that
    happened to MAX_REJECTED providers, the reading stops. After each verdict, the readings of
    this run are written into the work folder for the report; at the end, the index files.

    :param registry: The registry.
    :param client: The client of the Claude API.
    :param work: The work folder, with the archives of the fetch step.
    :raises ReadingConfigurationError: The credentials or the configuration of the reading are
        wrong; the verdicts before are recorded.
    """
    counts = ReadCounts()
    results: list[ReadingResult] = []
    rejected: set[str] = set()
    try:
        for planned in versions_to_read(registry):
            if planned.domain in rejected:
                _warn(planned.domain, planned.version, "the API refused an earlier version of it")
                counts.pending += 1
                continue
            provider = registry.providers[planned.domain]
            base = reading_base(provider, planned.version)
            try:
                reading = _read(provider, planned.version, base, client, work, counts.usage)
            except ReadingRejectedError as err:
                _warn(planned.domain, planned.version, err)
                rejected.add(planned.domain)
                if len(rejected) >= MAX_REJECTED:
                    msg = f"the API refused the requests of {len(rejected)} providers in this run"
                    raise ReadingConfigurationError(msg) from err
                reading = None
            if reading is None:
                counts.pending += 1
                continue
            file = provider.versions[planned.version]
            recorded = VersionReading(verdict=reading.verdict, read=format_date(now()), base=base)
            write_version(registry, planned.domain, replace(file, reading=recorded))
            results.append(
                ReadingResult(
                    domain=planned.domain, version=planned.version, base=base, reading=reading
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
    provider: Provider,
    version: str,
    base: str | None,
    client: anthropic.Anthropic,
    work: Path,
    usage: TokenUsage,
) -> Reading | None:
    """
    Return the reading of a version, or None after a warning when it cannot be read now.

    The tokens its requests used are logged and added to the usage.

    :param provider: The provider.
    :param version: The version.
    :param base: The version it is read against; None to read it in full.
    :param client: The client of the Claude API.
    :param work: The work folder, with the archives.
    :param usage: Where the tokens of the requests of the run are added up.
    :raises ReadingRejectedError: The API refused the request as not valid.
    :raises ReadingConfigurationError: The credentials or the configuration of the reading are
        wrong.
    """
    domain, used = provider.file.domain, TokenUsage()
    try:
        with tempfile.TemporaryDirectory() as folder:
            release = _side(provider, version, work, Path(folder, "release"))
            base_side = None
            if base is not None:
                base_side = _side(provider, base, work, Path(folder, "base"))
            return read_release(client, release, base_side, used)
    except (ReadingUnavailableError, ValueError, OSError, tarfile.TarError, EOFError) as err:
        _warn(domain, version, err)
        return None
    finally:
        if used.input_tokens or used.output_tokens:
            print(
                f"{domain} {version}: {used.input_tokens} input, {used.output_tokens} output tokens"
            )
            usage.input_tokens += used.input_tokens
            usage.output_tokens += used.output_tokens


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


def _warn(domain: str, version: str, reason: object) -> None:
    """Print the warning of a version that is read on another night, with the reason."""
    print(f"warning: {domain} {version}: not read: {reason}", file=sys.stderr)
