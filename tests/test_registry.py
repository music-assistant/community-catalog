"""Tests for reading and checking the files of the registry."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest

from registry.models import Verdict, VersionFile, VersionReading
from registry.registry import InvalidRegistryError, load_registry
from tests.conftest import DOMAIN, NIGHTLY_IMAGE, ROOT, manifest, provider_file, version_file

if TYPE_CHECKING:
    from tests.conftest import Files

PROVIDER = f"providers/{DOMAIN}/provider.json"
VERSION = f"providers/{DOMAIN}/versions/1.0.0.json"
ISSUE = "https://github.com/music-assistant/community-catalog/issues/31"
READ = "2026-11-03T03:00:00Z"


def _problems(files: Files) -> list[str]:
    """Return the problems the registry has, failing when it has none."""
    with pytest.raises(InvalidRegistryError) as err:
        load_registry(files.root)
    return err.value.problems


def test_repository_itself_is_valid() -> None:
    """The files of this repository are valid."""
    registry = load_registry(ROOT)

    assert set(registry.channels) >= {"nightly", "beta"}


def test_valid_registry(files: Files) -> None:
    """A registry of valid files loads, with every provider and version."""
    files.provider()
    files.version("1.0.0")
    files.version("1.1.0", tag="v1.1.0")
    files.provider("moving_one", folder="moving_one")

    registry = load_registry(files.root)

    assert set(registry.providers) == {DOMAIN, "moving_one"}
    assert set(registry.providers[DOMAIN].versions) == {"1.0.0", "1.1.0"}


def test_registry_without_providers_folder(files: Files) -> None:
    """A registry without the providers folder has no providers."""
    assert load_registry(files.root).providers == {}


@pytest.mark.parametrize(
    ("file", "problem"),
    [
        ("channels.json", "channels.json: is missing"),
        ("reserved_domains.json", "reserved_domains.json: is missing"),
    ],
)
def test_missing_file(files: Files, file: str, problem: str) -> None:
    """The channels and the reserved domains are needed."""
    (files.root / file).unlink()

    assert _problems(files) == [problem]


@pytest.mark.parametrize(
    ("channels", "problem"),
    [
        ({"channels": {"stable": {"image": NIGHTLY_IMAGE}}}, "'stable' is not a channel"),
        ({"channels": {}}, "there is no channel"),
        (
            {"channels": {"nightly": {"image": NIGHTLY_IMAGE, "frozen": "no"}}},
            "channels.frozen: expected bool, got str",
        ),
        ({"channels": {"nightly": {}}}, "channels.image: is missing"),
        (
            {"channels": {"nightly": {"image": "--privileged"}}},
            "channels.nightly.image: '--privileged' is not a container image with a tag",
        ),
        (
            {"channels": {"nightly": {"image": "ghcr.io/music-assistant/server"}}},
            "is not a container image with a tag",
        ),
    ],
)
def test_channels(files: Files, channels: dict[str, Any], problem: str) -> None:
    """Every channel is nightly, beta or MAJOR.MINOR, and there is at least one."""
    files.write("channels.json", channels)

    [found] = _problems(files)
    assert found.startswith("channels.json: ")
    assert problem in found


@pytest.mark.parametrize(
    ("content", "problem"),
    [
        ("{", "not valid JSON"),
        ("[]", "not a JSON object"),
        ('{"domain": "a", "domain": "b"}', "the key 'domain' appears twice"),
        (b"\xff", "not UTF-8"),
    ],
)
def test_unreadable_provider_file(files: Files, content: str | bytes, problem: str) -> None:
    """A provider.json that is not one JSON object with unique keys is refused."""
    path = files.root / PROVIDER
    path.parent.mkdir(parents=True)
    path.write_bytes(content if isinstance(content, bytes) else content.encode())

    [found] = _problems(files)
    assert found.startswith(f"{PROVIDER}: ")
    assert problem in found


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity", "1e400", '"\\ud800"'])
def test_json_the_server_refuses(files: Files, value: str) -> None:
    """A file holds plain JSON, which the server's parser reads: not what only json accepts."""
    files.provider()
    content = json.dumps(version_file(manifest={**manifest(), "extra": "X"}))
    files.write(VERSION, content.replace('"X"', value))

    [found] = _problems(files)
    assert found.startswith(f"{VERSION}: not valid JSON: ")


def test_a_provider_file_without_its_optional_keys(files: Files) -> None:
    """A provider.json without a status is listed, without a tag prefix, reason or blocks."""
    files.provider(status=None)

    file = load_registry(files.root).providers[DOMAIN].file

    assert (file.status, file.tag_prefix, file.reason, file.blocked_versions) == (
        "listed",
        "",
        None,
        {},
    )


@pytest.mark.parametrize(
    ("changes", "problem"),
    [
        ({"added": None}, "added: is missing"),
        ({"repository_id": None}, "repository_id: is missing"),
        ({"owner_id": "201"}, "owner_id: expected int, got str"),
        ({"homepage": "x"}, "unknown key homepage"),
        ({"maintainers": ["someone"]}, "unknown key maintainers"),
        ({"blocked_versions": ["1.0.0"]}, "blocked_versions"),
        ({"status": "hidden"}, "status: 'hidden' is not a valid ProviderStatus"),
    ],
)
def test_provider_file_shape(files: Files, changes: dict[str, Any], problem: str) -> None:
    """A provider.json has exactly the declared keys, each of its declared type."""
    files.provider(**changes)

    [found] = _problems(files)
    assert found.startswith(f"{PROVIDER}: ")
    assert problem in found


@pytest.mark.parametrize(
    ("domain", "changes", "problem"),
    [
        ("Sample", {}, "domain: 'Sample' is not a provider domain"),
        ("builtin_one", {}, "domain: builtin_one is a provider the server ships"),
        ("players", {}, "domain: players is a core controller of the server"),
        ("library", {}, "domain: library is a name the server reserves"),
        (DOMAIN, {"repository": "someone"}, "repository: 'someone' is not a GitHub owner/name"),
        (
            DOMAIN,
            {"folder": "src"},
            (
                "folder: 'src' is not a relative path in the repository whose last folder is named "
                "sample_music or provider"
            ),
        ),
        (DOMAIN, {"tag_prefix": "a b"}, "tag_prefix: 'a b' is not allowed in a tag"),
        (DOMAIN, {"repository_id": 0}, "repository_id: 0 is not an id GitHub gives"),
        (DOMAIN, {"owner_id": -1}, "owner_id: -1 is not an id GitHub gives"),
        (DOMAIN, {"reason": "x"}, "reason: a listed provider has no reason or reason_url"),
        (DOMAIN, {"status": "blocked"}, "reason: a blocked provider needs a reason"),
        (
            DOMAIN,
            {"status": "withdrawn", "reason": " "},
            "reason: a withdrawn provider needs a reason",
        ),
        (
            DOMAIN,
            {"status": "blocked", "reason": "x", "reason_url": "http://x"},
            "reason_url: 'http://x' is not an https address",
        ),
        (DOMAIN, {"added": "2026-10-01"}, "added: '2026-10-01' is not a date in UTC"),
        (DOMAIN, {"blocked_versions": {"1.0": "x"}}, "blocked_versions: '1.0' is not a version"),
        (DOMAIN, {"cleared_versions": {"1.0": ISSUE}}, "cleared_versions: '1.0' is not a version"),
        (
            DOMAIN,
            {"cleared_versions": {"1.0.0": "http://x"}},
            "cleared_versions: 1.0.0: 'http://x' is not an https address",
        ),
        (DOMAIN, {"cleared_versions": {"1.0.0": ""}}, "cleared_versions: 1.0.0: '' is not an"),
        (
            DOMAIN,
            {"status": "blocked", "reason": "x", "reason_url": "https://[::1"},
            "reason_url: 'https://[::1' is not an https address",
        ),
    ],
)
def test_provider_file_rules(
    files: Files, domain: str, changes: dict[str, Any], problem: str
) -> None:
    """Each value of a provider.json follows its rule."""
    files.write(
        f"providers/{domain}/provider.json", provider_file(domain, **{"folder": domain, **changes})
    )

    problems = _problems(files)

    assert f"providers/{domain}/provider.json: {problem}" in "\n".join(problems)


def test_domain_must_be_the_folder_name(files: Files) -> None:
    """The domain in provider.json is the name of its folder."""
    files.write(f"providers/{DOMAIN}/provider.json", provider_file("other_music"))

    assert f"{PROVIDER}: domain: other_music is not {DOMAIN}" in _problems(files)


def test_a_provider_that_moves_out_of_the_server_takes_its_domain(files: Files) -> None:
    """A built-in domain listed as moving can be admitted; any other built-in domain cannot."""
    files.provider("moving_one", folder="moving_one")

    assert set(load_registry(files.root).providers) == {"moving_one"}


def test_unexpected_files(files: Files) -> None:
    """A provider folder holds only provider.json and versions, versions only version files."""
    files.provider()
    files.write(f"providers/{DOMAIN}/README.md", "x")
    files.write(f"providers/{DOMAIN}/versions/notes.txt", "x")
    files.write("providers/stray.json", "{}")
    files.write(f"providers/{DOMAIN}/versions/.DS_Store", "")

    assert _problems(files) == [
        f"providers/{DOMAIN}/README.md: is not a file of the registry",
        f"providers/{DOMAIN}/versions/notes.txt: is not a version file",
        "providers/stray.json: is not a provider folder",
    ]


def test_provider_folder_without_provider_file(files: Files) -> None:
    """A provider folder needs a provider.json."""
    files.version()

    assert _problems(files) == [f"{PROVIDER}: is missing"]


@pytest.mark.parametrize(
    ("changes", "problem"),
    [
        ({"commit": None}, "commit: is missing"),
        (
            {"checks": {"nightly": {"passed": "yes", "checked": "x"}}},
            "checks.passed: expected bool",
        ),
        ({"tag": "1.0"}, "tag: '1.0' is not one of 1.0.0, v1.0.0"),
        ({"commit": "0123abcd"}, "commit: '0123abcd' is not a full commit SHA"),
        ({"commit": "0123456789ABCDEF0123456789ABCDEF01234567"}, "commit: '0123456789ABCDEF"),
        ({"content_hash": "e3b0c442"}, "content_hash: 'e3b0c442' is not a sha256 hex digest"),
        ({"folder": "providers/other"}, "folder: 'providers/other' is not a relative path"),
        ({"manifest": manifest("other_music")}, "manifest.domain: 'other_music' is not"),
        ({"manifest": {**manifest(), "type": "core"}}, "manifest.type: 'core' is not one of"),
        ({"manifest": {**manifest(), "name": ""}}, "manifest.name: is missing or empty"),
        ({"manifest": {**manifest(), "codeowners": "@x"}}, "manifest.codeowners: is not a list"),
        (
            {"manifest": {**manifest(), "codeowners": [f"@m{n}" for n in range(11)]}},
            "manifest.codeowners: more than 10",
        ),
        (
            {"manifest": {**manifest(), "description": "x" * 16_384}},
            "manifest: 16507 bytes as JSON, more than 16384",
        ),
        (
            {
                "manifest": manifest(requirements=[f"package-{n}==1.0" for n in range(51)]),
                "requirements": [f"package-{n}==1.0" for n in range(51)],
            },
            "manifest.requirements: more than 50",
        ),
        (
            {
                "manifest": manifest(requirements=["p" * 96 + "==1.0"]),
                "requirements": ["p" * 96 + "==1.0"],
            },
            "manifest.requirements: one is longer than 100 characters",
        ),
        (
            {
                "manifest": manifest(requirements=["x @ https://x"]),
                "requirements": ["x @ https://x"],
            },
            "requirements: 'x @ https://x' is not a package name with version specifiers",
        ),
        ({"requirements": []}, "requirements: differ from the requirements in the manifest"),
        ({"released": "2026-11-01T12:00:00+01:00"}, "released: '2026-11-01T12:00:00+01:00'"),
        ({"icon_svg": "<svg/>"}, "icon_svg: its root element is not an SVG element"),
        (
            {"checks": {"2.10": {"passed": True, "checked": "2026-11-02T03:00:00Z"}}},
            "checks: '2.10' is not a channel in channels.json",
        ),
        (
            {"checks": {"nightly": {"passed": False, "checked": "2026-11-02T03:00:00Z"}}},
            "checks.nightly: a version that did not pass needs a reason",
        ),
        (
            {
                "checks": {
                    "nightly": {"passed": True, "checked": "2026-11-02T03:00:00Z", "reason": "x"}
                }
            },
            "checks.nightly: a version that passed has no reason",
        ),
        (
            {"checks": {"nightly": {"passed": True, "checked": "today"}}},
            "checks.nightly.checked: 'today' is not a date",
        ),
        (
            {
                "checks": {
                    "nightly": {
                        "passed": False,
                        "checked": "2026-11-02T03:00:00Z",
                        "reason": "load: " + "x" * 1_000_000,
                    }
                }
            },
            "checks.nightly.reason: is not one line of at most 300 printable characters",
        ),
        (
            {"reading": {"verdict": "maybe", "read": READ}},
            "reading.verdict: 'maybe' is not a valid Verdict",
        ),
        ({"reading": {"verdict": "pass"}}, "reading.read: is missing"),
        ({"reading": {"verdict": "pass", "read": READ, "by": "x"}}, "reading: unknown key by"),
        ({"reading": {"verdict": "pass", "read": "today"}}, "reading.read: 'today' is not a date"),
        (
            {"reading": {"verdict": "pass", "read": READ, "base": "1.0.0"}},
            "reading.base: '1.0.0' is not a version below 1.0.0",
        ),
        (
            {"reading": {"verdict": "pass", "read": READ, "base": "1.0"}},
            "reading.base: '1.0' is not a version below 1.0.0",
        ),
    ],
)
def test_version_file_rules(files: Files, changes: dict[str, Any], problem: str) -> None:
    """Each value of a version file follows its rule."""
    files.provider()
    files.version(**changes)

    problems = _problems(files)

    assert f"{VERSION}: {problem}" in "\n".join(problems)


def test_version_must_be_the_file_name(files: Files) -> None:
    """The version in a version file is the name of the file, so a version appears once."""
    files.provider()
    files.write(VERSION, version_file("1.0.1", tag="v1.0.1"))

    assert _problems(files) == [f"{VERSION}: version: 1.0.1 is not 1.0.0"]


def test_version_is_major_minor_patch(files: Files) -> None:
    """A version with another spelling, which could name a version twice, is refused."""
    files.provider()
    files.write(f"providers/{DOMAIN}/versions/1.0.json", version_file("1.0", tag="v1.0"))

    assert f"providers/{DOMAIN}/versions/1.0.json: version: '1.0' is not a version" in "\n".join(
        _problems(files)
    )


def test_tag_with_prefix(files: Files) -> None:
    """With a tag prefix, the tag is the prefix and the version, with or without v."""
    files.provider(tag_prefix="sample-")
    files.version("1.0.0", tag="sample-1.0.0")
    files.version("1.1.0", tag="sample-v1.1.0")
    files.version("1.2.0", tag="v1.2.0")

    assert _problems(files) == [
        (
            f"providers/{DOMAIN}/versions/1.2.0.json: tag: 'v1.2.0' is not one of sample-1.2.0, "
            "sample-v1.2.0"
        )
    ]


def test_a_version_with_its_reading(files: Files) -> None:
    """A version keeps the reading of its change against a lower version."""
    reading = {"verdict": "hold", "read": READ, "base": "1.0.0"}
    files.provider(cleared_versions={"1.1.0": ISSUE})
    files.version("1.0.0", reading={"verdict": "pass", "read": READ})
    files.version("1.1.0", reading=reading)

    versions = load_registry(files.root).providers[DOMAIN].versions

    assert versions["1.0.0"].reading == VersionReading(Verdict.PASS, READ)
    assert versions["1.1.0"].reading == VersionReading(Verdict.HOLD, READ, "1.0.0")


def test_a_version_file_without_a_reading_has_no_reading_key() -> None:
    """A version that is not read yet is written without the key, and with it once it is read."""
    unread = VersionFile.from_dict(version_file())
    read = VersionFile.from_dict(version_file(reading={"verdict": "pass", "read": READ}))

    assert "reading" not in unread.to_dict()
    assert read.to_dict()["reading"] == {"verdict": "pass", "read": READ, "base": None}


def test_the_base_of_a_reading_needs_a_version_file(files: Files) -> None:
    """The version a change was read against is one of the versions of the provider."""
    files.provider()
    files.version("1.1.0", reading={"verdict": "pass", "read": READ, "base": "1.0.0"})

    assert _problems(files) == [
        f"providers/{DOMAIN}/versions/1.1.0.json: reading.base: 1.0.0 has no version file"
    ]


def test_the_accepted_versions(files: Files) -> None:
    """
    A version is accepted when it is cleared, or passed in full or against an accepted base.

    A blocked version never is, and a pass on a base that is not accepted (any more) is valid.
    """
    held = {"verdict": "hold", "read": READ}
    passed = {"verdict": "pass", "read": READ}
    files.provider(
        cleared_versions={"1.1.0": ISSUE, "2.0.0": ISSUE}, blocked_versions={"2.0.0": "x"}
    )
    files.version("1.0.0", reading=passed)
    files.version("1.1.0", reading=held)
    files.version("1.2.0", reading={**passed, "base": "1.1.0"})
    files.version("1.3.0")
    files.version("1.4.0", reading={**passed, "base": "1.3.0"})
    files.version("1.5.0", reading={**passed, "base": "1.4.0"})
    files.version("2.0.0", reading=held)
    files.version("2.1.0", reading={**passed, "base": "2.0.0"})

    assert load_registry(files.root).providers[DOMAIN].accepted() == {"1.0.0", "1.1.0", "1.2.0"}


@pytest.mark.parametrize("reading", [None, {"verdict": "hold", "read": READ}])
def test_a_held_or_pending_version_may_be_cleared(
    files: Files, reading: dict[str, str] | None
) -> None:
    """A person may clear a version the reading held or did not read yet."""
    files.provider(cleared_versions={"1.0.0": ISSUE})
    files.version("1.0.0", reading=reading)

    assert load_registry(files.root).providers[DOMAIN].file.cleared_versions == {"1.0.0": ISSUE}


def test_a_version_that_passed_is_not_cleared(files: Files) -> None:
    """A version the reading passed needs no clearance, so one is refused."""
    files.provider(cleared_versions={"1.0.0": ISSUE})
    files.version("1.0.0", reading={"verdict": "pass", "read": READ})

    assert _problems(files) == [
        f"{PROVIDER}: cleared_versions: 1.0.0 passed the reading, so it needs no clearance"
    ]


def test_a_cleared_version_needs_a_version_file(files: Files) -> None:
    """A cleared version is one of the versions of the provider."""
    files.provider(cleared_versions={"1.0.0": ISSUE, "2.0.0": ISSUE})
    files.version("1.0.0")

    assert _problems(files) == [f"{PROVIDER}: cleared_versions: 2.0.0 has no version file"]


def test_blocked_version_needs_a_version_file(files: Files) -> None:
    """A blocked version is one of the versions of the provider."""
    files.provider(blocked_versions={"1.0.0": "Sends data elsewhere", "2.0.0": "x"})
    files.version("1.0.0")

    assert _problems(files) == [f"{PROVIDER}: blocked_versions: 2.0.0 has no version file"]


def test_every_problem_is_reported(files: Files) -> None:
    """All problems of all files are reported at once, each with its file."""
    files.provider(repository="x")
    files.version(commit="x")
    files.write("providers/other_music/provider.json", json.dumps({}))

    problems = _problems(files)

    assert any(p.startswith(f"{PROVIDER}: repository") for p in problems)
    assert any(p.startswith(f"{VERSION}: commit") for p in problems)
    assert any(p.startswith("providers/other_music/provider.json: ") for p in problems)


def _refused(version: str = "2.0.0", **changes: Any) -> dict[str, Any]:
    """Return the content of a valid file of a refused release."""
    data = {
        "version": version,
        "tag": f"v{version}",
        "commit": "1" * 40,
        "released": "2026-11-01T12:00:00Z",
        "refused": "2026-11-02T03:00:00Z",
        "reason": "the repository has no folder providers/sample_music",
    }
    return {**data, **changes}


def test_refused_releases(files: Files) -> None:
    """A refused release is read with its provider."""
    files.provider()
    files.write(f"providers/{DOMAIN}/refused/2.0.0.json", _refused())

    provider = load_registry(files.root).providers[DOMAIN]

    assert provider.refused["2.0.0"].reason == "the repository has no folder providers/sample_music"


@pytest.mark.parametrize(
    ("name", "content", "problem"),
    [
        ("1.0.0", _refused("1.0.0"), "version: 1.0.0 has a version file as well"),
        ("2.0.0", _refused(tag="v2.0.1"), "tag: 'v2.0.1' does not name the version 2.0.0"),
        ("2.0.0", _refused(commit="x"), "commit: 'x' is not a full commit SHA"),
        ("2.0.0", _refused(reason=" "), "reason: a refused release needs a reason"),
        ("2.0.0", _refused(reason="x" * 2001), "reason: is longer than 2000 characters"),
        ("2.0.0", _refused(refused="today"), "refused: 'today' is not a date"),
        ("2.0.1", _refused(), "version: 2.0.0 is not 2.0.1"),
    ],
)
def test_refused_release_rules(
    files: Files, name: str, content: dict[str, Any], problem: str
) -> None:
    """A refused release names a version without a version file, with its tag, commit and reason."""
    files.provider()
    files.version("1.0.0")
    files.write(f"providers/{DOMAIN}/refused/{name}.json", content)

    [found] = _problems(files)
    assert found.startswith(f"providers/{DOMAIN}/refused/{name}.json: ")
    assert problem in found
