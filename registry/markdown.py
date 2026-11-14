"""Text from a provider in Markdown, as code that nothing in it can end."""

from __future__ import annotations

import re


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


def _longest_backticks(text: str) -> int:
    """Return the length of the longest run of backticks in text."""
    return max((len(run) for run in re.findall(r"`+", text)), default=0)
