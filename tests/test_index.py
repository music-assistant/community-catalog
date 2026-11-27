"""Tests for the index of each channel."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import orjson
import pytest

from registry.index import build_index, render_indexes, stale_index_files
from registry.registry import InvalidRegistryError, load_registry
from tests.conftest import (
    ADDED,
    CHECKED,
    COMMIT,
    CONTENT_HASH,
    DOMAIN,
    ICON,
    PASSED,
    RELEASED,
    manifest,
)

if TYPE_CHECKING:
    from tests.conftest import Files

NOW = datetime(2026, 11, 14, 8, 0, tzinfo=UTC)
# the fields of CatalogIndex, IndexProvider and IndexVersion in
# music_assistant/controllers/community_catalog/models.py of the server
SERVER_INDEX_FIELDS = {"schema", "channel", "generated", "providers"}
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


def _index(files: Files, channel: str = "nightly") -> dict[str, Any]:
    """Return the index of a channel as a server reads it."""
    return build_index(load_registry(files.root), channel, "2026-11-14T08:00:00Z").to_dict()


def test_index_has_the_shape_the_server_reads(files: Files) -> None:
    """The index has exactly the fields of the server's CatalogIndex, schema 1."""
    files.provider()
    files.version(reading=PASSED)

    index = _index(files)

    assert set(index) == SERVER_INDEX_FIELDS
    assert index["schema"] == 1
    assert index["channel"] == "nightly"
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


def test_maintainers_are_the_owner_and_the_code_owners(files: Files) -> None:
    """The owner and the code owners of the highest version, each once, on every channel."""
    requirements = ["some-package==1.0"]
    files.provider(repository="Someone/ma-sample")
    old = {**manifest(DOMAIN, requirements), "codeowners": ["@old"]}
    files.version("1.0.0", manifest=old, reading=PASSED)
    codeowners = ["@someone", "@another", "@Another", "@music-assistant/core", "@third"]
    newest = {**manifest(DOMAIN, requirements), "codeowners": codeowners}
    files.version("1.10.0", channels={"beta": True}, manifest=newest, reading=PASSED)

    expected = ["Someone", "another", "third"]
    assert _index(files)["providers"][DOMAIN]["maintainers"] == expected
    assert _index(files, "beta")["providers"][DOMAIN]["maintainers"] == expected


def test_code_owners_come_from_a_version_that_is_offered(files: Files) -> None:
    """A blocked, failed or unaccepted version names no maintainer; without others, the owner."""
    requirements = ["some-package==1.0"]

    def owners(*handles: str) -> dict[str, Any]:
        return {**manifest(DOMAIN, requirements), "codeowners": list(handles)}

    files.provider(blocked_versions={"9.0.0": "Sends the library elsewhere"})
    files.version("9.0.0", channels={"nightly": True}, manifest=owners("@victim"), reading=PASSED)
    held = {"verdict": "hold", "read": CHECKED}
    files.version("8.1.0", channels={"nightly": True}, manifest=owners("@held"), reading=held)
    files.version("8.0.1", channels={"nightly": True}, manifest=owners("@unread"))
    failed = {"nightly": False, "beta": False}
    files.version("8.0.0", channels=failed, manifest=owners("@another"), reading=PASSED)
    files.version("1.0.0", channels={"beta": True}, manifest=owners("@first"), reading=PASSED)

    assert _index(files)["providers"][DOMAIN]["maintainers"] == ["someone", "first"]
    files.version("1.0.0", channels={}, manifest=owners("@first"), reading=PASSED)
    assert _index(files)["providers"][DOMAIN]["maintainers"] == ["someone"]


def test_latest_is_the_highest_version_not_the_newest_text_or_date(files: Files) -> None:
    """Versions are compared as versions: 1.10.0 is above 1.9.0, also when released earlier."""
    files.provider()
    files.version("1.9.0", released="2026-11-10T00:00:00Z", reading=PASSED)
    files.version("1.10.0", released="2026-11-01T00:00:00Z", reading=PASSED)

    assert _index(files)["providers"][DOMAIN]["latest"] == "1.10.0"


def test_only_versions_that_passed_for_the_channel(files: Files) -> None:
    """A channel lists the versions that passed for it; the manifest and icon are of its latest."""
    files.provider()
    files.version("1.0.0", channels={"nightly": True, "beta": True}, reading=PASSED)
    files.version("2.0.0", channels={"nightly": True, "beta": False}, icon_svg=None, reading=PASSED)
    files.version("3.0.0", channels={}, reading=PASSED)

    nightly = _index(files, "nightly")["providers"][DOMAIN]
    beta = _index(files, "beta")["providers"][DOMAIN]

    assert (nightly["latest"], set(nightly["versions"])) == ("2.0.0", {"1.0.0", "2.0.0"})
    assert nightly["icon_svg"] is None
    assert (beta["latest"], set(beta["versions"])) == ("1.0.0", {"1.0.0"})
    assert beta["icon_svg"] == ICON


def test_the_five_highest_versions(files: Files) -> None:
    """A channel lists the five highest versions that passed for it."""
    files.provider()
    for minor in range(12):
        files.version(f"1.{minor}.0", reading=PASSED)

    versions = _index(files)["providers"][DOMAIN]["versions"]

    assert set(versions) == {"1.11.0", "1.10.0", "1.9.0", "1.8.0", "1.7.0"}


def test_a_blocked_version_is_never_latest(files: Files) -> None:
    """A blocked version is listed as blocked, not as a version to install."""
    files.provider(blocked_versions={"2.0.0": "Sends the library elsewhere"})
    files.version("1.0.0", reading=PASSED)
    files.version("2.0.0", reading=PASSED)

    provider = _index(files)["providers"][DOMAIN]

    assert provider["latest"] == "1.0.0"
    assert set(provider["versions"]) == {"1.0.0"}
    assert provider["blocked_versions"] == {"2.0.0": "Sends the library elsewhere"}


def test_only_accepted_versions_are_offered(files: Files) -> None:
    """A version the reading held or did not read is offered once a person clears it."""
    files.provider()
    files.version("1.0.0", reading=PASSED)
    files.version("1.1.0", reading={"verdict": "hold", "read": CHECKED})
    files.version("1.2.0")

    provider = _index(files)["providers"][DOMAIN]

    assert (provider["latest"], set(provider["versions"])) == ("1.0.0", {"1.0.0"})
    issue = "https://github.com/a/b/issues/1"
    files.provider(cleared_versions={"1.1.0": issue, "1.2.0": issue})
    provider = _index(files)["providers"][DOMAIN]
    assert (provider["latest"], set(provider["versions"])) == ("1.2.0", {"1.0.0", "1.1.0", "1.2.0"})


def test_a_pass_is_offered_while_its_base_is_accepted(files: Files) -> None:
    """A version read against a base that is blocked, or no longer cleared, is not offered."""
    issue = "https://github.com/a/b/issues/1"
    files.provider(cleared_versions={"1.0.0": issue})
    files.version("1.0.0", reading={"verdict": "hold", "read": CHECKED})
    files.version("1.1.0", reading={**PASSED, "base": "1.0.0"})
    files.version("1.2.0", reading={**PASSED, "base": "1.1.0"})

    assert _index(files)["providers"][DOMAIN]["latest"] == "1.2.0"
    files.provider(cleared_versions={"1.0.0": issue}, blocked_versions={"1.0.0": "x"})
    assert _index(files)["providers"][DOMAIN]["latest"] is None
    files.provider()
    assert _index(files)["providers"][DOMAIN]["latest"] is None


def test_blocked_and_withdrawn_providers_keep_their_versions(files: Files) -> None:
    """A blocked or withdrawn provider stays in the index with its versions and reason."""
    reason_url = "https://github.com/music-assistant/community-catalog/issues/1"
    files.provider(status="blocked", reason="Sends the library elsewhere", reason_url=reason_url)
    files.version(reading=PASSED)
    files.provider("other_music", status="withdrawn", reason="No longer maintained")
    files.version(domain="other_music", reading=PASSED)

    providers = _index(files)["providers"]

    assert providers[DOMAIN]["status"] == "blocked"
    assert providers[DOMAIN]["reason"] == "Sends the library elsewhere"
    assert providers[DOMAIN]["reason_url"] == reason_url
    assert providers[DOMAIN]["latest"] == "1.0.0"
    assert set(providers[DOMAIN]["versions"]) == {"1.0.0"}
    assert providers["other_music"]["status"] == "withdrawn"
    assert providers["other_music"]["latest"] == "1.0.0"


def test_a_provider_without_a_version_for_the_channel(files: Files) -> None:
    """A provider without a version for a channel is shown by its newest version, with no latest."""
    files.provider()
    files.version("1.0.0", channels={"beta": True}, reading=PASSED)
    changes: dict[str, Any] = {"manifest": manifest(), "requirements": [], "reading": PASSED}
    files.version("2.0.0", channels={"beta": False}, **changes)

    provider = _index(files)["providers"][DOMAIN]

    assert provider["latest"] is None
    assert provider["versions"] == {}
    assert provider["manifest"] == manifest()


def test_a_provider_without_a_version_for_the_channel_is_not_shown_by_a_blocked_one(
    files: Files,
) -> None:
    """The newest accepted version shows the provider; the newest when none is accepted."""
    files.provider(blocked_versions={"2.0.0": "Sends the library elsewhere"})
    files.version("1.0.0", channels={"beta": True}, reading=PASSED)
    changes: dict[str, Any] = {"manifest": manifest(), "requirements": [], "reading": PASSED}
    files.version("2.0.0", channels={"nightly": False}, **changes)
    unread = {**manifest(), "name": "Unread"}
    files.version("3.0.0", channels={"nightly": False}, manifest=unread, requirements=[])

    provider = _index(files)["providers"][DOMAIN]

    assert provider["latest"] is None
    assert provider["manifest"] == manifest(DOMAIN, ["some-package==1.0"])
    files.provider(blocked_versions={"1.0.0": "x", "2.0.0": "x"})
    assert _index(files)["providers"][DOMAIN]["manifest"] == unread


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
    """An index whose providers change is generated anew; the index of another channel is not."""
    files.provider()
    files.version("1.0.0", channels={"nightly": True, "beta": True}, reading=PASSED)
    for path, content in render_indexes(load_registry(files.root), NOW).items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    files.version("1.1.0", channels={"nightly": True}, reading=PASSED)
    registry = load_registry(files.root)

    later = render_indexes(registry, NOW + timedelta(days=1))

    nightly = json.loads(later[files.root / "site/v1/nightly.json"])
    beta = json.loads(later[files.root / "site/v1/beta.json"])
    assert nightly["generated"] == "2026-11-15T08:00:00Z"
    assert nightly["providers"][DOMAIN]["latest"] == "1.1.0"
    assert beta["generated"] == "2026-11-14T08:00:00Z"
    assert stale_index_files(registry, later) == ["site/v1/nightly.json: differs from the registry"]


def test_missing_and_unexpected_index_files(files: Files) -> None:
    """Every channel has its index file, and the published folder holds nothing else."""
    files.write("site/v1/2.10.json", "{}")
    files.write("site/v1/.hidden", "x")
    files.write("site/index.html", "x")
    files.write("site/v2/nightly.json", "{}")
    (files.root / "site/linked").symlink_to(files.root, target_is_directory=True)
    registry = load_registry(files.root)

    assert stale_index_files(registry, render_indexes(registry, NOW)) == [
        "site/v1/beta.json: is missing",
        "site/v1/nightly.json: is missing",
        "site/index.html: is no index of a channel in channels.json",
        "site/linked: is no index of a channel in channels.json",
        "site/v1/.hidden: is no index of a channel in channels.json",
        "site/v1/2.10.json: is no index of a channel in channels.json",
        "site/v2: is no index of a channel in channels.json",
        "site/v2/nightly.json: is no index of a channel in channels.json",
    ]


def test_every_index_is_read_by_the_server_parser(files: Files) -> None:
    """Each index the registry can generate reads the same with orjson, the server's parser."""
    text = 'Ünïcødé 🎵 \u2028 \x00 \\ " </script>'
    files.provider(blocked_versions={"1.2.0": text})
    for version, channels in {
        "1.0.0": {"nightly": True, "beta": True},
        "1.1.0": {"beta": False},
        "1.2.0": {"nightly": True},
        "1.3.0": {},
    }.items():
        data = {**manifest(), "name": text, "nested": [{"a": 1.5, "b": None, "c": 2**63}]}
        files.version(version, channels=channels, manifest=data, requirements=[])
    files.provider("withdrawn_one", status="withdrawn", reason=text)
    files.version(domain="withdrawn_one", icon_svg=None)
    files.provider(
        "blocked_one",
        status="blocked",
        reason="x",
        reason_url="https://x.org/1",
        blocked_versions={"1.0.0": "x"},
    )
    files.version(domain="blocked_one", channels={"beta": False})

    for path, content in render_indexes(load_registry(files.root), NOW).items():
        assert orjson.loads(content) == json.loads(content), path.name


def test_an_index_the_server_cannot_read_is_refused(files: Files) -> None:
    """An index is only rendered when it is plain JSON the server's parser reads."""
    files.provider()
    files.version()
    registry = load_registry(files.root)
    registry.providers[DOMAIN].versions["1.0.0"].manifest["extra"] = float("nan")

    with pytest.raises(InvalidRegistryError, match=r"site/v1/beta\.json: not plain JSON"):
        render_indexes(registry, NOW)
