"""What the steps of the release pipeline share: the work folder, what to check, the results."""

from __future__ import annotations

from typing import TYPE_CHECKING

from registry.edit import CommandError, record_check, write_indexes
from registry.files import load_model
from registry.formats import version_key
from registry.index import build_index
from registry.models import LineResults, ProviderStatus

if TYPE_CHECKING:
    from pathlib import Path

    from registry.registry import Registry

# the files in the work folder
FETCH_REPORT = "fetch.json"
ADMISSION_REPORT = "admission.json"
ISSUES_FOLDER = "issues"
_ARCHIVES_FOLDER = "archives"


def archive_path(work: Path, domain: str, version: str) -> Path:
    """
    Return where the fetch step keeps the GitHub archive of a version for the check step.

    :param work: The work folder.
    :param domain: The domain of the provider.
    :param version: The version.
    """
    return work / _ARCHIVES_FOLDER / domain / f"{version}.tar.gz"


def versions_to_check(registry: Registry, line: str) -> list[tuple[str, str]]:
    """
    Return the versions the checks run for on a server line, as (domain, version).

    Those are the versions without a result for the line, and the latest version of each
    provider on the line again. A frozen line, a blocked provider and a blocked version are
    not checked.

    :param registry: The registry.
    :param line: The server line.
    """
    if registry.server_lines[line].frozen:
        return []
    index = build_index(registry, line, "")
    result: list[tuple[str, str]] = []
    for domain, provider in sorted(registry.providers.items()):
        if provider.file.status == ProviderStatus.BLOCKED:
            continue
        latest = index.providers[domain].latest if domain in index.providers else None
        for version in sorted(provider.versions, key=version_key):
            if version in provider.file.blocked_versions:
                continue
            if line not in provider.versions[version].checks or version == latest:
                result.append((domain, version))
    return result


def read_results(folder: Path) -> list[LineResults]:
    """
    Return the results files of the check step in a folder, ordered by name.

    A folder that does not exist holds none.

    :param folder: The folder with the results files, one per server line.
    :raises CommandError: A results file is a symbolic link or not exactly a results file.
    """
    results: list[LineResults] = []
    for path in sorted(folder.glob("*.json")) if folder.is_dir() else []:
        if path.is_symlink() or not path.is_file():
            msg = f"{path}: is not a results file"
            raise CommandError(msg)
        try:
            results.append(load_model(path, LineResults))
        except (ValueError, OSError) as err:
            msg = f"{path}: {err}"
            raise CommandError(msg) from err
    return results


def apply_results(registry: Registry, results: list[LineResults]) -> None:
    """
    Record the results of the check step in the version files, and write the index files.

    The results are data from a step that ran provider code: every one is checked like any
    other change to a version file.

    :param registry: The registry.
    :param results: The results files of the server lines.
    :raises CommandError: A results file is for a line that is not tested, or for a version
        the registry does not have.
    :raises InvalidRegistryError: A result would make a version file invalid.
    """
    lines = [file.line for file in results]
    if len(set(lines)) != len(lines):
        raise CommandError("there are several results files for one server line")
    for file in results:
        if (line := registry.server_lines.get(file.line)) is None or line.frozen:
            msg = f"{file.line!r} is not a server line that is tested"
            raise CommandError(msg)
        for result in file.results:
            reason = None if result.passed else (result.reason or "")
            record_check(
                registry,
                result.domain,
                result.version,
                file.line,
                reason=reason,
                checked=file.checked,
            )
    write_indexes(registry.root)
