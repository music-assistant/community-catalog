"""Tests for text from a provider in Markdown."""

from __future__ import annotations

import pytest

from registry.markdown import code_span, github_link

REPOSITORY = "someone/ma-sample"


@pytest.mark.parametrize(
    ("tag", "path"),
    [
        ("v1.0.0", "v1.0.0"),
        ("sample_music/v1.0.0", "sample_music/v1.0.0"),
        ("@a-maintainer", "%40a-maintainer"),
        ("v1 `x`", "v1%20%60x%60"),
        ("v1)](https:/example.com)", "v1%29%5D%28https%3A/example.com%29"),
        ("v1\n[x]", "v1%0A%5Bx%5D"),
        ("v1?a#b%", "v1%3Fa%23b%25"),
    ],
)
def test_a_link_to_github_is_percent_encoded(tag: str, path: str) -> None:
    """Every part of the path is percent-encoded, and the text stays code."""
    link = github_link(tag, REPOSITORY, f"releases/tag/{tag}")

    assert link == (
        f"[{code_span(tag)}](https://github.com/{REPOSITORY}/releases/tag/{path})"
    )


@pytest.mark.parametrize(
    ("repository", "path"),
    [
        ("example.com/x", ""),
        ("someone/ma-sample)", ""),
        ("someone/ma sample", ""),
        ("someone/..", ""),
        ("someone", ""),
        ("", ""),
        (REPOSITORY, "releases/tag/../../../other/repository"),
        (REPOSITORY, "releases/tag/a//b"),
        (REPOSITORY, "releases/tag/./x"),
        (REPOSITORY, "releases/tag/"),
    ],
)
def test_no_link_without_a_safe_address(repository: str, path: str) -> None:
    """A repository that is no owner/name, or a path with an empty, . or .. part, is only code."""
    assert github_link("v1)](https://example.com)", repository, path) == code_span(
        "v1)](https://example.com)"
    )
