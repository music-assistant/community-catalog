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
class Admission(StrictModel):
    """The decision that admitted a provider to the catalog."""

    # GitHub handle of the person who decided
    decided_by: str
    decided: str
    # the pull request on the registry that added the provider
    pull_request: int


@dataclass
class ProviderFile(StrictModel):
    """`providers/<domain>/provider.json`: a provider, written at admission."""

    domain: str
    # owner/name of the repository on GitHub
    repository: str
    # the folder of the provider in the repository, named after the domain
    folder: str
    maintainers: list[str]
    status: ProviderStatus
    # when the provider entered the catalog
    added: str
    admission: Admission
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
class ServerLine(StrictModel):
    """A server line an index is built for."""

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
