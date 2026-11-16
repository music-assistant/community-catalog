"""The report step: one issue on the registry for each provider whose releases need attention."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from registry.formats import version_key
from registry.github import ACTIONS_LOGIN
from registry.markdown import code_block, code_span
from registry.models import Failure, ProviderReport

if TYPE_CHECKING:
    from registry.github import GitHub
    from registry.models import FetchReport, LineResults, VersionResult
    from registry.registry import Provider, Registry

# the label of the issues of the release pipeline
LABEL = "release-check"
README_URL = "https://github.com/music-assistant/community-catalog#releasing-updates"
CHANGE_FORM_URL = (
    "https://github.com/music-assistant/community-catalog/issues/new?template=report-provider.yml"
)
# the most maintainers a new issue mentions
MAX_MENTIONS = 10
# GitHub takes 65536 characters in an issue or a comment; the failures of one post share this
# much of the end of their output
_MAX_OUTPUT = 40_000
# the first line of an issue or comment of the pipeline: the domain and the keys of its failures
_MARKER = re.compile(r"<!-- release-check: ([a-z][a-z0-9_]*)((?:; [a-z0-9. ]+)*) -->")
# a manifest name a title shows; any other name is left out of the title
_TITLE_NAME = re.compile(r"[\w .,()&'+-]{1,60}")


@dataclass(frozen=True, slots=True)
class OpenIssue:
    """The open issue of the pipeline for a provider."""

    number: int
    # the keys of the failures it tells, in its text and in the comments of the pipeline
    keys: frozenset[str]


def build_reports(
    registry: Registry, fetched: FetchReport, results: list[LineResults]
) -> list[ProviderReport]:
    """
    Return the report of every provider: its failures in this run not settled, and its state.

    The failures are the releases refused in this run, the versions that failed a check and a
    repository that is not the admitted one. A failure is settled when is_settled says so.

    :param registry: The registry, with the refused releases of the fetch step.
    :param fetched: What the fetch step found.
    :param results: The results of the check step, as read_results returns them.
    """
    tested = sorted(line for line, config in registry.server_lines.items() if not config.frozen)
    passed: dict[tuple[str, str, str], bool] = {
        (domain, version, line): check.passed
        for domain, provider in registry.providers.items()
        for version, file in provider.versions.items()
        for line, check in file.checks.items()
    }
    failures: dict[str, list[Failure]] = {}
    for refused in fetched.refused:
        if refused.domain in registry.providers:
            provider = registry.providers[refused.domain]
            failures.setdefault(refused.domain, []).append(_refusal(provider, refused.version))
    for file in results:
        if file.line not in tested:
            continue
        for result in file.results:
            passed[result.domain, result.version, file.line] = result.passed
            if not result.passed and result.domain in registry.providers:
                provider = registry.providers[result.domain]
                failure = _check_failure(provider, file.line, result)
                failures.setdefault(result.domain, []).append(failure)
    reports: list[ProviderReport] = []
    for domain, provider in sorted(registry.providers.items()):
        newest = max([*provider.versions, *provider.refused], key=version_key, default=None)
        state = fetched.repositories.get(provider.file.repository)
        ids = (provider.file.repository_id, provider.file.owner_id)
        report = ProviderReport(
            domain=domain,
            title=_title(provider),
            maintainers=provider.maintainers()[:MAX_MENTIONS],
            failures=[],
            newest=newest,
            tested=tested,
            passed=[line for line in tested if passed.get((domain, newest or "", line))],
            admitted=state is not None and (state.repository_id, state.owner_id) == ids,
        )
        found = failures.get(domain, [])
        if state is not None and not report.admitted:
            found.insert(0, _repository_failure(provider, state.repository_id, state.owner_id))
        report.failures = [failure for failure in found if not is_settled(report, failure.key)]
        reports.append(report)
    return reports


def is_settled(report: ProviderReport, key: str) -> bool:
    """
    Return whether a failure of a provider no longer needs its maintainers.

    That is when the newest version passed on a tested line, and also on the line of a failed
    check, unless that line is no longer tested; for a repository that was not the admitted one,
    GitHub must give the admitted one again.

    :param report: The report of the provider.
    :param key: The key of the failure.
    """
    kind, *rest = key.split(" ")
    if not report.passed:
        return False
    match kind, rest:
        case "check", [_, line]:
            return line in report.passed or line not in report.tested
        case "refused", [_]:
            return True
        case "repository", [_, _]:
            return report.admitted
    return False


def open_issues(github: GitHub, repository: str) -> dict[str, OpenIssue]:
    """
    Return the open issue of the pipeline of each provider, by domain.

    Only what the pipeline's own account wrote counts, so nobody can settle or hide a failure by
    writing its key.

    :param github: A client.
    :param repository: The repository of the registry, as owner/name.
    :raises GitHubError: GitHub refused a request.
    """
    result: dict[str, OpenIssue] = {}
    for issue in sorted(github.issues(repository, LABEL), key=lambda issue: issue.number):
        if issue.author != ACTIONS_LOGIN or (told := _marker(issue.body)) is None:
            continue
        domain, keys = told
        if domain in result:
            continue
        for comment in github.comments(repository, issue.number):
            if comment.author == ACTIONS_LOGIN and (more := _marker(comment.body)) is not None:
                keys |= more[1]
        result[domain] = OpenIssue(issue.number, keys)
    return result


def post_report(
    report: ProviderReport, issue: OpenIssue | None, github: GitHub, repository: str
) -> str | None:
    """
    Tell the maintainers of a provider about its new failures, or close its issue once settled.

    Without an open issue, new failures open one that mentions the maintainers. With one, they
    are added as a comment, and an issue whose failures are all settled is closed. Returns what
    was done, or None when nothing was.

    :param report: The report of the provider.
    :param issue: The open issue of the pipeline for the provider.
    :param github: A client whose token may write issues on the registry.
    :param repository: The repository of the registry, as owner/name.
    :raises GitHubError: GitHub refused a request.
    """
    told = issue.keys if issue is not None else frozenset()
    new = [failure for failure in report.failures if failure.key not in told]
    if issue is None:
        if not new:
            return None
        mentions = " ".join(f"@{handle}" for handle in report.maintainers)
        fix = (
            "Publish a new release that fixes this. A refused release is not tried again. "
            if any(not failure.key.startswith("repository ") for failure in new)
            else ""
        )
        body = (
            f"{_marker_line(report.domain, new)}\n"
            f"The Music Assistant community catalog checks the releases of `{report.domain}`. "
            f"This needs the attention of its maintainers:\n\n{_failures(new)}"
            f"### What to do\n\n{fix}[Releasing updates]({README_URL}) tells how releases are "
            "checked. This issue closes by itself once the newest release passes the checks.\n\n"
            f"{mentions}\n"
        )
        return github.create_issue(repository, report.title, body, [LABEL])
    if new:
        body = f"{_marker_line(report.domain, new)}\n{_failures(new)}"
        github.add_comment(repository, issue.number, body)
        return f"#{issue.number}: {len(new)} new failure(s)"
    if all(is_settled(report, key) for key in told):
        line = f"Version {report.newest} passed the checks, so this issue is closed."
        github.add_comment(repository, issue.number, line)
        github.close_issue(repository, issue.number)
        return f"#{issue.number}: closed"
    return None


def _check_failure(provider: Provider, line: str, result: VersionResult) -> Failure:
    """Return the failure of a version that failed the checks on a server line."""
    version = result.version
    commit = provider.versions[version].commit
    if result.passed_before:
        heading = f"Version {version} no longer passes the checks on {line}"
        effect = (
            f"It passed before. Servers of the `{line}` line get the newest version that still "
            "passes there, if there is one."
        )
    else:
        heading = f"Version {version} does not pass the checks on {line}"
        effect = f"Servers of the `{line}` line are not offered this version."
    text = (
        f"Version {version} (commit `{commit}`) was checked in the server image of the `{line}` "
        f"line, and did not pass:\n\n{code_block(result.reason or '')}\n{effect}"
    )
    output = result.output if result.output and result.output != result.reason else None
    return Failure(
        key=f"check {version} {line}",
        heading=heading,
        text=text,
        output=output[-_MAX_OUTPUT:] if output else None,
    )


def _refusal(provider: Provider, version: str) -> Failure:
    """Return the failure of a release that was refused in this run."""
    release = provider.refused[version]
    text = (
        f"Release {code_span(release.tag)} (commit `{release.commit}`) could not become a "
        f"version:\n\n{code_block(release.reason)}\n"
        "This release is not offered to any server and is not tried again."
    )
    return Failure(key=f"refused {version}", heading=f"Release {version} was refused", text=text)


def _repository_failure(provider: Provider, repository_id: int, owner_id: int) -> Failure:
    """Return the failure of a repository that has other ids than when it was admitted."""
    file = provider.file
    text = (
        f"GitHub gives the repository `{file.repository}` repository_id {repository_id}, "
        f"owner_id {owner_id}, while the catalog admitted repository_id {file.repository_id}, "
        f"owner_id {file.owner_id}: it was renamed, transferred or replaced by another one under "
        "the same name. No release of it is checked until a registry maintainer updates the "
        "catalog. If you moved the repository, ask for the change with the form "
        f"[Report or change a provider]({CHANGE_FORM_URL})."
    )
    return Failure(
        key=f"repository {repository_id} {owner_id}",
        heading="The repository is not the one that was admitted",
        text=text,
    )


def _title(provider: Provider) -> str:
    """Return the title of an issue: the name of the newest manifest when plain, and the domain."""
    domain = provider.file.domain
    highest = max(provider.versions, key=version_key, default=None)
    name = provider.versions[highest].manifest.get("name") if highest is not None else None
    if isinstance(name, str) and name == name.strip() and _TITLE_NAME.fullmatch(name):
        return f"{name} ({domain}): release checks failed"
    return f"{domain}: release checks failed"


def _failures(failures: list[Failure]) -> str:
    """Return failures in Markdown, each under its heading, sharing the room for output."""
    room = _MAX_OUTPUT // len(failures)
    text = ""
    for failure in failures:
        text += f"### {failure.heading}\n\n{failure.text}\n\n"
        if failure.output:
            output = code_block(failure.output[-room:])
            text += f"<details><summary>Output</summary>\n\n{output}\n</details>\n\n"
    return text


def _marker_line(domain: str, failures: list[Failure]) -> str:
    """Return the first line of a post of the pipeline, which names the failures it tells."""
    return f"<!-- release-check: {'; '.join([domain, *(f.key for f in failures)])} -->"


def _marker(body: str) -> tuple[str, frozenset[str]] | None:
    """
    Return the domain and the keys a post of the pipeline tells, or None for another text.

    Only the first line counts: the rest shows text of the provider.

    :param body: The text of the issue or comment.
    """
    if (found := _MARKER.fullmatch(body.split("\n", 1)[0])) is None:
        return None
    return found[1], frozenset(found[2].split("; ")[1:])
