"""YAML file storage for PM Lens.

All YAML operations use safe_load / safe_dump only (security).
Output is human-readable with comment headers.

Concurrency (PMSERV-048 / ADR-011) and the private-save API (PMSERV-067):
- ``_save_yaml`` writes via ``utils._atomic_write_text`` (mkstemp + os.replace),
  preventing partial-write corruption on SIGKILL / OS crash.
- Public **mutators** (``add_*`` / ``update_*``) are the supported write API:
  each wraps the read-modify-write cycle in ``_yaml_transaction`` to prevent
  lost updates between concurrent processes. External callers MUST go through
  these — not through raw saves.
- ``_save_*`` helpers (``_save_tasks`` / ``_save_project`` / …) are PRIVATE raw
  I/O with no locking, renamed from the former public ``save_*`` (PMSERV-067)
  precisely to enforce the rule above. The only sanctioned callers are:
  (a) the mutators in this module, and (b) a small set of in-layer composite
  read-modify-write sites that already hold their own ``_yaml_transaction`` and
  must avoid re-entrant locking — ``server.pm_add_issue`` (load_tasks + multi-
  edit + ``_save_tasks``), ``server.pm_discover`` / ``server.pm_cleanup``
  (``_save_registry`` under a held registry lock), and
  ``workflow.advance_step`` (``_save_workflows``). Those call ``_save_*``
  deliberately; the leading underscore marks the intentional lock bypass.

Decision Lineage (ADR-056 S1) is the first place that holds two ledger locks at
once: ``add_decision_with_lineage`` and ``change_decision_lineage`` take the
decisions.yaml lock and then the ADR's own lineage lock
(``.pm/.locks/decision_lineage-ADR-NNN.lock``), always in that order. A writer
that only touches a lineage (S2) takes the lineage lock alone and must not take
the decisions lock inside it, and nobody holds two lineage locks at once.
``_yaml_transaction`` enforces this order at run time: taking the decisions
lock while a lineage lock is held, or a second lineage lock, raises
``PmServerError`` at once instead of risking an AB-BA deadlock. Other lock
labels are not ranked and behave as before. Lineage files are read through
``lineage.read_lineage_raw`` (bounded, lock-free) and written only through
``_save_yaml`` from the two composite functions above.
"""

from __future__ import annotations

import datetime as _dt
import os
import threading
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from itertools import chain
from pathlib import Path

import yaml
from filelock import FileLock
from filelock import Timeout as FileLockTimeout
from pydantic import BaseModel

from . import lineage as _lineage
from .lineage import LineageChange, LineageWriteRefused
from .models import (
    DailyLog,
    DailyLogEntry,
    Decision,
    DecisionStatus,
    KnowledgeNotFoundError,
    KnowledgeRecord,
    Milestone,
    PmServerError,
    Project,
    Registry,
    RegistryEntry,
    Risk,
    Task,
    TaskNotFoundError,
    Workflow,
    WorkflowNotFoundError,
    WorkflowStep,
    WorkflowTemplate,
)
from .utils import _atomic_write_text

PM_DIR = ".pm"
GLOBAL_PM_DIR = Path.home() / ".pm"

DEFAULT_LOCK_TIMEOUT_S = 5.0
# Env override for the lock-acquire timeout (PMSERV-109). Lets operators on slow
# or heavily-contended filesystems (networked storage, oversubscribed CI runners)
# raise the timeout without weakening the in-process fail-fast default.
_LOCK_TIMEOUT_ENV = "PM_LOCK_TIMEOUT_S"
_LOCKS_DIR = ".locks"
_LOCKS_GITIGNORE = "*\n!.gitignore\n"


# ─── Internal helpers ────────────────────────────────


def _yaml_header(filename: str) -> str:
    return f"# PM Lens - {filename}\n"


def _load_yaml(path: Path) -> dict | list | None:
    if not path.exists():
        return None
    text = path.read_text(encoding="utf-8")
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise PmServerError(f"Failed to parse {path.name}: {e}") from e


def _save_yaml(path: Path, data: dict | list, header_name: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = yaml.safe_dump(
        data,
        default_flow_style=False,
        allow_unicode=True,
        sort_keys=False,
    )
    _atomic_write_text(path, _yaml_header(header_name) + body)


def _ensure_locks_dir(base_dir: Path) -> Path:
    """Create ``base_dir/.locks/`` and seed it with a self-ignoring .gitignore.

    The seeded ``.gitignore`` (``*\\n!.gitignore\\n``) means lock files never
    get committed, even for users who track ``.pm/`` in git.
    """
    lock_dir = base_dir / _LOCKS_DIR
    lock_dir.mkdir(parents=True, exist_ok=True)
    gitignore = lock_dir / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text(_LOCKS_GITIGNORE, encoding="utf-8")
    return lock_dir


def _resolve_lock_timeout() -> float:
    """Resolve the default lock-acquire timeout, honoring ``PM_LOCK_TIMEOUT_S``.

    The in-process default is :data:`DEFAULT_LOCK_TIMEOUT_S` (5s) — deliberately
    fail-fast so a genuinely stuck lock surfaces quickly to the caller. Operators
    on slow or heavily-contended filesystems (networked storage, oversubscribed
    CI runners) can raise it via the ``PM_LOCK_TIMEOUT_S`` environment variable
    without weakening that default for everyone. Missing, non-numeric, or
    non-positive values fall back to :data:`DEFAULT_LOCK_TIMEOUT_S`.
    """
    raw = os.environ.get(_LOCK_TIMEOUT_ENV)
    if raw is None:
        return DEFAULT_LOCK_TIMEOUT_S
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_LOCK_TIMEOUT_S
    return value if value > 0 else DEFAULT_LOCK_TIMEOUT_S


@contextmanager
def _yaml_transaction(
    base_dir: Path,
    filename: str,
    *,
    timeout: float | None = None,
) -> Iterator[None]:
    """Acquire an exclusive lock for a yaml file's read-modify-write cycle.

    The lock file lives at ``base_dir/.locks/{stem}.lock`` where ``stem`` is
    ``filename`` with any ``.yaml`` suffix stripped (so callers can pass either
    ``"tasks.yaml"`` or a plain label like ``"registry"``).

    ``timeout`` is the seconds to wait for the lock. When ``None`` (the default
    used by every mutator), it is resolved via :func:`_resolve_lock_timeout`,
    which honors the ``PM_LOCK_TIMEOUT_S`` env override. An explicit value (e.g.
    a short timeout in tests asserting contention) bypasses the env entirely.

    Raises ``PmServerError`` if the lock cannot be acquired within ``timeout``
    seconds.

    Implementation note: ``filelock.FileLock`` is reentrant within the same
    instance but two distinct ``FileLock(same_path)`` calls in the same process
    deadlock. The convention for callers: do not nest mutators — i.e. inside
    a ``with _yaml_transaction(...):`` block, do not call another public
    mutator that would acquire the same lock.

    Nesting two ledger locks is allowed in exactly one order, decisions →
    ``decision_lineage-ADR-NNN`` (ADR-056 S1), and that order is checked at run
    time per thread: a ranked lock (decisions = 0, any lineage = 1) cannot be
    taken while a lock of the same or a higher rank is held. The violation
    raises ``PmServerError`` before any waiting, so holding a lineage lock and
    then asking for decisions (or for a second lineage) fails at once rather
    than deadlocking. Unranked labels (tasks, registry, daily-…) are not
    checked.
    """
    stem = filename.removesuffix(".yaml")
    rank = _lock_rank(stem)
    held = _held_ranked_locks()
    if rank is not None:
        blocking = [label for label, held_rank in held if held_rank >= rank]
        if blocking:
            raise PmServerError(
                f"lock order violation: cannot take {stem} while holding {blocking[-1]} "
                "(decisions must come first, and only one decision_lineage lock at a time)"
            )
    if timeout is None:
        timeout = _resolve_lock_timeout()
    lock_dir = _ensure_locks_dir(base_dir)
    lock_path = lock_dir / f"{stem}.lock"
    lock = FileLock(str(lock_path), timeout=timeout)
    try:
        with lock:
            if rank is not None:
                held.append((stem, rank))
            try:
                yield
            finally:
                if rank is not None:
                    held.pop()
    except FileLockTimeout as e:
        raise PmServerError(
            f"Failed to acquire lock on {filename}: timeout after {timeout}s"
        ) from e


# Per-thread stack of the ranked ledger locks currently held (see
# _yaml_transaction). The MCP server runs sync tools on worker threads, so the
# order is tracked per thread; across processes the order itself is what keeps
# two writers from deadlocking.
_LOCK_STATE = threading.local()
_DECISIONS_LOCK = "decisions"
_LINEAGE_LOCK_PREFIX = "decision_lineage-"


def _lock_rank(stem: str) -> int | None:
    """Rank of a lock label in the decisions → decision_lineage order, if any."""
    if stem == _DECISIONS_LOCK:
        return 0
    if stem.startswith(_LINEAGE_LOCK_PREFIX):
        return 1
    return None


def _held_ranked_locks() -> list[tuple[str, int]]:
    """This thread's stack of held ranked locks (label, rank)."""
    stack = getattr(_LOCK_STATE, "stack", None)
    if stack is None:
        stack = []
        _LOCK_STATE.stack = stack
    return stack


def _model_dump(model: BaseModel) -> dict:
    """Dump a Pydantic model to a dict for ``yaml.safe_dump``.

    Declared fields go through ``mode="json"`` (enums and dates become plain
    scalars). Keys kept by ``extra="allow"`` (PMSERV-218) are written back as
    the very objects ``safe_load`` produced, NOT re-serialised: a JSON-mode dump
    copies every shared reference, so a YAML alias "billion laughs" in an
    unknown key expanded to ~117 MB and held the ledger lock for ~25 s, while
    ``!!binary`` or a recursive anchor made every later write raise. Handed over
    unchanged, ``safe_dump`` re-emits the anchors/aliases and keeps the value's
    YAML type (bytes, dates, NaN, non-string keys) intact.
    """
    data = model.model_dump(mode="json", exclude=_extra_exclusions(model) or None)
    _merge_raw_extras(model, data)
    return data


def _extra_exclusions(model: BaseModel) -> dict:
    """``exclude`` spec covering a model's extras, nested models included."""
    spec: dict = dict.fromkeys(model.__pydantic_extra__ or {}, True)
    for name in type(model).model_fields:
        value = getattr(model, name, None)
        if isinstance(value, BaseModel):
            nested = _extra_exclusions(value)
            if nested:
                spec[name] = nested
    return spec


def _merge_raw_extras(model: BaseModel, data: dict) -> None:
    """Put each model's extras back into ``data`` as the loaded objects."""
    data.update(model.__pydantic_extra__ or {})
    for name in type(model).model_fields:
        value = getattr(model, name, None)
        if isinstance(value, BaseModel) and isinstance(data.get(name), dict):
            _merge_raw_extras(value, data[name])


def _with_sibling_keys(path: Path, list_key: str, items: list[dict]) -> dict:
    """Build a ledger document that keeps the file's other top-level keys.

    Whole-file rewrites used to emit only ``{list_key: [...]}``, so any
    top-level key a newer pmlens (or a person) added was dropped on the next
    write (PMSERV-218). Key order is preserved; ``list_key`` stays in place.
    """
    existing = _load_yaml(path)
    if not isinstance(existing, dict):
        return {list_key: items}
    doc = {k: (items if k == list_key else v) for k, v in existing.items()}
    doc.setdefault(list_key, items)
    return doc


def _next_number_from_ids(ids: Iterable[str]) -> int:
    """Return max(numeric id suffix) + 1, or 1 when there is none."""
    numbers = []
    for item_id in ids:
        parts = item_id.rsplit("-", 1)
        if len(parts) == 2 and parts[1].isdigit():
            numbers.append(int(parts[1]))
    return max(numbers, default=0) + 1


def _reject_duplicate_id(existing_ids: Iterable[str], new_id: str, ledger: str) -> None:
    """Refuse to append a record whose id is already in the ledger.

    Last line of defence for the numbering race (PMSERV-219): two records with
    the same id would otherwise both be saved, and every lookup by id would
    silently act on the first one only.
    """
    if new_id in set(existing_ids):
        raise PmServerError(f"{new_id} already exists in {ledger}; refusing a duplicate id")


# ─── Project ─────────────────────────────────────────


def load_project(pm_path: Path) -> Project:
    """Load project.yaml. Returns default Project if file doesn't exist."""
    data = _load_yaml(pm_path / "project.yaml")
    if data is None:
        return Project(name=pm_path.parent.name)
    return Project(**data)


def _save_project(pm_path: Path, project: Project) -> None:
    """Save project to project.yaml."""
    _save_yaml(pm_path / "project.yaml", _model_dump(project), "project.yaml")


def load_tracks(pm_path: Path) -> dict[str, list[str]]:
    """Load ``.pm/tracks.yaml`` — logical work-line label → branch glob patterns.

    Used by ``pm_recall(track=...)`` to resolve a logical line label (e.g.
    本流 / 論文 / 教材) to the git branches whose session summaries belong to that
    line (PMSERV-125 / ADR-028 / SynapticLedger ADR-035). Resolution happens at
    *query* time, so renaming or adding branches within a line never breaks
    continuity history.

    File format (absent file ⇒ ``{}`` ⇒ ``track`` is matched as a raw branch)::

        tracks:
          本流: [main]
          論文: [feat/p3-*, research/wave-scattering-*]
          教材: [edu/*]

    Returns a mapping of ``label -> [fnmatch glob, ...]``. A scalar value is
    promoted to a one-element list; non-string / empty patterns are dropped; a
    label left with no patterns is omitted. Raises ``PmServerError`` on
    malformed YAML (callers may degrade to raw-branch resolution).
    """
    data = _load_yaml(pm_path / "tracks.yaml")
    if not isinstance(data, dict):
        return {}
    raw = data.get("tracks")
    if not isinstance(raw, dict):
        return {}
    result: dict[str, list[str]] = {}
    for label, globs in raw.items():
        if isinstance(globs, str):
            patterns = [globs] if globs.strip() else []
        elif isinstance(globs, list):
            patterns = [g for g in globs if isinstance(g, str) and g.strip()]
        else:
            patterns = []
        if patterns:
            result[str(label)] = patterns
    return result


# ─── Tasks ───────────────────────────────────────────


def load_tasks(pm_path: Path) -> list[Task]:
    """Load all tasks from tasks.yaml."""
    data = _load_yaml(pm_path / "tasks.yaml")
    if data is None or not isinstance(data, dict) or "tasks" not in data:
        return []
    return [Task(**t) for t in data["tasks"]]


def _save_tasks(pm_path: Path, tasks: list[Task]) -> None:
    """Save all tasks to tasks.yaml."""
    _save_yaml(
        pm_path / "tasks.yaml",
        {"tasks": [_model_dump(t) for t in tasks]},
        "tasks.yaml",
    )


def add_task(pm_path: Path, task: Task) -> Task:
    """Append a new task and save."""
    with _yaml_transaction(pm_path, "tasks.yaml"):
        tasks = load_tasks(pm_path)
        _reject_duplicate_id((t.id for t in tasks), task.id, "tasks.yaml")
        tasks.append(task)
        _save_tasks(pm_path, tasks)
    return task


def add_task_with_next_id(pm_path: Path, build: Callable[[int], Task]) -> Task:
    """Number and append a task inside ONE tasks.yaml transaction (PMSERV-219).

    ``build`` receives the next task number and returns the Task to append.
    Computing the number outside the lock (``next_task_number`` + ``add_task``)
    let two concurrent callers take the same number and save duplicate ids.

    Contract for every ``add_*_with_next_id``: ``build`` runs while the ledger
    lock is held, so it must only construct the record. Calling a mutator or
    taking any ledger lock from inside it self-deadlocks (same lock) or risks
    an AB-BA deadlock (another ledger); do that work before or after the call.
    The one exception is the decisions → decision_lineage order that this
    module's composite functions (``add_decision_with_lineage``,
    ``change_decision_lineage``) take outside ``build``; ``_yaml_transaction``
    checks that order at run time.
    """
    with _yaml_transaction(pm_path, "tasks.yaml"):
        tasks = load_tasks(pm_path)
        task = build(_next_task_number_from_list(tasks))
        _reject_duplicate_id((t.id for t in tasks), task.id, "tasks.yaml")
        tasks.append(task)
        _save_tasks(pm_path, tasks)
    return task


def update_task(pm_path: Path, task_id: str, **updates) -> Task:
    """Update fields on an existing task by ID."""
    with _yaml_transaction(pm_path, "tasks.yaml"):
        tasks = load_tasks(pm_path)
        for task in tasks:
            if task.id == task_id:
                for key, value in updates.items():
                    if value is not None and hasattr(task, key):
                        setattr(task, key, value)
                task.updated = _dt.date.today()
                _save_tasks(pm_path, tasks)
                return task
    raise TaskNotFoundError(f"Task {task_id} not found")


def _next_task_number_from_list(tasks: list[Task]) -> int:
    """Compute the next task number from an already-loaded tasks list.

    Pure helper for compound-RMW callers that already hold
    ``_yaml_transaction(..., 'tasks.yaml')`` (e.g. ``pm_add_issue`` —
    see ADR-012 / PMSERV-065). Avoids the nested-load race that would
    occur if ``next_task_number`` were re-entered inside an open lock.
    """
    return _next_number_from_ids(t.id for t in tasks)


def next_task_number(pm_path: Path) -> int:
    """Return the next available task number.

    Read-only preview: it takes no lock, so do not use it to number a record
    you are about to append — use :func:`add_task_with_next_id` (PMSERV-219).
    """
    return _next_task_number_from_list(load_tasks(pm_path))


# ─── Decisions ───────────────────────────────────────


def load_decisions(pm_path: Path) -> list[Decision]:
    """Load all ADRs from decisions.yaml."""
    data = _load_yaml(pm_path / "decisions.yaml")
    if data is None or not isinstance(data, dict) or "decisions" not in data:
        return []
    return [Decision(**d) for d in data["decisions"]]


def _save_decisions(pm_path: Path, decisions: list[Decision]) -> None:
    """Save all decisions to decisions.yaml (other top-level keys are kept)."""
    path = pm_path / "decisions.yaml"
    _save_yaml(
        path,
        _with_sibling_keys(path, "decisions", [_model_dump(d) for d in decisions]),
        "decisions.yaml",
    )


def unknown_decision_statuses(decisions: list[Decision]) -> list[dict]:
    """Return ``{"id", "status"}`` for ADRs whose status is not a DecisionStatus.

    Loading keeps such a value as a raw string rather than failing the whole
    file (PMSERV-218); pm_status reports them as ``decision_status_unknown``.
    """
    return [
        {"id": d.id, "status": d.status}
        for d in decisions
        if not isinstance(d.status, DecisionStatus)
    ]


def _require_known_status(decision: Decision) -> None:
    """A NEW ADR must carry a DecisionStatus value (PMSERV-218 / ADR-056 D1).

    Loading tolerates an unknown status so one odd record does not take the
    whole file down, but writing one would make decisions.yaml unreadable for
    every already-shipped reader, so new records are held to the enum.
    """
    if not isinstance(decision.status, DecisionStatus):
        allowed = ", ".join(s.value for s in DecisionStatus)
        raise PmServerError(f"{decision.id}: status {decision.status!r} is not one of {allowed}")


def add_decision(pm_path: Path, decision: Decision) -> Decision:
    """Append a new ADR and save."""
    _require_known_status(decision)
    with _yaml_transaction(pm_path, "decisions.yaml"):
        decisions = load_decisions(pm_path)
        _reject_duplicate_id((d.id for d in decisions), decision.id, "decisions.yaml")
        decisions.append(decision)
        _save_decisions(pm_path, decisions)
    return decision


def add_decision_with_next_id(pm_path: Path, build: Callable[[int], Decision]) -> Decision:
    """Number and append an ADR inside ONE decisions.yaml transaction (PMSERV-219).

    ``build`` runs under the lock — see :func:`add_task_with_next_id`. Numbers
    skip ids still held by a lineage file (:func:`_next_decision_number`).
    """
    with _yaml_transaction(pm_path, "decisions.yaml"):
        decisions = load_decisions(pm_path)
        decision = build(_next_decision_number(pm_path, decisions))
        _require_known_status(decision)
        _reject_duplicate_id((d.id for d in decisions), decision.id, "decisions.yaml")
        decisions.append(decision)
        _save_decisions(pm_path, decisions)
    return decision


def next_decision_number(pm_path: Path) -> int:
    """Return the next available ADR number (read-only preview, no lock)."""
    return _next_decision_number(pm_path, load_decisions(pm_path))


def _lineage_stems(pm_path: Path) -> list[str]:
    """Ids of the lineage files on disk (``ADR-*.yaml`` whose stem is an ADR id).

    The stem must match ``lineage.DECISION_ID_RE``: ``"ADR-²".isdigit()`` is
    True but ``int("²")`` raises, and a ``tmp*.tmp`` left by a killed atomic
    write is not a lineage at all.
    """
    directory = pm_path / _lineage.LINEAGE_DIR
    try:
        names = [entry.stem for entry in directory.glob("ADR-*.yaml")]
    except OSError:
        return []
    return [name for name in names if _lineage.is_decision_id(name)]


def _next_decision_number(pm_path: Path, decisions: list[Decision]) -> int:
    """Next ADR number: past every id in decisions.yaml AND every lineage file.

    Counting lineage files keeps a new ADR off an orphaned lineage (one whose
    ADR was deleted by hand), which would otherwise hand the new ADR an old
    lifecycle, declared values and events (design §5.1).
    """
    return _next_number_from_ids(chain((d.id for d in decisions), _lineage_stems(pm_path)))


@dataclass(frozen=True)
class DecisionWrite:
    """Result of :func:`add_decision_with_lineage`.

    Attributes:
        decision: The appended ADR, or ``None`` when ``error`` is set.
        error: ``decision_id_exhausted`` or ``decisions_yaml_unreadable``
            (nothing written in either case), else ``None``.
        error_detail: For ``decisions_yaml_unreadable``, the exception's type
            and YAML position (:func:`lineage.error_summary`), never its text.
        lineage_state: ``written``; ``not_written`` (checking for or writing
            the lineage failed with an OS error — the ADR is saved without a
            lineage); or ``preexisting`` (a file was already there and was
            left untouched).
        recorded_at: The timestamp stored in the new lineage (``None`` unless
            it was written).
        lifecycle: The lifecycle readers show for the ADR after the call: the
            new lineage's when written, otherwise what
            :func:`lineage.effective_lifecycle` finds (derived from the status,
            or a preexisting file's own lifecycle when that file is attributed
            to the ADR).
    """

    decision: Decision | None
    error: str | None = None
    error_detail: str | None = None
    lineage_state: str = "written"
    recorded_at: str | None = None
    lifecycle: str | None = None


def _lineage_write_target(pm_path: Path, decision_id: str) -> Path:
    """Path of an ADR's lineage file, refusing a symlinked lineage directory.

    Callers decide what a symlink at the file itself means: creation treats it
    as an existing file (left alone), a change refuses it.

    Raises:
        LineageWriteRefused: The id is not an ADR id, or the lineage directory
            is a symbolic link (``decision_lineage_unreadable``).
    """
    if not _lineage.is_decision_id(decision_id):
        raise LineageWriteRefused(
            _lineage.LINEAGE_UNREADABLE, "a lineage file needs an id of the form ADR-NNN"
        )
    directory = pm_path / _lineage.LINEAGE_DIR
    if directory.is_symlink():
        raise LineageWriteRefused(
            _lineage.LINEAGE_UNREADABLE,
            f".pm/{_lineage.LINEAGE_DIR} is a symbolic link; refusing to write through it",
        )
    return directory / f"{decision_id}.yaml"


def add_decision_with_lineage(
    pm_path: Path,
    build: Callable[[int], Decision],
    *,
    declared: Mapping[str, str] | None = None,
    via: str = "pm_add_decision",
) -> DecisionWrite:
    """Number and append an ADR, then create its lineage (design §5.1).

    Locks are taken decisions → ``decision_lineage-ADR-NNN``, and the lineage
    lock is taken before anything is written, so a lineage-lock timeout leaves
    both files untouched. Writes go decisions.yaml first, lineage second: if
    the process dies between them, or checking for or writing the lineage
    fails with an OS error (``lineage_state="not_written"``), the ADR exists
    without a lineage and readers derive its lifecycle from the status. An
    existing lineage file is never overwritten (``lineage_state="preexisting"``),
    and a symlinked lineage directory refuses the call before anything is
    written.

    ``build`` runs under the decisions lock and must only construct the record
    (see :func:`add_task_with_next_id`); the lineage is written outside it.

    Args:
        pm_path: The project's ``.pm`` directory.
        build: Receives the next ADR number, returns the ADR to append. Its
            status must be a DecisionStatus and its id an ``ADR-NNN`` id.
        declared: Declared provenance (origin / recorded_timing /
            decision_kind); missing values are ``unknown``.
        via: The tool name recorded on the ``created`` event.

    Returns:
        The write result; ``error="decision_id_exhausted"`` when the next
        number would exceed 999,999, and ``error="decisions_yaml_unreadable"``
        when decisions.yaml cannot be loaded (described in ``error_detail`` by
        exception type and YAML position only). Nothing is written in either
        case.

    Raises:
        LineageWriteRefused: ``.pm/decision_lineage`` is a symbolic link
            (``decision_lineage_unreadable``). Checked before anything is
            written, so nothing is.
        PmServerError: A lock timed out, a declared value or the status is
            outside its vocabulary, the id is not an ADR id, or it is a
            duplicate. Nothing is written in these cases.
    """
    problem = _lineage.declared_error(declared or {})
    if problem is not None:
        raise PmServerError(problem["message"])
    with _yaml_transaction(pm_path, "decisions.yaml"):
        try:
            decisions = load_decisions(pm_path)
        except Exception as exc:  # noqa: BLE001 - reported by type only, never quoted
            # str(exc) would quote the offending YAML line or pydantic's
            # input_value, which can be a secret (design §4, D11).
            return DecisionWrite(
                decision=None,
                error="decisions_yaml_unreadable",
                error_detail=_lineage.error_summary(exc),
            )
        number = _next_decision_number(pm_path, decisions)
        if number > _lineage.MAX_DECISION_NUMBER:
            return DecisionWrite(decision=None, error="decision_id_exhausted")
        decision = build(number)
        _require_known_status(decision)
        _reject_duplicate_id((d.id for d in decisions), decision.id, "decisions.yaml")
        if not _lineage.is_decision_id(decision.id):
            raise PmServerError(f"{decision.id!r} is not an ADR-NNN id; refusing to record it")
        now = _lineage._utc_now()
        doc = _lineage.new_lineage_doc(decision, declared, now, via=via)
        with _yaml_transaction(pm_path, f"{_LINEAGE_LOCK_PREFIX}{decision.id}"):
            # Checked before decisions.yaml is written: a symlinked lineage
            # directory refuses the whole call instead of leaving an ADR
            # without its lineage (design §2.1).
            path = _lineage_write_target(pm_path, decision.id)
            decisions.append(decision)
            _save_decisions(pm_path, decisions)
            state = "written"
            lifecycle = doc["lifecycle"]
            # The existence check is inside the try: Path.exists() raises on
            # EACCES (an unsearchable lineage directory), and decisions.yaml is
            # already saved, so any OS error here means "nothing written".
            try:
                if path.exists() or path.is_symlink():
                    state = "preexisting"
                else:
                    _save_yaml(path, doc, _lineage.lineage_header_name(decision.id))
            except OSError:
                state = "not_written"
            if state != "written":
                # Report what a reader shows for this ADR now. A preexisting
                # file without an anchor is attributed to the new ADR, so its
                # lifecycle (not the one this call started with) is shown.
                lifecycle = _lineage.effective_lifecycle(
                    decision, _lineage.read_lineage_raw(pm_path, decision.id)
                )
    return DecisionWrite(
        decision=decision,
        lineage_state=state,
        recorded_at=now if state == "written" else None,
        lifecycle=lifecycle,
    )


@dataclass
class LineageChangeResult:
    """Result of :func:`change_decision_lineage` (design §4.3).

    Attributes:
        status: ``updated``, ``unchanged`` or ``error``.
        decision_id: The ADR id the call named.
        error: The error dict when ``status == "error"`` (nothing was written).
        lifecycle: The lineage lifecycle after the call.
        decision_status: decisions.yaml's status after the call (a redacted label).
        changes: What changed (``lifecycle`` / ``decision_status`` / ``declared``).
        links: The ADR's links after the call.
        events_added: Kinds of the events appended, in file order.
        warnings: Warning entries (``server._build_warning`` shape).
        next: A follow-up hint (the other side of a supersedes link), if any.
    """

    status: str
    decision_id: str
    error: dict | None = None
    lifecycle: str | None = None
    decision_status: str | None = None
    changes: dict = field(default_factory=dict)
    links: dict = field(default_factory=dict)
    events_added: list[str] = field(default_factory=list)
    warnings: list[dict] = field(default_factory=list)
    next: str | None = None


def _read_lineage_for_write(pm_path: Path, decision: Decision) -> dict | None:
    """Load an ADR's lineage for rewriting, or ``None`` when it has none.

    Uses the same bounded, symlink-refusing reader as the read path, then the
    write checks of design §2.6.

    Raises:
        LineageWriteRefused: The file cannot be read, or its shape, schema,
            lifecycle or anchor make it unsafe to rewrite.
    """
    raw = _lineage.read_lineage_raw(pm_path, decision.id)
    if not raw.exists:
        return None
    if raw.error:
        reason = f" ({raw.detail})" if raw.detail else ""
        raise LineageWriteRefused(
            _lineage.LINEAGE_UNREADABLE,
            f"{decision.id}: the lineage file cannot be read{reason}; fix it or move it out of "
            f".pm/{_lineage.LINEAGE_DIR} by hand",
        )
    refusal = _lineage.write_refusal(raw.data, decision)
    if refusal is not None:
        raise LineageWriteRefused(*refusal)
    return raw.data


def _counterpart_hints(
    pm_path: Path, decision_id: str, added: Mapping[str, list[str]]
) -> tuple[list[dict], str | None]:
    """Notices and a hint for supersedes links whose other side is not recorded.

    Only this ADR's lineage is ever written; the counterpart's lineage is just
    read (bounded, no lock) to see whether the reverse link exists.
    """
    hints: list[str] = []
    missing: list[str] = []
    for target in added.get("supersedes", []):
        reverse = _lineage.link_targets(_lineage.read_lineage_raw(pm_path, target), "superseded_by")
        if decision_id not in reverse:
            missing.append(target)
            hints.append(
                f"{target}'s lifecycle and superseded_by were not changed. If {decision_id} "
                f"replaces it, call pm_update_decision on {target} with lifecycle=superseded "
                f"and add_links superseded_by=[{decision_id}]."
            )
    for target in added.get("superseded_by", []):
        reverse = _lineage.link_targets(_lineage.read_lineage_raw(pm_path, target), "supersedes")
        if decision_id not in reverse:
            missing.append(target)
            hints.append(
                f"{target} does not list {decision_id} in supersedes. If it replaces "
                f"{decision_id}, call pm_update_decision on {target} with add_links "
                f"supersedes=[{decision_id}]."
            )
    if not missing:
        return [], None
    notices = [
        _lineage.notice(
            "info",
            "decision_lineage_link_asymmetric",
            f"{decision_id}: the reverse link is not recorded on {', '.join(missing)}.",
        )
    ]
    return notices, " ".join(hints)


def change_decision_lineage(
    pm_path: Path, decision_id: str, change: LineageChange
) -> LineageChangeResult:
    """Change one ADR's lineage and project its status (design §5.2).

    Under the decisions lock and then the ADR's lineage lock: the lineage is
    loaded (or started from the status), the change is validated against the
    current state and applied (:func:`lineage.apply_change`), the result's
    size is checked, the lineage is saved, and decisions.yaml is rewritten only
    when the projected status changed — and only that ADR's status. The ADR
    text is never touched. A failure to save decisions.yaml after the lineage
    was saved leaves the lineage (the source of truth) ahead; it is reported as
    ``decision_status_not_projected`` and repaired by calling again with the
    same lifecycle.

    Args:
        pm_path: The project's ``.pm`` directory.
        decision_id: The ADR id.
        change: What to change.

    Returns:
        The result. Expected failures come back as ``status="error"`` with
        nothing written; that includes a decisions.yaml that cannot be loaded
        (``decisions_yaml_unreadable``, described by exception type and YAML
        position only).

    Raises:
        PmServerError: A lock timed out, or the projected status is not one of
            the four DecisionStatus values. Nothing is written in these cases.
    """
    if not _lineage.is_decision_id(decision_id):
        return LineageChangeResult(
            status="error",
            decision_id=_lineage.scrub_label(decision_id),
            error=_lineage._error("invalid_decision_id", "decision_id must look like ADR-NNN"),
        )
    problem = _lineage.validate_change(change)
    if problem is not None:
        return LineageChangeResult(status="error", decision_id=decision_id, error=problem)
    with _yaml_transaction(pm_path, "decisions.yaml"):
        try:
            decisions = load_decisions(pm_path)
        except Exception as exc:  # noqa: BLE001 - reported by type only, never quoted
            # str(exc) would quote the offending YAML line or pydantic's
            # input_value, which can be a secret (design §4, D11).
            return LineageChangeResult(
                status="error",
                decision_id=decision_id,
                error=_lineage._error(
                    "decisions_yaml_unreadable",
                    f"decisions.yaml could not be read: {_lineage.error_summary(exc)}. Fix the "
                    "file by hand; nothing was changed.",
                ),
            )
        matches = [d for d in decisions if d.id == decision_id]
        if not matches:
            return LineageChangeResult(
                status="error",
                decision_id=decision_id,
                error=_lineage._error(
                    "decision_not_found", f"{decision_id} is not in decisions.yaml"
                ),
            )
        if len(matches) > 1:
            return LineageChangeResult(
                status="error",
                decision_id=decision_id,
                error=_lineage._error(
                    _lineage.DECISION_ID_DUPLICATE,
                    f"decisions.yaml holds {decision_id} {len(matches)} times; give one of them "
                    "another id by hand before changing its lineage",
                ),
            )
        adr = matches[0]
        with _yaml_transaction(pm_path, f"{_LINEAGE_LOCK_PREFIX}{decision_id}"):
            try:
                path = _lineage_write_target(pm_path, decision_id)
                if path.is_symlink():
                    raise LineageWriteRefused(
                        _lineage.LINEAGE_UNREADABLE,
                        f"{decision_id}: the lineage file is a symbolic link; refusing to "
                        "write through it",
                    )
                doc = _read_lineage_for_write(pm_path, adr)
            except LineageWriteRefused as refused:
                return LineageChangeResult(
                    status="error",
                    decision_id=decision_id,
                    error=_lineage._error(refused.code, str(refused)),
                )
            outcome = _lineage.apply_change(
                doc, adr, change, (d.id for d in decisions), _lineage._utc_now()
            )
            if outcome.error is not None:
                return LineageChangeResult(
                    status="error", decision_id=decision_id, error=outcome.error
                )
            result = LineageChangeResult(
                status="updated" if outcome.changed else "unchanged",
                decision_id=decision_id,
                lifecycle=outcome.lifecycle,
                decision_status=_lineage.scrub_label(outcome.new_status),
                changes=outcome.changes,
                links=outcome.links,
                events_added=outcome.events_added,
                warnings=list(outcome.notices),
            )
            if not outcome.changed or outcome.doc is None:
                return result
            text = _lineage.dump_lineage(outcome.doc, decision_id)
            if len(text.encode("utf-8")) > _lineage.MAX_LINEAGE_BYTES:
                return LineageChangeResult(
                    status="error",
                    decision_id=decision_id,
                    error=_lineage._error(
                        _lineage.LINEAGE_TOO_LARGE,
                        f"{decision_id}: the lineage would exceed "
                        f"{_lineage.MAX_LINEAGE_BYTES} bytes; nothing was written",
                    ),
                )
            status_changes = outcome.new_status != adr.status
            new_status: DecisionStatus | str = outcome.new_status
            if status_changes:
                try:
                    new_status = DecisionStatus(outcome.new_status)
                except ValueError:
                    pass  # _require_known_status rejects it below
                # Checked before anything is written: _save_decisions does not.
                _require_known_status(adr.model_copy(update={"status": new_status}))
            _save_yaml(path, outcome.doc, _lineage.lineage_header_name(decision_id))
            if status_changes:
                old_status = adr.status
                adr.status = new_status
                try:
                    _save_decisions(pm_path, decisions)
                except OSError:
                    adr.status = old_status
                    dropped = result.changes.pop("decision_status", {})
                    result.decision_status = dropped.get("from", result.decision_status)
                    # apply_change said a prior mismatch was resolved; it was not,
                    # and decision_status_not_projected below says what is left.
                    result.warnings = [
                        warning
                        for warning in result.warnings
                        if warning.get("code") != "decision_status_mismatch_resolved"
                    ]
                    result.warnings.append(
                        _lineage.notice(
                            "warning",
                            "decision_status_not_projected",
                            f"{decision_id}: the lineage was saved, but decisions.yaml could "
                            "not be updated, so its status still shows the old value.",
                            f"Call pm_update_decision on {decision_id} again with the same "
                            "lifecycle to project the status.",
                        )
                    )
    notices, hint = _counterpart_hints(pm_path, decision_id, outcome.added_links)
    result.warnings.extend(notices)
    result.next = hint
    return result


# ─── Milestones ──────────────────────────────────────


def load_milestones(pm_path: Path) -> list[Milestone]:
    """Load milestones from milestones.yaml."""
    data = _load_yaml(pm_path / "milestones.yaml")
    if data is None or not isinstance(data, dict) or "milestones" not in data:
        return []
    return [Milestone(**m) for m in data["milestones"]]


def _save_milestones(pm_path: Path, milestones: list[Milestone]) -> None:
    """Save milestones to milestones.yaml."""
    _save_yaml(
        pm_path / "milestones.yaml",
        {"milestones": [_model_dump(m) for m in milestones]},
        "milestones.yaml",
    )


def add_milestone(pm_path: Path, milestone: Milestone) -> Milestone:
    """Append a new milestone and save."""
    with _yaml_transaction(pm_path, "milestones.yaml"):
        milestones = load_milestones(pm_path)
        milestones.append(milestone)
        _save_milestones(pm_path, milestones)
    return milestone


# ─── Risks ───────────────────────────────────────────


def load_risks(pm_path: Path) -> list[Risk]:
    """Load risks from risks.yaml."""
    data = _load_yaml(pm_path / "risks.yaml")
    if data is None or not isinstance(data, dict) or "risks" not in data:
        return []
    return [Risk(**r) for r in data["risks"]]


def _save_risks(pm_path: Path, risks: list[Risk]) -> None:
    """Save risks to risks.yaml."""
    _save_yaml(
        pm_path / "risks.yaml",
        {"risks": [_model_dump(r) for r in risks]},
        "risks.yaml",
    )


def add_risk(pm_path: Path, risk: Risk) -> Risk:
    """Append a new risk and save."""
    with _yaml_transaction(pm_path, "risks.yaml"):
        risks = load_risks(pm_path)
        risks.append(risk)
        _save_risks(pm_path, risks)
    return risk


def next_risk_number(pm_path: Path) -> int:
    """Return the next available risk number."""
    risks = load_risks(pm_path)
    if not risks:
        return 1
    numbers = []
    for r in risks:
        parts = r.id.rsplit("-", 1)
        if len(parts) == 2 and parts[1].isdigit():
            numbers.append(int(parts[1]))
    return max(numbers, default=0) + 1


# ─── Knowledge Records ──────────────────────────────


def load_knowledge(pm_path: Path) -> list[KnowledgeRecord]:
    """Load all knowledge records from knowledge.yaml."""
    data = _load_yaml(pm_path / "knowledge.yaml")
    if data is None or not isinstance(data, dict) or "knowledge" not in data:
        return []
    return [KnowledgeRecord(**k) for k in data["knowledge"]]


def _save_knowledge(pm_path: Path, records: list[KnowledgeRecord]) -> None:
    """Save all knowledge records to knowledge.yaml (other top-level keys are kept)."""
    path = pm_path / "knowledge.yaml"
    _save_yaml(
        path,
        _with_sibling_keys(path, "knowledge", [_model_dump(r) for r in records]),
        "knowledge.yaml",
    )


def add_knowledge(pm_path: Path, record: KnowledgeRecord) -> KnowledgeRecord:
    """Append a new knowledge record and save."""
    with _yaml_transaction(pm_path, "knowledge.yaml"):
        records = load_knowledge(pm_path)
        _reject_duplicate_id((r.id for r in records), record.id, "knowledge.yaml")
        records.append(record)
        _save_knowledge(pm_path, records)
    return record


def add_knowledge_with_next_id(
    pm_path: Path, build: Callable[[int], KnowledgeRecord]
) -> KnowledgeRecord:
    """Number and append a knowledge record inside ONE transaction (PMSERV-219).

    ``build`` runs under the lock — see :func:`add_task_with_next_id`.
    """
    with _yaml_transaction(pm_path, "knowledge.yaml"):
        records = load_knowledge(pm_path)
        record = build(_next_number_from_ids(r.id for r in records))
        _reject_duplicate_id((r.id for r in records), record.id, "knowledge.yaml")
        records.append(record)
        _save_knowledge(pm_path, records)
    return record


def update_knowledge(pm_path: Path, record_id: str, **updates) -> KnowledgeRecord:
    """Update fields on an existing knowledge record by ID."""
    with _yaml_transaction(pm_path, "knowledge.yaml"):
        records = load_knowledge(pm_path)
        for rec in records:
            if rec.id == record_id:
                for key, value in updates.items():
                    if value is not None and hasattr(rec, key):
                        setattr(rec, key, value)
                rec.updated = _dt.date.today()
                _save_knowledge(pm_path, records)
                return rec
    raise KnowledgeNotFoundError(f"Knowledge record {record_id} not found")


def next_knowledge_number(pm_path: Path) -> int:
    """Return the next available knowledge record number (read-only preview, no lock)."""
    return _next_number_from_ids(r.id for r in load_knowledge(pm_path))


# ─── Daily Log ───────────────────────────────────────


def load_daily_log(pm_path: Path, log_date: _dt.date | None = None) -> DailyLog:
    """Load a daily log for the given date (default: today)."""
    log_date = log_date or _dt.date.today()
    log_file = pm_path / "daily" / f"{log_date.isoformat()}.yaml"
    data = _load_yaml(log_file)
    if data is None:
        return DailyLog(date=log_date)
    return DailyLog(**data)


def add_daily_log(
    pm_path: Path, entry: DailyLogEntry, log_date: _dt.date | None = None
) -> DailyLog:
    """Append an entry to today's daily log."""
    log_date = log_date or _dt.date.today()
    daily_dir = pm_path / "daily"
    daily_dir.mkdir(exist_ok=True)
    log_file = daily_dir / f"{log_date.isoformat()}.yaml"

    with _yaml_transaction(pm_path, f"daily-{log_date.isoformat()}"):
        log = load_daily_log(pm_path, log_date)
        log.entries.append(entry)
        _save_yaml(log_file, _model_dump(log), f"daily/{log_date.isoformat()}.yaml")
    return log


# ─── Registry ────────────────────────────────────────


def load_registry(registry_dir: Path | None = None) -> Registry:
    """Load the global registry. Creates empty Registry if not found."""
    registry_dir = registry_dir or GLOBAL_PM_DIR
    data = _load_yaml(registry_dir / "registry.yaml")
    if data is None:
        return Registry()
    return Registry(**data)


def _save_registry(registry: Registry, registry_dir: Path | None = None) -> None:
    """Save the global registry."""
    registry_dir = registry_dir or GLOBAL_PM_DIR
    registry_dir.mkdir(parents=True, exist_ok=True)
    _save_yaml(registry_dir / "registry.yaml", _model_dump(registry), "registry.yaml")


def register_project(project_path: Path, name: str, registry_dir: Path | None = None) -> Registry:
    """Register a project in the global registry. Idempotent."""
    base_dir = registry_dir or GLOBAL_PM_DIR
    base_dir.mkdir(parents=True, exist_ok=True)
    with _yaml_transaction(base_dir, "registry"):
        registry = load_registry(registry_dir)
        resolved = str(project_path.resolve())
        if any(p.path == resolved for p in registry.projects):
            return registry
        registry.projects.append(RegistryEntry(path=resolved, name=name))
        _save_registry(registry, registry_dir)
    return registry


def unregister_project(project_path: Path, registry_dir: Path | None = None) -> Registry:
    """Remove a project from the global registry."""
    base_dir = registry_dir or GLOBAL_PM_DIR
    base_dir.mkdir(parents=True, exist_ok=True)
    with _yaml_transaction(base_dir, "registry"):
        registry = load_registry(registry_dir)
        resolved = str(project_path.resolve())
        registry.projects = [p for p in registry.projects if p.path != resolved]
        _save_registry(registry, registry_dir)
    return registry


# ─── Init helpers ────────────────────────────────────


def init_pm_directory(project_path: Path) -> Path:
    """Create the .pm/ directory structure. Returns the pm_path."""
    pm_path = project_path / PM_DIR
    pm_path.mkdir(exist_ok=True)
    (pm_path / "daily").mkdir(exist_ok=True)
    return pm_path


# ─── Workflows ──────────────────────────────────────

BUILTIN_TEMPLATES_DIR = Path(__file__).parent / "templates" / "workflows"


def get_builtin_templates_dir_status() -> dict:
    """Return sanity-check info for ``BUILTIN_TEMPLATES_DIR`` (PMSERV-068).

    Captures the stale-module-cache pattern documented in the 2026-05-08
    incident: ``BUILTIN_TEMPLATES_DIR`` is resolved relative to ``__file__``
    at module-import time. If the wheel is later uninstalled in the same
    Python env (typically by ``pip install -e .``), the path remains in
    memory but no longer exists on disk, and ``list_workflow_templates``
    silently returns zero built-ins. Surfacing this state lets callers
    flag the MCP server for a restart instead of misreading the empty
    list as "no templates available".
    """
    path = BUILTIN_TEMPLATES_DIR
    exists = path.is_dir()
    template_count = 0
    if exists:
        try:
            template_count = sum(1 for _ in path.glob("*.yaml"))
        except OSError:
            template_count = -1
    return {
        "path": str(path),
        "exists": exists,
        "template_count": template_count,
        "stale": not exists,
    }


def load_workflows(pm_path: Path) -> list[Workflow]:
    """Load all workflows from workflows.yaml."""
    data = _load_yaml(pm_path / "workflows.yaml")
    if data is None or not isinstance(data, dict) or "workflows" not in data:
        return []
    return [Workflow(**w) for w in data["workflows"]]


def _save_workflows(pm_path: Path, workflows: list[Workflow]) -> None:
    """Save all workflows to workflows.yaml."""
    _save_yaml(
        pm_path / "workflows.yaml",
        {"workflows": [_model_dump(w) for w in workflows]},
        "workflows.yaml",
    )


def add_workflow(pm_path: Path, workflow: Workflow) -> Workflow:
    """Append a new workflow and save."""
    with _yaml_transaction(pm_path, "workflows.yaml"):
        workflows = load_workflows(pm_path)
        _reject_duplicate_id((w.id for w in workflows), workflow.id, "workflows.yaml")
        workflows.append(workflow)
        _save_workflows(pm_path, workflows)
    return workflow


def add_workflow_with_next_id(pm_path: Path, build: Callable[[int], Workflow]) -> Workflow:
    """Number and append a workflow inside ONE workflows.yaml transaction (PMSERV-219).

    ``build`` runs under the lock — see :func:`add_task_with_next_id`.
    """
    with _yaml_transaction(pm_path, "workflows.yaml"):
        workflows = load_workflows(pm_path)
        workflow = build(_next_number_from_ids(w.id for w in workflows))
        _reject_duplicate_id((w.id for w in workflows), workflow.id, "workflows.yaml")
        workflows.append(workflow)
        _save_workflows(pm_path, workflows)
    return workflow


def update_workflow(pm_path: Path, workflow_id: str, **updates) -> Workflow:
    """Update fields on an existing workflow by ID."""
    with _yaml_transaction(pm_path, "workflows.yaml"):
        workflows = load_workflows(pm_path)
        for wf in workflows:
            if wf.id == workflow_id:
                for key, value in updates.items():
                    if value is not None and hasattr(wf, key):
                        setattr(wf, key, value)
                wf.updated = _dt.date.today()
                _save_workflows(pm_path, workflows)
                return wf
    raise WorkflowNotFoundError(f"Workflow {workflow_id} not found")


def next_workflow_number(pm_path: Path) -> int:
    """Return the next available workflow number (read-only preview, no lock)."""
    return _next_number_from_ids(w.id for w in load_workflows(pm_path))


def load_workflow_template(name: str, pm_path: Path | None = None) -> WorkflowTemplate:
    """Load a workflow template by name.

    Resolution order:
    1. Custom: .pm/workflow_templates/{name}.yaml
    2. Built-in: templates/workflows/{name}.yaml
    """
    # Custom template
    if pm_path:
        custom_path = pm_path / "workflow_templates" / f"{name}.yaml"
        if custom_path.exists():
            data = _load_yaml(custom_path)
            if data:
                return _parse_workflow_template(data)

    # Built-in template
    builtin_path = BUILTIN_TEMPLATES_DIR / f"{name}.yaml"
    if builtin_path.exists():
        data = _load_yaml(builtin_path)
        if data:
            return _parse_workflow_template(data)

    raise PmServerError(f"Workflow template '{name}' not found")


def list_workflow_templates(pm_path: Path | None = None) -> list[dict]:
    """List all available workflow templates (built-in + custom)."""
    templates: list[dict] = []
    seen: set[str] = set()

    # Custom templates (higher priority, listed first)
    if pm_path:
        custom_dir = pm_path / "workflow_templates"
        if custom_dir.is_dir():
            for f in sorted(custom_dir.glob("*.yaml")):
                name = f.stem
                seen.add(name)
                data = _load_yaml(f)
                if data:
                    tmpl = _parse_workflow_template(data)
                    templates.append(
                        {
                            "name": name,
                            "description": tmpl.description,
                            "steps": len(tmpl.steps),
                            "chain_to": tmpl.chain_to,
                            "source": "custom",
                        }
                    )

    # Built-in templates
    if BUILTIN_TEMPLATES_DIR.is_dir():
        for f in sorted(BUILTIN_TEMPLATES_DIR.glob("*.yaml")):
            name = f.stem
            if name in seen:
                continue  # custom overrides built-in
            data = _load_yaml(f)
            if data:
                tmpl = _parse_workflow_template(data)
                templates.append(
                    {
                        "name": name,
                        "description": tmpl.description,
                        "steps": len(tmpl.steps),
                        "chain_to": tmpl.chain_to,
                        "source": "builtin",
                    }
                )

    return templates


def _parse_workflow_template(data: dict) -> WorkflowTemplate:
    """Parse raw YAML data into a WorkflowTemplate."""
    steps = [WorkflowStep(**s) for s in data.get("steps", [])]
    return WorkflowTemplate(
        name=data.get("name", ""),
        description=data.get("description", ""),
        chain_to=data.get("chain_to"),
        steps=steps,
    )
