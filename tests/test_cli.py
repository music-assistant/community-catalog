"""Tests for the command line of the registry."""

from __future__ import annotations

import json
import shutil
from typing import TYPE_CHECKING

import pytest

from registry.admission import COMMENT_MARKER
from registry.cli import main
from registry.content import read_folder
from registry.github import COMMENTS_PER_PAGE, GitHub
from registry.registry import load_registry
from tests.conftest import (
    COMMIT,
    DOMAIN,
    ICON,
    FakeGitHub,
    build_archive,
    manifest,
    provider_file,
    write_tree,
)

if TYPE_CHECKING:
    from pathlib import Path

    from tests.conftest import Files

REGISTRY = "music-assistant/community-catalog"
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


def test_a_provider_folder_named_otherwise(files: Files, tmp_path: Path) -> None:
    """A provider folder not named after the domain gives the same content and reaches the index."""
    files.provider(folder="provider")
    archive = tmp_path / "repo.tar.gz"
    archive.write_bytes(
        build_archive({f"provider/{path}": data for path, data in PROVIDER.items()})
    )
    named = tmp_path / DOMAIN
    write_tree(named, PROVIDER)

    assert _add(files, archive) == 0
    assert _run(files, "record-check", DOMAIN, "1.0.0", "nightly", "--passed") == 0
    assert _run(files, "build") == 0
    assert _run(files, "validate") == 0

    version = load_registry(files.root).providers[DOMAIN].versions["1.0.0"]
    assert version.folder == "provider"
    assert version.content_hash == read_folder(named).content_hash
    index = json.loads(files.root.joinpath("site/v1/nightly.json").read_bytes())
    assert index["providers"][DOMAIN]["versions"]["1.0.0"]["folder"] == "provider"


def test_a_provider_folder_named_otherwise_must_hold_the_domain(
    files: Files, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A folder that is not named after the domain is refused when its manifest names another."""
    files.provider(folder="provider")
    other = {**PROVIDER, "manifest.json": json.dumps(manifest("other_music")).encode()}
    archive = tmp_path / "repo.tar.gz"
    archive.write_bytes(build_archive({f"provider/{path}": data for path, data in other.items()}))

    assert _add(files, archive) == 1
    assert f"manifest.domain: 'other_music' is not {DOMAIN}" in capsys.readouterr().err
    assert "1.0.0" not in load_registry(files.root).providers[DOMAIN].versions


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


@pytest.mark.parametrize("reason", ["\udcff", "load: one\nload: two", "x" * 301, "\x1b[2J"])
def test_record_check_refuses_a_reason_that_is_not_one_line(
    files: Files, capsys: pytest.CaptureFixture[str], reason: str
) -> None:
    """A reason is one line of at most 300 printable characters, and plain JSON."""
    files.provider()
    path = files.version(channels={})
    before = path.read_bytes()

    assert _run(files, "record-check", DOMAIN, "1.0.0", "nightly", "--failed", reason) == 1

    assert "checks.nightly.reason: is not one line of at most 300 printable characters" in (
        capsys.readouterr().err
    )
    assert path.read_bytes() == before
    assert _run(files, "record-check", DOMAIN, "1.0.0", "nightly", "--failed", "x" * 300) == 0


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
    """The result of a channel is recorded and replaced; a channel that is not listed is refused."""
    files.provider()
    files.version(channels={})

    assert _run(files, "record-check", DOMAIN, "1.0.0", "nightly", "--failed", "no import") == 0
    assert _run(files, "record-check", DOMAIN, "1.0.0", "beta", "--passed",
                "--checked", "2026-11-02T03:00:00Z") == 0  # fmt: skip
    assert _run(files, "record-check", DOMAIN, "1.0.0", "2.10", "--passed") == 1
    assert "checks: '2.10' is not a channel in channels.json" in capsys.readouterr().err

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
    files.version("1.0.0", channels={})
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
    # a listed provider is written without the keys it does not need
    assert json.loads((files.root / f"providers/{DOMAIN}/provider.json").read_text()) == {
        key: value for key, value in provider_file().items() if key != "status"
    }
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
    files.version("1.0.0", channels={})
    report = {
        "checked": "2026-11-15T02:00:00Z",
        "issue_digest": "",
        "domain": DOMAIN,
        "version": "1.0.0",
        "findings": [{"passed": True, "text": "The domain is free", "detail": DOMAIN}],
    }
    files.write("work/admission.json", report)
    entry = [{"domain": DOMAIN, "version": "1.0.0"}]
    files.write("work/plan.json", {"channels": {"nightly": entry, "beta": entry}})
    failed = {"domain": DOMAIN, "version": "1.0.0", "passed": False, "reason": "load: x"}
    files.write(
        f"results/nightly/{DOMAIN}-1.0.0.json",
        {"channel": "nightly", "checked": "2026-11-15T03:00:00Z", "results": [failed]},
    )
    results = str(files.root / "results")

    assert _run(files, "check-admission", results) == 1
    out = capsys.readouterr().out
    assert "- ✅ The domain is free: ` sample_music `" in out
    assert "- ❌ Failed on the nightly channel: ` load: x `" in out
    assert "- ❌ The checks did not run on the beta channel" in out
    files.write(
        f"results/beta/{DOMAIN}-1.0.0.json",
        {
            "channel": "beta",
            "checked": "2026-11-15T03:00:00Z",
            "results": [{"domain": DOMAIN, "version": "1.0.0", "passed": True}],
        },
    )
    comment = files.root / "comment.md"
    url = "https://github.com/music-assistant/community-catalog/pull/12"
    assert _run(files, "check-admission", results, "--comment", str(comment), "--proposal",
                "success", "--outcome", "opened", "--pull-request", url) == 0  # fmt: skip
    out = capsys.readouterr().out
    assert "Every check passed. A registry maintainer reviews" in out
    assert f"[the pull request that adds it]({url})" in out
    assert comment.read_text() == out.removesuffix("\n")
    # a declined proposal is never offered for merging
    assert _run(files, "check-admission", results, "--proposal", "success", "--outcome",
                "changed", "--pull-request", "", "--others", "") == 0  # fmt: skip
    out = capsys.readouterr().out
    assert "but the issue changed while it was checked" in out
    assert "merging" not in out
    assert _run(files, "check-admission", results, "--proposal", "failure", "--outcome", "",
                "--pull-request", "") == 0  # fmt: skip
    assert "but a step after the checks could not finish" in (capsys.readouterr().out)
    with pytest.raises(SystemExit):
        _run(files, "check-admission", results, "--pull-request", "https://example.com/pull/1")
    with pytest.raises(SystemExit):
        _run(files, "check-admission", results, "--outcome", "merged")


def test_check_admission_without_the_prepare_step(
    files: Files, capsys: pytest.CaptureFixture[str]
) -> None:
    """Without the files of the prepare step the result says the checks could not run."""
    comment = files.root / "comment.md"
    run_url = "https://github.com/music-assistant/community-catalog/actions/runs/1"

    code = _run(files, "check-admission", str(files.root / "results"), "--comment",
                str(comment), "--run-url", run_url)  # fmt: skip

    assert code == 1
    captured = capsys.readouterr()
    assert "warning: the admission checks did not run: " in captured.err
    assert "The checks could not finish." in captured.out
    assert comment.read_text() == captured.out.removesuffix("\n")
    assert f"[The run of these checks]({run_url})" in comment.read_text()


def test_prepare_admission_of_an_issue_that_is_no_submission(
    files: Files, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An issue that is no submission ends the prepare step well, with the refusal in its report."""
    github = FakeGitHub()
    github.issue(REGISTRY, 7, "### Repository\n\nsomeone/ma-sample\n", labels=[])
    monkeypatch.setenv("GITHUB_TOKEN", "a-token")
    monkeypatch.setattr("registry.cli.GitHub", lambda token: GitHub(token, github))

    code = _run(files, "prepare-admission", "--repository", REGISTRY, "--issue", "7")

    assert code == 0
    assert "refused: '#7 is no issue with the label submission'" in capsys.readouterr().out
    report = json.loads((files.root / "work" / "admission.json").read_text())
    assert report["domain"] is None
    assert json.loads((files.root / "work" / "plan.json").read_text()) == {"channels": {}}
    assert [request.full_url for request in github.requests] == [
        f"https://api.github.com/repos/{REGISTRY}/issues/7"
    ]


def test_post_admission_comment(
    files: Files, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The result is one comment of the workflow on the issue, updated on every run."""
    github = FakeGitHub()
    monkeypatch.setenv("GITHUB_TOKEN", "a-token")
    monkeypatch.setattr("registry.cli.GitHub", lambda token: GitHub(token, github))
    repository = REGISTRY
    # the marker in a comment of someone else, and a full first page of other comments
    others = [
        {"id": 1000 + number, "user": {"login": "someone"}, "body": f"{COMMENT_MARKER} no"}
        for number in range(COMMENTS_PER_PAGE)
    ]
    github.comments[repository, 11] = list(others)
    comment = files.root / "comment.md"
    args = ("post-admission-comment", str(comment), "--repository", repository,
            "--issue", "11")  # fmt: skip

    comment.write_text("## Admission checks\n\nfirst\n")
    assert _run(files, *args) == 0
    comment.write_text("## Admission checks\n\nsecond\n")
    assert _run(files, *args) == 0

    [ours] = github.comments[repository, 11][COMMENTS_PER_PAGE:]
    assert ours["body"] == f"{COMMENT_MARKER}\n## Admission checks\n\nsecond\n"
    assert github.comments[repository, 11][:COMMENTS_PER_PAGE] == others
    assert capsys.readouterr().out.count(ours["html_url"]) == 2


def test_a_manifest_beyond_the_limits_is_refused_everywhere(
    files: Files, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """add-version refuses a manifest of more than 16 KiB, and validate a version file with one."""
    files.provider()
    folder = tmp_path / DOMAIN
    huge = {**manifest(), "description": "y" * 20_000_000}
    write_tree(folder, {**PROVIDER, "manifest.json": json.dumps(huge).encode()})

    assert _add(files, folder) == 1
    assert "manifest: 20000" in capsys.readouterr().err
    assert load_registry(files.root).providers[DOMAIN].versions == {}
    files.version("1.0.0", manifest={**manifest(requirements=["some-package==1.0"]), **huge})
    assert _run(files, "validate") == 1
    assert "bytes as JSON, more than 16384" in capsys.readouterr().err


def test_an_index_beyond_the_limit_is_refused(
    files: Files, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Build and validate refuse an index that a server may not download within 10 seconds."""
    files.provider()
    files.version("1.0.0")
    assert _run(files, "build") == 0
    monkeypatch.setattr("registry.index.MAX_INDEX_SIZE", 1000)

    assert _run(files, "build") == 1
    assert "site/v1/nightly.json: 1223 bytes, more than" in capsys.readouterr().err
    assert _run(files, "validate") == 1
    assert "more than the 1000 bytes a server is sure to download" in capsys.readouterr().err


def test_blocks_and_withdrawals_reach_a_frozen_channel(files: Files) -> None:
    """A frozen channel is not checked, while a block or a withdrawal still reach its index."""
    files.write(
        "channels.json",
        {"channels": {"2.11": {"image": "ghcr.io/music-assistant/server:2.11", "frozen": True}}},
    )
    files.provider()
    files.version("1.0.0", channels={"2.11": True})
    files.version("1.1.0", channels={"2.11": True})
    index = files.root / "site" / "v1" / "2.11.json"

    assert _run(files, "block-version", DOMAIN, "1.1.0", "--reason", "Sends data elsewhere") == 0
    provider = json.loads(index.read_text())["providers"][DOMAIN]
    assert (provider["latest"], provider["blocked_versions"]) == (
        "1.0.0",
        {"1.1.0": "Sends data elsewhere"},
    )
    assert _run(files, "set-status", DOMAIN, "withdrawn", "--reason", "No longer kept up") == 0
    assert json.loads(index.read_text())["providers"][DOMAIN]["status"] == "withdrawn"
    assert _run(files, "set-status", DOMAIN, "blocked", "--reason", "Sends data elsewhere") == 0
    assert json.loads(index.read_text())["providers"][DOMAIN]["status"] == "blocked"


@pytest.mark.parametrize(
    ("changed", "refused"),
    [
        (["providers/sample_music/provider.json", "site/v1/nightly.json"], []),
        (["registry/cli.py"], []),
        (
            ["providers/sample_music/versions/1.0.0.json", "providers/x/refused/1.0.0.json"],
            ["providers/sample_music/versions/1.0.0.json", "providers/x/refused/1.0.0.json"],
        ),
        (["providers/x/versions/new\nline.json"], ["providers/x/versions/new\nline.json"]),
    ],
)
def test_check_changes(
    files: Files,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    changed: list[str],
    refused: list[str],
) -> None:
    """A pull request that adds or changes a version file or a refusal is refused."""
    listing = tmp_path / "changed"
    listing.write_bytes("\0".join(changed).encode() + b"\0")

    assert _run(files, "check-changes", str(listing)) == (1 if refused else 0)

    out = capsys.readouterr().out
    assert out.splitlines() == [
        f"{path!r}: only the release pipeline writes version files and refusals" for path in refused
    ]
