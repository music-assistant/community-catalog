"""The command line of the registry: python -m registry <command>."""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path

from registry.checks import check_version
from registry.content import ProviderContent, read_archive, read_folder
from registry.files import dumps, load_model
from registry.formats import format_date
from registry.icon import check_icon
from registry.index import render_indexes, stale_index_files
from registry.models import CheckResult, ReservedDomains, VersionFile
from registry.registry import (
    RESERVED_DOMAINS_FILE,
    InvalidRegistryError,
    Registry,
    load_registry,
)
from registry.reserved import add_server_domains


class CommandError(Exception):
    """A command cannot be carried out; the message says why."""


def main(argv: list[str] | None = None) -> int:
    """
    Run a command of the registry and return the exit code: 0 when it succeeded, 1 otherwise.

    :param argv: The arguments, those of the process when omitted.
    """
    args = _parser().parse_args(argv)
    try:
        args.run(args)
    except InvalidRegistryError as err:
        for problem in err.problems:
            print(problem, file=sys.stderr)
        print(f"{len(err.problems)} problem(s) found", file=sys.stderr)
        return 1
    except CommandError as err:
        print(f"error: {err}", file=sys.stderr)
        return 1
    return 0


def _validate(args: argparse.Namespace) -> None:
    """Check every file of the registry, and that the index files are up to date."""
    registry = load_registry(args.root)
    if problems := stale_index_files(registry, render_indexes(registry, _now())):
        problems.append("run `python -m registry build` to update the index files")
        raise InvalidRegistryError(problems)
    print(f"{len(registry.providers)} provider(s) valid, index files up to date")


def _build(args: argparse.Namespace) -> None:
    """Write the index file of every server line."""
    registry = load_registry(args.root)
    for path, content in render_indexes(registry, _now()).items():
        if path.is_file() and path.read_bytes() == content:
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        print(f"wrote {path.relative_to(registry.root).as_posix()}")


def _add_version(args: argparse.Namespace) -> None:
    """Write the version file of a release, from its provider folder or its GitHub archive."""
    registry = load_registry(args.root)
    if (provider := registry.providers.get(args.domain)) is None:
        msg = f"there is no provider {args.domain}"
        raise CommandError(msg)
    path = registry.version_path(args.domain, args.version)
    if path.exists():
        msg = f"{args.domain} {args.version} exists already"
        raise CommandError(msg)
    content = _read_source(args.source, provider.file.folder)
    icon_svg = content.icon_svg
    if icon_svg is not None:
        try:
            check_icon(icon_svg)
        except ValueError as err:
            print(f"warning: the icon is left out: {err}", file=sys.stderr)
            icon_svg = None
    requirements = content.manifest.get("requirements", [])
    version = VersionFile(
        version=args.version,
        tag=args.tag,
        commit=args.commit,
        folder=provider.file.folder,
        content_hash=content.content_hash,
        manifest=content.manifest,
        requirements=requirements if isinstance(requirements, list) else [],
        released=args.released or format_date(_now()),
        icon_svg=icon_svg,
    )
    _write_version(registry, args.domain, version)
    print(f"content hash {content.content_hash}")


def _record_check(args: argparse.Namespace) -> None:
    """Record the result of the checks of a version on a server line."""
    registry = load_registry(args.root)
    if (provider := registry.providers.get(args.domain)) is None:
        msg = f"there is no provider {args.domain}"
        raise CommandError(msg)
    if (version := provider.versions.get(args.version)) is None:
        msg = f"{args.domain} has no version {args.version}"
        raise CommandError(msg)
    version.checks[args.line] = CheckResult(
        passed=args.failed is None,
        checked=args.checked or format_date(_now()),
        reason=args.failed,
    )
    _write_version(registry, args.domain, version)


def _content_hash(args: argparse.Namespace) -> None:
    """Print the content hash of a provider folder or of a folder in a GitHub archive."""
    print(_read_source(args.source, args.folder).content_hash)


def _refresh_reserved(args: argparse.Namespace) -> None:
    """Add the built-in providers and core controllers of server checkouts to the reserved list."""
    path = args.root / RESERVED_DOMAINS_FILE
    try:
        reserved = load_model(path, ReservedDomains)
        for server in args.servers:
            reserved = add_server_domains(reserved, server)
    except (ValueError, OSError) as err:
        raise CommandError(str(err)) from err
    path.write_bytes(dumps(reserved.to_dict()))
    print(f"wrote {RESERVED_DOMAINS_FILE}")


def _read_source(source: Path, folder: str | None) -> ProviderContent:
    """
    Return the content of a provider folder, or of a folder in a GitHub archive.

    :param source: A provider folder, or a downloaded archive of a commit.
    :param folder: The folder of the provider in the repository, needed for an archive.
    :raises CommandError: The source cannot be read or holds what a server would not install.
    """
    if source.is_file() and folder is None:
        raise CommandError("--folder is needed to read an archive")
    try:
        if source.is_dir():
            return read_folder(source)
        if source.is_file() and folder is not None:
            return read_archive(source, folder)
    except (ValueError, OSError) as err:
        msg = f"{source}: {err}"
        raise CommandError(msg) from err
    msg = f"{source} is no folder or archive"
    raise CommandError(msg)


def _write_version(registry: Registry, domain: str, version: VersionFile) -> None:
    """
    Write a version file after checking it.

    :param registry: The registry.
    :param domain: The domain of the provider.
    :param version: The version.
    :raises InvalidRegistryError: The version is not valid.
    """
    path = registry.version_path(domain, version.version)
    where = path.relative_to(registry.root).as_posix()
    problems = check_version(version, registry.providers[domain].file, registry.server_lines)
    if problems:
        raise InvalidRegistryError([f"{where}: {problem}" for problem in problems])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(dumps(version.to_dict()))
    print(f"wrote {where}")


def _now() -> datetime:
    """Return the current moment."""
    return datetime.now(UTC)


def _parser() -> argparse.ArgumentParser:
    """Return the parser of the command line."""
    parser = argparse.ArgumentParser(prog="python -m registry", description=__doc__)
    parser.add_argument(
        "--root", type=Path, default=Path(), help="root folder of the registry (default: .)"
    )
    commands = parser.add_subparsers(required=True, metavar="command")

    command = commands.add_parser("validate", help=_validate.__doc__)
    command.set_defaults(run=_validate)

    command = commands.add_parser("build", help=_build.__doc__)
    command.set_defaults(run=_build)

    command = commands.add_parser("add-version", help=_add_version.__doc__)
    command.add_argument("domain")
    command.add_argument("version", help="MAJOR.MINOR.PATCH")
    command.add_argument("source", type=Path, help="the provider folder, or a .tar.gz archive")
    command.add_argument("--tag", required=True, help="the tag of the release")
    command.add_argument("--commit", required=True, help="the full SHA of the tagged commit")
    command.add_argument("--released", help="when it was released (default: now)")
    command.set_defaults(run=_add_version)

    command = commands.add_parser("record-check", help=_record_check.__doc__)
    command.add_argument("domain")
    command.add_argument("version")
    command.add_argument("line", help="the server line, as in server_lines.json")
    result = command.add_mutually_exclusive_group(required=True)
    result.add_argument("--passed", action="store_true", help="the version passed")
    result.add_argument("--failed", metavar="REASON", help="the version failed, and why")
    command.add_argument("--checked", help="when it was checked (default: now)")
    command.set_defaults(run=_record_check)

    command = commands.add_parser("content-hash", help=_content_hash.__doc__)
    command.add_argument("source", type=Path, help="the provider folder, or a .tar.gz archive")
    command.add_argument("--folder", help="the folder of the provider in the archive")
    command.set_defaults(run=_content_hash)

    command = commands.add_parser("refresh-reserved", help=_refresh_reserved.__doc__)
    command.add_argument("servers", nargs="+", type=Path, help="checkouts of the server")
    command.set_defaults(run=_refresh_reserved)
    return parser
