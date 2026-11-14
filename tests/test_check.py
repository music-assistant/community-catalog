"""Tests for the check step, run for real with the interpreter of a server checkout."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
import zipfile
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from registry.edit import add_version, record_check
from registry.files import load_model
from registry.models import LineResults
from registry.pipeline import archive_path
from registry.registry import load_registry
from tests.conftest import COMMIT, ROOT, build_archive, manifest, write_tree

if TYPE_CHECKING:
    from tests.conftest import Files

# a checkout of the server with the community catalog and its environment with uv; by default
# the server next to the registry
SERVER = Path(os.environ.get("SERVER_CHECKOUT", ROOT.parent / "server"))
SERVER_PYTHON = SERVER / ".venv" / "bin" / "python"
# runs a command of the registry with uv pointed at the wheels the test built, as the server's
# own tests do; nothing reaches the network
RUN_OFFLINE = """
import sys
from music_assistant.controllers.community_catalog import dependencies
dependencies._UV_INDEX_ARGS[:] = ["--no-index", "--find-links", sys.argv[1]]
from registry.cli import main
sys.exit(main(sys.argv[2:]))
"""


def _server_has_the_catalog() -> bool:
    """Return whether the server checkout has everything the check step imports."""
    if not SERVER_PYTHON.is_file():
        return False
    probe = [str(SERVER_PYTHON), "-c", "import registry.check, registry.load"]
    return subprocess.run(probe, cwd=ROOT, capture_output=True, check=False).returncode == 0


pytestmark = pytest.mark.skipif(
    not _server_has_the_catalog(), reason="needs a server checkout with the community catalog"
)


@pytest.fixture(scope="module")
def wheels(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Return a folder with tiny wheels, one of them needing another version of orjson."""
    folder = tmp_path_factory.mktemp("wheels")
    server_orjson = subprocess.run(
        [SERVER_PYTHON, "-c", "from importlib.metadata import version; print(version('orjson'))"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    _build_wheel(folder, "orjson", server_orjson)
    _build_wheel(folder, "orjson", "1.0")
    _build_wheel(folder, "helper-lib", "1.0")
    _build_wheel(folder, "needs-old-orjson", "1.0", "orjson==1.0")
    return folder


def _run(wheels: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run a command of the registry with the interpreter of the server checkout."""
    environment = {key: value for key, value in os.environ.items() if not key.startswith("UV_")}
    environment["UV_OFFLINE"] = "1"
    return subprocess.run(
        [SERVER_PYTHON, "-c", RUN_OFFLINE, str(wheels), *args],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )


def _release(
    files: Files, domain: str, version: str, code: str, requirements: list[str] | None = None
) -> None:
    """Write the archive of a release into the work folder and add its version file."""
    files.provider(domain)
    archive = archive_path(files.root / "work", domain, version)
    archive.parent.mkdir(parents=True, exist_ok=True)
    content = {
        f"providers/{domain}/manifest.json": json.dumps(
            manifest(domain, requirements or [])
        ).encode(),
        f"providers/{domain}/__init__.py": code.encode(),
    }
    archive.write_bytes(build_archive(content))
    registry = load_registry(files.root)
    add_version(registry, domain, version, archive, tag=f"v{version}", commit=COMMIT)


def test_check_a_line(files: Files, wheels: Path) -> None:
    """Each version that needs a result gets the verdict of the server's own code."""
    _release(files, "good_music", "1.0.0", "import helper_lib\n", ["helper-lib==1.0"])
    _release(files, "clash_music", "1.0.0", "", ["needs-old-orjson==1.0"])
    _release(files, "missing_music", "1.0.0", "", ["not-on-the-index==1.0"])
    _release(files, "broken_music", "1.0.0", "import does_not_exist\n")
    _release(files, "exiting_music", "1.0.0", "raise SystemExit(0)\n")
    # passed before and is the latest on nightly, so it is checked again; it fails now
    _release(files, "stopping_music", "1.0.0", "import does_not_exist\n")
    # failed before and is not the latest: not checked again
    _release(files, "stopping_music", "0.9.0", "")
    registry = load_registry(files.root)
    record_check(registry, "stopping_music", "1.0.0", "nightly", reason=None)
    record_check(registry, "stopping_music", "0.9.0", "nightly", reason="load: it did not load")
    # an archive that differs from its version file
    _release(files, "changed_music", "1.0.0", "")
    archive_path(files.root / "work", "changed_music", "1.0.0").write_bytes(
        build_archive({"providers/changed_music/manifest.json": b"{}"})
    )
    # without an archive in the work folder: no result
    _release(files, "absent_music", "1.0.0", "")
    archive_path(files.root / "work", "absent_music", "1.0.0").unlink()
    output = files.root / "results" / "nightly.json"

    run = _run(wheels, "--root", str(files.root), "check", "--line", "nightly",
               "--output", str(output))  # fmt: skip

    assert run.returncode == 0, run.stderr
    assert "absent_music 1.0.0: skipped, its archive is not in the work folder" in run.stdout
    results = load_model(output, LineResults)
    assert results.line == "nightly"
    found = {(r.domain, r.version): r for r in results.results}
    assert set(found) == {
        ("good_music", "1.0.0"),
        ("clash_music", "1.0.0"),
        ("missing_music", "1.0.0"),
        ("broken_music", "1.0.0"),
        ("exiting_music", "1.0.0"),
        ("stopping_music", "1.0.0"),
        ("changed_music", "1.0.0"),
    }
    assert found["good_music", "1.0.0"].passed
    clash = found["clash_music", "1.0.0"]
    assert not clash.passed
    assert clash.reason is not None
    assert clash.reason.startswith("dependencies: Requirements of clash_music clash with server")
    assert clash.output is not None
    assert "orjson==1.0" in clash.output
    missing = found["missing_music", "1.0.0"].reason
    assert missing is not None
    assert missing.startswith("dependencies: Requirements of missing_music cannot be installed")
    assert found["broken_music", "1.0.0"].reason == (
        "load: ModuleNotFoundError: No module named 'does_not_exist'"
    )
    assert "Traceback" in (found["broken_music", "1.0.0"].output or "")
    assert found["exiting_music", "1.0.0"].reason == "load: SystemExit: 0"
    stopping = found["stopping_music", "1.0.0"]
    assert (stopping.passed, stopping.passed_before) == (False, True)
    assert not found["good_music", "1.0.0"].passed_before
    changed = found["changed_music", "1.0.0"].reason
    assert changed is not None
    assert changed.startswith("content: the archive has the content hash")
    # the check step writes nothing but its results file
    assert load_registry(files.root).providers["good_music"].versions["1.0.0"].checks == {}


def test_a_frozen_line_is_not_checked(files: Files, wheels: Path) -> None:
    """A frozen line gets an empty results file."""
    files.write(
        "server_lines.json",
        {"lines": {"nightly": {"image": "ghcr.io/music-assistant/server:nightly", "frozen": True}}},
    )
    _release(files, "good_music", "1.0.0", "")
    output = files.root / "nightly.json"

    run = _run(wheels, "--root", str(files.root), "check", "--line", "nightly",
               "--output", str(output))  # fmt: skip

    assert run.returncode == 0, run.stderr
    assert "nightly is frozen" in run.stdout
    assert load_model(output, LineResults).results == []


def test_check_folder(files: Files, wheels: Path, tmp_path: Path) -> None:
    """A provider's own CI checks its manifest, requirements and import against a server image."""
    good = tmp_path / "checkout" / "good_music"
    write_tree(
        good,
        {
            "manifest.json": json.dumps(manifest("good_music", ["helper-lib==1.0"])).encode(),
            "__init__.py": b"import helper_lib\n",
        },
    )
    taken = tmp_path / "checkout" / "builtin_one"
    write_tree(taken, {"manifest.json": json.dumps(manifest("builtin_one")).encode()})
    clash = tmp_path / "checkout" / "clash_music"
    write_tree(
        clash,
        {"manifest.json": json.dumps(manifest("clash_music", ["needs-old-orjson==1.0"])).encode()},
    )

    passed = _run(wheels, "--root", str(files.root), "check-folder", str(good))
    reserved = _run(wheels, "--root", str(files.root), "check-folder", str(taken))
    clashing = _run(wheels, "--root", str(files.root), "check-folder", str(clash))

    assert (passed.returncode, passed.stdout.strip()) == (0, "good_music: passed")
    assert reserved.returncode == 1
    assert "builtin_one: manifest: the domain builtin_one is a provider the server ships" in (
        reserved.stderr
    )
    assert clashing.returncode == 1
    assert "clash_music: dependencies: Requirements of clash_music clash with server" in (
        clashing.stderr
    )


def _build_wheel(folder: Path, name: str, version: str, *requires: str) -> None:
    """Write a pure-Python wheel with one module named after the distribution."""
    module = name.replace("-", "_")
    dist_info = f"{module}-{version}.dist-info"
    metadata = f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n"
    metadata += "".join(f"Requires-Dist: {req}\n" for req in requires)
    content = {
        f"{module}/__init__.py": f"VERSION = {version!r}\n",
        f"{dist_info}/METADATA": metadata,
        f"{dist_info}/WHEEL": "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
    }
    record = "".join(
        f"{path},sha256={_record_hash(text)},{len(text)}\n" for path, text in content.items()
    )
    content[f"{dist_info}/RECORD"] = f"{record}{dist_info}/RECORD,,\n"
    with zipfile.ZipFile(folder / f"{module}-{version}-py3-none-any.whl", "w") as wheel:
        for path, text in content.items():
            wheel.writestr(path, text)


def _record_hash(content: str) -> str:
    """Return the hash of a file as a wheel's RECORD writes it."""
    digest = hashlib.sha256(content.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
