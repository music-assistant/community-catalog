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
# the fields of the form, as GitHub writes them into the issue: a heading each, in this order
FIELDS = ("Repository", "Folder", "Tag prefix", "Before you submit")
# the boxes of the last field, each one required
CONFIRMATIONS = 2
# far more than the form gives; a longer issue is refused
MAX_BODY = 10_000
# what GitHub writes for an optional field left empty
_NO_RESPONSE = "_No response_"
# far more than a manifest holds (the index takes 16 KiB); a larger one is not read
_MAX_MANIFEST = 64 * 1024
_REPOSITORY_URL = re.compile(r"(?:https?://)?(?:www\.)?github\.com/([^/]+/[^/]+?)(?:\.git)?/?")
_TICKED = re.compile(r"- \[[xX]\] \S.*")


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
    ticked = values[FIELDS[-1]].splitlines()
    if len(ticked) != CONFIRMATIONS or not all(_TICKED.fullmatch(line) for line in ticked):
        msg = f"{FIELDS[-1]}: every box is to be ticked"
        raise SubmissionError(msg)
    return Submission(repository=repository, folder=folder, tag_prefix=tag_prefix)


def find_provider(archive: Path, folder: str | None) -> tuple[str, str]:
    """
    Return the provider folder in the GitHub archive of a release, and the domain it names.

    Without a folder given, it is the folder `provider` when that holds a manifest.json, and
    otherwise the one top-level folder that holds one.

    :param archive: The archive of the commit of the release.
    :param folder: The provider folder the submission names, or None to find it.
    :raises SubmissionError: The folder is not there or cannot be found, or its manifest.json
        does not name a valid domain.
    :raises OSError: The archive cannot be read.
    """
    manifests = _manifests(archive, folder)
    if folder is not None:
        if folder not in manifests:
            msg = f"the release has no {folder}/{MANIFEST_FILE}"
            raise SubmissionError(msg)
    elif formats.PROVIDER_FOLDER_NAME in manifests:
        folder = formats.PROVIDER_FOLDER_NAME
    elif len(manifests) == 1:
        [folder] = manifests
    else:
        found = ", ".join(sorted(manifests)) or "none"
        msg = (
            f"the release has {len(manifests)} top-level folders with a {MANIFEST_FILE} "
            f"({found}): name the provider folder in the field Folder"
        )
        raise SubmissionError(msg)
    where = f"{folder}/{MANIFEST_FILE}"
    if (content := manifests[folder]) is None:
        msg = f"{where} is no regular file of at most {_MAX_MANIFEST} bytes"
        raise SubmissionError(msg)
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


def _manifests(archive: Path, folder: str | None) -> dict[str, bytes | None]:
    """
    Return the manifest.json files of the folders a provider may be in, by folder.

    Those are the given folder, or else every top-level folder. A manifest.json that is no
    regular file of a sensible size is None.

    :param archive: The GitHub archive, with the repository in one top-level folder.
    :param folder: The provider folder the submission names, or None.
    :raises SubmissionError: The archive cannot be read.
    :raises OSError: The archive cannot be opened.
    """
    result: dict[str, bytes | None] = {}
    try:
        with tarfile.open(archive, "r:gz") as tar:
            for member in tar:
                # the first part is the top-level folder GitHub names after the repository
                parts = PurePosixPath(member.name).parts[1:]
                if len(parts) < 2 or parts[-1] != MANIFEST_FILE or ".." in parts:  # noqa: PLR2004
                    continue
                where = "/".join(parts[:-1])
                if where != folder and (folder is not None or len(parts) != 2):  # noqa: PLR2004
                    continue
                source = (
                    tar.extractfile(member)
                    if member.isfile() and member.size <= _MAX_MANIFEST
                    else None
                )
                result[where] = source.read() if source is not None else None
    except (tarfile.TarError, EOFError) as err:
        msg = f"the archive of the release cannot be read: {err}"
        raise SubmissionError(msg) from err
    return result
