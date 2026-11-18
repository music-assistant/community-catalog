"""A submission of a provider: the issue form, and where its provider is in a release."""

from __future__ import annotations

import re
import tarfile
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

from registry import formats
from registry.content import MANIFEST_FILE
from registry.files import loads

if TYPE_CHECKING:
    from pathlib import Path

# the label the form gives an issue; only an issue with it is a submission
SUBMISSION_LABEL = "submission"
# the field of the form whose box makes the submission a move of a provider the server ships
MOVE_FIELD = "Moving from the server"
# the fields of the form, as GitHub writes them into the issue: a heading each, in this order
FIELDS = ("Repository", "Folder", "Tag prefix", MOVE_FIELD, "Before you submit")
# the boxes of the last field, each one required
CONFIRMATIONS = 2
# far more than the form gives; a longer issue is refused
MAX_BODY = 10_000
# what GitHub writes for an optional field left empty
_NO_RESPONSE = "_No response_"
# far more than a manifest holds (the index takes 16 KiB); a larger one is not read
_MAX_MANIFEST = 64 * 1024
# the most of an archive the search for the provider folder walks: far more than the repository
# of a provider holds
MAX_ARCHIVE_MEMBERS = 20_000
MAX_ARCHIVE_EXPANDED = 1024 * 1024 * 1024
# the folders a refusal names at most
_LISTED_FOLDERS = 5
_MAX_FOLDER_NAME = 100
_REPOSITORY_URL = re.compile(
    r"(?:https?://)?(?:www\.)?github\.com/([^/]+/[^/]+?)(?:\.git)?/?", re.IGNORECASE
)
_TICKED = re.compile(r"- \[[xX]\] \S.*")
_BOX = re.compile(r"- \[[ xX]\] \S.*")


class SubmissionError(Exception):
    """The issue is no valid submission; the message says why."""


@dataclass(frozen=True, slots=True)
class Submission:
    """What the form of a submission holds."""

    # owner/name
    repository: str
    # the provider folder in the repository, None to find it
    folder: str | None
    tag_prefix: str
    # whether it moves a provider the server ships into the community catalog
    move: bool = False


def parse_submission(body: str) -> Submission:
    """
    Return what the issue of a submission holds, which must be the fields of the form only.

    :param body: The text of the issue, as GitHub writes it from the form.
    :raises SubmissionError: The text is not the form, or a field is not valid.
    """
    text = body.replace("\r\n", "\n")
    if len(text) > MAX_BODY:
        msg = f"the issue holds {len(text)} characters, more than the {MAX_BODY} of a submission"
        raise SubmissionError(msg)
    parts = re.split(r"^### (.*)$", text, flags=re.MULTILINE)
    if parts[0].strip() or [heading.strip() for heading in parts[1::2]] != list(FIELDS):
        msg = f"the issue does not hold the fields of the form, and only those: {', '.join(FIELDS)}"
        raise SubmissionError(msg)
    values = dict(zip(FIELDS, (value.strip() for value in parts[2::2]), strict=True))
    for field in FIELDS[:-1]:
        if "\n" in values[field]:
            msg = f"{field}: holds more than one line"
            raise SubmissionError(msg)
    repository = values["Repository"]
    if found := _REPOSITORY_URL.fullmatch(repository):
        repository = found[1]
    if not formats.is_repository(repository):
        msg = f"Repository: {repository!r} is not a GitHub repository as owner/name"
        raise SubmissionError(msg)
    folder = None if values["Folder"] == _NO_RESPONSE else values["Folder"].strip("/") or None
    if folder is not None and not formats.is_folder(folder):
        msg = f"Folder: {folder!r} is not a folder in a repository, such as providers/my_provider"
        raise SubmissionError(msg)
    tag_prefix = "" if values["Tag prefix"] == _NO_RESPONSE else values["Tag prefix"]
    if not formats.is_tag_prefix(tag_prefix):
        msg = f"Tag prefix: {tag_prefix!r} cannot start a tag"
        raise SubmissionError(msg)
    move = values[MOVE_FIELD]
    if move != _NO_RESPONSE and not _BOX.fullmatch(move):
        msg = f"{MOVE_FIELD}: holds something else than the box of the form"
        raise SubmissionError(msg)
    ticked = values[FIELDS[-1]].splitlines()
    if len(ticked) != CONFIRMATIONS or not all(_TICKED.fullmatch(line) for line in ticked):
        msg = f"{FIELDS[-1]}: every box is to be ticked"
        raise SubmissionError(msg)
    return Submission(
        repository=repository,
        folder=folder,
        tag_prefix=tag_prefix,
        move=bool(_TICKED.fullmatch(move)),
    )


def find_provider(archive: Path, folder: str | None) -> tuple[str, str]:
    """
    Return the provider folder in the GitHub archive of a release, and the domain it names.

    Without a folder given, it is the folder `provider` when that holds a manifest.json, and
    otherwise the one top-level folder that holds one. Only the manifest.json of that folder
    is read.

    :param archive: The archive of the commit of the release.
    :param folder: The provider folder the submission names, or None to find it.
    :raises SubmissionError: The archive holds more than the search walks, the folder is not
        there or cannot be found, or its manifest.json does not name a valid domain.
    :raises OSError: The archive cannot be read.
    """
    try:
        with tarfile.open(archive, "r:gz") as tar:
            manifests = _manifests(tar, folder)
            folder = _choose(manifests, folder)
            where = f"{folder}/{MANIFEST_FILE}"
            member = manifests[folder]
            source = tar.extractfile(member) if member.isfile() else None
            if source is None or member.size > _MAX_MANIFEST:
                msg = f"{where} is no regular file of at most {_MAX_MANIFEST} bytes"
                raise SubmissionError(msg)
            with source:
                content = source.read(_MAX_MANIFEST)
    except (tarfile.TarError, EOFError) as err:
        msg = f"the archive of the release cannot be read: {err}"
        raise SubmissionError(msg) from err
    try:
        manifest = loads(content)
    except ValueError as err:
        msg = f"{where}: {err}"
        raise SubmissionError(msg) from err
    domain = manifest.get("domain") if isinstance(manifest, dict) else None
    if not isinstance(domain, str) or not formats.is_domain(domain):
        msg = f"{where}: domain: {domain!r} is not a provider domain"
        raise SubmissionError(msg)
    return folder, domain


def _manifests(tar: tarfile.TarFile, folder: str | None) -> dict[str, tarfile.TarInfo]:
    """
    Return the manifest.json members of the folders a provider may be in, by folder.

    Those are the given folder, or else every top-level folder. Only the names are looked at.

    :param tar: The GitHub archive, with the repository in one top-level folder.
    :param folder: The provider folder the submission names, or None.
    :raises SubmissionError: The archive holds more members or bytes than the search walks.
    """
    result: dict[str, tarfile.TarInfo] = {}
    walked = 0
    for count, member in enumerate(tar, 1):
        # walking past a member decompresses it
        walked += max(member.size, 0)
        if count > MAX_ARCHIVE_MEMBERS or walked > MAX_ARCHIVE_EXPANDED:
            msg = (
                f"the archive of the release holds more than {MAX_ARCHIVE_MEMBERS} files or "
                f"{MAX_ARCHIVE_EXPANDED} bytes"
            )
            raise SubmissionError(msg)
        # the first part is the top-level folder GitHub names after the repository
        parts = PurePosixPath(member.name).parts[1:]
        if len(parts) < 2 or parts[-1] != MANIFEST_FILE or ".." in parts:  # noqa: PLR2004
            continue
        where = "/".join(parts[:-1])
        if where == folder or (folder is None and len(parts) == 2):  # noqa: PLR2004
            result[where] = member
    return result


def _choose(manifests: dict[str, tarfile.TarInfo], folder: str | None) -> str:
    """
    Return the provider folder among the folders with a manifest.json.

    :param manifests: The manifest.json members, by folder.
    :param folder: The provider folder the submission names, or None.
    :raises SubmissionError: The given folder has no manifest.json, or none or several can be
        taken.
    """
    if folder is not None:
        if folder not in manifests:
            msg = f"the release has no {folder}/{MANIFEST_FILE}"
            raise SubmissionError(msg)
        return folder
    if formats.PROVIDER_FOLDER_NAME in manifests:
        return formats.PROVIDER_FOLDER_NAME
    if len(manifests) == 1:
        return next(iter(manifests))
    names = sorted(manifests)
    found = ", ".join(name[:_MAX_FOLDER_NAME] for name in names[:_LISTED_FOLDERS]) or "none"
    if len(names) > _LISTED_FOLDERS:
        found += f" and {len(names) - _LISTED_FOLDERS} more"
    msg = (
        f"the release has {len(names)} top-level folders with a {MANIFEST_FILE} ({found}): "
        "name the provider folder in the field Folder"
    )
    raise SubmissionError(msg)
