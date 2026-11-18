"""The registry as a whole: every file read and checked."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from registry.checks import check_channels, check_provider, check_refused, check_version
from registry.files import load_model
from registry.formats import is_github_handle, version_key
from registry.models import (
    Channels,
    ProviderFile,
    RefusedRelease,
    ReservedDomains,
    VersionFile,
)

if TYPE_CHECKING:
    from pathlib import Path

    from registry.models import Channel, StrictModel

PROVIDERS_FOLDER = "providers"
PROVIDER_FILE = "provider.json"
VERSIONS_FOLDER = "versions"
REFUSED_FOLDER = "refused"
CHANNELS_FILE = "channels.json"
RESERVED_DOMAINS_FILE = "reserved_domains.json"
# the folder that is published; a server reads <published address>/v1/<channel>.json
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
    # the releases that did not become a version, by version
    refused: dict[str, RefusedRelease]

    def maintainers(self) -> list[str]:
        """
        Return the GitHub handles of the maintainers, without @ and each once.

        They are the owner of the repository and the code owners in the manifest of the highest
        version that is offered: not blocked, and passed on a channel. Without such a version
        the owner is the only one.
        """
        offered = [
            file
            for file in self.versions.values()
            if file.version not in self.file.blocked_versions
            and any(check.passed for check in file.checks.values())
        ]
        newest = max(offered, key=lambda file: version_key(file.version), default=None)
        codeowners = newest.manifest.get("codeowners", []) if newest else []
        handles = [self.file.repository.split("/", 1)[0]]
        handles += [owner.removeprefix("@") for owner in codeowners]
        result: list[str] = []
        seen: set[str] = set()
        for handle in handles:
            if is_github_handle(handle) and handle.lower() not in seen:
                seen.add(handle.lower())
                result.append(handle)
        return result


@dataclass
class Registry:
    """The content of the registry, every file of it valid."""

    root: Path
    channels: dict[str, Channel]
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

    def refused_path(self, domain: str, version: str) -> Path:
        """
        Return the file of a refused release of a provider.

        :param domain: The domain of the provider.
        :param version: The version the release names.
        """
        return self.root / PROVIDERS_FOLDER / domain / REFUSED_FOLDER / f"{version}.json"


def load_registry(root: Path) -> Registry:
    """
    Return the registry in a folder, after checking every file of it.

    :param root: The root folder of the registry.
    :raises InvalidRegistryError: A file is missing or not exactly as expected.
    """
    problems: list[str] = []
    channels = _load(root, root / CHANNELS_FILE, Channels, problems)
    if channels is not None:
        problems += [f"{CHANNELS_FILE}: {problem}" for problem in check_channels(channels)]
    reserved = _load(root, root / RESERVED_DOMAINS_FILE, ReservedDomains, problems)
    if channels is None or reserved is None:
        raise InvalidRegistryError(problems)
    # the commands write below these paths, which a link would lead out of the registry
    written = [PROVIDERS_FOLDER, SITE_FOLDER, INDEX_FOLDER]
    written += [f"{INDEX_FOLDER}/{channel}.json" for channel in sorted(channels.channels)]
    problems += [f"{path}: is a symbolic link" for path in written if (root / path).is_symlink()]
    providers: dict[str, Provider] = {}
    folder = root / PROVIDERS_FOLDER
    for path in _entries(folder):
        if provider := _load_provider(root, path, channels.channels, reserved, problems):
            providers[provider.file.domain] = provider
    if problems:
        raise InvalidRegistryError(problems)
    return Registry(root, channels.channels, reserved, providers)


def _load_provider(
    root: Path,
    folder: Path,
    channels: dict[str, Channel],
    reserved: ReservedDomains,
    problems: list[str],
) -> Provider | None:
    """
    Return a provider and its versions, adding what is wrong in its folder to the problems.

    :param root: The root folder of the registry.
    :param folder: The folder of the provider.
    :param channels: The channels an index is built for.
    :param reserved: The domains the server ships or reserves.
    :param problems: The list to add problems to.
    """
    where = folder.relative_to(root).as_posix()
    if folder.is_symlink() or not folder.is_dir():
        problems.append(f"{where}: is not a provider folder")
        return None
    problems += [
        f"{where}/{path.name}: is not a file of the registry"
        for path in _entries(folder)
        if path.name not in {PROVIDER_FILE, VERSIONS_FOLDER, REFUSED_FOLDER}
    ]
    provider = _load(root, folder / PROVIDER_FILE, ProviderFile, problems)
    if provider is None:
        return None
    if provider.domain != folder.name:
        problems.append(f"{where}/{PROVIDER_FILE}: domain: {provider.domain} is not {folder.name}")
    problems += [f"{where}/{PROVIDER_FILE}: {p}" for p in check_provider(provider, reserved)]
    versions: dict[str, VersionFile] = {}
    for path, version in _load_folder(
        root, folder / VERSIONS_FOLDER, VersionFile, "version file", problems
    ):
        file = path.relative_to(root).as_posix()
        if version.version != path.stem:
            problems.append(f"{file}: version: {version.version} is not {path.stem}")
        problems += [f"{file}: {p}" for p in check_version(version, provider, channels)]
        versions[path.stem] = version
    refused: dict[str, RefusedRelease] = {}
    for path, release in _load_folder(
        root, folder / REFUSED_FOLDER, RefusedRelease, "refused release", problems
    ):
        file = path.relative_to(root).as_posix()
        if release.version != path.stem:
            problems.append(f"{file}: version: {release.version} is not {path.stem}")
        if path.stem in versions:
            problems.append(f"{file}: version: {path.stem} has a version file as well")
        problems += [f"{file}: {p}" for p in check_refused(release, provider)]
        refused[path.stem] = release
    problems += [
        f"{where}/{PROVIDER_FILE}: {key}: {version} has no version file"
        for key, kept in (
            ("blocked_versions", provider.blocked_versions),
            ("cleared_versions", provider.cleared_versions),
        )
        for version in kept
        if version not in versions
    ]
    problems += [
        f"{where}/{VERSIONS_FOLDER}/{version}.json: reading.base: {file.reading.base} has no "
        "version file"
        for version, file in versions.items()
        if file.reading is not None
        and file.reading.base is not None
        and file.reading.base not in versions
    ]
    return Provider(provider, versions, refused)


def _load_folder[T: StrictModel](
    root: Path, folder: Path, model: type[T], kind: str, problems: list[str]
) -> list[tuple[Path, T]]:
    """
    Return the files of a versions or refused folder, adding what is wrong to the problems.

    :param root: The root folder of the registry.
    :param folder: The folder.
    :param model: The model each file holds.
    :param kind: What each file holds, for the problems.
    :param problems: The list to add problems to.
    """
    if folder.is_symlink():
        problems.append(f"{folder.relative_to(root).as_posix()}: is a symbolic link")
    result: list[tuple[Path, T]] = []
    for path in _entries(folder):
        if path.suffix != ".json" or path.is_symlink() or not path.is_file():
            problems.append(f"{path.relative_to(root).as_posix()}: is not a {kind}")
            continue
        if (content := _load(root, path, model, problems)) is not None:
            result.append((path, content))
    return result


def _entries(folder: Path) -> list[Path]:
    """
    Return what a folder holds, sorted; nothing when it does not exist or is a symbolic link.

    Names starting with a dot, such as .DS_Store, are no part of the registry and are skipped.

    :param folder: The folder.
    """
    if folder.is_symlink() or not folder.is_dir():
        return []
    return sorted(path for path in folder.iterdir() if not path.name.startswith("."))


def _load[T: StrictModel](root: Path, path: Path, model: type[T], problems: list[str]) -> T | None:
    """
    Return the model in a file, or None after adding why it cannot be read to the problems.

    :param root: The root folder of the registry.
    :param path: The file.
    :param model: The model the file holds.
    :param problems: The list to add a problem to.
    """
    where = path.relative_to(root).as_posix()
    if path.is_symlink():
        problems.append(f"{where}: is a symbolic link")
        return None
    if not path.is_file():
        problems.append(f"{where}: is missing")
        return None
    try:
        return load_model(path, model)
    except (ValueError, OSError) as err:
        problems.append(f"{where}: {err}")
        return None
