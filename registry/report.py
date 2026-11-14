"""The report step: an issue for the maintainer of every release that failed or was refused."""

from __future__ import annotations

import hashlib
import re
from typing import TYPE_CHECKING

from registry.models import Issue

if TYPE_CHECKING:
    from registry.models import FetchReport, LineResults, VersionResult
    from registry.registry import Provider, Registry

# where an issue goes when the repository of a provider has issues switched off
REGISTRY_REPOSITORY = "music-assistant/community-catalog"
README_URL = f"https://github.com/{REGISTRY_REPOSITORY}#readme"


def build_issues(
    registry: Registry, fetched: FetchReport, results: list[LineResults]
) -> list[Issue]:
    """
    Return an issue for every release refused in this run and every version that failed a check.

    :param registry: The registry, with the refused releases of the fetch step.
    :param fetched: What the fetch step found.
    :param results: The results files of the check step.
    """
    issues: list[Issue] = []
    for refused in fetched.refused:
        provider = registry.providers[refused.domain]
        release = provider.refused[refused.version]
        text = (
            f"The Music Assistant community catalog could not take release `{release.tag}` "
            f"(commit `{release.commit}`) of `{refused.domain}`:\n\n{_code(release.reason)}\n"
            "This release is not offered to any server and is not tried again. Publish a new "
            "release once it is fixed."
        )
        issues.append(
            _issue(
                fetched,
                provider,
                f"release {refused.version}",
                f"Release {release.tag} of {refused.domain} was refused by the community catalog",
                text,
            )
        )
    for file in results:
        for result in file.results:
            if not result.passed:
                provider = registry.providers[result.domain]
                issues.append(_check_issue(fetched, provider, file.line, result))
    return issues


def _check_issue(
    fetched: FetchReport, provider: Provider, line: str, result: VersionResult
) -> Issue:
    """Return the issue for a version that failed the checks on a server line."""
    domain, version = result.domain, result.version
    commit = provider.versions[version].commit
    if result.passed_before:
        title = f"{domain} {version} no longer passes the community catalog checks on {line}"
        effect = (
            f"It passed before. Servers of the `{line}` line now get the newest version that "
            "still passes there, if there is one."
        )
    else:
        title = f"{domain} {version} does not pass the community catalog checks on {line}"
        effect = f"Servers of the `{line}` line are not offered this version."
    text = (
        f"The Music Assistant community catalog checked version {version} (commit `{commit}`) "
        f"of `{domain}` in the server image of the `{line}` line, and it did not pass:\n\n"
        f"{_code(result.reason or '')}\n{effect}"
    )
    if result.output and result.output != (result.reason or ""):
        text += f"\n\n<details><summary>Output</summary>\n\n{_code(result.output)}\n</details>"
    return _issue(fetched, provider, f"check {version} {line}", title, text)


def _issue(fetched: FetchReport, provider: Provider, what: str, title: str, text: str) -> Issue:
    """
    Return an issue, on the repository of the provider or on the registry when it has none.

    :param fetched: What the fetch step found of the repositories.
    :param provider: The provider.
    :param what: What failed, which with the domain makes the key of the issue.
    :param title: The title of the issue.
    :param text: What failed and what it means, in Markdown.
    """
    domain, repository = provider.file.domain, provider.file.repository
    key = hashlib.sha256(f"{domain} {what}".encode()).hexdigest()[:16]
    body = f"{text}\n\nWhat the checks do: {README_URL}\n"
    state = fetched.repositories.get(repository)
    if state is None or not state.has_issues:
        mentions = " ".join(f"@{handle}" for handle in provider.file.maintainers)
        body += (
            f"\nThis issue is opened here because `{repository}` has issues switched off. "
            f"{mentions}\n"
        )
        repository = REGISTRY_REPOSITORY
    body += f"\n<!-- community-catalog: {key} -->\n"
    return Issue(key=key, repository=repository, title=title, body=body)


def _code(text: str) -> str:
    """Return text as a Markdown code block, fenced so nothing in it ends the block."""
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{fence}\n{text}\n{fence}\n"
