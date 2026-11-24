"""Tests for the read step: which versions it reads, against what, and what it records."""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any

import anthropic
import httpx2
import pytest

from registry import reading as reading_module
from registry.cli import main
from registry.content import read_archive
from registry.files import load_model
from registry.github import GitHub
from registry.models import ReadingResults, Verdict
from registry.pipeline import (
    MAX_READINGS_PER_RUN,
    READINGS_FILE,
    PlannedReading,
    archive_path,
    versions_to_read,
)
from registry.registry import load_registry
from tests.conftest import DOMAIN, provider_archive
from tests.test_reading import PASS, REQUEST_URL, FakeTokenService, _message

if TYPE_CHECKING:
    from collections.abc import Mapping

    from anthropic.types import Message

    from tests.conftest import Files

READ = "2026-11-02T03:00:00Z"
# the variables the read command takes from the environment of the workflow job
ENVIRONMENT = {
    "ACTIONS_ID_TOKEN_REQUEST_URL": REQUEST_URL,
    "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "request-token",
    "ANTHROPIC_ORGANIZATION_ID": "organization",
    "ANTHROPIC_FEDERATION_RULE_ID": "fdrl_rule",
    "ANTHROPIC_SERVICE_ACCOUNT_ID": "svac_account",
    "ANTHROPIC_WORKSPACE_ID": "wrkspc_workspace",
}
_REQUEST = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
# a hold on the one file of code of the provider
HOLD: dict[str, Any] = {
    "verdict": "hold",
    "findings": [
        {
            "rule": "harms_user",
            "file": "__init__.py",
            "line": 1,
            "explanation": "Sends the library to a host the description does not name.",
        }
    ],
    "summary": "Sets the version.",
}


class VersionClient:
    """A client of the API that answers the reading of each version as it is told."""

    def __init__(self, answers: Mapping[str, Message | Exception]) -> None:
        """
        Return a client with an answer per version.

        :param answers: The message to answer with, or the error to raise, by version.
        """
        self.answers = answers
        # the version and the base version of each request, in their order
        self.read: list[tuple[str, str | None]] = []
        self.messages = self

    def create(self, **kwargs: Any) -> Message:
        """Record the version a request reads, and answer it."""
        header = json.loads(kwargs["messages"][0]["content"].split("\n")[2])
        self.read.append((header["version"], header["base_version"]))
        answer = self.answers[header["version"]]
        if isinstance(answer, Exception):
            raise answer
        return answer


def _status_error(kind: type[anthropic.APIStatusError], status: int) -> anthropic.APIStatusError:
    """Return an error the API answers with."""
    return kind("error", response=httpx2.Response(status, request=_REQUEST), body=None)


def _version(
    files: Files, version: str, released: str = "2026-11-01T12:00:00Z", **changes: Any
) -> None:
    """Write a version of the sample provider, and its archive into the work folder."""
    archive = archive_path(files.root / "work", DOMAIN, version)
    archive.parent.mkdir(parents=True, exist_ok=True)
    archive.write_bytes(provider_archive(code=f"VERSION = {version!r}\n".encode()))
    content = read_archive(archive, f"providers/{DOMAIN}")
    files.version(
        version,
        content_hash=content.content_hash,
        manifest=content.manifest,
        requirements=[],
        released=released,
        **changes,
    )


def _read(
    files: Files,
    monkeypatch: pytest.MonkeyPatch,
    client: VersionClient,
    environment: Mapping[str, str] = ENVIRONMENT,
) -> int:
    """Run the read command with a client of the API that answers as it is told."""
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(reading_module, "federated_client", lambda *_, **__: client)
    return main(["--root", str(files.root), "read"])


def _readings(files: Files) -> dict[str, str | None]:
    """Return the verdict of each version of the sample provider; None for one not read."""
    versions = load_registry(files.root).providers[DOMAIN].versions
    return {
        version: file.reading.verdict.value if file.reading else None
        for version, file in sorted(versions.items())
    }


def test_read_records_each_verdict_newest_first(
    files: Files, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Each new version is read against the last accepted one; the report gets what it found."""
    files.provider()
    _version(files, "1.0.0", reading={"verdict": "pass", "read": READ})
    _version(files, "1.1.0", "2026-11-03T12:00:00Z")
    _version(files, "1.2.0", "2026-11-04T12:00:00Z")
    client = VersionClient({"1.1.0": _message(PASS), "1.2.0": _message(HOLD)})

    assert _read(files, monkeypatch, client) == 0

    # the bases are planned before the run: a version read in it is no base in it
    assert client.read == [("1.2.0", "1.0.0"), ("1.1.0", "1.0.0")]
    versions = load_registry(files.root).providers[DOMAIN].versions
    assert {v: (f.reading.verdict, f.reading.base) for v, f in versions.items() if f.reading} == {
        "1.0.0": (Verdict.PASS, None),
        "1.1.0": (Verdict.PASS, "1.0.0"),
        "1.2.0": (Verdict.HOLD, "1.0.0"),
    }
    results = load_model(files.root / "work" / READINGS_FILE, ReadingResults).results
    assert [(r.version, r.base, r.reading.verdict) for r in results] == [
        ("1.2.0", "1.0.0", Verdict.HOLD),
        ("1.1.0", "1.0.0", Verdict.PASS),
    ]
    assert results[0].reading.findings[0].explanation == HOLD["findings"][0]["explanation"]
    assert results[0].reading.summary == HOLD["summary"]
    output = capsys.readouterr()
    # the log has counts, nothing of what the reading says
    assert output.out.endswith("1 passed, 1 held, 0 left for another night\n")
    assert HOLD["summary"] not in output.out + output.err
    assert main(["--root", str(files.root), "validate"]) == 0


def test_at_most_a_number_of_versions_a_run(files: Files, monkeypatch: pytest.MonkeyPatch) -> None:
    """The most recently released versions are read first; the others wait for the next night."""
    files.provider()
    count = MAX_READINGS_PER_RUN + 1
    for number in range(count):
        _version(files, f"1.0.{number}", f"2026-11-{number + 1:02d}T12:00:00Z")
    client = VersionClient({f"1.0.{number}": _message(PASS) for number in range(count)})

    assert _read(files, monkeypatch, client) == 0

    assert [version for version, _ in client.read] == [
        f"1.0.{number}" for number in reversed(range(1, count))
    ]
    assert _readings(files)["1.0.0"] is None


def test_what_is_not_read(files: Files, monkeypatch: pytest.MonkeyPatch) -> None:
    """Read, cleared, blocked and failed versions, and blocked providers, are not read."""
    issue = "https://github.com/music-assistant/community-catalog/issues/31"
    files.provider(
        cleared_versions={"1.1.0": issue}, blocked_versions={"1.2.0": "Sends the library away"}
    )
    _version(files, "1.0.0", reading={"verdict": "hold", "read": READ})
    _version(files, "1.1.0")
    _version(files, "1.2.0")
    _version(files, "1.3.0", channels={"nightly": False, "beta": False})
    # not checked yet: it may pass
    _version(files, "1.4.0", channels={})
    files.provider("other_music", status="blocked", reason="Sends the library away")
    files.version("1.0.0", domain="other_music")
    client = VersionClient({"1.4.0": _message(PASS)})

    assert _read(files, monkeypatch, client) == 0

    # read against the cleared version
    assert client.read == [("1.4.0", "1.1.0")]
    assert _readings(files) == {
        "1.0.0": "hold",
        "1.1.0": None,
        "1.2.0": None,
        "1.3.0": None,
        "1.4.0": "pass",
    }


def test_the_base_is_the_highest_lower_version_accepted(files: Files) -> None:
    """A pass of the reading or a clearance makes a base; a hold or a block does not."""
    issue = "https://github.com/music-assistant/community-catalog/issues/31"
    files.provider(
        cleared_versions={"1.1.0": issue}, blocked_versions={"1.2.0": "Sends the library away"}
    )
    files.version("1.0.0", reading={"verdict": "pass", "read": READ})
    files.version("1.1.0")
    files.version("1.2.0", reading={"verdict": "pass", "read": READ})
    files.version("1.3.0", reading={"verdict": "hold", "read": READ})
    files.version("1.4.0", released="2026-11-05T12:00:00Z")
    files.provider("other_music")
    files.version("0.1.0", domain="other_music", released="2026-11-04T12:00:00Z")
    files.version("0.2.0", domain="other_music", reading={"verdict": "hold", "read": READ})

    assert versions_to_read(load_registry(files.root)) == [
        PlannedReading(DOMAIN, "1.4.0", "1.1.0"),
        PlannedReading("other_music", "0.1.0", None),
    ]


@pytest.mark.parametrize(
    "error",
    [
        anthropic.WorkloadIdentityError("Token exchange failed (HTTP 401)", status_code=401),
        _status_error(anthropic.AuthenticationError, 401),
        _status_error(anthropic.PaymentRequiredError, 402),
        _status_error(anthropic.PermissionDeniedError, 403),
        _status_error(anthropic.NotFoundError, 404),
    ],
    ids=["exchange", "401", "402", "403", "404"],
)
def test_a_fault_fails_the_command_and_records_nothing_for_its_version(
    files: Files,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    error: Exception,
) -> None:
    """A failed exchange or a refused request fails loudly; what was read before is recorded."""
    files.provider()
    _version(files, "1.0.0", "2026-11-01T12:00:00Z")
    _version(files, "1.1.0", "2026-11-02T12:00:00Z")
    _version(files, "1.2.0", "2026-11-03T12:00:00Z")
    client = VersionClient({"1.2.0": _message(PASS), "1.1.0": error, "1.0.0": _message(PASS)})

    assert _read(files, monkeypatch, client) == 1

    assert [version for version, _ in client.read] == ["1.2.0", "1.1.0"]
    assert _readings(files) == {"1.0.0": None, "1.1.0": None, "1.2.0": "pass"}
    results = load_model(files.root / "work" / READINGS_FILE, ReadingResults).results
    assert [result.version for result in results] == ["1.2.0"]
    assert capsys.readouterr().err.startswith("error: the reading cannot run: ")
    assert main(["--root", str(files.root), "validate"]) == 0


@pytest.mark.parametrize(
    "answer",
    [
        anthropic.APIConnectionError(request=_REQUEST),
        anthropic.APITimeoutError(request=_REQUEST),
        _status_error(anthropic.RateLimitError, 429),
        _status_error(anthropic.InternalServerError, 500),
        _message("not JSON"),
        _message({**HOLD, "summary": "x" * 601}),
    ],
    ids=["connection", "timeout", "429", "500", "malformed-answer", "inconsistent-answer"],
)
def test_a_failure_of_the_api_or_an_invalid_answer_leaves_the_version_for_another_night(
    files: Files,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    answer: Message | Exception,
) -> None:
    """The version stays without a verdict, and the next one is read all the same."""
    files.provider()
    _version(files, "1.0.0", "2026-11-01T12:00:00Z")
    _version(files, "1.1.0", "2026-11-02T12:00:00Z")
    client = VersionClient({"1.1.0": answer, "1.0.0": _message(HOLD)})

    assert _read(files, monkeypatch, client) == 0

    assert _readings(files) == {"1.0.0": "hold", "1.1.0": None}
    output = capsys.readouterr()
    assert output.out.endswith("0 passed, 1 held, 1 left for another night\n")
    assert f"warning: {DOMAIN} 1.1.0: not read: " in output.err


def test_a_version_without_its_archives_waits(
    files: Files, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Without the archive of a version or its base, or with another content, it is not read."""
    files.provider()
    _version(files, "1.0.0", "2026-11-01T12:00:00Z", reading={"verdict": "pass", "read": READ})
    _version(files, "1.1.0", "2026-11-02T12:00:00Z")
    _version(files, "1.2.0", "2026-11-03T12:00:00Z")
    _version(files, "1.3.0", "2026-11-04T12:00:00Z")
    work = files.root / "work"
    archive_path(work, DOMAIN, "1.0.0").unlink()
    archive_path(work, DOMAIN, "1.3.0").write_bytes(provider_archive(code=b"OTHER = 1\n"))

    assert _read(files, monkeypatch, VersionClient({})) == 0

    assert _readings(files) == {"1.0.0": "pass", "1.1.0": None, "1.2.0": None, "1.3.0": None}
    err = capsys.readouterr().err
    assert f"{DOMAIN} 1.2.0: not read: the archive of 1.0.0 is not in the work folder" in err
    assert f"{DOMAIN} 1.3.0: not read: the archive of 1.3.0 does not hold the content" in err
    assert not (work / READINGS_FILE).exists()


def test_read_needs_the_identity_token_and_the_ids(
    files: Files, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Without the permission id-token: write or the ids of the federation, nothing is read."""
    files.provider()
    _version(files, "1.0.0")
    environment = {**ENVIRONMENT, "ACTIONS_ID_TOKEN_REQUEST_URL": "", "ANTHROPIC_WORKSPACE_ID": ""}

    assert _read(files, monkeypatch, VersionClient({}), environment) == 1

    assert capsys.readouterr().err == (
        "error: set ACTIONS_ID_TOKEN_REQUEST_URL, ANTHROPIC_WORKSPACE_ID\n"
    )
    assert _readings(files) == {"1.0.0": None}


def test_read_authenticates_by_federation(files: Files, monkeypatch: pytest.MonkeyPatch) -> None:
    """The client gets the ids of the environment and an identity token of the job per call."""
    files.provider()
    service = FakeTokenService()
    made: dict[str, Any] = {}

    def credentials(**kwargs: Any) -> object:
        made.update(kwargs)
        return object()

    for name, value in ENVIRONMENT.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(anthropic, "WorkloadIdentityCredentials", credentials)
    monkeypatch.setattr(anthropic, "Anthropic", lambda **_: VersionClient({}))
    monkeypatch.setattr("registry.cli.GitHub", lambda token: GitHub(token, service))

    assert main(["--root", str(files.root), "read"]) == 0

    provider = made.pop("identity_token_provider")
    assert made == {
        "organization_id": "organization",
        "federation_rule_id": "fdrl_rule",
        "service_account_id": "svac_account",
        "workspace_id": "wrkspc_workspace",
        "http_client": None,
    }
    assert [provider(), provider()] == ["jwt-1", "jwt-2"]
    assert all(r.get_header("Authorization") == "Bearer request-token" for r in service.requests)
    assert re.fullmatch(rf"{re.escape(REQUEST_URL)}&audience=.+", service.requests[0].full_url)
