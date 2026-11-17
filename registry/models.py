"""The files of the registry and the index it publishes."""

from __future__ import annotations

import typing
from collections.abc import Callable
from dataclasses import dataclass, field, fields
from enum import StrEnum
from typing import Any

from mashumaro import DataClassDictMixin
from mashumaro.config import BaseConfig

# the version of the index format; the server reads only the schema it knows
INDEX_SCHEMA = 1


def _exactly[T](kind: type[T]) -> Callable[[object], T]:
    """Return a deserializer that takes a value of exactly this type, so "1" is no int."""

    def deserialize(value: object) -> T:
        if type(value) is not kind:
            msg = f"expected {kind.__name__}, got {type(value).__name__}"
            raise TypeError(msg)
        return value

    return deserialize


class StrictModel(DataClassDictMixin):
    """A model that refuses unknown keys and values of another type than declared."""

    class Config(BaseConfig):
        """Refuse what is not exactly as declared."""

        forbid_extra_keys = True
        serialization_strategy = {  # noqa: RUF012 - read by mashumaro
            str: {"deserialize": _exactly(str)},
            int: {"deserialize": _exactly(int)},
            bool: {"deserialize": _exactly(bool)},
        }

    @classmethod
    def __pre_deserialize__(cls, d: Any) -> dict[Any, Any]:
        """Refuse a string where a list is declared, which mashumaro would split into letters."""
        if not isinstance(d, dict):
            msg = f"expected an object, got {type(d).__name__}"
            raise TypeError(msg)
        hints = typing.get_type_hints(cls)
        for item in fields(cls):  # type: ignore[arg-type]
            value = d.get(item.name, [])
            if typing.get_origin(hints[item.name]) is list and not isinstance(value, list):
                msg = f'Field "{item.name}" must be a list'
                raise TypeError(msg)
        return d


class ProviderStatus(StrEnum):
    """Whether the catalog offers a provider."""

    LISTED = "listed"
    # offered no longer; what is installed keeps loading
    WITHDRAWN = "withdrawn"
    # may not load at all
    BLOCKED = "blocked"


@dataclass
class ProviderFile(StrictModel):
    """`providers/<domain>/provider.json`: a provider, written at admission."""

    class Config(StrictModel.Config):
        """Write only the keys that differ from their default."""

        omit_default = True

    domain: str
    # owner/name of the repository on GitHub
    repository: str
    # the ids GitHub gives the repository and its owner, which a rename or transfer keeps and a
    # new repository under the same name does not have
    repository_id: int
    owner_id: int
    # the folder of the provider in the repository, named after the domain or provider; the server
    # installs it under the domain
    folder: str
    # when the provider entered the catalog
    added: str
    status: ProviderStatus = ProviderStatus.LISTED
    # what the release tags of this provider start with, for a repository with several providers
    tag_prefix: str = ""
    reason: str | None = None
    reason_url: str | None = None
    # the reason, by version
    blocked_versions: dict[str, str] = field(default_factory=dict)


@dataclass
class CheckResult(StrictModel):
    """The result of the checks of a version on one server line."""

    passed: bool
    checked: str
    # why the version did not pass
    reason: str | None = None


@dataclass
class VersionFile(StrictModel):
    """`providers/<domain>/versions/<version>.json`: a released version, generated."""

    version: str
    tag: str
    commit: str
    # the folder of the provider in the repository at this commit
    folder: str
    content_hash: str
    # manifest.json of the provider as it is in the release
    manifest: dict[str, Any]
    requirements: list[str]
    released: str
    icon_svg: str | None = None
    # the result by server line
    checks: dict[str, CheckResult] = field(default_factory=dict)


@dataclass
class RefusedRelease(StrictModel):
    """`providers/<domain>/refused/<version>.json`: a release that did not become a version."""

    version: str
    tag: str
    commit: str
    released: str
    refused: str
    reason: str


@dataclass
class ServerLine(StrictModel):
    """A server line an index is built for."""

    # the official server image of the line, which the checks of a version run in
    image: str
    # a frozen line is no longer tested; blocks still reach its index
    frozen: bool = False


@dataclass
class ServerLines(StrictModel):
    """`server_lines.json`: the server lines an index is built for."""

    lines: dict[str, ServerLine]


@dataclass
class ReservedDomains(StrictModel):
    """`reserved_domains.json`: domains a community provider cannot take."""

    # the providers the server ships
    builtin_providers: list[str]
    # the core controllers of the server, which share the namespace of provider domains
    core_controllers: list[str]
    # names the server gives a meaning of its own, such as library
    reserved_names: list[str]
    # built-in providers that move to the community catalog under their own domain
    moving: list[str] = field(default_factory=list)


# The files the steps of the release pipeline hand each other in the work folder.


@dataclass
class VersionResult(StrictModel):
    """The result of the checks of one version."""

    domain: str
    version: str
    passed: bool
    # why the version did not pass, in one line
    reason: str | None = None
    # the full output of the check that failed, for the maintainer
    output: str | None = None
    # the version passed on this line before, so a failure means it stopped passing
    passed_before: bool = False
    # the check could not run in the server image of the line, for a reason that is not the
    # provider's; the reason and output say why
    unavailable: bool = False


@dataclass
class LineResults(StrictModel):
    """The results file the check step writes for one server line."""

    line: str
    checked: str
    results: list[VersionResult]


@dataclass
class RepositoryState(StrictModel):
    """What the fetch step learned of a repository on GitHub."""

    owner: str
    repository_id: int
    owner_id: int
    private: bool
    archived: bool
    has_issues: bool
    # the key of the licence GitHub recognises, "other" for one it does not
    license: str | None


@dataclass
class ProviderVersion(StrictModel):
    """A version of a provider."""

    domain: str
    version: str


@dataclass
class FetchReport(StrictModel):
    """`fetch.json`: what the fetch step found, for the report."""

    # by owner/name
    repositories: dict[str, RepositoryState]
    # the releases refused in this run
    refused: list[ProviderVersion]


@dataclass
class CheckPlan(StrictModel):
    """`plan.json`: the versions the check step checks, by server line."""

    lines: dict[str, list[ProviderVersion]]


@dataclass
class Finding(StrictModel):
    """The outcome of one admission check."""

    passed: bool
    # our own words
    text: str
    # what the provider or its repository holds, shown as code
    detail: str | None = None


@dataclass
class AdmissionReport(StrictModel):
    """`admission.json`: the admission checks of a submission before its release is checked."""

    # when the checks ran
    checked: str
    findings: list[Finding]
    # the digest of the text of the issue that was checked
    issue_digest: str
    # what was checked, as far as the submission got: the repository as owner/name, the domain
    # of its provider and the latest release
    repository: str | None = None
    domain: str | None = None
    tag: str | None = None
    commit: str | None = None
    # the version of the latest release, when it became a version file, and its name
    version: str | None = None
    name: str | None = None
    # the provider.json to add when every check passes
    provider: ProviderFile | None = None
    # why the issue is no valid submission, so nothing was checked
    refusal: str | None = None


@dataclass
class Failure(StrictModel):
    """A failure of a provider that its maintainers are told about."""

    # the same failure always has the same key, such as "check 1.2.0 beta", so it is told once
    key: str
    heading: str
    # what failed and what it means, in Markdown
    text: str
    # the end of the output of a check that failed, shown as code
    output: str | None = None


@dataclass
class ProviderReport(StrictModel):
    """`issues/<domain>.json`: what the issue of a provider on the registry says and settles."""

    domain: str
    title: str
    # the GitHub handles mentioned in a new issue
    maintainers: list[str]
    # the failures of this run that are not settled
    failures: list[Failure]
    # the highest version of the provider, with a version file or refused
    newest: str | None
    # the server lines tested in this run, and those the newest version passed on
    tested: list[str]
    passed: list[str]
    # whether GitHub gives the repository the ids of provider.json
    admitted: bool


@dataclass
class LineReport(StrictModel):
    """`issues/lines/<line>.json`: whether the checks could run on a server line in this run."""

    line: str
    # why the checks could not run in the server image of the line; None when they ran
    reason: str | None = None
    # the output of a check that could not run
    output: str | None = None


# The index, in the shape the server reads it: CatalogIndex and the classes below it in
# music_assistant/controllers/community_catalog/models.py of the server.


@dataclass
class IndexVersion(DataClassDictMixin):
    """A version of a provider in the index."""

    commit: str
    folder: str
    content_hash: str
    requirements: list[str]
    released: str


@dataclass
class IndexProvider(DataClassDictMixin):
    """A provider in the index."""

    status: ProviderStatus
    repository: str
    manifest: dict[str, Any]
    added: str
    reason: str | None
    reason_url: str | None
    maintainers: list[str]
    icon_svg: str | None
    latest: str | None
    versions: dict[str, IndexVersion]
    blocked_versions: dict[str, str]


@dataclass
class CatalogIndex(DataClassDictMixin):
    """The index of the catalog for one server line."""

    schema: int
    server_line: str
    generated: str
    providers: dict[str, IndexProvider]
