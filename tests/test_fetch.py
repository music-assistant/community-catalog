"""Tests for the fetch step, against a fake GitHub."""

from __future__ import annotations

import http.client
import json
import os
from typing import TYPE_CHECKING, Any

import pytest

from registry.cli import main
from registry.fetch import fetch
from registry.files import load_model
from registry.github import GitHub, GitHubError
from registry.models import CheckPlan, FetchReport
from registry.pipeline import FETCH_REPORT, PLAN_FILE, archive_path
from registry.registry import load_registry
from tests.conftest import (
    DOMAIN,
    FakeGitHub,
    build_archive,
    manifest,
    provider_archive,
    release,
    request_json,
)

if TYPE_CHECKING:
    from tests.conftest import Files

REPOSITORY = "someone/ma-sample"
COMMIT_1 = "1" * 40
COMMIT_2 = "2" * 40
COMMIT_3 = "3" * 40


@pytest.fixture
def github() -> FakeGitHub:
    """Return a GitHub without repositories."""
    return FakeGitHub()


def _fetch(files: Files, github: FakeGitHub) -> FetchReport:
    """Run the fetch step on the registry of the fixture, with its work folder."""
    return fetch(load_registry(files.root), GitHub("a-token", github), files.root / "work")


def _downloads(github: FakeGitHub) -> list[str]:
    """Return the archives downloaded so far."""
    return [r.full_url for r in github.requests if r.full_url.startswith("https://codeload")]


def test_only_the_newest_release_becomes_a_version(files: Files, github: FakeGitHub) -> None:
    """Of several new releases only the newest is taken; drafts and pre-releases are ignored."""
    files.provider()
    github.repository(
        REPOSITORY,
        [
            release("v3.0.0", COMMIT_3, "2026-11-06T00:00:00Z", draft=True),
            release("v2.0.0", COMMIT_3, "2026-11-05T00:00:00Z", prerelease=True),
            release("v1.2.0", COMMIT_2, "2026-11-03T00:00:00Z"),
            release("v1.1.0", COMMIT_1, "2026-11-02T00:00:00Z"),
        ],
    )
    github.archives[REPOSITORY, COMMIT_2] = provider_archive()
    github.archives[REPOSITORY, COMMIT_1] = provider_archive()

    report = _fetch(files, github)

    versions = load_registry(files.root).providers[DOMAIN].versions
    assert set(versions) == {"1.2.0"}
    version = versions["1.2.0"]
    assert (version.tag, version.commit, version.released) == (
        "v1.2.0",
        COMMIT_2,
        "2026-11-03T00:00:00Z",
    )
    assert version.checks == {}
    assert archive_path(files.root / "work", DOMAIN, "1.2.0").is_file()
    assert report.refused == []
    assert report.repositories[REPOSITORY].has_issues
    assert load_model(files.root / "work" / FETCH_REPORT, FetchReport) == report


def test_the_most_recently_published_release_counts(files: Files, github: FakeGitHub) -> None:
    """A release for an older line, published after a higher version, is taken."""
    files.provider()
    files.version("2.0.0")
    github.repository(
        REPOSITORY,
        [
            release("v1.5.0", COMMIT_1, "2026-11-04T00:00:00Z"),
            release("v2.0.0", COMMIT_2, "2026-11-03T00:00:00Z"),
        ],
    )
    github.archives[REPOSITORY, COMMIT_1] = provider_archive()

    _fetch(files, github)

    assert set(load_registry(files.root).providers[DOMAIN].versions) == {"1.5.0", "2.0.0"}


def test_a_known_newest_release_is_left_alone(files: Files, github: FakeGitHub) -> None:
    """When the newest release has a version file, an older release without one is not taken."""
    files.provider()
    files.version("1.2.0", commit=COMMIT_2, lines={"nightly": False, "beta": False})
    github.repository(
        REPOSITORY,
        [
            release("v1.2.0", COMMIT_2, "2026-11-03T00:00:00Z"),
            release("v1.1.0", COMMIT_1, "2026-11-02T00:00:00Z"),
        ],
    )
    github.archives[REPOSITORY, COMMIT_1] = provider_archive()

    _fetch(files, github)

    assert set(load_registry(files.root).providers[DOMAIN].versions) == {"1.2.0"}
    # only to check the known version again, as it passes on no line
    assert _downloads(github) == [f"https://codeload.github.com/{REPOSITORY}/tar.gz/{COMMIT_2}"]


def test_tags_of_other_providers_are_ignored(files: Files, github: FakeGitHub) -> None:
    """In a repository with several providers only tags with the provider's prefix count."""
    files.provider(tag_prefix="sample_music-")
    files.provider("other_music", tag_prefix="other_music-")
    github.repository(
        REPOSITORY,
        [
            release("other_music-v2.0.0", COMMIT_3, "2026-11-05T00:00:00Z"),
            release("1.9.0", COMMIT_3, "2026-11-04T00:00:00Z"),
            release("sample_music-1.0.0", COMMIT_1, "2026-11-03T00:00:00Z"),
        ],
    )
    archive = provider_archive()
    github.archives[REPOSITORY, COMMIT_1] = archive
    github.archives[REPOSITORY, COMMIT_3] = provider_archive("other_music")

    _fetch(files, github)

    providers = load_registry(files.root).providers
    assert set(providers[DOMAIN].versions) == {"1.0.0"}
    assert providers[DOMAIN].versions["1.0.0"].tag == "sample_music-1.0.0"
    assert set(providers["other_music"].versions) == {"2.0.0"}
    # one GraphQL request for both providers of the repository
    assert len([r for r in github.requests if r.full_url.endswith("/graphql")]) == 1


def test_a_refused_release_is_recorded_and_not_tried_again(
    files: Files, github: FakeGitHub
) -> None:
    """A release that cannot become a version is recorded with the reason, once."""
    files.provider()
    github.repository(REPOSITORY, [release("v1.0.0", COMMIT_1)])
    github.archives[REPOSITORY, COMMIT_1] = provider_archive(domain="other_music")

    report = _fetch(files, github)
    again = _fetch(files, github)

    provider = load_registry(files.root).providers[DOMAIN]
    assert provider.versions == {}
    refused = provider.refused["1.0.0"]
    assert (refused.tag, refused.commit) == ("v1.0.0", COMMIT_1)
    assert refused.reason == "the repository has no folder providers/sample_music"
    assert [(r.domain, r.version) for r in report.refused] == [(DOMAIN, "1.0.0")]
    assert again.refused == []
    assert len(_downloads(github)) == 1


def test_an_invalid_manifest_refuses_the_release(files: Files, github: FakeGitHub) -> None:
    """The checks of add-version decide what is refused."""
    files.provider()
    github.repository(REPOSITORY, [release("v1.0.0", COMMIT_1)])
    github.archives[REPOSITORY, COMMIT_1] = provider_archive(type="core")

    _fetch(files, github)

    reason = load_registry(files.root).providers[DOMAIN].refused["1.0.0"].reason
    assert "manifest.type: 'core' is not one of" in reason


def test_a_download_that_fails_is_tried_again(files: Files, github: FakeGitHub) -> None:
    """An archive GitHub does not serve is no refusal: nothing is recorded."""
    files.provider()
    github.repository(REPOSITORY, [release("v1.0.0", COMMIT_1)])

    report = _fetch(files, github)

    provider = load_registry(files.root).providers[DOMAIN]
    assert (provider.versions, provider.refused, report.refused) == ({}, {}, [])


def test_blocked_and_missing_repositories(
    files: Files, github: FakeGitHub, capsys: pytest.CaptureFixture[str]
) -> None:
    """A blocked provider is not read; a repository that is gone is a warning."""
    files.provider(status="blocked", reason="Sends the library elsewhere")
    files.provider("gone_music", repository="someone/gone")
    files.provider("other_music", repository="someone/other", status="withdrawn", reason="Old")
    github.repository(REPOSITORY, [release("v1.0.0", COMMIT_1)])
    github.repository("someone/other", [release("v1.0.0", COMMIT_2)])
    github.archives["someone/other", COMMIT_2] = provider_archive("other_music")

    report = _fetch(files, github)

    providers = load_registry(files.root).providers
    assert providers[DOMAIN].versions == {}
    assert set(providers["other_music"].versions) == {"1.0.0"}
    assert set(report.repositories) == {"someone/other"}
    assert "gone_music: someone/gone does not exist or is not public" in capsys.readouterr().err
    [query] = [r for r in github.requests if r.full_url.endswith("/graphql")]
    assert REPOSITORY not in request_json(query)["variables"].values()


def test_archives_of_the_versions_to_check(files: Files, github: FakeGitHub) -> None:
    """The latest version of each line and versions without a result are downloaded too."""
    files.provider()
    # the latest on nightly, without a result for beta
    files.version("1.2.0", commit=COMMIT_3, lines={"nightly": True})
    # the latest on beta
    files.version("1.1.0", commit=COMMIT_2, lines={"nightly": True, "beta": True})
    # below the latest on both lines
    files.version("1.0.0", commit=COMMIT_1, lines={"nightly": True, "beta": False})
    github.repository(REPOSITORY, [release("v1.2.0", COMMIT_3)])
    for commit in (COMMIT_1, COMMIT_2, COMMIT_3):
        github.archives[REPOSITORY, commit] = provider_archive()

    _fetch(files, github)

    work = files.root / "work"
    assert archive_path(work, DOMAIN, "1.2.0").is_file()
    assert archive_path(work, DOMAIN, "1.1.0").is_file()
    assert not archive_path(work, DOMAIN, "1.0.0").exists()
    plan = load_model(work / PLAN_FILE, CheckPlan)
    assert {line: [(e.domain, e.version) for e in v] for line, v in plan.lines.items()} == {
        "beta": [(DOMAIN, "1.1.0"), (DOMAIN, "1.2.0")],
        "nightly": [(DOMAIN, "1.2.0")],
    }


def test_repositories_are_read_in_batches(files: Files, github: FakeGitHub) -> None:
    """Many repositories take a few requests, and their names are passed as variables."""
    for number in range(60):
        files.provider(f"music_{number}", repository=f"someone/repository-{number}")
        github.repository(f"someone/repository-{number}")

    _fetch(files, github)

    queries = [r for r in github.requests if r.full_url.endswith("/graphql")]
    assert len(queries) == 2
    for query in queries:
        assert "repository-" not in request_json(query)["query"]


def test_fetch_needs_a_token(
    files: Files, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Without a token in the environment nothing is requested."""
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    assert main(["--root", str(files.root), "fetch"]) == 1
    assert "set GITHUB_TOKEN to a GitHub token" in capsys.readouterr().err


def test_a_repository_that_is_not_the_admitted_one_is_skipped(
    files: Files, github: FakeGitHub, capsys: pytest.CaptureFixture[str]
) -> None:
    """Whoever holds the name of a repository with other ids releases nothing into the index."""
    files.provider()
    files.version("1.0.0", lines={"nightly": True})
    files.provider("other_music", repository="someone/other", folder="other_music")
    github.repository(REPOSITORY, [release("v1.1.0", COMMIT_1)], databaseId=999)
    github.repository("someone/other", [release("v1.0.0", COMMIT_2)])
    github.archives[REPOSITORY, COMMIT_1] = provider_archive()
    github.archives["someone/other", COMMIT_2] = _other_archive()

    report = _fetch(files, github)

    providers = load_registry(files.root).providers
    assert set(providers[DOMAIN].versions) == {"1.0.0"}
    assert set(providers["other_music"].versions) == {"1.0.0"}
    assert REPOSITORY not in "".join(_downloads(github))
    assert report.repositories[REPOSITORY].repository_id == 999
    assert f"{DOMAIN}: {REPOSITORY} is not the repository that was admitted" in (
        capsys.readouterr().err
    )
    plan = load_model(files.root / "work" / PLAN_FILE, CheckPlan)
    assert all(entry.domain != DOMAIN for entries in plan.lines.values() for entry in entries)


def test_one_repository_that_cannot_be_read_does_not_stop_the_others(
    files: Files, github: FakeGitHub, capsys: pytest.CaptureFixture[str]
) -> None:
    """A GraphQL error about one repository skips that provider only."""
    files.provider()
    files.provider("other_music", repository="someone/other", folder="other_music")
    github.repository(REPOSITORY, [release("v1.0.0", COMMIT_1)])
    github.errors[REPOSITORY] = {"type": "FORBIDDEN", "message": "Resource not accessible"}
    github.repository("someone/other", [release("v1.0.0", COMMIT_2)])
    github.archives["someone/other", COMMIT_2] = _other_archive()

    _fetch(files, github)

    providers = load_registry(files.root).providers
    assert providers[DOMAIN].versions == {}
    assert set(providers["other_music"].versions) == {"1.0.0"}
    assert "sample_music: GitHub could not answer someone/ma-sample: Resource not" in (
        capsys.readouterr().err
    )


def test_a_batch_that_fails_does_not_stop_the_next(
    files: Files, github: FakeGitHub, capsys: pytest.CaptureFixture[str]
) -> None:
    """When GitHub refuses a whole request, the repositories of the other requests are read."""
    for number in range(51):
        files.provider(f"music_{number}", repository=f"someone/repository-{number}")
        github.repository(f"someone/repository-{number}")
    graphql = GitHub("a-token", github)._graphql  # noqa: SLF001 - fails the first request
    calls: list[int] = []

    def flaky(query: str, variables: dict[str, str]) -> Any:
        calls.append(1)
        if len(calls) == 1:
            raise GitHubError("https://api.github.com/graphql answered 502")
        return graphql(query, variables)

    client = GitHub("a-token", github)
    client._graphql = flaky  # type: ignore[method-assign] # noqa: SLF001

    report = fetch(load_registry(files.root), client, files.root / "work")

    assert set(report.repositories) == {"someone/repository-9"}
    assert "music_0: https://api.github.com/graphql answered 502" in capsys.readouterr().err


def test_a_download_that_breaks_off_does_not_stop_the_others(
    files: Files, github: FakeGitHub, capsys: pytest.CaptureFixture[str]
) -> None:
    """An archive whose download breaks off is tried again tomorrow; the rest goes on."""
    files.provider()
    files.provider("other_music", repository="someone/other", folder="other_music")
    files.version("1.0.0", "other_music", commit=COMMIT_3, folder="other_music", lines={})
    github.repository(REPOSITORY, [release("v1.0.0", COMMIT_1)])
    github.repository("someone/other", [release("v2.0.0", COMMIT_2)])
    github.archives[REPOSITORY, COMMIT_1] = provider_archive()
    github.archives["someone/other", COMMIT_2] = _other_archive()
    github.archives["someone/other", COMMIT_3] = _other_archive()
    broken = f"https://codeload.github.com/{REPOSITORY}/tar.gz/{COMMIT_1}"
    github.broken[broken] = http.client.IncompleteRead(b"partial", 100)
    github.broken[f"https://codeload.github.com/someone/other/tar.gz/{COMMIT_3}"] = (
        http.client.IncompleteRead(b"", 100)
    )

    report = _fetch(files, github)

    providers = load_registry(files.root).providers
    assert (providers[DOMAIN].versions, providers[DOMAIN].refused) == ({}, {})
    assert set(providers["other_music"].versions) == {"1.0.0", "2.0.0"}
    assert report.refused == []
    err = capsys.readouterr().err
    assert "sample_music v1.0.0: the answer broke off: IncompleteRead" in err
    assert "other_music 1.0.0: the answer broke off" in err
    work = files.root / "work"
    assert not archive_path(work, DOMAIN, "1.0.0").exists()
    assert not list(work.rglob("*.part"))


def test_an_archive_beyond_the_limit_is_refused(
    files: Files, github: FakeGitHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A download larger than the limit is stopped there, and the release is refused."""
    monkeypatch.setattr("registry.github.MAX_ARCHIVE_SIZE", 100_000)
    files.provider()
    github.repository(REPOSITORY, [release("v1.0.0", COMMIT_1)])
    github.archives[REPOSITORY, COMMIT_1] = build_archive(
        {
            f"providers/{DOMAIN}/manifest.json": json.dumps(manifest()).encode(),
            "media/large.bin": os.urandom(150_000),
        }
    )

    report = _fetch(files, github)

    refused = load_registry(files.root).providers[DOMAIN].refused["1.0.0"]
    assert refused.reason == "the archive of the commit is larger than 100000 bytes"
    assert [(r.domain, r.version) for r in report.refused] == [(DOMAIN, "1.0.0")]
    assert not list((files.root / "work").rglob("*.tar.gz*"))


def test_deleting_a_refused_file_tries_the_release_again(files: Files, github: FakeGitHub) -> None:
    """A release whose refused file is deleted is downloaded and checked again."""
    files.provider()
    github.repository(REPOSITORY, [release("v1.0.0", COMMIT_1)])
    github.archives[REPOSITORY, COMMIT_1] = provider_archive(type="core")
    _fetch(files, github)
    path = files.root / "providers" / DOMAIN / "refused" / "1.0.0.json"
    assert path.is_file()
    # the reason was ours: a rule of the registry changed, say
    github.archives[REPOSITORY, COMMIT_1] = provider_archive()
    path.unlink()

    _fetch(files, github)

    provider = load_registry(files.root).providers[DOMAIN]
    assert (set(provider.versions), provider.refused) == ({"1.0.0"}, {})
    assert len(_downloads(github)) == 2


def test_a_long_refusal_is_cut(files: Files, github: FakeGitHub) -> None:
    """The reason of a refused release keeps at most 2000 characters."""
    files.provider()
    github.repository(REPOSITORY, [release("v1.0.0", COMMIT_1)])
    many = [f"bad requirement {number}" for number in range(40)]
    github.archives[REPOSITORY, COMMIT_1] = provider_archive(requirements=many, name="x" * 1000)

    _fetch(files, github)

    reason = load_registry(files.root).providers[DOMAIN].refused["1.0.0"].reason
    assert len(reason) == 2000
    assert reason.endswith("…")


def _other_archive() -> bytes:
    """Return an archive of the repository of other_music, with the provider at its root."""
    return build_archive(
        {
            "other_music/manifest.json": json.dumps(manifest("other_music")).encode(),
            "other_music/__init__.py": b"",
        }
    )
