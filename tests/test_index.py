"""Tests for the index of each server line."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from registry.index import build_index, render_indexes, stale_index_files
from registry.registry import load_registry
from tests.conftest import ADDED, COMMIT, CONTENT_HASH, DOMAIN, ICON, RELEASED, manifest

if TYPE_CHECKING:
    from tests.conftest import Files

NOW = datetime(2026, 11, 14, 8, 0, tzinfo=UTC)
# the fields of CatalogIndex, IndexProvider and IndexVersion in
# music_assistant/controllers/community_catalog/models.py of the server
SERVER_INDEX_FIELDS = {"schema", "server_line", "generated", "providers"}
SERVER_PROVIDER_FIELDS = {
    "status",
    "repository",
    "manifest",
    "added",
    "reason",
    "reason_url",
    "maintainers",
    "icon_svg",
    "latest",
    "versions",
    "blocked_versions",
}
SERVER_VERSION_FIELDS = {"commit", "folder", "content_hash", "requirements", "released"}


def _index(files: Files, line: str = "nightly") -> dict[str, Any]:
    """Return the index of a line as a server reads it."""
    return build_index(load_registry(files.root), line, "2026-11-14T08:00:00Z").to_dict()


def test_index_has_the_shape_the_server_reads(files: Files) -> None:
    """The index has exactly the fields of the server's CatalogIndex, schema 1."""
    files.provider()
    files.version()

    index = _index(files)

    assert set(index) == SERVER_INDEX_FIELDS
    assert index["schema"] == 1
    assert index["server_line"] == "nightly"
    provider = index["providers"][DOMAIN]
    assert set(provider) == SERVER_PROVIDER_FIELDS
    assert set(provider["versions"]["1.0.0"]) == SERVER_VERSION_FIELDS
    assert provider == {
        "status": "listed",
        "repository": "someone/ma-sample",
        "manifest": manifest(DOMAIN, ["some-package==1.0"]),
        "added": ADDED,
        "reason": None,
        "reason_url": None,
        "maintainers": ["someone"],
        "icon_svg": ICON,
        "latest": "1.0.0",
        "versions": {
            "1.0.0": {
                "commit": COMMIT,
                "folder": f"providers/{DOMAIN}",
                "content_hash": CONTENT_HASH,
                "requirements": ["some-package==1.0"],
                "released": RELEASED,
            }
        },
        "blocked_versions": {},
    }


def test_latest_is_the_highest_version_not_the_newest_text_or_date(files: Files) -> None:
    """Versions are compared as versions: 1.10.0 is above 1.9.0, also when released earlier."""
    files.provider()
    files.version("1.9.0", released="2026-11-10T00:00:00Z")
    files.version("1.10.0", released="2026-11-01T00:00:00Z")

    assert _index(files)["providers"][DOMAIN]["latest"] == "1.10.0"


def test_only_versions_that_passed_for_the_line(files: Files) -> None:
    """A line lists the versions that passed for it; the manifest and icon are of its latest."""
    files.provider()
    files.version("1.0.0", lines={"nightly": True, "beta": True})
    files.version("2.0.0", lines={"nightly": True, "beta": False}, icon_svg=None)
    files.version("3.0.0", lines={})

    nightly = _index(files, "nightly")["providers"][DOMAIN]
    beta = _index(files, "beta")["providers"][DOMAIN]

    assert (nightly["latest"], set(nightly["versions"])) == ("2.0.0", {"1.0.0", "2.0.0"})
    assert nightly["icon_svg"] is None
    assert (beta["latest"], set(beta["versions"])) == ("1.0.0", {"1.0.0"})
    assert beta["icon_svg"] == ICON


def test_the_five_highest_versions(files: Files) -> None:
    """A line lists the five highest versions that passed for it."""
    files.provider()
    for minor in range(12):
        files.version(f"1.{minor}.0")

    versions = _index(files)["providers"][DOMAIN]["versions"]

    assert set(versions) == {"1.11.0", "1.10.0", "1.9.0", "1.8.0", "1.7.0"}


def test_a_blocked_version_is_never_latest(files: Files) -> None:
    """A blocked version is listed as blocked, not as a version to install."""
    files.provider(blocked_versions={"2.0.0": "Sends the library elsewhere"})
    files.version("1.0.0")
    files.version("2.0.0")

    provider = _index(files)["providers"][DOMAIN]

    assert provider["latest"] == "1.0.0"
    assert set(provider["versions"]) == {"1.0.0"}
    assert provider["blocked_versions"] == {"2.0.0": "Sends the library elsewhere"}


def test_blocked_and_withdrawn_providers_keep_their_versions(files: Files) -> None:
    """A blocked or withdrawn provider stays in the index with its versions and reason."""
    reason_url = "https://github.com/music-assistant/community-catalog/issues/1"
    files.provider(status="blocked", reason="Sends the library elsewhere", reason_url=reason_url)
    files.version()
    files.provider("other_music", status="withdrawn", reason="No longer maintained")
    files.version(domain="other_music")

    providers = _index(files)["providers"]

    assert providers[DOMAIN]["status"] == "blocked"
    assert providers[DOMAIN]["reason"] == "Sends the library elsewhere"
    assert providers[DOMAIN]["reason_url"] == reason_url
    assert providers[DOMAIN]["latest"] == "1.0.0"
    assert set(providers[DOMAIN]["versions"]) == {"1.0.0"}
    assert providers["other_music"]["status"] == "withdrawn"
    assert providers["other_music"]["latest"] == "1.0.0"


def test_a_provider_without_a_version_for_the_line(files: Files) -> None:
    """A provider without a version for a line is shown by its newest version, with no latest."""
    files.provider()
    files.version("1.0.0", lines={"beta": True})
    files.version("2.0.0", lines={"beta": False}, manifest=manifest(), requirements=[])

    provider = _index(files)["providers"][DOMAIN]

    assert provider["latest"] is None
    assert provider["versions"] == {}
    assert provider["manifest"] == manifest()


def test_a_provider_without_any_version_is_left_out(files: Files) -> None:
    """A provider that has no version file yet is not in the index."""
    files.provider()

    assert _index(files)["providers"] == {}


def test_indexes_are_deterministic(files: Files) -> None:
    """An unchanged registry gives byte-identical files, also at a later moment."""
    files.provider()
    files.version("1.0.0")
    registry = load_registry(files.root)
    first = render_indexes(registry, NOW)
    for path, content in first.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)

    later = render_indexes(load_registry(files.root), NOW + timedelta(days=1))

    assert later == first
    assert stale_index_files(registry, later) == []
    nightly = json.loads(first[files.root / "site/v1/nightly.json"])
    assert nightly["generated"] == "2026-11-14T08:00:00Z"
    assert list(nightly) == sorted(nightly)


def test_a_changed_index_gets_a_new_date(files: Files) -> None:
    """An index whose providers change is generated anew; the index of another line is not."""
    files.provider()
    files.version("1.0.0", lines={"nightly": True, "beta": True})
    for path, content in render_indexes(load_registry(files.root), NOW).items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    files.version("1.1.0", lines={"nightly": True})
    registry = load_registry(files.root)

    later = render_indexes(registry, NOW + timedelta(days=1))

    nightly = json.loads(later[files.root / "site/v1/nightly.json"])
    beta = json.loads(later[files.root / "site/v1/beta.json"])
    assert nightly["generated"] == "2026-11-15T08:00:00Z"
    assert nightly["providers"][DOMAIN]["latest"] == "1.1.0"
    assert beta["generated"] == "2026-11-14T08:00:00Z"
    assert stale_index_files(registry, later) == ["site/v1/nightly.json: differs from the registry"]


def test_missing_and_unexpected_index_files(files: Files) -> None:
    """Every line has its index file, and the published folder holds no other file."""
    files.write("site/v1/2.10.json", "{}")
    registry = load_registry(files.root)

    assert stale_index_files(registry, render_indexes(registry, NOW)) == [
        "site/v1/beta.json: is missing",
        "site/v1/nightly.json: is missing",
        "site/v1/2.10.json: is no index of a line in server_lines.json",
    ]
