"""Tests for scripts/check-line.sh: what the trusted side keeps of the containers it runs."""

from __future__ import annotations

import json
import sys
from typing import TYPE_CHECKING

import pytest

from registry.cli import main
from registry.registry import load_registry
from tests.conftest import can_run_check_line, docker_runs, run_check_line

if TYPE_CHECKING:
    from pathlib import Path

    from tests.conftest import Files

pytestmark = pytest.mark.skipif(not can_run_check_line(), reason="needs bash, jq and timeout")

# a container that does what a hostile provider could, by domain and stage
HOSTILE = """
import json, os, sys

args = sys.argv[1:]
value = {key: args[args.index(key) + 1] for key in ("--domain", "--stage", "--output")}
domain, stage, output = value["--domain"], value["--stage"], value["--output"]
results = os.path.dirname(output)


def result(of=domain, reason=None):
    content = {"line": "nightly", "checked": "2026-11-15T03:00:00Z", "results": [
        {"domain": of, "version": "1.0.0", "passed": reason is None, "reason": reason}]}
    return json.dumps(content)


if stage == "install":
    if domain == "unavailable_music":
        sys.exit(1)
    if domain == "failing_music":
        open(output, "w").write(result(reason="dependencies: clash"))
    sys.exit(0)
if domain == "link_music":
    os.symlink(os.environ["SECRET"], output)
elif domain == "folder_music":
    os.mkdir(output)
    open(os.path.join(output, "x"), "w").write(result())
elif domain == "large_music":
    open(output, "w").write(" " * 300_000 + result())
elif domain == "fifo_music":
    os.mkfifo(output)
elif domain == "good_music":
    open(output, "w").write(result())
    open(os.path.join(results, "extra.json"), "w").write(result(of="victim_music"))
    os.mkdir(os.path.join(results, "locked"))
    os.chmod(os.path.join(results, "locked"), 0)
elif domain == "forging_music":
    open(output, "w").write(result(of="victim_music", reason="load: forged"))
"""
DOMAINS = [
    "failing_music",
    "fifo_music",
    "folder_music",
    "forging_music",
    "good_music",
    "large_music",
    "link_music",
    "unavailable_music",
    "victim_music",
]


@pytest.fixture
def catalog(files: Files) -> Files:
    """Return a registry with a version of each provider, all of them in the plan for nightly."""
    for domain in DOMAINS:
        files.provider(domain, folder=domain)
        files.version("1.0.0", domain, folder=domain, lines={"nightly": True})
    plan = [
        {"domain": domain, "version": "1.0.0"} for domain in DOMAINS if domain != "victim_music"
    ]
    files.write("work/plan.json", {"lines": {"nightly": plan}})
    assert main(["--root", str(files.root), "build"]) == 0
    return files


def test_only_the_results_file_each_container_was_asked_for_is_kept(
    catalog: Files, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A link, a folder, a special or large file, and any other file a container wrote are left."""
    script = tmp_path / "hostile.py"
    script.write_text(HOSTILE)
    secret = tmp_path / "environ"
    secret.write_text("ACTIONS_RUNTIME_TOKEN=secret")
    monkeypatch.setenv("SECRET", str(secret))
    log = tmp_path / "docker.log"

    run = run_check_line(catalog.root, "nightly", [sys.executable, str(script)], log)

    assert run.returncode == 0, run.stderr
    kept = catalog.root / "results" / "nightly"
    assert sorted(path.name for path in kept.iterdir()) == [
        "failing_music-1.0.0.json",
        "forging_music-1.0.0.json",
        "good_music-1.0.0.json",
    ]
    assert all(path.is_file() and not path.is_symlink() for path in kept.iterdir())
    assert "secret" not in "".join(path.read_text() for path in kept.iterdir())
    for domain in ("link_music", "folder_music", "large_music", "fifo_music"):
        assert f"{domain}-1.0.0.json: no result, the container left no results file" in run.stdout
    assert "unavailable_music-1.0.0.json: no result, the check could not run" in run.stdout
    # the forged result of the victim is ignored by apply-results, the others are recorded
    assert main(["--root", str(catalog.root), "apply-results", str(catalog.root / "results")]) == 0
    versions = {d: p.versions["1.0.0"] for d, p in load_registry(catalog.root).providers.items()}
    assert versions["victim_music"].checks["nightly"].passed
    assert not versions["failing_music"].checks["nightly"].passed
    assert versions["good_music"].checks["nightly"].checked == "2026-11-15T03:00:00Z"


def test_one_container_per_version_and_stage_with_limits(
    catalog: Files, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each version gets its own containers; provider code runs without network or privileges."""
    script = tmp_path / "hostile.py"
    script.write_text(HOSTILE)
    monkeypatch.setenv("SECRET", str(tmp_path))
    log = tmp_path / "docker.log"

    run = run_check_line(catalog.root, "nightly", [sys.executable, str(script)], log)

    assert run.returncode == 0, run.stderr
    runs = docker_runs(log)
    names = [run[run.index("--name") + 1] for run in runs]
    assert len(set(names)) == len(names)
    stages = {(run[run.index("--domain") + 1], run[run.index("--stage") + 1]) for run in runs}
    # the load stage runs only when the install stage left no result and did not fail
    assert ("failing_music", "load") not in stages
    assert ("unavailable_music", "load") not in stages
    assert ("good_music", "install") in stages
    assert ("good_music", "load") in stages
    for call in runs:
        options = json.dumps(call)
        for option in (
            '"--user"',
            '"--cap-drop", "ALL"',
            '"--security-opt", "no-new-privileges"',
            '"--pids-limit", "512"',
            '"--memory", "4g"',
            ":/catalog:ro",
        ):
            assert option in options
        load = call[call.index("--stage") + 1] == "load"
        assert call[call.index("--network") + 1] == ("none" if load else "bridge")
        assert any(v.endswith(":/scratch:ro" if load else ":/scratch:rw") for v in call)
        # the trusted side names the results file
        assert call[call.index("--output") + 1] == "/results/result.json"


def test_nothing_runs_for_a_line_without_a_plan(catalog: Files, tmp_path: Path) -> None:
    """A frozen line has no versions in the plan, so no container runs."""
    log = tmp_path / "docker.log"

    run = run_check_line(catalog.root, "2.11", [sys.executable, "-c", "raise SystemExit(1)"], log)

    assert run.returncode == 0, run.stderr
    assert docker_runs(log) == []
    assert list((catalog.root / "results" / "2.11").iterdir()) == []
