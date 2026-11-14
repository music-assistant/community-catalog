"""Reading and writing the JSON files of the registry."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from mashumaro.exceptions import ExtraKeysError, InvalidFieldValue, MissingField

if TYPE_CHECKING:
    from pathlib import Path

    from registry.models import StrictModel


def dumps(data: Any) -> bytes:
    """
    Return data as the registry writes JSON: sorted keys, two spaces, a newline at the end.

    The same data always gives the same bytes.

    :param data: The data to write.
    """
    return (json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode()


def read_object(path: Path) -> dict[str, Any]:
    """
    Return the JSON object in a file.

    :param path: The file to read.
    :raises ValueError: The file is not UTF-8, not valid JSON, holds a key twice, or holds
        something else than an object.
    :raises OSError: The file cannot be read.
    """
    try:
        data = json.loads(path.read_bytes().decode(), object_pairs_hook=_refuse_duplicates)
    except UnicodeDecodeError as err:
        msg = f"not UTF-8: {err}"
        raise ValueError(msg) from err
    except json.JSONDecodeError as err:
        msg = f"not valid JSON: {err}"
        raise ValueError(msg) from err
    if not isinstance(data, dict):
        raise ValueError("not a JSON object")
    return data


def load_model[T: StrictModel](path: Path, model: type[T]) -> T:
    """
    Return the model in a JSON file.

    :param path: The file to read.
    :param model: The model the file holds.
    :raises ValueError: The file does not hold exactly this model; the message says where.
    :raises OSError: The file cannot be read.
    """
    data = read_object(path)
    try:
        return model.from_dict(data)
    except (ValueError, TypeError, LookupError) as err:
        raise ValueError(_describe(err)) from err


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
