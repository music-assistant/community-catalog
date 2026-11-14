"""The registry as a whole: every file read and checked."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from registry.checks import check_provider, check_server_lines, check_version
from registry.files import load_model
from registry.models import ProviderFile, ReservedDomains, ServerLines, VersionFile

if TYPE_CHECKING:
    from pathlib import Path

    from registry.models import ServerLine, StrictModel

PROVIDERS_FOLDER = "providers"
PROVIDER_FILE = "provider.json"
VERSIONS_FOLDER = "versions"
SERVER_LINES_FILE = "server_lines.json"
RESERVED_DOMAINS_FILE = "reserved_domains.json"
# the folder that is published; a server reads <published address>/v1/<line>.json
SITE_FOLDER = "site"
INDEX_FOLDER = "site/v1"


class InvalidRegistryError(Exception):
    """The registry has files that are not exactly as expected."""

    def __init__(self, problems: list[str]) -> None:
        """
        Initialize the error.

        :param problems: Every problem, each starting with the file it is in.
        """
        super().__init__("\n".join(problems))
        self.problems = problems


@dataclass
class Provider:
    """A provider of the registry and its versions."""

    file: ProviderFile
    # by version
    versions: dict[str, VersionFile]


@dataclass
class Registry:
    """The content of the registry, every file of it valid."""

    root: Path
    server_lines: dict[str, ServerLine]
    reserved: ReservedDomains
    # by domain
    providers: dict[str, Provider]

    def version_path(self, domain: str, version: str) -> Path:
        """
        Return the file of a version of a provider.

        :param domain: The domain of the provider.
        :param version: The version.
        """
        return self.root / PROVIDERS_FOLDER / domain / VERSIONS_FOLDER / f"{version}.json"


def load_registry(root: Path) -> Registry:
    """
    Return the registry in a folder, after checking every file of it.

    :param root: The root folder of the registry.
    :raises InvalidRegistryError: A file is missing or not exactly as expected.
    """
    problems: list[str] = []
    lines = _load(root, root / SERVER_LINES_FILE, ServerLines, problems)
    if lines is not None:
        problems += [f"{SERVER_LINES_FILE}: {problem}" for problem in check_server_lines(lines)]
    reserved = _load(root, root / RESERVED_DOMAINS_FILE, ReservedDomains, problems)
    if lines is None or reserved is None:
        raise InvalidRegistryError(problems)
    providers: dict[str, Provider] = {}
    folder = root / PROVIDERS_FOLDER
    for path in sorted(folder.iterdir()) if folder.is_dir() else []:
        if provider := _load_provider(root, path, lines.lines, reserved, problems):
            providers[provider.file.domain] = provider
    if problems:
        raise InvalidRegistryError(problems)
    return Registry(root, lines.lines, reserved, providers)


def _load_provider(
    root: Path,
    folder: Path,
    lines: dict[str, ServerLine],
    reserved: ReservedDomains,
    problems: list[str],
) -> Provider | None:
    """
    Return a provider and its versions, adding what is wrong in its folder to the problems.

    :param root: The root folder of the registry.
    :param folder: The folder of the provider.
    :param lines: The server lines an index is built for.
    :param reserved: The domains the server ships or reserves.
    :param problems: The list to add problems to.
    """
    where = folder.relative_to(root).as_posix()
    if folder.is_symlink() or not folder.is_dir():
        problems.append(f"{where}: is not a provider folder")
        return None
    problems += [
        f"{where}/{path.name}: is not a file of the registry"
        for path in sorted(folder.iterdir())
        if path.name not in {PROVIDER_FILE, VERSIONS_FOLDER}
    ]
    provider = _load(root, folder / PROVIDER_FILE, ProviderFile, problems)
    if provider is None:
        return None
    if provider.domain != folder.name:
        problems.append(f"{where}/{PROVIDER_FILE}: domain: {provider.domain} is not {folder.name}")
    problems += [f"{where}/{PROVIDER_FILE}: {p}" for p in check_provider(provider, reserved)]
    versions: dict[str, VersionFile] = {}
    versions_folder = folder / VERSIONS_FOLDER
    for path in sorted(versions_folder.iterdir()) if versions_folder.is_dir() else []:
        file = path.relative_to(root).as_posix()
        if path.suffix != ".json" or path.is_symlink() or not path.is_file():
            problems.append(f"{file}: is not a version file")
            continue
        if (version := _load(root, path, VersionFile, problems)) is None:
            continue
        if version.version != path.stem:
            problems.append(f"{file}: version: {version.version} is not {path.stem}")
        problems += [f"{file}: {p}" for p in check_version(version, provider, lines)]
        versions[path.stem] = version
    problems += [
        f"{where}/{PROVIDER_FILE}: blocked_versions: {version} has no version file"
        for version in provider.blocked_versions
        if version not in versions
    ]
    return Provider(provider, versions)


def _load[T: StrictModel](root: Path, path: Path, model: type[T], problems: list[str]) -> T | None:
    """
    Return the model in a file, or None after adding why it cannot be read to the problems.

    :param root: The root folder of the registry.
    :param path: The file.
    :param model: The model the file holds.
    :param problems: The list to add a problem to.
    """
    where = path.relative_to(root).as_posix()
    if path.is_symlink() or not path.is_file():
        problems.append(f"{where}: is missing")
        return None
    try:
        return load_model(path, model)
    except (ValueError, OSError) as err:
        problems.append(f"{where}: {err}")
        return None
