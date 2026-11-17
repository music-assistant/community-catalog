"""
The report step: one issue on the registry for each provider whose releases need attention.

Also one issue for each channel whose checks cannot run, for the maintainers of the registry.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from registry.formats import channel_name, version_key
from registry.github import ACTIONS_LOGIN
from registry.markdown import code_block, code_span
from registry.models import ChannelReport, Failure, ProviderReport

if TYPE_CHECKING:
    from collections.abc import Callable

    from registry.github import GitHub, IssueInfo
    from registry.models import ChannelResults, FetchReport, VersionResult
    from registry.registry import Provider, Registry

# the label of the issues of the release pipeline
LABEL = "release-check"
README_URL = "https://github.com/music-assistant/community-catalog#releasing-updates"
CHANGE_FORM_URL = (
    "https://github.com/music-assistant/community-catalog/issues/new?template=report-provider.yml"
)
# the most maintainers a new issue mentions
MAX_MENTIONS = 10
# GitHub takes 65536 characters in an issue or a comment, counted as UTF-16
_MAX_TEXT = 65_536
# the failures of one post share at most this much of the end of their output
_MAX_OUTPUT = 40_000
# the first line of an issue or comment of the pipeline: the domain and the keys of its failures
_MARKER = re.compile(r"<!-- release-check: ([a-z][a-z0-9_]*)((?:; [a-z0-9. ]+)*) -->")
# the first line of an issue of the pipeline about a channel whose checks cannot run; an open
# issue may still name it with "line", which its next update replaces
_CHANNEL_MARKER = re.compile(r"<!-- release-check (?:channel|line): ([a-z0-9.]+) -->")
# a manifest name a title shows; any other name is left out of the title
_TITLE_NAME = re.compile(r"[\w .,()&'+-]{1,60}")


@dataclass(frozen=True, slots=True)
class OpenIssue:
    """The open issue of the pipeline for a provider."""

    number: int
    # the keys of the failures it tells, in its text and in the comments of the pipeline
    keys: frozenset[str]


def build_reports(
    registry: Registry, fetched: FetchReport, results: list[ChannelResults]
) -> list[ProviderReport]:
    """
    Return the report of every provider: its failures in this run not settled, and its state.

    The failures are the releases refused in this run, the versions that failed a check and a
    repository that is not the admitted one. A failure is settled when is_settled says so. A channel
    whose checks could not run in this run counts as not tested.

    :param registry: The registry, with the refused releases of the fetch step.
    :param fetched: What the fetch step found.
    :param results: The results of the check step, as read_results returns them.
    """
    unavailable = {
        channel.channel
        for channel in build_channel_reports(registry, results)
        if channel.reason is not None
    }
    tested = sorted(
        channel
        for channel, config in registry.channels.items()
        if not config.frozen and channel not in unavailable
    )
    passed: dict[tuple[str, str, str], bool] = {
        (domain, version, channel): check.passed
        for domain, provider in registry.providers.items()
        for version, file in provider.versions.items()
        for channel, check in file.checks.items()
    }
    failures: dict[str, list[Failure]] = {}
    for refused in fetched.refused:
        if refused.domain in registry.providers:
            provider = registry.providers[refused.domain]
            failures.setdefault(refused.domain, []).append(_refusal(provider, refused.version))
    for file in results:
        if file.channel not in tested:
            continue
        for result in file.results:
            passed[result.domain, result.version, file.channel] = result.passed
            if not result.passed and result.domain in registry.providers:
                provider = registry.providers[result.domain]
                failure = _check_failure(provider, file.channel, result)
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
            passed=[channel for channel in tested if passed.get((domain, newest or "", channel))],
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

    That is when the newest version passed on a tested channel, and also on the channel of a failed
    check, unless that channel is not tested in this run; for a repository that was not the admitted
    one, GitHub must give the admitted one again.

    :param report: The report of the provider.
    :param key: The key of the failure.
    """
    kind, *rest = key.split(" ")
    if not report.passed:
        return False
    match kind, rest:
        case "check", [_, channel]:
            return channel in report.passed or channel not in report.tested
        case "refused", [_]:
            return True
        case "repository", [_, _]:
            return report.admitted
    return False


def build_channel_reports(registry: Registry, results: list[ChannelResults]) -> list[ChannelReport]:
    """
    Return whether the checks could run, for every tested channel with results in this run.

    :param registry: The registry.
    :param results: The results of the check step, as read_results returns them.
    """
    reports: dict[str, ChannelReport] = {}
    for file in results:
        if (config := registry.channels.get(file.channel)) is None or config.frozen:
            continue
        report = reports.setdefault(file.channel, ChannelReport(channel=file.channel))
        for result in file.results:
            if result.unavailable and report.reason is None:
                report.reason, report.output = result.reason, result.output
    return [reports[channel] for channel in sorted(reports)]


def open_issues(github: GitHub, repository: str) -> dict[str, OpenIssue | OSError]:
    """
    Return the open issue of the pipeline of each provider, by domain.

    Only what the pipeline's own account wrote counts, so nobody can settle or hide a failure by
    writing its key. A provider whose issue could not be read this time gets the error instead.

    :param github: A client.
    :param repository: The repository of the registry, as owner/name.
    :raises OSError: The issues could not be listed.
    """
    result: dict[str, OpenIssue | OSError] = {}
    for issue in sorted(github.issues(repository, LABEL), key=lambda issue: issue.number):
        if issue.author != ACTIONS_LOGIN or (told := _marker(issue.body)) is None:
            continue
        domain, keys = told
        if domain in result:
            continue
        try:
            comments = github.comments(repository, issue.number)
        except OSError as err:
            result[domain] = err
            continue
        for comment in comments:
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
        body = _fit(
            lambda room: (
                f"{_marker_line(report.domain, new)}\n"
                f"The Music Assistant community catalog checks the releases of `{report.domain}`. "
                f"This needs the attention of its maintainers:\n\n{_failures(new, room)}"
                f"### What to do\n\n{fix}[Releasing updates]({README_URL}) tells how releases are "
                "checked. This issue closes by itself once the newest release passes the checks."
                f"\n\n{mentions}\n"
            )
        )
        return github.create_issue(repository, report.title, body, [LABEL])
    if new:
        body = _fit(lambda room: f"{_marker_line(report.domain, new)}\n{_failures(new, room)}")
        github.add_comment(repository, issue.number, body)
        return f"#{issue.number}: {len(new)} new failure(s)"
    if all(is_settled(report, key) for key in told):
        line = f"Version {report.newest} passed the checks, so this issue is closed."
        github.add_comment(repository, issue.number, line)
        github.close_issue(repository, issue.number)
        return f"#{issue.number}: closed"
    return None


def open_channel_issues(github: GitHub, repository: str) -> dict[str, IssueInfo]:
    """
    Return the open issue of the pipeline about each channel whose checks cannot run, by channel.

    Only what the pipeline's own account wrote counts.

    :param github: A client.
    :param repository: The repository of the registry, as owner/name.
    :raises OSError: The issues could not be listed.
    """
    result: dict[str, IssueInfo] = {}
    for issue in sorted(github.issues(repository, LABEL), key=lambda issue: issue.number):
        found = _CHANNEL_MARKER.fullmatch(issue.body.split("\n", 1)[0])
        if issue.author == ACTIONS_LOGIN and found is not None:
            result.setdefault(found[1], issue)
    return result


def post_channel_report(
    report: ChannelReport, issue: IssueInfo | None, github: GitHub, repository: str
) -> str | None:
    """
    Tell the maintainers of the registry that the checks cannot run on a channel.

    A channel whose checks cannot run gets an issue, whose text is kept up to date while they
    cannot; once they run again, the issue is closed. Returns what was done, or None when
    nothing was.

    :param report: The report of the channel.
    :param issue: The open issue of the pipeline about the channel.
    :param github: A client whose token may write issues on the registry.
    :param repository: The repository of the registry, as owner/name.
    :raises GitHubError: GitHub refused a request.
    """
    channel = report.channel
    name = channel_name(channel)
    if report.reason is None:
        if issue is None:
            return None
        text = f"The checks run on {name} again, so this issue is closed."
        github.add_comment(repository, issue.number, text)
        github.close_issue(repository, issue.number)
        return f"#{issue.number}: closed"
    failure = Failure(
        key=f"channel {channel}",
        heading=f"The checks cannot run in the server image of {name}",
        text=(
            f"The checks of every version stopped in the server image of {name}, before any "
            f"provider code ran:\n\n{code_block(report.reason)}\n"
            f"Until this is fixed, no version gets a new result on {name}: servers on {name} are "
            "offered only the versions that passed there before. The maintainers of the "
            "providers are not told about this."
        ),
        output=report.output if report.output != report.reason else None,
    )
    body = _fit(
        lambda room: (
            f"<!-- release-check channel: {channel} -->\n"
            f"The Music Assistant community catalog cannot check releases on {name}. "
            f"This needs the attention of the maintainers of the catalog:\n\n"
            f"{_failures([failure], room)}### What to do\n\n"
            "Make the checks of the catalog run in the server image of this channel again. This "
            "issue is kept up to date while the checks cannot run, and closes by itself once "
            f"they run on {name} again.\n"
        )
    )
    if issue is None:
        title = f"The release checks cannot run on {name}"
        return github.create_issue(repository, title, body, [LABEL])
    if issue.body == body:
        return None
    github.update_issue(repository, issue.number, body)
    return f"#{issue.number}: updated"


def _check_failure(provider: Provider, channel: str, result: VersionResult) -> Failure:
    """Return the failure of a version that failed the checks on a channel."""
    version = result.version
    commit = provider.versions[version].commit
    name = channel_name(channel)
    if result.passed_before:
        heading = f"Version {version} no longer passes the checks on {name}"
        effect = (
            f"It passed before. Servers on {name} get the newest version that still passes "
            "there, if there is one."
        )
    else:
        heading = f"Version {version} does not pass the checks on {name}"
        effect = f"Servers on {name} are not offered this version."
    text = (
        f"Version {version} (commit `{commit}`) was checked in the server image of {name}, and "
        f"did not pass:\n\n{code_block(result.reason or '')}\n{effect}"
    )
    output = result.output if result.output and result.output != result.reason else None
    return Failure(
        key=f"check {version} {channel}",
        heading=heading,
        text=text,
        output=output,
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


def _fit(render: Callable[[int], str]) -> str:
    """
    Return the text of a post with as much output as GitHub takes in it.

    :param render: Returns the text with at most this many characters of output.
    """
    room = _MAX_OUTPUT
    # the fences grow with the output, so the whole text is measured
    while len((text := render(room)).encode("utf-16-le")) // 2 > _MAX_TEXT and room:
        room //= 2
    return text


def _failures(failures: list[Failure], room: int) -> str:
    """Return failures in Markdown, each under its heading, sharing the room for output."""
    share = room // len(failures)
    text = ""
    for failure in failures:
        text += f"### {failure.heading}\n\n{failure.text}\n\n"
        if failure.output:
            shown = failure.output[-share:] if share else ""
            summary = (
                "Output"
                if shown == failure.output
                else f"Output, shortened to its last {len(shown)} characters"
            )
            output = code_block(shown)
            text += f"<details><summary>{summary}</summary>\n\n{output}\n</details>\n\n"
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
