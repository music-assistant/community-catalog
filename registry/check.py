"""
The check step, which runs inside the official server image of a line and executes provider code.

Only the standard library, the registry and what the server image has are used here. The
verdict on the requirements comes from the server's own code, so it is exactly what a server of
the line decides.
"""

from __future__ import annotations

import asyncio
import shutil
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING

from music_assistant.controllers.community_catalog.dependencies import (
    CommunityDependencies,
    DependenciesUnavailable,
    DependencyConflict,
)

from registry import formats
from registry.checks import check_manifest, reserved_reason
from registry.content import content_hash, read_folder, unpack
from registry.formats import format_date
from registry.models import LineResults, VersionResult
from registry.pipeline import archive_path, versions_to_check

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime

    from registry.models import ReservedDomains, VersionFile
    from registry.registry import Registry

# an import that takes longer than this hangs
LOAD_TIMEOUT = 300
# the folder that holds the registry package, which the load process imports
_CODE_ROOT = Path(__file__).parent.parent
# a version file keeps one short line; the issue for the maintainer gets the output
_MAX_REASON = 300
_MAX_OUTPUT = 50_000


class CheckUnavailableError(Exception):
    """A check could not run, for a reason that is not the provider's; it is tried again later."""


@dataclass(frozen=True, slots=True)
class Outcome:
    """The verdict of the checks on one provider folder."""

    passed: bool
    reason: str | None = None
    output: str | None = None


async def check_line(registry: Registry, line: str, work: Path, now: datetime) -> LineResults:
    """
    Run the checks for every version that needs a result on a server line.

    A version whose archive is not in the work folder, or whose check could not run, gets no
    result and is checked on another night.

    :param registry: The registry.
    :param line: The server line, which must be the line of this server image.
    :param work: The work folder with the archives of the fetch step.
    :param now: When the versions are checked.
    """
    results: list[VersionResult] = []
    for domain, version in versions_to_check(registry, line):
        file = registry.providers[domain].versions[version]
        archive = archive_path(work, domain, version)
        if not archive.is_file():
            print(f"{domain} {version}: skipped, its archive is not in the work folder")
            continue
        try:
            outcome = await _check_archive(archive, domain, file)
        except CheckUnavailableError as err:
            print(f"{domain} {version}: skipped, the check could not run: {err}")
            continue
        previous = file.checks.get(line)
        results.append(
            VersionResult(
                domain=domain,
                version=version,
                passed=outcome.passed,
                reason=outcome.reason,
                output=outcome.output,
                passed_before=previous is not None and previous.passed,
            )
        )
        print(f"{domain} {version}: {'passed' if outcome.passed else outcome.reason}")
    return LineResults(line=line, checked=format_date(now), results=results)


async def check_folder(folder: Path, reserved: ReservedDomains) -> Outcome:
    """
    Run the checks on a provider folder of a checkout, as a provider's own CI does.

    :param folder: The provider folder, named after its domain.
    :param reserved: The domains a community provider cannot take.
    :raises CheckUnavailableError: A check could not run, for a reason that is not the provider's.
    """
    domain = folder.name
    try:
        content = read_folder(folder)
    except (ValueError, OSError) as err:
        return _failed("content", str(err))
    problems = check_manifest(content.manifest, domain)
    if not formats.is_domain(domain):
        problems.append(f"{domain!r} is not a provider domain")
    elif reason := reserved_reason(domain, reserved):
        problems.append(f"the domain {domain} is {reason}")
    requirements = content.manifest.get("requirements", [])
    if isinstance(requirements, list):
        problems += [
            f"requirements: {requirement!r} is not a package name with version specifiers"
            for requirement in requirements
            if not (isinstance(requirement, str) and formats.is_requirement(requirement))
        ]
    if problems:
        return _failed("manifest", "\n".join(problems))
    with tempfile.TemporaryDirectory() as scratch:
        location = Path(scratch, "providers")
        shutil.copytree(folder, location / domain)
        return await _check_provider(location, domain, requirements, Path(scratch))


async def _check_archive(archive: Path, domain: str, file: VersionFile) -> Outcome:
    """
    Run the checks on the provider folder in the archive of a version.

    :param archive: The GitHub archive of the commit of the version.
    :param domain: The domain of the provider.
    :param file: The version file.
    :raises CheckUnavailableError: A check could not run, for a reason that is not the provider's.
    """
    with tempfile.TemporaryDirectory() as scratch:
        location = Path(scratch, "providers")
        try:
            unpack(archive, file.folder, location / domain)
            found = content_hash(location / domain)
        except (ValueError, OSError) as err:
            return _failed("content", str(err))
        if found != file.content_hash:
            return _failed(
                "content", f"the archive has the content hash {found}, not the listed one"
            )
        return await _check_provider(location, domain, file.requirements, Path(scratch))


async def _check_provider(
    location: Path, domain: str, requirements: Sequence[str], scratch: Path
) -> Outcome:
    """
    Resolve and install the requirements of a provider the way the server does, then import it.

    :param location: The folder that holds the provider folder.
    :param domain: The domain of the provider, also the name of its folder.
    :param requirements: The requirements of its manifest.
    :param scratch: A folder for the installed requirements.
    :raises CheckUnavailableError: uv failed or the package index could not be reached.
    """
    cache = scratch / "cache"
    dependencies = CommunityDependencies(SimpleNamespace(cache_path=str(cache)))
    try:
        await dependencies.sync(await dependencies.resolve({domain: list(requirements)}))
    except DependencyConflict as err:
        return _failed("dependencies", str(err))
    except DependenciesUnavailable as err:
        raise CheckUnavailableError(str(err)) from err
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "registry.load",
        str(location),
        domain,
        str(cache),
        cwd=_CODE_ROOT,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), LOAD_TIMEOUT)
    except TimeoutError:
        process.kill()
        await process.wait()
        return _failed("load", f"the import did not finish within {LOAD_TIMEOUT} seconds")
    if process.returncode == 0:
        return Outcome(passed=True)
    output = stdout.decode(errors="replace").strip()
    last = output.splitlines()[-1] if output else ""
    stage, separator, message = last.partition(": ")
    if not separator or stage not in {"manifest", "load"}:
        stage, message = "load", f"the import ended with exit code {process.returncode}"
    return _failed(stage, message, output)


def _failed(stage: str, message: str, output: str | None = None) -> Outcome:
    """
    Return the outcome of a check that failed.

    :param stage: The check that failed: content, manifest, dependencies or load.
    :param message: Why it failed.
    :param output: The full output of the check, when it has more than the message.
    """
    # a name in an archive or an output can hold what plain JSON cannot
    message = message.encode(errors="replace").decode()
    line = " ".join(message.split())
    if len(line) > _MAX_REASON:
        line = f"{line[: _MAX_REASON - 1]}…"
    full = (output or message).encode(errors="replace").decode()
    return Outcome(
        passed=False,
        reason=f"{stage}: {line}",
        output=full[-_MAX_OUTPUT:] if len(full) > _MAX_OUTPUT else full,
    )
