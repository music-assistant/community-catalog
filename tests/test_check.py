"""Tests for the check step, run for real with the interpreter of a server checkout."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import subprocess
import zipfile
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from registry.cli import main
from registry.edit import add_version, record_check
from registry.files import load_model
from registry.models import LineResults
from registry.pipeline import archive_path, make_plan, read_results, write_plan
from registry.registry import load_registry
from tests.conftest import (
    COMMIT,
    ROOT,
    build_archive,
    can_run_check_line,
    docker_runs,
    manifest,
    run_check_line,
    write_tree,
)

if TYPE_CHECKING:
    from tests.conftest import Files

# a checkout of the server with the community catalog and its environment with uv; by default
# the server next to the registry
SERVER = Path(os.environ.get("SERVER_CHECKOUT", ROOT.parent / "server"))
SERVER_PYTHON = SERVER / ".venv" / "bin" / "python"
# runs a command of the registry with uv pointed at the wheels the test built, as the server's
# own tests do; nothing reaches the network
RUN_OFFLINE = """
import os, sys
from music_assistant.controllers.community_catalog import dependencies
dependencies._UV_INDEX_ARGS[:] = ["--no-index", "--find-links", sys.argv[1]]
if "CHECK_LOAD_TIMEOUT" in os.environ:
    import registry.check
    registry.check.LOAD_TIMEOUT = int(os.environ["CHECK_LOAD_TIMEOUT"])
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


def _run(wheels: Path, *args: str, timeout: int | None = None) -> subprocess.CompletedProcess[str]:
    """Run a command of the registry with the interpreter of the server checkout."""
    environment = {key: value for key, value in os.environ.items() if not key.startswith("UV_")}
    environment["UV_OFFLINE"] = "1"
    if timeout is not None:
        environment["CHECK_LOAD_TIMEOUT"] = str(timeout)
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


def _check(
    files: Files, wheels: Path, domain: str, stage: str, **run: int
) -> subprocess.CompletedProcess[str]:
    """Run a stage of the checks of version 1.0.0 of a provider on nightly, as a container does."""
    scratch = files.root / "scratch" / domain
    scratch.mkdir(parents=True, exist_ok=True)
    return _run(wheels, "--root", str(files.root), "check", "--line", "nightly",
                "--domain", domain, "--version", "1.0.0", "--stage", stage,
                "--scratch", str(scratch), "--output", str(scratch / f"{stage}.json"),
                **run)  # fmt: skip


@pytest.mark.skipif(not can_run_check_line(), reason="needs bash, jq and timeout")
def test_check_a_line(files: Files, wheels: Path, tmp_path: Path) -> None:
    """Each version of the plan gets the verdict of the server's own code, in containers."""
    _release(files, "good_music", "1.0.0", "import helper_lib\n", ["helper-lib==1.0"])
    _release(files, "clash_music", "1.0.0", "", ["needs-old-orjson==1.0"])
    _release(files, "missing_music", "1.0.0", "", ["not-on-the-index==1.0"])
    _release(files, "broken_music", "1.0.0", "import does_not_exist\n")
    _release(files, "exiting_music", "1.0.0", "raise SystemExit(0)\n")
    # passed before and is the latest on nightly, so it is checked again; it fails now
    _release(files, "stopping_music", "1.0.0", "import does_not_exist\n")
    # failed before and is below the latest: not checked again
    _release(files, "stopping_music", "0.9.0", "")
    registry = load_registry(files.root)
    record_check(registry, "stopping_music", "1.0.0", "nightly", reason=None)
    record_check(registry, "stopping_music", "0.9.0", "nightly", reason="load: it did not load")
    # an archive that differs from its version file
    _release(files, "changed_music", "1.0.0", "")
    archive_path(files.root / "work", "changed_music", "1.0.0").write_bytes(
        build_archive({"providers/changed_music/manifest.json": b"{}"})
    )
    # without an archive in the work folder: not in the plan
    _release(files, "absent_music", "1.0.0", "")
    archive_path(files.root / "work", "absent_music", "1.0.0").unlink()
    # the registry code travels with the files, as in the artifact of the fetch step
    shutil.copytree(ROOT / "registry", files.root / "registry")
    plan = make_plan(load_registry(files.root), files.root / "work")
    write_plan(plan, files.root / "work")
    log = tmp_path / "docker.log"

    run = run_check_line(
        files.root, "nightly", [str(SERVER_PYTHON), "-c", RUN_OFFLINE, str(wheels)], log
    )

    assert run.returncode == 0, run.stderr
    assert [(e.domain, e.version) for e in plan.lines["nightly"]] == [
        ("broken_music", "1.0.0"),
        ("changed_music", "1.0.0"),
        ("clash_music", "1.0.0"),
        ("exiting_music", "1.0.0"),
        ("good_music", "1.0.0"),
        ("missing_music", "1.0.0"),
        ("stopping_music", "1.0.0"),
    ]
    results = read_results(files.root / "results", plan)
    found = {(r.domain, r.version): r for file in results for r in file.results}
    assert set(found) == {(e.domain, e.version) for e in plan.lines["nightly"]}
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
    # a version that fails before its code runs gets no load stage
    loaded = {call[call.index("--domain") + 1] for call in docker_runs(log) if "load" in call}
    assert loaded == {"broken_music", "exiting_music", "good_music", "stopping_music"}
    # the check step writes nothing but its results files
    assert load_registry(files.root).providers["good_music"].versions["1.0.0"].checks == {}
    assert main(["--root", str(files.root), "apply-results", str(files.root / "results")]) == 0
    assert main(["--root", str(files.root), "validate"]) == 0


def test_a_check_that_cannot_run_gives_no_result(files: Files, tmp_path: Path) -> None:
    """When uv cannot reach the package index, the install stage writes no results file."""
    _release(files, "good_music", "1.0.0", "", ["helper-lib==1.0"])

    run = _check(files, tmp_path / "no-such-index", "good_music", "install")

    assert run.returncode == 1
    assert "good_music 1.0.0: the check could not run: " in run.stderr
    assert not (files.root / "scratch" / "good_music" / "install.json").exists()


def test_the_load_timeout_ends_every_process_of_the_import(
    files: Files, wheels: Path, tmp_path: Path
) -> None:
    """An import that hangs fails after the timeout, and what it started is ended with it."""
    pid_file = tmp_path / "child.pid"
    code = (
        "import subprocess, time\n"
        "child = subprocess.Popen(['sleep', '600'])\n"
        f"open({str(pid_file)!r}, 'w').write(str(child.pid))\n"
        "time.sleep(600)\n"
    )
    _release(files, "hanging_music", "1.0.0", code)

    assert _check(files, wheels, "hanging_music", "install").returncode == 0
    run = _check(files, wheels, "hanging_music", "load", timeout=3)

    assert run.returncode == 0, run.stderr
    result = load_model(files.root / "scratch" / "hanging_music" / "load.json", LineResults)
    assert result.results[0].reason == "load: the import did not finish within 3 seconds"
    pid = int(pid_file.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_the_output_of_a_load_keeps_its_end(files: Files, wheels: Path) -> None:
    """A provider that prints a lot keeps the last 50 kB of its output, with the error."""
    code = "print('x' * 200_000)\nraise RuntimeError('boom')\n"
    _release(files, "noisy_music", "1.0.0", code)

    assert _check(files, wheels, "noisy_music", "install").returncode == 0
    assert _check(files, wheels, "noisy_music", "load").returncode == 0

    [result] = load_model(files.root / "scratch" / "noisy_music" / "load.json", LineResults).results
    assert result.reason == "load: RuntimeError: boom"
    assert result.output is not None
    assert len(result.output) <= 50_000
    assert result.output.rstrip().endswith("load: RuntimeError: boom")


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
