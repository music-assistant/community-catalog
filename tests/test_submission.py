"""Tests for reading the issue form of a submission and finding its provider in a release."""

from __future__ import annotations

import json
import re
import tarfile
from typing import TYPE_CHECKING

import pytest

from registry.submission import (
    MAX_BODY,
    Submission,
    SubmissionError,
    find_provider,
    parse_submission,
)
from tests.conftest import ARCHIVE_ROOT, build_archive, manifest, member

if TYPE_CHECKING:
    from pathlib import Path

TICKED = (
    "- [X] I own this repository, or I am a code owner in the manifest of its provider.\n"
    "- [X] I have read the [rules of the community catalog](https://example.com/rules)."
)


def body(
    repository: str = "someone/ma-sample",
    folder: str = "_No response_",
    tag_prefix: str = "_No response_",
    ticked: str = TICKED,
) -> str:
    """
    Return the text of an issue as GitHub writes it from the form.

    :param repository: The value of the field Repository.
    :param folder: The value of the field Folder.
    :param tag_prefix: The value of the field Tag prefix.
    :param ticked: The boxes of the field Before you submit.
    """
    return (
        f"### Repository\n\n{repository}\n\n### Folder\n\n{folder}\n\n"
        f"### Tag prefix\n\n{tag_prefix}\n\n### Before you submit\n\n{ticked}"
    )


@pytest.mark.parametrize(
    "repository",
    [
        "someone/ma-sample",
        "https://github.com/someone/ma-sample",
        "https://github.com/someone/ma-sample/",
        "https://github.com/someone/ma-sample.git",
        "github.com/someone/ma-sample",
    ],
)
def test_a_repository_as_owner_name_or_address(repository: str) -> None:
    """The repository is taken as owner/name or as its address on github.com."""
    assert parse_submission(body(repository)) == Submission("someone/ma-sample", None, "")


def test_the_optional_fields() -> None:
    """A folder and a tag prefix are taken when given; GitHub's line endings do not matter."""
    text = body(folder="providers/sample_music/", tag_prefix="sample_music-")

    assert parse_submission(text.replace("\n", "\r\n")) == Submission(
        "someone/ma-sample", "providers/sample_music", "sample_music-"
    )


@pytest.mark.parametrize(
    ("text", "problem"),
    [
        (body("someone"), "Repository: 'someone' is not a GitHub repository as owner/name"),
        (
            body("https://gitlab.com/someone/ma-sample"),
            "Repository: 'https://gitlab.com/someone/ma-sample' is not a GitHub repository",
        ),
        (body("someone/ma-sample\nanother/one"), "Repository: holds more than one line"),
        (body(folder="../elsewhere"), "Folder: '../elsewhere' is not a folder in a repository"),
        (body(tag_prefix="a b"), "Tag prefix: 'a b' cannot start a tag"),
        (body(ticked=TICKED.replace("[X]", "[ ]", 1)), "Before you submit: every box"),
        (body(ticked=TICKED.splitlines()[0]), "Before you submit: every box"),
        (body() + "\n\n### Approved\n\nyes", "does not hold the fields of the form"),
        ("Please add my provider: someone/ma-sample", "does not hold the fields of the form"),
        ("**Approved**\n\n" + body(), "does not hold the fields of the form"),
        (body().replace("### Folder", "### Tag prefix", 1), "does not hold the fields"),
        ("", "does not hold the fields of the form"),
        (body(folder="x" * MAX_BODY), f"more than the {MAX_BODY} of a submission"),
    ],
    ids=[
        "no owner",
        "not github",
        "two lines",
        "folder outside",
        "tag prefix",
        "a box not ticked",
        "a box missing",
        "an extra heading",
        "junk",
        "text before the form",
        "a heading twice",
        "empty",
        "oversized",
    ],
)
def test_what_is_not_the_form_is_refused(text: str, problem: str) -> None:
    """Only the fields of the form, each valid, make a submission."""
    with pytest.raises(SubmissionError, match=re.escape(problem)):
        parse_submission(text)


def _archive(tmp_path: Path, *folders: str, extra: tuple[tarfile.TarInfo, ...] = ()) -> Path:
    """Return a GitHub archive with a provider in each folder, named after its last part."""
    content = {
        f"{folder}/manifest.json": json.dumps(manifest(folder.rsplit("/", 1)[-1])).encode()
        for folder in folders
    }
    path = tmp_path / "release.tar.gz"
    path.write_bytes(build_archive({"README.md": b"x", **content}, extra=extra))
    return path


@pytest.mark.parametrize(
    ("folders", "given", "found"),
    [
        (["sample_music"], None, "sample_music"),
        (["provider", "other_music"], None, "provider"),
        (["providers/sample_music", "providers/other_music"], "providers/other_music", None),
        (["sample_music", "other_music"], "other_music", None),
    ],
    ids=["single top-level", "provider", "given", "given at the top"],
)
def test_the_provider_folder_is_found(
    tmp_path: Path, folders: list[str], given: str | None, found: str | None
) -> None:
    """The given folder, else provider, else the one top-level folder with a manifest."""
    # a top-level folder without a manifest does not count
    archive = _archive(
        tmp_path, *folders, extra=(member(f"{ARCHIVE_ROOT}/tests", tarfile.DIRTYPE),)
    )
    folder = given or found
    assert folder is not None

    assert find_provider(archive, given) == (folder, folder.rsplit("/", 1)[-1])


@pytest.mark.parametrize(
    ("folders", "given", "problem"),
    [
        (["sample_music", "other_music"], None, "2 top-level folders with a manifest.json"),
        (["providers/sample_music"], None, "0 top-level folders with a manifest.json"),
        (["sample_music"], "other_music", "the release has no other_music/manifest.json"),
    ],
    ids=["ambiguous", "none", "given but missing"],
)
def test_a_folder_that_cannot_be_found(
    tmp_path: Path, folders: list[str], given: str | None, problem: str
) -> None:
    """Several folders or none ask for the field Folder; a given folder must exist."""
    with pytest.raises(SubmissionError, match=re.escape(problem)) as err:
        find_provider(_archive(tmp_path, *folders), given)

    if given is None:
        assert str(err.value).endswith("name the provider folder in the field Folder")


def test_a_manifest_that_names_no_domain(tmp_path: Path) -> None:
    """The domain comes from the manifest, and must be one."""
    content = {"provider/manifest.json": json.dumps({"domain": "Not a domain"}).encode()}
    archive = tmp_path / "release.tar.gz"
    archive.write_bytes(build_archive(content))

    with pytest.raises(
        SubmissionError, match=re.escape("provider/manifest.json: domain: 'Not a domain'")
    ):
        find_provider(archive, None)


def test_a_manifest_that_is_a_link(tmp_path: Path) -> None:
    """A manifest.json that is no regular file is not read."""
    link = member(f"{ARCHIVE_ROOT}/provider/manifest.json", tarfile.SYMTYPE, "/etc/passwd")
    archive = _archive(tmp_path, extra=(link,))

    with pytest.raises(
        SubmissionError, match=re.escape("provider/manifest.json is no regular file")
    ):
        find_provider(archive, None)
