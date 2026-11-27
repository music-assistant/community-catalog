"""Tests for the reading of a release against the rules, with a fake client of the API."""

from __future__ import annotations

import io
import json
import re
import urllib.request
import zlib
from typing import TYPE_CHECKING, Any, BinaryIO, Self, cast

import anthropic
import httpx2
import pytest
from anthropic.types import Message, MessageTokensCount

from registry import reading as reading_module
from registry.cli import main
from registry.content import unpack
from registry.github import GitHub
from registry.models import Reading, ReadingFinding, Rule, Verdict
from registry.reading import (
    ANSWER_SCHEMA,
    AUDIENCE,
    CUT_OFF,
    EFFORT,
    INSTRUCTIONS_FOLDER,
    MAX_EXPLANATION,
    MAX_INPUT_TOKENS,
    MAX_REQUEST,
    MAX_SUMMARY,
    MAX_TOKENS,
    MODEL,
    NO_FINDING,
    REFUSED,
    TOO_LARGE,
    UNREADABLE,
    ReadingConfigurationError,
    ReadingRejectedError,
    ReadingUnavailableError,
    Side,
    Status,
    TokenUsage,
    compare_folders,
    federated_client,
    identity_token_provider,
    read_release,
    system_prompt,
)
from tests.conftest import DOMAIN, build_archive, manifest

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

FOLDER = f"providers/{DOMAIN}"
# where a workflow job requests its identity tokens
REQUEST_URL = "https://pipelines.actions.githubusercontent.com/abc/idtoken?api-version=2.0"


def _png(data: bytes = b"\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00") -> bytes:
    """Return a PNG of an IHDR chunk with this data, and IEND."""
    chunks = b""
    for kind, body in ((b"IHDR", data), (b"IEND", b"")):
        crc = zlib.crc32(kind + body).to_bytes(4)
        chunks += len(body).to_bytes(4) + kind + body + crc
    return b"\x89PNG\r\n\x1a\n" + chunks


def _riff(form: bytes, body: bytes = b"fmt \x00\x00") -> bytes:
    """Return a RIFF file of a form."""
    return b"RIFF" + (4 + len(body)).to_bytes(4, "little") + form + body


def _ico(image: bytes = b"\x89\x00image") -> bytes:
    """Return an icon file with one image."""
    entry = b"\x10\x10\x00\x00\x01\x00\x20\x00" + len(image).to_bytes(4, "little")
    return b"\x00\x00\x01\x00\x01\x00" + entry + (22).to_bytes(4, "little") + image


def _sfnt(version: bytes = b"\x00\x01\x00\x00", offset: int = 28) -> bytes:
    """Return a font with one table."""
    record = b"head" + b"\x00" * 4 + offset.to_bytes(4) + (4).to_bytes(4)
    return version + (1).to_bytes(2) + b"\x00" * 6 + record + b"\x00\x01\x00\x00"


def _mp4(*boxes: bytes) -> bytes:
    """Return an MP4 file: an ftyp box, then these boxes."""
    return b"\x00\x00\x00\x14ftypM4A \x00\x00\x00\x00M4A " + b"".join(boxes)


def _box(kind: bytes, body: bytes = b"", size: int | None = None) -> bytes:
    """Return a box of an MP4 file, of its own size unless another is given."""
    return (len(body) + 8 if size is None else size).to_bytes(4) + kind + body


def _woff(signature: bytes, extra: int = 0) -> bytes:
    """Return a WOFF or WOFF2 font that gives its length."""
    return signature + b"\x00\x01\x00\x00" + (16 + extra).to_bytes(4) + b"\x00" * 4


MEDIA = {
    "image.png": _png(),
    "image.jpg": b"\xff\xd8\xff\xe0\x00\x10JFIF\xff\xd9",
    "image.jpeg": b"\xff\xd8\xff\xe1\x00\x10Exif\xff\xd9",
    "image.gif": b"GIF89a\x01\x00\x01\x00\x00;",
    "image.webp": _riff(b"WEBP", b"VP8 \x00\x00"),
    "sound.wav": _riff(b"WAVE"),
    "icon.ico": _ico(),
    "sound.flac": b"fLaC\x00\x00\x00\x22",
    "sound.ogg": b"OggS\x00\x02",
    "sound.opus": b"OggS\x00\x02",
    "sound.m4a": _mp4(_box(b"moov", b"\x00" * 8), _box(b"mdat", b"audio")),
    "last.m4a": _mp4(_box(b"mdat", b"audio to the end", size=0)),
    "large.m4a": _mp4((1).to_bytes(4) + b"mdat" + (20).to_bytes(8) + b"\x00" * 4),
    "sound.mp3": b"ID3\x04\x00",
    "frames.mp3": b"\xff\xfb\x90\x00",
    "font.woff": _woff(b"wOFF"),
    "font.woff2": _woff(b"wOF2"),
    "font.ttf": _sfnt(),
    "font.otf": _sfnt(b"OTTO"),
}
BASE = {
    "manifest.json": json.dumps(manifest(requirements=["some-package==1.0"])).encode(),
    "__init__.py": b"import api\n\nVALUE = 1\nNAME = 'sample'\n",
    "helpers.py": b"def helper():\n    return 1\n",
    "unchanged.py": b"STAYS = True\n",
    "logo.png": _png(b"\x00old"),
}
RELEASE = {
    "manifest.json": json.dumps(manifest(requirements=["some-package==2.0"])).encode(),
    "__init__.py": b"import api\n\nVALUE = 2\nNAME = 'sample'\n",
    "unchanged.py": b"STAYS = True\n",
    "api.py": b"import json\n\n\ndef fetch():\n    return json.loads('{}')\n",
    "vendor/lib.py": b"VENDORED = 1\n",
    "logo.png": _png(b"\x00new"),
    "web/app.min.js": b"var a=1;\n",
    "web/page.js": b"x" * 2001 + b"\n",
    "fonts/font.woff2": _woff(b"wOF2"),
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


class FakeStream:
    """A streamed answer of the API, which ends in a message or breaks off with an error."""

    def __init__(self, answer: Message | Exception) -> None:
        """
        Return a streamed answer.

        :param answer: The message it ends in, or the error it breaks off with.
        """
        self.answer = answer

    def __enter__(self) -> Self:
        """Open the stream."""
        return self

    def __exit__(self, *_: object) -> None:
        """Close the stream."""

    def get_final_message(self) -> Message:
        """Return the message the stream ends in, or raise the error it breaks off with."""
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


class FakeClient:
    """A client of the API that records each request and answers with what it is given."""

    def __init__(self, *answers: Message | Exception, tokens: int | Exception = 1) -> None:
        """
        Return a client that answers the requests in turn, and any after them as the last.

        :param answers: The messages to answer with or the errors to raise; none for no request.
        :param tokens: The input tokens of each request as the API counts them, or the error
            counting them raises.
        """
        self.answers = answers
        self.tokens = tokens
        self.requests: list[dict[str, Any]] = []
        self.counted: list[dict[str, Any]] = []
        self.messages = self

    def count_tokens(self, **kwargs: Any) -> MessageTokensCount:
        """Record a request whose tokens are counted, and count them."""
        self.counted.append(kwargs)
        assert self.answers, "the reading asked the API"
        if isinstance(self.tokens, Exception):
            raise self.tokens
        return MessageTokensCount(input_tokens=self.tokens)

    def stream(self, **kwargs: Any) -> FakeStream:
        """Record a request and stream the answer of its turn."""
        self.requests.append(kwargs)
        assert self.answers, "the reading asked the API"
        return FakeStream(self.answers[min(len(self.requests), len(self.answers)) - 1])


class FakeTokenService:
    """Where a workflow job requests its identity tokens, as a transport of the GitHub client."""

    def __init__(self, status: int = 200, answer: Any = None) -> None:
        """
        Return a service that answers every request the same, or with a new token each.

        :param status: The status of each answer.
        :param answer: The JSON of each answer; None for a new identity token each time.
        """
        self.status = status
        self.answer = answer
        self.requests: list[urllib.request.Request] = []

    def __call__(self, request: urllib.request.Request) -> tuple[int, BinaryIO]:
        """Answer a request."""
        self.requests.append(request)
        answer = self.answer if self.answer is not None else {"value": f"jwt-{len(self.requests)}"}
        return self.status, io.BytesIO(json.dumps(answer).encode())


class FakeAPI:
    """The token exchange and the messages of the Claude API, as a transport of httpx2."""

    def __init__(self, exchange: int = 200, messages: int = 200, lifetime: int = 600) -> None:
        """
        Return an API that answers with these statuses, and a pass to each message.

        :param exchange: The status of each token exchange.
        :param messages: The status of each request for a message.
        :param lifetime: How many seconds a token of the API lives.
        """
        self.exchange = exchange
        self.messages = messages
        self.lifetime = lifetime
        self.exchanges: list[dict[str, Any]] = []
        self.requests: list[httpx2.Request] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        """Answer a request."""
        if request.url.path == "/v1/oauth/token":
            self.exchanges.append(json.loads(request.content))
            if self.exchange != 200:
                return httpx2.Response(self.exchange, json={"error": "invalid_grant"})
            token = {
                "access_token": f"token-{len(self.exchanges)}",
                "expires_in": self.lifetime,
                "token_type": "Bearer",
            }
            return httpx2.Response(200, json=token)
        self.requests.append(request)
        if request.url.path == "/v1/messages/count_tokens":
            return httpx2.Response(200, json={"input_tokens": 1})
        if self.messages != 200:
            # tried again by the client at once
            error = {"type": "error", "error": {"type": "authentication_error", "message": "no"}}
            return httpx2.Response(self.messages, headers={"retry-after-ms": "1"}, json=error)
        return httpx2.Response(
            200, headers={"content-type": "text/event-stream"}, content=_events(_message(PASS))
        )

    def client(self, service: FakeTokenService) -> anthropic.Anthropic:
        """Return the client of the read step, with the identity tokens of a token service."""
        return federated_client(
            identity_token_provider(GitHub("request-token", service), REQUEST_URL),
            organization_id="organization",
            federation_rule_id="fdrl_rule",
            service_account_id="svac_account",
            workspace_id="wrkspc_workspace",
            http_client=httpx2.Client(transport=httpx2.MockTransport(self)),
        )


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


def _events(message: Message) -> bytes:
    """Return a message as the events of a stream of the API, each block whole at its start."""
    content = message.model_dump(mode="json")["content"]
    start = message.model_dump(mode="json") | {"content": [], "stop_reason": None}
    events: list[dict[str, Any]] = [{"type": "message_start", "message": start}]
    for index, block in enumerate(content):
        events.append({"type": "content_block_start", "index": index, "content_block": block})
        events.append({"type": "content_block_stop", "index": index})
    events.append(
        {
            "type": "message_delta",
            "delta": {"stop_reason": message.stop_reason, "stop_sequence": None},
            "usage": {"output_tokens": message.usage.output_tokens},
        }
    )
    events.append({"type": "message_stop"})
    return "".join(
        f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events
    ).encode()


def _folder(tmp_path: Path, name: str, files: dict[str, bytes]) -> Path:
    """Return a provider folder unpacked from a GitHub archive of these files."""
    archive = tmp_path / f"{name}.tar.gz"
    archive.parent.mkdir(parents=True, exist_ok=True)
    archive.write_bytes(build_archive({f"{FOLDER}/{path}": data for path, data in files.items()}))
    unpack(archive, FOLDER, target := tmp_path / name)
    return target


def _read(
    tmp_path: Path,
    client: FakeClient | anthropic.Anthropic,
    release: dict[str, bytes] = RELEASE,
    base: dict[str, bytes] | None = BASE,
    usage: TokenUsage | None = None,
) -> Reading:
    """Read a release against a base with a client, the fake one or one with a fake API."""
    return read_release(
        cast("anthropic.Anthropic", client),
        Side(_folder(tmp_path, "release", release), "1.1.0", "https://github.com/a/b/commit/2"),
        Side(_folder(tmp_path, "base", base), "1.0.0", "https://github.com/a/b/commit/1")
        if base is not None
        else None,
        usage,
    )


def _status_error(kind: type[anthropic.APIStatusError], status: int) -> anthropic.APIStatusError:
    """Return an error the API answers with."""
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    return kind("error", response=httpx2.Response(status, request=request), body=None)


def _assert_not_valid(tmp_path: Path, message: Message, flaw: str) -> None:
    """Assert that reading with this answer leaves the release pending, for this flaw."""
    with pytest.raises(ReadingUnavailableError) as raised:
        _read(tmp_path, FakeClient(message))

    assert str(raised.value) == f"the answer of the reading is not valid: {flaw}"


def _read_long_file(tmp_path: Path, line: int | None) -> Reading:
    """Read a file changed on line 15, with a finding on a line of it."""
    code = [f"line_{number} = {number}\n".encode() for number in range(1, 21)]
    changed = [*code[:14], b"line_15 = 'changed'\n", *code[15:]]
    answer = {**HOLD, "findings": [{**FINDING, "file": "long.py", "line": line}]}
    return _read(
        tmp_path,
        FakeClient(_message(answer)),
        release={**RELEASE, "long.py": b"".join(changed)},
        base={**BASE, "long.py": b"".join(code)},
    )


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
        "web/app.min.js": (Status.ADDED, None),
        "web/page.js": (Status.ADDED, None),
    }
    assert not any(change.unreadable for change in changes.values())
    assert changes["api.py"].text == (
        "     1\timport json\n     2\t\n     3\t\n     4\tdef fetch():\n"
        "     5\t    return json.loads('{}')"
    )
    assert changes["api.py"].shown_lines == (range(1, 6),)
    assert changes["__init__.py"].text == (
        "--- a/__init__.py\n+++ b/__init__.py\n@@ -1,4 +1,4 @@\n import api\n \n"
        "-VALUE = 1\n+VALUE = 2\n NAME = 'sample'"
    )
    assert changes["helpers.py"].text == (
        "--- a/helpers.py\n+++ b/helpers.py\n@@ -1,2 +0,0 @@\n-def helper():\n-    return 1"
    )
    assert changes["helpers.py"].shown_lines == (range(1, 3),)
    assert changes["logo.png"].text is None
    assert changes["logo.png"].shown_lines == ()


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


@pytest.mark.parametrize(
    "path", ["code.py", "settings.json", "page.js", "style.css", "notes.txt", "page.html"]
)
def test_long_lines_are_shown(tmp_path: Path, path: str) -> None:
    """Text with a long line is shown."""
    content = b'{"key": "' + b"x" * 2001 + b'"}\n'

    changes = compare_folders(_folder(tmp_path, "release", {path: content}), None)

    assert (changes[0].not_shown, changes[0].text is not None) == (None, True)


@pytest.mark.parametrize("path", ["app.min.js", "style.min.css", "app.js.map", "APP.MIN.JS"])
def test_built_web_assets_are_shown(tmp_path: Path, path: str) -> None:
    """Minified scripts and styles and source maps can reach the browser, so they are read too."""
    changes = compare_folders(_folder(tmp_path, "release", {path: b"var a=1;\n"}), None)

    assert (changes[0].not_shown, changes[0].text) == (None, "     1\tvar a=1;")


@pytest.mark.parametrize("path", sorted(MEDIA))
def test_media_of_its_own_format_is_listed(tmp_path: Path, path: str) -> None:
    """Media and fonts whose content is the format their name says are listed, not read."""
    changes = compare_folders(_folder(tmp_path, "release", {path: MEDIA[path]}), None)

    assert (changes[0].not_shown, changes[0].unreadable) == ("media or font", False)


@pytest.mark.parametrize(
    ("path", "content"),
    [
        ("cover.png", b"\x00\x01 arbitrary binary data"),
        ("cover.png", _png()[:-1] + b"\x00"),
        ("cover.png", _png() + b"\x7fELF appended"),
        ("cover.png", _png()[:-12]),
        ("cover.jpg", _png()),
        ("cover.jpg", b"\xff\xd8\xff\xe0\x00\x10JFIF\x00"),
        ("cover.gif", b"GIF89a\x01\x00\x01\x00\x00"),
        ("cover.webp", _riff(b"WEBP") + b"\x00\x00"),
        ("sound.wav", _riff(b"WEBP")),
        ("icon.ico", _ico()[:-3]),
        ("sound.flac", b"fLaX\x00\x00"),
        ("sound.ogg", b"OggX\x00\x02"),
        ("sound.opus", b"OggX\x00\x02"),
        ("sound.m4a", _box(b"moov") + _mp4()),
        ("sound.m4a", _mp4(_box(b"mdat", b"audio", size=40))),
        ("sound.m4a", _mp4(_box(b"mdat", size=4))),
        ("sound.m4a", _mp4(b"\x7fELF")),
        ("sound.mp3", b"\xff\x10\x00\x00"),
        ("font.woff", _woff(b"wOFF", extra=1)),
        ("font.woff2", _woff(b"wOFF")),
        ("font.ttf", _sfnt(offset=400)),
        ("font.otf", _sfnt(b"\x00\x00\x00\x00")),
    ],
    ids=[
        "arbitrary-png",
        "png-bad-crc",
        "png-data-after-iend",
        "png-without-iend",
        "png-named-jpg",
        "jpg-without-end",
        "gif-without-trailer",
        "webp-of-another-size",
        "wav-of-another-form",
        "ico-image-outside",
        "flac-magic",
        "ogg-magic",
        "opus-magic",
        "m4a-without-ftyp-first",
        "m4a-box-past-the-end",
        "m4a-box-smaller-than-its-header",
        "m4a-data-after-the-boxes",
        "mp3-magic",
        "woff-of-another-length",
        "woff-named-woff2",
        "ttf-table-outside",
        "otf-version",
    ],
)
def test_media_of_another_format_is_held(tmp_path: Path, path: str, content: bytes) -> None:
    """Binary data named as media or a font that is not that format is held without a request."""
    reading = _read(tmp_path, FakeClient(), release={**RELEASE, path: content})

    assert reading == Reading(
        Verdict.HOLD,
        reason=UNREADABLE,
        findings=[ReadingFinding(Rule.UNCLEAR, path, None, UNREADABLE)],
    )


def test_lines_end_as_python_reads_them(tmp_path: Path) -> None:
    """Lines end at CR LF, CR and LF only, as Python numbers the lines of source."""
    code = b"a = 1\rb = 2\r\nc = '\x0c\x1c\xe2\x80\xa8'\n"

    [change] = compare_folders(_folder(tmp_path, "release", {"code.py": code}), None)

    assert change.text == "     1\ta = 1\n     2\tb = 2\n     3\tc = '\x0c\x1c\u2028'"
    assert change.shown_lines == (range(1, 4),)


def test_a_change_of_the_end_of_a_file_shows(tmp_path: Path) -> None:
    """A change of only the line end at the end of a file is a diff, with the end marked."""
    [change] = compare_folders(
        _folder(tmp_path, "release", {"code.py": b"a = 1\nb = 2\n"}),
        _folder(tmp_path, "base", {"code.py": b"a = 1\nb = 2"}),
    )

    assert change.text == (
        "--- a/code.py\n+++ b/code.py\n@@ -1,2 +1,2 @@\n a = 1\n-b = 2\n"
        "\\ No newline at end of file\n+b = 2"
    )


def test_a_pass(tmp_path: Path) -> None:
    """The model reads the change in one request without tools, and its pass is the verdict."""
    client = FakeClient(_message(PASS))

    reading = _read(tmp_path, client)

    assert reading == Reading(Verdict.PASS, summary="Adds an API module.")
    [request] = client.requests
    # its tokens are counted with the same settings first
    assert client.counted == [{key: request[key] for key in request if key != "max_tokens"}]
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
        'file "web/app.min.js": numbered lines',
        'file "web/page.js": numbered lines',
        "end",
    ]
    header = json.loads(first.split("\n")[2])
    assert header["version"] == "1.1.0"
    assert header["base_version"] == "1.0.0"
    assert header["requirements"] == ["some-package==2.0"]
    assert header["base_requirements"] == ["some-package==1.0"]


def test_a_tool_that_steers_an_assistant_is_a_rule_of_its_own(tmp_path: Path) -> None:
    """The answer format and its check take a finding on a tool that steers an AI assistant."""
    finding = {**FINDING, "rule": "steers_assistant", "file": "manifest.json", "line": None}

    reading = _read(tmp_path, FakeClient(_message({**HOLD, "findings": [finding]})))

    rules = ANSWER_SCHEMA["properties"]["findings"]["items"]["properties"]["rule"]["enum"]
    assert {"steers_assistant", "addresses_reader"} <= set(rules)
    assert reading.reason is None
    assert [found.rule for found in reading.findings] == [Rule.STEERS_ASSISTANT]


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
    ],
)
def test_a_refused_or_cut_off_answer_is_a_hold(
    tmp_path: Path, stop_reason: str, reason: str
) -> None:
    """A refusal, or an answer that was cut off twice, is a hold, whatever text it holds."""
    client = FakeClient(_message(PASS, stop_reason))

    reading = _read(tmp_path, client)

    assert reading == Reading(Verdict.HOLD, reason=reason)
    assert len(client.requests) == (1 if reason == REFUSED else 2)


def test_a_cut_off_answer_is_asked_for_once_more(tmp_path: Path) -> None:
    """An answer that ran out of tokens is asked for again, and the second answer counts."""
    client = FakeClient(_message(HOLD, "max_tokens"), _message(PASS))

    reading = _read(tmp_path, client)

    assert reading == Reading(Verdict.PASS, summary="Adds an API module.")
    assert client.requests[0] == client.requests[1]


def test_the_tokens_of_the_requests_are_added_up(tmp_path: Path) -> None:
    """The input and output tokens of every request of a reading are counted, retries too."""
    usage = TokenUsage(input_tokens=10, output_tokens=20)

    _read(tmp_path, FakeClient(_message(PASS, "max_tokens"), _message(PASS)), usage=usage)

    assert usage == TokenUsage(input_tokens=12, output_tokens=22)


@pytest.mark.parametrize("stop_reason", ["pause_turn", "tool_use"])
def test_an_answer_that_stopped_for_another_reason_leaves_the_release_pending(
    tmp_path: Path, stop_reason: str
) -> None:
    """An answer that did not end its turn is not valid, whatever text it holds."""
    _assert_not_valid(tmp_path, _message(PASS, stop_reason), "it did not end")


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
def test_a_malformed_answer_leaves_the_release_pending(tmp_path: Path, answer: Any) -> None:
    """An answer that is not exactly the answer format is not valid."""
    text = answer if isinstance(answer, str) else json.dumps(answer)

    _assert_not_valid(tmp_path, _message(text), "it is not in the answer format")


@pytest.mark.parametrize(
    "extra",
    [
        [{"type": "text", "text": json.dumps(PASS)}],
        [{"type": "tool_use", "id": "toolu_1", "name": "run", "input": {}}],
    ],
    ids=["two-texts", "tool-use"],
)
def test_an_answer_with_other_blocks_leaves_the_release_pending(
    tmp_path: Path, extra: list[dict[str, Any]]
) -> None:
    """An answer of more than one text, or with a block of another kind, is not valid."""
    _assert_not_valid(tmp_path, _message(PASS, extra=extra), "it is not one text")


def test_a_pass_with_findings_is_a_hold(tmp_path: Path) -> None:
    """Findings make a hold, whatever the verdict says."""
    reading = _read(tmp_path, FakeClient(_message({**PASS, "findings": [FINDING]})))

    assert reading == Reading(
        Verdict.HOLD,
        findings=[ReadingFinding(Rule.HARMS_USER, "api.py", 5, FINDING["explanation"])],
        summary=PASS["summary"],
    )


def test_a_hold_without_findings_is_a_hold(tmp_path: Path) -> None:
    """A hold that names no finding is held for a reason of the code's own."""
    reading = _read(tmp_path, FakeClient(_message({**HOLD, "findings": []})))

    assert reading == Reading(Verdict.HOLD, reason=NO_FINDING, summary=HOLD["summary"])


def test_texts_that_are_too_long_are_shortened(tmp_path: Path) -> None:
    """An explanation or summary beyond its length is cut, and marked where it was cut."""
    answer = {
        **HOLD,
        "findings": [{**FINDING, "explanation": "e" * 401}],
        "summary": "s" * 601,
    }

    reading = _read(tmp_path, FakeClient(_message(answer)))

    assert reading.verdict is Verdict.HOLD
    assert reading.findings[0].explanation == "e" * (MAX_EXPLANATION - 1) + "…"
    assert reading.summary == "s" * (MAX_SUMMARY - 1) + "…"


def test_a_pass_with_a_long_summary_is_a_pass(tmp_path: Path) -> None:
    """A summary that is too long is shortened; the pass stays a pass."""
    reading = _read(tmp_path, FakeClient(_message({**PASS, "summary": "s" * 601})))

    assert reading == Reading(Verdict.PASS, summary="s" * (MAX_SUMMARY - 1) + "…")


@pytest.mark.parametrize(
    ("finding", "file", "line"),
    [
        ({**FINDING, "file": "elsewhere.py"}, "", None),
        ({**FINDING, "file": "unchanged.py", "line": None}, "", None),
        ({**FINDING, "file": "API.PY", "line": None}, "", None),
        ({**FINDING, "line": 6}, "api.py", None),
        ({**FINDING, "line": 0}, "api.py", None),
        ({**FINDING, "file": "logo.png", "line": 1}, "logo.png", None),
    ],
    ids=[
        "file-not-in-the-table",
        "unchanged-file",
        "file-of-another-case",
        "line-past-the-end",
        "line-zero",
        "line-of-a-file-not-shown",
    ],
)
def test_a_citation_the_model_was_not_shown_is_left_out(
    tmp_path: Path, finding: dict[str, Any], file: str, line: int | None
) -> None:
    """A finding on a file or line the model was not shown stays, without that file or line."""
    reading = _read(tmp_path, FakeClient(_message({**HOLD, "findings": [finding]})))

    assert reading.verdict is Verdict.HOLD
    assert reading.reason is None
    assert [(found.file, found.line) for found in reading.findings] == [(file, line)]


def test_the_flaw_of_an_answer_holds_nothing_of_it(tmp_path: Path) -> None:
    """The error names the check that failed in our own words, so a public log leaks no detail."""
    finding = {**FINDING, "file": "secret_name.py", "explanation": "secret explanation"}
    answer = {**HOLD, "findings": [finding], "summary": "secret summary", "extra": "secret"}

    with pytest.raises(ReadingUnavailableError) as raised:
        _read(tmp_path, FakeClient(_message(answer)))

    message = str(raised.value)
    assert message.endswith("it is not in the answer format")
    assert not any(
        secret in message for secret in ("secret_name", "secret explanation", "secret summary")
    )


@pytest.mark.parametrize("line", [None, 12, 15, 18])
def test_a_finding_may_cite_a_line_that_was_shown(tmp_path: Path, line: int | None) -> None:
    """A line inside a hunk the diff shows, or no line at all, is evidence."""
    reading = _read_long_file(tmp_path, line)

    assert reading.reason is None
    assert [finding.line for finding in reading.findings] == [line]


@pytest.mark.parametrize("line", [11, 19, 5])
def test_a_finding_on_a_line_outside_the_hunks_loses_its_line(tmp_path: Path, line: int) -> None:
    """A line of a changed file outside the hunks the diff shows is no evidence."""
    reading = _read_long_file(tmp_path, line)

    assert [(finding.file, finding.line) for finding in reading.findings] == [("long.py", None)]


def test_a_finding_on_a_whole_file_not_shown(tmp_path: Path) -> None:
    """A finding without a line may name a file that is listed but not shown."""
    finding = {**FINDING, "file": "logo.png", "line": None}

    reading = _read(tmp_path, FakeClient(_message({**HOLD, "findings": [finding]})))

    assert reading.verdict is Verdict.HOLD
    assert [(found.file, found.line) for found in reading.findings] == [("logo.png", None)]


@pytest.mark.parametrize(
    "error",
    [
        anthropic.APIConnectionError(
            request=httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
        ),
        anthropic.APITimeoutError(
            request=httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
        ),
        _status_error(anthropic.RequestTimeoutError, 408),
        _status_error(anthropic.ConflictError, 409),
        _status_error(anthropic.RateLimitError, 429),
        _status_error(anthropic.InternalServerError, 500),
        _status_error(anthropic.OverloadedError, 529),
        _status_error(anthropic.APIStatusError, 200),
        httpx2.ReadError("connection lost"),
    ],
    ids=["connection", "timeout", "408", "409", "429", "500", "529", "in-the-stream", "read"],
)
def test_a_failure_of_the_api_leaves_the_release_pending(tmp_path: Path, error: Exception) -> None:
    """A failure that says nothing about the release gives no verdict: it is read again later."""
    with pytest.raises(ReadingUnavailableError):
        _read(tmp_path, FakeClient(error))
    with pytest.raises(ReadingUnavailableError):
        _read(tmp_path / "count", FakeClient(_message(PASS), tokens=error))


@pytest.mark.parametrize(
    "error",
    [
        anthropic.WorkloadIdentityError("Token exchange failed (HTTP 401)", status_code=401),
        anthropic.CredentialsError("no identity token of the workflow job"),
        _status_error(anthropic.AuthenticationError, 401),
        _status_error(anthropic.PaymentRequiredError, 402),
        _status_error(anthropic.PermissionDeniedError, 403),
        _status_error(anthropic.NotFoundError, 404),
        _status_error(anthropic.UnprocessableEntityError, 422),
    ],
    ids=["exchange", "identity-token", "401", "402", "403", "404", "422"],
)
def test_a_fault_of_the_credentials_or_the_request_gives_no_verdict(
    tmp_path: Path, error: Exception
) -> None:
    """A failed exchange, or a request the API refuses for its own sake, is a fault, no pending."""
    with pytest.raises(ReadingConfigurationError):
        _read(tmp_path, FakeClient(error))
    with pytest.raises(ReadingConfigurationError):
        _read(tmp_path / "count", FakeClient(_message(PASS), tokens=error))


def test_a_request_the_api_refuses_as_not_valid_is_read_again(tmp_path: Path) -> None:
    """A 400 may be the release's own, so only that release waits for another night."""
    error = _status_error(anthropic.BadRequestError, 400)

    with pytest.raises(ReadingRejectedError, match="the API refused the request with 400"):
        _read(tmp_path, FakeClient(error))
    with pytest.raises(ReadingRejectedError, match="the API refused the request with 400"):
        _read(tmp_path / "count", FakeClient(_message(PASS), tokens=error))


def test_a_request_of_too_many_tokens_is_held_without_asking(tmp_path: Path) -> None:
    """A request the API counts above MAX_INPUT_TOKENS is a hold, and the model is not asked."""
    client = FakeClient(_message(PASS), tokens=MAX_INPUT_TOKENS + 1)

    assert _read(tmp_path, client) == Reading(Verdict.HOLD, reason=TOO_LARGE)
    assert client.requests == []
    assert _read(
        tmp_path / "fits", FakeClient(_message(PASS), tokens=MAX_INPUT_TOKENS)
    ).verdict is (Verdict.PASS)


def test_every_call_requests_a_new_identity_token() -> None:
    """An identity token of the job can be exchanged once, so each call requests a new one."""
    service = FakeTokenService()
    fetch = identity_token_provider(GitHub("request-token", service), REQUEST_URL)

    assert [fetch(), fetch()] == ["jwt-1", "jwt-2"]

    assert AUDIENCE == "https://api.anthropic.com"
    for request in service.requests:
        assert request.full_url == f"{REQUEST_URL}&audience=https%3A%2F%2Fapi.anthropic.com"
        assert request.get_method() == "GET"
        assert request.get_header("Authorization") == "Bearer request-token"
        assert request.get_header("Accept") == "application/json"


@pytest.mark.parametrize(
    ("status", "answer"),
    [(500, {"message": "down"}), (200, {"count": 1}), (200, {"value": ""}), (200, ["jwt"])],
    ids=["error", "no-value", "empty", "no-object"],
)
def test_no_identity_token_is_a_fault_of_the_credentials(status: int, answer: Any) -> None:
    """Without an identity token the client gets an error of the credentials, no lost connection."""
    fetch = identity_token_provider(
        GitHub("request-token", FakeTokenService(status, answer)), REQUEST_URL
    )

    with pytest.raises(anthropic.CredentialsError, match="no identity token of the workflow job"):
        fetch()


def test_the_client_exchanges_a_new_identity_token_for_each_token(tmp_path: Path) -> None:
    """Each token of the API costs one new identity token, and no key of the API is sent."""
    api, service = FakeAPI(lifetime=10), FakeTokenService()
    client = api.client(service)
    (tmp_path / "one").mkdir()
    (tmp_path / "two").mkdir()

    readings = [_read(tmp_path / "one", client), _read(tmp_path / "two", client)]

    assert [reading.verdict for reading in readings] == [Verdict.PASS, Verdict.PASS]
    # a token of ten seconds is exchanged again before the next request: the count of the tokens
    # of a reading, then its message, which is streamed
    assert [exchange["assertion"] for exchange in api.exchanges] == [
        "jwt-1",
        "jwt-2",
        "jwt-3",
        "jwt-4",
    ]
    assert [request.url.path for request in api.requests] == [
        "/v1/messages/count_tokens",
        "/v1/messages",
    ] * 2
    assert json.loads(api.requests[1].content)["stream"] is True
    assert {key: api.exchanges[0][key] for key in api.exchanges[0] if key != "assertion"} == {
        "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
        "federation_rule_id": "fdrl_rule",
        "organization_id": "organization",
        "service_account_id": "svac_account",
        "workspace_id": "wrkspc_workspace",
    }
    assert [request.headers["authorization"] for request in api.requests] == [
        "Bearer token-1",
        "Bearer token-2",
        "Bearer token-3",
        "Bearer token-4",
    ]
    assert not any("x-api-key" in request.headers for request in api.requests)


def test_a_failed_exchange_stops_the_reading(tmp_path: Path) -> None:
    """The client tries a refused exchange once more, with a new identity token, then fails."""
    api, service = FakeAPI(exchange=401), FakeTokenService()

    with pytest.raises(ReadingConfigurationError, match="could not be exchanged"):
        _read(tmp_path, api.client(service))

    assert [exchange["assertion"] for exchange in api.exchanges] == ["jwt-1", "jwt-2"]
    assert api.requests == []


@pytest.mark.parametrize("status", [401, 403])
def test_a_token_the_api_refuses_stops_the_reading(tmp_path: Path, status: int) -> None:
    """A refused token is exchanged again once; a token the API still refuses is a fault."""
    api, service = FakeAPI(messages=status), FakeTokenService()

    with pytest.raises(ReadingConfigurationError, match=f"Error code: {status}"):
        _read(tmp_path, api.client(service))

    assert len(api.exchanges) == (2 if status == 401 else 1)
    assert len(service.requests) == len(api.exchanges)


def test_a_request_too_large_for_the_api_is_a_hold(tmp_path: Path) -> None:
    """The API refusing the size of the request is a hold, as a change above MAX_SHOWN is."""
    error = _status_error(anthropic.ContentTooLargeError, 413)

    assert _read(tmp_path, FakeClient(error)) == Reading(Verdict.HOLD, reason=TOO_LARGE)
    held = _read(tmp_path / "count", FakeClient(_message(PASS), tokens=error))
    assert held == Reading(Verdict.HOLD, reason=TOO_LARGE)


def test_a_change_larger_than_the_reading_takes_is_held_without_a_request(tmp_path: Path) -> None:
    """A change with more text than MAX_REQUEST is held without asking the model."""
    line = b"x = 1  # " + b"y" * 90 + b"\n"
    big = {**RELEASE, "big.py": line * (MAX_REQUEST // len(line) + 1)}

    reading = _read(tmp_path, FakeClient(), release=big)

    assert reading == Reading(Verdict.HOLD, reason=TOO_LARGE)


def test_text_beyond_the_cap_is_not_built(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Once the shown text would pass MAX_REQUEST, no more text is numbered or diffed."""
    lines = reading_module._lines  # noqa: SLF001 - counts the texts that are built
    seen: list[str] = []

    def counted(text: str) -> list[str]:
        seen.append(text)
        return lines(text)

    monkeypatch.setattr(reading_module, "_lines", counted)
    # a.py is shown in two thirds of the cap, as numbered lines of 13 bytes; b.py alone is more
    # than the third that is left
    release = {
        "a.py": b"x = 1\n" * (MAX_REQUEST * 2 // 3 // 13),
        "b.py": b"y = 2\n" * (MAX_REQUEST // 2 // 6),
        "c.py": b"z = 3\n",
    }

    changes = compare_folders(_folder(tmp_path, "release", release), None)

    assert [(change.path, change.too_large) for change in changes] == [
        ("a.py", False),
        ("b.py", True),
        ("c.py", True),
    ]
    assert [change.text is None for change in changes] == [False, True, True]
    assert len(seen) == 1


def test_the_whole_request_counts_against_the_cap(tmp_path: Path) -> None:
    """The instructions and the header count: text under the cap is held when the request is not."""
    code = b"x = 1\n" * ((MAX_REQUEST - len(system_prompt()) // 2) // 13)
    release = {**RELEASE, "code.py": code}
    (tmp_path / "check").mkdir()

    changes = compare_folders(_folder(tmp_path / "check", "release", release), None)
    reading = _read(tmp_path, FakeClient(), release=release, base=None)

    assert sum(len((change.text or "").encode()) for change in changes) < MAX_REQUEST
    assert reading == Reading(Verdict.HOLD, reason=TOO_LARGE)


def test_the_file_table_counts_against_the_cap(tmp_path: Path) -> None:
    """A release of many files that are only listed is held when its file table is too large."""
    # each row of the file table holds at least its path of 612 bytes
    folder = f"{'d' * 200}/" * 3
    release = {
        f"{folder}{number:05d}.png": _png(number.to_bytes(4))
        for number in range(MAX_REQUEST // 612 + 1)
    }

    reading = _read(tmp_path, FakeClient(), release={**RELEASE, **release}, base=None)

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


@pytest.mark.parametrize(
    "declaration",
    [
        b"# -*- coding: utf-7 -*-\n",
        b"# coding=latin-1\n",
        b"#!/usr/bin/env python\n# vim: set fileencoding=cp1252 :\n",
        b"# coding: no-such-codec\n",
    ],
    ids=["utf-7", "latin-1", "second-line", "unknown"],
)
def test_a_file_that_declares_another_encoding_is_held(tmp_path: Path, declaration: bytes) -> None:
    """Python decodes source by its declared encoding, in which a comment can hide code."""
    code = declaration + b"# +AAo-print('runs in utf-7')\n"

    reading = _read(tmp_path, FakeClient(), release={**RELEASE, "hidden.py": code})

    assert reading == Reading(
        Verdict.HOLD,
        reason=UNREADABLE,
        findings=[ReadingFinding(Rule.UNCLEAR, "hidden.py", None, UNREADABLE)],
    )


@pytest.mark.parametrize(
    "declaration",
    [b"# -*- coding: utf-8 -*-\n", b"# coding: UTF8\n", b"\xef\xbb\xbf# a byte order mark\n"],
    ids=["utf-8", "utf8", "bom"],
)
def test_a_file_in_utf8_is_shown(tmp_path: Path, declaration: bytes) -> None:
    """A declaration of UTF-8, or its byte order mark, decodes the file as it is shown."""
    changes = compare_folders(_folder(tmp_path, "release", {"code.py": declaration}), None)

    assert (changes[0].not_shown, changes[0].unreadable) == (None, False)


def test_a_removed_file_that_declares_another_encoding_is_no_hold(tmp_path: Path) -> None:
    """A file that leaves the provider runs nowhere."""
    base = {**BASE, "old.py": b"# coding: utf-7\nOLD = 1\n"}

    changes = compare_folders(
        _folder(tmp_path, "release", RELEASE), _folder(tmp_path, "base", base)
    )

    assert not any(change.unreadable for change in changes)


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


def test_read_archive_with_an_answer_that_is_not_valid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An answer that is not valid says the release is pending, and names the failed check."""
    client = FakeClient(_message("not JSON"))
    monkeypatch.setattr(anthropic, "Anthropic", lambda: client)
    (tmp_path / "release.tar.gz").write_bytes(
        build_archive({f"{FOLDER}/{path}": data for path, data in RELEASE.items()})
    )

    assert main(["read-archive", str(tmp_path / "release.tar.gz"), "--folder", FOLDER]) == 1

    output = capsys.readouterr()
    assert output.out == ""
    assert (
        output.err
        == "pending: the answer of the reading is not valid: it is not in the answer format\n"
    )


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
