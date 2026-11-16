"""Reading and writing the JSON files of the registry."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import orjson
from mashumaro.exceptions import ExtraKeysError, InvalidFieldValue, MissingField

if TYPE_CHECKING:
    from pathlib import Path

    from registry.models import StrictModel


def dumps(data: Any) -> bytes:
    """
    Return data as the registry writes JSON: sorted keys, two spaces, a newline at the end.

    The same data always gives the same bytes, and the server reads them.

    :param data: The data to write.
    :raises ValueError: The data holds what plain JSON cannot: NaN, an infinite number or a
        lone surrogate.
    """
    try:
        content = json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False)
        encoded = (content + "\n").encode()
        # the proof that the server, which parses with orjson, reads what is written
        orjson.loads(encoded)
    except ValueError as err:
        msg = f"not plain JSON: {err}"
        raise ValueError(msg) from err
    return encoded


def loads(content: bytes) -> Any:
    """
    Return the content of a JSON document, which must be plain JSON the server reads as well.

    :param content: The document.
    :raises ValueError: The document is not UTF-8, not valid JSON, or holds a key twice. Valid
        JSON excludes what the server refuses: NaN, Infinity, a number beyond the range of a
        double and a lone surrogate.
    """
    try:
        text = content.decode()
        # orjson, the parser of the server, refuses what json accepts beyond plain JSON
        orjson.loads(content)
        return json.loads(text, object_pairs_hook=_refuse_duplicates)
    except UnicodeDecodeError as err:
        msg = f"not UTF-8: {err}"
        raise ValueError(msg) from err
    # also orjson.JSONDecodeError, a subclass of it
    except json.JSONDecodeError as err:
        msg = f"not valid JSON: {err}"
        raise ValueError(msg) from err


def read_object(path: Path) -> dict[str, Any]:
    """
    Return the JSON object in a file.

    :param path: The file to read.
    :raises ValueError: The file is not plain JSON (see loads), or holds something else than
        an object.
    :raises OSError: The file cannot be read.
    """
    return _object(path.read_bytes())


def load_model[T: StrictModel](path: Path, model: type[T]) -> T:
    """
    Return the model in a JSON file.

    :param path: The file to read.
    :param model: The model the file holds.
    :raises ValueError: The file does not hold exactly this model; the message says where.
    :raises OSError: The file cannot be read.
    """
    return parse_model(path.read_bytes(), model)


def parse_model[T: StrictModel](content: bytes, model: type[T]) -> T:
    """
    Return the model in the content of a JSON file.

    :param content: The content of the file.
    :param model: The model the file holds.
    :raises ValueError: The content is not exactly this model; the message says where.
    """
    data = _object(content)
    try:
        return model.from_dict(data)
    except (ValueError, TypeError, LookupError) as err:
        raise ValueError(_describe(err)) from err


def _object(content: bytes) -> dict[str, Any]:
    """
    Return the JSON object in the content of a file.

    :param content: The content.
    :raises ValueError: It is not plain JSON (see loads), or holds something else than an object.
    """
    data = loads(content)
    if not isinstance(data, dict):
        raise ValueError("not a JSON object")
    return data


def _describe(err: BaseException) -> str:
    """Return what is wrong in a document, with the path of the field, from a mashumaro error."""
    path: list[str] = []
    leaf: BaseException = err
    current: BaseException | None = err
    while current is not None:
        leaf = current
        if isinstance(current, InvalidFieldValue | MissingField):
            path.append(current.field_name)
        current = current.__cause__ or current.__context__
    if isinstance(leaf, MissingField):
        text = "is missing"
    elif isinstance(leaf, InvalidFieldValue):
        text = f"has the invalid value {leaf.field_value!r}"
    elif isinstance(leaf, ExtraKeysError):
        text = f"unknown key {', '.join(sorted(leaf.extra_keys))}"
    else:
        text = str(leaf)
    return f"{'.'.join(path)}: {text}" if path else text


def _refuse_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Return the pairs of a JSON object as a dict, refusing a key that appears twice."""
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            msg = f"the key {key!r} appears twice"
            raise ValueError(msg)
        result[key] = value
    return result
