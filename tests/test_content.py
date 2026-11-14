"""Tests for the content of a provider folder: the content hash and what a server installs."""

from __future__ import annotations

import json
import os
import tarfile
from typing import TYPE_CHECKING

import pytest

from registry.content import content_hash, read_archive, read_folder, unpack
from tests.conftest import ARCHIVE_ROOT, ICON, build_archive, manifest, member, write_tree

if TYPE_CHECKING:
    from pathlib import Path

# the test vector of test_installer.py in the server, computed with coreutils in the folder:
# find . -type f ! -path '*/__pycache__/*' ! -name '*.pyc' | sed 's|^\./||' | LC_ALL=C sort
#   | xargs sha256sum | sha256sum
HASH_FOLDER = {
    "manifest.json": b'{"domain": "foo"}\n',
    "__init__.py": b"",
    "a.py": b"a\n",
    "a/b.py": b"b\n",
    "B.py": b"B\n",
    "é.txt": "é\n".encode(),
    "sub/deep/data.bin": bytes(range(256)),
}
HASH_FOLDER_HASH = "826ca411123465fad19e0b73dd160bd95dd150699eda49f13d4ac514bcf3ddf8"
FOLDER = "providers/foo"
COMPILED = {
    "stray.pyc": b"x",
    "__pycache__/__init__.cpython-314.pyc": b"x",
    "sub/__pycache__/notes.txt": b"x",
}


def _repository(provider: dict[str, bytes]) -> dict[str, bytes]:
    """Return a repository with the provider in FOLDER and a file next to it."""
    return {
        "README.md": b"readme\n",
        **{f"{FOLDER}/{path}": data for path, data in provider.items()},
    }


def test_content_hash_is_the_one_of_the_server(tmp_path: Path) -> None:
    """The content hash of the server's test vector is the server's expected value."""
    write_tree(tmp_path, HASH_FOLDER)

    assert content_hash(tmp_path) == HASH_FOLDER_HASH


def test_content_hash_ignores_compiled_files_modes_times_and_empty_folders(tmp_path: Path) -> None:
    """Compiled Python files, modes, times and empty folders do not change the content hash."""
    write_tree(tmp_path, {**HASH_FOLDER, **COMPILED})
    (tmp_path / "empty" / "deeper").mkdir(parents=True)
    (tmp_path / "a.py").chmod(0o755)
    os.utime(tmp_path / "B.py", (0, 0))

    assert content_hash(tmp_path) == HASH_FOLDER_HASH


@pytest.mark.parametrize("change", ["rename", "content"])
def test_content_hash_changes_with_a_name_or_content(tmp_path: Path, change: str) -> None:
    """Renaming a file or changing its content changes the content hash."""
    write_tree(tmp_path, HASH_FOLDER)
    if change == "rename":
        (tmp_path / "a.py").rename(tmp_path / "c.py")
    else:
        (tmp_path / "a.py").write_bytes(b"c\n")

    assert content_hash(tmp_path) != HASH_FOLDER_HASH


def test_archive_gives_the_hash_of_the_folder(tmp_path: Path) -> None:
    """The hash of a folder in an archive is the one of the same folder on disk."""
    archive = tmp_path / "repo.tar.gz"
    archive.write_bytes(build_archive(_repository({**HASH_FOLDER, **COMPILED})))

    assert read_archive(archive, FOLDER).content_hash == HASH_FOLDER_HASH


def test_unpack_takes_only_the_provider_folder_without_compiled_files(tmp_path: Path) -> None:
    """Only the provider folder is taken, compiled files left out, as the server installs it."""
    archive = tmp_path / "repo.tar.gz"
    repository = _repository({**HASH_FOLDER, **COMPILED})
    repository["providers/foo_other/x.py"] = b"x"
    archive.write_bytes(build_archive(repository))
    target = tmp_path / "out"

    unpack(archive, FOLDER, target)

    files = {p.relative_to(target).as_posix() for p in target.rglob("*") if p.is_file()}
    assert files == set(HASH_FOLDER)


def test_archive_reads_manifest_and_icon(tmp_path: Path) -> None:
    """The manifest and the SVG icon of the folder are read as they are."""
    archive = tmp_path / "repo.tar.gz"
    provider = {"manifest.json": json.dumps(manifest("foo")).encode(), "icon.svg": ICON.encode()}
    archive.write_bytes(build_archive(_repository(provider)))

    content = read_archive(archive, FOLDER)

    assert content.manifest == manifest("foo")
    assert content.icon_svg == ICON


@pytest.mark.parametrize(
    ("kind", "linkname"),
    [
        (tarfile.SYMTYPE, "../../README.md"),
        (tarfile.LNKTYPE, f"{ARCHIVE_ROOT}/README.md"),
        (tarfile.FIFOTYPE, ""),
    ],
    ids=["symlink", "hardlink", "fifo"],
)
def test_archive_refuses_anything_but_files_and_folders(
    tmp_path: Path, kind: bytes, linkname: str
) -> None:
    """A link or a special file inside the provider folder refuses the archive."""
    archive = tmp_path / "repo.tar.gz"
    special = member(f"{ARCHIVE_ROOT}/{FOLDER}/sub/special", kind, linkname)
    archive.write_bytes(build_archive(_repository(HASH_FOLDER), extra=[special]))

    with pytest.raises(ValueError, match="sub/special is not a regular file or folder"):
        read_archive(archive, FOLDER)


def test_archive_ignores_links_outside_the_provider_folder(tmp_path: Path) -> None:
    """Links and special files elsewhere in the repository are not looked at."""
    archive = tmp_path / "repo.tar.gz"
    extra = [
        member(f"{ARCHIVE_ROOT}/docs/link", tarfile.SYMTYPE, "../README.md"),
        member(f"{ARCHIVE_ROOT}/docs/fifo", tarfile.FIFOTYPE),
    ]
    archive.write_bytes(build_archive(_repository(HASH_FOLDER), extra=extra))

    assert read_archive(archive, FOLDER).content_hash == HASH_FOLDER_HASH


@pytest.mark.parametrize(
    "name",
    [f"{ARCHIVE_ROOT}/{FOLDER}/../../../evil.py", "/providers/foo/evil.py"],
    ids=["parent", "absolute"],
)
def test_archive_refuses_a_path_outside_the_repository(tmp_path: Path, name: str) -> None:
    """A member that leaves the repository refuses the archive."""
    archive = tmp_path / "repo.tar.gz"
    archive.write_bytes(build_archive(_repository(HASH_FOLDER), extra=[tarfile.TarInfo(name)]))

    with pytest.raises(ValueError, match="lies outside the repository"):
        read_archive(archive, FOLDER)


def test_archive_refuses_a_file_that_appears_twice(tmp_path: Path) -> None:
    """A file that appears twice in the provider folder refuses the archive."""
    archive = tmp_path / "repo.tar.gz"
    twice = tarfile.TarInfo(f"{ARCHIVE_ROOT}/{FOLDER}/a.py")
    archive.write_bytes(build_archive(_repository(HASH_FOLDER), extra=[twice]))

    with pytest.raises(ValueError, match=r"holds .*a\.py twice"):
        read_archive(archive, FOLDER)


def test_archive_refuses_a_missing_folder(tmp_path: Path) -> None:
    """An archive without the provider folder is refused."""
    archive = tmp_path / "repo.tar.gz"
    archive.write_bytes(build_archive(_repository(HASH_FOLDER)))

    with pytest.raises(ValueError, match="the repository has no folder providers/bar"):
        read_archive(archive, "providers/bar")


def test_archive_refuses_what_is_no_archive(tmp_path: Path) -> None:
    """A file that is not a gzipped tar archive is refused."""
    archive = tmp_path / "repo.tar.gz"
    archive.write_bytes(b"not an archive")

    with pytest.raises(ValueError, match="the archive cannot be read"):
        read_archive(archive, FOLDER)


def test_folder_refuses_a_link(tmp_path: Path) -> None:
    """A link in a local provider folder is refused, as it would be in an archive."""
    write_tree(tmp_path, HASH_FOLDER)
    (tmp_path / "link.py").symlink_to(tmp_path / "a.py")

    with pytest.raises(ValueError, match=r"link\.py is not a regular file or folder"):
        read_folder(tmp_path)


def test_folder_refuses_a_missing_or_invalid_manifest(tmp_path: Path) -> None:
    """A folder needs a manifest.json with a JSON object."""
    with pytest.raises(ValueError, match=r"it has no manifest\.json"):
        read_folder(tmp_path)
    (tmp_path / "manifest.json").write_text("[]")
    with pytest.raises(ValueError, match=r"manifest\.json is not a JSON object"):
        read_folder(tmp_path)


def test_archive_refuses_a_provider_folder_beyond_the_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A provider folder that unpacks to more than the limit is refused before it fills a disk."""
    monkeypatch.setattr("registry.content.MAX_FOLDER_SIZE", 1000)
    archive = tmp_path / "repo.tar.gz"
    archive.write_bytes(
        build_archive(
            {
                "provider/manifest.json": json.dumps(manifest("provider")).encode(),
                "provider/data.bin": b"\0" * 2000,
                "other/large.bin": b"\0" * 5000,
            }
        )
    )

    with pytest.raises(ValueError, match="the provider folder holds more than 1000 bytes"):
        read_archive(archive, "provider")
