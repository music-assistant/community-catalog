"""Tests for the command line of the registry."""

from __future__ import annotations

import json
import shutil
from typing import TYPE_CHECKING

import pytest

from registry.cli import main
from registry.registry import load_registry
from tests.conftest import COMMIT, DOMAIN, ICON, build_archive, manifest, write_tree

if TYPE_CHECKING:
    from pathlib import Path

    from tests.conftest import Files

PROVIDER = {
    "manifest.json": json.dumps(manifest(requirements=["some-package==1.0"])).encode(),
    "__init__.py": b"VALUE = 1\n",
    "icon.svg": ICON.encode(),
}


def _run(files: Files, *args: str) -> int:
    """Run a command on the registry of the fixture."""
    return main(["--root", str(files.root), *args])


def _add(files: Files, source: Path, version: str = "1.0.0") -> int:
    """Add a version from a source with the fixture's commit."""
    return _run(files, "add-version", DOMAIN, version, str(source), "--tag", f"v{version}",
                "--commit", COMMIT, "--released", "2026-11-01T12:00:00Z")  # fmt: skip


def test_add_version_from_an_archive(files: Files, tmp_path: Path) -> None:
    """A version file is written from the provider folder of a GitHub archive."""
    files.provider()
    archive = tmp_path / "repo.tar.gz"
    archive.write_bytes(
        build_archive({f"providers/{DOMAIN}/{path}": data for path, data in PROVIDER.items()})
    )

    assert _add(files, archive) == 0

    version = load_registry(files.root).providers[DOMAIN].versions["1.0.0"]
    assert version.folder == f"providers/{DOMAIN}"
    assert version.manifest == manifest(requirements=["some-package==1.0"])
    assert version.requirements == ["some-package==1.0"]
    assert version.icon_svg == ICON
    assert version.checks == {}


def test_add_version_from_a_folder_gives_the_hash_of_the_archive(
    files: Files, tmp_path: Path
) -> None:
    """A local folder with the files of the archive gives the same content hash."""
    files.provider()
    archive = tmp_path / "repo.tar.gz"
    archive.write_bytes(
        build_archive({f"providers/{DOMAIN}/{path}": data for path, data in PROVIDER.items()})
    )
    folder = tmp_path / DOMAIN
    write_tree(folder, {**PROVIDER, "__pycache__/x.cpython-314.pyc": b"x"})

    assert _add(files, folder) == 0
    assert _run(files, "content-hash", str(archive), "--folder", f"providers/{DOMAIN}") == 0

    version = load_registry(files.root).providers[DOMAIN].versions["1.0.0"]
    assert (
        version.content_hash
        in files.root.joinpath(f"providers/{DOMAIN}/versions/1.0.0.json").read_text()
    )


def test_add_version_refuses_what_is_not_valid(
    files: Files, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A version that would not be valid, or exists already, is not written."""
    files.provider()
    folder = tmp_path / DOMAIN
    write_tree(folder, PROVIDER)

    assert _add(files, folder, "1.0") == 1
    assert "version: '1.0' is not a version" in capsys.readouterr().err
    assert _add(files, folder) == 0
    assert _add(files, folder) == 1
    assert f"{DOMAIN} 1.0.0 exists already" in capsys.readouterr().err
    assert _run(files, "add-version", "other_music", "1.0.0", str(folder), "--tag", "v1.0.0",
                "--commit", COMMIT) == 1  # fmt: skip
    assert "there is no provider other_music" in capsys.readouterr().err
    assert set(load_registry(files.root).providers[DOMAIN].versions) == {"1.0.0"}


@pytest.mark.parametrize("value", ["NaN", '"\\ud800"'])
def test_add_version_refuses_a_manifest_the_server_cannot_read(
    files: Files, tmp_path: Path, capsys: pytest.CaptureFixture[str], value: str
) -> None:
    """A manifest that is not plain JSON is refused with a message, and nothing is written."""
    files.provider()
    folder = tmp_path / DOMAIN
    content = json.dumps({**manifest(), "extra": "X"}).replace('"X"', value)
    write_tree(folder, {**PROVIDER, "manifest.json": content.encode()})

    assert _add(files, folder) == 1

    assert "its manifest.json: not valid JSON: " in capsys.readouterr().err
    assert load_registry(files.root).providers[DOMAIN].versions == {}


def test_record_check_refuses_what_is_no_plain_json(
    files: Files, capsys: pytest.CaptureFixture[str]
) -> None:
    """A reason that cannot be written as plain JSON, such as a lone surrogate, is refused."""
    files.provider()
    path = files.version(lines={})
    before = path.read_bytes()

    assert _run(files, "record-check", DOMAIN, "1.0.0", "nightly", "--failed", "\udcff") == 1

    assert "1.0.0.json: not plain JSON: " in capsys.readouterr().err
    assert path.read_bytes() == before


def test_add_version_leaves_out_an_icon_that_is_not_allowed(
    files: Files, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """An icon that does not pass its check is left out with a warning; the version is added."""
    files.provider()
    folder = tmp_path / DOMAIN
    write_tree(
        folder, {**PROVIDER, "icon.svg": b'<svg xmlns="http://www.w3.org/2000/svg" onload="x"/>'}
    )

    assert _add(files, folder) == 0

    assert "warning: the icon is left out: the event handler onload" in capsys.readouterr().err
    assert load_registry(files.root).providers[DOMAIN].versions["1.0.0"].icon_svg is None


def test_record_check(files: Files, capsys: pytest.CaptureFixture[str]) -> None:
    """The result of a line is recorded and replaced; a line that is not listed is refused."""
    files.provider()
    files.version(lines={})

    assert _run(files, "record-check", DOMAIN, "1.0.0", "nightly", "--failed", "no import") == 0
    assert _run(files, "record-check", DOMAIN, "1.0.0", "beta", "--passed",
                "--checked", "2026-11-02T03:00:00Z") == 0  # fmt: skip
    assert _run(files, "record-check", DOMAIN, "1.0.0", "2.10", "--passed") == 1
    assert "checks: '2.10' is not a line in server_lines.json" in capsys.readouterr().err

    checks = load_registry(files.root).providers[DOMAIN].versions["1.0.0"].checks
    assert (checks["nightly"].passed, checks["nightly"].reason) == (False, "no import")
    assert (checks["beta"].passed, checks["beta"].checked) == (True, "2026-11-02T03:00:00Z")


def test_build_and_validate(files: Files, capsys: pytest.CaptureFixture[str]) -> None:
    """Validate fails until build has written the index files, and on an invalid file."""
    files.provider()
    files.version()

    assert _run(files, "validate") == 1
    assert "site/v1/nightly.json: is missing" in capsys.readouterr().err
    assert _run(files, "build") == 0
    assert _run(files, "validate") == 0
    files.version(commit="x")
    assert _run(files, "validate") == 1
    assert _run(files, "build") == 1
    assert "commit: 'x' is not a full commit SHA" in capsys.readouterr().err


def test_refresh_reserved(files: Files, tmp_path: Path) -> None:
    """Every provider package and core controller of a server is added, nothing is removed."""
    server = tmp_path / "server"
    write_tree(
        server / "music_assistant",
        {
            "providers/new_one/manifest.json": b'{"domain": "new_one"}',
            "providers/no_manifest/__init__.py": b"",
            "providers/_demo_one/manifest.json": b'{"domain": "_demo_one"}',
            "providers/__pycache__/x.pyc": b"",
            "providers/__init__.py": b"",
            "controllers/streams/__init__.py": b"",
            "controllers/__pycache__/x.pyc": b"",
            "controllers/__init__.py": b"",
        },
    )

    assert _run(files, "refresh-reserved", str(server)) == 0

    reserved = load_registry(files.root).reserved
    assert reserved.builtin_providers == ["builtin_one", "moving_one", "new_one", "no_manifest"]
    assert reserved.core_controllers == ["players", "streams"]
    assert reserved.moving == ["moving_one"]
    assert _run(files, "refresh-reserved", str(tmp_path)) == 1


@pytest.mark.parametrize(
    ("link", "command"),
    [
        ("providers", ["record-check", DOMAIN, "1.0.0", "nightly", "--passed"]),
        (f"providers/{DOMAIN}/versions", ["record-check", DOMAIN, "1.0.0", "nightly", "--passed"]),
        ("site", ["build"]),
        ("site/v1", ["build"]),
        ("site/v1/nightly.json", ["build"]),
        ("reserved_domains.json", ["refresh-reserved", "SERVER"]),
    ],
)
def test_a_symbolic_link_in_the_registry_is_refused(
    files: Files,
    tmp_path_factory: pytest.TempPathFactory,
    capsys: pytest.CaptureFixture[str],
    link: str,
    command: list[str],
) -> None:
    """Validate reports a link, and no command writes through one out of the registry."""
    server = tmp_path_factory.mktemp("server")
    write_tree(
        server / "music_assistant",
        {"providers/new_one/__init__.py": b"", "controllers/new_core/__init__.py": b""},
    )
    files.provider()
    files.version("1.0.0", lines={})
    assert _run(files, "build") == 0
    # a change every command would write
    files.version("1.1.0")
    path = files.root / link
    outside = tmp_path_factory.mktemp("outside") / path.name
    shutil.move(path, outside)
    path.symlink_to(outside, target_is_directory=outside.is_dir())
    before = _tree(outside)
    capsys.readouterr()

    assert _run(files, "validate") == 1
    assert f"{link}: is a symbolic link" in capsys.readouterr().err
    assert _run(files, *[str(server) if arg == "SERVER" else arg for arg in command]) == 1
    assert f"{link}: is a symbolic link" in capsys.readouterr().err
    assert _tree(outside) == before


def _tree(path: Path) -> dict[str, bytes]:
    """Return the content of a file, or of every file below a folder, by relative path."""
    if path.is_file():
        return {"": path.read_bytes()}
    return {
        item.relative_to(path).as_posix(): item.read_bytes()
        for item in path.rglob("*")
        if item.is_file()
    }


def test_set_status(files: Files, capsys: pytest.CaptureFixture[str]) -> None:
    """A provider is withdrawn, blocked and listed again in one command, with its index."""
    files.provider()
    files.version()
    url = "https://github.com/music-assistant/community-catalog/issues/2"

    assert _run(files, "set-status", DOMAIN, "blocked", "--reason", "Sends data elsewhere",
                "--reason-url", url) == 0  # fmt: skip
    provider = json.loads((files.root / "site/v1/nightly.json").read_text())["providers"][DOMAIN]
    assert (provider["status"], provider["reason"], provider["reason_url"]) == (
        "blocked",
        "Sends data elsewhere",
        url,
    )
    assert _run(files, "set-status", DOMAIN, "withdrawn") == 1
    assert "reason: a withdrawn provider needs a reason" in capsys.readouterr().err
    assert _run(files, "set-status", DOMAIN, "listed") == 0
    assert _run(files, "validate") == 0
    file = load_registry(files.root).providers[DOMAIN].file
    assert (file.status, file.reason, file.reason_url) == ("listed", None, None)
    assert _run(files, "set-status", "other_music", "listed") == 1
    assert "there is no provider other_music" in capsys.readouterr().err


def test_block_version(files: Files, capsys: pytest.CaptureFixture[str]) -> None:
    """A version is blocked and unblocked in one command, with its index."""
    files.provider()
    files.version("1.0.0")
    files.version("1.1.0")

    assert _run(files, "block-version", DOMAIN, "1.1.0", "--reason", "Sends data elsewhere") == 0
    provider = json.loads((files.root / "site/v1/nightly.json").read_text())["providers"][DOMAIN]
    assert (provider["latest"], provider["blocked_versions"]) == (
        "1.0.0",
        {"1.1.0": "Sends data elsewhere"},
    )
    assert _run(files, "block-version", DOMAIN, "2.0.0", "--reason", "x") == 1
    assert "blocked_versions: 2.0.0 has no version file" in capsys.readouterr().err
    assert _run(files, "unblock-version", DOMAIN, "1.0.0") == 1
    assert f"{DOMAIN} 1.0.0 is not blocked" in capsys.readouterr().err
    assert _run(files, "unblock-version", DOMAIN, "1.1.0") == 0
    assert _run(files, "validate") == 0
    assert load_registry(files.root).providers[DOMAIN].file.blocked_versions == {}


def test_check_admission(files: Files, capsys: pytest.CaptureFixture[str]) -> None:
    """The result of the admission is printed, and the exit code says whether it passed."""
    files.provider()
    files.version("1.0.0", lines={})
    report = {
        "domain": DOMAIN,
        "version": "1.0.0",
        "needed": True,
        "findings": [{"passed": True, "text": "The domain sample_music is free"}],
    }
    files.write("work/admission.json", report)
    failed = {"domain": DOMAIN, "version": "1.0.0", "passed": False, "reason": "load: x"}
    files.write(
        "results/nightly.json",
        {"line": "nightly", "checked": "2026-11-15T03:00:00Z", "results": [failed]},
    )
    results = str(files.root / "results")

    assert _run(files, "check-admission", DOMAIN, results) == 1
    out = capsys.readouterr().out
    assert "- ❌ nightly: load: x" in out
    assert "- ❌ beta: the checks did not run" in out
    files.write(
        "results/beta.json",
        {
            "line": "beta",
            "checked": "2026-11-15T03:00:00Z",
            "results": [{"domain": DOMAIN, "version": "1.0.0", "passed": True}],
        },
    )
    assert _run(files, "check-admission", DOMAIN, results) == 0
    assert "Every check passed." in capsys.readouterr().out
    assert _run(files, "check-admission", "other_music", results) == 1
    assert "admission.json is about sample_music, not other_music" in capsys.readouterr().err
