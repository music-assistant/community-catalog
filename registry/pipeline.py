"""What the steps of the release pipeline share: the work folder, what to check, the results."""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

from registry.edit import CommandError, clear_failed_check, record_check, write_indexes
from registry.files import dumps, load_model
from registry.formats import is_reason, version_key
from registry.index import build_index
from registry.models import ChannelResults, CheckPlan, ProviderStatus, ProviderVersion
from registry.registry import InvalidRegistryError

if TYPE_CHECKING:
    from pathlib import Path

    from registry.registry import Registry

# the files in the work folder
FETCH_REPORT = "fetch.json"
ADMISSION_REPORT = "admission.json"
PLAN_FILE = "plan.json"
ISSUES_FOLDER = "issues"
# the reports of the channels, in the issues folder
CHANNELS_FOLDER = "channels"
_ARCHIVES_FOLDER = "archives"
# the versions above the latest of a channel that are checked again every night, newest first
RECHECKED_ABOVE_LATEST = 3
# a results file holds a reason of one line and at most 50 kB of output
MAX_RESULTS_SIZE = 200_000


def archive_path(work: Path, domain: str, version: str) -> Path:
    """
    Return where the fetch step keeps the GitHub archive of a version for the check step.

    :param work: The work folder.
    :param domain: The domain of the provider.
    :param version: The version.
    """
    return work / _ARCHIVES_FOLDER / domain / f"{version}.tar.gz"


def result_path(results: Path, channel: str, domain: str, version: str) -> Path:
    """
    Return where the check step puts the results file of a version on a channel.

    :param results: The folder with the results files.
    :param channel: The channel.
    :param domain: The domain of the provider.
    :param version: The version.
    """
    return results / channel / f"{domain}-{version}.json"


def versions_to_check(registry: Registry, channel: str) -> list[tuple[str, str]]:
    """
    Return the versions the checks run for on a channel, as (domain, version).

    Those are the versions without a result for the channel, the latest version of each provider
    on the channel again, and the newest versions above it, so a version that failed once (on a
    night the server image was broken, say) can pass again. A frozen channel, a blocked provider
    and a blocked version are not checked.

    :param registry: The registry.
    :param channel: The channel.
    """
    if registry.channels[channel].frozen:
        return []
    index = build_index(registry, channel, "")
    result: list[tuple[str, str]] = []
    for domain, provider in sorted(registry.providers.items()):
        if provider.file.status == ProviderStatus.BLOCKED:
            continue
        latest = index.providers[domain].latest if domain in index.providers else None
        allowed = sorted(
            (v for v in provider.versions if v not in provider.file.blocked_versions),
            key=version_key,
        )
        above = [v for v in allowed if latest is None or version_key(v) > version_key(latest)]
        rechecked = {latest, *above[-RECHECKED_ABOVE_LATEST:]}
        result += [
            (domain, version)
            for version in allowed
            if channel not in provider.versions[version].checks or version in rechecked
        ]
    return result


def make_plan(registry: Registry, work: Path) -> CheckPlan:
    """
    Return the plan of the check step: what to check on each channel that is tested.

    Those are the versions to check whose archive is in the work folder.

    :param registry: The registry.
    :param work: The work folder, with the archives.
    """
    return CheckPlan(
        channels={
            channel: [
                ProviderVersion(domain=domain, version=version)
                for domain, version in versions_to_check(registry, channel)
                if archive_path(work, domain, version).is_file()
            ]
            for channel, config in sorted(registry.channels.items())
            if not config.frozen
        }
    )


def write_plan(plan: CheckPlan, work: Path) -> None:
    """
    Write the plan of the check step into the work folder.

    :param plan: The plan.
    :param work: The work folder.
    """
    path = work / PLAN_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(dumps(plan.to_dict()))


def read_plan(work: Path) -> CheckPlan:
    """
    Return the plan of the check step.

    :param work: The work folder.
    :raises CommandError: The plan is missing or not exactly a plan.
    """
    try:
        return load_model(work / PLAN_FILE, CheckPlan)
    except (ValueError, OSError) as err:
        msg = f"{work / PLAN_FILE}: {err}"
        raise CommandError(msg) from err


def read_results(folder: Path, plan: CheckPlan) -> list[ChannelResults]:
    """
    Return the results of the check step for the versions of the plan.

    The results come from containers that ran provider code. A result counts only when its
    file is where the plan puts it and holds one valid result of exactly that channel, provider
    and version; any other file is ignored, and every problem is a warning.

    :param folder: The folder with the results files.
    :param plan: The plan of the check step.
    """
    results: list[ChannelResults] = []
    expected: set[Path] = set()
    for channel, versions in sorted(plan.channels.items()):
        for entry in versions:
            path = result_path(folder, channel, entry.domain, entry.version)
            expected.add(path)
            if (file := _read_result(path, channel, entry)) is not None:
                results.append(file)
    for path in sorted(folder.rglob("*") if folder.is_dir() else []):
        if path not in expected and not path.is_dir():
            _warn(f"{str(path)!r}: ignored, not a results file of the plan")
    return results


def apply_results(registry: Registry, results: list[ChannelResults]) -> None:
    """
    Record the results of the check step in the version files, and write the index files.

    A check that could not run removes a failed result of its version on the channel, which leaves
    the version unchecked there; a passed result stays. A result that cannot be recorded is
    skipped with a warning; the others are recorded.

    :param registry: The registry.
    :param results: The results of the check step, as read_results returns them.
    :raises InvalidRegistryError: An index file cannot be written.
    """
    for file in results:
        if (channel := registry.channels.get(file.channel)) is None or channel.frozen:
            _warn(f"{file.channel!r}: skipped, not a channel that is tested")
            continue
        for result in file.results:
            try:
                if result.unavailable:
                    clear_failed_check(registry, result.domain, result.version, file.channel)
                else:
                    record_check(
                        registry,
                        result.domain,
                        result.version,
                        file.channel,
                        reason=None if result.passed else (result.reason or ""),
                        checked=file.checked,
                    )
            except (CommandError, InvalidRegistryError) as err:
                _warn(f"{result.domain} {result.version} on {file.channel}: skipped: {err}")
    write_indexes(registry.root)


def _read_result(path: Path, channel: str, entry: ProviderVersion) -> ChannelResults | None:
    """
    Return the results file of a version, or None after a warning when it does not count.

    :param path: Where the plan puts the file.
    :param channel: The channel.
    :param entry: The version.
    """
    if not path.exists() and not path.is_symlink():
        _warn(f"{entry.domain} {entry.version} on {channel}: no result")
        return None
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_RESULTS_SIZE:
        _warn(f"{path}: ignored, not a regular file of at most {MAX_RESULTS_SIZE} bytes")
        return None
    try:
        file = load_model(path, ChannelResults)
    except (ValueError, OSError) as err:
        _warn(f"{path}: ignored: {err}")
        return None
    names = [(file.channel, result.domain, result.version) for result in file.results]
    if names != [(channel, entry.domain, entry.version)]:
        _warn(f"{path}: ignored, it does not hold the one result of {entry.domain} {entry.version}")
        return None
    [result] = file.results
    if result.passed != (result.reason is None) or not (
        result.reason is None or is_reason(result.reason)
    ):
        _warn(f"{path}: ignored, a failed check has one line of reason and a pass none")
        return None
    return file


def _warn(text: str) -> None:
    """Print a warning of the pipeline."""
    print(f"warning: {text}", file=sys.stderr)
