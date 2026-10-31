"""The project's decision posture: ``.pm/decision_policy.yaml`` (ADR-058, S2 spec §2.10).

The posture says how the assistant leans when the user cannot judge a kind of
decision: bold (new or exploratory work), cautious (maintenance and fixes) or
unset (not chosen yet), which acts as cautious. The file holds the value and
an append-only history of changes::

    schema: 1
    decision_posture: cautious
    history:
    - {at: '…Z', from: unset, to: bold, reason: '', via: pm_init}

It is a file of its own, not a key of project.yaml, so an older pmlens that
rewrites project.yaml cannot drop it, and every key this version does not know
(a later policy, say) is kept by value when the posture changes.

Like lineage.py, this module never writes and never locks. It reads the file
through lineage's bounded reader (:func:`lineage.read_bounded_yaml`: no link is
followed, a FIFO does not block the open, at most MAX_POLICY_BYTES) and builds
values: the lenient view every reader shows (:func:`read_decision_policy`,
:func:`policy_view`) and, for the one writer
``storage.change_decision_posture``, its refusals and the next document
(:func:`posture_write_refusal`, :func:`posture_changed_doc`). Nothing built here
quotes an exception's text, and a stored value is shown only redacted and cut
(:func:`lineage.scrub_label_counted`). tests/test_lineage.py holds this module
to lineage.py's import and no-write rules.

"S2 spec §N" below refers to docs/issues/DESIGN_decision-lineage-s2.md.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from .lineage import (
    MAX_LABEL_CHARS,
    ScrubCache,
    _error,
    notice,
    read_bounded_yaml,
    scrub_label_counted,
)
from .models import DecisionPosture

# ─── Constants ───────────────────────────────────────

POLICY_FILE = "decision_policy.yaml"
POLICY_SCHEMA = 1
# One cap for the reader and the writer, compared with the bytes on disk
# (storage.written_size): a write accepted at the cap must read back.
MAX_POLICY_BYTES = 64 * 1024
MAX_REASON_CHARS = 1_000
# The history is read from its end and at most this many entries are looked
# at, whatever the file repeats through aliases (S2 spec §2.10).
MAX_HISTORY_SCAN = 1_000
# The last change's reason is shown longer than the other fields (S2 spec §3.9).
LAST_REASON_CHARS = 200
HISTORY_FIELDS: tuple[str, ...] = ("at", "from", "to", "reason", "via")

POSTURES: tuple[str, ...] = tuple(v.value for v in DecisionPosture)
BOLD = DecisionPosture.BOLD.value
CAUTIOUS = DecisionPosture.CAUTIOUS.value
UNSET = DecisionPosture.UNSET.value

# What the file was found to be (PolicyView.state).
STATE_MISSING = "missing"
STATE_READ = "read"
STATE_UNREADABLE = "unreadable"

# Warning and error codes (S2 spec §4).
INVALID_POSTURE = "invalid_decision_posture"
POLICY_UNREADABLE = "decision_policy_unreadable"
POLICY_SCHEMA_UNSUPPORTED = "decision_policy_schema_unsupported"
POLICY_TOO_LARGE = "decision_policy_too_large"
POLICY_SECRETS_REDACTED = "decision_policy_secrets_redacted"
POSTURE_UNKNOWN = "decision_posture_unknown"
POSTURE_HISTORY_MISMATCH = "decision_posture_history_mismatch"
POSTURE_CHANGED = "decision_posture_changed"
POSTURE_UNSET = "decision_posture_unset"
POSTURE_NOT_CHANGED = "decision_posture_not_changed"
POSTURE_NOT_SET = "decision_posture_not_set"


# ─── Reading ─────────────────────────────────────────


@dataclass(frozen=True)
class RawPolicy:
    """What :func:`read_policy_raw` found.

    Attributes:
        exists: Whether something is at ``.pm/decision_policy.yaml``.
        data: The parsed mapping when it could be read.
        unreadable: Whether something is there that cannot be used: a link,
            not a regular file, over MAX_POLICY_BYTES, an OS error, invalid
            UTF-8 or YAML, nested more than MAX_LINEAGE_DEPTH levels, or not a
            mapping.
        detail: A content-free reason when it is unreadable.
    """

    exists: bool
    data: dict | None = None
    unreadable: bool = False
    detail: str | None = None


def read_policy_raw(pm_path: Path) -> RawPolicy:
    """Read ``.pm/decision_policy.yaml`` without locking, creating or raising.

    Args:
        pm_path: The project's ``.pm`` directory.

    Returns:
        The raw result; :func:`policy_view` turns it into what readers show.
    """
    path = pm_path / POLICY_FILE
    try:
        # O_NOFOLLOW refuses a link where the platform has it; this check
        # covers the rest (a Windows symlink or junction).
        linked = path.is_symlink()
    except OSError:
        linked = False  # the bounded read below reports what is wrong
    if linked:
        return RawPolicy(exists=True, unreadable=True, detail="a symbolic link")
    found = read_bounded_yaml(path, MAX_POLICY_BYTES)
    if not found.exists:
        return RawPolicy(exists=False)
    if found.unreadable:
        return RawPolicy(exists=True, unreadable=True, detail=found.detail)
    if not isinstance(found.data, dict):
        return RawPolicy(exists=True, unreadable=True, detail="not a mapping")
    return RawPolicy(exists=True, data=found.data)


def schema_unsupported(doc: Mapping) -> bool:
    """Whether ``schema`` is present and not an int up to POLICY_SCHEMA (bool excluded)."""
    if "schema" not in doc:
        return False
    schema = doc["schema"]
    return not isinstance(schema, int) or isinstance(schema, bool) or schema > POLICY_SCHEMA


def stored_posture(doc: Mapping | None) -> object:
    """The ``decision_posture`` value as stored; unset when there is no file or no value.

    A key holding null counts as no value. Anything else comes back as it is
    (an unknown string, bytes, a mapping …) for the caller to label.
    """
    if doc is None:
        return UNSET
    value = doc.get("decision_posture")
    return UNSET if value is None else value


def posture_label(value: object, cache: ScrubCache | None = None) -> tuple[str | None, int]:
    """A stored posture as it may be shown or copied into a new record.

    A known value is itself; another string is redacted and cut to
    MAX_LABEL_CHARS; anything that is not a string is ``None`` (never
    ``str(value)``, which would show the bytes or the nested content).

    Returns:
        The label and how many redactions were made.
    """
    if isinstance(value, str) and value in POSTURES:
        return value, 0
    if isinstance(value, str):
        return scrub_label_counted(value, MAX_LABEL_CHARS, cache)
    return None, 0


def _scan_history(history: object) -> tuple[dict | None, int]:
    """The last mapping of the history and how many mappings there are.

    Only the last MAX_HISTORY_SCAN entries are looked at; entries that are
    not mappings are skipped, and a history that is not a list has none.
    """
    if not isinstance(history, list):
        return None, 0
    last: dict | None = None
    total = 0
    for entry in reversed(history[-MAX_HISTORY_SCAN:]):
        if isinstance(entry, dict):
            total += 1
            if last is None:
                last = entry
    return last, total


def history_count(doc: Mapping | None) -> int:
    """How many history entries a reader counts (``history_total``)."""
    if doc is None:
        return 0
    return _scan_history(doc.get("history"))[1]


def _history_mismatch(stored: object, last: Mapping | None) -> bool:
    """Whether the value disagrees with the history's last ``to`` (S2 spec §2.10).

    With no history, only unset agrees. A ``to`` (or a value) that is not a
    string never agrees.
    """
    if last is None:
        return not (isinstance(stored, str) and stored == UNSET)
    to = last.get("to")
    return not (isinstance(stored, str) and isinstance(to, str) and stored == to)


def _last_change(entry: Mapping | None, cache: ScrubCache) -> tuple[dict | None, int]:
    """The last history entry as pm_status shows it (S2 spec §3.9).

    Each field is shown only when it is a string, redacted and cut (the
    reason to LAST_REASON_CHARS, the others to MAX_LABEL_CHARS); otherwise it
    is null.

    Returns:
        The fields (``None`` without an entry) and how many redactions were made.
    """
    if entry is None:
        return None, 0
    out: dict[str, str | None] = {}
    hits = 0
    for name in HISTORY_FIELDS:
        value = entry.get(name)
        if not isinstance(value, str):
            out[name] = None
            continue
        limit = LAST_REASON_CHARS if name == "reason" else MAX_LABEL_CHARS
        out[name], count = scrub_label_counted(value, limit, cache)
        hits += count
    return out, hits


@dataclass(frozen=True)
class PolicyView:
    """The project's decision posture as readers show it (S2 spec §2.10, §3.9).

    Attributes:
        value: The stored posture: a known value, an unknown string redacted
            and cut, ``None`` when it is not a string; unset when there is no
            file, no value or the file is unreadable.
        effective: bold only when the stored value is bold; cautious otherwise.
        last_change: The last history entry (``at``, ``from``, ``to``,
            ``reason``, ``via``), or ``None``.
        history_total: How many history entries were counted.
        warnings: Notices about the file (``_build_warning`` shape). The
            unset notice is not among them: its wording depends on the host.
        redactions: How many redactions the labels above needed, for a
            reader that reports them; the count carries no cleartext.
        state: STATE_MISSING, STATE_READ or STATE_UNREADABLE.
    """

    value: str | None
    effective: str
    last_change: dict | None = None
    history_total: int = 0
    warnings: list[dict] = field(default_factory=list)
    redactions: int = 0
    state: str = STATE_MISSING

    def as_dict(self) -> dict:
        """The ``decision_posture`` key of a pm_status response."""
        return {
            "value": self.value,
            "effective": self.effective,
            "last_change": None if self.last_change is None else dict(self.last_change),
            "history_total": self.history_total,
        }


def _unreadable_notice(detail: str | None) -> dict:
    reason = f" ({detail})" if detail else ""
    return notice(
        "warning",
        POLICY_UNREADABLE,
        f"decision_policy.yaml cannot be read{reason}; the decision posture is treated as "
        "unset, which acts as cautious.",
        "Fix .pm/decision_policy.yaml by hand or move it out of .pm (git can restore an "
        "earlier version); the posture cannot be changed until it can be read.",
    )


def _schema_notice() -> dict:
    return notice(
        "warning",
        POLICY_SCHEMA_UNSUPPORTED,
        "decision_policy.yaml uses a schema this pmlens does not know; it is read as far as "
        "it can be, and the posture cannot be changed with this version.",
        "Update pmlens on every host that uses this .pm directory.",
    )


def policy_view(raw: RawPolicy) -> PolicyView:
    """The lenient view of decision_policy.yaml; pure (S2 spec §2.10).

    Every row of the spec's reading table: no file is unset; an unreadable
    file is unset with ``decision_policy_unreadable``; an unknown schema is
    read as far as it can be with ``decision_policy_schema_unsupported``; an
    unknown or non-string value is shown as a redacted label or null with
    ``decision_posture_unknown``; a value that disagrees with the history's
    last ``to`` (or a value other than unset with no history) adds
    ``decision_posture_history_mismatch``. The effective posture is cautious
    unless the value is bold. No message quotes a stored value.

    Args:
        raw: What :func:`read_policy_raw` found.

    Returns:
        The view.
    """
    if not raw.exists:
        return PolicyView(value=UNSET, effective=CAUTIOUS, state=STATE_MISSING)
    if raw.unreadable or raw.data is None:
        return PolicyView(
            value=UNSET,
            effective=CAUTIOUS,
            warnings=[_unreadable_notice(raw.detail)],
            state=STATE_UNREADABLE,
        )
    doc = raw.data
    warnings: list[dict] = []
    if schema_unsupported(doc):
        warnings.append(_schema_notice())
    cache = ScrubCache()
    stored = stored_posture(doc)
    value, redactions = posture_label(stored, cache)
    if not (isinstance(stored, str) and stored in POSTURES):
        warnings.append(
            notice(
                "warning",
                POSTURE_UNKNOWN,
                "decision_posture in decision_policy.yaml is not one of "
                f"{', '.join(POSTURES)}; it is treated as cautious.",
                "Ask the user which posture the project should have; a change made with "
                "their reason replaces the value and is kept in the history.",
            )
        )
    last, total = _scan_history(doc.get("history"))
    if _history_mismatch(stored, last):
        warnings.append(
            notice(
                "warning",
                POSTURE_HISTORY_MISMATCH,
                "decision_posture in decision_policy.yaml differs from the last change in its "
                "history, so it may have been changed without a record.",
                "Ask the user whether the current posture is what they want. A change made "
                "with their reason is recorded in the history; otherwise fix the value or add "
                "the missing history entry by hand.",
            )
        )
    last_change, hits = _last_change(last, cache)
    return PolicyView(
        value=value,
        effective=BOLD if isinstance(stored, str) and stored == BOLD else CAUTIOUS,
        last_change=last_change,
        history_total=total,
        warnings=warnings,
        redactions=redactions + hits,
        state=STATE_READ,
    )


def read_decision_policy(pm_path: Path) -> PolicyView:
    """The project's decision posture for a reader (pm_status, the brief).

    Takes no lock and creates nothing; a writer replaces the file atomically,
    so this sees the old or the new one.
    """
    return policy_view(read_policy_raw(pm_path))


# ─── The writer's pure parts ─────────────────────────


def posture_write_refusal(raw: RawPolicy) -> dict | None:
    """Why the posture writer must not rewrite the file, or ``None`` (S2 spec §2.10).

    An unreadable file and a history that is not a list (appending would
    replace it, and the history is kept, never rewritten) are
    ``decision_policy_unreadable``; an unknown schema is
    ``decision_policy_schema_unsupported``. A value or history that disagrees
    is not refused: the change records the current value as ``from``.

    Returns:
        An error dict; its message names the problem, never a value.
    """
    if not raw.exists:
        return None
    if raw.unreadable or raw.data is None:
        reason = f" ({raw.detail})" if raw.detail else ""
        return _error(
            POLICY_UNREADABLE,
            f"decision_policy.yaml cannot be read{reason}; nothing was written. Fix it by hand "
            "or move it out of .pm, then try again.",
        )
    if schema_unsupported(raw.data):
        return _error(
            POLICY_SCHEMA_UNSUPPORTED,
            "decision_policy.yaml uses a schema this pmlens cannot write; nothing was written. "
            "Update pmlens before changing the posture.",
        )
    history = raw.data.get("history")
    if history is not None and not isinstance(history, list):
        return _error(
            POLICY_UNREADABLE,
            "history in decision_policy.yaml is not a list; nothing was written. Fix it by "
            "hand (a list of changes), then try again.",
        )
    return None


def unwritable_value_refusal(doc: object) -> dict | None:
    """Refuse a document safe_dump would change, or ``None`` (S2 spec §2.10).

    safe_load builds ``!!omap`` and ``!!pairs`` as lists of tuples, and
    safe_dump writes a tuple back as a plain sequence without its tag, so a
    rewrite would change an unknown key that the writer keeps by value.
    Every other safe_load type round-trips. The walk visits each object once,
    so values shared through YAML aliases cost nothing more.

    Returns:
        ``decision_policy_unreadable`` naming the tags, never the key or value.
    """
    seen: set[int] = set()
    pending: list[object] = [doc]
    while pending:
        value = pending.pop()
        if id(value) in seen:
            continue
        seen.add(id(value))
        if isinstance(value, tuple):
            return _error(
                POLICY_UNREADABLE,
                "decision_policy.yaml holds an ordered mapping or pairs (!!omap / !!pairs) "
                "that pmlens cannot write back unchanged; nothing was written. Rewrite it by "
                "hand as a plain mapping or list, then try again.",
            )
        if isinstance(value, Mapping):
            pending.extend(value.keys())
            pending.extend(value.values())
        elif isinstance(value, (list, set)):
            pending.extend(value)
    return None


def history_entry(at: str, previous: str | None, to: str, reason: str, via: str) -> dict:
    """One history entry, in the order the file shows its fields."""
    return {"at": at, "from": previous, "to": to, "reason": reason, "via": via}


def posture_changed_doc(doc: Mapping | None, to: str, entry: dict) -> dict:
    """The document after a change; the loaded one is not modified (copy-on-write).

    safe_load can share one list or mapping between keys through a YAML
    alias, so the history is a new list holding the old entries plus
    ``entry``, and every other key keeps its object as loaded: an unknown
    key, an alias and a ``!!binary`` value are written back as they were
    (``!!omap`` and ``!!pairs`` cannot be: :func:`unwritable_value_refusal`).

    Args:
        doc: The loaded mapping, or ``None`` when there is no file.
        to: The new posture.
        entry: The history entry to append (:func:`history_entry`).
    """
    if doc is None:
        return {"schema": POLICY_SCHEMA, "decision_posture": to, "history": [entry]}
    new_doc = dict(doc)
    new_doc["decision_posture"] = to
    new_doc["history"] = [*(doc.get("history") or []), entry]
    return new_doc


def posture_changed_notice(previous: str | None, to: str) -> dict:
    """The info a change returns (``decision_posture_changed``, S2 spec §3.8).

    The previous value is named only when it is a known posture.
    """
    before = previous if previous in POSTURES else "a value pmlens does not recognise"
    return notice(
        "info",
        POSTURE_CHANGED,
        f"The project's decision posture changed from {before} to {to}. pmlens cannot tell "
        "who asked for it; tell the user.",
    )


def secrets_notice(count: int) -> dict:
    """The warning for redactions made before saving (``decision_policy_secrets_redacted``)."""
    return notice(
        "warning",
        POLICY_SECRETS_REDACTED,
        f"{count} secret-like string(s) were removed from the change before saving.",
        "If a real credential was pasted, revoke it.",
    )
