"""Fixtures for the tests of the registry: a registry in a temporary folder."""

from __future__ import annotations

import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, BinaryIO
from urllib.parse import parse_qs, urlsplit

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
NIGHTLY_IMAGE = "ghcr.io/music-assistant/server:nightly"
BETA_IMAGE = "ghcr.io/music-assistant/server:beta"
# the top-level folder of an archive GitHub serves
ARCHIVE_ROOT = "someone-sample-0123456"
# the ids GitHub gives the sample repository and its owner
REPOSITORY_ID = 101
OWNER_ID = 201


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
        "repository_id": REPOSITORY_ID,
        "owner_id": OWNER_ID,
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
    registry.write(
        "server_lines.json",
        {
            "lines": {
                "nightly": {"image": NIGHTLY_IMAGE},
                "beta": {"image": BETA_IMAGE, "frozen": False},
            }
        },
    )
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


def provider_archive(domain: str = DOMAIN, code: bytes = b"VALUE = 1\n", **changes: Any) -> bytes:
    """
    Return a GitHub archive of a repository with one valid provider in providers/<domain>.

    :param domain: The domain of the provider.
    :param code: The content of its __init__.py.
    :param changes: Keys to set in its manifest.
    """
    content = json.dumps({**manifest(domain), **changes}).encode()
    return build_archive(
        {f"providers/{domain}/manifest.json": content, f"providers/{domain}/__init__.py": code}
    )


class FakeGitHub:
    """A transport that answers as GitHub does, for the repositories, archives and issues it has."""

    # the account of the token
    LOGIN = "catalog-reporter"

    def __init__(self) -> None:
        """Initialize a GitHub without repositories."""
        # the GraphQL node of each repository, by owner/name
        self.repositories: dict[str, dict[str, Any]] = {}
        # a GraphQL error about a repository instead of its node, by owner/name
        self.errors: dict[str, dict[str, Any]] = {}
        # the archive of each commit, by (owner/name, commit)
        self.archives: dict[tuple[str, str], bytes] = {}
        # what reading the answer to an address raises, by address
        self.broken: dict[str, Exception] = {}
        # the issues that exist, as (owner/name, author, key)
        self.issues: set[tuple[str, str, str]] = set()
        self.created: list[tuple[str, dict[str, Any]]] = []
        self.requests: list[urllib.request.Request] = []

    def repository(
        self,
        name: str = "someone/ma-sample",
        releases: Sequence[dict[str, Any]] = (),
        **changes: Any,
    ) -> None:
        """
        Add a repository.

        :param name: The repository, as owner/name.
        :param releases: Its releases, newest first, as made by release().
        :param changes: Fields of the GraphQL node to set.
        """
        self.repositories[name] = {
            "databaseId": REPOSITORY_ID,
            "isPrivate": False,
            "isArchived": False,
            "hasIssuesEnabled": True,
            "owner": {"login": name.split("/", maxsplit=1)[0], "databaseId": OWNER_ID},
            "licenseInfo": {"key": "mit"},
            "releases": {"nodes": list(releases)},
            **changes,
        }

    def __call__(self, request: urllib.request.Request) -> tuple[int, BinaryIO]:
        """Answer a request."""
        self.requests.append(request)
        status, body = self._answer(request)
        if (error := self.broken.get(request.full_url)) is not None:
            return status, _Broken(error)
        return status, io.BytesIO(body)

    def _answer(self, request: urllib.request.Request) -> tuple[int, bytes]:
        """Return the status and the body of the answer to a request."""
        url = request.full_url
        if url == "https://api.github.com/graphql":
            return 200, json.dumps(self._graphql(request)).encode()
        if url.startswith("https://codeload.github.com/"):
            owner, name, _, commit = url.removeprefix("https://codeload.github.com/").split("/")
            archive = self.archives.get((f"{owner}/{name}", commit))
            return (200, archive) if archive is not None else (404, b"Not Found")
        if url == "https://api.github.com/user":
            return 200, json.dumps({"login": self.LOGIN}).encode()
        if url.startswith("https://api.github.com/search/issues?"):
            query = parse_qs(urlsplit(url).query)["q"][0]
            found = re.fullmatch(r'"(\w+)" repo:(\S+) is:issue in:body author:(\S+)', query)
            assert found is not None, query
            key, repository, author = found.groups()
            count = int((repository, author, key) in self.issues)
            return 200, json.dumps({"total_count": count}).encode()
        if url.startswith("https://api.github.com/repos/") and url.endswith("/issues"):
            repository = url.removeprefix("https://api.github.com/repos/").removesuffix("/issues")
            self.created.append((repository, request_json(request)))
            return 201, json.dumps(
                {"html_url": f"https://github.com/{repository}/issues/1"}
            ).encode()
        return 404, b"{}"

    def _graphql(self, request: urllib.request.Request) -> dict[str, Any]:
        """Answer a GraphQL query for repositories, by the variables it passes."""
        assert request.get_header("Authorization") == "Bearer a-token"
        variables = request_json(request)["variables"]
        data: dict[str, Any] = {}
        errors = []
        for key, owner in variables.items():
            if not key.startswith("o"):
                continue
            number = key.removeprefix("o")
            name = f"{owner}/{variables[f'n{number}']}"
            data[f"r{number}"] = None if name in self.errors else self.repositories.get(name)
            if name in self.errors:
                errors.append({**self.errors[name], "path": [f"r{number}"]})
            elif name not in self.repositories:
                errors.append({"type": "NOT_FOUND", "path": [f"r{number}"]})
        return {"data": data, "errors": errors} if errors else {"data": data}


class _Broken(io.BytesIO):
    """A body whose reading fails."""

    def __init__(self, error: Exception) -> None:
        """Initialize a body that raises an error when it is read."""
        super().__init__()
        self._error = error

    def read(self, size: int | None = -1) -> bytes:  # noqa: ARG002 - the signature of read
        """Raise the error."""
        raise self._error


def release(
    tag: str,
    commit: str = COMMIT,
    published: str = RELEASED,
    *,
    draft: bool = False,
    prerelease: bool = False,
) -> dict[str, Any]:
    """
    Return a release as the GraphQL API answers it.

    :param tag: The tag of the release.
    :param commit: The tagged commit.
    :param published: When it was published.
    :param draft: Whether it is a draft.
    :param prerelease: Whether it is a pre-release.
    """
    return {
        "tagName": tag,
        "isDraft": draft,
        "isPrerelease": prerelease,
        "publishedAt": None if draft else published,
        "tagCommit": {"oid": commit},
    }


def request_json(request: urllib.request.Request) -> dict[str, Any]:
    """
    Return the JSON object a request sends.

    :param request: The request.
    """
    assert isinstance(request.data, bytes)
    data = json.loads(request.data)
    assert isinstance(data, dict)
    return data


# stands in for docker in the tests of scripts/check-line.sh: it records every call, and runs the
# command of a container on this machine, with its paths mapped to the folders mounted there
_FAKE_DOCKER = """#!{python}
import json, os, subprocess, sys

VALUED = {{
    "--name", "--pull", "--platform", "--network", "--user", "--cap-drop", "--security-opt",
    "--pids-limit", "--memory", "--memory-swap", "--env", "--volume", "--workdir", "--entrypoint",
}}
args = sys.argv[1:]
with open(os.environ["FAKE_DOCKER_LOG"], "a") as log:
    log.write(json.dumps(args) + "\\n")
if args[0] != "run":
    sys.exit(0)
rest, options, mounts = args[1:], {{}}, []
while rest[0].startswith("--"):
    flag = rest.pop(0)
    if flag in VALUED:
        value = rest.pop(0)
        options[flag] = value
        if flag == "--volume":
            host, inside = value.split(":")[:2]
            mounts.append((inside, host))


def mapped(arg):
    for inside, host in mounts:
        if arg == inside or arg.startswith(inside + "/"):
            return host + arg[len(inside):]
    return arg


command = [mapped(arg) for arg in rest[1:]]
assert command[:2] == ["-m", "registry"], command
run = json.loads(os.environ["FAKE_DOCKER_COMMAND"]) + command[2:]
code = subprocess.run(run, cwd=mapped(options["--workdir"]), stdin=subprocess.DEVNULL).returncode
sys.exit(code)
"""
CHECK_LINE = ROOT / "scripts" / "check-line.sh"


def can_run_check_line() -> bool:
    """Return whether this machine has what scripts/check-line.sh needs besides docker."""
    return all(shutil.which(tool) for tool in ("bash", "jq", "timeout", "find"))


def run_check_line(
    catalog: Path, line: str, command: Sequence[str], log: Path
) -> subprocess.CompletedProcess[str]:
    """
    Run scripts/check-line.sh with a docker that runs each container's command on this machine.

    :param catalog: The folder the script mounts as /catalog, with work/plan.json.
    :param line: The server line.
    :param command: What runs in place of `python -m registry` in each container.
    :param log: The file the fake docker writes each of its calls to, as a JSON list.
    """
    folder = log.parent / "bin"
    folder.mkdir(exist_ok=True)
    docker = folder / "docker"
    docker.write_text(_FAKE_DOCKER.format(python=sys.executable))
    docker.chmod(0o755)
    environment = {key: value for key, value in os.environ.items() if not key.startswith("UV_")}
    environment |= {
        "PATH": f"{folder}{os.pathsep}{os.environ['PATH']}",
        "FAKE_DOCKER_LOG": str(log),
        "FAKE_DOCKER_COMMAND": json.dumps(list(command)),
        "UV_OFFLINE": "1",
    }
    bash = shutil.which("bash")
    assert bash is not None
    return subprocess.run(
        [bash, str(CHECK_LINE), str(catalog), line, "ghcr.io/music-assistant/server:nightly",
         str(catalog / "results")],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=600,
    )  # fmt: skip


def docker_runs(log: Path) -> list[list[str]]:
    """Return the `docker run` calls the fake docker recorded."""
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    return [call for call in calls if call[0] == "run"]
