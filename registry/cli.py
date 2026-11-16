"""The command line of the registry: python -m registry <command>."""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
from pathlib import Path

from registry.admission import (
    COMMENT_MARKER,
    Outcome,
    Proposal,
    admission_comment,
    check_admission,
    prepare_admission,
    propose_admission,
)
from registry.edit import (
    CommandError,
    add_version,
    block_version,
    get_provider,
    now,
    read_source,
    record_check,
    set_status,
    write_indexes,
)
from registry.fetch import fetch
from registry.files import dumps, load_model
from registry.formats import format_date
from registry.github import ACTIONS_LOGIN, GitHub
from registry.index import render_indexes, stale_index_files
from registry.models import (
    AdmissionReport,
    FetchReport,
    LineResults,
    ProviderReport,
    ProviderStatus,
    ReservedDomains,
    VersionResult,
)
from registry.pipeline import (
    ADMISSION_REPORT,
    FETCH_REPORT,
    ISSUES_FOLDER,
    apply_results,
    archive_path,
    read_plan,
    read_results,
)
from registry.registry import (
    REFUSED_FOLDER,
    RESERVED_DOMAINS_FILE,
    VERSIONS_FOLDER,
    InvalidRegistryError,
    load_registry,
)
from registry.report import build_reports, open_issues, post_report
from registry.reserved import add_server_domains

# the token the commands that talk to GitHub read from the environment
TOKEN_VARIABLE = "GITHUB_TOKEN"  # noqa: S105 - the name, not a token
_PULL_REQUEST_URL = re.compile(r"https://github\.com/[A-Za-z0-9-]+/[A-Za-z0-9._-]+/pull/[0-9]+")


def main(argv: list[str] | None = None) -> int:
    """
    Run a command of the registry and return the exit code: 0 when it succeeded, 1 otherwise.

    :param argv: The arguments, those of the process when omitted.
    """
    args = _parser().parse_args(argv)
    if "work" in args and args.work is None:
        args.work = args.root / "work"
    try:
        return args.run(args) or 0
    except InvalidRegistryError as err:
        for problem in err.problems:
            print(problem, file=sys.stderr)
        print(f"{len(err.problems)} problem(s) found", file=sys.stderr)
    except CommandError as err:
        print(f"error: {err}", file=sys.stderr)
    except OSError as err:
        print(f"error: {err}", file=sys.stderr)
    return 1


def _validate(args: argparse.Namespace) -> None:
    """Check every file of the registry, and that the index files are up to date."""
    registry = load_registry(args.root)
    if problems := stale_index_files(registry, render_indexes(registry, now())):
        problems.append("run `python -m registry build` to update the index files")
        raise InvalidRegistryError(problems)
    print(f"{len(registry.providers)} provider(s) valid, index files up to date")


def _build(args: argparse.Namespace) -> None:
    """Write the index file of every server line."""
    write_indexes(args.root)


def _add_version(args: argparse.Namespace) -> None:
    """Write the version file of a release, from its provider folder or its GitHub archive."""
    file = add_version(
        load_registry(args.root),
        args.domain,
        args.version,
        args.source,
        tag=args.tag,
        commit=args.commit,
        released=args.released,
    )
    print(f"content hash {file.content_hash}")


def _record_check(args: argparse.Namespace) -> None:
    """Record the result of the checks of a version on a server line."""
    registry = load_registry(args.root)
    record_check(
        registry, args.domain, args.version, args.line, reason=args.failed, checked=args.checked
    )


def _content_hash(args: argparse.Namespace) -> None:
    """Print the content hash of a provider folder or of a folder in a GitHub archive."""
    print(read_source(args.source, args.folder).content_hash)


def _refresh_reserved(args: argparse.Namespace) -> None:
    """Add the built-in providers and core controllers of server checkouts to the reserved list."""
    path = args.root / RESERVED_DOMAINS_FILE
    if path.is_symlink():
        msg = f"{RESERVED_DOMAINS_FILE}: is a symbolic link"
        raise CommandError(msg)
    try:
        reserved = load_model(path, ReservedDomains)
        for server in args.servers:
            reserved = add_server_domains(reserved, server)
        content = dumps(reserved.to_dict())
    except (ValueError, OSError) as err:
        raise CommandError(str(err)) from err
    path.write_bytes(content)
    print(f"wrote {RESERVED_DOMAINS_FILE}")


def _fetch(args: argparse.Namespace) -> None:
    """Write a version file for the newest release of every provider, and download the archives."""
    fetch(load_registry(args.root), GitHub(_token()), args.work)


def _check(args: argparse.Namespace) -> None:
    """Run a stage of the checks of a version, inside the server image of a line."""
    # the server is installed in its image only
    from registry.check import CheckUnavailableError, install_version, load_version  # noqa: PLC0415

    registry = load_registry(args.root)
    if (line := registry.server_lines.get(args.line)) is None or line.frozen:
        msg = f"{args.line!r} is not a server line that is tested"
        raise CommandError(msg)
    name = f"{args.domain} {args.version}"
    if (file := get_provider(registry, args.domain).versions.get(args.version)) is None:
        msg = f"there is no version {name}"
        raise CommandError(msg)
    if not (archive := archive_path(args.work, args.domain, args.version)).is_file():
        msg = f"{name}: its archive is not in the work folder"
        raise CommandError(msg)
    if args.stage == "install":
        try:
            outcome = asyncio.run(install_version(archive, file, args.domain, args.scratch))
        except CheckUnavailableError as err:
            msg = f"{name}: the check could not run on {args.line}: {err}"
            raise CommandError(msg) from err
        if outcome is None:
            print(f"{name}: ready to load")
            return
    else:
        outcome = asyncio.run(load_version(archive, file, args.domain, args.scratch))
    previous = file.checks.get(args.line)
    result = VersionResult(
        domain=args.domain,
        version=args.version,
        passed=outcome.passed,
        reason=outcome.reason,
        output=outcome.output,
        passed_before=previous is not None and previous.passed,
    )
    results = LineResults(line=args.line, checked=format_date(now()), results=[result])
    args.output.write_bytes(dumps(results.to_dict()))
    print(f"{name}: {'passed' if outcome.passed else outcome.reason}")


def _check_folder(args: argparse.Namespace) -> int:
    """Run the checks on a provider folder, inside a server image, as a provider's own CI does."""
    from registry.check import check_folder  # noqa: PLC0415

    reserved = load_registry(args.root).reserved
    outcome = asyncio.run(check_folder(args.folder.resolve(), reserved))
    if outcome.passed:
        print(f"{args.folder.resolve().name}: passed")
        return 0
    print(outcome.output, file=sys.stderr)
    print(f"{args.folder.resolve().name}: {outcome.reason}", file=sys.stderr)
    return 1


def _apply_results(args: argparse.Namespace) -> None:
    """Record the results of the check step that follow the plan, and write the index files."""
    apply_results(load_registry(args.root), read_results(args.results, read_plan(args.work)))


def _report(args: argparse.Namespace) -> None:
    """Write the report of every provider: its failures in this run and what settles them."""
    fetched = _load(args.work / FETCH_REPORT, FetchReport)
    results = read_results(args.results, read_plan(args.work))
    reports = build_reports(load_registry(args.root), fetched, results)
    folder = args.work / ISSUES_FOLDER
    folder.mkdir(parents=True, exist_ok=True)
    for report in reports:
        (folder / f"{report.domain}.json").write_bytes(dumps(report.to_dict()))
        for failure in report.failures:
            print(f"{report.domain}: {failure.heading}")
    print(f"{len(reports)} report(s) written to {folder}")


def _post_issues(args: argparse.Namespace) -> int:
    """Open, add to or close the issue of each provider on the registry, after its report."""
    github = GitHub(_token())
    issues = open_issues(github, args.repository)
    failed = 0
    for path in sorted((args.work / ISSUES_FOLDER).glob("*.json")):
        report = _load(path, ProviderReport)
        try:
            done = post_report(report, issues.get(report.domain), github, args.repository)
        except OSError as err:
            failed += 1
            print(f"warning: {report.domain}: {err}", file=sys.stderr)
            continue
        if done is not None:
            print(f"{report.domain}: {done}")
    return 1 if failed else 0


def _check_changes(args: argparse.Namespace) -> int:
    """Refuse a pull request that writes what only the release pipeline writes."""
    paths = args.changed.read_bytes().decode(errors="replace").split("\0")
    generated = sorted(
        path
        for path in paths
        if (parts := path.split("/"))[0] == "providers"
        and len(parts) > 2  # noqa: PLR2004 - providers/<domain>/<folder>
        and parts[2] in {VERSIONS_FOLDER, REFUSED_FOLDER}
    )
    for path in generated:
        print(f"{path!r}: only the release pipeline writes version files and refusals")
    return 1 if generated else 0


def _prepare_admission(args: argparse.Namespace) -> None:
    """Run the admission checks of a submission that need GitHub and take its latest release."""
    report = prepare_admission(
        args.root,
        repository=args.repository,
        issue=args.issue,
        github=GitHub(_token()),
        work=args.work,
    )
    path = args.work / ADMISSION_REPORT
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(dumps(report.to_dict()))
    if report.refusal is not None:
        print(f"refused: {report.refusal!r}")
    for finding in report.findings:
        detail = f": {finding.detail!r}" if finding.detail is not None else ""
        print(f"{'passed' if finding.passed else 'failed'}: {finding.text}{detail}")


def _check_admission(args: argparse.Namespace) -> int:
    """Print the result of the admission checks of a submission, with the checks of its release."""
    report, results = _admission(args)
    passed, text = check_admission(
        load_registry(args.root),
        report,
        results,
        run_url=args.run_url,
        proposal=Proposal(Outcome(args.outcome), args.pull_request or None, args.others.split())
        if args.proposal == "success" and args.outcome
        else None,
        proposal_failed=args.proposal == "failure",
    )
    print(text)
    if args.comment is not None:
        args.comment.write_text(admission_comment(text, args.run_url), encoding="utf-8")
    return 0 if passed else 1


def _post_admission_comment(args: argparse.Namespace) -> None:
    """Write the result of the admission checks as the one comment of them on the issue."""
    body = args.comment.read_text(encoding="utf-8")
    github = GitHub(_token())
    url = github.set_comment(
        args.repository, args.issue, body, marker=COMMENT_MARKER, author=ACTIONS_LOGIN
    )
    print(url)


def _propose_admission(args: argparse.Namespace) -> None:
    """Open or update the pull request that adds a provider whose checks passed, or close it."""
    report, results = _admission(args)
    proposal = propose_admission(
        load_registry(args.root),
        report,
        results,
        repository=args.repository,
        issue=args.issue,
        author=args.author,
        run_url=args.run_url,
        github=GitHub(_token()),
    )
    print(f"{proposal.outcome}: {proposal.url or 'no pull request'}")
    for url in proposal.others:
        print(f"another submission for the domain: {url}")
    if args.output is not None:
        # the outputs of the job, one line each; addresses GitHub answered, checked all the same
        urls = [url for url in [proposal.url or "", *proposal.others] if url]
        if not all(_PULL_REQUEST_URL.fullmatch(url) for url in urls):
            msg = f"GitHub answered an address that is not a pull request: {urls!r}"
            raise CommandError(msg)
        with args.output.open("a", encoding="utf-8") as output:
            output.write(
                f"outcome={proposal.outcome}\nurl={proposal.url or ''}\n"
                f"others={' '.join(proposal.others)}\n"
            )


def _set_status(args: argparse.Namespace) -> None:
    """Change whether the catalog offers a provider, and write the index files."""
    registry = load_registry(args.root)
    set_status(registry, args.domain, ProviderStatus(args.status), args.reason, args.reason_url)
    write_indexes(args.root)


def _block_version(args: argparse.Namespace) -> None:
    """Block a version of a provider, so no server loads it, and write the index files."""
    block_version(load_registry(args.root), args.domain, args.version, args.reason)
    write_indexes(args.root)


def _unblock_version(args: argparse.Namespace) -> None:
    """Lift the block of a version of a provider, and write the index files."""
    block_version(load_registry(args.root), args.domain, args.version, None)
    write_indexes(args.root)


def _admission(args: argparse.Namespace) -> tuple[AdmissionReport | None, list[LineResults]]:
    """
    Return the report of the prepare step and the results of the check step.

    The report is None, and there are no results, when the prepare step did not finish.

    :param args: The arguments, with the work folder and the folder of the results files.
    """
    try:
        report = _load(args.work / ADMISSION_REPORT, AdmissionReport)
        return report, read_results(args.results, read_plan(args.work))
    except CommandError as err:
        print(f"warning: the admission checks did not run: {err}", file=sys.stderr)
        return None, []


def _token() -> str:
    """
    Return the GitHub token of the environment.

    :raises CommandError: There is none.
    """
    if not (token := os.environ.get(TOKEN_VARIABLE)):
        msg = f"set {TOKEN_VARIABLE} to a GitHub token"
        raise CommandError(msg)
    return token


def _load[T: (FetchReport, ProviderReport, AdmissionReport)](path: Path, model: type[T]) -> T:
    """
    Return a file of the work folder.

    :param path: The file.
    :param model: The model it holds.
    :raises CommandError: The file is missing or not exactly this model.
    """
    try:
        return load_model(path, model)
    except (ValueError, OSError) as err:
        msg = f"{path}: {err}"
        raise CommandError(msg) from err


def _pull_request_url(value: str) -> str:
    """
    Return the address of a pull request on GitHub, or an empty value as it is.

    :param value: The value of the argument.
    :raises argparse.ArgumentTypeError: It is another address.
    """
    if value and not _PULL_REQUEST_URL.fullmatch(value):
        msg = f"{value!r} is not the address of a pull request on GitHub"
        raise argparse.ArgumentTypeError(msg)
    return value


def _pull_request_urls(value: str) -> str:
    """
    Return addresses of pull requests on GitHub separated by spaces, or an empty value.

    :param value: The value of the argument.
    :raises argparse.ArgumentTypeError: One is another address.
    """
    for url in value.split():
        _pull_request_url(url)
    return value


def _parser() -> argparse.ArgumentParser:  # noqa: PLR0915 - a statement per argument
    """Return the parser of the command line."""
    parser = argparse.ArgumentParser(prog="python -m registry", description=__doc__)
    parser.add_argument(
        "--root", type=Path, default=Path(), help="root folder of the registry (default: .)"
    )
    commands = parser.add_subparsers(required=True, metavar="command")
    work = argparse.ArgumentParser(add_help=False)
    work.add_argument(
        "--work", type=Path, help="the work folder the steps share (default: work in the root)"
    )

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

    command = commands.add_parser("fetch", help=_fetch.__doc__, parents=[work])
    command.set_defaults(run=_fetch)

    command = commands.add_parser("check", help=_check.__doc__, parents=[work])
    command.add_argument("--line", required=True, help="the server line of this server image")
    command.add_argument("--domain", required=True)
    command.add_argument("--version", required=True)
    command.add_argument(
        "--stage",
        required=True,
        choices=["install", "load"],
        help="install: the content and the requirements, without provider code; load: the import",
    )
    command.add_argument(
        "--scratch", type=Path, required=True, help="where install keeps the requirements for load"
    )
    command.add_argument(
        "--output", type=Path, required=True, help="the results file to write, when there is one"
    )
    command.set_defaults(run=_check)

    command = commands.add_parser("check-folder", help=_check_folder.__doc__)
    command.add_argument(
        "folder", type=Path, help="the provider folder, named after its domain or provider"
    )
    command.set_defaults(run=_check_folder)

    command = commands.add_parser("apply-results", help=_apply_results.__doc__, parents=[work])
    command.add_argument("results", type=Path, help="the folder with the results files")
    command.set_defaults(run=_apply_results)

    command = commands.add_parser("report", help=_report.__doc__, parents=[work])
    command.add_argument("results", type=Path, help="the folder with the results files")
    command.set_defaults(run=_report)

    command = commands.add_parser("post-issues", help=_post_issues.__doc__, parents=[work])
    command.add_argument(
        "--repository", required=True, help="the repository of the registry, as owner/name"
    )
    command.set_defaults(run=_post_issues)

    command = commands.add_parser("check-changes", help=_check_changes.__doc__)
    command.add_argument(
        "changed", type=Path, help="a file with the changed paths, separated by NUL characters"
    )
    command.set_defaults(run=_check_changes)

    issue = argparse.ArgumentParser(add_help=False)
    issue.add_argument(
        "--repository", required=True, help="the repository of the registry, as owner/name"
    )
    issue.add_argument("--issue", type=int, required=True, help="the number of the submission")

    command = commands.add_parser(
        "prepare-admission", help=_prepare_admission.__doc__, parents=[work, issue]
    )
    command.set_defaults(run=_prepare_admission)

    command = commands.add_parser("check-admission", help=_check_admission.__doc__, parents=[work])
    command.add_argument("results", type=Path, help="the folder with the results files")
    command.add_argument(
        "--comment", type=Path, help="a file to write the result into as a comment"
    )
    command.add_argument("--run-url", help="the address of the workflow run, to link to")
    command.add_argument(
        "--pull-request",
        type=_pull_request_url,
        default="",
        help="the address of the pull request of the outcome; empty for none",
    )
    command.add_argument(
        "--proposal", help="the result of the job that takes care of the pull request: success"
    )
    command.add_argument(
        "--outcome",
        choices=["", *(outcome.value for outcome in Outcome)],
        default="",
        help="what that job did with the pull request",
    )
    command.add_argument(
        "--others",
        type=_pull_request_urls,
        default="",
        help="the pull requests of other submissions of the domain, separated by spaces",
    )
    command.set_defaults(run=_check_admission)

    command = commands.add_parser(
        "post-admission-comment", help=_post_admission_comment.__doc__, parents=[issue]
    )
    command.add_argument("comment", type=Path, help="the file check-admission wrote")
    command.set_defaults(run=_post_admission_comment)

    command = commands.add_parser(
        "propose-admission", help=_propose_admission.__doc__, parents=[work, issue]
    )
    command.add_argument("results", type=Path, help="the folder with the results files")
    command.add_argument(
        "--author", required=True, help="the account of the token, which opens pull requests"
    )
    command.add_argument("--run-url", required=True, help="the address of the workflow run")
    command.add_argument(
        "--output", type=Path, help="a file to add the outputs of the job to: outcome, url, others"
    )
    command.set_defaults(run=_propose_admission)

    command = commands.add_parser("set-status", help=_set_status.__doc__)
    command.add_argument("domain")
    command.add_argument("status", choices=[status.value for status in ProviderStatus])
    command.add_argument("--reason", help="why; required for withdrawn and blocked")
    command.add_argument("--reason-url", help="an https link with more about the reason")
    command.set_defaults(run=_set_status)

    command = commands.add_parser("block-version", help=_block_version.__doc__)
    command.add_argument("domain")
    command.add_argument("version")
    command.add_argument("--reason", required=True, help="why the version is blocked")
    command.set_defaults(run=_block_version)

    command = commands.add_parser("unblock-version", help=_unblock_version.__doc__)
    command.add_argument("domain")
    command.add_argument("version")
    command.set_defaults(run=_unblock_version)
    return parser
