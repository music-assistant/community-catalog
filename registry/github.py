"""The requests the registry makes to GitHub: releases, archives, issues and pull requests."""

from __future__ import annotations

import base64
import http.client
import itertools
import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, BinaryIO, Protocol, cast

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
# the largest archive of a commit that is downloaded; a larger one is refused
MAX_ARCHIVE_SIZE = 100 * 1024 * 1024
# the comments and pull requests read per request, the most GitHub answers
COMMENTS_PER_PAGE = 100
PULL_REQUESTS_PER_PAGE = 100
# far more than any answer of the API the registry asks for
_MAX_ANSWER_SIZE = 10 * 1024 * 1024
_CHUNK_SIZE = 64 * 1024
_TIMEOUT = 60
_REPOSITORY_FIELDS = f"""
    nameWithOwner databaseId isPrivate isArchived hasIssuesEnabled
    owner {{ login ... on User {{ databaseId }} ... on Organization {{ databaseId }} }}
    licenseInfo {{ key }}
    releases(first: {RELEASES_PER_REPOSITORY}, orderBy: {{field: CREATED_AT, direction: DESC}}) {{
      nodes {{ tagName isDraft isPrerelease publishedAt tagCommit {{ oid }} }}
    }}
"""


class Transport(Protocol):
    """Sends a request and returns the status and the body of the response, to be read."""

    def __call__(self, request: urllib.request.Request) -> tuple[int, BinaryIO]:
        """
        Send a request; the caller closes the body.

        :param request: The request.
        :raises OSError: The request could not be sent.
        """


class GitHubError(OSError):
    """GitHub did not answer as expected."""


class ArchiveTooLargeError(GitHubError):
    """The archive of a commit is larger than the registry downloads."""


class NotFoundError(GitHubError):
    """GitHub answered that what was asked for does not exist."""


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

    # owner/name as GitHub spells it
    name: str
    owner: str
    repository_id: int
    owner_id: int
    private: bool
    archived: bool
    has_issues: bool
    # the key of the licence GitHub recognises, "other" for one it does not
    license: str | None
    # newest first
    releases: list[Release]


@dataclass(frozen=True, slots=True)
class IssueInfo:
    """What the registry reads of an issue."""

    author: str
    body: str
    labels: list[str]
    open: bool
    # the API answers a pull request as an issue as well
    pull_request: bool


@dataclass(frozen=True, slots=True)
class PullRequest:
    """An open pull request."""

    number: int
    url: str
    author: str
    # owner/name of the repository its branch is in, None when that repository is deleted
    head_repository: str | None
    branch: str
    body: str


def urlopen(request: urllib.request.Request) -> tuple[int, BinaryIO]:
    """
    Send a request over the network.

    :param request: The request.
    :raises OSError: The request could not be sent.
    """
    try:
        response = urllib.request.urlopen(request, timeout=_TIMEOUT)  # noqa: S310 - https only
    except urllib.error.HTTPError as err:
        # the error is the response, with its body to read
        return err.code, cast("BinaryIO", err)
    except http.client.HTTPException as err:
        msg = f"{request.full_url}: {err!r}"
        raise GitHubError(msg) from err
    return response.status, response


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
        self._login: str | None = None

    def repositories(self, names: Sequence[str]) -> dict[str, RepositoryInfo | GitHubError | None]:
        """
        Return what the registry reads of repositories, with their releases, in a few requests.

        Each repository is None when it does not exist or is not public, and a GitHubError when
        it could not be read this time; the others are read all the same.

        :param names: The repositories, as owner/name.
        """
        result: dict[str, RepositoryInfo | GitHubError | None] = {}
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
            try:
                data, errors = self._graphql(query, variables)
            except OSError as err:
                failure = err if isinstance(err, GitHubError) else GitHubError(str(err))
                result |= dict.fromkeys(batch, failure)
                continue
            for number, name in enumerate(batch):
                if (error := errors.get(f"r{number}")) is not None:
                    result[name] = GitHubError(f"GitHub could not answer {name}: {error}")
                elif (node := data.get(f"r{number}")) is None:
                    result[name] = None
                else:
                    try:
                        result[name] = _repository(node)
                    except GitHubError as err:
                        result[name] = err
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
        target.parent.mkdir(parents=True, exist_ok=True)
        partial = target.with_name(f"{target.name}.part")
        try:
            status, body = self._transport(self._request(url, authenticated=False))
            with body, partial.open("wb") as file:
                if status != 200:  # noqa: PLR2004
                    msg = f"{url} answered {status}"
                    raise GitHubError(msg)
                size = 0
                while chunk := _read(body, _CHUNK_SIZE):
                    size += len(chunk)
                    if size > MAX_ARCHIVE_SIZE:
                        msg = f"the archive of the commit is larger than {MAX_ARCHIVE_SIZE} bytes"
                        raise ArchiveTooLargeError(msg)
                    file.write(chunk)
            partial.replace(target)
        finally:
            partial.unlink(missing_ok=True)

    def set_comment(
        self, repository: str, number: int, body: str, *, marker: str, author: str
    ) -> str:
        """
        Update the comment of an account that starts with a marker, or add it; return its address.

        The comment is on an issue or a pull request, and its text is the marker and the body.

        :param repository: The repository, as owner/name.
        :param number: The number of the issue or pull request.
        :param body: The text of the comment, in Markdown.
        :param marker: What the comment starts with, such as an HTML comment.
        :param author: The account of the token, whose comment is updated.
        :raises GitHubError: GitHub refused a request.
        """
        content = json.dumps({"body": f"{marker}\n{body}"}).encode()
        comments = f"{API_URL}/repos/{repository}/issues/{number}/comments"
        for page in itertools.count(1):
            url = f"{comments}?per_page={COMMENTS_PER_PAGE}&page={page}"
            listed = self._answer(self._request(url))
            if not isinstance(listed, list):
                msg = f"{url} answered no list"
                raise GitHubError(msg)
            for comment in listed:
                if _is_comment(comment, author, marker):
                    request = self._request(
                        f"{API_URL}/repos/{repository}/issues/comments/{comment['id']}",
                        content,
                        method="PATCH",
                    )
                    return str(self._json(request).get("html_url"))
            if len(listed) < COMMENTS_PER_PAGE:
                break
        return str(self._json(self._request(comments, content), expected=201).get("html_url"))

    def add_comment(self, repository: str, number: int, body: str) -> None:
        """
        Add a comment to an issue or a pull request.

        :param repository: The repository, as owner/name.
        :param number: The number of the issue or pull request.
        :param body: The text of the comment, in Markdown.
        :raises GitHubError: GitHub refused the request.
        """
        content = json.dumps({"body": body}).encode()
        url = f"{API_URL}/repos/{repository}/issues/{number}/comments"
        self._json(self._request(url, content), expected=201)

    def issue(self, repository: str, number: int) -> IssueInfo:
        """
        Return what the registry reads of an issue.

        :param repository: The repository, as owner/name.
        :param number: The number of the issue.
        :raises GitHubError: GitHub refused the request, or answered an issue that cannot be read.
        """
        found = self._json(self._request(f"{API_URL}/repos/{repository}/issues/{number}"))
        try:
            return IssueInfo(
                author=_text(found["user"]["login"]),
                body=_text(found["body"] or ""),
                labels=[_text(label["name"]) for label in found["labels"]],
                open=found["state"] == "open",
                pull_request="pull_request" in found,
            )
        except (KeyError, TypeError) as err:
            msg = f"GitHub answered an issue that cannot be read: {err!r}"
            raise GitHubError(msg) from err

    def pull_requests(self, repository: str) -> list[PullRequest]:
        """
        Return the open pull requests of a repository.

        :param repository: The repository, as owner/name.
        :raises GitHubError: GitHub refused a request, or answered a pull request that cannot be
            read.
        """
        result: list[PullRequest] = []
        pulls = f"{API_URL}/repos/{repository}/pulls?state=open&per_page={PULL_REQUESTS_PER_PAGE}"
        for page in itertools.count(1):
            url = f"{pulls}&page={page}"
            listed = self._answer(self._request(url))
            if not isinstance(listed, list):
                msg = f"{url} answered no list"
                raise GitHubError(msg)
            try:
                result += [_pull_request(pull) for pull in listed]
            except (KeyError, TypeError) as err:
                msg = f"GitHub answered a pull request that cannot be read: {err!r}"
                raise GitHubError(msg) from err
            if len(listed) < PULL_REQUESTS_PER_PAGE:
                break
        return result

    def create_pull_request(
        self, repository: str, *, title: str, branch: str, base: str, body: str
    ) -> str:
        """
        Open a pull request from a branch of the repository itself and return its address.

        :param repository: The repository, as owner/name.
        :param title: The title.
        :param branch: The branch with the changes.
        :param base: The branch to merge into.
        :param body: The text of the pull request, in Markdown.
        :raises GitHubError: GitHub refused the request.
        """
        content = json.dumps({"title": title, "head": branch, "base": base, "body": body})
        request = self._request(f"{API_URL}/repos/{repository}/pulls", content.encode())
        return str(self._json(request, expected=201).get("html_url"))

    def update_pull_request(self, repository: str, number: int, **fields: str) -> None:
        """
        Change a pull request.

        :param repository: The repository, as owner/name.
        :param number: The number of the pull request.
        :param fields: What to set, such as its title, body or state.
        :raises GitHubError: GitHub refused the request.
        """
        url = f"{API_URL}/repos/{repository}/pulls/{number}"
        self._json(self._request(url, json.dumps(fields).encode(), method="PATCH"))

    def branch_commit(self, repository: str, branch: str) -> str | None:
        """
        Return the commit a branch points to, or None when there is no such branch.

        :param repository: The repository, as owner/name.
        :param branch: The name of the branch.
        :raises GitHubError: GitHub refused the request, or answered no commit.
        """
        url = f"{API_URL}/repos/{repository}/git/ref/heads/{urllib.parse.quote(branch)}"
        try:
            found = self._json(self._request(url))
        except NotFoundError:
            return None
        commit = (found.get("object") or {}).get("sha")
        if not isinstance(commit, str) or not is_commit(commit):
            msg = f"{url} answered no commit"
            raise GitHubError(msg)
        return commit

    def set_branch(self, repository: str, branch: str, commit: str) -> None:
        """
        Point a branch at a commit, creating the branch or moving it there whatever it held.

        :param repository: The repository, as owner/name.
        :param branch: The name of the branch.
        :param commit: The full SHA of the commit.
        :raises GitHubError: GitHub refused a request.
        """
        refs = f"{API_URL}/repos/{repository}/git/refs"
        if self.branch_commit(repository, branch) is None:
            content = json.dumps({"ref": f"refs/heads/{branch}", "sha": commit}).encode()
            self._json(self._request(refs, content), expected=201)
            return
        content = json.dumps({"sha": commit, "force": True}).encode()
        url = f"{refs}/heads/{urllib.parse.quote(branch)}"
        self._json(self._request(url, content, method="PATCH"))

    def delete_branch(self, repository: str, branch: str) -> None:
        """
        Delete a branch, unless it is gone already.

        :param repository: The repository, as owner/name.
        :param branch: The name of the branch.
        :raises GitHubError: GitHub refused the request.
        """
        url = f"{API_URL}/repos/{repository}/git/refs/heads/{urllib.parse.quote(branch)}"
        status, answer = self._transport(self._request(url, method="DELETE"))
        with answer:
            body = _read(answer, 1000)
        # 422 is the answer for a branch that does not exist
        if status not in {204, 404, 422}:
            msg = f"{url} answered {status}: {body[:500]!r}"
            raise GitHubError(msg)

    def put_file(
        self, repository: str, branch: str, path: str, content: bytes, message: str
    ) -> None:
        """
        Commit a file that does not exist on a branch yet.

        :param repository: The repository, as owner/name.
        :param branch: The name of the branch.
        :param path: The path of the file in the repository.
        :param content: The content of the file.
        :param message: The commit message.
        :raises GitHubError: GitHub refused the request.
        """
        data = {
            "message": message,
            "content": base64.b64encode(content).decode(),
            "branch": branch,
        }
        url = f"{API_URL}/repos/{repository}/contents/{urllib.parse.quote(path)}"
        self._json(self._request(url, json.dumps(data).encode(), method="PUT"), expected=201)

    def login(self) -> str:
        """
        Return the handle of the account the token belongs to.

        :raises GitHubError: GitHub refused the request.
        """
        if self._login is None:
            self._login = str(self._json(self._request(f"{API_URL}/user")).get("login"))
        return self._login

    def issue_exists(self, repository: str, key: str) -> bool:
        """
        Return whether the account of the token opened an issue with a key on a repository.

        The issue may be open or closed; the key is in its body.

        :param repository: The repository, as owner/name.
        :param key: The key of the issue.
        :raises GitHubError: GitHub refused the request.
        """
        search = f'"{key}" repo:{repository} is:issue in:body author:{self.login()}'
        query = urllib.parse.urlencode({"q": search})
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

    def _graphql(
        self, query: str, variables: dict[str, str]
    ) -> tuple[dict[str, Any], dict[str, str]]:
        """
        Run a GraphQL query and return its data and the errors by the field they are about.

        A repository that does not exist is None in the data and no error.

        :param query: The query.
        :param variables: The values of its variables.
        :raises GitHubError: GitHub refused the query, or reported an error about no field.
        """
        content = json.dumps({"query": query, "variables": variables}).encode()
        answer = self._json(self._request(GRAPHQL_URL, content))
        data = answer.get("data")
        errors: dict[str, str] = {}
        for error in answer.get("errors") or []:
            path = error.get("path") if isinstance(error, dict) else None
            # a repository that does not exist, or is not public, is an error of its own and null
            if isinstance(path, list) and path and isinstance(path[0], str):
                if error.get("type") != "NOT_FOUND":
                    errors.setdefault(path[0], str(error.get("message") or error)[:500])
            else:
                data = None
        if not isinstance(data, dict):
            msg = f"GitHub refused the query: {str(answer)[:500]}"
            raise GitHubError(msg)
        return data, errors

    def _json(self, request: urllib.request.Request, expected: int = 200) -> dict[str, Any]:
        """
        Send a request to the API and return the JSON object it answers.

        :param request: The request.
        :param expected: The status of a successful answer.
        :raises NotFoundError: GitHub answered that it does not exist.
        :raises GitHubError: GitHub answered another status or no JSON object.
        """
        data = self._answer(request, expected)
        if not isinstance(data, dict):
            msg = f"{request.full_url} answered no JSON object"
            raise GitHubError(msg)
        return data

    def _answer(self, request: urllib.request.Request, expected: int = 200) -> Any:
        """
        Send a request to the API and return the JSON it answers.

        :param request: The request.
        :param expected: The status of a successful answer.
        :raises NotFoundError: GitHub answered that it does not exist.
        :raises GitHubError: GitHub answered another status or no JSON.
        """
        status, answer = self._transport(request)
        with answer:
            body = _read(answer, _MAX_ANSWER_SIZE + 1)
        if status != expected:
            msg = f"{request.full_url} answered {status}: {body[:500]!r}"
            raise (NotFoundError if status == 404 else GitHubError)(msg)  # noqa: PLR2004
        if len(body) > _MAX_ANSWER_SIZE:
            msg = f"{request.full_url} answered more than {_MAX_ANSWER_SIZE} bytes"
            raise GitHubError(msg)
        try:
            return json.loads(body)
        except ValueError as err:
            msg = f"{request.full_url} answered no JSON: {err}"
            raise GitHubError(msg) from err

    def _request(
        self,
        url: str,
        content: bytes | None = None,
        *,
        authenticated: bool = True,
        method: str | None = None,
    ) -> urllib.request.Request:
        """
        Return a request to GitHub, a POST when it has content and no other method.

        :param url: The address.
        :param content: The JSON body.
        :param authenticated: Whether to send the token.
        :param method: The HTTP method.
        """
        headers = {"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"}
        if authenticated and self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        if content is not None:
            headers["Content-Type"] = "application/json"
        # https only
        return urllib.request.Request(url, content, headers, method=method)  # noqa: S310


def post_issue(issue: Issue, github: GitHub) -> str | None:
    """
    Open an issue on GitHub, unless the same account opened an issue with its key before.

    Returns the address of the new issue, or None when it was reported before.

    :param issue: The issue.
    :param github: A client whose token may open issues on the repository of the issue.
    :raises GitHubError: GitHub refused a request.
    :raises OSError: A request could not be sent.
    """
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
            name=node["nameWithOwner"],
            owner=node["owner"]["login"],
            repository_id=_id(node["databaseId"]),
            owner_id=_id(node["owner"]["databaseId"]),
            private=node["isPrivate"],
            archived=node["isArchived"],
            has_issues=node["hasIssuesEnabled"],
            license=(node["licenseInfo"] or {}).get("key"),
            releases=releases,
        )
    except (KeyError, TypeError, AttributeError) as err:
        msg = f"GitHub answered a repository that cannot be read: {err!r}"
        raise GitHubError(msg) from err


def _is_comment(comment: object, author: str, marker: str) -> bool:
    """Return whether a comment as the API answers it is by an account and starts with a marker."""
    if not isinstance(comment, dict):
        return False
    user, body = comment.get("user"), comment.get("body")
    return (
        isinstance(user, dict)
        and type(comment.get("id")) is int
        and user.get("login") == author
        and isinstance(body, str)
        and body.startswith(marker)
    )


def _pull_request(pull: dict[str, Any]) -> PullRequest:
    """
    Return a pull request as the API answers it.

    :param pull: The pull request in the answer.
    :raises KeyError: A field is missing.
    :raises TypeError: A field is of another type.
    """
    head_repository = pull["head"]["repo"]
    return PullRequest(
        number=_id(pull["number"]),
        url=_text(pull["html_url"]),
        author=_text(pull["user"]["login"]),
        head_repository=None if head_repository is None else _text(head_repository["full_name"]),
        branch=_text(pull["head"]["ref"]),
        body=_text(pull["body"] or ""),
    )


def _id(value: object) -> int:
    """
    Return an id GitHub answered.

    :param value: The id in the answer.
    :raises TypeError: It is no id.
    """
    if type(value) is not int:
        msg = f"{value!r} is no id"
        raise TypeError(msg)
    return value


def _text(value: object) -> str:
    """
    Return a text GitHub answered.

    :param value: The text in the answer.
    :raises TypeError: It is no text.
    """
    if not isinstance(value, str):
        msg = f"{value!r} is no text"
        raise TypeError(msg)
    return value


def _read(body: BinaryIO, size: int) -> bytes:
    """
    Return up to size bytes of the body of a response.

    :param body: The body.
    :param size: The most bytes to read.
    :raises GitHubError: The response broke off.
    """
    try:
        return body.read(size)
    except http.client.HTTPException as err:
        msg = f"the answer broke off: {err!r}"
        raise GitHubError(msg) from err
