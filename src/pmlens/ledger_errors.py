"""Content-free errors for ledger records that cannot be loaded (PMSERV-266).

A pydantic ``ValidationError`` quotes the offending value (``input_value``) and
a YAML error quotes the offending line; either can be a secret. FastMCP sends
an exception's text to the client and logs its traceback, chained exceptions
included, and Lens read-only tools reach the same loaders. Loaders therefore
raise :class:`LedgerReadError`, whose text is built from the file name, a
record's position, the model's field names and pydantic's error type codes,
plus one piece of file content: the record's id, and only when it has the
ASCII shape pmlens gives ids (``_RECORD_ID_RE``). It is raised ``from None`` so
the original exception is never printed.
"""

from __future__ import annotations

import re
import typing
from collections.abc import Mapping, Sequence
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from .models import PmServerError

try:
    from pydantic_core.core_schema import ErrorType as _ErrorType

    _ERROR_TYPES: frozenset[str] = frozenset(typing.get_args(_ErrorType))
except ImportError:  # pragma: no cover - pydantic-core always ships it today
    _ERROR_TYPES = frozenset()

# An error type pmlens does not know is shown as this, never as given.
_UNKNOWN_ERROR_TYPE = "invalid"
# How many "field (type)" items a message lists before "and N more".
_FIELDS_SHOWN = 5
# A record id is file content, shown only when it has the common ASCII id shape
# (PMSERV-012, ADR-007, KR-001, WF-045, RISK-001). Ids made from project names
# with dots, spaces, a leading digit or non-ASCII letters are left out, as is
# anything longer; [0-9], not \d, which also matches other scripts' digits.
_RECORD_ID_RE = re.compile(r"[A-Z][A-Z0-9]{0,15}-[0-9]{1,6}")
_SEQUENCE_ORIGINS = (list, tuple, set, frozenset, Sequence)

M = TypeVar("M", bound=BaseModel)


class LedgerReadError(PmServerError):
    """A ledger file or record that cannot be read, described without its content.

    Attributes:
        summary: The problem without the file name, for messages that name the
            file themselves (``lineage.error_summary`` returns it).
    """

    def __init__(self, message: str, summary: str) -> None:
        super().__init__(message)
        self.summary = summary


class LedgerWriteRefused(PmServerError):  # noqa: N818 - matches LineageWriteRefused
    """A write into ``.pm/`` refused because a symlink or special file is in the way.

    A repository can ship ``.pm/daily``, ``.pm/.locks`` or a ledger file as a
    symlink, and writing through it would create or replace files anywhere the
    user can write (PMSERV-277). The message names the ``.pm`` path and the
    fixed reason only.
    """


def _field_shape(annotation: Any) -> tuple[type[BaseModel] | None, bool]:
    """The model a field holds and whether it holds a sequence of values.

    Looks through ``X | None``, ``list[X]`` and similar wrappers.
    """
    model: type[BaseModel] | None = None
    sequence = False
    stack = [annotation]
    while stack:
        current = stack.pop()
        if isinstance(current, type) and issubclass(current, BaseModel):
            model = model or current
            continue
        origin = typing.get_origin(current)
        if isinstance(origin, type) and issubclass(origin, _SEQUENCE_ORIGINS):
            sequence = True
        stack.extend(typing.get_args(current))
    return model, sequence


def _field_path(model: type[BaseModel] | None, loc: Sequence[object]) -> str:
    """Render an error location using only names and positions pmlens defines.

    A string is shown only when it is a field of the model reached so far, and
    a number only where that field holds a sequence; the path stops at anything
    else (a union member tag, or a key taken from the data).
    """
    parts: list[str] = []
    current = model
    in_sequence = False
    for element in loc:
        if isinstance(element, int) and not isinstance(element, bool) and in_sequence:
            parts.append(f"[{element}]")
            in_sequence = False
            continue
        if isinstance(element, str) and current is not None and element in current.model_fields:
            parts.append(f".{element}" if parts else element)
            current, in_sequence = _field_shape(current.model_fields[element].annotation)
            continue
        break
    return "".join(parts)


def _known_type(kind: object) -> str:
    """pydantic's error type code if pmlens knows it, else a fixed word."""
    return kind if isinstance(kind, str) and kind in _ERROR_TYPES else _UNKNOWN_ERROR_TYPE


def fields_summary(model: type[BaseModel] | None, exc: ValidationError) -> str:
    """List the invalid fields of ``exc`` as ``"title (string_type); ..."``.

    Reads only each error's location and type code, never its input, message
    or context, which can quote the value.
    """
    items: dict[str, None] = {}
    for error in exc.errors():
        kind = _known_type(error.get("type"))
        path = _field_path(model, error.get("loc", ()))
        items.setdefault(f"{path} ({kind})" if path else f"({kind})", None)
    listed = list(items)
    text = "; ".join(listed[:_FIELDS_SHOWN])
    if len(listed) > _FIELDS_SHOWN:
        text += f"; and {len(listed) - _FIELDS_SHOWN} more"
    return text


def _record_label(raw: object, index: int | None) -> str:
    if index is None:
        return "the document"
    record_id = raw.get("id") if isinstance(raw, Mapping) else None
    if isinstance(record_id, str) and _RECORD_ID_RE.fullmatch(record_id):
        return f"record {index} ({record_id})"
    return f"record {index}"


def build_record(
    model: type[M],
    raw: object,
    file: str,
    index: int | None = None,
    label: str | None = None,
) -> M:
    """Build one ledger record, or raise a LedgerReadError that quotes nothing.

    Args:
        model: The pydantic model to build.
        raw: The record as loaded from the file.
        file: The file name to put in the message (e.g. ``"tasks.yaml"``).
        index: The record's 1-based position in its list, or None when the
            whole document is one record (project.yaml, registry.yaml).
        label: How to name the record instead (e.g. ``"memory 12"``); it must
            not contain file content.

    Returns:
        The built record.

    Raises:
        LedgerReadError: The record is not a mapping or fails validation.
    """
    label = label or _record_label(raw, index)
    if not isinstance(raw, Mapping):
        summary = f"{label} is not a mapping ({type(raw).__name__})"
        raise LedgerReadError(f"{file}: {summary}", summary)
    try:
        return model(**raw)
    except ValidationError as exc:
        summary = f"ValidationError in {label}: {fields_summary(model, exc)}"
    except TypeError:
        # Model(**raw) with a key that is not a string; the message names no key.
        summary = f"{label} has a key that is not text"
    raise LedgerReadError(f"{file}: {summary}", summary) from None


def build_records(model: type[M], items: object, file: str, key: str) -> list[M]:
    """Build every record of a ledger list (``tasks:``, ``decisions:`` ...).

    An empty value (``tasks:``, ``tasks: {}``, ``tasks: ''``) holds no records
    and reads as an empty list, as before; any other value that is not a list
    is refused, so a writer never treats it as empty and overwrites it.

    Raises:
        LedgerReadError: ``items`` is a non-empty value that is not a list, or
            a record cannot be built.
    """
    if items is None or (isinstance(items, (dict, str)) and not items):
        return []
    if not isinstance(items, list):
        summary = f"{key!r} is not a list ({type(items).__name__})"
        raise LedgerReadError(f"{file}: {summary}", summary)
    return [build_record(model, raw, file, index) for index, raw in enumerate(items, 1)]


def safe_validation_message(exc: ValidationError) -> str:
    """Describe a ValidationError of unknown origin by error types only.

    For the tool-boundary safety net, which cannot tell which model or file the
    error came from, nor whether the value came from a stored file or from the
    caller.
    """
    kinds = dict.fromkeys(_known_type(error.get("type")) for error in exc.errors())
    return (
        f"Validation failed ({exc.error_count()} error(s): {', '.join(kinds)}); "
        "values are not shown because they may come from a stored file"
    )
