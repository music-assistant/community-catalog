"""
The check step, which runs inside the official server image of a channel and executes provider code.

Only the standard library, the registry and what the server image has are used here. The
verdict on the requirements comes from the server's own code, so it is exactly what a server on
the channel decides.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import signal
import sys
import tempfile
import traceback
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING

from music_assistant.controllers.community_catalog import dependencies
from music_assistant.controllers.community_catalog.dependencies import (
    CommunityDependencies,
    DependenciesUnavailable,
    DependencyConflict,
)

from registry import formats
from registry.checks import check_manifest, reserved_reason
from registry.content import content_hash, read_folder, unpack
from registry.formats import MAX_REASON

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from registry.models import ReservedDomains, VersionFile

# an import that takes longer than this hangs
LOAD_TIMEOUT = 300
# the folder that holds the registry package, which the load process imports
_CODE_ROOT = Path(__file__).parent.parent
# the issue for the maintainer gets the end of the output
_MAX_OUTPUT = 50_000
# where the requirements are installed, in the scratch folder of a version
_CACHE = "cache"
# uv exits with 1 when the requirements cannot be resolved
_UV_UNRESOLVABLE = 1
# the glibc versions uv knows a manylinux target for, as 2.<minor>
_MANYLINUX_MINORS = (17, 28, *range(31, 41))


class CheckUnavailableError(Exception):
    """A check could not run, for a reason that is not the provider's; it is tried again later."""


@dataclass(frozen=True, slots=True)
class Outcome:
    """The verdict of the checks on one provider folder."""

    passed: bool
    reason: str | None = None
    output: str | None = None
    # the check could not run in this server image, for a reason that is not the provider's
    unavailable: bool = False


async def install_version(
    archive: Path, file: VersionFile, domain: str, scratch: Path
) -> Outcome | None:
    """
    Check the content of a version and install its requirements the way the server does.

    Runs no provider code. Returns the outcome when the version fails here, None when it is
    ready for load_version.

    :param archive: The GitHub archive of the commit of the version.
    :param file: The version file.
    :param domain: The domain of the provider.
    :param scratch: An empty folder that keeps the installed requirements for load_version.
    :raises CheckUnavailableError: uv failed or the package index could not be reached.
    """
    with tempfile.TemporaryDirectory() as folder:
        location = Path(folder, "providers")
        if (failed := _unpack_version(archive, file, location / domain)) is not None:
            return failed
    return await _install(domain, file.requirements, scratch / _CACHE)


async def load_version(archive: Path, file: VersionFile, domain: str, scratch: Path) -> Outcome:
    """
    Import a version the way the server does, with the requirements install_version installed.

    :param archive: The GitHub archive of the commit of the version.
    :param file: The version file.
    :param domain: The domain of the provider.
    :param scratch: The folder install_version installed the requirements in; only read.
    """
    with tempfile.TemporaryDirectory() as folder:
        location = Path(folder, "providers")
        if (failed := _unpack_version(archive, file, location / domain)) is not None:
            return failed
        return await _load(location, domain, scratch / _CACHE, Path(folder))


async def check_folder(folder: Path, reserved: ReservedDomains) -> Outcome:
    """
    Run the checks on a provider folder of a checkout, as a provider's own CI does.

    :param folder: The provider folder, named after the domain its manifest names or provider.
    :param reserved: The domains a community provider cannot take.
    :raises CheckUnavailableError: A check could not run, for a reason that is not the provider's.
    """
    try:
        content = read_folder(folder)
    except (ValueError, OSError) as err:
        return _failed("content", str(err))
    domain = content.manifest.get("domain")
    if not (isinstance(domain, str) and formats.is_domain(domain)):
        return _failed("manifest", f"manifest.domain: {domain!r} is not a provider domain")
    problems = check_manifest(content.manifest, domain)
    if folder.name not in {domain, formats.PROVIDER_FOLDER_NAME}:
        problems.append(
            f"folder: {folder.name!r} is named neither {domain} nor {formats.PROVIDER_FOLDER_NAME}"
        )
    if reason := reserved_reason(domain, reserved):
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
        cache = Path(scratch, _CACHE)
        if (failed := await _install(domain, requirements, cache)) is not None:
            return failed
        return await _load(location, domain, cache, Path(scratch))


def arm64_platform(libc: str | None) -> str:
    """
    Return the uv target of linux arm64 for a C library, the newest one it can run.

    :param libc: The C library, as ``os.confstr("CS_GNU_LIBC_VERSION")`` gives it, such as
        ``glibc 2.41``; outside a server image it may be another or none.
    """
    try:
        # outside a server image, such as in the tests, it need not be glibc 2.x
        minor = int((libc or "").split()[1].split(".")[1])
    except IndexError, ValueError:
        minor = 0
    known = [target for target in _MANYLINUX_MINORS if target <= minor] or [_MANYLINUX_MINORS[0]]
    return f"aarch64-manylinux_2_{known[-1]}"


def _unpack_version(archive: Path, file: VersionFile, target: Path) -> Outcome | None:
    """
    Unpack the provider folder of a version, and return the outcome when it is not as listed.

    :param archive: The GitHub archive of the commit of the version.
    :param file: The version file.
    :param target: The folder to unpack to, which must not exist.
    """
    try:
        unpack(archive, file.folder, target)
        found = content_hash(target)
    except (ValueError, OSError) as err:
        return _failed("content", str(err))
    if found != file.content_hash:
        return _failed("content", f"the archive has the content hash {found}, not the listed one")
    return None


async def _install(domain: str, requirements: Sequence[str], cache: Path) -> Outcome | None:
    """
    Resolve and install the requirements of a provider the way the server does.

    Returns the outcome when they clash with the server or cannot be installed, else None.

    :param domain: The domain of the provider.
    :param requirements: The requirements of its manifest.
    :param cache: The folder to install them in.
    :raises CheckUnavailableError: uv failed or the package index could not be reached.
    """
    dependencies = CommunityDependencies(SimpleNamespace(cache_path=str(cache)))
    try:
        await dependencies.sync(await dependencies.resolve({domain: list(requirements)}))
    except DependencyConflict as err:
        return _failed("dependencies", str(err))
    except DependenciesUnavailable as err:
        raise CheckUnavailableError(str(err)) from err
    if requirements and (cause := await _resolve_for_arm64(requirements)) is not None:
        return _failed(
            "dependencies", f"Requirements of {domain} have no wheels for linux arm64", cause
        )
    return None


async def _resolve_for_arm64(requirements: Sequence[str]) -> str | None:
    """
    Resolve requirements as a server on linux arm64 would, without installing anything.

    The checks run on amd64. The arm64 image has the same packages and C library, and a server
    installs wheels only, so a resolve for that platform tells whether it can install them.
    Returns the explanation of uv when they cannot be resolved there, else None.

    :param requirements: The requirements of the manifest.
    :raises CheckUnavailableError: uv failed or the package index could not be reached.
    """
    index_args, server_constraints = _server_resolve_settings()
    with tempfile.TemporaryDirectory() as workdir:
        wanted, server = await asyncio.to_thread(
            _write_resolve_inputs, Path(workdir), requirements, server_constraints
        )
        # the same interpreter, indexes and pins the server resolves with
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "uv", "pip", "compile", str(wanted),
            "--constraint", str(server), "--python", sys.executable,
            "--python-platform", arm64_platform(_libc()),
            "--only-binary", ":all:", "--no-header", "--no-annotate", "--no-config", "--no-cache",
            *index_args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )  # fmt: skip
        output, _ = await process.communicate()
    text = output.decode(errors="replace").strip()
    if process.returncode == 0:
        return None
    if process.returncode == _UV_UNRESOLVABLE:
        return text
    msg = f"uv pip compile for linux arm64 failed with exit code {process.returncode}: {text}"
    raise CheckUnavailableError(msg)


def _server_resolve_settings() -> tuple[Sequence[str], Callable[[], list[str]]]:
    """
    Return the package indexes and the function that lists the pins the server resolves with.

    They are internals of the server's community catalog, so a server image can lack them.

    :raises CheckUnavailableError: The server of this image lacks one of them.
    """
    try:
        # the server has no public name for them
        return dependencies._UV_INDEX_ARGS, dependencies._server_constraints  # noqa: SLF001
    except AttributeError as err:
        msg = f"the server of this image has no {err.name} in {dependencies.__name__}"
        raise CheckUnavailableError(msg) from err


def _libc() -> str | None:
    """Return the C library of this system, None when it does not say."""
    try:
        return os.confstr("CS_GNU_LIBC_VERSION")
    except ValueError, OSError:
        return None


def _write_resolve_inputs(
    workdir: Path, requirements: Sequence[str], server_constraints: Callable[[], list[str]]
) -> tuple[Path, Path]:
    """Write the requirements and the server's pins for a resolve, and return both files."""
    wanted = workdir / "requirements.txt"
    wanted.write_text("\n".join(requirements) + "\n", encoding="utf-8")
    server = workdir / "server.txt"
    server.write_text("\n".join(server_constraints()) + "\n", encoding="utf-8")
    return wanted, server


async def _load(location: Path, domain: str, cache: Path, scratch: Path) -> Outcome:
    """
    Import a provider in a process of its own, the way the server does.

    :param location: The folder that holds the provider folder.
    :param domain: The domain of the provider, also the name of its folder.
    :param cache: The folder its requirements were installed in.
    :param scratch: A folder for the output of the import.
    """
    try:
        # what the load script imports from the server, before any provider code runs
        import registry.load  # noqa: F401, PLC0415
    except ImportError as err:
        message = f"the load script cannot start in this server image: {err}"
        return _failed("load", message, "".join(traceback.format_exception(err)), unavailable=True)
    log = scratch / "load.log"
    with log.open("wb") as output:
        # a session of its own, so the processes the import starts end with it
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "registry.load",
            str(location),
            domain,
            str(cache),
            cwd=_CODE_ROOT,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=output,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            await asyncio.wait_for(process.wait(), LOAD_TIMEOUT)
        except TimeoutError:
            return _failed("load", f"the import did not finish within {LOAD_TIMEOUT} seconds")
        finally:
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            await process.wait()
    if process.returncode == 0:
        return Outcome(passed=True)
    output_text = _tail(log).strip()
    last = output_text.splitlines()[-1] if output_text else ""
    stage, separator, message = last.partition(": ")
    if not separator or stage not in {"manifest", "load"}:
        stage, message = "load", f"the import ended with exit code {process.returncode}"
    return _failed(stage, message, output_text)


def _tail(path: Path) -> str:
    """Return the end of a file, at most the output an issue gets."""
    with path.open("rb") as file:
        file.seek(max(0, file.seek(0, os.SEEK_END) - _MAX_OUTPUT))
        return file.read().decode(errors="replace")


def _failed(
    stage: str, message: str, output: str | None = None, *, unavailable: bool = False
) -> Outcome:
    """
    Return the outcome of a check that failed.

    :param stage: The check that failed: content, manifest, dependencies or load.
    :param message: Why it failed.
    :param output: The full output of the check, when it has more than the message.
    :param unavailable: The check could not run, for a reason that is not the provider's.
    """
    # a name in an archive or an output can hold what plain JSON or one line cannot
    line = " ".join(message.encode(errors="replace").decode().split())
    line = "".join(char if char.isprintable() else "\ufffd" for char in line)
    reason = f"{stage}: {line}"
    if len(reason) > MAX_REASON:
        reason = f"{reason[: MAX_REASON - 1]}…"
    full = (output or message).encode(errors="replace").decode()
    return Outcome(passed=False, reason=reason, output=full[-_MAX_OUTPUT:], unavailable=unavailable)
