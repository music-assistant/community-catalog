"""Tests for the reading of a release against the rules, with a fake client of the API."""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any, cast

import anthropic
import httpx2
import pytest
from anthropic.types import Message

from registry.cli import main
from registry.content import unpack
from registry.models import Verdict
from registry.reading import (
    CUT_OFF,
    EFFORT,
    INSTRUCTIONS_FOLDER,
    INVALID_ANSWER,
    MAX_SHOWN,
    MAX_TOKENS,
    MODEL,
    REFUSED,
    TOO_LARGE,
    UNREADABLE,
    Reading,
    ReadingFinding,
    ReadingUnavailableError,
    Rule,
    Side,
    Status,
    compare_folders,
    read_release,
)
from tests.conftest import DOMAIN, build_archive, manifest

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

FOLDER = f"providers/{DOMAIN}"
BASE = {
    "manifest.json": json.dumps(manifest(requirements=["some-package==1.0"])).encode(),
    "__init__.py": b"import api\n\nVALUE = 1\nNAME = 'sample'\n",
    "helpers.py": b"def helper():\n    return 1\n",
    "unchanged.py": b"STAYS = True\n",
    "logo.png": b"\x89PNG\r\n\x1a\n\x00old",
}
RELEASE = {
    "manifest.json": json.dumps(manifest(requirements=["some-package==2.0"])).encode(),
    "__init__.py": b"import api\n\nVALUE = 2\nNAME = 'sample'\n",
    "unchanged.py": b"STAYS = True\n",
    "api.py": b"import json\n\n\ndef fetch():\n    return json.loads('{}')\n",
    "vendor/lib.py": b"VENDORED = 1\n",
    "logo.png": b"\x89PNG\r\n\x1a\n\x00new",
    "web/app.min.js": b"var a=1;\n",
    "web/page.js": b"x" * 2001 + b"\n",
    "fonts/font.woff2": b"wOF2\x00\x01",
}
PASS: dict[str, Any] = {"verdict": "pass", "findings": [], "summary": "Adds an API module."}
FINDING: dict[str, Any] = {
    "rule": "harms_user",
    "file": "api.py",
    "line": 5,
    "explanation": "Sends the library to a host the description does not name.",
}
HOLD: dict[str, Any] = {
    "verdict": "hold",
    "findings": [FINDING],
    "summary": "Adds an API module that sends the library elsewhere.",
}


class FakeClient:
    """A client of the API that records each request and answers with what it is given."""

    def __init__(self, answer: Message | Exception | None = None) -> None:
        """
        Return a client that answers each request the same.

        :param answer: The message to answer with or the error to raise; None for no request.
        """
        self.answer = answer
        self.requests: list[dict[str, Any]] = []
        self.messages = self

    def create(self, **kwargs: Any) -> Message:
        """Record a request and answer it."""
        self.requests.append(kwargs)
        assert self.answer is not None, "the reading asked the API"
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


def _message(
    text: str | dict[str, Any], stop_reason: str = "end_turn", extra: Sequence[dict[str, Any]] = ()
) -> Message:
    """
    Return an answer of the API with a thinking block and a text block.

    :param text: The text, or the JSON it holds.
    :param stop_reason: Why the model stopped.
    :param extra: Content blocks after the text.
    """
    content = text if isinstance(text, str) else json.dumps(text)
    return Message.model_validate(
        {
            "id": "msg_1",
            "type": "message",
            "role": "assistant",
            "model": MODEL,
            "content": [
                {"type": "thinking", "thinking": "", "signature": "sig"},
                {"type": "text", "text": content},
                *extra,
            ],
            "stop_reason": stop_reason,
            "stop_sequence": None,
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }
    )


def _folder(tmp_path: Path, name: str, files: dict[str, bytes]) -> Path:
    """Return a provider folder unpacked from a GitHub archive of these files."""
    archive = tmp_path / f"{name}.tar.gz"
    archive.write_bytes(build_archive({f"{FOLDER}/{path}": data for path, data in files.items()}))
    unpack(archive, FOLDER, target := tmp_path / name)
    return target


def _read(
    tmp_path: Path,
    client: FakeClient,
    release: dict[str, bytes] = RELEASE,
    base: dict[str, bytes] | None = BASE,
) -> Reading:
    """Read a release against a base with the fake client."""
    return read_release(
        cast("anthropic.Anthropic", client),
        Side(_folder(tmp_path, "release", release), "1.1.0", "https://github.com/a/b/commit/2"),
        Side(_folder(tmp_path, "base", base), "1.0.0", "https://github.com/a/b/commit/1")
        if base is not None
        else None,
    )


def _status_error(kind: type[anthropic.APIStatusError], status: int) -> anthropic.APIStatusError:
    """Return an error the API answers with."""
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    return kind("error", response=httpx2.Response(status, request=request), body=None)


def test_changes_between_two_archives(tmp_path: Path) -> None:
    """Added, changed and removed files are listed; only text written by people is shown."""
    changes = {
        change.path: change
        for change in compare_folders(
            _folder(tmp_path, "release", RELEASE), _folder(tmp_path, "base", BASE)
        )
    }

    assert {path: (change.status, change.not_shown) for path, change in changes.items()} == {
        "__init__.py": (Status.CHANGED, None),
        "api.py": (Status.ADDED, None),
        "fonts/font.woff2": (Status.ADDED, "media or font"),
        "helpers.py": (Status.REMOVED, None),
        "logo.png": (Status.CHANGED, "media or font"),
        "manifest.json": (Status.CHANGED, None),
        "vendor/lib.py": (Status.ADDED, None),
        "web/app.min.js": (Status.ADDED, "minified or generated"),
        "web/page.js": (Status.ADDED, "minified or generated"),
    }
    assert not any(change.unreadable for change in changes.values())
    assert changes["api.py"].text == (
        "     1\timport json\n     2\t\n     3\t\n     4\tdef fetch():\n"
        "     5\t    return json.loads('{}')"
    )
    assert changes["api.py"].lines == 5
    assert changes["__init__.py"].text == (
        "--- a/__init__.py\n+++ b/__init__.py\n@@ -1,4 +1,4 @@\n import api\n \n"
        "-VALUE = 1\n+VALUE = 2\n NAME = 'sample'"
    )
    assert changes["helpers.py"].text == (
        "--- a/helpers.py\n+++ b/helpers.py\n@@ -1,2 +0,0 @@\n-def helper():\n-    return 1"
    )
    assert changes["helpers.py"].lines == 2
    assert changes["logo.png"].text is None
    assert changes["logo.png"].lines == 0


def test_a_first_read_takes_every_file_as_added(tmp_path: Path) -> None:
    """Without a base, every file is added and shown in full."""
    changes = compare_folders(_folder(tmp_path, "release", BASE), None)

    assert [(change.path, change.status) for change in changes] == [
        ("__init__.py", Status.ADDED),
        ("helpers.py", Status.ADDED),
        ("logo.png", Status.ADDED),
        ("manifest.json", Status.ADDED),
        ("unchanged.py", Status.ADDED),
    ]


def test_a_long_line_in_python_is_shown(tmp_path: Path) -> None:
    """Python with a long line runs in the server, so it is shown, not taken as generated."""
    changes = compare_folders(_folder(tmp_path, "release", {"code.py": b"x" * 2001 + b"\n"}), None)

    assert changes[0].not_shown is None


def test_a_pass(tmp_path: Path) -> None:
    """The model reads the change in one request without tools, and its pass is the verdict."""
    client = FakeClient(_message(PASS))

    reading = _read(tmp_path, client)

    assert reading == Reading(Verdict.PASS, summary="Adds an API module.")
    [request] = client.requests
    assert set(request) == {
        "model",
        "max_tokens",
        "thinking",
        "output_config",
        "system",
        "messages",
    }
    assert request["model"] == MODEL
    assert request["max_tokens"] == MAX_TOKENS
    assert request["thinking"] == {"type": "adaptive"}
    assert request["output_config"]["effort"] == EFFORT
    assert request["output_config"]["format"]["type"] == "json_schema"
    assert "# The rules of the community catalog" in request["system"]
    assert "## What is not accepted" in request["system"]
    [message] = request["messages"]
    assert message["role"] == "user"
    assert "     5\t    return json.loads('{}')" in message["content"]
    assert "-VALUE = 1\n+VALUE = 2" in message["content"]
    assert "unchanged.py" not in message["content"]


def test_the_data_lies_between_lines_of_a_random_boundary(tmp_path: Path) -> None:
    """Each request names its own boundary, which opens every section and ends the data."""
    client = FakeClient(_message(PASS))
    (tmp_path / "one").mkdir()
    (tmp_path / "two").mkdir()

    _read(tmp_path / "one", client)
    _read(tmp_path / "two", client)

    first, second = (request["messages"][0]["content"] for request in client.requests)
    boundary = re.search(r"<<<([0-9a-f]{32}) header>>>", first)
    assert boundary is not None
    assert boundary.group(1) not in second
    sections = re.findall(rf"^<<<{boundary.group(1)} (.*)>>>$", first, re.MULTILINE)
    assert sections == [
        "header",
        "files",
        'file "__init__.py": unified diff',
        'file "api.py": numbered lines',
        'file "helpers.py": unified diff',
        'file "manifest.json": unified diff',
        'file "vendor/lib.py": numbered lines',
        "end",
    ]
    header = json.loads(first.split("\n")[2])
    assert header["version"] == "1.1.0"
    assert header["base_version"] == "1.0.0"
    assert header["requirements"] == ["some-package==2.0"]
    assert header["base_requirements"] == ["some-package==1.0"]


def test_a_hold(tmp_path: Path) -> None:
    """A hold of the model, with its findings, is the verdict."""
    reading = _read(tmp_path, FakeClient(_message(HOLD)))

    assert reading == Reading(
        Verdict.HOLD,
        findings=[
            ReadingFinding(
                Rule.HARMS_USER,
                "api.py",
                5,
                "Sends the library to a host the description does not name.",
            )
        ],
        summary="Adds an API module that sends the library elsewhere.",
    )


@pytest.mark.parametrize(
    ("stop_reason", "reason"),
    [
        ("refusal", REFUSED),
        ("max_tokens", CUT_OFF),
        ("model_context_window_exceeded", CUT_OFF),
        ("pause_turn", INVALID_ANSWER),
    ],
)
def test_an_answer_that_did_not_finish_is_a_hold(
    tmp_path: Path, stop_reason: str, reason: str
) -> None:
    """A refusal or an answer that stopped early is a hold, whatever text it holds."""
    reading = _read(tmp_path, FakeClient(_message(PASS, stop_reason)))

    assert reading == Reading(Verdict.HOLD, reason=reason)


@pytest.mark.parametrize(
    "answer",
    [
        "not JSON",
        '{"verdict": "pass", "findings": [], "summary": "a", "summary": "b"}',
        {**PASS, "extra": True},
        {**PASS, "verdict": "maybe"},
        {"verdict": "pass", "findings": []},
        {**HOLD, "findings": [{**FINDING, "rule": "other"}]},
        {**HOLD, "findings": [{**FINDING, "line": "5"}]},
        [PASS],
    ],
    ids=[
        "not-json",
        "duplicate-key",
        "extra-key",
        "unknown-verdict",
        "missing-key",
        "unknown-rule",
        "line-as-text",
        "not-an-object",
    ],
)
def test_a_malformed_answer_is_a_hold(tmp_path: Path, answer: Any) -> None:
    """An answer that is not exactly the answer format is a hold."""
    text = answer if isinstance(answer, str) else json.dumps(answer)

    reading = _read(tmp_path, FakeClient(_message(text)))

    assert reading == Reading(Verdict.HOLD, reason=INVALID_ANSWER)


@pytest.mark.parametrize(
    "extra",
    [
        [{"type": "text", "text": json.dumps(PASS)}],
        [{"type": "tool_use", "id": "toolu_1", "name": "run", "input": {}}],
    ],
    ids=["two-texts", "tool-use"],
)
def test_an_answer_with_other_blocks_is_a_hold(tmp_path: Path, extra: list[dict[str, Any]]) -> None:
    """An answer of more than one text, or with a block of another kind, is a hold."""
    reading = _read(tmp_path, FakeClient(_message(PASS, extra=extra)))

    assert reading == Reading(Verdict.HOLD, reason=INVALID_ANSWER)


@pytest.mark.parametrize(
    "answer",
    [
        {**PASS, "findings": [FINDING]},
        {**HOLD, "findings": []},
        {**HOLD, "findings": [{**FINDING, "line": 6}]},
        {**HOLD, "findings": [{**FINDING, "line": 0}]},
        {**HOLD, "findings": [{**FINDING, "file": "logo.png", "line": 1}]},
        {**HOLD, "findings": [{**FINDING, "explanation": "x" * 401}]},
        {**PASS, "summary": "x" * 601},
    ],
    ids=[
        "pass-with-findings",
        "hold-without-findings",
        "line-past-the-end",
        "line-zero",
        "line-of-a-file-not-shown",
        "long-explanation",
        "long-summary",
    ],
)
def test_an_inconsistent_answer_is_a_hold(tmp_path: Path, answer: dict[str, Any]) -> None:
    """An answer whose verdict, findings, lines or lengths do not hold together is a hold."""
    reading = _read(tmp_path, FakeClient(_message(answer)))

    assert reading == Reading(Verdict.HOLD, reason=INVALID_ANSWER)


@pytest.mark.parametrize("file", ["unchanged.py", "elsewhere.py", "API.PY"])
def test_a_finding_on_a_file_not_in_the_input_is_a_hold(tmp_path: Path, file: str) -> None:
    """A finding may only name a file of the file table: not an unchanged or an unknown one."""
    answer = {**HOLD, "findings": [{**FINDING, "file": file, "line": None}]}

    reading = _read(tmp_path, FakeClient(_message(answer)))

    assert reading == Reading(Verdict.HOLD, reason=INVALID_ANSWER)


def test_a_finding_on_a_whole_file_not_shown(tmp_path: Path) -> None:
    """A finding without a line may name a file that is listed but not shown."""
    finding = {**FINDING, "file": "web/app.min.js", "line": None}

    reading = _read(tmp_path, FakeClient(_message({**HOLD, "findings": [finding]})))

    assert reading.verdict is Verdict.HOLD
    assert reading.reason is None


@pytest.mark.parametrize(
    "error",
    [
        anthropic.APIConnectionError(
            request=httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
        ),
        anthropic.APITimeoutError(
            request=httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
        ),
        _status_error(anthropic.RateLimitError, 429),
        _status_error(anthropic.InternalServerError, 500),
        _status_error(anthropic.OverloadedError, 529),
        _status_error(anthropic.AuthenticationError, 401),
        _status_error(anthropic.PaymentRequiredError, 402),
        _status_error(anthropic.BadRequestError, 400),
    ],
    ids=["connection", "timeout", "429", "500", "529", "401", "402", "400"],
)
def test_a_failure_of_the_api_leaves_the_release_pending(tmp_path: Path, error: Exception) -> None:
    """A failure that says nothing about the release gives no verdict: it is read again later."""
    with pytest.raises(ReadingUnavailableError):
        _read(tmp_path, FakeClient(error))


def test_a_request_too_large_for_the_api_is_a_hold(tmp_path: Path) -> None:
    """The API refusing the size of the request is a hold, as a change above MAX_SHOWN is."""
    reading = _read(tmp_path, FakeClient(_status_error(anthropic.ContentTooLargeError, 413)))

    assert reading == Reading(Verdict.HOLD, reason=TOO_LARGE)


def test_a_change_larger_than_the_reading_takes_is_held_without_a_request(tmp_path: Path) -> None:
    """A change with more shown text than MAX_SHOWN is held without asking the model."""
    line = b"x = 1  # " + b"y" * 90 + b"\n"
    big = {**RELEASE, "big.py": line * (MAX_SHOWN // len(line) + 1)}

    reading = _read(tmp_path, FakeClient(), release=big)

    assert reading == Reading(Verdict.HOLD, reason=TOO_LARGE)


@pytest.mark.parametrize(
    ("path", "content"),
    [
        ("native.so", b"\x7fELF\x02\x01\x01\x00"),
        ("tool.exe", b"text, but named as code\n"),
        ("bundle.whl", b"PK\x03\x04\x14\x00"),
        ("data.bin", b"\x00\x01\x02\x03"),
        ("cover.png", b"\x7fELF\x02\x01\x01\x00"),
        ("archive.tar", b"\x00" * 257 + b"ustar\x00"),
        ("payload.txt", b"\xff\xfe\x00not utf-8"),
    ],
    ids=["elf", "exe", "wheel", "unknown", "elf-as-media", "tar", "not-utf-8"],
)
def test_binary_code_is_held_without_a_request(tmp_path: Path, path: str, content: bytes) -> None:
    """Added or changed binary code or data the reading cannot read is held without a request."""
    reading = _read(tmp_path, FakeClient(), release={**RELEASE, path: content})

    assert reading == Reading(
        Verdict.HOLD,
        reason=UNREADABLE,
        findings=[ReadingFinding(Rule.UNCLEAR, path, None, UNREADABLE)],
    )


def test_removed_binary_code_is_no_hold(tmp_path: Path) -> None:
    """Binary code that a release removes is listed, and the reading goes on."""
    client = FakeClient(_message(PASS))

    reading = _read(tmp_path, client, base={**BASE, "native.so": b"\x7fELF\x02"})

    assert reading.verdict is Verdict.PASS
    assert (
        '"file": "native.so", "status": "removed"' in client.requests[0]["messages"][0]["content"]
    )


def test_rules_are_the_two_sections_of_the_rules_page() -> None:
    """
    The rules the reading follows are the sections of the rules page that say what is accepted.

    rules.md is a verbatim copy of the sections "What is not accepted" and "What is allowed" of
    COMMUNITY_CATALOG_RULES.md in music-assistant/.github. A change to those sections is copied
    here in the same week, with:

    gh api repos/music-assistant/.github/contents/COMMUNITY_CATALOG_RULES.md \
      -H "Accept: application/vnd.github.raw" \
      | sed -n '/^## What is not accepted$/,/^## Getting a provider listed$/p' | sed '$d' \
      | sed '$d' > registry/reading/rules.md
    """
    rules = (INSTRUCTIONS_FOLDER / "rules.md").read_text(encoding="utf-8")

    assert re.findall(r"^#.*$", rules, re.MULTILINE) == [
        "## What is not accepted",
        "## What is allowed",
    ]
    assert rules.startswith("## What is not accepted\n")
    assert rules.endswith(".\n")
    assert not rules.endswith("\n\n")


def test_read_archive_prints_the_verdict_and_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The local command reads an archive against a base and prints the reading as JSON."""
    client = FakeClient(_message(HOLD))
    monkeypatch.setattr(anthropic, "Anthropic", lambda: client)
    (tmp_path / "release.tar.gz").write_bytes(
        build_archive({f"{FOLDER}/{path}": data for path, data in RELEASE.items()})
    )
    (tmp_path / "base.tar.gz").write_bytes(
        build_archive({f"{FOLDER}/{path}": data for path, data in BASE.items()})
    )
    before = sorted(tmp_path.rglob("*"))

    code = main(["read-archive", str(tmp_path / "release.tar.gz"), "--folder", FOLDER,
                 "--base", str(tmp_path / "base.tar.gz")])  # fmt: skip

    assert code == 0
    assert json.loads(capsys.readouterr().out) == {**HOLD, "reason": None}
    assert sorted(tmp_path.rglob("*")) == before
    assert "-VALUE = 1\n+VALUE = 2" in client.requests[0]["messages"][0]["content"]


def test_read_archive_without_a_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """When the API cannot be used, the command says the release is pending and fails."""
    client = FakeClient(_status_error(anthropic.RateLimitError, 429))
    monkeypatch.setattr(anthropic, "Anthropic", lambda: client)
    (tmp_path / "release.tar.gz").write_bytes(
        build_archive({f"{FOLDER}/{path}": data for path, data in RELEASE.items()})
    )

    assert main(["read-archive", str(tmp_path / "release.tar.gz"), "--folder", FOLDER]) == 1

    output = capsys.readouterr()
    assert output.out == ""
    assert output.err == "pending: the API answered 429\n"


def test_read_archive_of_another_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An archive without the folder is an error, and nothing is asked."""
    client = FakeClient()
    monkeypatch.setattr(anthropic, "Anthropic", lambda: client)
    (tmp_path / "release.tar.gz").write_bytes(build_archive({"other/x.py": b"x = 1\n"}))

    assert main(["read-archive", str(tmp_path / "release.tar.gz"), "--folder", FOLDER]) == 1

    assert f"the repository has no folder {FOLDER}" in capsys.readouterr().err
    assert client.requests == []
