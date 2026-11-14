"""Tests for the fetch step, against a fake GitHub."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from registry.cli import main
from registry.fetch import fetch
from registry.files import load_model
from registry.github import GitHub
from registry.models import FetchReport
from registry.pipeline import FETCH_REPORT, archive_path
from registry.registry import load_registry
from tests.conftest import DOMAIN, FakeGitHub, provider_archive, release, request_json

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
    files.version("1.2.0", lines={"nightly": False, "beta": False})
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
    assert _downloads(github) == []


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
    files.version("1.1.0", commit=COMMIT_2, lines={"nightly": True})
    # results on both lines, and the latest on neither
    files.version("1.0.0", commit=COMMIT_1, lines={"nightly": True, "beta": False})
    github.repository(REPOSITORY, [release("v1.1.0", COMMIT_2)])
    github.archives[REPOSITORY, COMMIT_1] = provider_archive()
    github.archives[REPOSITORY, COMMIT_2] = provider_archive()

    _fetch(files, github)

    work = files.root / "work"
    assert archive_path(work, DOMAIN, "1.1.0").is_file()
    assert not archive_path(work, DOMAIN, "1.0.0").exists()


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
