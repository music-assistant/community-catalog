"""Tests for the formats of the values in the files of the registry."""

from __future__ import annotations

import pytest

from registry import formats


@pytest.mark.parametrize(
    ("value", "valid"),
    [
        ("sample_music", True),
        ("abc2", True),
        ("Sample", False),
        ("_sample", False),
        ("2sample", False),
        ("sample-music", False),
        ("sample.music", False),
        ("ßample", False),
        ("import", False),
        ("", False),
    ],
)
def test_domain(value: str, valid: bool) -> None:
    """A domain is a lower case Python identifier that starts with a letter and is no keyword."""
    assert formats.is_domain(value) is valid


@pytest.mark.parametrize(
    ("value", "valid"),
    [
        ("1.2.3", True),
        ("0.0.0", True),
        ("10.20.30", True),
        ("1.2", False),
        ("1.2.3.4", False),
        ("v1.2.3", False),
        ("01.2.3", False),
        ("1.2.3b1", False),
        ("1.2.3-rc.1", False),
    ],
)
def test_version(value: str, valid: bool) -> None:
    """A version is MAJOR.MINOR.PATCH without leading zeros, so each version has one spelling."""
    assert formats.is_version(value) is valid


def test_versions_are_ordered_as_versions() -> None:
    """Versions are ordered by their numbers, not as text."""
    versions = ["1.10.0", "1.9.0", "2.0.0", "1.9.10", "1.9.2"]

    assert sorted(versions, key=formats.version_key) == [
        "1.9.0",
        "1.9.2",
        "1.9.10",
        "1.10.0",
        "2.0.0",
    ]


@pytest.mark.parametrize(
    ("value", "valid"),
    [("nightly", True), ("beta", True), ("2.11", True), ("stable", False), ("2.11.0", False)],
)
def test_channel(value: str, valid: bool) -> None:
    """A channel is nightly, beta or MAJOR.MINOR."""
    assert formats.is_channel(value) is valid


@pytest.mark.parametrize(
    ("value", "valid"),
    [
        ("2026-11-14T08:00:00Z", True),
        ("2026-11-14T08:00:00+00:00", False),
        ("2026-11-14T08:00:00", False),
        ("2026-11-14", False),
        ("2026-02-30T08:00:00Z", False),
        ("2026-11-14T08:00:00.123Z", False),
    ],
)
def test_date(value: str, valid: bool) -> None:
    """A date is ISO 8601 in UTC, to the second, with a Z."""
    assert formats.is_date(value) is valid


@pytest.mark.parametrize(
    ("value", "valid"),
    [
        ("some-package", True),
        ("some_package==1.0", True),
        ("Some.Package>=1.0,<2", True),
        ("pkg[extra, other]~=1.4.2", True),
        ("newdep @ https://example.com/newdep-1.0-py3-none-any.whl", False),
        ("--find-links /tmp", False),
        ("-r more.txt", False),
        ("pkg; python_version < '3.15'", False),
        ("git+https://github.com/someone/pkg", False),
        ("foo\n==1", False),
        ("foo\x0b==1", False),
        ("foo\t==1", False),
    ],
)
def test_requirement(value: str, valid: bool) -> None:
    """A requirement is one line the server installs: a package name with specifiers only."""
    assert formats.is_requirement(value) is valid


@pytest.mark.parametrize(
    ("value", "valid"),
    [
        ("someone", True),
        ("some-one", True),
        ("a" * 39, True),
        ("a" * 40, False),
        ("@someone", False),
        ("-someone", False),
        ("someone-", False),
        ("some--one", False),
    ],
)
def test_github_handle(value: str, valid: bool) -> None:
    """A GitHub handle has letters, digits and single inner hyphens, at most 39 characters."""
    assert formats.is_github_handle(value) is valid


@pytest.mark.parametrize(
    ("value", "valid"),
    [
        ("someone/ma-sample", True),
        ("someone/ma.sample_2", True),
        ("someone", False),
        ("someone/..", False),
        ("someone/a/b", False),
        ("https://github.com/someone/ma-sample", False),
    ],
)
def test_repository(value: str, valid: bool) -> None:
    """A repository is owner/name on GitHub."""
    assert formats.is_repository(value) is valid


@pytest.mark.parametrize(
    ("value", "valid"),
    [
        ("sample_music", True),
        ("providers/sample_music", True),
        ("provider", True),
        ("some/path/provider", True),
        ("providers/other", False),
        ("src", False),
        ("plugin", False),
        ("foo/bar", False),
        ("provider/sample", False),
        (".", False),
        ("/providers/sample_music", False),
        ("providers/../sample_music", False),
        ("providers//sample_music", False),
        ("providers/sample_music/", False),
        ("", False),
    ],
)
def test_provider_folder(value: str, valid: bool) -> None:
    """The provider folder is a relative path named after the domain or provider."""
    assert formats.is_provider_folder(value, "sample_music") is valid


@pytest.mark.parametrize(
    ("value", "valid"),
    [
        ("https://github.com/music-assistant/community-catalog/issues/1", True),
        ("http://example.com", False),
        ("https://", False),
        ("https://example.com/a b", False),
        ("https://[", False),
        ("https://[::1", False),
        ("https://[not-ipv6]/x", False),
        ("https://[::1]/x", True),
    ],
)
def test_https_url(value: str, valid: bool) -> None:
    """A reason_url is an https address."""
    assert formats.is_https_url(value) is valid
