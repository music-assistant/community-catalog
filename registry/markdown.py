"""Text from a provider in Markdown, as code that nothing in it can end."""

from __future__ import annotations

import re
from urllib.parse import quote

from registry.formats import is_repository

_GITHUB_URL = "https://github.com"


def code_block(text: str, indent: str = "") -> str:
    """
    Return text as a fenced code block, with a fence longer than any run of backticks in it.

    :param text: The text.
    :param indent: What every line starts with, such as two spaces inside a list item.
    """
    fence = "`" * max(3, _longest_backticks(text) + 1)
    lines = [fence, *text.splitlines(), fence]
    return "".join(f"{indent}{line}\n" for line in lines)


def code_span(text: str) -> str:
    """
    Return one line of text as an inline code span that nothing in it can end.

    :param text: The text; line breaks become spaces.
    """
    line = " ".join(text.splitlines())
    fence = "`" * (_longest_backticks(line) + 1)
    return f"{fence} {line} {fence}"


def github_link(text: str, repository: str, path: str = "") -> str:
    """
    Return one line of text as an inline code span that links to a page of a repository on GitHub.

    The text is code without a link when the repository is no owner/name, or a part of the path
    is empty, . or ..

    :param text: The text; line breaks become spaces.
    :param repository: The repository, as owner/name.
    :param path: The page in the repository, such as releases/tag/v1.0.0; every part between
        slashes is percent-encoded.
    """
    parts = path.split("/") if path else []
    if not is_repository(repository) or any(part in {"", ".", ".."} for part in parts):
        return code_span(text)
    url = "/".join([_GITHUB_URL, repository, *(quote(part, safe="") for part in parts)])
    return f"[{code_span(text)}]({url})"


def _longest_backticks(text: str) -> int:
    """Return the length of the longest run of backticks in text."""
    return max((len(run) for run in re.findall(r"`+", text)), default=0)
