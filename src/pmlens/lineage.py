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
:func:`apply_change`). Its bounded reader is also the one policy.py reads
``.pm/decision_policy.yaml`` through (:func:`read_bounded_yaml`); ``os.open``
appears only in :func:`_read_bounded`.
``storage`` imports this module and owns every lock and write; this module does
not import ``storage`` (tests/test_lineage.py pins both with an AST check).

Reads are lenient (ADR-056): a lineage file that is missing, unreadable,
malformed or written for another ADR is reported through notes, and the ADR is
shown as *derived* from its status. No note, warning or error built here quotes
an exception's text: ``str(exc)`` carries the offending YAML line or pydantic's
``input_value`` and can leak a secret (:func:`error_summary`).

S2 adds the ADR's fact core: its facts, versioned, with the version an
adoption fixed (:func:`fact_core_refusal`, :func:`adopted_fact_core_version`,
:func:`explanation_staleness`, and the ``fact_core`` argument of
:func:`apply_change`), explanations written from it
(:func:`add_explanation_doc`) and what the user said about the ADR
(:func:`add_feedback_doc`, and the ``caused_by`` argument of
:func:`apply_change`).

"design §N" below refers to docs/issues/DESIGN_decision-lineage-s1.md, "S2
spec §N" to docs/issues/DESIGN_decision-lineage-s2.md.
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

from .ledger_errors import LedgerReadError
from .models import (
    ConfidenceLevel,
    Decision,
    DecisionKind,
    DecisionLifecycle,
    DecisionOrigin,
    DecisionStatus,
    EvaluationKind,
    FactSource,
    FeedbackKind,
    PmServerError,
    RecordedTiming,
    RespondentScope,
    Reversibility,
)
from .redaction import redact_secrets
from .safe_yaml import UnreadableValueError

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
LINEAGE_STARTED = "decision_lineage_started"
LINEAGE_SECRETS_REDACTED = "decision_lineage_secrets_redacted"

# Fact core ids (S2 spec §2.2). ``re.fullmatch`` only, as for DECISION_ID_RE.
# Numbers run 1-99 and are never reused, retired ones included.
FACT_ID_RE = re.compile(
    r"F-(?:what|change|revert|weak|uncertain)|F-(?:alt|conditions|premise)\.[1-9][0-9]?"
)
# Explanation ids (S2 spec §2.4). An adoption reads the explanations section
# only to see whether one of the current version exists (S2 spec §3.1).
EXPLANATION_ID_RE = re.compile(r"ADR-[0-9]{1,6}\.EX-[1-9][0-9]{0,5}")
# Feedback ids (S2 spec §2.4). The fact core writer reads them only to tell a
# readable feedback entry from one readers skip (S2 spec §2.7).
FEEDBACK_ID_RE = re.compile(r"ADR-[0-9]{1,6}\.FB-[1-9][0-9]{0,5}")
FACT_SINGLE_KINDS: tuple[str, ...] = ("what", "change", "revert", "weak", "uncertain")
FACT_NUMBERED_KINDS: tuple[str, ...] = ("alt", "conditions", "premise")
FACT_KINDS: tuple[str, ...] = FACT_SINGLE_KINDS + FACT_NUMBERED_KINDS
# Every version from 1 on holds these; premise_dependent ADRs also need a
# premise (S2 spec §2.2, Q6).
REQUIRED_FACT_IDS: tuple[str, ...] = tuple(f"F-{kind}" for kind in FACT_SINGLE_KINDS)
MAX_FACT_NUMBER = 99
MAX_FACT_ITEMS = 30
MAX_FACT_TEXT_CHARS = 500
MAX_FACT_CORE_CHARS = 6_000
# The keys a fact_core argument item may carry. ``source`` is not one: pmlens
# writes proposal for every item, so no caller can mark a fact as observed.
FACT_INPUT_KEYS = frozenset({"id", "kind", "text", "confidence", "reversibility"})
REVERSIBILITIES: tuple[str, ...] = tuple(v.value for v in Reversibility)
CONFIDENCES: tuple[str, ...] = tuple(v.value for v in ConfidenceLevel)
CODE_REVERTABLE = Reversibility.CODE_REVERTABLE.value
SOURCE_PROPOSAL = FactSource.PROPOSAL.value
# How a stored reversibility that is not a vocabulary value is named in a
# message or an error field: values written by hand are never quoted.
REVERSIBILITY_OUT_OF_VOCABULARY = "out_of_vocabulary"
REVERSIBILITY_UNREADABLE = "unreadable"
# While an ADR adopted with a recorded fact core version is in one of these,
# its fact core is fixed; only a move back to proposed lifts that (Q13).
FACT_CORE_FIXED_LIFECYCLES = frozenset(
    {
        DecisionLifecycle.ADOPTED.value,
        DecisionLifecycle.DEPRECATED.value,
        DecisionLifecycle.SUPERSEDED.value,
        DecisionLifecycle.REVERTED.value,
    }
)
# Whether the lifecycle went through an adoption, read from the events
# (:func:`adopted_fact_core_version`), never from the lifecycle value alone.
ADOPTION_ADOPTED = "adopted"
ADOPTION_NOT_ADOPTED = "not_adopted"
ADOPTION_UNKNOWN = "unknown"
# Caps of the S2 view (S2 spec §2.6): the writer's limits, or above what a
# writer can put in a file of MAX_LINEAGE_BYTES.
VIEW_RETIRED_IDS = len(FACT_NUMBERED_KINDS) * MAX_FACT_NUMBER
VIEW_PREVIOUS_VALUES = 30
SCAN_PREVIOUS_VALUES = 5_000
MAX_CHANGED_IDS = len(FACT_SINGLE_KINDS) + VIEW_RETIRED_IDS
PREVIOUS_TEXT_CHARS = 200
# Explanations are scanned from the end up to this many entries (S2 spec §2.6).
SCAN_EXPLANATIONS = 1_000
# The questions an explanation may end with (S2 spec §2.9). The brief's first
# question, which kinds the reader can judge, is not one of them: it is never
# saved, so passing it is refused. decision_brief.py imports this tuple.
QUESTION_KINDS: tuple[str, ...] = (
    "judgment",
    "premise_check",
    "understanding_check",
    "tech_review",
)
# The writer's limits of one explanation (S2 spec §2.3, §2.8); the view applies
# them again to what a hand-written file holds (S2 spec §2.6).
MAX_PARAGRAPHS = 40
MAX_PARAGRAPH_CHARS = 2_000
MAX_EXPLANATION_CHARS = 12_000
MAX_QUESTION_CHARS = 500
MAX_PARAGRAPH_FACT_IDS = 2
# Explanation numbers stay within EXPLANATION_ID_RE (S2 spec §2.4).
MAX_ENTRY_NUMBER = 999_999
VIEW_EXPLANATIONS = 20
# An explanation's accuracy is never checked; every reader shows this (S2 spec §2.3).
ACCURACY_UNCONFIRMED = "unconfirmed"
EXPLANATION_VIA = "pm_add_explanation"
# A decision_explanations_stale notice lists at most this many ids.
_LISTED_STALE_IDS = 10
_EXPLANATIONS_KEYS = frozenset({"last_number", "items"})
_EXPLANATION_KEYS = frozenset(
    {"id", "at", "via", "fact_core_version", "fact_ids_present", "paragraphs", "question"}
)
_PARAGRAPH_KEYS = frozenset({"fact_ids", "text"})
_QUESTION_KEYS = frozenset({"question_kind", "fact_ids", "text"})
# Feedback (S2 spec §2.4, §2.9): what the user said, recorded as said and
# never scored.
FEEDBACK_KINDS: tuple[str, ...] = tuple(v.value for v in FeedbackKind)
RESPONDENT_SCOPES: tuple[str, ...] = tuple(v.value for v in RespondentScope)
CAN_JUDGE = RespondentScope.CAN_JUDGE.value
CANNOT_JUDGE = RespondentScope.CANNOT_JUDGE.value
# The replies that answer for the decision: they carry a review notice and are
# counted in declared_respondent_scope (S2 spec §2.4, §3.4).
JUDGED_FEEDBACK_KINDS = frozenset(
    {FeedbackKind.APPROVE.value, FeedbackKind.REJECT.value, FeedbackKind.CHECK_ANSWER.value}
)
# Approval and rejection recorded as feedback never change the lifecycle.
LIFECYCLE_FEEDBACK_KINDS = frozenset({FeedbackKind.APPROVE.value, FeedbackKind.REJECT.value})
MAX_FEEDBACK_FACT_IDS = 10
# Feedback is scanned from the end up to this many entries (S2 spec §2.6).
SCAN_FEEDBACK = 1_000
VIEW_FEEDBACK = 20
VIEW_FEEDBACK_TEXT_CHARS = 500
FEEDBACK_VIA = "pm_add_feedback"
# Labels derived from a declaration, for display only (S2 spec §2.4, §2.9).
# Neither says a review took place; pmlens never checks the declaration.
REVIEW_DECLARED_CAN_JUDGE = "respondent_declared_can_judge"
REVIEW_NONE = "no_review_by_able_person"
SCOPE_LABEL_NONE = "none"
SCOPE_LABEL_MIXED = "mixed"
_FEEDBACK_SECTION_KEYS = frozenset({"last_number", "items"})
_FEEDBACK_KEYS = frozenset(
    {
        "id",
        "at",
        "via",
        "feedback_kind",
        "text",
        "fact_core_version",
        "explanation_id",
        "fact_ids",
        "respondent_scope",
        "question_kind",
    }
)
# caused_by: feedback ids of the same ADR, at most this many per call (S2 spec §2.8).
MAX_CAUSED_BY = 10
# The keys each level of the fact core section may hold (S2 spec §2.1); others
# are shown by name only, as ``unknown_fields`` (S2 spec §2.6).
_FACT_CORE_KEYS = frozenset(
    {"version", "recorded_reversibility", "items", "retired_ids", "previous_values"}
)
_FACT_ITEM_KEYS = frozenset({"id", "text", "source", "since", "confidence", "reversibility"})
_PREVIOUS_VALUE_KEYS = frozenset(
    {"id", "last_version", "text", "source", "confidence", "reversibility"}
)

# Fact core codes (S2 spec §4).
FACT_CORE_AFTER_ADOPTION = "fact_core_after_adoption"
FACT_CORE_VERSION_MISMATCH = "fact_core_version_mismatch"
FACT_CORE_LOWERING_REFUSED = "fact_core_reversibility_lowering_refused"
FACT_CORE_CHANGED_AFTER_ADOPTION = "decision_fact_core_changed_after_adoption"
FACT_CORE_WRITTEN_AFTER_ADOPTION = "decision_fact_core_written_after_adoption"
FACT_CORE_CHANGED_SINCE_ADOPTION = "decision_fact_core_changed_since_adoption"
FACT_CORE_NO_CONDITIONS = "decision_fact_core_no_conditions"
REVERSIBILITY_CHANGED_OUTSIDE_TOOLS = "decision_reversibility_changed_outside_tools"
REVERSIBILITY_DECLARED_AFTER_REVIEW = "decision_reversibility_declared_after_review"
ADOPTION_EXPLANATION_CHECK_SKIPPED = "decision_adoption_explanation_check_skipped"
ADOPTED_WITHOUT_CURRENT_EXPLANATION = "decision_adopted_without_current_explanation"
# Explanation codes (S2 spec §4).
DECISION_FACT_CORE_MISSING = "decision_fact_core_missing"
EXPLANATIONS_STALE = "decision_explanations_stale"
EXPLANATION_FACT_IDS_MISSING = "explanation_fact_ids_missing"
EXPLANATION_FACT_IDS_UNKNOWN = "explanation_fact_ids_unknown"
EXPLANATION_TOO_LARGE = "explanation_too_large"
EXPLANATION_IDS_EXHAUSTED = "explanation_ids_exhausted"
# Feedback and caused_by codes (S2 spec §4).
FEEDBACK_DOES_NOT_CHANGE_LIFECYCLE = "decision_feedback_does_not_change_lifecycle"
NO_REVIEW_BY_ABLE_PERSON = "decision_no_review_by_able_person"
RESPONDENT_DECLARED_CAN_JUDGE = "decision_respondent_declared_can_judge"
EXPLANATION_NOT_FOUND = "explanation_not_found"
CHECK_ANSWER_WITHOUT_QUESTION = "check_answer_without_question"
FEEDBACK_FACT_IDS_UNKNOWN = "feedback_fact_ids_unknown"
FEEDBACK_IDS_EXHAUSTED = "feedback_ids_exhausted"
INVALID_CAUSED_BY = "invalid_caused_by"
CAUSED_BY_NOT_FOUND = "caused_by_not_found"

# Fields each event kind may show, in this order (design §2.4, S2 spec §2.5).
# Multi-event calls also append their events in this order. ``at`` / ``kind`` /
# ``via`` are common to every kind.
_EVENT_FIELDS: dict[str, tuple[str, ...]] = {
    "created": ("lifecycle", "status"),
    "lineage_started": ("basis", "status", "lifecycle"),
    "lifecycle": ("from", "to", "status", "reason", "fact_core_version"),
    "link": ("op", "type", "target", "reason"),
    "declared": ("field", "value", "basis", "reason"),
    "fact_core": ("version", "changed_ids", "caused_by", "reason", "edited_outside_tools"),
    "evaluation": ("evaluation_kind", "text", "caused_by"),
    "note": ("text",),
    "status_reprojected": ("from_status", "to_status"),
}
EVENT_KINDS: tuple[str, ...] = tuple(_EVENT_FIELDS)
_EVENT_COMMON: tuple[str, ...] = ("at", "kind", "via")
_FREE_TEXT_FIELDS = frozenset({"text", "reason"})
# Allow-listed event fields that are not strings (S2 spec §2.5): an int (bool
# excluded), an int of 1 or more, or a list of ids each matching a pattern, read
# up to a cap (S2 spec §2.6).
_EVENT_INT_FIELDS = frozenset({"version"})
_EVENT_POSITIVE_INT_FIELDS = frozenset({"fact_core_version"})
_EVENT_ID_LIST_FIELDS: dict[str, tuple[re.Pattern[str], int]] = {
    "changed_ids": (FACT_ID_RE, MAX_CHANGED_IDS),
    "edited_outside_tools": (FACT_ID_RE, MAX_CHANGED_IDS),
    "caused_by": (FEEDBACK_ID_RE, MAX_CAUSED_BY),
}
# One S2 section joins at a time, with the task that shows it (S2 spec §2.6).
_KNOWN_TOP_LEVEL = frozenset(
    {
        "schema",
        "decision_id",
        "anchor",
        "recorded_at",
        "declared",
        "lifecycle",
        "links",
        "events",
        "fact_core",
        "explanations",
        "feedback",
    }
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
    """Describe an exception without quoting its text.

    ``str(exc)`` quotes the offending YAML line or pydantic's ``input_value``,
    which can be a secret, so only the type name is used. A LedgerReadError
    (raised by the ledger loaders) carries a content-free ``summary``, which is
    returned as is. For a YAML error, possibly wrapped with the YAMLError as
    ``__cause__``, the line and column of ``problem_mark`` are added; they are
    positions, not content.

    Args:
        exc: The exception to describe.

    Returns:
        e.g. ``"ScannerError at line 3, column 9"``,
        ``"ValidationError in record 2 (ADR-002): title (string_type)"`` or
        ``"ValidationError"``.
    """
    if isinstance(exc, LedgerReadError):
        return exc.summary
    yaml_exc: BaseException | None = None
    if isinstance(exc, yaml.YAMLError):
        yaml_exc = exc
    elif isinstance(exc.__cause__, yaml.YAMLError):
        yaml_exc = exc.__cause__
    if yaml_exc is None:
        return type(exc).__name__
    if isinstance(yaml_exc, UnreadableValueError):
        return str(yaml_exc)  # names only the constructor's exception type
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
    """The path is not a regular file of at most the reader's size limit."""


def _read_bounded(path: Path, limit: int = MAX_LINEAGE_BYTES) -> bytes:
    """Read a regular file of at most ``limit`` bytes, without following links.

    ``O_NOFOLLOW`` refuses a symlink and ``O_NONBLOCK`` keeps a FIFO from
    blocking the open; the checks run on the opened descriptor, so swapping the
    path between a check and the open cannot slip past them. This is the one
    ``os.open`` of the module (tests/test_lineage.py pins it); other bounded
    readers go through :func:`read_bounded_yaml`.

    Args:
        path: The file.
        limit: The largest size accepted, in bytes (MAX_LINEAGE_BYTES for a
            lineage; decision_policy.yaml has its own, S2 spec §2.10).

    Raises:
        FileNotFoundError: The file (or its directory) does not exist.
        OSError: The file could not be opened or read (a symlink included).
        _NotRegularOrTooLargeError: Not a regular file, or larger than ``limit``.
    """
    flags = (
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_BINARY", 0)
    )
    fd = os.open(path, flags)
    chunks: list[bytes] = []
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise _NotRegularOrTooLargeError("not a regular file")
        if info.st_size > limit:
            raise _NotRegularOrTooLargeError("larger than the size limit")
        remaining = limit + 1
        while remaining > 0:
            chunk = os.read(fd, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
    finally:
        os.close(fd)
    payload = b"".join(chunks)
    if len(payload) > limit:
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
class BoundedYaml:
    """What :func:`read_bounded_yaml` found at one path.

    Attributes:
        exists: Whether something is at the path.
        data: The ``safe_load`` result when it could be read and parsed.
        unreadable: Whether something is there but could not be read, parsed
            or kept (see :func:`read_bounded_yaml`).
        detail: A content-free reason when it is unreadable (exception type,
            YAML line and column).
        size: How many bytes were read (0 when nothing was read).
    """

    exists: bool
    data: object = None
    unreadable: bool = False
    detail: str | None = None
    size: int = 0


def read_bounded_text(path: Path, limit: int) -> str | None:
    """Read one small UTF-8 text file without following links or raising.

    For a hint taken from a file that is not pmlens's own (a project's
    ``.gitignore``). A FIFO does not block the open.

    Args:
        path: The file.
        limit: The largest size accepted, in bytes.

    Returns:
        The text, or None when nothing is there or it cannot be used: a
        symlink, anything but a regular file, a file over ``limit`` bytes, an
        OS error, or bytes that are not UTF-8.
    """
    try:
        return _read_bounded(path, limit).decode("utf-8")
    except (OSError, _NotRegularOrTooLargeError, UnicodeDecodeError):
        return None


def read_bounded_yaml(path: Path, limit: int) -> BoundedYaml:
    """Read and parse one YAML file without locking, creating, following links or raising.

    The bounded reader every ledger read outside storage goes through (the
    lineage files and .pm/decision_policy.yaml, S2 spec §2.10): a symlink,
    anything but a regular file, a file over ``limit`` bytes, an OS error,
    invalid UTF-8, invalid YAML and lists or mappings nested more than
    MAX_LINEAGE_DEPTH levels deep all come back as ``unreadable`` with a
    reason that quotes nothing from the file (:func:`error_summary`). A FIFO
    does not block the open.

    Args:
        path: The file; its directory is the caller's to check.
        limit: The largest size accepted, in bytes.

    Returns:
        What was found; ``exists`` is False when nothing is at ``path``.
    """
    try:
        payload = _read_bounded(path, limit)
    except FileNotFoundError:
        return BoundedYaml(exists=False)
    except _NotRegularOrTooLargeError as exc:
        return BoundedYaml(exists=True, unreadable=True, detail=str(exc))
    except OSError as exc:
        return BoundedYaml(exists=True, unreadable=True, detail=error_summary(exc))
    size = len(payload)
    try:
        data = yaml.safe_load(payload.decode("utf-8"))
    except Exception as exc:  # noqa: BLE001 - a read never raises; the type is reported
        return BoundedYaml(exists=True, unreadable=True, detail=error_summary(exc), size=size)
    if _nested_deeper_than(data, MAX_LINEAGE_DEPTH):
        return BoundedYaml(
            exists=True,
            unreadable=True,
            detail=f"nested more than {MAX_LINEAGE_DEPTH} levels deep",
            size=size,
        )
    return BoundedYaml(exists=True, data=data, size=size)


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
    found = read_bounded_yaml(directory / f"{decision_id}.yaml", MAX_LINEAGE_BYTES)
    if not found.exists:
        return RawLineage(decision_id, exists=False)
    if found.unreadable:
        return RawLineage(
            decision_id,
            exists=True,
            error=LINEAGE_UNREADABLE,
            detail=found.detail,
            size=found.size,
        )
    return RawLineage(decision_id, exists=True, data=found.data, size=found.size)


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


# ─── Fact core (pure; S2 spec §2.2) ──────────────────

# What a stored field holds when it is not there at all (``None`` is a value).
_MISSING: object = object()


def _is_positive_int(value: object) -> bool:
    """True for an int of 1 or more; a bool (an int subclass) is not one."""
    return isinstance(value, int) and not isinstance(value, bool) and value >= 1


def is_fact_id(value: object) -> bool:
    """True when ``value`` is a string matching FACT_ID_RE whole."""
    return isinstance(value, str) and FACT_ID_RE.fullmatch(value) is not None


def fact_kind(fact_id: str) -> str:
    """The kind of a fact id (``F-premise.2`` -> ``premise``, ``F-what`` -> ``what``)."""
    return fact_id[2:].split(".", 1)[0]


def _fact_number(fact_id: str) -> int | None:
    """The number of a numbered fact id; ``None`` for one of the single items.

    FACT_ID_RE allows ASCII digits only, so ``int`` cannot fail.
    """
    _, dot, number = fact_id.partition(".")
    return int(number) if dot else None


def _path_of(adr: str) -> str:
    """The lineage file of ``adr`` as a remediation names it."""
    return f".pm/{LINEAGE_DIR}/{adr}.yaml"


def _fact_item_problem(item: object, seen: set[str]) -> str | None:
    """Why a stored fact core item cannot be used, or ``None`` (S2 spec §2.7).

    Readers skip such an item; the fact core writer refuses the whole file
    (:func:`fact_core_refusal`). A value outside a vocabulary is not a problem:
    it is shown redacted and copied redacted. The reason names fields, never
    values.

    Args:
        item: One entry of ``fact_core.items``.
        seen: The fact ids held by the entries before it, usable or not: an
            id repeated after a broken entry is still a repeat (S2 spec §2.7).
    """
    if not isinstance(item, dict):
        return "is not a mapping"
    fact_id = item.get("id")
    if not is_fact_id(fact_id):
        return "has no valid fact id"
    if fact_id in seen:
        return "repeats an id used earlier"
    if not isinstance(item.get("text"), str):
        return "has no text string"
    for name in ("source", "confidence", "reversibility"):
        if name in item and not isinstance(item[name], str):
            return f"has a {name} that is not a string"
    if fact_id == "F-revert" and "reversibility" not in item:
        return "has no reversibility"
    if fact_id == "F-uncertain" and "confidence" not in item:
        return "has no confidence"
    return None


def _previous_value_problem(element: object) -> str | None:
    """Why an entry of ``fact_core.previous_values`` cannot be used, or ``None``."""
    if not isinstance(element, dict):
        return "is not a mapping"
    if not is_fact_id(element.get("id")):
        return "has no valid fact id"
    if not _is_positive_int(element.get("last_version")):
        return "has no last_version of 1 or more"
    if not isinstance(element.get("text"), str):
        return "has no text string"
    for name in ("source", "confidence", "reversibility"):
        if name in element and not isinstance(element[name], str):
            return f"has a {name} that is not a string"
    return None


def _readable_fact_core(raw: object) -> dict | None:
    """``raw`` when readers can show it as a fact core, else ``None`` (S2 spec §2.7).

    A mapping whose ``version`` is an int of 1 or more and whose ``items`` is a
    list. Its items are then taken one by one (:func:`_readable_items`).
    """
    if (
        isinstance(raw, dict)
        and _is_positive_int(raw.get("version"))
        and isinstance(raw.get("items"), list)
    ):
        return raw
    return None


def _readable_items(items: list) -> tuple[list[dict], int]:
    """The usable items among the first MAX_FACT_ITEMS entries, and how many were not.

    Entries past the cap are counted without being read, so an alias-inflated
    list costs no more than the cap.
    """
    seen: set[str] = set()
    kept: list[dict] = []
    skipped = max(0, len(items) - MAX_FACT_ITEMS)
    for item in items[:MAX_FACT_ITEMS]:
        problem = _fact_item_problem(item, seen)
        if isinstance(item, dict) and is_fact_id(item.get("id")):
            seen.add(item["id"])
        if problem is not None:
            skipped += 1
            continue
        kept.append(item)
    return kept, skipped


def _revert_value(items: Iterable[dict]) -> object:
    """F-revert's stored reversibility among usable items (``_MISSING`` without one)."""
    for item in items:
        if item["id"] == "F-revert":
            return item["reversibility"]
    return _MISSING


def _current_revert_value(raw_items: list, usable: list[dict]) -> object:
    """F-revert's stored reversibility, as a change outside the tools is judged by it.

    The judgement compares the reversibility itself (S2 spec §2.2), so an
    F-revert skipped for another field (its text, source or confidence) still
    gives the value it holds. Only a missing F-revert, or a reversibility that
    is missing or not a string, reads as unreadable. The floor still treats a
    skipped F-revert as absent (S2 spec §2.7).

    Args:
        raw_items: ``fact_core.items`` as loaded; only the first MAX_FACT_ITEMS
            entries are looked at, as :func:`_readable_items` does.
        usable: The usable items among them.
    """
    value = _revert_value(usable)
    if value is not _MISSING:
        return value
    for item in raw_items[:MAX_FACT_ITEMS]:
        if isinstance(item, dict) and item.get("id") == "F-revert":
            return item.get("reversibility", _MISSING)
    return _MISSING


def reversibility_label(value: object) -> str:
    """A stored reversibility as a vocabulary name, never quoting anything else.

    Returns:
        The value itself when it is in the vocabulary, ``out_of_vocabulary``
        for another string, ``unreadable`` when missing or not a string.
    """
    if isinstance(value, str):
        return value if value in REVERSIBILITIES else REVERSIBILITY_OUT_OF_VOCABULARY
    return REVERSIBILITY_UNREADABLE


def floor_reversibility(fact_core: object) -> str:
    """The reversibility the floor and the ratchet read from a stored fact core.

    Only a stored ``code_revertable`` reads as code_revertable. No readable
    fact core, no usable F-revert, a skipped F-revert entry (a broken one, or
    a repeat of a usable one: S2 spec §2.7), and a reversibility that is
    missing, not a string or outside the vocabulary all read as ``unknown``,
    which keeps a person able to judge in the loop (S2 spec §2.2, §3.5).

    Args:
        fact_core: The lineage's ``fact_core`` value, as loaded.

    Returns:
        One of the Reversibility values.
    """
    value = _floor_revert_value(fact_core)
    if isinstance(value, str) and value in REVERSIBILITIES:
        return value
    return Reversibility.UNKNOWN.value


def _floor_revert_value(fact_core: object) -> object:
    """The stored F-revert reversibility the floor reads, ``_MISSING`` when it sees none.

    The floor looks only at the first MAX_FACT_ITEMS entries, as the view
    does, and sees F-revert only when exactly one entry there carries the id
    and that entry is usable (S2 spec §2.7). The ratchet names its base from
    this same value, so a refusal never names an entry the floor did not read
    (S2 spec §2.2, §3.1 step 8).
    """
    readable = _readable_fact_core(fact_core)
    if readable is None:
        return _MISSING
    raw_items = readable["items"]
    revert_entries = sum(
        1
        for item in raw_items[:MAX_FACT_ITEMS]
        if isinstance(item, dict) and item.get("id") == "F-revert"
    )
    if revert_entries != 1:
        return _MISSING
    return _revert_value(_readable_items(raw_items)[0])


def lowers_reversibility(fact_core: object, new_value: object) -> bool:
    """Whether writing ``new_value`` would lower F-revert's reversibility (Q12).

    The ratchet: once a fact core exists (version 1 or more; only a missing
    one is version 0), F-revert may become code_revertable only by the user's
    own edit of the file. The base is the value stored now (a person's edit
    included), read by :func:`floor_reversibility`, not ``recorded_reversibility``.
    A first version is a declaration and is never lowering.

    Args:
        fact_core: The lineage's ``fact_core`` value, as loaded (``None`` when
            there is none).
        new_value: The reversibility the new version would hold.
    """
    return (
        fact_core is not None
        and new_value == CODE_REVERTABLE
        and floor_reversibility(fact_core) != CODE_REVERTABLE
    )


def _changed_outside_tools(recorded: object, current: object) -> bool:
    """Whether F-revert's stored value differs from what a tool last recorded.

    A ``recorded_reversibility`` that is missing or not a vocabulary string
    counts as a difference whose earlier value is unknown (S2 spec §2.2).
    """
    if not (isinstance(recorded, str) and recorded in REVERSIBILITIES):
        return True
    return not isinstance(current, str) or current != recorded


def _scan_adoption(doc: Mapping) -> tuple[int | None, dict | None, str]:
    """The latest event that set the lifecycle to adopted or proposed (S2 spec §2.2).

    Events are walked from the end in file order; created and lineage_started
    set their ``lifecycle``, a lifecycle event its ``to``.

    Returns:
        ``(index, event, adoption)``: the event's 0-based position and the
        event (both ``None`` when there is none), and ``adopted`` /
        ``not_adopted`` for an event setting adopted / proposed. Without
        such an event, ``not_adopted`` when the first event is created (an
        ADR recorded as superseded or deprecated), else ``unknown`` (a
        lineage started from a deprecated or superseded status: whether it
        was ever adopted is not known).
    """
    events = doc.get("events")
    if not isinstance(events, list):
        return None, None, ADOPTION_UNKNOWN
    for index in range(len(events) - 1, -1, -1):
        event = events[index]
        if not isinstance(event, dict):
            continue
        kind = event.get("kind")
        if kind in ("created", "lineage_started"):
            target = event.get("lifecycle")
        elif kind == "lifecycle":
            target = event.get("to")
        else:
            continue
        if target == DecisionLifecycle.ADOPTED:
            return index, event, ADOPTION_ADOPTED
        if target == DecisionLifecycle.PROPOSED:
            return index, event, ADOPTION_NOT_ADOPTED
    first = events[0] if events else None
    if isinstance(first, dict) and first.get("kind") == "created":
        return None, None, ADOPTION_NOT_ADOPTED
    return None, None, ADOPTION_UNKNOWN


def adopted_fact_core_version(doc: Mapping) -> tuple[int | None, bool, str]:
    """The fact core version the ADR's adoption fixed (S2 spec §2.2, Q13).

    Read from the latest event that set the lifecycle to adopted or proposed
    (:func:`_scan_adoption`): when it adopted and holds ``fact_core_version``,
    that value. The locked writer and the view share this function.

    Args:
        doc: An attributed lineage document.

    Returns:
        ``(value, malformed, adoption)``. ``value`` is the version when the
        adopting event holds an int of 1 or more that is not above the current
        version (a fact core removed by hand keeps it, so the ADR stays fixed);
        otherwise ``None``. ``malformed`` is True when the adopting event holds
        ``fact_core_version`` of another type or range. ``adoption`` is
        ``adopted``, ``not_adopted`` or ``unknown``.
    """
    _, event, adoption = _scan_adoption(doc)
    if event is None or adoption != ADOPTION_ADOPTED or "fact_core_version" not in event:
        return None, False, adoption
    value = event["fact_core_version"]
    current = _readable_fact_core(doc.get("fact_core"))
    if not _is_positive_int(value) or (current is not None and value > current["version"]):
        return None, True, adoption
    return value, False, adoption


def _fixed_by_adoption(doc: Mapping, adopted: int | None) -> bool:
    """Whether the fact core is fixed: adopted with a version, and not moved back."""
    lifecycle = doc.get("lifecycle")
    return (
        adopted is not None
        and isinstance(lifecycle, str)
        and lifecycle in FACT_CORE_FIXED_LIFECYCLES
    )


def fact_core_refusal(doc: Mapping, *, writing: str = "the fact core") -> dict | None:
    """Why the fact core writer must not write this lineage's fact core, or ``None``.

    One predicate for three places (S2 spec §2.2, §2.7, §3.1): the fact core
    writer refuses with it, an adoption records the fact core version only
    when it is ``None`` and a fact core exists, and the adoption's
    ``decision_adoption_explanation_check_skipped`` follows it. Refused: a
    ``fact_core`` that is not a mapping, a ``version`` that is not an int of 1
    or more, ``items`` that is not a list, any item :func:`_fact_item_problem`
    rejects, a missing single item, and ``retired_ids`` or ``previous_values``
    entries of the wrong shape, so a broken or non-string value never travels
    into a new version. A missing fact core is version 0, not a refusal, and a
    missing or unknown ``recorded_reversibility`` is taken in by the next
    version instead.

    Args:
        doc: A lineage document that passed :func:`write_refusal`.
        writing: What the refused writer would write, as its message names it
            (pm_add_explanation reads the fact core through the same predicate,
            S2 spec §2.7).

    Returns:
        A ``decision_lineage_unreadable`` error dict naming the section and the
        1-based entry (never a value), with a remediation addressed to the
        user; or ``None``.
    """
    adr = doc.get("decision_id") if is_decision_id(doc.get("decision_id")) else "this ADR"
    raw = doc.get("fact_core")
    if raw is None:
        return None
    path = _path_of(adr)

    def refuse(where: str, problem: str, remediation: str | None = None) -> dict:
        return _error(
            LINEAGE_UNREADABLE,
            f"{adr}: {where} in the lineage file {problem}, so {writing} cannot be "
            "written; nothing was written.",
            remediation=remediation
            or (
                f"Tell the user. Only the user may fix {where} in {path} by hand, when no "
                "assistant session is writing. Do not edit the lineage file yourself."
            ),
        )

    if not isinstance(raw, dict):
        return refuse("fact_core", "is not a mapping")
    if not _is_positive_int(raw.get("version")):
        return refuse("fact_core.version", "is not a whole number of 1 or more")
    items = raw.get("items")
    if not isinstance(items, list):
        return refuse("fact_core.items", "is not a list")
    seen: set[str] = set()
    for position, item in enumerate(items, 1):
        problem = _fact_item_problem(item, seen)
        if problem is None:
            seen.add(item["id"])
            continue
        remediation = None
        if isinstance(item, dict) and item.get("id") == "F-revert" and "reversibility" in problem:
            remediation = (
                "Tell the user. Only the user may fix the one reversibility line of F-revert "
                f"in {path} with code_revertable, external_irreversible or unknown, when no "
                "assistant session is writing; recorded_reversibility needs no change. Do not "
                "edit the lineage file yourself."
            )
        return refuse(f"entry {position} of fact_core.items", problem, remediation)
    missing = [fact_id for fact_id in REQUIRED_FACT_IDS if fact_id not in seen]
    if missing:
        return refuse("fact_core.items", f"has no {', '.join(missing)}")
    retired = raw.get("retired_ids")
    if retired is not None:
        if not isinstance(retired, list):
            return refuse("fact_core.retired_ids", "is not a list")
        for position, value in enumerate(retired, 1):
            if not is_fact_id(value):
                return refuse(f"entry {position} of fact_core.retired_ids", "is not a fact id")
    previous = raw.get("previous_values")
    if previous is not None:
        if not isinstance(previous, list):
            return refuse("fact_core.previous_values", "is not a list")
        for position, element in enumerate(previous, 1):
            problem = _previous_value_problem(element)
            if problem is not None:
                return refuse(f"entry {position} of fact_core.previous_values", problem)
    return None


def _fact_core_traces(doc: Mapping) -> str | None:
    """Where a lineage without a fact core still refers to one, or ``None`` (S2 spec §2.2).

    An explanation or feedback item recorded from a version of 1 or more, or
    a fact_core event of version 1 or more, means the fact core was removed by
    hand: a first version written over it would undo the ratchet. Only the
    readable entries of explanations and feedback are looked at, mappings whose
    id matches EXPLANATION_ID_RE / FEEDBACK_ID_RE (a broken section or entry is
    no reason to refuse here, S2 spec §2.7); every event is.

    Returns:
        ``explanations``, ``feedback`` or ``events``; ``None`` without traces.
    """
    for section, pattern in (("explanations", EXPLANATION_ID_RE), ("feedback", FEEDBACK_ID_RE)):
        part = doc.get(section)
        entries = part.get("items") if isinstance(part, dict) else None
        if isinstance(entries, list) and any(
            isinstance(entry, dict)
            and isinstance(entry.get("id"), str)
            and pattern.fullmatch(entry["id"]) is not None
            and _is_positive_int(entry.get("fact_core_version"))
            for entry in entries
        ):
            return section
    events = doc.get("events")
    if isinstance(events, list) and any(
        isinstance(event, dict)
        and event.get("kind") == "fact_core"
        and _is_positive_int(event.get("version"))
        for event in events
    ):
        return "events"
    return None


@dataclass(frozen=True)
class Staleness:
    """Whether an explanation is stale and which facts changed since (S2 spec §2.3, D6).

    Attributes:
        stale: The explanation was written from another version than the
            current one, or F-revert's reversibility was changed outside the
            tools since the current version was written.
        changed_ids: The ids whose value changed, appeared or was retired after
            the explanation's version (F-revert added on a change outside the
            tools), without repeats, at most MAX_CHANGED_IDS.
        changed_ids_truncated: ``previous_values`` was longer than
            SCAN_PREVIOUS_VALUES, so ids beyond the scan may be missing.
    """

    stale: bool
    changed_ids: list[str] = field(default_factory=list)
    changed_ids_truncated: bool = False


def explanation_staleness(fact_core: object, written_from: int) -> Staleness:
    """Decide whether an explanation written from ``written_from`` is stale; pure.

    Only the fact core section decides it, never the events (moving events
    out by hand changes nothing)::

        changed_ids = {item.id | item.since > written_from}
                    ∪ {pv.id | pv.last_version >= written_from}

    ``previous_values`` is read up to SCAN_PREVIOUS_VALUES entries, past the
    view's display cap, because retired ids alone can number more than that
    cap. An item whose ``since`` is not an int from 1 to the current version
    counts as changed in the current version.

    Args:
        fact_core: The lineage's ``fact_core`` value, as loaded. One readers
            cannot show counts as version 0.
        written_from: The explanation's ``fact_core_version``.

    Returns:
        The staleness and the changed ids.
    """
    readable = _readable_fact_core(fact_core)
    if readable is None:
        return Staleness(stale=written_from != 0)
    current: int = readable["version"]
    items, _ = _readable_items(readable["items"])
    ids: dict[str, None] = {}
    for item in items:
        since = item.get("since")
        if not _is_positive_int(since) or since > current:
            since = current
        if since > written_from:
            ids[item["id"]] = None
    truncated = False
    previous = readable.get("previous_values")
    if isinstance(previous, list):
        truncated = len(previous) > SCAN_PREVIOUS_VALUES
        for element in previous[:SCAN_PREVIOUS_VALUES]:
            if _previous_value_problem(element) is None and element["last_version"] >= written_from:
                ids[element["id"]] = None
    outside = _changed_outside_tools(
        readable.get("recorded_reversibility"), _current_revert_value(readable["items"], items)
    )
    if outside:
        ids["F-revert"] = None
    return Staleness(
        stale=written_from != current or outside,
        changed_ids=list(ids)[:MAX_CHANGED_IDS],
        changed_ids_truncated=truncated,
    )


# ─── Explanations (S2 spec §2.3) ─────────────────────


def _fact_ids_problem(value: object) -> str | None:
    """Why the ``fact_ids`` of a paragraph or a question are refused, or ``None``.

    One or two strings matching FACT_ID_RE, without repeats (S2 spec §2.3 checks
    2 and 6). The reason names the rule, never the value.
    """
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_PARAGRAPH_FACT_IDS:
        return f"must be a list of 1 to {MAX_PARAGRAPH_FACT_IDS} fact ids"
    if not all(is_fact_id(item) for item in value):
        return (
            "must hold only fact ids: F-what, F-change, F-revert, F-weak, F-uncertain or "
            "F-alt.N / F-conditions.N / F-premise.N"
        )
    if len(set(value)) != len(value):
        return "must not repeat an id"
    return None


def _mermaid_only(text: str) -> bool:
    """Whether a paragraph is only one mermaid code block (S2 spec §2.3 check 5).

    The text, without surrounding whitespace, opens with a ```` ```mermaid ````
    line and ends with the closing fence, with no other fence between. Such a
    paragraph does not count as leading with its first fact id.
    """
    body = text.strip()
    opening, newline, rest = body.partition("\n")
    return (
        opening.rstrip() == "```mermaid"
        and newline == "\n"
        and rest.endswith("```")
        and "```" not in rest[:-3]
    )


def _question_problem(question: object) -> str | None:
    """Why the question after an explanation is refused by its shape, or ``None``.

    The shape part of S2 spec §2.3 check 6: a mapping of question_kind,
    fact_ids and text only; a question kind of QUESTION_KINDS; 1 or 2 fact
    ids; a text that is not blank and at most MAX_QUESTION_CHARS long; and an
    F-premise id for premise_check. Whether its ids are current needs the
    fact core (:func:`explanation_refusal`).
    """
    if not isinstance(question, Mapping) or set(question) != _QUESTION_KEYS:
        return "must be a mapping holding only question_kind, fact_ids and text"
    kind = question["question_kind"]
    if not isinstance(kind, str) or kind not in QUESTION_KINDS:
        return (
            f"question_kind must be one of: {', '.join(QUESTION_KINDS)} (the question about "
            "which kinds the reader can judge is not part of an explanation)"
        )
    problem = _fact_ids_problem(question["fact_ids"])
    if problem is not None:
        return f"fact_ids {problem}"
    text = question["text"]
    if not isinstance(text, str) or not text.strip():
        return "text must be a non-empty string"
    if len(text) > MAX_QUESTION_CHARS:
        return f"text is longer than {MAX_QUESTION_CHARS} characters"
    if kind == "premise_check" and not any(
        fact_kind(fact_id) == "premise" for fact_id in question["fact_ids"]
    ):
        return "of kind premise_check needs an F-premise id in fact_ids"
    return None


def explanation_call_error(
    decision_id: object, fact_core_version: object, paragraphs: object, question: object
) -> dict | None:
    """Check a pm_add_explanation call by its arguments alone (S2 spec §3.2 step 1).

    In this order: the id's shape (``invalid_decision_id``), a version of 1
    or more (``invalid_fact_core_version``; a bool is not one), then
    :func:`explanation_argument_error`. Nothing is read and no message quotes
    a value passed, so the tool runs it before the project is resolved and
    the storage writer again before any file is read.

    Returns:
        An error dict, or ``None`` when the arguments are fine.
    """
    if not is_decision_id(decision_id):
        return _error("invalid_decision_id", "decision_id must look like ADR-NNN")
    if not _is_positive_int(fact_core_version):
        return _error(
            "invalid_fact_core_version", "fact_core_version must be a whole number of 1 or more"
        )
    return explanation_argument_error(paragraphs, question)


def explanation_argument_error(paragraphs: object, question: object) -> dict | None:
    """Check an explanation by its shape alone, before any file is read (S2 spec §3.2 step 1).

    Checks 1-3 of S2 spec §2.3, each over every paragraph in turn, then the
    shape part of check 6 on the question. Messages name positions (0-based,
    as ``paragraph_index``) and limits, never the text passed.

    Args:
        paragraphs: The paragraphs as passed, ``[{fact_ids, text}]``.
        question: The question as passed, or ``None``.

    Returns:
        An error dict, or ``None`` when the shape is fine.
    """
    if not isinstance(paragraphs, list) or not paragraphs:
        return _error(
            "invalid_paragraphs", "paragraphs must be a list of one or more {fact_ids, text} items"
        )
    # 1. The list and each paragraph's shape.
    if len(paragraphs) > MAX_PARAGRAPHS:
        return _error(
            EXPLANATION_TOO_LARGE, f"an explanation holds at most {MAX_PARAGRAPHS} paragraphs"
        )
    for index, paragraph in enumerate(paragraphs):
        if (
            not isinstance(paragraph, Mapping)
            or set(paragraph) != _PARAGRAPH_KEYS
            or not isinstance(paragraph["text"], str)
        ):
            return _error(
                "invalid_paragraphs",
                f"paragraph {index}: must be a mapping holding only fact_ids and a text string",
                paragraph_index=index,
            )
    # 2. Each paragraph's fact ids.
    for index, paragraph in enumerate(paragraphs):
        problem = _fact_ids_problem(paragraph["fact_ids"])
        if problem is not None:
            return _error(
                "invalid_fact_ids", f"paragraph {index}: fact_ids {problem}", paragraph_index=index
            )
    # 3. Each paragraph's text, then their total.
    total = 0
    for index, paragraph in enumerate(paragraphs):
        text: str = paragraph["text"]
        if not text.strip():
            return _error(
                "empty_paragraph", f"paragraph {index}: text is blank", paragraph_index=index
            )
        if len(text) > MAX_PARAGRAPH_CHARS:
            return _error(
                "text_too_long",
                f"paragraph {index}: text is longer than {MAX_PARAGRAPH_CHARS} characters",
                field="paragraphs",
                paragraph_index=index,
            )
        total += len(text)
    if total > MAX_EXPLANATION_CHARS:
        return _error(
            EXPLANATION_TOO_LARGE,
            f"the paragraphs' texts together are longer than {MAX_EXPLANATION_CHARS} characters",
        )
    # 6. The question's shape.
    if question is not None:
        problem = _question_problem(question)
        if problem is not None:
            return _error("invalid_question", f"question {problem}")
    return None


def _current_fact_ids(fact_core: object) -> list[str]:
    """The ids of the fact core's current version, in file order (none for version 0).

    Every usable item counts, not only the first MAX_FACT_ITEMS a view shows:
    an explanation covers the whole version (S2 spec §2.3 check 5).
    """
    readable = _readable_fact_core(fact_core)
    if readable is None:
        return []
    seen: set[str] = set()
    ids: list[str] = []
    for item in readable["items"]:
        if _fact_item_problem(item, seen) is None:
            ids.append(item["id"])
        if isinstance(item, dict) and is_fact_id(item.get("id")):
            seen.add(item["id"])
    return ids


def explanation_refusal(fact_core: object, paragraphs: object, question: object) -> dict | None:
    """Why an explanation may not be saved against ``fact_core``, or ``None``; pure (D4).

    S2 spec §2.3 checks 1-7, called under the lineage lock after the version
    matched. The shape comes first (:func:`explanation_argument_error`), then
    the coverage: every id must be one of the current version (check 4, which
    refuses retired ids too) and every current id must be the first id (the
    main id) of a paragraph that is not only a mermaid block (check 5). Both
    lists are computed and returned together (check 7); the code says
    ``missing`` when anything is missing. Last, the question's ids must be
    current (check 6). pmlens never reads ids out of the text, and nothing
    here judges whether the explanation is accurate.

    Args:
        fact_core: The lineage's ``fact_core`` value, as loaded.
        paragraphs: The paragraphs as passed.
        question: The question as passed, or ``None``.

    Returns:
        An error dict (``missing_fact_ids`` and ``unknown_fact_ids`` on a
        coverage failure), or ``None``.
    """
    shape = explanation_argument_error(paragraphs, question)
    if shape is not None:
        return shape
    current = _current_fact_ids(fact_core)
    known = set(current)
    unknown: dict[str, None] = {}
    leading: set[str] = set()
    for paragraph in paragraphs:
        for fact_id in paragraph["fact_ids"]:
            if fact_id not in known:
                unknown[fact_id] = None
        if not _mermaid_only(paragraph["text"]):
            leading.add(paragraph["fact_ids"][0])
    missing = [fact_id for fact_id in current if fact_id not in leading]
    if missing or unknown:
        # The ids passed FACT_ID_RE above, so they can be named.
        parts = []
        if missing:
            parts.append(
                "Every fact needs a paragraph that leads with it; these have none: "
                f"{', '.join(missing)}."
            )
        if unknown:
            parts.append(f"These ids are not facts of the current version: {', '.join(unknown)}.")
        return _error(
            EXPLANATION_FACT_IDS_MISSING if missing else EXPLANATION_FACT_IDS_UNKNOWN,
            " ".join(parts),
            missing_fact_ids=missing,
            unknown_fact_ids=list(unknown),
        )
    if question is not None and not all(fact_id in known for fact_id in question["fact_ids"]):
        return _error("invalid_question", "question fact_ids must be facts of the current version")
    return None


def _explanation_entry_problem(entry: object) -> str | None:
    """Why an entry of ``explanations.items`` cannot be read, or ``None`` (S2 spec §2.7).

    Not a mapping, no id matching EXPLANATION_ID_RE, or no ``fact_core_version``
    of 0 or more (a bool is not one). Readers skip such an entry; the
    adoption's check and pm_add_explanation treat the section as broken. The
    reason names fields, never values.
    """
    if not isinstance(entry, dict):
        return "is not a mapping"
    entry_id = entry.get("id")
    if not (isinstance(entry_id, str) and EXPLANATION_ID_RE.fullmatch(entry_id)):
        return "has no valid explanation id"
    version = entry.get("fact_core_version")
    if not (isinstance(version, int) and not isinstance(version, bool) and version >= 0):
        return "has no fact_core_version of 0 or more"
    return None


def _entry_number(entry_id: str) -> int:
    """The number of an id matching EXPLANATION_ID_RE or FEEDBACK_ID_RE.

    Both end in ASCII digits after the last hyphen, so ``int`` cannot fail.
    """
    return int(entry_id.rpartition("-")[2])


def _explanations_refusal(
    doc: Mapping, adr: str, *, writing: str = "an explanation", numbering: bool = True
) -> dict | None:
    """Why a writer must not use this explanations section, or ``None``.

    The writer's reading of S2 spec §2.7: a section that is not a mapping, has
    no ``items`` list, holds an entry :func:`_explanation_entry_problem`
    rejects, or (for pm_add_explanation, which numbers entries) a
    ``last_number`` that is not an int of 0 or more (S2 spec §2.4). Every
    entry is looked at, since the next number is taken from all of them and
    pm_add_feedback looks an id up among all of them. A missing section is
    empty, not broken.

    Args:
        doc: A lineage document that passed :func:`write_refusal`.
        adr: The ADR id, as messages name it.
        writing: What the refused writer would write, as its message names it.
        numbering: Whether the writer numbers entries of this section, so
            ``last_number`` is looked at too.

    Returns:
        A ``decision_lineage_unreadable`` error naming the place (never a
        value), with a remediation addressed to the user; or ``None``.
    """
    part = doc.get("explanations")
    if part is None:
        return None

    def refuse(where: str, problem: str) -> dict:
        return _error(
            LINEAGE_UNREADABLE,
            f"{adr}: {where} in the lineage file {problem}, so {writing} cannot be "
            "written; nothing was written.",
            remediation=(
                f"Tell the user. Only the user may fix {where} in {_path_of(adr)} by hand, "
                "when no assistant session is writing. Do not edit the lineage file yourself."
            ),
        )

    if not isinstance(part, dict):
        return refuse("explanations", "is not a mapping")
    entries = part.get("items")
    if not isinstance(entries, list):
        return refuse("explanations.items", "is not a list")
    if numbering and "last_number" in part:
        last = part["last_number"]
        if not (isinstance(last, int) and not isinstance(last, bool) and last >= 0):
            return refuse("explanations.last_number", "is not a whole number of 0 or more")
    for position, entry in enumerate(entries, 1):
        problem = _explanation_entry_problem(entry)
        if problem is not None:
            return refuse(f"entry {position} of explanations.items", problem)
    return None


def _stale_explanations_notice(
    doc: Mapping, adr: str, written_from: int, new_version: int
) -> dict | None:
    """``decision_explanations_stale`` for a fact core revision (S2 spec §3.1).

    The explanations written from the version being replaced are stale once
    the new one is saved. The fact core writer does not look at the
    explanations section (S2 spec §2.7), so it is read leniently here: the
    last SCAN_EXPLANATIONS entries that are mappings with that
    ``fact_core_version``. Ids are listed only when they match
    EXPLANATION_ID_RE (at most _LISTED_STALE_IDS); others are counted.

    Returns:
        The info notice, or ``None`` when no such explanation is on record.
    """
    part = doc.get("explanations")
    entries = part.get("items") if isinstance(part, dict) else None
    if not isinstance(entries, list):
        return None
    shown: list[str] = []
    others = 0
    for entry in entries[-SCAN_EXPLANATIONS:]:
        if not isinstance(entry, dict):
            continue
        version = entry.get("fact_core_version")
        if not isinstance(version, int) or isinstance(version, bool) or version != written_from:
            continue
        entry_id = entry.get("id")
        if isinstance(entry_id, str) and EXPLANATION_ID_RE.fullmatch(entry_id):
            shown.append(entry_id)
        else:
            others += 1
    total = len(shown) + others
    if not total:
        return None
    listed = _dedupe(shown)[:_LISTED_STALE_IDS]
    named = ", ".join(listed)
    rest = total - len(listed)
    if rest and named:
        named += f" and {rest} more"
    elif rest:
        named = f"{rest} whose id cannot be shown"
    return notice(
        "info",
        EXPLANATIONS_STALE,
        f"{adr}: {total} explanation(s) written from version {written_from} of the fact core "
        f"are now stale ({named}); they are shown with the facts that changed.",
        f"Explain version {new_version} to the user again and save that explanation with "
        "pm_add_explanation.",
    )


# ─── Feedback (S2 spec §2.4) ─────────────────────────


def _feedback_entry_problem(entry: object) -> str | None:
    """Why an entry of ``feedback.items`` cannot be read, or ``None`` (S2 spec §2.7).

    Not a mapping, no id matching FEEDBACK_ID_RE, or no ``fact_core_version``
    of 0 or more (a bool is not one): the reading of an explanation entry
    (:func:`_explanation_entry_problem`). Readers skip such an entry; a
    writer that looks at the section treats it as broken. The reason names
    fields, never values.
    """
    if not isinstance(entry, dict):
        return "is not a mapping"
    entry_id = entry.get("id")
    if not (isinstance(entry_id, str) and FEEDBACK_ID_RE.fullmatch(entry_id)):
        return "has no valid feedback id"
    version = entry.get("fact_core_version")
    if not (isinstance(version, int) and not isinstance(version, bool) and version >= 0):
        return "has no fact_core_version of 0 or more"
    return None


def _feedback_refusal(doc: Mapping, adr: str, *, purpose: str, numbering: bool) -> dict | None:
    """Why a writer must not use this feedback section, or ``None`` (S2 spec §2.7).

    pm_add_feedback numbers its entry from every entry, and caused_by is
    looked up among every entry (S2 spec §3.1), so both refuse a section that
    is not a mapping, has no ``items`` list or holds an entry
    :func:`_feedback_entry_problem` rejects. pm_add_feedback also refuses a
    ``last_number`` that is not an int of 0 or more (``numbering``, S2 spec
    §2.4). A missing section is empty, not broken.

    Args:
        doc: A lineage document that passed :func:`write_refusal`.
        adr: The ADR id, as messages name it.
        purpose: What cannot be done, as the message says it.
        numbering: Whether the writer numbers entries of this section.

    Returns:
        A ``decision_lineage_unreadable`` error naming the place (never a
        value), with a remediation addressed to the user; or ``None``.
    """
    part = doc.get("feedback")
    if part is None:
        return None

    def refuse(where: str, problem: str) -> dict:
        return _error(
            LINEAGE_UNREADABLE,
            f"{adr}: {where} in the lineage file {problem}, so {purpose}; nothing was written.",
            remediation=(
                f"Tell the user. Only the user may fix {where} in {_path_of(adr)} by hand, "
                "when no assistant session is writing. Do not edit the lineage file yourself."
            ),
        )

    if not isinstance(part, dict):
        return refuse("feedback", "is not a mapping")
    entries = part.get("items")
    if not isinstance(entries, list):
        return refuse("feedback.items", "is not a list")
    if numbering and "last_number" in part:
        last = part["last_number"]
        if not (isinstance(last, int) and not isinstance(last, bool) and last >= 0):
            return refuse("feedback.last_number", "is not a whole number of 0 or more")
    for position, entry in enumerate(entries, 1):
        problem = _feedback_entry_problem(entry)
        if problem is not None:
            return refuse(f"entry {position} of feedback.items", problem)
    return None


def _feedback_fact_ids_problem(value: object) -> str | None:
    """Why the ``fact_ids`` of a pm_add_feedback call are refused, or ``None``.

    Left out (``None``), or a list of at most MAX_FEEDBACK_FACT_IDS strings
    matching FACT_ID_RE without repeats (S2 spec §2.8, §3.4). The reason names
    the rule, never the value.
    """
    if value is None:
        return None
    if not isinstance(value, list) or len(value) > MAX_FEEDBACK_FACT_IDS:
        return f"must be a list of at most {MAX_FEEDBACK_FACT_IDS} fact ids"
    if not all(is_fact_id(item) for item in value):
        return (
            "must hold only fact ids: F-what, F-change, F-revert, F-weak, F-uncertain or "
            "F-alt.N / F-conditions.N / F-premise.N"
        )
    if len(set(value)) != len(value):
        return "must not repeat an id"
    return None


def feedback_call_error(
    decision_id: object,
    feedback_kind: object,
    text: object,
    explanation_id: object = None,
    fact_ids: object = None,
    respondent_scope: object = RespondentScope.UNKNOWN.value,
) -> dict | None:
    """Check a pm_add_feedback call by its arguments alone (S2 spec §3.4).

    In this order: the id's shape (``invalid_decision_id``), the vocabularies
    (``invalid_feedback_kind``, ``invalid_respondent_scope``), a text that is
    not blank (``feedback_text_required``: the user's own words are recorded,
    never an empty mark) and at most MAX_TEXT_CHARS long (``text_too_long``,
    counted before redaction), an ``explanation_id`` of this ADR
    (``invalid_explanation_id``), the ``fact_ids`` (``invalid_fact_ids``) and,
    for check_answer, an explanation to answer (``check_answer_without_question``).
    Nothing is read and no message quotes a value passed, so the tool runs it
    before the project is resolved and the storage writer again before any
    file is read (as :func:`explanation_call_error`).

    Returns:
        An error dict, or ``None`` when the arguments are fine.
    """
    if not is_decision_id(decision_id):
        return _error("invalid_decision_id", "decision_id must look like ADR-NNN")
    if not isinstance(feedback_kind, str) or feedback_kind not in FEEDBACK_KINDS:
        return _error(
            "invalid_feedback_kind", f"feedback_kind must be one of: {', '.join(FEEDBACK_KINDS)}"
        )
    if not isinstance(respondent_scope, str) or respondent_scope not in RESPONDENT_SCOPES:
        return _error(
            "invalid_respondent_scope",
            f"respondent_scope must be one of: {', '.join(RESPONDENT_SCOPES)}",
        )
    if not isinstance(text, str) or not text.strip():
        return _error(
            "feedback_text_required", "text is required: record what the user said, in their words"
        )
    if len(text) > MAX_TEXT_CHARS:
        return _error(
            "text_too_long", f"text is longer than {MAX_TEXT_CHARS} characters", field="text"
        )
    if explanation_id is not None and not (
        isinstance(explanation_id, str)
        and EXPLANATION_ID_RE.fullmatch(explanation_id) is not None
        and explanation_id.partition(".")[0] == decision_id
    ):
        # decision_id matched DECISION_ID_RE above, so it can be named.
        return _error(
            "invalid_explanation_id",
            f"explanation_id must be the id of an explanation of {decision_id} "
            f"({decision_id}.EX-N)",
        )
    problem = _feedback_fact_ids_problem(fact_ids)
    if problem is not None:
        return _error("invalid_fact_ids", f"fact_ids {problem}")
    if feedback_kind == FeedbackKind.CHECK_ANSWER and explanation_id is None:
        return _error(
            CHECK_ANSWER_WITHOUT_QUESTION,
            "check_answer needs explanation_id: the explanation whose question the user answered",
        )
    return None


def _declared_scope(entry: Mapping) -> str:
    """A stored feedback entry's declared respondent scope, as the counts read it.

    A value of RESPONDENT_SCOPES; anything else (left out, out of vocabulary,
    not a string) is ``unknown``, never can_judge (S2 spec §2.4).
    """
    value = entry.get("respondent_scope")
    if isinstance(value, str) and value in RESPONDENT_SCOPES:
        return value
    return RespondentScope.UNKNOWN.value


def review_label(respondent_scope: str) -> str:
    """The ``review`` a reply is shown with (S2 spec §2.4); neither value says it was checked."""
    return REVIEW_DECLARED_CAN_JUDGE if respondent_scope == CAN_JUDGE else REVIEW_NONE


def _scope_label(counts: Mapping[str, int]) -> str:
    """The label of ``declared_respondent_scope``: ``mixed`` unless every reply agrees.

    can_judge alongside cannot_judge or unknown is ``mixed``, so one reply
    declared able to judge never hides one that was not (S2 spec §2.4).
    """
    total = sum(counts.values())
    if not total:
        return SCOPE_LABEL_NONE
    if counts[CAN_JUDGE] == total:
        return REVIEW_DECLARED_CAN_JUDGE
    if counts[CAN_JUDGE] == 0:
        return REVIEW_NONE
    return SCOPE_LABEL_MIXED


def _empty_declared_scope() -> dict:
    """``declared_respondent_scope`` without any reply to count (S2 spec §2.4)."""
    return {"label": SCOPE_LABEL_NONE, "counts": dict.fromkeys(RESPONDENT_SCOPES, 0)}


def declared_respondent_scope(doc: Mapping) -> dict:
    """What the replies to the current version declared about who answered (S2 spec §2.4).

    ``{label, counts: {can_judge, cannot_judge, unknown}, counts_capped?}``
    over the approve, reject and check_answer entries recorded against the
    current fact core version (0 without a fact core; none are counted while
    the fact core cannot be read). Replies to an older version are not
    counted: a new version is asked again. A declaration out of vocabulary or
    not a string counts as unknown, as each reply's ``review`` reads it. The
    last SCAN_FEEDBACK entries are read, as the view reads them;
    ``counts_capped`` says there were more. It is a summary of declarations,
    not a review status, and nothing is scored.

    Args:
        doc: An attributed lineage document.

    Returns:
        The summary, a new dict.
    """
    out = _empty_declared_scope()
    raw = doc.get("fact_core")
    if raw is None:
        current = 0
    else:
        readable = _readable_fact_core(raw)
        if readable is None:
            return out
        current = readable["version"]
    part = doc.get("feedback")
    entries = part.get("items") if isinstance(part, dict) else None
    if not isinstance(entries, list):
        return out
    counts: dict[str, int] = out["counts"]
    for entry in entries[-SCAN_FEEDBACK:]:
        if _feedback_entry_problem(entry) is not None:
            continue
        kind = entry.get("feedback_kind")
        if not (isinstance(kind, str) and kind in JUDGED_FEEDBACK_KINDS):
            continue
        if entry["fact_core_version"] == current:
            counts[_declared_scope(entry)] += 1
    out["label"] = _scope_label(counts)
    if len(entries) > SCAN_FEEDBACK:
        out["counts_capped"] = True
    return out


def _caused_by_argument_error(change: LineageChange) -> dict | None:
    """Argument-only checks of caused_by (S2 spec §3.1 step 1).

    A list of feedback ids (``invalid_caused_by``) that goes with fact_core or
    an evaluation (``caused_by_without_target``), at most MAX_CAUSED_BY once
    repeats are dropped (``too_many_caused_by``). An empty list asks for
    nothing. Whether the ids belong to this ADR and exist is checked under the
    lock (:func:`_caused_by_refusal`).
    """
    value = change.caused_by
    if value is None:
        return None
    if isinstance(value, (str, bytes, Mapping)) or not isinstance(value, Sequence):
        return _error(INVALID_CAUSED_BY, "caused_by must be a list of feedback ids (ADR-NNN.FB-n)")
    if not value:
        return None
    if change.fact_core is None and not _given(change.evaluation):
        return _error(
            "caused_by_without_target",
            "caused_by goes only with fact_core or evaluation: it names the feedback that led "
            "to that revision or evaluation",
        )
    if not all(isinstance(item, str) and FEEDBACK_ID_RE.fullmatch(item) for item in value):
        return _error(INVALID_CAUSED_BY, "caused_by must hold only feedback ids (ADR-NNN.FB-n)")
    if len(_dedupe(value)) > MAX_CAUSED_BY:
        return _error(
            "too_many_caused_by", f"caused_by may hold at most {MAX_CAUSED_BY} feedback ids"
        )
    return None


def _caused_by_feedback_refusal(doc: Mapping, adr: str) -> dict | None:
    """The feedback section must be whole before caused_by is looked up in it (S2 spec §2.7)."""
    return _feedback_refusal(
        doc, adr, purpose="the feedback named in caused_by cannot be looked up", numbering=False
    )


def _caused_by_refusal(doc: Mapping, adr: str, caused_by: list[str]) -> dict | None:
    """Whether caused_by names feedback of this ADR that is on record (S2 spec §3.1 step 6).

    ``invalid_caused_by`` for an id of another ADR: its feedback would sit in
    another lineage, whose lock a writer cannot take as well (links and notes
    say how ADRs relate instead). ``caused_by_not_found`` for an id with no
    entry in ``feedback.items``. The section passed
    :func:`_caused_by_feedback_refusal`, and the ids matched FEEDBACK_ID_RE,
    so they can be named.
    """
    others = [item for item in caused_by if item.partition(".")[0] != adr]
    if others:
        return _error(
            INVALID_CAUSED_BY,
            f"caused_by may name only feedback of {adr}; {', '.join(others)} belong to "
            "another ADR. Relate ADRs with links and a note instead.",
        )
    part = doc.get("feedback")
    entries = part.get("items") if isinstance(part, dict) else None
    known = {entry["id"] for entry in entries} if isinstance(entries, list) else set()
    missing = [item for item in caused_by if item not in known]
    if missing:
        return _error(
            CAUSED_BY_NOT_FOUND,
            f"{adr}: no feedback {', '.join(missing)} is on record; nothing was written.",
            remediation=(
                "Record what the user said with pm_add_feedback first, and pass the "
                "feedback_id it returns."
            ),
        )
    return None


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


def _empty_explanations() -> dict:
    """The explanations of an S2 view without any (S2 spec §2.6)."""
    return {"items": [], "explanations_total": 0}


def _empty_feedback() -> dict:
    """The feedback of an S2 view without any (S2 spec §2.6)."""
    return {"items": [], "feedback_total": 0}


@dataclass
class LineageView:
    """What readers show for one ADR's lineage (design §2.7, §3.2).

    Built only from allow-listed fields as plain strings, ints and lists of
    them, so it is bounded and JSON-safe whatever the file holds. ``derived``
    means the lifecycle and the rest were inferred from the ADR's status, not
    taken from a lineage. The S2 fields are filled only by a view built with
    ``include_s2=True`` (``s2``); the attributes after ``s2`` are for the
    notices of :func:`fact_core_notices` and the brief's floor, and are not
    part of :meth:`as_dict`.
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
    fact_core: dict | None = None
    explanations: dict = field(default_factory=_empty_explanations)
    feedback: dict = field(default_factory=_empty_feedback)
    declared_respondent_scope: dict = field(default_factory=_empty_declared_scope)
    adopted_fact_core_version: int | None = None
    s2: bool = False
    fact_core_adoption: str | None = None
    # (recorded, current) reversibility labels when they disagree.
    reversibility_change: tuple[str, str] | None = None
    # What the brief's floor reads (:func:`floor_reversibility` of the stored
    # fact core, so it agrees with the ratchet); unknown without one.
    reversibility_floor: str = Reversibility.UNKNOWN.value
    # Whether the adoption fixed the fact core, or that cannot be read (a
    # malformed adopted version): a fact core revision would be refused.
    fact_core_fixed: bool = False

    @property
    def note_codes(self) -> set[str]:
        """The note codes present in this view."""
        return {entry["code"] for entry in self.notes}

    def as_dict(self) -> dict:
        """The ``lineage`` object of a read response (S2 keys only when ``s2``)."""
        out = {
            "derived": self.derived,
            "schema": self.schema,
            "lifecycle": self.lifecycle,
            "recorded_at": self.recorded_at,
            "declared": dict(self.declared),
            "declared_later": list(self.declared_later),
            "not_recorded": list(self.not_recorded),
            "links": {key: list(value) for key, value in self.links.items()},
            "events": [_plain_copy(event) for event in self.events],
            "events_total": self.events_total,
            "unknown_keys": list(self.unknown_keys),
            "notes": [dict(entry) for entry in self.notes],
        }
        if self.s2:
            out["fact_core"] = _plain_copy(self.fact_core)
            out["explanations"] = _plain_copy(self.explanations)
            out["feedback"] = _plain_copy(self.feedback)
            out["declared_respondent_scope"] = _plain_copy(self.declared_respondent_scope)
            out["adopted_fact_core_version"] = self.adopted_fact_core_version
        return out


def _plain_copy(value: object) -> object:
    """A copy of a view value: dicts and lists are copied, scalars shared."""
    if isinstance(value, dict):
        return {key: _plain_copy(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_plain_copy(item) for item in value]
    return value


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


class _UnknownNames:
    """Names of unknown keys inside the fact core, under one cap for the section.

    Unknown keys inside an S2 section are shown by name only (S2 spec §2.6).
    The fact core's items are never cut to fit a get's budget (S2 spec §3.6),
    so the section, its items and its previous values share MAX_UNKNOWN_FIELDS
    names; names past the cap are counted as skipped.
    """

    def __init__(self, notes: _ViewNotes) -> None:
        self.left = MAX_UNKNOWN_FIELDS
        self.notes = notes

    def add(self, out: dict, entry: dict, known: frozenset[str]) -> None:
        """Put ``unknown_fields`` on ``out`` when ``entry`` has keys outside ``known``."""
        extra = [key for key in entry if key not in known]
        if not extra:
            return
        names = _key_names(extra, self.left, self.notes)
        self.left -= len(names)
        if len(extra) > len(names):
            self.notes.skipped(len(extra) - len(names))
        if names:
            out["unknown_fields"] = names


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


def _view_id_list(
    value: object, pattern: re.Pattern[str], limit: int, notes: _ViewNotes
) -> list[str] | None:
    """A list of ids for a view: only strings matching ``pattern``, at most ``limit``.

    Entries past ``limit`` are counted as skipped without being read, so a list
    inflated through YAML aliases costs no more than the cap.

    Returns:
        The ids, or ``None`` when ``value`` is not a list (counted as skipped).
    """
    if not isinstance(value, list):
        notes.skipped()
        return None
    ids: list[str] = []
    for item in value[:limit]:
        if isinstance(item, str) and pattern.fullmatch(item) is not None:
            ids.append(item)
        else:
            notes.skipped()
    if len(value) > limit:
        notes.skipped(len(value) - limit)
    return ids


def _view_event(event: dict, notes: _ViewNotes) -> dict:
    """One event through the per-kind allow-list (design §2.4, S2 spec §2.5)."""
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
        if name in _EVENT_INT_FIELDS or name in _EVENT_POSITIVE_INT_FIELDS:
            if (
                isinstance(value, int)
                and not isinstance(value, bool)
                and (name in _EVENT_INT_FIELDS or value >= 1)
            ):
                out[name] = value
            else:
                notes.skipped()
            continue
        id_list = _EVENT_ID_LIST_FIELDS.get(name)
        if id_list is not None:
            ids = _view_id_list(value, id_list[0], id_list[1], notes)
            if ids is not None:
                out[name] = ids
            continue
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


def _drop_versions_above(events: list[dict], fact_core: dict | None, notes: _ViewNotes) -> None:
    """Remove a shown ``fact_core_version`` above the current version (S2 spec §2.7).

    Such a value is not shown, as ``adopted_fact_core_version`` is not, and is
    counted as skipped. Without a readable fact core every value is kept (a
    fact core removed by hand leaves the adoption fixed, S2 spec §2.2).

    Args:
        events: The shown events, changed in place.
        fact_core: The readable fact core, or ``None``.
        notes: Where the dropped values are counted.
    """
    if fact_core is None:
        return
    for event in events:
        value = event.get("fact_core_version")
        if isinstance(value, int) and value > fact_core["version"]:
            del event["fact_core_version"]
            notes.skipped()


def _view_fact_fields(entry: dict, notes: _ViewNotes) -> tuple[str, dict[str, str], bool]:
    """An item's source and, for its kind, its confidence or reversibility, redacted.

    A missing source is the documented default, proposal (S2 spec §2.9).
    Confidence is shown on F-uncertain and reversibility on F-revert only, the
    way the writer copies them; values outside a vocabulary are shown as they
    are, redacted.

    Returns:
        (source, ``{"confidence" | "reversibility": value}``, whether any was cut).
    """
    source, cut = notes.text(entry.get("source", SOURCE_PROPOSAL), MAX_LABEL_CHARS)
    extra: dict[str, str] = {}
    for name, owner in (("confidence", "F-uncertain"), ("reversibility", "F-revert")):
        if entry["id"] == owner and name in entry:
            extra[name], clipped = notes.text(entry[name], MAX_LABEL_CHARS)
            cut |= clipped
    return source, extra, cut


def _view_fact_items(items: list[dict], notes: _ViewNotes, names: _UnknownNames) -> list[dict]:
    """The usable items, their text cut to MAX_FACT_TEXT_CHARS and in total to MAX_FACT_CORE_CHARS.

    The writer's total limit is applied here too, so a hand-written file of
    MAX_FACT_ITEMS long items cannot inflate a response; an item cut by the
    total is marked ``truncated`` and counted as skipped (S2 spec §2.6).
    """
    shown: list[dict] = []
    budget = MAX_FACT_CORE_CHARS
    for item in items:
        text, cut = notes.text(item["text"], MAX_FACT_TEXT_CHARS)
        if len(text) > budget:
            text = text[:budget]
            cut = True
            notes.skipped()
        budget -= len(text)
        source, extra, clipped = _view_fact_fields(item, notes)
        since = item.get("since")
        out: dict[str, object] = {
            "id": item["id"],
            "text": text,
            "source": source,
            "since": since if _is_positive_int(since) else None,
            **extra,
        }
        names.add(out, item, _FACT_ITEM_KEYS)
        if cut or clipped:
            out["truncated"] = True
        shown.append(out)
    return shown


def _view_previous_values(
    raw: object, notes: _ViewNotes, names: _UnknownNames
) -> tuple[list[dict], int]:
    """The latest previous value of each id, newest first, and the list's length.

    The latest is the entry with the highest ``last_version``; on a tie, the
    later one in the file (an absorbed change outside the tools comes before
    the change made in the same version, S2 spec §2.2). Up to
    SCAN_PREVIOUS_VALUES entries are read, and VIEW_PREVIOUS_VALUES ids shown.
    """
    if raw is None:
        return [], 0
    if not isinstance(raw, list):
        notes.skipped()
        return [], 0
    latest: dict[str, tuple[int, int, dict]] = {}
    for position, element in enumerate(raw[:SCAN_PREVIOUS_VALUES]):
        if _previous_value_problem(element) is not None:
            notes.skipped()
            continue
        rank = (element["last_version"], position)
        known = latest.get(element["id"])
        if known is None or rank > known[:2]:
            latest[element["id"]] = (*rank, element)
    if len(raw) > SCAN_PREVIOUS_VALUES:
        notes.skipped(len(raw) - SCAN_PREVIOUS_VALUES)
    ordered = sorted(latest.values(), key=lambda found: found[:2], reverse=True)
    if len(ordered) > VIEW_PREVIOUS_VALUES:
        notes.skipped(len(ordered) - VIEW_PREVIOUS_VALUES)
    shown: list[dict] = []
    for last_version, _, element in ordered[:VIEW_PREVIOUS_VALUES]:
        text, cut = notes.text(element["text"], PREVIOUS_TEXT_CHARS)
        source, extra, clipped = _view_fact_fields(element, notes)
        out: dict[str, object] = {
            "id": element["id"],
            "last_version": last_version,
            "text": text,
            "source": source,
            **extra,
        }
        names.add(out, element, _PREVIOUS_VALUE_KEYS)
        if cut or clipped:
            out["truncated"] = True
        shown.append(out)
    return shown, len(raw)


def _view_fact_core(raw: object, notes: _ViewNotes) -> tuple[dict | None, tuple[str, str] | None]:
    """The ``fact_core`` of an S2 view (S2 spec §2.6, §2.7).

    Keys this version does not know, on the section, an item or a shown
    previous value, are named in that level's ``unknown_fields`` (never their
    values), so a reader can tell the file holds more than is shown.

    Returns:
        The fact core (``None`` when there is none, or when it is not a
        mapping with an int ``version`` of 1 or more and a list of ``items``,
        which is counted as skipped), and the ``(recorded, current)``
        reversibility labels when F-revert's value differs from what a tool
        last recorded.
    """
    if raw is None:
        return None, None
    readable = _readable_fact_core(raw)
    if readable is None:
        notes.skipped()
        return None, None
    items, skipped = _readable_items(readable["items"])
    if skipped:
        notes.skipped(skipped)
    retired_raw = readable.get("retired_ids")
    retired: list[str] = []
    retired_total = 0
    if retired_raw is not None:
        retired = _view_id_list(retired_raw, FACT_ID_RE, VIEW_RETIRED_IDS, notes) or []
        retired_total = len(retired_raw) if isinstance(retired_raw, list) else 0
    names = _UnknownNames(notes)
    section_names: dict[str, list[str]] = {}
    names.add(section_names, readable, _FACT_CORE_KEYS)
    shown_items = _view_fact_items(items, notes, names)
    previous, previous_total = _view_previous_values(readable.get("previous_values"), notes, names)
    recorded = readable.get("recorded_reversibility")
    current = _current_revert_value(readable["items"], items)
    outside = _changed_outside_tools(recorded, current)
    view = {
        "version": readable["version"],
        "items": shown_items,
        "items_total": len(readable["items"]),
        "retired_ids": retired,
        "retired_ids_total": retired_total,
        "previous_values": previous,
        "previous_values_total": previous_total,
        "recorded_reversibility": (
            recorded if isinstance(recorded, str) and recorded in REVERSIBILITIES else None
        ),
        "reversibility_changed_outside_tools": outside,
        **section_names,
    }
    change = (reversibility_label(recorded), reversibility_label(current)) if outside else None
    return view, change


def _view_paragraphs(
    raw: object, notes: _ViewNotes, names: _UnknownNames
) -> tuple[list[dict], bool]:
    """The paragraphs of the one explanation shown with its body (S2 spec §2.6).

    At most MAX_PARAGRAPHS are read, each text cut to MAX_PARAGRAPH_CHARS and
    all of them to MAX_EXPLANATION_CHARS: the writer's limits, applied again
    so a hand-written file (one long paragraph repeated through aliases)
    cannot inflate a response. Paragraphs that are not mappings with a text
    string are skipped; what is cut or left out is counted as skipped.

    Returns:
        The paragraphs ``{fact_ids, text, truncated?}`` and whether the body
        was cut short (``paragraphs_truncated``).
    """
    if not isinstance(raw, list):
        notes.skipped()
        return [], False
    truncated = len(raw) > MAX_PARAGRAPHS
    if truncated:
        notes.skipped(len(raw) - MAX_PARAGRAPHS)
    entries = raw[:MAX_PARAGRAPHS]
    shown: list[dict] = []
    budget = MAX_EXPLANATION_CHARS
    for position, paragraph in enumerate(entries):
        if not isinstance(paragraph, dict) or not isinstance(paragraph.get("text"), str):
            notes.skipped()
            continue
        if budget <= 0:
            notes.skipped(len(entries) - position)
            truncated = True
            break
        text, cut = notes.text(paragraph["text"], MAX_PARAGRAPH_CHARS)
        if len(text) > budget:
            text = text[:budget]
            cut = truncated = True
            notes.skipped()
        budget -= len(text)
        fact_ids = _view_id_list(
            paragraph.get("fact_ids"), FACT_ID_RE, MAX_PARAGRAPH_FACT_IDS, notes
        )
        out: dict[str, object] = {"fact_ids": fact_ids or [], "text": text}
        names.add(out, paragraph, _PARAGRAPH_KEYS)
        if cut:
            out["truncated"] = True
        shown.append(out)
    return shown, truncated


def _view_question_kind(value: object, notes: _ViewNotes) -> str | None:
    """A stored question kind when it is one of QUESTION_KINDS, else ``None`` (counted)."""
    if isinstance(value, str) and value in QUESTION_KINDS:
        return value
    if value is not None:
        notes.skipped()
    return None


def _view_question(raw: object, notes: _ViewNotes, names: _UnknownNames) -> dict | None:
    """The question of the explanation shown with its body: ``{question_kind, fact_ids, text}``."""
    if raw is None:
        return None
    if not isinstance(raw, dict) or not isinstance(raw.get("text"), str):
        notes.skipped()
        return None
    text, cut = notes.text(raw["text"], MAX_QUESTION_CHARS)
    fact_ids = _view_id_list(raw.get("fact_ids"), FACT_ID_RE, MAX_PARAGRAPH_FACT_IDS, notes)
    out: dict[str, object] = {
        "question_kind": _view_question_kind(raw.get("question_kind"), notes),
        "fact_ids": fact_ids or [],
        "text": text,
    }
    names.add(out, raw, _QUESTION_KEYS)
    if cut:
        out["truncated"] = True
    return out


def _view_explanation_row(
    entry: dict, staleness: Staleness, notes: _ViewNotes, names: _UnknownNames
) -> dict:
    """One explanation without its body (S2 spec §2.6).

    The accuracy is never stored and always ``unconfirmed`` (S2 spec §2.3); of
    the question only its kind is shown, and only a kind of QUESTION_KINDS.
    """
    at, _ = _view_time(entry.get("at"), notes)
    question = entry.get("question")
    paragraphs = entry.get("paragraphs")
    row: dict[str, object] = {
        "id": entry["id"],
        "at": at,
        "fact_core_version": entry["fact_core_version"],
        "stale": staleness.stale,
        "changed_ids": list(staleness.changed_ids),
    }
    if staleness.changed_ids_truncated:
        row["changed_ids_truncated"] = True
    row.update(
        fact_ids_present=entry.get("fact_ids_present") is True,
        accuracy=ACCURACY_UNCONFIRMED,
        question_kind=(
            _view_question_kind(question.get("question_kind"), notes)
            if isinstance(question, dict)
            else None
        ),
        paragraphs_total=len(paragraphs) if isinstance(paragraphs, list) else 0,
    )
    names.add(row, entry, _EXPLANATION_KEYS)
    return row


def _view_explanations(raw: object, fact_core: object, notes: _ViewNotes) -> dict:
    """The ``explanations`` of an S2 view (S2 spec §2.3, §2.6, §2.7).

    The last SCAN_EXPLANATIONS entries are read, newest first, and up to
    VIEW_EXPLANATIONS readable ones shown, each marked stale or not with the
    ids changed since it was written (:func:`explanation_staleness`, from the
    fact core section alone). Only the newest explanation that is not stale
    carries its body (``paragraphs`` and ``question``). A section that is not
    a mapping or has no ``items`` list is shown empty, and entries that
    :func:`_explanation_entry_problem` rejects are skipped, both counted as
    skipped: the same reading as the adoption's check. Unknown keys are named
    in ``unknown_fields``, at most MAX_UNKNOWN_FIELDS for the whole section.

    Args:
        raw: The lineage's ``explanations`` value, as loaded.
        fact_core: The lineage's ``fact_core`` value, as loaded.
        notes: Where skipped entries and redactions are counted.

    Returns:
        ``{items, explanations_total, explanations_total_capped?, unknown_fields?}``;
        the total counts the scanned entries and is capped at SCAN_EXPLANATIONS.
    """
    out = _empty_explanations()
    if raw is None:
        return out
    entries = raw.get("items") if isinstance(raw, dict) else None
    if not isinstance(entries, list):
        notes.skipped()
        return out
    names = _UnknownNames(notes)
    names.add(out, raw, _EXPLANATIONS_KEYS)
    scanned = entries[-SCAN_EXPLANATIONS:]
    out["explanations_total"] = len(scanned)
    if len(entries) > SCAN_EXPLANATIONS:
        out["explanations_total_capped"] = True
        notes.skipped(len(entries) - SCAN_EXPLANATIONS)
    staleness: dict[int, Staleness] = {}
    shown: list[tuple[dict, dict]] = []
    for entry in reversed(scanned):
        if _explanation_entry_problem(entry) is not None:
            notes.skipped()
            continue
        if len(shown) >= VIEW_EXPLANATIONS:
            continue
        version: int = entry["fact_core_version"]
        if version not in staleness:
            staleness[version] = explanation_staleness(fact_core, version)
        shown.append((_view_explanation_row(entry, staleness[version], notes, names), entry))
    for row, entry in shown:
        if row["stale"]:
            continue
        paragraphs, truncated = _view_paragraphs(entry.get("paragraphs"), notes, names)
        row["paragraphs"] = paragraphs
        if truncated:
            row["paragraphs_truncated"] = True
        row["question"] = _view_question(entry.get("question"), notes, names)
        break
    out["items"] = [row for row, _ in shown]
    return out


def _view_feedback_row(entry: dict, notes: _ViewNotes, names: _UnknownNames) -> dict:
    """One feedback entry of an S2 view (S2 spec §2.6).

    Vocabulary fields are shown only with a vocabulary value (``feedback_kind``
    and ``question_kind`` are null otherwise, counted as skipped), the text is
    cut at VIEW_FEEDBACK_TEXT_CHARS with ``text_truncated``, and ``review``
    is derived from the declared scope, which reads as unknown unless it is a
    vocabulary value (S2 spec §2.4). Every reply is shown as what an assistant
    recorded, unchecked; nothing is scored.
    """
    at, _ = _view_time(entry.get("at"), notes)
    kind = entry.get("feedback_kind")
    if not (isinstance(kind, str) and kind in FEEDBACK_KINDS):
        kind = None
        notes.skipped()
    raw_text = entry.get("text")
    text: str | None = None
    cut = False
    if isinstance(raw_text, str):
        text, cut = notes.text(raw_text, VIEW_FEEDBACK_TEXT_CHARS)
    else:
        notes.skipped()
    explanation_id = entry.get("explanation_id")
    if explanation_id is not None and not (
        isinstance(explanation_id, str) and EXPLANATION_ID_RE.fullmatch(explanation_id)
    ):
        explanation_id = None
        notes.skipped()
    fact_ids: list[str] = []
    if entry.get("fact_ids") is not None:
        fact_ids = _view_id_list(entry["fact_ids"], FACT_ID_RE, MAX_FEEDBACK_FACT_IDS, notes) or []
    scope = _declared_scope(entry)
    if entry.get("respondent_scope") not in (None, scope):
        notes.skipped()  # out of vocabulary or not a string: read as unknown
    row: dict[str, object] = {
        "id": entry["id"],
        "at": at,
        "feedback_kind": kind,
        "text": text,
        "text_truncated": cut,
        "fact_core_version": entry["fact_core_version"],
        "explanation_id": explanation_id,
        "fact_ids": fact_ids,
        "respondent_scope": scope,
        "question_kind": _view_question_kind(entry.get("question_kind"), notes),
        "review": review_label(scope),
        "recorded_as": ASSISTANT_RECORDED,
    }
    names.add(row, entry, _FEEDBACK_KEYS)
    return row


def _view_feedback(raw: object, notes: _ViewNotes) -> dict:
    """The ``feedback`` of an S2 view (S2 spec §2.4, §2.6, §2.7).

    The last SCAN_FEEDBACK entries are read, newest first, and up to
    VIEW_FEEDBACK readable ones shown (:func:`_view_feedback_row`). A section
    that is not a mapping or has no ``items`` list is shown empty, and entries
    that :func:`_feedback_entry_problem` rejects are skipped, both counted as
    skipped. Unknown keys are named in ``unknown_fields``, at most
    MAX_UNKNOWN_FIELDS for the whole section. The shape follows the
    explanations' (:func:`_view_explanations`).

    Args:
        raw: The lineage's ``feedback`` value, as loaded.
        notes: Where skipped entries and redactions are counted.

    Returns:
        ``{items, feedback_total, feedback_total_capped?, unknown_fields?}``;
        the total counts the scanned entries and is capped at SCAN_FEEDBACK.
    """
    out = _empty_feedback()
    if raw is None:
        return out
    entries = raw.get("items") if isinstance(raw, dict) else None
    if not isinstance(entries, list):
        notes.skipped()
        return out
    names = _UnknownNames(notes)
    names.add(out, raw, _FEEDBACK_SECTION_KEYS)
    scanned = entries[-SCAN_FEEDBACK:]
    out["feedback_total"] = len(scanned)
    if len(entries) > SCAN_FEEDBACK:
        out["feedback_total_capped"] = True
        notes.skipped(len(entries) - SCAN_FEEDBACK)
    rows: list[dict] = []
    for entry in reversed(scanned):
        if _feedback_entry_problem(entry) is not None:
            notes.skipped()
            continue
        if len(rows) < VIEW_FEEDBACK:
            rows.append(_view_feedback_row(entry, notes, names))
    out["items"] = rows
    return out


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
    include_s2: bool = False,
) -> LineageView:
    """Build what readers show for one ADR; pure, no I/O (design §2.7, S2 spec §2.6).

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
        include_s2: True for a reader that shows the S2 sections (a get):
            ``fact_core``, ``explanations``, ``feedback``,
            ``declared_respondent_scope`` and ``adopted_fact_core_version``
            are then built,
            with the list caps of S2 spec §2.6 (both ``None`` for a derived
            view). A list row and the draft guard leave it False.

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
            s2=include_s2,
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
        _drop_versions_above(events, _readable_fact_core(doc.get("fact_core")), notes)
        unknown_keys = _key_names(
            (key for key in doc if key not in _KNOWN_TOP_LEVEL), MAX_UNKNOWN_KEYS, notes
        )

    fact_core: dict | None = None
    explanations = _empty_explanations()
    feedback = _empty_feedback()
    declared_scope = _empty_declared_scope()
    revert_change: tuple[str, str] | None = None
    adopted: int | None = None
    adoption: str | None = None
    fixed = False
    revert_floor = Reversibility.UNKNOWN.value
    if include_s2:
        fact_core, revert_change = _view_fact_core(doc.get("fact_core"), notes)
        revert_floor = floor_reversibility(doc.get("fact_core"))
        explanations = _view_explanations(doc.get("explanations"), doc.get("fact_core"), notes)
        feedback = _view_feedback(doc.get("feedback"), notes)
        declared_scope = declared_respondent_scope(doc)
        adopted, malformed, adoption = adopted_fact_core_version(doc)
        fixed = malformed or _fixed_by_adoption(doc, adopted)
        if malformed:
            notes.skipped()

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
        fact_core=fact_core,
        explanations=explanations,
        feedback=feedback,
        declared_respondent_scope=declared_scope,
        adopted_fact_core_version=adopted,
        s2=include_s2,
        fact_core_adoption=adoption,
        reversibility_change=revert_change,
        reversibility_floor=revert_floor,
        fact_core_fixed=fixed,
    )


# The remediation of decision_reversibility_changed_outside_tools on an ADR
# whose adoption fixed the fact core (or whether it did cannot be read): the
# revision that would take the change in is refused with
# fact_core_after_adoption until the ADR is back in proposed (S2 spec §5.4).
_OUTSIDE_CHANGE_FIXED_REMEDIATION = (
    "Tell the user. The fact core of this ADR is fixed since the user adopted it, so no "
    "revision can take the change in while it stays adopted. Only if the user asks, move it "
    "back to proposed with pm_update_decision and revise, or record the change as a new ADR "
    "with amends."
)


def fact_core_notices(view: LineageView) -> list[dict]:
    """The info notices a reader of an S2 view adds for its fact core (S2 spec §2.6).

    ``decision_reversibility_changed_outside_tools`` when F-revert's value
    differs from what a tool last recorded, ``decision_fact_core_written_after_adoption``
    for facts the adoption did not fix on an ADR that went through one (or may
    have), and ``decision_fact_core_changed_since_adoption`` when the fixed
    version is not the current one. Values are named by vocabulary only.

    Args:
        view: A view built with ``include_s2=True``.

    Returns:
        Warning entries in the shape of ``server._build_warning``.
    """
    if not view.s2 or view.derived:
        return []
    adr = view.decision_id
    out: list[dict] = []
    if view.reversibility_change is not None:
        recorded, current = view.reversibility_change
        if current == REVERSIBILITY_UNREADABLE:
            remediation = (
                "Tell the user. Until the user fixes the one reversibility line of F-revert in "
                f"{_path_of(adr)} with code_revertable, external_irreversible or unknown, the "
                "fact core cannot be written. Do not edit the lineage file yourself."
            )
        elif view.fact_core_fixed:
            remediation = _OUTSIDE_CHANGE_FIXED_REMEDIATION
        else:
            remediation = (
                "Tell the user. The next fact core revision with pm_update_decision records "
                "the change in a new version."
            )
        out.append(
            notice(
                "info",
                REVERSIBILITY_CHANGED_OUTSIDE_TOOLS,
                f"{adr}: F-revert's reversibility was changed outside pmlens's tools "
                f"(recorded {recorded}, now {current}). Explanations of the current version "
                "are shown as stale, and the floor follows the value now in the file.",
                remediation,
            )
        )
    current_version = view.fact_core["version"] if view.fact_core is not None else 0
    if (
        view.fact_core is not None
        and view.adopted_fact_core_version is None
        and view.fact_core_adoption in (ADOPTION_ADOPTED, ADOPTION_UNKNOWN)
    ):
        if view.fact_core_adoption == ADOPTION_ADOPTED:
            message = (
                f"{adr}: its fact core was written after the ADR was adopted, so it is not "
                "what the user accepted."
            )
        else:
            message = (
                f"{adr}: whether this ADR was ever adopted is not known from its record (its "
                "lineage was started from its status); its fact core is not what the user "
                "accepted."
            )
        out.append(notice("info", FACT_CORE_WRITTEN_AFTER_ADOPTION, message))
    if (
        view.adopted_fact_core_version is not None
        and view.adopted_fact_core_version != current_version
    ):
        out.append(
            notice(
                "info",
                FACT_CORE_CHANGED_SINCE_ADOPTION,
                f"{adr}: the user adopted version {view.adopted_fact_core_version} of the fact "
                f"core, but the file now holds version {current_version}; it was changed by "
                "hand after the adoption.",
                "Tell the user.",
            )
        )
    return out


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
    """One pm_update_decision call's requested changes (design §4.3, S2 spec §3.1).

    ``None`` (or an empty string for the texts) means "not requested".
    ``fact_core`` is the whole new list of facts (``{id or kind, text,
    confidence?, reversibility?}``) and goes with ``expected_fact_core_version``
    (0 for the first version), ``reason``, ``caused_by`` and nothing else.
    ``caused_by`` names feedback of the same ADR (``ADR-NNN.FB-n``) that led
    to the fact core version or the evaluation (S2 spec §3.1).
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
    fact_core: Sequence[Mapping[str, object]] | None = None
    expected_fact_core_version: int | None = None
    caused_by: Sequence[str] | None = None
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
        fact_core: ``{"version", "ids"}`` of the fact core after a call that
            passed ``fact_core`` (``unchanged`` included), so the caller learns
            the numbers it was given; ``None`` for any other call. Its
            ``changes`` then hold ``fact_core`` (S2 spec §3.1).
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
    fact_core: dict | None = None


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


def _fact_item_argument_error(index: int, item: object, seen: set[str]) -> dict | None:
    """Check one item of the fact_core argument by its shape alone (S2 spec §2.2).

    Args:
        index: The item's 0-based position (returned as ``item_index``).
        item: The item as passed.
        seen: The ids of the earlier items; this item's id is added when known
            from the argument (a given id, or the kind of a single item).
    """

    def invalid(message: str, code: str = "invalid_fact_core") -> dict:
        return _error(code, f"fact_core item {index}: {message}", item_index=index)

    if not isinstance(item, Mapping):
        return invalid("must be a mapping {id or kind, text}")
    if any(key not in FACT_INPUT_KEYS for key in item):
        # Key names are not echoed: they are the caller's text.
        return invalid(
            "takes only id, kind, text, confidence and reversibility; pmlens sets "
            "source and since itself"
        )
    fact_id = item.get("id")
    kind = item.get("kind")
    if fact_id is not None and not is_fact_id(fact_id):
        return invalid(
            "id must be one of F-what, F-change, F-revert, F-weak, F-uncertain or "
            "F-alt.N / F-conditions.N / F-premise.N (N from 1 to 99)",
            "invalid_fact_core_id",
        )
    if kind is not None and (not isinstance(kind, str) or kind not in FACT_KINDS):
        return invalid(f"kind must be one of: {', '.join(FACT_KINDS)}")
    if fact_id is None and kind is None:
        return invalid("needs its id, or only its kind for a new numbered item")
    if fact_id is not None and kind is not None and fact_kind(fact_id) != kind:
        return invalid("id and kind name different kinds")
    item_kind = fact_kind(fact_id) if fact_id is not None else kind
    text = item.get("text")
    if not isinstance(text, str) or not text.strip():
        return invalid("text must be a non-empty string")
    if len(text) > MAX_FACT_TEXT_CHARS:
        return _error(
            "text_too_long",
            f"fact_core item {index}: text is longer than {MAX_FACT_TEXT_CHARS} characters",
            field="fact_core",
            item_index=index,
        )
    for name, owner, vocabulary in (
        ("confidence", "uncertain", CONFIDENCES),
        ("reversibility", "revert", REVERSIBILITIES),
    ):
        value = item.get(name)
        if item_kind == owner:
            if not isinstance(value, str) or value not in vocabulary:
                return invalid(f"F-{owner} needs {name}, one of: {', '.join(vocabulary)}")
        elif value is not None:
            return invalid(f"{name} goes on F-{owner} only")
    known_id = fact_id
    if known_id is None and kind in FACT_SINGLE_KINDS:
        known_id = f"F-{kind}"
    if known_id is not None:
        if known_id in seen:
            return _error(
                "fact_core_id_duplicate",
                f"fact_core item {index}: {known_id} is already in the list",
                item_index=index,
            )
        seen.add(known_id)
    return None


def _fact_core_argument_error(change: LineageChange) -> dict | None:
    """Argument-only checks of a fact_core call (S2 spec §3.1 step 1)."""
    if change.fact_core is None:
        if change.expected_fact_core_version is not None:
            return _error(
                "invalid_fact_core_version",
                "expected_fact_core_version is passed only with fact_core",
            )
        return None
    combined = [
        name
        for name, value in (
            ("lifecycle", change.lifecycle),
            ("add_links", change.add_links),
            ("remove_links", change.remove_links),
            ("evaluation", change.evaluation),
            ("note", change.note),
            ("origin", change.origin),
            ("recorded_timing", change.recorded_timing),
        )
        if value is not None
    ]
    if combined:
        return _error(
            "fact_core_not_combinable",
            f"fact_core cannot be combined with {', '.join(combined)}: revise the facts in one "
            "call, and adopt in another after the user has read an explanation of the "
            "current version",
        )
    expected = change.expected_fact_core_version
    if expected is None:
        return _error(
            "fact_core_version_required",
            "expected_fact_core_version is required with fact_core (0 for the first version)",
        )
    if not isinstance(expected, int) or isinstance(expected, bool) or expected < 0:
        return _error(
            "invalid_fact_core_version",
            "expected_fact_core_version must be a whole number of 0 or more",
        )
    items = change.fact_core
    if isinstance(items, (str, bytes, Mapping)) or not isinstance(items, Sequence):
        return _error("invalid_fact_core", "fact_core must be a list of {id or kind, text} items")
    if len(items) > MAX_FACT_ITEMS:
        return _error("fact_core_too_large", f"fact_core may hold at most {MAX_FACT_ITEMS} items")
    seen: set[str] = set()
    for index, item in enumerate(items):
        problem = _fact_item_argument_error(index, item, seen)
        if problem is not None:
            return problem
    if sum(len(item["text"]) for item in items) > MAX_FACT_CORE_CHARS:
        return _error(
            "fact_core_too_large",
            f"the texts of fact_core may hold at most {MAX_FACT_CORE_CHARS} characters in all",
        )
    return None


def validate_change(change: LineageChange) -> dict | None:
    """Argument-only checks for a lineage change (no file state needed).

    Returns:
        An error dict, or ``None`` when the arguments are acceptable.
    """
    problem = _fact_core_argument_error(change) or _caused_by_argument_error(change)
    if problem is not None:
        return problem
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
            return _error(
                "text_too_long",
                f"{name} is longer than {MAX_TEXT_CHARS} characters",
                field=name,  # S2 spec §4: the S1 code with ``field``
            )
    needs_reason = (
        (any(change.remove_links.values()) if change.remove_links else False)
        or change.origin is not None
        or change.recorded_timing is not None
    )
    if needs_reason and not _given(change.reason):
        return _error(
            "reason_required",
            "reason is required to remove links or to fill in a declared value",
            field="reason",  # S2 spec §4: the S1 code with ``field``
        )
    return None


def _dedupe(items: Iterable[str]) -> list[str]:
    """``items`` without repeats, first appearance first (linear time)."""
    return list(dict.fromkeys(items))


def appends(change: LineageChange) -> bool:
    """Whether ``change`` adds a note, an evaluation or a fact core version.

    Those are what a caller can add without limit, so they stop at
    MAX_APPEND_BYTES (:func:`size_refusal`); S2's lineage-only writers do too
    (S2 spec §2.8, §5.2).
    """
    return _given(change.note) or _given(change.evaluation) or change.fact_core is not None


def size_refusal(
    decision_id: str, size: int | None, appends: bool, *, section: str | None = None
) -> dict | None:
    """The ``decision_lineage_too_large`` error for a rewritten lineage, or ``None``.

    Every change must leave the file within MAX_LINEAGE_BYTES (what readers
    accept). A write that appends (a note, an evaluation, a fact core version:
    :func:`appends`) must also leave it within MAX_APPEND_BYTES, so the last
    LINEAGE_RESERVE_BYTES stay free for
    lifecycle, link and declared changes: a lineage filled with notes can
    still be adopted, rejected or superseded (design §2.6). That room is not
    guaranteed, since repeated link and lifecycle changes use it too, and the
    remediation does not promise it.

    Args:
        decision_id: The ADR id.
        size: The new text's size in bytes as written (``storage.written_size``:
            UTF-8, with the platform's newlines, S2 spec §2.8); ``None`` when
            the capped dump gave up because it exceeds MAX_LINEAGE_BYTES.
        appends: Whether the write appends (:func:`appends` for a change).
        section: The S2 section a lineage-only writer appends to (``explanations``);
            such a write always appends, and its remediation names the items
            that may be moved out by hand (S2 spec §2.8).

    Returns:
        The error dict (with a remediation), or ``None`` when the size is fine.
    """
    path = f".pm/{LINEAGE_DIR}/{decision_id}.yaml"
    if section is not None and (size is None or size > MAX_APPEND_BYTES):
        return _error(
            LINEAGE_TOO_LARGE,
            f"{decision_id}: {section} are not added once the lineage would pass "
            f"{MAX_APPEND_BYTES} bytes, so that room is kept for lifecycle and link changes; "
            "nothing was written",
            remediation=(
                "Tell the user. Older explanation and feedback items can be moved out of "
                f"{path} by hand (for example into a file outside .pm/{LINEAGE_DIR}), keeping "
                "each section's last_number and items list and every event; then make the "
                "call again."
            ),
        )
    trim = (
        f"move older note and evaluation events out of {path} by hand (for example into a "
        f"file outside .pm/{LINEAGE_DIR}), keeping its lifecycle, link, declared and "
        "fact_core events"
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
    if appends and size > MAX_APPEND_BYTES:
        return _error(
            LINEAGE_TOO_LARGE,
            f"{decision_id}: notes, evaluations and fact core versions are not added once the "
            "lineage would pass "
            f"{MAX_APPEND_BYTES} bytes, so that room is kept for lifecycle and link changes; "
            "nothing was written",
            remediation=(
                "Lifecycle, link and declared changes usually still fit: make them in a call "
                "without note and evaluation, and without fact_core. Repeated link and "
                "lifecycle changes use up that room too, so one of them can be refused as "
                "well; then shorten its reason. A note, an evaluation or a fact core revision "
                "refused here is written only after the file is trimmed: to add more notes or "
                "evaluations, a new fact core version, or when even a short reason does not "
                f"fit, {trim}, then make the call again."
            ),
        )
    return None


def _keep_one_reason(events: list[dict]) -> None:
    """Keep a call's reason on the last of its lifecycle, link, declared and fact_core events.

    A call has one reason. Copying it to every event let one call with 50
    links and a 4,000-character reason fill most of the size cap, so it is
    stored once (design §2.4). It goes on the last such event because readers
    show the file's last RECENT_EVENTS events: a call's events are contiguous
    and its lifecycle, link, declared and fact_core events come before its
    evaluation, note and status_reprojected ones (the order of
    ``_EVENT_FIELDS``), so a window that shows any of them also shows the one
    with the reason. A fact_core event is never combined with the others (S2
    spec §3.1), so it always keeps its own reason (S2 spec §2.5).

    That event keeps ``reason`` even when the call gave none (an empty
    string; only add_links and fact_core can be called without one).
    Same-second calls share
    ``at`` and ``via``, so this is what tells them apart: a lifecycle, link or
    declared event without ``reason`` belongs to the call of the next event
    that has one.

    Args:
        events: The call's new events in file order; each lifecycle, link,
            declared and fact_core event carries the reason (possibly empty)
            on entry.
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


def _with_section(doc: dict, name: str, value: object, before: tuple[str, ...]) -> dict:
    """A copy of ``doc`` with ``name`` set; a new key goes before the first of ``before``.

    The other keys keep their order and values (S2 spec §2.1 shows the layout).
    """
    if name in doc:
        out = dict(doc)
        out[name] = value
        return out
    out = {}
    for key, item in doc.items():
        if name not in out and key in before:
            out[name] = value
        out[key] = item
    if name not in out:
        out[name] = value
    return out


def _with_fact_core(doc: dict, fact_core: dict) -> dict:
    """``doc`` with ``fact_core`` set; a new key goes before the S2 sections and events."""
    return _with_section(doc, "fact_core", fact_core, ("explanations", "feedback", "events"))


def _status_mismatch_notice(
    adr: str, status_label: str, lifecycle: str, projection: DecisionStatus
) -> dict:
    """``decision_status_mismatch``: a call that names no lifecycle left the status alone."""
    return notice(
        "warning",
        "decision_status_mismatch",
        f"{adr}: decisions.yaml says status={status_label}, but the lineage lifecycle "
        f"is {lifecycle} (status {_status_text(projection)}). This "
        "call left the status as it was.",
        f"Ask the user which one is right. To follow the lineage, call "
        f"pm_update_decision again with lifecycle={lifecycle}. To follow the "
        f"status, move the lifecycle to the matching value (allowed from "
        f"{lifecycle}: {', '.join(allowed_to(lifecycle)) or 'none'}).",
    )


def _explanation_entries(doc: Mapping) -> list | None:
    """The explanation entries an adoption looks at, or ``None`` when the section is broken.

    Broken as in the S2 spec §2.7 table: ``explanations`` that is not a
    mapping or whose ``items`` is not a list, or an entry that is not a
    mapping, has no id matching EXPLANATION_ID_RE or no ``fact_core_version``
    of 0 or more (a bool is not one). A missing section has no entries. Only
    the last SCAN_EXPLANATIONS entries are looked at (S2 spec §2.6).
    """
    part = doc.get("explanations")
    if part is None:
        return []
    entries = part.get("items") if isinstance(part, dict) else None
    if not isinstance(entries, list):
        return None
    scanned = entries[-SCAN_EXPLANATIONS:]
    if any(_explanation_entry_problem(entry) is not None for entry in scanned):
        return None
    return scanned


def _adoption_record(doc: Mapping, adr: str) -> tuple[int | None, dict | None]:
    """The fact core version an adoption records, and the info notice it carries.

    Only a fact core the writer could write (:func:`fact_core_refusal` is
    ``None``) is recorded, so the adoption fixes exactly what readers show
    in full (S2 spec §2.2, §3.1). An ADR without a fact core gets neither: its
    lifecycle event keeps the S1 shape. An ADR with one is adopted whatever
    its explanations hold; the notice only says what was checked (S2 spec §3.1).

    Returns:
        ``(version, None)`` when an explanation of the current version that is
        not stale exists; ``(version, decision_adopted_without_current_explanation)``
        when none does; ``decision_adoption_explanation_check_skipped`` (with
        the version, or ``None`` and "not fixed" for a fact core the writer
        refuses) when ``fact_core`` or ``explanations`` cannot be read; and
        ``(None, None)`` without a fact core.
    """
    fact_core = doc.get("fact_core")
    if fact_core is None:
        return None, None
    readable = fact_core_refusal(doc) is None
    entries = _explanation_entries(doc)
    if not readable or entries is None:
        broken = [
            name
            for name, ok in (("fact_core", readable), ("explanations", entries is not None))
            if not ok
        ]
        message = (
            f"{adr}: {' and '.join(broken)} in the lineage file cannot be read, so whether an "
            "explanation of the fact core's current version exists was not checked."
        )
        if not readable:
            message += (
                " The adopted version was not recorded, so this ADR's fact core is not fixed."
            )
        return (
            fact_core["version"] if readable else None,
            notice(
                "info",
                ADOPTION_EXPLANATION_CHECK_SKIPPED,
                message,
                "Tell the user. The adoption itself was recorded.",
            ),
        )
    current: int = fact_core["version"]
    # Explanations of the current version are stale only through a change of
    # F-revert outside the tools (S2 spec §2.3), so one check covers them all.
    outside = explanation_staleness(fact_core, current).stale
    if not outside and any(entry["fact_core_version"] == current for entry in entries):
        return current, None
    why = (
        "F-revert's reversibility was changed outside pmlens's tools, so its explanations are stale"
        if outside
        else "no explanation written from that version is on record"
    )
    return current, notice(
        "info",
        ADOPTED_WITHOUT_CURRENT_EXPLANATION,
        f"{adr}: it was adopted with version {current} of its fact core, but {why}; the "
        "user adopted it without a current explanation.",
        "Tell the user. The adoption itself was recorded. To have one, explain version "
        f"{current} to the user and save that explanation with pm_add_explanation.",
    )


def _resolve_fact_ids(
    adr: str,
    items: Sequence[Mapping[str, object]],
    stored: Mapping[str, dict],
    used_ids: Iterable[object],
    retired: set[str],
) -> list[tuple[str, Mapping[str, object]]] | dict:
    """Give every item of the fact_core argument its id (S2 spec §2.2).

    A numbered item passed by kind alone gets the highest number its kind has
    used (current, retired and previous ids alike) plus one; numbers are never
    reused. A numbered id must name a current item.

    Returns:
        ``[(id, item)]`` in argument order, or an error dict
        (``fact_core_id_unknown`` / ``fact_core_id_retired`` /
        ``fact_core_ids_exhausted``).
    """
    highest = dict.fromkeys(FACT_NUMBERED_KINDS, 0)
    for used in used_ids:
        if is_fact_id(used):
            number = _fact_number(used)
            if number is not None:
                kind = fact_kind(used)
                highest[kind] = max(highest[kind], number)
    resolved: list[tuple[str, Mapping[str, object]]] = []
    for index, item in enumerate(items):
        fact_id = item.get("id")
        kind = item.get("kind")
        if fact_id is None:
            if kind in FACT_SINGLE_KINDS:
                fact_id = f"F-{kind}"
            else:
                number = highest[kind] + 1
                if number > MAX_FACT_NUMBER:
                    return _error(
                        "fact_core_ids_exhausted",
                        f"{adr}: the {kind} items have used every number up to "
                        f"{MAX_FACT_NUMBER}, retired ones included, and numbers are never "
                        "reused",
                        item_index=index,
                    )
                highest[kind] = number
                fact_id = f"F-{kind}.{number}"
        elif _fact_number(fact_id) is not None and fact_id not in stored:
            if fact_id in retired:
                return _error(
                    "fact_core_id_retired",
                    f"{adr}: {fact_id} was retired and its number is never used again; pass "
                    "a new item with its kind and no id",
                    item_index=index,
                )
            return _error(
                "fact_core_id_unknown",
                f"{adr}: {fact_id} is not in the current fact core; pass a new item with its "
                "kind and no id",
                item_index=index,
            )
        resolved.append((fact_id, item))
    return resolved


def _previous_value(
    item: dict, last_version: int, cache: ScrubCache, reversibility: str | None = None
) -> dict:
    """A stored item as an entry of ``previous_values``, every value redacted.

    Every field of that version is copied (text, source, and confidence or
    reversibility for the item that owns it), redacted and cut as readers show
    them. ``reversibility`` replaces the stored one (an absorbed change made
    outside the tools records what the tool had written, S2 spec §2.2). The
    redactions are counted by :func:`_stored_hits`, once per stored item.

    Returns:
        The entry.
    """
    text, _, _ = _scrub_clip(item["text"], MAX_FACT_TEXT_CHARS, cache)
    source, _, _ = _scrub_clip(item.get("source", SOURCE_PROPOSAL), MAX_LABEL_CHARS, cache)
    entry: dict[str, object] = {
        "id": item["id"],
        "last_version": last_version,
        "text": text,
        "source": source,
    }
    if item["id"] == "F-uncertain":
        entry["confidence"], _, _ = _scrub_clip(item["confidence"], MAX_LABEL_CHARS, cache)
    if item["id"] == "F-revert":
        if reversibility is None:
            entry["reversibility"], _, _ = _scrub_clip(
                item["reversibility"], MAX_LABEL_CHARS, cache
            )
        else:
            entry["reversibility"] = reversibility
    return entry


def _stored_hits(item: dict, cache: ScrubCache) -> int:
    """How many redactions a stored item's values hold (S2 spec §3.1 step 7).

    Text, source, confidence and reversibility are each counted, whichever
    of them the item has; a usable item holds strings only (S2 spec §2.7).
    """
    return sum(
        cache.scrub(item[name])[1]
        for name in ("text", "source", "confidence", "reversibility")
        if isinstance(item.get(name), str)
    )


def _stored_differs(stored: dict, new: dict, cache: ScrubCache) -> bool:
    """Whether a new item's values differ from the stored item's, compared redacted.

    The stored text (and confidence or reversibility of the item that owns
    one) is redacted before the comparison: readers only ever show the
    redacted value, so handing back what was read is no change (S2 spec §2.2).
    ``source`` is never compared.
    """
    if cache.scrub(stored["text"])[0] != new["text"]:
        return True
    for name in ("confidence", "reversibility"):
        if name in new and cache.scrub(stored[name])[0] != new[name]:
            return True
    return False


def _has_feedback(doc: Mapping) -> bool:
    """Whether the lineage holds a feedback entry (read leniently)."""
    part = doc.get("feedback")
    entries = part.get("items") if isinstance(part, dict) else None
    return isinstance(entries, list) and any(isinstance(entry, dict) for entry in entries)


def _apply_fact_core(
    doc: dict, decision: Decision, change: LineageChange, now: str, *, started: bool
) -> ChangeOutcome:
    """Create or revise an ADR's fact core; pure, no I/O (S2 spec §2.2, §3.1).

    ``doc`` passed :func:`write_refusal` or was just started. Refusals come in
    the order of S2 spec §3.1: what the fact core writer reads (its section
    and the adopting event, §2.7), ``fact_core_after_adoption``, traces of a
    removed fact core, the version (D5), numbering and required items, the
    reversibility ratchet. A call that changes nothing is ``unchanged``,
    unless F-revert's reversibility was changed outside the tools: that is
    taken into a new version. The status is never projected (the lifecycle
    does not change), and nothing of ``doc`` is changed in place: the new
    document shares only what it does not change (copy-on-write, §2.7).

    Returns:
        The outcome; ``fact_core`` and ``changes["fact_core"]`` are set unless
        there is an error.
    """
    adr = decision.id
    lifecycle: str = doc["lifecycle"]
    stored_links = doc.get("links") if isinstance(doc.get("links"), dict) else {}
    links = {t: list(stored_links.get(t) or []) for t in LINK_TYPES}
    status_label = scrub_label(decision.status)
    notices: list[dict] = []
    if not started:
        projection = project_status(lifecycle, links["superseded_by"])
        if projection != decision.status:
            notices.append(_status_mismatch_notice(adr, status_label, lifecycle, projection))

    # 2. What the fact core writer reads must be whole (S2 spec §2.7).
    refusal = fact_core_refusal(doc)
    if refusal is not None:
        return ChangeOutcome(error=refusal)
    adopted, malformed, adoption = adopted_fact_core_version(doc)
    if malformed:
        position = (_scan_adoption(doc)[0] or 0) + 1
        return ChangeOutcome(
            error=_error(
                LINEAGE_UNREADABLE,
                f"{adr}: fact_core_version of entry {position} of events in the lineage file "
                "is not a whole number from 1 to the current version, so whether the fact "
                "core is fixed cannot be told; nothing was written.",
                remediation=(
                    f"Tell the user. Only the user may fix or remove that value in "
                    f"{_path_of(adr)} by hand (removing it lifts the fix). Do not edit the "
                    "lineage file yourself."
                ),
            )
        )
    caused_by = _dedupe(change.caused_by or ())
    if caused_by:
        # caused_by is looked up in the feedback section (S2 spec §2.7).
        refusal = _caused_by_feedback_refusal(doc, adr)
        if refusal is not None:
            return ChangeOutcome(error=refusal)
    fact_core: dict | None = doc.get("fact_core")
    current = fact_core["version"] if fact_core is not None else 0

    # 3. Fixed by the adoption (Q13).
    if _fixed_by_adoption(doc, adopted):
        return ChangeOutcome(
            error=_error(
                FACT_CORE_AFTER_ADOPTION,
                f"{adr}: the user adopted version {adopted} of its fact core, so the fact "
                f"core is fixed while the ADR is {lifecycle}; nothing was written.",
                adopted_fact_core_version=adopted,
                current_version=current,
                lifecycle=lifecycle,
                remediation=(
                    "Tell the user. Only if the user asks for the change, move the ADR back "
                    "to proposed with pm_update_decision (lifecycle=proposed and a reason), "
                    "revise the fact core, explain it again and let the user adopt it again. "
                    "Otherwise record the change as a new ADR linked with amends."
                ),
            )
        )

    # 4. A fact core removed by hand is not written over (S2 spec §2.2).
    if fact_core is None:
        where = _fact_core_traces(doc)
        if where is not None:
            return ChangeOutcome(
                error=_error(
                    LINEAGE_UNREADABLE,
                    f"{adr}: the lineage file has no fact_core, but its {where} refer to a "
                    "fact core version: fact_core was removed by hand, and a first version "
                    "is not written over that; nothing was written.",
                    remediation=(
                        f"Tell the user. Only the user may restore fact_core in "
                        f"{_path_of(adr)} (for example from version control). Do not edit "
                        "the lineage file yourself."
                    ),
                )
            )

    # 5. The version (D5).
    expected = change.expected_fact_core_version
    if expected != current:
        return ChangeOutcome(
            error=_error(
                FACT_CORE_VERSION_MISMATCH,
                f"{adr}: the fact core is at version {current}, not {expected}; nothing "
                "was written.",
                current_version=current,
                remediation=(
                    "Read the current fact core (pm_decision_query action=get), redo the "
                    "change on it, and pass its version as expected_fact_core_version."
                ),
            )
        )

    # 6. Numbering and the required items.
    stored_items: list[dict] = list(fact_core["items"]) if fact_core is not None else []
    stored = {item["id"]: item for item in stored_items}
    retired_before: list = list(fact_core.get("retired_ids") or []) if fact_core else []
    previous_before: list = list(fact_core.get("previous_values") or []) if fact_core else []
    items = change.fact_core or ()
    resolved = _resolve_fact_ids(
        adr,
        items,
        stored,
        [
            *stored,
            *retired_before,
            *(entry["id"] for entry in previous_before),
        ],
        set(retired_before),
    )
    if isinstance(resolved, dict):
        return ChangeOutcome(error=resolved)
    new_ids = [fact_id for fact_id, _ in resolved]
    # ``missing`` holds kinds, the values the argument's ``kind`` takes (S2 spec §2.2).
    missing = [fact_kind(fact_id) for fact_id in REQUIRED_FACT_IDS if fact_id not in new_ids]
    declared = doc.get("declared")
    decision_kind = declared.get("decision_kind") if isinstance(declared, dict) else None
    if decision_kind == DecisionKind.PREMISE_DEPENDENT and not any(
        fact_kind(fact_id) == "premise" for fact_id in new_ids
    ):
        missing.append("premise")
    if missing:
        return ChangeOutcome(
            error=_error(
                "fact_core_item_required",
                f"{adr}: the fact core needs an item of each kind: {', '.join(missing)}"
                + (
                    " (a premise_dependent ADR states at least one premise)"
                    if "premise" in missing
                    else ""
                ),
                missing=missing,
            )
        )
    refusal = _caused_by_refusal(doc, adr, caused_by) if caused_by else None
    if refusal is not None:
        return ChangeOutcome(error=refusal)

    # 7. Redact, then compare redacted values (S2 spec §2.2).
    new_version = current + 1
    cache = ScrubCache()
    hits = 0
    new_items: list[dict] = []
    added: list[str] = []
    edited: list[str] = []
    for fact_id, item in resolved:
        text, found = scrub_text(str(item["text"]))
        hits += found
        entry: dict[str, object] = {"id": fact_id, "text": text}
        # Plain strings: safe_dump rejects StrEnum members.
        if fact_id == "F-uncertain":
            entry["confidence"] = str(item["confidence"])
        if fact_id == "F-revert":
            entry["reversibility"] = str(item["reversibility"])
        entry["source"] = SOURCE_PROPOSAL
        old = stored.get(fact_id)
        if old is None:
            added.append(fact_id)
            entry["since"] = new_version
        elif _stored_differs(old, entry, cache):
            edited.append(fact_id)
            entry["since"] = new_version
        else:
            since = old.get("since")
            entry["since"] = since if _is_positive_int(since) and since <= current else current
        new_items.append(entry)
    removed = [fact_id for fact_id in stored if fact_id not in new_ids]
    new_revert = next(entry["reversibility"] for entry in new_items if entry["id"] == "F-revert")

    # 8. The reversibility ratchet (Q12), whatever caused the revision.
    if lowers_reversibility(fact_core, new_revert):
        # The base is named from the entry the floor read, not from ``stored``,
        # which also holds entries past the cap (S2 spec §2.2, §3.1 step 8).
        base = _floor_revert_value(fact_core)
        before = reversibility_label(base)
        if base is _MISSING:
            remediation = (
                "Tell the user. Keep asking a person able to judge before any irreversible "
                "effect. F-revert is not among the first "
                f"{MAX_FACT_ITEMS} items of the fact core in {_path_of(adr)}, so the floor "
                "does not read it; only the user may fix that file, when no assistant "
                "session is writing. Do not edit the lineage file yourself."
            )
        else:
            remediation = (
                "Tell the user. Keep asking a person able to judge before any "
                "irreversible effect. If such a person says it can be reverted in code, "
                "record their words with pm_add_feedback; only the user may change the "
                f"one reversibility line in {_path_of(adr)}, when no assistant session is "
                "writing. Do not edit the lineage file yourself."
            )
        return ChangeOutcome(
            error=_error(
                FACT_CORE_LOWERING_REFUSED,
                f"This tool cannot lower F-revert's reversibility from {before} to "
                f"{CODE_REVERTABLE}; the floor that asks a person able to judge stays on.",
                **{"from": before, "to": CODE_REVERTABLE},
                remediation=remediation,
            )
        )

    # 9. Unchanged, or a new version taking in a change made outside the tools.
    recorded = fact_core.get("recorded_reversibility") if fact_core is not None else None
    outside = fact_core is not None and _changed_outside_tools(
        recorded, stored["F-revert"]["reversibility"]
    )
    summary_changes = {
        "from_version": current,
        "to_version": current,
        "changed_ids": [],
        "added_ids": [],
        "retired_ids": [],
    }
    outcome = ChangeOutcome(
        status_before=status_label,
        new_status=_status_text(decision.status),
        lifecycle=lifecycle,
        links=links,
        notices=notices,
        changes={"fact_core": summary_changes},
        fact_core={"version": current, "ids": list(stored)},
    )
    if not (added or edited or removed or outside):
        # Nothing is written, but the secrets removed from the items passed in
        # still count: the caller pasted them and may need to revoke them
        # (S2 spec §3.1 step 7). The reason is not stored, so it is not counted.
        if hits:
            notices.append(
                notice(
                    "warning",
                    LINEAGE_SECRETS_REDACTED,
                    f"{hits} secret-like string(s) were removed from the fact core items "
                    "passed in; nothing was written.",
                    "If a real credential was pasted, revoke it.",
                )
            )
        return outcome

    if outside:
        for entry in new_items:
            if entry["id"] == "F-revert":
                entry["since"] = new_version
    previous_added: list[dict] = []
    for item in stored_items:
        fact_id = item["id"]
        # Every stored value leaves the file redacted: written again, copied
        # to previous_values, or (a kept item's source) replaced by proposal.
        # Each is counted once (S2 spec §3.1 step 7).
        hits += _stored_hits(item, cache)
        if fact_id == "F-revert" and outside and recorded in REVERSIBILITIES:
            previous_added.append(_previous_value(item, current, cache, reversibility=recorded))
        if fact_id in edited or fact_id in removed:
            previous_added.append(_previous_value(item, current, cache))
    changed_ids = [
        entry["id"]
        for entry in new_items
        if entry["id"] in added or entry["id"] in edited or (outside and entry["id"] == "F-revert")
    ] + removed
    retired_now = [fact_id for fact_id in removed if _fact_number(fact_id) is not None]

    new_fact_core = dict(fact_core) if fact_core is not None else {}
    new_fact_core["version"] = new_version
    new_fact_core["recorded_reversibility"] = new_revert
    new_fact_core["items"] = new_items
    new_fact_core["retired_ids"] = retired_before + retired_now
    new_fact_core["previous_values"] = previous_before + previous_added

    reason_text, reason_hits = scrub_text(change.reason) if _given(change.reason) else ("", 0)
    hits += reason_hits
    event: dict[str, object] = {
        "at": now,
        "kind": "fact_core",
        "version": new_version,
        "changed_ids": changed_ids,
    }
    if caused_by:
        event["caused_by"] = caused_by
    event["reason"] = reason_text
    if outside:
        event["edited_outside_tools"] = ["F-revert"]
    event["via"] = change.via
    _keep_one_reason([event])

    new_doc = _with_fact_core(doc, new_fact_core)
    if doc.get("anchor") is None:
        new_doc = _with_anchor(new_doc, decision)
        notices.append(
            notice(
                "info",
                LINEAGE_ANCHOR_MISSING,
                f"{adr}: the lineage had no anchor; it is now tied to the ADR's current date "
                "and title.",
            )
        )
    new_doc["events"] = list(doc.get("events") or []) + [event]

    if started:
        notices.append(
            notice(
                "info",
                LINEAGE_STARTED,
                f"{adr} had no lineage; one was started from its status. When it was recorded "
                "and who decided it are not recorded.",
            )
        )
    if adopted is None and adoption != ADOPTION_NOT_ADOPTED:
        if adoption == ADOPTION_UNKNOWN:
            message = (
                f"{adr}: whether this ADR was ever adopted is not known from its record (its "
                "lineage was started from its status); the fact core just written is not "
                "what the user accepted."
            )
        elif fact_core is None:
            message = (
                f"{adr} was adopted before it had a fact core; these facts were written by an "
                "assistant after the adoption and are not what the user accepted."
            )
        else:
            message = (
                f"{adr} was adopted without a recorded fact core; this revision was written "
                "after the adoption and is not what the user accepted."
            )
        notices.append(
            notice(
                "info",
                FACT_CORE_CHANGED_AFTER_ADOPTION,
                message,
                "Show the user what you wrote and check it with them. To fix these facts, the "
                "user may move the ADR back to proposed and adopt it again.",
            )
        )
    # D6: the explanations of the replaced version are stale from now on.
    stale = _stale_explanations_notice(doc, adr, current, new_version)
    if stale is not None:
        notices.append(stale)
    if fact_core is None:
        if new_revert == CODE_REVERTABLE and (
            lifecycle != DecisionLifecycle.PROPOSED or _has_feedback(doc)
        ):
            notices.append(
                notice(
                    "info",
                    REVERSIBILITY_DECLARED_AFTER_REVIEW,
                    f"{adr}: the first fact core says F-revert is {CODE_REVERTABLE}, declared "
                    "by an assistant after the user had seen this ADR. No floor applies to "
                    "it, and this tool cannot lower it again once raised.",
                    "Tell the user.",
                )
            )
        if not any(fact_kind(fact_id) == "conditions" for fact_id in new_ids):
            notices.append(
                notice(
                    "info",
                    FACT_CORE_NO_CONDITIONS,
                    f"{adr}: the first fact core has no F-conditions item.",
                    "Record each condition the proposal or the user stated as its own "
                    "conditions item; never invent one. Ask the user whether a condition is "
                    "missing.",
                )
            )
    if hits:
        notices.append(
            notice(
                "warning",
                LINEAGE_SECRETS_REDACTED,
                f"{hits} secret-like string(s) were removed from the fact core before saving.",
                "If a real credential was pasted, revoke it.",
            )
        )
    summary_changes.update(
        to_version=new_version,
        changed_ids=changed_ids,
        added_ids=added,
        retired_ids=retired_now,
    )
    if outside:
        summary_changes["edited_outside_tools"] = ["F-revert"]
    outcome.changed = True
    outcome.doc = new_doc
    outcome.events_added = (["lineage_started"] if started else []) + ["fact_core"]
    outcome.fact_core = {"version": new_version, "ids": new_ids}
    return outcome


def apply_change(
    doc: dict | None,
    decision: Decision,
    change: LineageChange,
    known_ids: Iterable[str],
    now: str,
) -> ChangeOutcome:
    """Apply one lineage change to a loaded document; pure, no I/O (design §4.3).

    A call with ``fact_core`` only creates or revises the fact core
    (:func:`_apply_fact_core`, S2 spec §3.1). Otherwise the order is:
    declared backfill, links, lifecycle, evaluation, note. Events are
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
    if change.fact_core is not None:
        return _apply_fact_core(doc, decision, change, now, started=started)
    # An evaluation with caused_by looks at the feedback section too; other
    # calls of the S1 path never do (S2 spec §2.7).
    caused_by = _dedupe(change.caused_by or ())
    if caused_by:
        refusal = _caused_by_feedback_refusal(doc, adr) or _caused_by_refusal(doc, adr, caused_by)
        if refusal is not None:
            return ChangeOutcome(error=refusal)

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
                error=_error(
                    "reason_required", "reason is required to change the lifecycle", field="reason"
                )
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
            _status_mismatch_notice(adr, status_label, lifecycle_before, lineage_projection)
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
        event: dict = {
            "at": now,
            "kind": "lifecycle",
            "from": lifecycle_before,
            "to": lifecycle_after,
            "status": new_status_value,
            "reason": reason_text,
        }
        if lifecycle_after == DecisionLifecycle.ADOPTED:
            # An adoption fixes the fact core it was made on (Q13, S2 spec §3.1).
            version, adoption_notice = _adoption_record(doc, adr)
            if version is not None:
                event["fact_core_version"] = version
            if adoption_notice is not None:
                notices.append(adoption_notice)
        event["via"] = via
        buckets["lifecycle"].append(event)
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
        event = {
            "at": now,
            "kind": "evaluation",
            "evaluation_kind": change.evaluation_kind,
            "text": text,
        }
        if caused_by:
            event["caused_by"] = caused_by
        event["via"] = via
        buckets["evaluation"].append(event)
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
                LINEAGE_STARTED,
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
                LINEAGE_SECRETS_REDACTED,
                f"{redactions} secret-like string(s) were removed from reason / note / "
                "evaluation before saving.",
                "If a real credential was pasted, revoke it.",
            )
        )
    outcome.doc = new_doc
    outcome.events_added = events_added
    outcome.changes = changes
    return outcome


# ─── Lineage-only writers (S2 spec §5.1) ─────────────


@dataclass
class AppendOutcome:
    """Result of a lineage-only writer's pure step (:func:`add_explanation_doc`,
    :func:`add_feedback_doc`).

    Attributes:
        error: An error dict; when set nothing must be written.
        doc: The new lineage document (``None`` on an error).
        fields: The writer's own response fields; for an explanation
            ``explanation_id``, ``fact_core_version``, ``fact_ids_present``,
            ``accuracy`` and ``paragraphs`` (S2 spec §3.2); for feedback
            ``feedback_id``, ``feedback_kind``, ``fact_core_version``,
            ``respondent_scope``, ``review`` and ``next`` (S2 spec §3.4).
        notices: Warning entries (``server._build_warning`` shape).
    """

    error: dict | None = None
    doc: dict | None = None
    fields: dict = field(default_factory=dict)
    notices: list[dict] = field(default_factory=list)


def fact_core_missing(adr: str) -> dict:
    """``decision_fact_core_missing``: an explanation needs a fact core (S2 spec §3.2 step 3)."""
    return _error(
        DECISION_FACT_CORE_MISSING,
        f"{adr} has no fact core, so there is nothing an explanation can be written from; "
        "nothing was written.",
        remediation=(
            "Record the facts first with pm_update_decision (fact_core, with "
            "expected_fact_core_version=0), then explain that version to the user."
        ),
    )


def add_explanation_doc(
    doc: dict,
    decision: Decision,
    now: str,
    *,
    fact_core_version: object,
    paragraphs: object,
    question: object = None,
) -> AppendOutcome:
    """Append one explanation to an ADR's lineage; pure, no I/O (S2 spec §3.2 steps 3-5).

    ``doc`` passed :func:`write_refusal` under the ADR's lineage lock. In this
    order: the sections the writer reads must be whole (the fact core through
    :func:`fact_core_refusal`, the explanations through
    :func:`_explanations_refusal`, S2 spec §2.7), the version must be the
    current one (``fact_core_version_mismatch`` with ``current_version``),
    the explanation must cover that version (:func:`explanation_refusal`, D4),
    and the next number must fit EXPLANATION_ID_RE. The texts are scrubbed,
    and the entry is numbered ``ADR-n.EX-k`` with ``k = max(last_number,
    numbers on record) + 1``. Only ``fact_ids_present`` is stored about the
    checks; the accuracy is never stored. Nothing of ``doc`` is changed in
    place (copy-on-write, S2 spec §2.7): the explanations mapping and its
    items list are new objects, so a value shared through YAML aliases keeps
    its value everywhere else.

    Args:
        doc: The lineage document as loaded.
        decision: The ADR from decisions.yaml.
        now: The timestamp of the entry.
        fact_core_version: The version the explanation was written from.
        paragraphs: ``[{fact_ids, text}]`` as passed.
        question: ``{question_kind, fact_ids, text}`` as passed, or ``None``.

    Returns:
        The outcome; ``doc`` and ``fields`` are set unless there is an error.
    """
    adr = decision.id
    if not _is_positive_int(fact_core_version):
        return AppendOutcome(
            error=_error(
                "invalid_fact_core_version", "fact_core_version must be a whole number of 1 or more"
            )
        )
    fact_core = doc.get("fact_core")
    if fact_core is None:
        return AppendOutcome(error=fact_core_missing(adr))
    refusal = fact_core_refusal(doc, writing="an explanation") or _explanations_refusal(doc, adr)
    if refusal is not None:
        return AppendOutcome(error=refusal)
    current: int = fact_core["version"]
    if fact_core_version != current:
        return AppendOutcome(
            error=_error(
                FACT_CORE_VERSION_MISMATCH,
                f"{adr}: the fact core is at version {current}, not {fact_core_version}; "
                "nothing was written.",
                current_version=current,
                remediation=(
                    "Read the current fact core (pm_decision_query action=get), explain that "
                    "version to the user, and save that explanation with its version."
                ),
            )
        )
    refusal = explanation_refusal(fact_core, paragraphs, question)
    if refusal is not None:
        return AppendOutcome(error=refusal)
    part = doc.get("explanations")
    section: dict = dict(part) if isinstance(part, dict) else {}
    entries: list = list(section.get("items", []))
    number = max([section.get("last_number", 0), *(_entry_number(e["id"]) for e in entries)])
    number += 1
    if number > MAX_ENTRY_NUMBER:
        return AppendOutcome(
            error=_error(
                EXPLANATION_IDS_EXHAUSTED,
                f"{adr}: explanation numbers run out at {MAX_ENTRY_NUMBER}; nothing was written.",
                remediation="Tell the user. Record a new ADR that amends this one instead.",
            )
        )

    # explanation_refusal checked every shape read below.
    hits = 0
    stored: list[dict] = []
    for paragraph in paragraphs:
        text, count = scrub_text(paragraph["text"])
        hits += count
        stored.append({"fact_ids": [str(i) for i in paragraph["fact_ids"]], "text": text})
    entry_id = f"{adr}.EX-{number}"
    entry: dict[str, object] = {
        "id": entry_id,
        "at": now,
        "via": EXPLANATION_VIA,
        "fact_core_version": current,
        "fact_ids_present": True,
        "paragraphs": stored,
    }
    if question is not None:
        text, count = scrub_text(question["text"])
        hits += count
        entry["question"] = {
            "question_kind": str(question["question_kind"]),
            "fact_ids": [str(i) for i in question["fact_ids"]],
            "text": text,
        }
    section["last_number"] = number
    section["items"] = entries + [entry]

    notices: list[dict] = []
    new_doc = _with_section(doc, "explanations", section, ("feedback", "events"))
    if doc.get("anchor") is None:
        new_doc = _with_anchor(new_doc, decision)
        notices.append(
            notice(
                "info",
                LINEAGE_ANCHOR_MISSING,
                f"{adr}: the lineage had no anchor; it is now tied to the ADR's current date "
                "and title.",
            )
        )
    usable, _ = _readable_items(fact_core["items"])
    recorded = fact_core.get("recorded_reversibility")
    revert = _current_revert_value(fact_core["items"], usable)
    if _changed_outside_tools(recorded, revert):
        adopted, malformed, _ = adopted_fact_core_version(doc)
        if malformed or _fixed_by_adoption(doc, adopted):
            remediation = _OUTSIDE_CHANGE_FIXED_REMEDIATION
        else:
            remediation = (
                "Tell the user. Revise the fact core with pm_update_decision first, which "
                "records the change in a new version, then explain that version and save it."
            )
        notices.append(
            notice(
                "info",
                REVERSIBILITY_CHANGED_OUTSIDE_TOOLS,
                f"{adr}: F-revert's reversibility was changed outside pmlens's tools (recorded "
                f"{reversibility_label(recorded)}, now {reversibility_label(revert)}), so this "
                "explanation was saved but is shown as stale.",
                remediation,
            )
        )
    if hits:
        notices.append(
            notice(
                "warning",
                LINEAGE_SECRETS_REDACTED,
                f"{hits} secret-like string(s) were removed from the explanation before saving.",
                "If a real credential was pasted, revoke it.",
            )
        )
    return AppendOutcome(
        doc=new_doc,
        fields={
            "explanation_id": entry_id,
            "fact_core_version": current,
            "fact_ids_present": True,
            "accuracy": ACCURACY_UNCONFIRMED,
            "paragraphs": len(stored),
        },
        notices=notices,
    )


# The next step after a reply, per kind (S2 spec §3.4). Only the feedback id
# pmlens assigned and the fact core version read under the lock are filled
# in; nothing read from the file and no text passed goes into it.
_REVISE_CALL = (
    "pm_update_decision (fact_core, expected_fact_core_version={version}, "
    'caused_by=["{feedback_id}"])'
)
_FEEDBACK_NEXT: dict[str, str] = {
    FeedbackKind.PREMISE_MISMATCH.value: (
        f"If the premise is wrong, revise the fact core with {_REVISE_CALL} and explain the "
        "new version."
    ),
    FeedbackKind.WHAT_IF.value: (
        f"If the what-if changes the facts, revise the fact core with {_REVISE_CALL} and "
        "explain the new version; otherwise answer it in the conversation."
    ),
    FeedbackKind.UNACCEPTABLE.value: (
        "Talk the objection through with the user: revise the fact core with "
        f"{_REVISE_CALL} and explain the new version, or weigh an alternative together."
    ),
    FeedbackKind.TRY_ALTERNATIVE.value: (
        f"To weigh the alternative, add it as an alt item by revising the fact core with "
        f"{_REVISE_CALL} and explain the new version, or record it as a new ADR."
    ),
    FeedbackKind.APPROVE.value: (
        "Approval recorded here does not adopt the ADR. Only if the user has decided to "
        "adopt it, set lifecycle=adopted with pm_update_decision and a reason."
    ),
    FeedbackKind.REJECT.value: (
        "Rejection recorded here does not reject the ADR. Only if the user has decided, "
        "set the lifecycle with pm_update_decision (rejected for a proposed ADR) and a reason."
    ),
}
# The four kinds above that point to a revision, once the adoption fixed the
# fact core (or whether it did cannot be read): a revision would be refused.
_FEEDBACK_NEXT_FIXED = (
    "The fact core of this ADR is fixed since the user adopted it. Tell the user; only if "
    "the user asks, move it back to proposed with pm_update_decision and revise, or record "
    "the change as a new ADR with amends."
)
_REVISING_FEEDBACK_KINDS = frozenset(
    {
        FeedbackKind.PREMISE_MISMATCH.value,
        FeedbackKind.WHAT_IF.value,
        FeedbackKind.UNACCEPTABLE.value,
        FeedbackKind.TRY_ALTERNATIVE.value,
    }
)


def feedback_next(kind: str, feedback_id: str, version: int, *, fixed: bool) -> str | None:
    """The fixed ``next`` template of a recorded reply (S2 spec §3.4).

    Args:
        kind: A value of FEEDBACK_KINDS.
        feedback_id: The id pmlens assigned (matches FEEDBACK_ID_RE).
        version: The fact core version read under the lock.
        fixed: Whether the adoption fixed the fact core, or that cannot be
            read; the four kinds that point to a revision then point to the
            user instead.

    Returns:
        The text; ``None`` for check_answer and comment.
    """
    if fixed and kind in _REVISING_FEEDBACK_KINDS:
        return _FEEDBACK_NEXT_FIXED
    template = _FEEDBACK_NEXT.get(kind)
    if template is None:
        return None
    return template.format(feedback_id=feedback_id, version=version)


def add_feedback_doc(
    doc: dict,
    decision: Decision,
    now: str,
    *,
    feedback_kind: object,
    text: object,
    explanation_id: object = None,
    fact_ids: object = None,
    respondent_scope: object = RespondentScope.UNKNOWN.value,
) -> AppendOutcome:
    """Append what the user said about an ADR to its lineage; pure, no I/O (S2 spec §3.4).

    ``doc`` passed :func:`write_refusal` under the ADR's lineage lock (or was
    just started). After the argument checks (:func:`feedback_call_error`),
    in this order: the fact core must be whole, whether or not the call names
    an explanation or facts, since the reply is recorded against its version
    (:func:`fact_core_refusal`; a missing fact core is version 0); the
    feedback section must be whole; a named explanation must be on record
    (``explanation_not_found``) and, for check_answer, end with a question of
    a QUESTION_KINDS kind (``check_answer_without_question``; only the
    vocabulary value is copied, never the stored one); the fact ids must be
    facts of the current version (``feedback_fact_ids_unknown``); and the
    next number must fit FEEDBACK_ID_RE. The text is scrubbed and the entry
    numbered ``ADR-n.FB-k`` with ``k = max(last_number, numbers on record) +
    1``. The declared respondent scope is stored as declared and never
    checked; the review label derived from it never says the reply was
    checked, and nothing is scored. The lifecycle and the status never
    change, approve and reject included. Nothing of ``doc`` is changed in
    place (copy-on-write, S2 spec §2.7).

    Args:
        doc: The lineage document as loaded.
        decision: The ADR from decisions.yaml.
        now: The timestamp of the entry.
        feedback_kind: One of FEEDBACK_KINDS.
        text: The user's words.
        explanation_id: The explanation replied to (required for check_answer).
        fact_ids: The facts of the current version the reply is about.
        respondent_scope: One of RESPONDENT_SCOPES, as declared.

    Returns:
        The outcome; ``doc`` and ``fields`` (``feedback_id``,
        ``feedback_kind``, ``fact_core_version``, ``respondent_scope``,
        ``review`` and ``next``) are set unless there is an error.
    """
    adr = decision.id
    problem = feedback_call_error(
        adr, feedback_kind, text, explanation_id, fact_ids, respondent_scope
    )
    if problem is not None:
        return AppendOutcome(error=problem)
    # feedback_call_error checked every type and vocabulary read below.
    kind = FeedbackKind(str(feedback_kind)).value
    scope = RespondentScope(str(respondent_scope)).value
    words = str(text)
    explained = str(explanation_id) if explanation_id is not None else None
    ids = [str(fact_id) for fact_id in fact_ids] if isinstance(fact_ids, list) else []

    refusal = fact_core_refusal(doc, writing="feedback") or _feedback_refusal(
        doc, adr, purpose="feedback cannot be written", numbering=True
    )
    if refusal is not None:
        return AppendOutcome(error=refusal)
    question_kind: str | None = None
    if explained is not None:
        refusal = _explanations_refusal(doc, adr, writing="feedback", numbering=False)
        if refusal is not None:
            return AppendOutcome(error=refusal)
        part = doc.get("explanations")
        entries: list = part["items"] if isinstance(part, dict) else []
        found = [entry for entry in entries if entry["id"] == explained]
        if not found:
            return AppendOutcome(
                error=_error(
                    EXPLANATION_NOT_FOUND,
                    f"{adr}: no explanation {explained} is on record; nothing was written.",
                    remediation=(
                        "Pass the explanation_id pm_add_explanation returned, or leave it out "
                        "when the reply was not about a saved explanation."
                    ),
                )
            )
        if kind == FeedbackKind.CHECK_ANSWER:
            question = found[-1].get("question")
            stored = question.get("question_kind") if isinstance(question, dict) else None
            # The vocabulary's own value is copied, never the file's (S2 spec §2.4).
            question_kind = next(
                (value for value in QUESTION_KINDS if isinstance(stored, str) and value == stored),
                None,
            )
            if question_kind is None:
                return AppendOutcome(
                    error=_error(
                        CHECK_ANSWER_WITHOUT_QUESTION,
                        f"{adr}: explanation {explained} does not end with a question pmlens "
                        "can read, so there is no answer to record; nothing was written.",
                        remediation=(
                            "Record the reply as a comment instead, or explain again with a "
                            "question and record the answer to that explanation."
                        ),
                    )
                )
    fact_core = doc.get("fact_core")
    current: int = fact_core["version"] if fact_core is not None else 0
    if ids:
        known = set(_current_fact_ids(fact_core))
        unknown = [fact_id for fact_id in ids if fact_id not in known]
        if unknown:
            # The ids matched FACT_ID_RE in feedback_call_error, so they can be named.
            return AppendOutcome(
                error=_error(
                    FEEDBACK_FACT_IDS_UNKNOWN,
                    f"{adr}: these ids are not facts of version {current} of the fact core: "
                    f"{', '.join(unknown)}; nothing was written.",
                    unknown_fact_ids=unknown,
                    current_version=current,
                    remediation=(
                        "Read the current fact core (pm_decision_query action=get) and pass "
                        "only its ids, or leave fact_ids out."
                    ),
                )
            )

    part = doc.get("feedback")
    section: dict = dict(part) if isinstance(part, dict) else {}
    entries = list(section.get("items", []))
    number = max([section.get("last_number", 0), *(_entry_number(e["id"]) for e in entries)])
    number += 1
    if number > MAX_ENTRY_NUMBER:
        return AppendOutcome(
            error=_error(
                FEEDBACK_IDS_EXHAUSTED,
                f"{adr}: feedback numbers run out at {MAX_ENTRY_NUMBER}; nothing was written.",
                remediation="Tell the user. Record a new ADR that amends this one instead.",
            )
        )
    stored_text, hits = scrub_text(words)
    feedback_id = f"{adr}.FB-{number}"
    entry: dict[str, object] = {
        "id": feedback_id,
        "at": now,
        "via": FEEDBACK_VIA,
        "feedback_kind": kind,
        "text": stored_text,
        "fact_core_version": current,
    }
    if explained is not None:
        entry["explanation_id"] = explained
    if ids:
        entry["fact_ids"] = ids
    entry["respondent_scope"] = scope
    if question_kind is not None:
        entry["question_kind"] = question_kind
    section["last_number"] = number
    section["items"] = entries + [entry]

    notices: list[dict] = []
    new_doc = _with_section(doc, "feedback", section, ("events",))
    if doc.get("anchor") is None:
        new_doc = _with_anchor(new_doc, decision)
        notices.append(
            notice(
                "info",
                LINEAGE_ANCHOR_MISSING,
                f"{adr}: the lineage had no anchor; it is now tied to the ADR's current date "
                "and title.",
            )
        )
    if kind in LIFECYCLE_FEEDBACK_KINDS:
        # write_refusal passed, so the lifecycle is a vocabulary value.
        notices.append(
            notice(
                "info",
                FEEDBACK_DOES_NOT_CHANGE_LIFECYCLE,
                f"{adr}: the {kind} was recorded as feedback {feedback_id}; the ADR is still "
                f"{doc['lifecycle']} and its status did not change.",
                "Adopt or reject the ADR only as the user decides, with pm_update_decision "
                "(lifecycle and a reason).",
            )
        )
    if kind in JUDGED_FEEDBACK_KINDS:
        if scope == CAN_JUDGE:
            notices.append(
                notice(
                    "info",
                    RESPONDENT_DECLARED_CAN_JUDGE,
                    f"{feedback_id} was recorded as the reply of a person declared able to "
                    "judge this kind of decision; pmlens has not checked that declaration.",
                    "Tell the user how the reply was recorded.",
                )
            )
        else:
            notices.append(
                notice(
                    "info",
                    NO_REVIEW_BY_ABLE_PERSON,
                    f"{feedback_id} was recorded with no review by a person able to judge this "
                    f"kind of decision (respondent_scope {scope}).",
                    "Tell the user. Until such a person replies, lean as the brief's posture "
                    "says; or ask a person able to judge this kind of decision and record "
                    "their reply.",
                )
            )
    if hits:
        notices.append(
            notice(
                "warning",
                LINEAGE_SECRETS_REDACTED,
                f"{hits} secret-like string(s) were removed from the feedback before saving.",
                "If a real credential was pasted, revoke it.",
            )
        )
    adopted, malformed, _ = adopted_fact_core_version(doc)
    fixed = malformed or _fixed_by_adoption(doc, adopted)
    return AppendOutcome(
        doc=new_doc,
        fields={
            "feedback_id": feedback_id,
            "feedback_kind": kind,
            "fact_core_version": current,
            "respondent_scope": scope,
            "review": review_label(scope),
            "next": feedback_next(kind, feedback_id, current, fixed=fixed),
        },
        notices=notices,
    )
