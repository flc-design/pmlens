"""Decision Lineage: an ADR's lifecycle, declared provenance and events (ADR-056 S1).

One YAML file per ADR, ``.pm/decision_lineage/ADR-NNN.yaml``, holds the ADR's
lifecycle (the source of truth), what the caller declared about where the
decision came from, links to other ADRs and an append-only list of events.
decisions.yaml keeps the four-value ``status`` that already-shipped readers
understand; it is a projection of the lifecycle (:func:`project_status`).

This module never writes and never locks. It reads one lineage file at a time
through a bounded reader (:func:`read_lineage_raw`), lists the lineage
directory only to find links pointing at an ADR (:func:`scan_linked_from`),
and otherwise only builds values: views for readers (:func:`lineage_view`) and
new documents for the writers in ``storage`` (:func:`new_lineage_doc`,
:func:`apply_change`).
``storage`` imports this module and owns every lock and write; this module does
not import ``storage`` (tests/test_lineage.py pins both with an AST check).

Reads are lenient (ADR-056): a lineage file that is missing, unreadable,
malformed or written for another ADR is reported through notes, and the ADR is
shown as *derived* from its status. No note, warning or error built here quotes
an exception's text: ``str(exc)`` carries the offending YAML line or pydantic's
``input_value`` and can leak a secret (:func:`error_summary`).

"design §N" below refers to docs/issues/DESIGN_decision-lineage-s1.md.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import os
import re
import stat
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .models import (
    Decision,
    DecisionKind,
    DecisionLifecycle,
    DecisionOrigin,
    DecisionStatus,
    EvaluationKind,
    PmServerError,
    RecordedTiming,
)
from .redaction import redact_secrets

# ─── Constants ───────────────────────────────────────

# ``re.fullmatch`` only: ``^ADR-\d{1,6}$`` would also accept "ADR-001\n" and
# non-ASCII digits such as "ADR-١٢٣", which ``int()`` and file names mishandle.
DECISION_ID_RE = re.compile(r"ADR-[0-9]{1,6}")
MAX_DECISION_NUMBER = 999_999
LINEAGE_DIR = "decision_lineage"
LINEAGE_SCHEMA = 1
# One cap for readers and writers: a writer never produces a file a reader
# refuses, and parsing one file under the decisions lock stays well below the
# default lock timeout (a dense 200 KB YAML took ~0.36 s to safe_load).
MAX_LINEAGE_BYTES = 256 * 1024
# Notes and evaluations are appended without a count limit, so they stop this
# far below MAX_LINEAGE_BYTES: the rest is kept for lifecycle, link and
# declared changes, which must still fit when the notes have filled the file
# (a lifecycle event with a 4,000-character reason in a 3-byte script is
# about 12.5 KB). Readers still accept anything up to MAX_LINEAGE_BYTES.
LINEAGE_RESERVE_BYTES = 16 * 1024
MAX_APPEND_BYTES = MAX_LINEAGE_BYTES - LINEAGE_RESERVE_BYTES
# How deeply lists and mappings may nest in a lineage file. PyYAML's composer,
# representer and serializer recurse per level: a file nested ~350 deep loads
# but cannot be dumped again (RecursionError). Readers and writers share this
# limit, checked without recursion (:func:`_nested_deeper_than`), so neither
# accepts a file the other cannot handle. S1 files nest 3 deep.
MAX_LINEAGE_DEPTH = 64
# The linked_from scan reads other ADRs' lineages (the ADR's own supersedes /
# superseded_by targets first, then the rest newest first) and stops at
# whichever comes first: this many files, or this many bytes read. It is the
# only read of other ADRs' lineages in a get, so the byte budget bounds the
# parse time when many files sit near MAX_LINEAGE_BYTES (each takes tens of
# milliseconds to safe_load): it holds 500 files of 16 KiB, far more than S1
# writes, but only about 32 files at the size cap.
LINKED_FROM_SCAN_LIMIT = 500
LINKED_FROM_SCAN_BYTES = 8 * 1024 * 1024
MAX_LINKS_PER_TYPE = 50
MAX_TEXT_CHARS = 4_000
MAX_LABEL_CHARS = 100
RECENT_EVENTS = 20
MAX_UNKNOWN_KEYS = 50
MAX_UNKNOWN_FIELDS = 10

UNKNOWN = "unknown"
LINK_TYPES: tuple[str, ...] = ("supersedes", "superseded_by", "amends")
DECLARED_FIELDS: tuple[str, ...] = ("origin", "recorded_timing", "decision_kind")
NOT_RECORDED_ORDER: tuple[str, ...] = ("recorded_at", *DECLARED_FIELDS)
LIFECYCLES: tuple[str, ...] = tuple(v.value for v in DecisionLifecycle)
ASSISTANT_RECORDED = "assistant_recorded_unverified"
BACKFILL_ORIGINS: frozenset[str] = frozenset({DecisionOrigin.AI_AUTO.value})
BACKFILL_TIMINGS: frozenset[str] = frozenset(
    {
        RecordedTiming.BEFORE_IMPL.value,
        RecordedTiming.DURING_IMPL.value,
        RecordedTiming.POST_HOC.value,
    }
)

# Note / warning / error codes (design §4.4).
DECISION_ID_INVALID = "decision_id_invalid"
DECISION_ID_DUPLICATE = "decision_id_duplicate"
LINEAGE_UNREADABLE = "decision_lineage_unreadable"
LINEAGE_SCHEMA_UNSUPPORTED = "decision_lineage_schema_unsupported"
LINEAGE_LIFECYCLE_UNKNOWN = "decision_lineage_lifecycle_unknown"
LINEAGE_ANCHOR_MISMATCH = "decision_lineage_anchor_mismatch"
LINEAGE_ANCHOR_MISSING = "decision_lineage_anchor_missing"
LINEAGE_NO_SUCCESSOR = "decision_lineage_superseded_without_successor"
LINEAGE_ITEMS_SKIPPED = "decision_lineage_items_skipped"
LINEAGE_TOO_LARGE = "decision_lineage_too_large"

# Fields each event kind may show, in this order (design §2.4). Multi-event
# calls also append their events in this order. ``at`` / ``kind`` / ``via`` are
# common to every kind.
_EVENT_FIELDS: dict[str, tuple[str, ...]] = {
    "created": ("lifecycle", "status"),
    "lineage_started": ("basis", "status", "lifecycle"),
    "lifecycle": ("from", "to", "status", "reason"),
    "link": ("op", "type", "target", "reason"),
    "declared": ("field", "value", "basis", "reason"),
    "evaluation": ("evaluation_kind", "text"),
    "note": ("text",),
    "status_reprojected": ("from_status", "to_status"),
}
EVENT_KINDS: tuple[str, ...] = tuple(_EVENT_FIELDS)
_EVENT_COMMON: tuple[str, ...] = ("at", "kind", "via")
_FREE_TEXT_FIELDS = frozenset({"text", "reason"})
_KNOWN_TOP_LEVEL = frozenset(
    {"schema", "decision_id", "anchor", "recorded_at", "declared", "lifecycle", "links", "events"}
)

# Lifecycle -> decisions.yaml status (design §3.1). reverted is handled apart
# because it depends on superseded_by.
_PROJECTION: dict[str, DecisionStatus] = {
    DecisionLifecycle.PROPOSED.value: DecisionStatus.PROPOSED,
    DecisionLifecycle.ADOPTED.value: DecisionStatus.ACCEPTED,
    DecisionLifecycle.DEPRECATED.value: DecisionStatus.DEPRECATED,
    DecisionLifecycle.SUPERSEDED.value: DecisionStatus.SUPERSEDED,
    DecisionLifecycle.REJECTED.value: DecisionStatus.DEPRECATED,
}
# decisions.yaml status -> lifecycle, for an ADR without a lineage (§3.2).
_DERIVED: dict[str, str] = {
    DecisionStatus.PROPOSED.value: DecisionLifecycle.PROPOSED.value,
    DecisionStatus.ACCEPTED.value: DecisionLifecycle.ADOPTED.value,
    DecisionStatus.DEPRECATED.value: DecisionLifecycle.DEPRECATED.value,
    DecisionStatus.SUPERSEDED.value: DecisionLifecycle.SUPERSEDED.value,
}
# Transitions a tool may make (design §4.3). Staying on the same value is an
# allowed no-op and is not listed here.
_TRANSITIONS: dict[str, frozenset[str]] = {
    "proposed": frozenset({"adopted", "superseded", "rejected"}),
    "adopted": frozenset({"proposed", "deprecated", "superseded", "reverted"}),
    "deprecated": frozenset({"adopted", "superseded"}),
    "superseded": frozenset({"adopted", "deprecated"}),
    "rejected": frozenset({"proposed"}),
    "reverted": frozenset({"proposed"}),
}


class LineageWriteRefused(PmServerError):  # noqa: N818 - the name the S1 spec uses
    """A lineage file is in a state a writer must not overwrite.

    Raised before anything is written; ``code`` is one of the
    ``decision_lineage_*`` codes of design §2.6.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


# ─── Small helpers ───────────────────────────────────


def _utc_now() -> str:
    """The one clock for lineage timestamps (``YYYY-MM-DDTHH:MM:SSZ``, UTC).

    Tests monkeypatch this function; every writer takes its time from here.
    """
    return _dt.datetime.now(_dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def is_decision_id(value: object) -> bool:
    """True when ``value`` is a string of the form ``ADR-`` + 1-6 ASCII digits."""
    return isinstance(value, str) and DECISION_ID_RE.fullmatch(value) is not None


def lineage_header_name(decision_id: str) -> str:
    """The name written in the file's comment header."""
    return f"{LINEAGE_DIR}/{decision_id}.yaml"


def error_summary(exc: BaseException) -> str:
    """Describe an exception by type (and YAML position) without its text.

    ``str(exc)`` quotes the offending YAML line or pydantic's ``input_value``,
    which can be a secret, so only the type name is used. For a YAML error,
    possibly wrapped (``storage._load_yaml`` re-raises it as PmServerError with
    the YAMLError as ``__cause__``), the line and column of ``problem_mark`` are
    added; they are positions, not content.

    Args:
        exc: The exception to describe.

    Returns:
        e.g. ``"ScannerError at line 3, column 9"`` or ``"ValidationError"``.
    """
    yaml_exc: BaseException | None = None
    if isinstance(exc, yaml.YAMLError):
        yaml_exc = exc
    elif isinstance(exc.__cause__, yaml.YAMLError):
        yaml_exc = exc.__cause__
    if yaml_exc is None:
        return type(exc).__name__
    mark = getattr(yaml_exc, "problem_mark", None)
    line = getattr(mark, "line", None)
    column = getattr(mark, "column", None)
    if isinstance(line, int) and isinstance(column, int):
        return f"{type(yaml_exc).__name__} at line {line + 1}, column {column + 1}"
    return type(yaml_exc).__name__


def scrub_text(text: str) -> tuple[str, int]:
    """Remove credential-shaped strings (``redaction.redact_secrets``).

    Takes time linear in ``text`` (every catalog pattern does), so callers
    scan a value whole and cut it for display afterwards.

    Returns:
        The scrubbed text and how many matches were replaced. The count carries
        no cleartext, so it may be reported.
    """
    scrubbed, counts = redact_secrets(text)
    return scrubbed, sum(counts.values())


class ScrubCache:
    """:func:`scrub_text` remembered per string, for one view or one response.

    A YAML alias makes one string appear many times (``&e`` once, ``*e`` on
    every event, or one status on every ADR of decisions.yaml), so without
    this a small file could be scanned once per place it is shown: 500 ADRs
    sharing one 256 KiB status took 10 s to list. Remembering by value keeps
    the work proportional to the distinct text; each appearance still adds its
    own count. Create one per response (or per view) and pass it to every
    scrub of that response; it holds the scrubbed copies until dropped.
    """

    def __init__(self) -> None:
        self._done: dict[str, tuple[str, int]] = {}

    def scrub(self, text: str) -> tuple[str, int]:
        """:func:`scrub_text`, scanning each distinct string once.

        Returns:
            The scrubbed text and how many matches were replaced.
        """
        found = self._done.get(text)
        if found is None:
            found = self._done[text] = scrub_text(text)
        return found


def _clip(text: str, limit: int) -> tuple[str, bool]:
    """Cut ``text`` to ``limit`` characters; also say whether it was cut."""
    if len(text) <= limit:
        return text, False
    return text[:limit], True


def _scrub_clip(text: str, limit: int, cache: ScrubCache | None = None) -> tuple[str, bool, int]:
    """Redact the whole of ``text``, then cut it to ``limit``.

    Redaction comes first, over the full value, so a secret straddling the
    limit is replaced as a whole rather than left half-visible, and a keyword
    stays next to its value (``password = <value>``, ``Bearer <token>``).

    Returns:
        (text, truncated, redactions).
    """
    scrubbed, count = cache.scrub(text) if cache is not None else scrub_text(text)
    out, cut = _clip(scrubbed, limit)
    return out, cut, count


def scrub_cut(text: str, limit: int, cache: ScrubCache | None = None) -> tuple[str, bool, int]:
    """Redact the whole of ``text``, then cut it to ``limit`` characters.

    For a reader that shows a shortened value and says that it was cut (a
    list row's title). Pass the response's :class:`ScrubCache` as ``cache``.

    Returns:
        (text, cut, redactions); the count carries no cleartext.
    """
    return _scrub_clip(text, limit, cache)


def scrub_label_counted(
    value: object, limit: int = MAX_LABEL_CHARS, cache: ScrubCache | None = None
) -> tuple[str, int]:
    """:func:`scrub_label`, also returning how many redactions were made.

    For a reader that reports the redactions of a response
    (``decision_text_secrets_redacted``): a label redacted before the exit
    pass reaches :func:`scrub_view` already clean, so its count must be
    carried separately or it is lost.

    Args:
        value: The untrusted value.
        limit: How many characters of the redacted text to keep.
        cache: One :class:`ScrubCache` for every label of a response. A
            reader that labels a value from each of many records needs it: the
            whole value is scanned before the cut, and a YAML alias can give
            every record the same long value.

    Returns:
        The label and how many redactions were made in ``value``.
    """
    text, cut, count = _scrub_clip(str(value), limit, cache)
    return (text + "…" if cut else text), count


def scrub_label(
    value: object, limit: int = MAX_LABEL_CHARS, cache: ScrubCache | None = None
) -> str:
    """A short, redacted rendering of an untrusted value for a message.

    ``str(value)`` is redacted whole, then cut to ``limit`` characters; "…"
    marks a cut. Pass ``cache`` when labelling values from many records in
    one response (:func:`scrub_label_counted`).
    """
    return scrub_label_counted(value, limit, cache)[0]


def scrub_view(value: object) -> tuple[object, int]:
    """Redact every string in a response built from plain dicts and lists.

    Meant for the exit of a read tool: one pass over the whole response so no
    field is missed. Every string is scanned whole, in time linear in its
    length (ADR text from decisions.yaml has no length cap); a string that
    appears more than once is scanned once (:class:`ScrubCache`). Dict keys
    are left alone (responses use fixed key names; untrusted key names travel
    as list values). Nothing is mutated.

    Returns:
        A redacted copy and the total number of replacements.
    """
    total = 0
    cache = ScrubCache()

    def walk(node: object) -> object:
        nonlocal total
        if isinstance(node, str):
            text, count = cache.scrub(node)
            total += count
            return text
        if isinstance(node, dict):
            return {key: walk(item) for key, item in node.items()}
        if isinstance(node, (list, tuple)):
            return [walk(item) for item in node]
        return node

    return walk(value), total


def notice(level: str, code: str, message: str, remediation: str | None = None) -> dict:
    """A warning entry in the shape of ``server._build_warning``."""
    entry: dict[str, str] = {"level": level, "code": code, "message": message}
    if remediation:
        entry["remediation"] = remediation
    return entry


def _error(code: str, message: str, **extra: object) -> dict:
    """An expected-failure dict (``{"status": "error", "code", "message"}``)."""
    return {"status": "error", "code": code, "message": message, **extra}


def _iso(value: _dt.date) -> str:
    return value.isoformat()


def _status_text(value: object) -> str:
    """A status as a plain ``str`` (safe_dump rejects StrEnum members)."""
    return value.value if isinstance(value, DecisionStatus) else str(value)


# ─── Projection and transitions ──────────────────────


def project_status(lifecycle: object, superseded_by: object = None) -> DecisionStatus:
    """Map a lifecycle onto the four decisions.yaml status values (design §3.1).

    Total: every input yields one of the four DecisionStatus values, so a
    projection can never write a status older readers reject. An unknown
    lifecycle maps to deprecated, which older screens do not show as a valid
    guideline; S1 writers never project an unknown lifecycle.

    Args:
        lifecycle: The lineage lifecycle value.
        superseded_by: The ADR's superseded_by links (only reverted uses it).

    Returns:
        The status to store in decisions.yaml.
    """
    if lifecycle == DecisionLifecycle.REVERTED:
        return DecisionStatus.SUPERSEDED if superseded_by else DecisionStatus.DEPRECATED
    if isinstance(lifecycle, str) and lifecycle in _PROJECTION:
        return _PROJECTION[lifecycle]
    return DecisionStatus.DEPRECATED


def derive_lifecycle(status: object) -> str | None:
    """Infer a lifecycle from a decisions.yaml status (design §3.2).

    deprecated cannot tell rejected or reverted apart. An unknown status has
    no lifecycle (``None``) and cannot be a transition source.
    """
    if isinstance(status, str):
        return _DERIVED.get(status)
    return None


def is_known_lifecycle(value: object) -> bool:
    """True for one of the six lifecycle values."""
    return isinstance(value, str) and value in _TRANSITIONS


def transition_allowed(source: str, target: str) -> bool:
    """True when a tool may move ``source`` to ``target`` (same value included)."""
    return source == target or target in _TRANSITIONS.get(source, frozenset())


def allowed_to(source: str) -> list[str]:
    """Targets reachable from ``source`` in one call, in vocabulary order."""
    targets = _TRANSITIONS.get(source, frozenset())
    return [value for value in LIFECYCLES if value in targets]


# ─── Anchor ──────────────────────────────────────────


def title_sha256(title: str) -> str:
    """SHA-256 of the ADR title (UTF-8). The anchor never copies the text."""
    return hashlib.sha256(str(title).encode("utf-8")).hexdigest()


def stored_title(title: str) -> str:
    """The title as decisions.yaml gives it back once saved (safe_dump, then safe_load).

    A YAML round trip is not the identity for every string: U+0085 (NEL) is
    written raw and read back as a line break, which folds to a space. The
    creator of an ADR stores this form, so the anchor it hashes is the title
    every later reader loads (design §2.2). The title is dumped where a
    decisions.yaml record holds it (a block mapping inside a block sequence),
    so the emitter picks the same style and the same line width.

    Args:
        title: The title as given.

    Returns:
        The title after the round trip (unchanged for almost every string).
    """
    text = yaml.safe_dump(
        {"decisions": [{"title": title}]},
        default_flow_style=False,
        allow_unicode=True,
        sort_keys=False,
    )
    loaded = yaml.safe_load(text)
    value = loaded["decisions"][0]["title"]
    return value if isinstance(value, str) else title


def anchor_for(decision: Decision, *, date_on_file: bool | None = None) -> dict[str, str | None]:
    """The fingerprint tying a lineage to the ADR it was written for.

    An ADR whose decisions.yaml entry has no ``date`` key still gets one from
    the model's default (today), which moves every day and is frozen at some
    later day whenever any pmlens rewrites decisions.yaml. Such a date cannot
    identify the ADR, so the anchor stores ``date: null`` and
    :func:`anchor_state` then compares the title alone.

    Args:
        decision: The ADR.
        date_on_file: Whether decisions.yaml holds the ADR's date. ``None``
            (the default) reads it from the model: the date counts only when
            it was given at load time (``model_fields_set``). The creator of a
            new ADR passes True: it writes the ADR out, date included.

    Returns:
        ``{"date": ISO date or None, "title_sha256": hex digest}``.
    """
    if date_on_file is None:
        date_on_file = "date" in decision.model_fields_set
    return {
        "date": _iso(decision.date) if date_on_file else None,
        "title_sha256": title_sha256(decision.title),
    }


def anchor_state(doc: Mapping, decision: Decision) -> str:
    """Compare a lineage document's anchor with the ADR.

    ``anchor.date: null`` means the lineage was written for an ADR without a
    date (:func:`anchor_for`); only the title hash is compared then, so a date
    added later (by hand, or by a rewrite that fills in the default) does not
    detach the lineage.

    Returns:
        ``"missing"`` when there is no anchor (only a hand edit removes it),
        ``"match"`` when date and title hash agree, otherwise ``"mismatch"``.
    """
    anchor = doc.get("anchor")
    if anchor is None:
        return "missing"
    if not isinstance(anchor, dict) or "date" not in anchor:
        return "mismatch"
    if anchor.get("title_sha256") != title_sha256(decision.title):
        return "mismatch"
    date = anchor["date"]
    if date is None:
        return "match"
    if isinstance(date, _dt.date):  # an unquoted date read back by safe_load
        date = date.isoformat()
    return "match" if date == _iso(decision.date) else "mismatch"


# ─── Bounded read ────────────────────────────────────


class _NotRegularOrTooLargeError(Exception):
    """The path is not a regular file of at most MAX_LINEAGE_BYTES."""


def _read_bounded(path: Path) -> bytes:
    """Read a regular file of at most MAX_LINEAGE_BYTES, without following links.

    ``O_NOFOLLOW`` refuses a symlink and ``O_NONBLOCK`` keeps a FIFO from
    blocking the open; the checks run on the opened descriptor, so swapping the
    path between a check and the open cannot slip past them.

    Raises:
        FileNotFoundError: The file (or its directory) does not exist.
        OSError: The file could not be opened or read (a symlink included).
        _NotRegularOrTooLargeError: Not a regular file, or larger than the cap.
    """
    flags = (
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    fd = os.open(path, flags)
    chunks: list[bytes] = []
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise _NotRegularOrTooLargeError("not a regular file")
        if info.st_size > MAX_LINEAGE_BYTES:
            raise _NotRegularOrTooLargeError("larger than the size limit")
        remaining = MAX_LINEAGE_BYTES + 1
        while remaining > 0:
            chunk = os.read(fd, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
    finally:
        os.close(fd)
    payload = b"".join(chunks)
    if len(payload) > MAX_LINEAGE_BYTES:
        raise _NotRegularOrTooLargeError("larger than the size limit")
    return payload


def _nested_deeper_than(data: object, limit: int) -> bool:
    """True when lists and mappings in ``data`` nest more than ``limit`` levels deep.

    Iterative, so a deep value cannot exhaust the stack here. Containers are
    visited in document order and each one once (by identity), the way
    safe_dump expands a value at its first appearance and writes an alias
    after that: the depth found is the nesting a dump would recurse through,
    and an alias bomb or a recursive alias costs one visit per container.

    Args:
        data: A ``safe_load`` result.
        limit: The deepest nesting allowed; the top-level container is level 1.

    Returns:
        Whether the nesting exceeds ``limit``.
    """
    seen: set[int] = set()
    stack: list[tuple[object, int]] = [(data, 1)]
    while stack:
        node, depth = stack.pop()
        if not isinstance(node, (dict, list, tuple, set, frozenset)) or id(node) in seen:
            continue
        seen.add(id(node))
        if depth > limit:
            return True
        if isinstance(node, dict):
            children: Iterable[object] = node.values()
        elif isinstance(node, (list, tuple)):
            children = node
        else:
            continue  # a set's members are hashable scalars
        stack.extend((child, depth + 1) for child in reversed(list(children)))
    return False


@dataclass(frozen=True)
class RawLineage:
    """What :func:`read_lineage_raw` found for one ADR.

    Attributes:
        decision_id: The ADR id the file was looked up for ("" when invalid).
        exists: Whether something is at the lineage path.
        data: The ``safe_load`` result when it could be read and parsed.
        error: A note code when it could not (``decision_lineage_unreadable``
            or ``decision_id_invalid``).
        detail: A content-free reason (exception type, YAML line and column).
        size: How many bytes were read from the file (0 when nothing was
            read); what parsing it cost, for readers with a byte budget.
    """

    decision_id: str
    exists: bool
    data: object = None
    error: str | None = None
    detail: str | None = None
    size: int = 0


def read_lineage_raw(pm_path: Path, decision_id: str) -> RawLineage:
    """Read one ADR's lineage file without locking, creating or raising.

    The id is checked against DECISION_ID_RE before a path is built from it
    (the path-traversal guard). A symlinked lineage directory or file, anything
    but a regular file, a file over MAX_LINEAGE_BYTES, an OS error, invalid
    UTF-8, invalid YAML and lists or mappings nested more than
    MAX_LINEAGE_DEPTH levels deep all come back as
    ``decision_lineage_unreadable``. Writers read through here too, so they
    never take on a file they could not dump again.

    Args:
        pm_path: The project's ``.pm`` directory.
        decision_id: The ADR id.

    Returns:
        The raw result; :func:`lineage_view` turns it into what readers show.
    """
    if not is_decision_id(decision_id):
        return RawLineage(decision_id="", exists=False, error=DECISION_ID_INVALID)
    directory = pm_path / LINEAGE_DIR
    if directory.is_symlink():
        return RawLineage(
            decision_id, exists=True, error=LINEAGE_UNREADABLE, detail="symlinked directory"
        )
    try:
        payload = _read_bounded(directory / f"{decision_id}.yaml")
    except FileNotFoundError:
        return RawLineage(decision_id, exists=False)
    except _NotRegularOrTooLargeError as exc:
        return RawLineage(decision_id, exists=True, error=LINEAGE_UNREADABLE, detail=str(exc))
    except OSError as exc:
        return RawLineage(
            decision_id, exists=True, error=LINEAGE_UNREADABLE, detail=error_summary(exc)
        )
    size = len(payload)
    try:
        data = yaml.safe_load(payload.decode("utf-8"))
    except Exception as exc:  # noqa: BLE001 - a read never raises; the type is reported
        return RawLineage(
            decision_id,
            exists=True,
            error=LINEAGE_UNREADABLE,
            detail=error_summary(exc),
            size=size,
        )
    if _nested_deeper_than(data, MAX_LINEAGE_DEPTH):
        return RawLineage(
            decision_id,
            exists=True,
            error=LINEAGE_UNREADABLE,
            detail=f"nested more than {MAX_LINEAGE_DEPTH} levels deep",
            size=size,
        )
    return RawLineage(decision_id, exists=True, data=data, size=size)


def link_targets(raw: RawLineage, link_type: str) -> list[str]:
    """Valid ADR ids listed under ``links.<link_type>`` of a raw lineage.

    Anything unreadable, malformed or not an ADR id is ignored. The anchor is
    not checked; use this for hints, not for attribution.
    """
    data = raw.data
    if raw.error or not isinstance(data, dict):
        return []
    links = data.get("links")
    if not isinstance(links, dict):
        return []
    items = links.get(link_type)
    if not isinstance(items, list):
        return []
    return [item for item in items if is_decision_id(item)]


# ─── Read view ───────────────────────────────────────


class _ViewNotes:
    """Collects note codes (with counts) and the redaction tally of one view."""

    def __init__(self) -> None:
        self.counts: dict[str, int] = {}
        self.redactions = 0
        self._cache = ScrubCache()

    def add(self, code: str, count: int = 1) -> None:
        self.counts[code] = self.counts.get(code, 0) + count

    def skipped(self, count: int = 1) -> None:
        self.add(LINEAGE_ITEMS_SKIPPED, count)

    def text(self, value: str, limit: int) -> tuple[str, bool]:
        out, cut, count = _scrub_clip(value, limit, self._cache)
        self.redactions += count
        return out, cut

    def as_list(self) -> list[dict]:
        return [{"code": code, "count": count} for code, count in self.counts.items()]


def _unknown_declared() -> dict[str, str]:
    return dict.fromkeys(DECLARED_FIELDS, UNKNOWN)


def _empty_links() -> dict[str, list[str]]:
    return {link_type: [] for link_type in LINK_TYPES}


@dataclass
class LineageView:
    """What readers show for one ADR's lineage (design §2.7, §3.2).

    Built only from allow-listed fields as plain strings, so it is bounded and
    JSON-safe whatever the file holds. ``derived`` means the lifecycle and the
    rest were inferred from the ADR's status, not taken from a lineage.
    """

    decision_id: str
    derived: bool
    lifecycle: str | None
    effective_lifecycle: str | None
    lifecycle_known: bool = False
    schema: int | None = None
    recorded_at: str | None = None
    declared: dict[str, str] = field(default_factory=_unknown_declared)
    declared_later: list[str] = field(default_factory=list)
    not_recorded: list[str] = field(default_factory=list)
    links: dict[str, list[str]] = field(default_factory=_empty_links)
    events: list[dict] = field(default_factory=list)
    events_total: int = 0
    unknown_keys: list[str] = field(default_factory=list)
    notes: list[dict] = field(default_factory=list)
    projected_status: str | None = None
    status_mismatch: bool = False
    redactions: int = 0

    @property
    def note_codes(self) -> set[str]:
        """The note codes present in this view."""
        return {entry["code"] for entry in self.notes}

    def as_dict(self) -> dict:
        """The ``lineage`` object of a read response."""
        return {
            "derived": self.derived,
            "schema": self.schema,
            "lifecycle": self.lifecycle,
            "recorded_at": self.recorded_at,
            "declared": dict(self.declared),
            "declared_later": list(self.declared_later),
            "not_recorded": list(self.not_recorded),
            "links": {key: list(value) for key, value in self.links.items()},
            "events": [dict(event) for event in self.events],
            "events_total": self.events_total,
            "unknown_keys": list(self.unknown_keys),
            "notes": [dict(entry) for entry in self.notes],
        }


def _not_recorded(recorded_at: str | None, declared: Mapping[str, str]) -> list[str]:
    """Fields never recorded, in NOT_RECORDED_ORDER (design §3.2).

    The same rule applies whether or not the view is derived: a null
    ``recorded_at`` and every declared value still ``unknown``.
    """
    return [
        name
        for name in NOT_RECORDED_ORDER
        if (recorded_at is None if name == "recorded_at" else declared.get(name) == UNKNOWN)
    ]


def _key_names(keys: Iterable[object], limit: int, notes: _ViewNotes) -> list[str]:
    """Untrusted key names as redacted strings, each cut to MAX_LABEL_CHARS."""
    names: list[str] = []
    for key in keys:
        if len(names) >= limit:
            break
        text, _ = notes.text(str(key), MAX_LABEL_CHARS)
        names.append(text)
    return names


def _view_time(value: object, notes: _ViewNotes) -> tuple[str | None, bool]:
    """A timestamp as a string; date/datetime via isoformat, other types null."""
    if value is None:
        return None, False
    if isinstance(value, str):
        return notes.text(value, MAX_LABEL_CHARS)
    if isinstance(value, _dt.date):  # datetime is a date subclass
        return value.isoformat(), False
    notes.skipped()
    return None, False


def _view_declared(raw: object, notes: _ViewNotes) -> dict[str, str]:
    declared = _unknown_declared()
    if raw is None:
        return declared
    if not isinstance(raw, dict):
        notes.skipped()
        return declared
    for name in DECLARED_FIELDS:
        if name not in raw:
            continue
        value = raw[name]
        if isinstance(value, str):
            declared[name], _ = notes.text(value, MAX_LABEL_CHARS)
        else:
            notes.skipped()
    return declared


def _view_links(raw: object, notes: _ViewNotes) -> dict[str, list[str]]:
    links = _empty_links()
    if raw is None:
        return links
    if not isinstance(raw, dict):
        notes.skipped()
        return links
    for link_type in LINK_TYPES:
        items = raw.get(link_type)
        if items is None:
            continue
        if not isinstance(items, list):
            notes.skipped()
            continue
        for item in items:
            if (
                is_decision_id(item)
                and item not in links[link_type]
                and len(links[link_type]) < MAX_LINKS_PER_TYPE
            ):
                links[link_type].append(item)
            else:
                notes.skipped()
    return links


def _view_event(event: dict, notes: _ViewNotes) -> dict:
    """One event through the per-kind allow-list (design §2.4)."""
    out: dict[str, object] = {}
    truncated = False
    out["at"], cut = _view_time(event.get("at"), notes)
    truncated |= cut
    kind = event.get("kind")
    if isinstance(kind, str):
        out["kind"], cut = notes.text(kind, MAX_LABEL_CHARS)
        truncated |= cut
    else:
        out["kind"] = None
        notes.skipped()
    via = event.get("via")
    if isinstance(via, str):
        out["via"], cut = notes.text(via, MAX_LABEL_CHARS)
        truncated |= cut
    elif via is not None:
        notes.skipped()
    allowed = _EVENT_FIELDS.get(kind) if isinstance(kind, str) else None
    for name in allowed or ():
        if name not in event:
            continue
        value = event[name]
        if not isinstance(value, str):
            notes.skipped()
            continue
        limit = MAX_TEXT_CHARS if name in _FREE_TEXT_FIELDS else MAX_LABEL_CHARS
        out[name], cut = notes.text(value, limit)
        truncated |= cut
    if kind == "evaluation":
        out["recorded_as"] = ASSISTANT_RECORDED
    extra = [
        key for key in event if key not in _EVENT_COMMON and (allowed is None or key not in allowed)
    ]
    if extra:
        out["unknown_fields"] = _key_names(extra, MAX_UNKNOWN_FIELDS, notes)
    if truncated:
        out["truncated"] = True
    return out


def _view_events(raw: object, notes: _ViewNotes) -> tuple[list[dict], int, list[str]]:
    """The last RECENT_EVENTS events (file order), the total, declared_later."""
    if raw is None:
        return [], 0, []
    if not isinstance(raw, list):
        notes.skipped()
        return [], 0, []
    declared_later: list[str] = []
    for event in raw:
        if not isinstance(event, dict) or event.get("kind") != "declared":
            continue
        name = event.get("field")
        if isinstance(name, str) and name in DECLARED_FIELDS and name not in declared_later:
            declared_later.append(name)
    shown: list[dict] = []
    for event in raw[-RECENT_EVENTS:]:
        if isinstance(event, dict):
            shown.append(_view_event(event, notes))
        else:
            notes.skipped()
    return shown, len(raw), declared_later


def _attributed_doc(
    decision: Decision, raw: RawLineage, *, duplicate: bool = False
) -> tuple[dict | None, str | None]:
    """The lineage document to attribute to ``decision``, or why there is none.

    A lineage belongs to the ADR only when the ADR id is valid and unique, the
    file was read, it is a mapping whose ``decision_id`` names this ADR and its
    anchor (when present) matches. :func:`lineage_view` and
    :func:`scan_linked_from` share this rule, so a reader never attributes a
    lineage the view would not.

    Returns:
        ``(doc, None)`` when attributed; ``(None, code)`` with the note code
        that says why not; ``(None, None)`` when the ADR simply has no file.
    """
    if not is_decision_id(decision.id):
        return None, DECISION_ID_INVALID
    if duplicate:
        return None, DECISION_ID_DUPLICATE
    if raw.decision_id != decision.id and (raw.exists or raw.error):
        return None, LINEAGE_UNREADABLE
    if raw.error:
        return None, raw.error
    if not raw.exists:
        return None, None
    doc = raw.data
    if not isinstance(doc, dict) or doc.get("decision_id") != decision.id:
        return None, LINEAGE_UNREADABLE
    if anchor_state(doc, decision) == "mismatch":
        return None, LINEAGE_ANCHOR_MISMATCH
    return doc, None


def lineage_view(
    decision: Decision,
    raw: RawLineage,
    *,
    duplicate: bool = False,
    include_events: bool = True,
) -> LineageView:
    """Build what readers show for one ADR; pure, no I/O (design §2.7).

    The lineage is attributed to the ADR only when the ADR id is valid and
    unique, the file was read, it is a mapping whose ``decision_id`` names this
    ADR and its anchor (when present) matches. Otherwise the ADR is shown as
    derived from its status, with a note saying why.

    Args:
        decision: The ADR from decisions.yaml.
        raw: :func:`read_lineage_raw` for the same id.
        duplicate: True when decisions.yaml holds this id more than once.
        include_events: False for a reader that needs only the lifecycle,
            the declared values, the links and the attribution notes (a list
            row, the draft guard): the events, ``declared_later`` and the
            unknown top-level keys are then left empty, so their text is
            neither redacted nor cut. Everything else is the same as with
            True.

    Returns:
        The view; its notes carry codes and counts only.
    """
    notes = _ViewNotes()
    derived_lifecycle = derive_lifecycle(decision.status)

    def derived() -> LineageView:
        declared = _unknown_declared()
        if derived_lifecycle == DecisionLifecycle.SUPERSEDED:
            notes.add(LINEAGE_NO_SUCCESSOR)
        return LineageView(
            decision_id=decision.id if is_decision_id(decision.id) else "",
            derived=True,
            lifecycle=derived_lifecycle,
            effective_lifecycle=derived_lifecycle,
            declared=declared,
            not_recorded=_not_recorded(None, declared),
            notes=notes.as_list(),
            redactions=notes.redactions,
        )

    doc, why_not = _attributed_doc(decision, raw, duplicate=duplicate)
    if doc is None:
        if why_not is not None:
            notes.add(why_not)
        return derived()
    if anchor_state(doc, decision) == "missing":
        notes.add(LINEAGE_ANCHOR_MISSING)

    schema: int | None = None
    if "schema" in doc:
        value = doc["schema"]
        if isinstance(value, int) and not isinstance(value, bool):
            schema = value
            if value > LINEAGE_SCHEMA:
                notes.add(LINEAGE_SCHEMA_UNSUPPORTED)
        else:
            notes.add(LINEAGE_SCHEMA_UNSUPPORTED)

    recorded_at, _ = _view_time(doc.get("recorded_at"), notes)
    declared = _view_declared(doc.get("declared"), notes)
    links = _view_links(doc.get("links"), notes)

    raw_lifecycle = doc.get("lifecycle")
    lifecycle_known = is_known_lifecycle(raw_lifecycle)
    if lifecycle_known:
        lifecycle: str | None = raw_lifecycle
        effective = raw_lifecycle
    elif isinstance(raw_lifecycle, str):
        lifecycle, _ = notes.text(raw_lifecycle, MAX_LABEL_CHARS)
        effective = derived_lifecycle
        notes.add(LINEAGE_LIFECYCLE_UNKNOWN)
    else:
        lifecycle = derived_lifecycle
        effective = derived_lifecycle
        notes.add(LINEAGE_LIFECYCLE_UNKNOWN)
    if lifecycle == DecisionLifecycle.SUPERSEDED and not links["superseded_by"]:
        notes.add(LINEAGE_NO_SUCCESSOR)

    events: list[dict] = []
    events_total = 0
    declared_later: list[str] = []
    unknown_keys: list[str] = []
    if include_events:
        events, events_total, declared_later = _view_events(doc.get("events"), notes)
        unknown_keys = _key_names(
            (key for key in doc if key not in _KNOWN_TOP_LEVEL), MAX_UNKNOWN_KEYS, notes
        )

    projected: str | None = None
    mismatch = False
    if lifecycle_known:
        projected = project_status(raw_lifecycle, links["superseded_by"]).value
        mismatch = projected != decision.status

    return LineageView(
        decision_id=decision.id,
        derived=False,
        lifecycle=lifecycle,
        effective_lifecycle=effective,
        lifecycle_known=lifecycle_known,
        schema=schema,
        recorded_at=recorded_at,
        declared=declared,
        declared_later=declared_later,
        not_recorded=_not_recorded(recorded_at, declared),
        links=links,
        events=events,
        events_total=events_total,
        unknown_keys=unknown_keys,
        notes=notes.as_list(),
        projected_status=projected,
        status_mismatch=mismatch,
        redactions=notes.redactions,
    )


def effective_lifecycle(
    decision: Decision, raw: RawLineage, *, duplicate: bool = False
) -> str | None:
    """The lifecycle to act on: the lineage's when attributed and known, else derived.

    Pure; takes no lock. ``None`` when neither exists (unknown status, no
    usable lineage). Builds the view without its events
    (``include_events=False``): the lifecycle does not depend on them.
    """
    return lineage_view(
        decision, raw, duplicate=duplicate, include_events=False
    ).effective_lifecycle


def key_labels(keys: Iterable[object], limit: int = MAX_UNKNOWN_KEYS) -> tuple[list[str], int]:
    """Untrusted key names for a response: ``str()``, redacted, cut, at most ``limit``.

    Used for the unknown keys of a decisions.yaml record: only the names are
    shown, never the values, which may be bytes, aliases or secrets.

    Returns:
        The names (each at most MAX_LABEL_CHARS) and how many redactions were made.
    """
    notes = _ViewNotes()
    return _key_names(keys, limit, notes), notes.redactions


@dataclass
class LinkedFrom:
    """Other ADRs whose attributed lineage links to one ADR (design §4.2).

    Attributes:
        supersedes: ADRs that list it under ``supersedes``, in number order.
        amends: ADRs that list it under ``amends``, in number order.
        superseded_by: ADRs that list it under ``superseded_by`` (not shown as
            ``linked_from``; used to find one-sided links), in number order.
        truncated: How many lineages of ADRs in decisions.yaml the scan left
            unread because it reached its file limit or byte budget; neither
            linked_from nor the check of the other side of a link covers them.
        unreadable: How many lineage files of ADRs in decisions.yaml were read
            but could not be used (unreadable, not a mapping, or naming
            another ADR): whether they link to the ADR is unknown.
        links_of: The links of every lineage the scan read, by ADR id, as
            :func:`lineage_view` shows them (ADR ids only, deduplicated, at
            most MAX_LINKS_PER_TYPE; ``None`` when the lineage is not
            attributed), so a caller checking the other side of a link need
            not read the file again. An ADR the scan wanted but left unread is
            missing here (it is counted in ``truncated``).
    """

    supersedes: list[str] = field(default_factory=list)
    amends: list[str] = field(default_factory=list)
    superseded_by: list[str] = field(default_factory=list)
    truncated: int = 0
    unreadable: int = 0
    links_of: dict[str, dict[str, list[str]] | None] = field(default_factory=dict)


def _decision_order(decision_id: str) -> tuple[int, str]:
    """Sort key for DECISION_ID_RE ids: by number, then by text (ADR-010 vs ADR-0010).

    The digits are ASCII only (DECISION_ID_RE), so ``int`` cannot fail.
    """
    return int(decision_id[4:]), decision_id


def scan_linked_from(
    pm_path: Path,
    decision_id: str,
    decisions: Mapping[str, Decision],
    *,
    first: Iterable[str] = (),
    limit: int | None = None,
    max_bytes: int | None = None,
) -> LinkedFrom:
    """Collect the ADRs whose lineage links to ``decision_id``, without writing.

    The lineage directory is globbed for ``ADR-*.yaml``; only stems matching
    DECISION_ID_RE and naming an ADR of ``decisions`` are read, through the
    bounded reader. The ADRs in ``first`` (the ADR's own supersedes and
    superseded_by targets, whose links the caller checks from the other side)
    are read first, whether or not the glob listed them; then the rest. Each
    group is read newest (highest number) first: supersedes and amends
    usually point from a newer ADR to an older one, so when the scan stops
    early it is the oldest lineages, the least likely to link here, that are
    left unread. It
    stops after ``limit`` files (LINKED_FROM_SCAN_LIMIT by default) or once
    ``max_bytes`` have been read (LINKED_FROM_SCAN_BYTES by default; the file
    that crosses the budget is still used), so with ``first`` it is the only
    reader of other ADRs' lineages a get needs. A file counts only when its
    lineage is attributed to its ADR (the rule :func:`lineage_view` uses), so
    an orphaned lineage, one written for an older ADR with the same number
    (anchor mismatch) or one for an ADR not in ``decisions`` links nothing and
    is not counted. A file of such an ADR that cannot be used (unreadable, not
    a mapping, or naming another ADR) is counted in ``unreadable``: whether it
    links here is unknown.

    Args:
        pm_path: The project's ``.pm`` directory.
        decision_id: The ADR the links should point to.
        decisions: The ADRs of decisions.yaml whose id is valid and unique.
        first: ADR ids to read before the others (ids not in ``decisions``,
            invalid ones and ``decision_id`` itself are ignored).
        limit: How many lineage files to read at most.
        max_bytes: How many bytes to read at most (checked before each file).

    Returns:
        The linking ADR ids per link type (in number order), what each lineage
        read links to, and how many were left unread or could not be used.
    """
    found = LinkedFrom()
    if not is_decision_id(decision_id):
        return found
    directory = pm_path / LINEAGE_DIR
    if directory.is_symlink():  # read_lineage_raw refuses it as well
        return found
    try:
        listed = {entry.stem for entry in directory.glob("ADR-*.yaml")}
    except OSError:
        listed = set()

    def wanted(stem: str) -> bool:
        return is_decision_id(stem) and stem != decision_id and stem in decisions

    ahead = sorted({stem for stem in first if wanted(stem)}, key=_decision_order, reverse=True)
    rest = sorted(
        (stem for stem in listed.difference(ahead) if wanted(stem)),
        key=_decision_order,
        reverse=True,
    )
    stems = ahead + rest
    cap = LINKED_FROM_SCAN_LIMIT if limit is None else limit
    budget = LINKED_FROM_SCAN_BYTES if max_bytes is None else max_bytes
    spent = 0
    for index, stem in enumerate(stems):
        if index >= cap or spent >= budget:
            found.truncated = len(stems) - index
            break
        raw = read_lineage_raw(pm_path, stem)
        spent += raw.size
        doc, why_not = _attributed_doc(decisions[stem], raw)
        if doc is None:
            found.links_of[stem] = None
            if why_not == LINEAGE_UNREADABLE:
                found.unreadable += 1
            continue
        links = _view_links(doc.get("links"), _ViewNotes())
        found.links_of[stem] = links
        if decision_id in links["supersedes"]:
            found.supersedes.append(stem)
        if decision_id in links["amends"]:
            found.amends.append(stem)
        if decision_id in links["superseded_by"]:
            found.superseded_by.append(stem)
    for ids in (found.supersedes, found.amends, found.superseded_by):
        ids.sort(key=_decision_order)
    return found


# ─── Write-side builders (pure; storage writes) ──────


def declared_error(declared: Mapping[str, object]) -> dict | None:
    """Check declared values against the vocabularies (missing means unknown).

    Returns:
        An error dict (``invalid_origin`` / ``invalid_recorded_timing`` /
        ``invalid_decision_kind``) or ``None``.
    """
    for name, vocabulary in (
        ("origin", DecisionOrigin),
        ("recorded_timing", RecordedTiming),
        ("decision_kind", DecisionKind),
    ):
        value = declared.get(name, UNKNOWN)
        allowed = [item.value for item in vocabulary]
        if not isinstance(value, str) or value not in allowed:
            return _error(
                f"invalid_{name}",
                f"{name} must be one of: {', '.join(allowed)}",
            )
    return None


def new_lineage_doc(
    decision: Decision,
    declared: Mapping[str, str] | None,
    now: str,
    *,
    via: str = "pm_add_decision",
) -> dict:
    """The lineage document for a newly recorded ADR (kind=created).

    Args:
        decision: The ADR just appended to decisions.yaml (known status), with
            its title as stored (:func:`stored_title`), which the anchor hashes.
        declared: Declared provenance; missing values are ``unknown``.
        now: The timestamp from :func:`_utc_now`.
        via: The tool name recorded on the event.

    Raises:
        PmServerError: The status has no lifecycle, or a declared value is
            outside its vocabulary.
    """
    declared = declared or {}
    problem = declared_error(declared)
    if problem is not None:
        raise PmServerError(problem["message"])
    lifecycle = derive_lifecycle(decision.status)
    if lifecycle is None:
        raise PmServerError(f"{decision.id}: status has no lifecycle; cannot start a lineage")
    status = DecisionStatus(decision.status).value
    return {
        "schema": LINEAGE_SCHEMA,
        "decision_id": decision.id,
        # The creator writes the ADR out with its date (the model default
        # included), so the date is part of the fingerprint from the start.
        "anchor": anchor_for(decision, date_on_file=True),
        "recorded_at": now,
        "declared": {name: str(declared.get(name, UNKNOWN)) for name in DECLARED_FIELDS},
        "lifecycle": lifecycle,
        "links": _empty_links(),
        "events": [
            {"at": now, "kind": "created", "lifecycle": lifecycle, "status": status, "via": via}
        ],
    }


def started_doc(decision: Decision, now: str, *, via: str = "pm_update_decision") -> dict | None:
    """A lineage for an existing ADR that had none (kind=lineage_started).

    ``recorded_at`` stays null and declared values unknown: nothing is
    invented after the fact. Returns ``None`` when the status is unknown.
    """
    lifecycle = derive_lifecycle(decision.status)
    if lifecycle is None:
        return None
    status = DecisionStatus(decision.status).value
    return {
        "schema": LINEAGE_SCHEMA,
        "decision_id": decision.id,
        "anchor": anchor_for(decision),
        "recorded_at": None,
        "declared": _unknown_declared(),
        "lifecycle": lifecycle,
        "links": _empty_links(),
        "events": [
            {
                "at": now,
                "kind": "lineage_started",
                "basis": "derived_from_status",
                "status": status,
                "lifecycle": lifecycle,
                "via": via,
            }
        ],
    }


def dump_lineage(doc: Mapping, decision_id: str) -> str:
    """The exact text ``storage._save_yaml`` writes for ``doc`` (header included).

    Unbounded: a string repeated through aliases is written out every time.
    Writers use ``storage``'s capped dump, which produces the same text.
    """
    body = yaml.safe_dump(
        doc,
        default_flow_style=False,
        allow_unicode=True,
        sort_keys=False,
    )
    return f"# PM Lens - {lineage_header_name(decision_id)}\n" + body


def write_refusal(doc: object, decision: Decision) -> tuple[str, str] | None:
    """Why a writer must not rewrite this lineage document, or ``None`` (§2.6).

    A writer edits the loaded mapping in place and writes everything else back
    as it was, so it must refuse a document whose shape it cannot edit safely.

    Returns:
        ``(code, message)``; messages name the ADR and the problem, never values.
    """
    adr = decision.id
    if not isinstance(doc, dict):
        return LINEAGE_UNREADABLE, f"{adr}: the lineage file is not a mapping"
    if doc.get("decision_id") != adr:
        return LINEAGE_UNREADABLE, f"{adr}: the lineage file names a different decision_id"
    if "schema" in doc:
        schema = doc["schema"]
        if not isinstance(schema, int) or isinstance(schema, bool) or schema > LINEAGE_SCHEMA:
            return (
                LINEAGE_SCHEMA_UNSUPPORTED,
                f"{adr}: the lineage file uses a schema this pmlens cannot write; "
                "update pmlens before changing it",
            )
    if not is_known_lifecycle(doc.get("lifecycle")):
        return (
            LINEAGE_LIFECYCLE_UNKNOWN,
            f"{adr}: the lineage lifecycle is missing or not one of {', '.join(LIFECYCLES)}",
        )
    declared = doc.get("declared")
    if declared is not None and not isinstance(declared, dict):
        return LINEAGE_UNREADABLE, f"{adr}: declared in the lineage file is not a mapping"
    links = doc.get("links")
    if links is not None:
        if not isinstance(links, dict):
            return LINEAGE_UNREADABLE, f"{adr}: links in the lineage file is not a mapping"
        for link_type in LINK_TYPES:
            items = links.get(link_type)
            if items is None:
                continue
            if not isinstance(items, list) or not all(is_decision_id(item) for item in items):
                return (
                    LINEAGE_UNREADABLE,
                    f"{adr}: links.{link_type} in the lineage file is not a list of ADR ids",
                )
    events = doc.get("events")
    if events is not None and not isinstance(events, list):
        return LINEAGE_UNREADABLE, f"{adr}: events in the lineage file is not a list"
    if anchor_state(doc, decision) == "mismatch":
        return (
            LINEAGE_ANCHOR_MISMATCH,
            f"{adr}: the lineage file's anchor does not match the ADR's date and title: it "
            "was written for another ADR with this id, or the ADR's title or date changed "
            "after it was written. Nothing was written. If the lineage belongs to this ADR, "
            f"delete anchor from .pm/{LINEAGE_DIR}/{adr}.yaml by hand and call again; the "
            "next change ties it to the ADR's current date and title. Otherwise move the "
            f"file out of .pm/{LINEAGE_DIR} by hand.",
        )
    return None


@dataclass(frozen=True)
class LineageChange:
    """One pm_update_decision call's requested changes (design §4.3).

    ``None`` (or an empty string for the texts) means "not requested".
    """

    lifecycle: str | None = None
    reason: str | None = None
    add_links: Mapping[str, Sequence[str]] | None = None
    remove_links: Mapping[str, Sequence[str]] | None = None
    evaluation: str | None = None
    evaluation_kind: str = EvaluationKind.OTHER.value
    note: str | None = None
    origin: str | None = None
    recorded_timing: str | None = None
    via: str = "pm_update_decision"


@dataclass
class ChangeOutcome:
    """Result of :func:`apply_change`.

    Attributes:
        error: An error dict; when set nothing must be written.
        doc: The new lineage document (``None`` when there is nothing to write).
        changed: Whether the call changed anything worth writing.
        status_before: The ADR's status before the call, as a redacted label.
        new_status: The status value decisions.yaml should hold afterwards (the
            stored value itself when the call does not change it, so compare it
            with the ADR's status, and label it before showing it).
        lifecycle: The lineage lifecycle afterwards.
        links: The lineage links afterwards.
        events_added: Kinds of the appended events, in file order.
        changes: ``{"lifecycle": {...}, "decision_status": {...}, "declared": {...}}``,
            only for what changed.
        notices: Warning entries (server._build_warning shape).
        added_links: ``{link_type: [ids]}`` actually added by this call.
    """

    error: dict | None = None
    doc: dict | None = None
    changed: bool = False
    status_before: str = ""
    new_status: str = ""
    lifecycle: str | None = None
    links: dict[str, list[str]] = field(default_factory=_empty_links)
    events_added: list[str] = field(default_factory=list)
    changes: dict = field(default_factory=dict)
    notices: list[dict] = field(default_factory=list)
    added_links: dict[str, list[str]] = field(default_factory=dict)


def _given(text: str | None) -> bool:
    return isinstance(text, str) and text.strip() != ""


def _link_argument_error(argument: str, value: object) -> dict | None:
    """Check one of add_links / remove_links by its shape and size alone.

    The length of each list is checked before anything looks at its items, so
    an oversized argument is refused in constant time, before any lock is
    taken (no type may hold more than MAX_LINKS_PER_TYPE ids anyway).
    """
    if value is None:
        return None
    if not isinstance(value, Mapping):
        return _error(
            "invalid_link_type",
            f"{argument} must map supersedes / superseded_by / amends to lists of ADR ids",
        )
    for link_type, targets in value.items():
        if link_type not in LINK_TYPES:
            return _error(
                "invalid_link_type",
                f"{argument}: link type must be one of {', '.join(LINK_TYPES)}",
            )
        if isinstance(targets, (str, bytes)) or not isinstance(targets, Sequence):
            return _error("invalid_link_target", f"{argument}.{link_type} must be a list of ids")
        if len(targets) > MAX_LINKS_PER_TYPE:
            return _error(
                "too_many_links",
                f"{argument}.{link_type} may list at most {MAX_LINKS_PER_TYPE} ids",
            )
        for target in targets:
            if not is_decision_id(target):
                return _error(
                    "invalid_link_target",
                    f"{argument}.{link_type}: every target must look like ADR-NNN",
                )
    return None


def validate_change(change: LineageChange) -> dict | None:
    """Argument-only checks for a lineage change (no file state needed).

    Returns:
        An error dict, or ``None`` when the arguments are acceptable.
    """
    if change.lifecycle is not None and not is_known_lifecycle(change.lifecycle):
        return _error("invalid_lifecycle", f"lifecycle must be one of: {', '.join(LIFECYCLES)}")
    kinds = [item.value for item in EvaluationKind]
    if change.evaluation_kind not in kinds:
        return _error(
            "invalid_evaluation_kind", f"evaluation_kind must be one of: {', '.join(kinds)}"
        )
    origins = [item.value for item in DecisionOrigin]
    if change.origin is not None and change.origin not in origins:
        return _error("invalid_origin", f"origin must be one of: {', '.join(origins)}")
    timings = [item.value for item in RecordedTiming]
    if change.recorded_timing is not None and change.recorded_timing not in timings:
        return _error(
            "invalid_recorded_timing", f"recorded_timing must be one of: {', '.join(timings)}"
        )
    if change.origin is not None and change.origin not in BACKFILL_ORIGINS:
        return _error(
            "declared_backfill_value_not_allowed",
            "origin can only be filled in later as ai_auto; a value saying a person took "
            "part cannot be added after the fact",
        )
    if change.recorded_timing is not None and change.recorded_timing not in BACKFILL_TIMINGS:
        return _error(
            "declared_backfill_value_not_allowed",
            "recorded_timing can only be filled in later as before_impl, during_impl or post_hoc",
        )
    for argument, value in (("add_links", change.add_links), ("remove_links", change.remove_links)):
        problem = _link_argument_error(argument, value)
        if problem is not None:
            return problem
    for name, text in (
        ("reason", change.reason),
        ("note", change.note),
        ("evaluation", change.evaluation),
    ):
        if text is not None and len(text) > MAX_TEXT_CHARS:
            return _error("text_too_long", f"{name} is longer than {MAX_TEXT_CHARS} characters")
    needs_reason = (
        (any(change.remove_links.values()) if change.remove_links else False)
        or change.origin is not None
        or change.recorded_timing is not None
    )
    if needs_reason and not _given(change.reason):
        return _error(
            "reason_required",
            "reason is required to remove links or to fill in a declared value",
        )
    return None


def _dedupe(items: Iterable[str]) -> list[str]:
    """``items`` without repeats, first appearance first (linear time)."""
    return list(dict.fromkeys(items))


def appends_text(change: LineageChange) -> bool:
    """Whether ``change`` appends a note or an evaluation.

    Those are the events a caller can add without limit, so they stop at
    MAX_APPEND_BYTES (:func:`size_refusal`).
    """
    return _given(change.note) or _given(change.evaluation)


def size_refusal(decision_id: str, size: int | None, change: LineageChange) -> dict | None:
    """The ``decision_lineage_too_large`` error for a rewritten lineage, or ``None``.

    Every change must leave the file within MAX_LINEAGE_BYTES (what readers
    accept). A change that appends a note or an evaluation must also leave it
    within MAX_APPEND_BYTES, so the last LINEAGE_RESERVE_BYTES stay free for
    lifecycle, link and declared changes: a lineage filled with notes can
    still be adopted, rejected or superseded (design §2.6). That room is not
    guaranteed, since repeated link and lifecycle changes use it too, and the
    remediation does not promise it.

    Args:
        decision_id: The ADR id.
        size: The new text's size in UTF-8 bytes; ``None`` when the capped
            dump gave up because it exceeds MAX_LINEAGE_BYTES.
        change: The change that produced it.

    Returns:
        The error dict (with a remediation), or ``None`` when the size is fine.
    """
    path = f".pm/{LINEAGE_DIR}/{decision_id}.yaml"
    trim = (
        f"move older note and evaluation events out of {path} by hand (for example into a "
        f"file outside .pm/{LINEAGE_DIR}), keeping its lifecycle, link and declared events"
    )
    if size is None or size > MAX_LINEAGE_BYTES:
        return _error(
            LINEAGE_TOO_LARGE,
            f"{decision_id}: the lineage would exceed {MAX_LINEAGE_BYTES} bytes; nothing was "
            "written",
            remediation=(
                f"Shorten the reason or split the change into smaller calls. If the file is "
                f"full, {trim}. A value the file repeats through YAML aliases (&name, *name) "
                "is written out in full at every repeat; replace such repeats by hand."
            ),
        )
    if appends_text(change) and size > MAX_APPEND_BYTES:
        return _error(
            LINEAGE_TOO_LARGE,
            f"{decision_id}: notes and evaluations are not added once the lineage would pass "
            f"{MAX_APPEND_BYTES} bytes, so that room is kept for lifecycle and link changes; "
            "nothing was written",
            remediation=(
                "Lifecycle, link and declared changes usually still fit: make them in a call "
                "without note and evaluation. Repeated link and lifecycle changes use up that "
                "room too, so one of them can be refused as well; then shorten its reason. To "
                f"add more notes or evaluations, or when even a short reason does not fit, {trim}."
            ),
        )
    return None


def _keep_one_reason(events: list[dict]) -> None:
    """Keep a call's reason on the last of its lifecycle, link and declared events.

    A call has one reason. Copying it to every event let one call with 50
    links and a 4,000-character reason fill most of the size cap, so it is
    stored once (design §2.4). It goes on the last such event because readers
    show the file's last RECENT_EVENTS events: a call's events are contiguous
    and its lifecycle, link and declared events come before its evaluation,
    note and status_reprojected ones (the order of ``_EVENT_FIELDS``), so a
    window that shows any of them also shows the one with the reason.

    That event keeps ``reason`` even when the call gave none (an empty
    string; only add_links can be called without one). Same-second calls share
    ``at`` and ``via``, so this is what tells them apart: a lifecycle, link or
    declared event without ``reason`` belongs to the call of the next event
    that has one.

    Args:
        events: The call's new events in file order; each lifecycle, link and
            declared event carries the reason (possibly empty) on entry.
    """
    carriers = [event for event in events if "reason" in event]
    for event in carriers[:-1]:
        del event["reason"]


def _with_anchor(doc: dict, decision: Decision) -> dict:
    """Re-insert the anchor right after decision_id (key order kept otherwise)."""
    out: dict = {}
    for key, value in doc.items():
        if key == "anchor":
            continue
        out[key] = value
        if key == "decision_id":
            out["anchor"] = anchor_for(decision)
    if "anchor" not in out:
        out["anchor"] = anchor_for(decision)
    return out


def apply_change(
    doc: dict | None,
    decision: Decision,
    change: LineageChange,
    known_ids: Iterable[str],
    now: str,
) -> ChangeOutcome:
    """Apply one lineage change to a loaded document; pure, no I/O (design §4.3).

    Order: declared backfill, links, lifecycle, evaluation, note. Events are
    appended with one ``at`` in the order of design §2.4, and the call's reason
    is stored once, on the last lifecycle, link or declared event
    (:func:`_keep_one_reason`). The status is decided
    by design §3.3 rule 4: when the lineage already disagreed with
    decisions.yaml before the call, the status is rewritten only if the call
    names a lifecycle (the same value is enough); otherwise it is left alone
    and ``decision_status_mismatch`` says so.

    Args:
        doc: The loaded lineage (already passed :func:`write_refusal`), or
            ``None`` when the ADR has none yet (a lineage_started one is made).
        decision: The ADR (its status is the projection to compare against).
        change: The requested change.
        known_ids: Ids present in decisions.yaml (link targets must be one).
        now: The timestamp from :func:`_utc_now`.

    Returns:
        The outcome; when ``error`` is set or ``changed`` is False, write nothing.
    """
    problem = validate_change(change)
    if problem is not None:
        return ChangeOutcome(error=problem)
    adr = decision.id
    started = doc is None
    if doc is None:
        doc = started_doc(decision, now, via=change.via)
        if doc is None:
            return ChangeOutcome(
                error=_error(
                    "decision_status_unknown",
                    f"{adr} has no lineage and its status is not one of proposed, accepted, "
                    "deprecated or superseded, so its lifecycle cannot be derived; fix the "
                    "status in decisions.yaml by hand first",
                )
            )
    else:
        refusal = write_refusal(doc, decision)
        if refusal is not None:
            return ChangeOutcome(error=_error(refusal[0], refusal[1]))
    anchor_missing = doc.get("anchor") is None

    stored_links = doc.get("links") if isinstance(doc.get("links"), dict) else {}
    links_before = {t: list(stored_links.get(t) or []) for t in LINK_TYPES}
    links = {t: list(items) for t, items in links_before.items()}
    lifecycle_before: str = doc["lifecycle"]
    status_before = decision.status
    status_label = scrub_label(status_before)
    pre_mismatch = (
        not started
        and project_status(lifecycle_before, links_before["superseded_by"]) != status_before
    )
    ids = set(known_ids)
    reason_text, reason_hits = scrub_text(change.reason) if _given(change.reason) else ("", 0)
    reason_used = False
    buckets: dict[str, list[dict]] = {kind: [] for kind in EVENT_KINDS}
    changes: dict = {}
    notices: list[dict] = []
    via = change.via

    # 1. declared backfill. A value that is not a string counts as unknown, as
    # readers show it (design §2.7), so what is shown as unknown can be filled.
    declared = dict(doc.get("declared") or {})
    for name, value in (("origin", change.origin), ("recorded_timing", change.recorded_timing)):
        if value is None:
            continue
        current = declared.get(name)
        if isinstance(current, str) and current != UNKNOWN:
            return ChangeOutcome(
                error=_error(
                    "declared_already_set",
                    f"{adr}: {name} is already declared; only an unknown value can be filled in",
                )
            )
        declared[name] = value
        changes.setdefault("declared", {})[name] = {"from": UNKNOWN, "to": value}
        buckets["declared"].append(
            {
                "at": now,
                "kind": "declared",
                "field": name,
                "value": value,
                "basis": "backfill",
                "reason": reason_text,
                "via": via,
            }
        )
        reason_used = True

    # 2. links
    added: dict[str, list[str]] = {}
    for link_type, targets in (change.remove_links or {}).items():
        for target in _dedupe(targets):
            if target not in links[link_type]:
                continue
            links[link_type] = [item for item in links[link_type] if item != target]
            buckets["link"].append(
                {
                    "at": now,
                    "kind": "link",
                    "op": "remove",
                    "type": link_type,
                    "target": target,
                    "reason": reason_text,
                    "via": via,
                }
            )
            reason_used = True
    for link_type, targets in (change.add_links or {}).items():
        for target in _dedupe(targets):
            if target == adr:
                return ChangeOutcome(error=_error("self_link", f"{adr} cannot link to itself"))
            if target not in ids:
                return ChangeOutcome(
                    error=_error("link_target_not_found", f"{target} is not in decisions.yaml")
                )
            if target in links[link_type]:
                continue
            links[link_type].append(target)
            added.setdefault(link_type, []).append(target)
            # An empty reason is kept too: it marks where a call without a
            # reason ends (_keep_one_reason).
            buckets["link"].append(
                {
                    "at": now,
                    "kind": "link",
                    "op": "add",
                    "type": link_type,
                    "target": target,
                    "reason": reason_text,
                    "via": via,
                }
            )
            reason_used = reason_used or bool(reason_text)
        if len(links[link_type]) > MAX_LINKS_PER_TYPE:
            return ChangeOutcome(
                error=_error(
                    "too_many_links",
                    f"{adr}: {link_type} may hold at most {MAX_LINKS_PER_TYPE} ids",
                )
            )
    successors_changed = links["superseded_by"] != links_before["superseded_by"]

    # 3. lifecycle
    target = change.lifecycle
    lifecycle_after = lifecycle_before
    lifecycle_changed = target is not None and target != lifecycle_before
    if lifecycle_changed:
        if not transition_allowed(lifecycle_before, target):
            return ChangeOutcome(
                error=_error(
                    "transition_not_allowed",
                    f"{adr}: {lifecycle_before} cannot move to {target}",
                    allowed_to=allowed_to(lifecycle_before),
                )
            )
        if not reason_text:
            return ChangeOutcome(
                error=_error("reason_required", "reason is required to change the lifecycle")
            )
        lifecycle_after = target

    # Invariants 1 and 2, only for what this call changed.
    if lifecycle_changed or successors_changed:
        if lifecycle_after == DecisionLifecycle.SUPERSEDED and not links["superseded_by"]:
            return ChangeOutcome(
                error=_error(
                    "superseded_by_required",
                    f"{adr}: superseded needs the replacing ADR in add_links superseded_by",
                )
            )
        if links["superseded_by"] and lifecycle_after not in (
            DecisionLifecycle.SUPERSEDED,
            DecisionLifecycle.REVERTED,
        ):
            return ChangeOutcome(
                error=_error(
                    "superseded_by_not_allowed",
                    f"{adr}: superseded_by is only kept while the lifecycle is superseded or "
                    "reverted; remove it with remove_links in the same call",
                )
            )

    projected = project_status(lifecycle_after, links["superseded_by"])
    if pre_mismatch and target is None:
        new_status: DecisionStatus | str = status_before
        lineage_projection = project_status(lifecycle_before, links_before["superseded_by"])
        notices.append(
            notice(
                "warning",
                "decision_status_mismatch",
                f"{adr}: decisions.yaml says status={status_label}, but the lineage lifecycle "
                f"is {lifecycle_before} (status {_status_text(lineage_projection)}). This "
                "call left the status as it was.",
                f"Ask the user which one is right. To follow the lineage, call "
                f"pm_update_decision again with lifecycle={lifecycle_before}. To follow the "
                f"status, move the lifecycle to the matching value (allowed from "
                f"{lifecycle_before}: {', '.join(allowed_to(lifecycle_before)) or 'none'}).",
            )
        )
    else:
        new_status = projected
        if pre_mismatch:
            notices.append(
                notice(
                    "warning",
                    "decision_status_mismatch_resolved",
                    f"{adr}: decisions.yaml said status={status_label}, which disagreed with "
                    f"the lineage; it now says {_status_text(projected)}, projected from "
                    f"lifecycle {lifecycle_after}.",
                )
            )
    status_changed = new_status != status_before
    new_status_value = _status_text(new_status)

    if lifecycle_changed:
        changes["lifecycle"] = {"from": lifecycle_before, "to": lifecycle_after}
        buckets["lifecycle"].append(
            {
                "at": now,
                "kind": "lifecycle",
                "from": lifecycle_before,
                "to": lifecycle_after,
                "status": new_status_value,
                "reason": reason_text,
                "via": via,
            }
        )
        reason_used = True
    elif status_changed:
        buckets["status_reprojected"].append(
            {
                "at": now,
                "kind": "status_reprojected",
                "from_status": status_label,
                "to_status": new_status_value,
                "via": via,
            }
        )
    if status_changed:
        changes["decision_status"] = {"from": status_label, "to": new_status_value}

    # 4-5. evaluation and note
    redactions = reason_hits if reason_used else 0
    if _given(change.evaluation):
        text, hits = scrub_text(change.evaluation)
        redactions += hits
        buckets["evaluation"].append(
            {
                "at": now,
                "kind": "evaluation",
                "evaluation_kind": change.evaluation_kind,
                "text": text,
                "via": via,
            }
        )
    if _given(change.note):
        text, hits = scrub_text(change.note)
        redactions += hits
        buckets["note"].append({"at": now, "kind": "note", "text": text, "via": via})

    new_events = [event for kind in EVENT_KINDS for event in buckets[kind]]
    _keep_one_reason(new_events)
    changed = bool(new_events)
    outcome = ChangeOutcome(
        changed=changed,
        status_before=status_label,
        new_status=new_status_value,
        lifecycle=lifecycle_after if changed else lifecycle_before,
        links=links if changed else links_before,
        notices=notices,
        added_links=added if changed else {},
    )
    if not changed:
        return outcome

    new_doc = dict(doc)
    if anchor_missing:
        new_doc = _with_anchor(new_doc, decision)
        notices.append(
            notice(
                "info",
                LINEAGE_ANCHOR_MISSING,
                f"{adr}: the lineage had no anchor; it is now tied to the ADR's current date "
                "and title.",
            )
        )
    new_doc["lifecycle"] = lifecycle_after
    new_doc["links"] = {**stored_links, **links}
    if "declared" in changes:
        new_doc["declared"] = declared
    new_doc["events"] = list(doc.get("events") or []) + new_events

    events_added = (["lineage_started"] if started else []) + [e["kind"] for e in new_events]
    if started:
        notices.append(
            notice(
                "info",
                "decision_lineage_started",
                f"{adr} had no lineage; one was started from its status. When it was recorded "
                "and who decided it are not recorded.",
            )
        )
    if lifecycle_changed:
        # Every move, not only to adopted / rejected: leaving proposed takes
        # the ADR off the list waiting for the user's review, and leaving
        # adopted withdraws a decision, so each is a change the user must see.
        notices.append(
            notice(
                "info",
                "decision_lifecycle_changed",
                f"{adr} is now {lifecycle_after} (was {lifecycle_before}). pmlens cannot "
                "confirm that the user made this decision; tell the user about this change.",
            )
        )
    if lifecycle_after == DecisionLifecycle.SUPERSEDED and not links["superseded_by"]:
        notices.append(
            notice(
                "info",
                LINEAGE_NO_SUCCESSOR,
                f"{adr} stays superseded with no superseded_by; only the other changes were "
                "recorded.",
            )
        )
    if redactions:
        notices.append(
            notice(
                "warning",
                "decision_lineage_secrets_redacted",
                f"{redactions} secret-like string(s) were removed from reason / note / "
                "evaluation before saving.",
                "If a real credential was pasted, revoke it.",
            )
        )
    outcome.doc = new_doc
    outcome.events_added = events_added
    outcome.changes = changes
    return outcome
