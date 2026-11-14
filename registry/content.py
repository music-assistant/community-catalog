"""The content of a provider folder: the files a server installs and their content hash."""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, NoReturn

from registry.files import loads

ICON_FILE = "icon.svg"
MANIFEST_FILE = "manifest.json"
# the most the files of a provider folder may hold together, so an archive cannot fill a disk
MAX_FOLDER_SIZE = 100 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class ProviderContent:
    """What a version file records of a provider folder."""

    content_hash: str
    manifest: dict[str, Any]
    icon_svg: str | None


def content_hash(folder: Path) -> str:
    """
    Return the content hash of a provider folder, as the server computes it.

    Only the paths and the content of the files count: not their modes or times, not empty
    folders and not compiled Python files.

    :param folder: The provider folder.
    :raises OSError: The folder or a file in it cannot be read.
    """
    # the same definition as content_hash in installer.py of the server
    lines: list[tuple[bytes, bytes]] = []
    for root, dirs, files in os.walk(folder, onerror=_raise):
        dirs[:] = [name for name in dirs if name != "__pycache__"]
        for name in files:
            path = Path(root, name)
            if name.endswith(".pyc") or not stat.S_ISREG(path.lstat().st_mode):
                continue
            relative = path.relative_to(folder).as_posix().encode()
            with path.open("rb") as file:
                digest = hashlib.file_digest(file, "sha256").hexdigest()
            lines.append((relative, f"{digest}  ".encode() + relative + b"\n"))
    return hashlib.sha256(b"".join(line for _, line in sorted(lines))).hexdigest()


def read_folder(folder: Path) -> ProviderContent:
    """
    Return the content of a local provider folder, refusing what a server would not install.

    The content hash equals the one of the release only when the folder holds exactly the
    files of the GitHub archive of its commit.

    :param folder: The provider folder.
    :raises ValueError: The folder holds a link or a special file, or its manifest or icon
        cannot be read.
    :raises OSError: The folder or a file in it cannot be read.
    """
    for root, dirs, files in os.walk(folder, onerror=_raise):
        dirs[:] = [name for name in dirs if name != "__pycache__"]
        for name in [*dirs, *files]:
            path = Path(root, name)
            mode = path.lstat().st_mode
            if not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)) and not name.endswith(".pyc"):
                msg = f"{path.relative_to(folder).as_posix()} is not a regular file or folder"
                raise ValueError(msg)
    return ProviderContent(content_hash(folder), _read_manifest(folder), _read_icon(folder))


def read_archive(archive: Path, folder: str) -> ProviderContent:
    """
    Return the content of a provider folder in a GitHub archive, as a server would install it.

    :param archive: The archive of a commit, as GitHub serves it at
        https://codeload.github.com/<owner>/<name>/tar.gz/<commit>.
    :param folder: The folder of the provider in the repository.
    :raises ValueError: The archive is not one a server installs from: a member leaves the
        repository, the folder holds anything but regular files and folders or is missing,
        or its manifest or icon cannot be read.
    :raises OSError: The archive cannot be read.
    """
    with tempfile.TemporaryDirectory() as workdir:
        target = Path(workdir, "provider")
        try:
            unpack(archive, folder, target)
        except (tarfile.TarError, EOFError) as err:
            msg = f"the archive cannot be read: {err}"
            raise ValueError(msg) from err
        except FileExistsError as err:
            msg = f"the archive holds {err.filename} twice"
            raise ValueError(msg) from err
        return ProviderContent(content_hash(target), _read_manifest(target), _read_icon(target))


def unpack(archive: Path, folder: str, target: Path) -> None:
    """
    Write what lies below a folder of a GitHub archive to a new folder, as the server installs it.

    :param archive: The archive, with the repository in one top-level folder.
    :param folder: The folder in the repository to take.
    :param target: The folder to write to, which must not exist.
    :raises ValueError: A member leaves the repository, the folder holds anything but regular
        files and folders or more than MAX_FOLDER_SIZE bytes, or the folder is not in the
        archive.
    """
    # the same rules as _unpack in installer.py of the server, and a limit on the size
    prefix = PurePosixPath(folder).parts
    size = 0
    with tarfile.open(archive, "r:gz") as tar:
        for member in tar:
            name = PurePosixPath(member.name)
            parts = name.parts
            if name.is_absolute() or ".." in parts:
                msg = f"{member.name} lies outside the repository"
                raise ValueError(msg)
            # the first part is the top-level folder GitHub names after the repository and commit
            if parts[1 : len(prefix) + 1] != prefix:
                continue
            relative = parts[len(prefix) + 1 :]
            # compiled files are no part of the content hash, so they are not installed either
            if "__pycache__" in relative or name.suffix == ".pyc":
                continue
            path = target.joinpath(*relative)
            if member.isdir():
                path.mkdir(parents=True, exist_ok=True)
            elif member.isfile() and (source := tar.extractfile(member)) is not None:
                size += member.size
                if size > MAX_FOLDER_SIZE:
                    msg = f"the provider folder holds more than {MAX_FOLDER_SIZE} bytes"
                    raise ValueError(msg)
                path.parent.mkdir(parents=True, exist_ok=True)
                with source, path.open("xb") as file:
                    shutil.copyfileobj(source, file)
            else:
                msg = f"{PurePosixPath(*parts[1:])} is not a regular file or folder"
                raise ValueError(msg)
    if not target.is_dir():
        msg = f"the repository has no folder {folder}"
        raise ValueError(msg)


def _read_manifest(folder: Path) -> dict[str, Any]:
    """
    Return the manifest of a provider folder.

    :param folder: The provider folder.
    :raises ValueError: The manifest is missing, is not plain JSON or is not a JSON object.
    """
    try:
        manifest = loads((folder / MANIFEST_FILE).read_bytes())
    except FileNotFoundError as err:
        msg = f"it has no {MANIFEST_FILE}"
        raise ValueError(msg) from err
    except ValueError as err:
        msg = f"its {MANIFEST_FILE}: {err}"
        raise ValueError(msg) from err
    if not isinstance(manifest, dict):
        msg = f"its {MANIFEST_FILE} is not a JSON object"
        raise ValueError(msg)
    return manifest


def _read_icon(folder: Path) -> str | None:
    """
    Return the SVG icon of a provider folder, or None when it has none.

    :param folder: The provider folder.
    :raises ValueError: The icon is not UTF-8 text.
    """
    try:
        return (folder / ICON_FILE).read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except UnicodeDecodeError as err:
        msg = f"its {ICON_FILE} is not UTF-8 text"
        raise ValueError(msg) from err


def _raise(err: OSError) -> NoReturn:
    """Raise an error of os.walk, which would skip what it cannot read."""
    raise err
