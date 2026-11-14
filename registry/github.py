"""The requests the registry makes to GitHub: releases, archives and issues."""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from registry.formats import is_commit

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from registry.models import Issue

API_URL = "https://api.github.com"
GRAPHQL_URL = f"{API_URL}/graphql"
ARCHIVE_URL = "https://codeload.github.com/{repository}/tar.gz/{commit}"
USER_AGENT = "music-assistant-community-catalog"
# repositories per GraphQL request; well below the limits GitHub sets for one query
BATCH_SIZE = 50
# the releases read per repository, newest first; also for a repository with several providers
RELEASES_PER_REPOSITORY = 30
_TIMEOUT = 60
_REPOSITORY_FIELDS = f"""
    isPrivate isArchived hasIssuesEnabled
    owner {{ login }}
    licenseInfo {{ key }}
    releases(first: {RELEASES_PER_REPOSITORY}, orderBy: {{field: CREATED_AT, direction: DESC}}) {{
      nodes {{ tagName isDraft isPrerelease publishedAt tagCommit {{ oid }} }}
    }}
"""


class Transport(Protocol):
    """Sends a request and returns the status and the body of the response."""

    def __call__(self, request: urllib.request.Request) -> tuple[int, bytes]:
        """
        Send a request.

        :param request: The request.
        :raises OSError: The request could not be sent.
        """


class GitHubError(OSError):
    """GitHub did not answer as expected."""


@dataclass(frozen=True, slots=True)
class Release:
    """A published release of a repository."""

    tag: str
    commit: str
    published: str
    prerelease: bool


@dataclass(frozen=True, slots=True)
class RepositoryInfo:
    """What the registry reads of a repository."""

    owner: str
    private: bool
    archived: bool
    has_issues: bool
    # the key of the licence GitHub recognises, "other" for one it does not
    license: str | None
    # newest first
    releases: list[Release]


def urlopen(request: urllib.request.Request) -> tuple[int, bytes]:
    """
    Send a request over the network.

    :param request: The request.
    :raises OSError: The request could not be sent.
    """
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT) as response:  # noqa: S310 - https only
            return response.status, response.read()
    except urllib.error.HTTPError as err:
        return err.code, err.read()


class GitHub:
    """The GitHub API and the archives of commits."""

    def __init__(self, token: str | None, transport: Transport = urlopen) -> None:
        """
        Initialize the client.

        :param token: The token to authenticate with; the GraphQL API needs one.
        :param transport: What sends the requests.
        """
        self._token = token
        self._transport = transport

    def repositories(self, names: Sequence[str]) -> dict[str, RepositoryInfo | None]:
        """
        Return what the registry reads of repositories, with their releases, in a few requests.

        :param names: The repositories, as owner/name.
        :raises GitHubError: GitHub refused a request or answered what cannot be read.
        """
        result: dict[str, RepositoryInfo | None] = {}
        for start in range(0, len(names), BATCH_SIZE):
            batch = names[start : start + BATCH_SIZE]
            # the names are passed as variables, never written into the query
            variables: dict[str, str] = {}
            parts: list[str] = []
            for number, name in enumerate(batch):
                variables[f"o{number}"], variables[f"n{number}"] = name.split("/", 1)
                parts.append(f"r{number}: repository(owner: $o{number}, name: $n{number}) {{")
                parts.append(f"{_REPOSITORY_FIELDS}}}")
            arguments = ", ".join(f"${key}: String!" for key in variables)
            query = f"query({arguments}) {{\n{'\n'.join(parts)}\n}}"
            data = self._graphql(query, variables)
            for number, name in enumerate(batch):
                node = data.get(f"r{number}")
                result[name] = None if node is None else _repository(node)
        return result

    def download_archive(self, repository: str, commit: str, target: Path) -> None:
        """
        Download the GitHub archive of a commit, which is what a server installs from.

        :param repository: The repository, as owner/name.
        :param commit: The full SHA of the commit.
        :param target: The file to write.
        :raises GitHubError: The archive could not be downloaded.
        :raises OSError: The request could not be sent or the file not written.
        """
        if not is_commit(commit):
            msg = f"{commit!r} is not a commit"
            raise GitHubError(msg)
        url = ARCHIVE_URL.format(repository=repository, commit=commit)
        status, body = self._transport(self._request(url, authenticated=False))
        if status != 200:  # noqa: PLR2004
            msg = f"{url} answered {status}"
            raise GitHubError(msg)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(body)

    def issue_exists(self, repository: str, key: str) -> bool:
        """
        Return whether a repository has an issue, open or closed, whose body holds a key.

        :param repository: The repository, as owner/name.
        :param key: The key of the issue.
        :raises GitHubError: GitHub refused the request.
        """
        query = urllib.parse.urlencode({"q": f'"{key}" repo:{repository} is:issue in:body'})
        found = self._json(self._request(f"{API_URL}/search/issues?{query}"))
        return bool(found.get("total_count"))

    def create_issue(self, repository: str, title: str, body: str) -> str:
        """
        Open an issue and return its address.

        :param repository: The repository, as owner/name.
        :param title: The title of the issue.
        :param body: The text of the issue, in Markdown.
        :raises GitHubError: GitHub refused the request.
        """
        content = json.dumps({"title": title, "body": body}).encode()
        request = self._request(f"{API_URL}/repos/{repository}/issues", content)
        return str(self._json(request, expected=201).get("html_url"))

    def _graphql(self, query: str, variables: dict[str, str]) -> dict[str, Any]:
        """
        Run a GraphQL query and return its data; a repository that does not exist is None.

        :param query: The query.
        :param variables: The values of its variables.
        :raises GitHubError: GitHub refused the query or reported another error.
        """
        content = json.dumps({"query": query, "variables": variables}).encode()
        answer = self._json(self._request(GRAPHQL_URL, content))
        # a repository that does not exist, or is not public, is an error of its own and null
        errors = [error for error in answer.get("errors") or [] if error.get("type") != "NOT_FOUND"]
        data = answer.get("data")
        if errors or not isinstance(data, dict):
            msg = f"GitHub refused the query: {errors or answer}"
            raise GitHubError(msg)
        return data

    def _json(self, request: urllib.request.Request, expected: int = 200) -> dict[str, Any]:
        """
        Send a request to the API and return the JSON object it answers.

        :param request: The request.
        :param expected: The status of a successful answer.
        :raises GitHubError: GitHub answered another status or no JSON object.
        """
        status, body = self._transport(request)
        if status != expected:
            msg = f"{request.full_url} answered {status}: {body[:500]!r}"
            raise GitHubError(msg)
        try:
            data = json.loads(body)
        except ValueError as err:
            msg = f"{request.full_url} answered no JSON: {err}"
            raise GitHubError(msg) from err
        if not isinstance(data, dict):
            msg = f"{request.full_url} answered no JSON object"
            raise GitHubError(msg)
        return data

    def _request(
        self, url: str, content: bytes | None = None, *, authenticated: bool = True
    ) -> urllib.request.Request:
        """
        Return a request to GitHub, a POST when it has content.

        :param url: The address.
        :param content: The JSON body.
        :param authenticated: Whether to send the token.
        """
        headers = {"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"}
        if authenticated and self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        if content is not None:
            headers["Content-Type"] = "application/json"
        return urllib.request.Request(url, content, headers)  # noqa: S310 - https only


def post_issue(issue: Issue, token: str, transport: Transport = urlopen) -> str | None:
    """
    Open an issue on GitHub, unless an issue with its key exists already.

    Returns the address of the new issue, or None when it was reported before.

    :param issue: The issue.
    :param token: A token that may open issues on the repository of the issue.
    :param transport: What sends the requests.
    :raises GitHubError: GitHub refused a request.
    :raises OSError: A request could not be sent.
    """
    github = GitHub(token, transport)
    if github.issue_exists(issue.repository, issue.key):
        return None
    return github.create_issue(issue.repository, issue.title, issue.body)


def _repository(node: dict[str, Any]) -> RepositoryInfo:
    """
    Return a repository as the GraphQL API answers it.

    :param node: The repository in the answer.
    :raises GitHubError: The answer is not as queried.
    """
    try:
        releases = [
            Release(
                tag=release["tagName"],
                commit=(release["tagCommit"] or {}).get("oid", ""),
                published=release["publishedAt"],
                prerelease=release["isPrerelease"],
            )
            for release in node["releases"]["nodes"]
            # a draft is not released; only a token with push access sees it
            if not release["isDraft"] and release["publishedAt"]
        ]
        return RepositoryInfo(
            owner=node["owner"]["login"],
            private=node["isPrivate"],
            archived=node["isArchived"],
            has_issues=node["hasIssuesEnabled"],
            license=(node["licenseInfo"] or {}).get("key"),
            releases=releases,
        )
    except (KeyError, TypeError, AttributeError) as err:
        msg = f"GitHub answered a repository that cannot be read: {err!r}"
        raise GitHubError(msg) from err
