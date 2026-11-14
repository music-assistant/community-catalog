"""Fixtures for the tests of the registry: a registry in a temporary folder."""

from __future__ import annotations

import io
import json
import tarfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

ROOT = Path(__file__).parent.parent
DOMAIN = "sample_music"
ADDED = "2026-10-01T12:00:00Z"
RELEASED = "2026-11-01T12:00:00Z"
CHECKED = "2026-11-02T03:00:00Z"
COMMIT = "0123456789abcdef0123456789abcdef01234567"
CONTENT_HASH = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
ICON = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">'
    '<path fill="#fff" d="M0 0h24v24H0z"/></svg>'
)
RESERVED = {
    "builtin_providers": ["builtin_one", "moving_one"],
    "core_controllers": ["players"],
    "reserved_names": ["library"],
    "moving": ["moving_one"],
}
# the top-level folder of an archive GitHub serves
ARCHIVE_ROOT = "someone-sample-0123456"


def manifest(domain: str = DOMAIN, requirements: Sequence[str] = ()) -> dict[str, Any]:
    """
    Return the manifest of a music provider.

    :param domain: The domain of the provider.
    :param requirements: The requirements of the provider.
    """
    return {
        "type": "music",
        "domain": domain,
        "name": "Sample music",
        "description": "A provider for the tests",
        "codeowners": ["@someone"],
        "requirements": list(requirements),
    }


def provider_file(domain: str = DOMAIN, **changes: Any) -> dict[str, Any]:
    """
    Return the content of a valid provider.json.

    :param domain: The domain of the provider.
    :param changes: Keys to set; a value of None removes the key.
    """
    data: dict[str, Any] = {
        "domain": domain,
        "repository": "someone/ma-sample",
        "folder": f"providers/{domain}",
        "maintainers": ["someone"],
        "status": "listed",
        "added": ADDED,
        "admission": {"decided_by": "a-maintainer", "decided": ADDED, "pull_request": 1},
    }
    data.update(changes)
    return {key: value for key, value in data.items() if value is not None}


def version_file(
    version: str = "1.0.0",
    domain: str = DOMAIN,
    lines: Mapping[str, bool] | None = None,
    **changes: Any,
) -> dict[str, Any]:
    """
    Return the content of a valid version file.

    :param version: The version.
    :param domain: The domain of the provider.
    :param lines: Whether the version passed, by server line; nightly passed when omitted.
    :param changes: Keys to set; a value of None removes the key.
    """
    lines = {"nightly": True} if lines is None else lines
    data: dict[str, Any] = {
        "version": version,
        "tag": f"v{version}",
        "commit": COMMIT,
        "folder": f"providers/{domain}",
        "content_hash": CONTENT_HASH,
        "manifest": manifest(domain, ["some-package==1.0"]),
        "requirements": ["some-package==1.0"],
        "released": RELEASED,
        "icon_svg": ICON,
        "checks": {
            line: {"passed": True, "checked": CHECKED}
            if passed
            else {"passed": False, "checked": CHECKED, "reason": "it does not load"}
            for line, passed in lines.items()
        },
    }
    data.update(changes)
    return {key: value for key, value in data.items() if value is not None}


@dataclass
class Files:
    """A registry in a folder, with helpers to write its files."""

    root: Path

    def write(self, path: str, data: Any) -> Path:
        """
        Write a JSON file of the registry.

        :param path: The file, relative to the root.
        :param data: The content; a str is written as it is.
        """
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")
        return target

    def provider(self, domain: str = DOMAIN, **changes: Any) -> Path:
        """
        Write a provider.json.

        :param domain: The domain, also the name of the folder.
        :param changes: Keys to set in the file.
        """
        return self.write(f"providers/{domain}/provider.json", provider_file(domain, **changes))

    def version(
        self,
        version: str = "1.0.0",
        domain: str = DOMAIN,
        lines: Mapping[str, bool] | None = None,
        **changes: Any,
    ) -> Path:
        """
        Write a version file.

        :param version: The version, also the name of the file.
        :param domain: The domain of the provider.
        :param lines: Whether the version passed, by server line; nightly passed when omitted.
        :param changes: Keys to set in the file.
        """
        data = version_file(version, domain, lines, **changes)
        return self.write(f"providers/{domain}/versions/{version}.json", data)


@pytest.fixture
def files(tmp_path: Path) -> Files:
    """Return a registry without providers, for the lines nightly and beta."""
    registry = Files(tmp_path)
    registry.write("server_lines.json", {"lines": {"nightly": {}, "beta": {"frozen": False}}})
    registry.write("reserved_domains.json", RESERVED)
    return registry


def build_archive(
    files: Mapping[str, bytes],
    *,
    extra: Sequence[tarfile.TarInfo] = (),
) -> bytes:
    """
    Return a repository as GitHub archives it: one top-level folder, each folder before its files.

    :param files: The content of each file, by path in the repository.
    :param extra: Members added after the files, named by their full path in the archive.
    """
    folders = {parent for path in files for parent in PurePosixPath(path).parents}
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for folder in sorted(folders, key=str):
            info = tarfile.TarInfo(str(PurePosixPath(ARCHIVE_ROOT, folder)))
            info.type, info.mode = tarfile.DIRTYPE, 0o755
            tar.addfile(info)
        for path, content in files.items():
            info = tarfile.TarInfo(f"{ARCHIVE_ROOT}/{path}")
            info.size, info.mode = len(content), 0o644
            tar.addfile(info, io.BytesIO(content))
        for info in extra:
            tar.addfile(info)
    return buffer.getvalue()


def member(name: str, kind: bytes, linkname: str = "") -> tarfile.TarInfo:
    """
    Return an archive member that is not a regular file.

    :param name: The full path in the archive.
    :param kind: The tarfile type of the member.
    :param linkname: The target of a link.
    """
    info = tarfile.TarInfo(name)
    info.type, info.linkname = kind, linkname
    return info


def write_tree(folder: Path, content: Mapping[str, bytes]) -> None:
    """
    Write files below a folder.

    :param folder: The folder.
    :param content: The content of each file, by relative path.
    """
    for path, data in content.items():
        target = folder / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
