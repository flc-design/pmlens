"""FastMCP server with all PM Server tools."""

from __future__ import annotations

import datetime as _dt
import os
import uuid
from pathlib import Path

from fastmcp import FastMCP

from . import storage as _storage
from .discovery import detect_project_info, discover_projects
from .memory import MemoryStore, _has_pm_server_schema
from .models import (
    ConfidenceLevel,
    Consequences,
    DailyLogEntry,
    Decision,
    IssueSeverity,
    KnowledgeCategory,
    KnowledgeRecord,
    KnowledgeStatus,
    LogCategory,
    Memory,
    MemoryType,
    PhaseStatus,
    PmServerError,
    Priority,
    Project,
    ProjectNotFoundError,
    ProjectStatus,
    RegistryEntry,
    RiskStatus,
    SessionSummary,
    Task,
    TaskNotFoundError,
    TaskStatus,
    WorkflowStatus,
)
from .outbox import default_outbox_db_path, get_outbox_store
from .storage import (
    GLOBAL_PM_DIR,
    _next_task_number_from_list,
    _yaml_transaction,
    add_daily_log,
    add_decision,
    add_knowledge,
    add_task,
    get_builtin_templates_dir_status,
    init_pm_directory,
    list_workflow_templates,
    load_knowledge,
    load_project,
    load_registry,
    load_risks,
    load_tasks,
    load_workflows,
    next_decision_number,
    next_knowledge_number,
    next_task_number,
    register_project,
    save_project,
    save_registry,
    save_tasks,
    update_knowledge,
    update_task,
)
from .utils import (
    aggregate_task_status,
    calculate_phase_progress,
    generate_decision_id,
    generate_task_id,
    get_utils_fingerprint,
    resolve_project_path,
)
from .velocity import calculate_velocity, detect_risks
from .workflow import abandon_workflow, advance_step, start_workflow, workflow_status

mcp = FastMCP("pm-server")


# ─── Lens Mode (PMSERV-079, WF-025) ──────────────────
# Claude Desktop/Cowork 向けの read-only 配布モード。PM_LENS=1 が立った時、
# RO_ALLOWLIST のツールのみ FastMCP に登録される。mutator/subprocess 経路は
# 全て除外され、ADR-015/017/018 の不変条件が構造的に保証される。

PM_LENS_ENABLED: bool = os.environ.get("PM_LENS", "").lower() in {"1", "true", "yes", "on"}

# ─── Desktop Outbox Mode (ADR-019, WF-028) ──────────
# PM_LENS=1 + PM_DESKTOP_WRITE=1 で Claude Desktop が ~/.pm/desktop/desktop.db
# に書き込み可能になる。main `.pm/memory.db` の不変条件は保持されたまま、
# OUTBOX_WRITE_ALLOWLIST の限定 tools のみ writable surface として開放される。

PM_DESKTOP_WRITE_ENABLED: bool = os.environ.get("PM_DESKTOP_WRITE", "").lower() in {
    "1",
    "true",
    "yes",
    "on",
}

RO_ALLOWLIST: frozenset[str] = frozenset(
    {
        "pm_status",
        "pm_next",
        "pm_tasks",
        "pm_blockers",
        "pm_recall",
        "pm_memory_search",
        "pm_memory_stats",
        "pm_dashboard",
        "pm_risks",
        "pm_velocity",
        "pm_list",
        "pm_knowledge_query",
        "pm_workflow_status",
        "pm_workflow_list",
        "pm_workflow_templates",
    }
)

# Tools that are read-only on main memory.db but RW on the Desktop outbox
# (~/.pm/desktop/desktop.db). Registered under PM_LENS=1 only when
# PM_DESKTOP_WRITE=1 is also set, otherwise hidden alongside other mutators.
OUTBOX_WRITE_ALLOWLIST: frozenset[str] = frozenset(
    {
        "pm_outbox_remember",
        "pm_outbox_log",
    }
)

REGISTERED_TOOLS: set[str] = set()


def _tool():
    """``@_tool()`` をラップして PM_LENS / PM_DESKTOP_WRITE モードを尊重する。

    Gating logic:
    - PM_LENS=0 (Claude Code): すべての tool を登録する。
    - PM_LENS=1, PM_DESKTOP_WRITE=0 (Lens viewer): RO_ALLOWLIST のみ登録、
      RO_ALLOWLIST 外は素の関数を返して MCP 経由で不可視にする。
    - PM_LENS=1, PM_DESKTOP_WRITE=1 (Desktop outbox host, ADR-019): 上記に加えて
      OUTBOX_WRITE_ALLOWLIST も登録する。これにより pm_outbox_remember /
      pm_outbox_log が Desktop で reach 可能となり Phase 2 の機能経路が成立する。
    """

    def decorator(fn):
        tool_name = fn.__name__
        if PM_LENS_ENABLED and tool_name not in RO_ALLOWLIST:
            if not (PM_DESKTOP_WRITE_ENABLED and tool_name in OUTBOX_WRITE_ALLOWLIST):
                return fn
        REGISTERED_TOOLS.add(tool_name)
        return mcp.tool()(fn)

    return decorator


def _outbox_host_id() -> str:
    """Identify the writing host on outbox entries.

    PM_LENS=1 の Desktop (or Cowork) は "claude-desktop"、Claude Code default
    は "claude-code"。LLM が pending list を眺めたとき origin が判別可能な
    ことが outbox UX の前提なので env 由来で機械的に決定する。
    """
    return "claude-desktop" if PM_LENS_ENABLED else "claude-code"


# ─── Session ID (one per server process = one per Claude Code session) ───

_current_session_id: str = (
    f"sess-{_dt.datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
)

# ─── Multi-session disambiguation (PMSERV-049) ──────

_AMBIGUITY_WINDOW_DEFAULT = 30  # minutes


def _get_ambiguity_window() -> int:
    """Read ambiguity window from env each call so tests can monkeypatch."""
    raw = os.getenv("PM_SERVER_RECALL_AMBIGUITY_WINDOW_MIN")
    if not raw:
        return _AMBIGUITY_WINDOW_DEFAULT
    try:
        return int(raw)
    except ValueError:
        return _AMBIGUITY_WINDOW_DEFAULT


def _detect_session_ambiguity(
    store: MemoryStore,
    current_session_id: str,
    window_minutes: int = 30,
) -> tuple[bool, list[SessionSummary]]:
    """Detect when multiple sessions have produced summaries within window.

    Returns (ambiguity_detected, candidates). Ambiguity is flagged when at
    least two distinct session_ids appear in summaries updated within the
    window — that's when the caller cannot tell which "last_session" is theirs.
    """
    summaries = store.list_summaries_within(window_minutes=window_minutes, limit=10)
    distinct_sessions = {s.session_id for s in summaries}
    return (len(distinct_sessions) >= 2, summaries)


# ─── Memory store cache (lazy init per project) ─────

_memory_stores: dict[str, MemoryStore] = {}

# PMSERV-091/093: explanatory note appended to read-tool responses when the
# Lens MemoryStore fell back to an empty in-memory store (DB absent OR
# exists-but-uninitialized). Lets users distinguish "no records yet" from
# "store unavailable" without a stack trace.
_LENS_FALLBACK_NOTE: str = (
    "memory store not initialized for this project (Lens fallback to in-memory). "
    "Use Claude Code in this project to record memories — "
    "Lens (PM_LENS=1) is read-only."
)


def _maybe_add_lens_note(result: dict, store: MemoryStore) -> dict:
    """Append the Lens-fallback note to a tool result when applicable.

    No-op when the store was opened against an initialized DB. Only intended
    for local-DB result dicts; cross-project paths should not call this since
    they read the global index regardless of local state (PMSERV-093).
    """
    if getattr(store, "lens_fallback", False):
        result["note"] = _LENS_FALLBACK_NOTE
    return result


def _get_memory_store(project_path: str | None) -> MemoryStore:
    """Get or create a MemoryStore for the project.

    PM_LENS=1 (PMSERV-080 R5): open the on-disk DB with mode=ro&immutable=1
    so the Desktop/Cowork host never creates -wal/-shm sidecars in another
    project's .pm/. If the DB file does not exist OR exists-but-uninitialized
    (PMSERV-093 — e.g. touched by an older install or partial init), fall
    back to an in-memory empty store so read queries return empty results
    without raising ``sqlite3.OperationalError``. The fallback flag flows
    through ``MemoryStore.lens_fallback`` so tools can attach an explanatory
    note via ``_maybe_add_lens_note`` (PMSERV-091).
    """
    pm_path = _get_pm_path(project_path)
    key = str(pm_path)
    if key not in _memory_stores:
        db_path = pm_path / "memory.db"
        if PM_LENS_ENABLED:
            if db_path.exists() and _has_pm_server_schema(db_path):
                _memory_stores[key] = MemoryStore(db_path, readonly=True)
            else:
                _memory_stores[key] = MemoryStore(Path(":memory:"), lens_fallback=True)
        else:
            global_db_path = _storage.GLOBAL_PM_DIR / "memory.db"
            _memory_stores[key] = MemoryStore(db_path, global_db_path=global_db_path)
    return _memory_stores[key]


# ─── Helpers ─────────────────────────────────────────


def _get_pm_path(project_path: str | None) -> Path:
    """Resolve project and return .pm/ path."""
    root = resolve_project_path(project_path)
    return root / ".pm"


def _task_summary(task: Task) -> dict:
    """Convert a Task to a concise dict for tool output."""
    result = {
        "id": task.id,
        "title": task.title,
        "status": task.status.value,
        "priority": task.priority.value,
        "phase": task.phase,
        "tags": task.tags,
        "blocked_by": task.blocked_by,
    }
    if task.parent_id:
        result["parent_id"] = task.parent_id
    if task.severity is not None:
        result["severity"] = task.severity.value
    return result


def _get_active_tasks(pm_path: Path) -> list[Task]:
    """Return in-progress tasks for the project."""
    tasks = load_tasks(pm_path)
    return [t for t in tasks if t.status == TaskStatus.IN_PROGRESS]


def _build_next_actions(active_tasks: list[dict], all_tasks: list[Task]) -> list[str]:
    """Build contextual PM action reminders based on current state."""
    actions = []

    if active_tasks:
        ids = ", ".join(t["id"] for t in active_tasks)
        actions.append(f"Call pm_update_task when tasks are done (active: {ids})")
        actions.append("Call pm_remember when you discover something important")
    else:
        todo = [t for t in all_tasks if t.status == TaskStatus.TODO]
        if todo:
            actions.append("Call pm_update_task to start a task (set in_progress)")

    actions.append("Call pm_log after completing work")
    actions.append("Call pm_session_summary before ending the session")

    return actions


# ─── Project Management ─────────────────────────────


@_tool()
def pm_init(project_path: str | None = None, project_name: str | None = None) -> dict:
    """Initialize PM for a project.

    Creates .pm/ directory, auto-detects project info, and registers in global registry.
    project_path defaults to current directory.
    project_name defaults to directory name or detected from config files.
    """
    root = Path(project_path).resolve() if project_path else Path.cwd().resolve()
    pm_path = init_pm_directory(root)

    # Detect project info
    info = detect_project_info(root)
    if project_name:
        info["name"] = project_name
        info["display_name"] = project_name

    # Only create project.yaml if it doesn't already exist (idempotent)
    project_yaml = pm_path / "project.yaml"
    if project_yaml.exists():
        project = load_project(pm_path)
    else:
        project = Project(
            name=info["name"],
            display_name=info.get("display_name", info["name"]),
            version=info.get("version", "0.1.0"),
            status=ProjectStatus.DEVELOPMENT,
            started=_dt.date.today(),
            repository=info.get("repository"),
            description=info.get("description", ""),
        )
        with _yaml_transaction(pm_path, "project.yaml"):
            save_project(pm_path, project)

    # Register in global registry
    register_project(root, project.name)

    # Ensure CLAUDE.md has PM Server rules
    from .claudemd import ensure_claudemd

    claudemd_result = ensure_claudemd(root)

    return {
        "status": "initialized",
        "path": str(root),
        "project": project.model_dump(mode="json"),
        "claudemd": claudemd_result,
    }


@_tool()
def pm_status(project_path: str | None = None) -> dict:
    """Get current project status.

    Returns phase progress, task counts, blockers, overdue items, and velocity.
    """
    pm_path = _get_pm_path(project_path)
    project = load_project(pm_path)
    tasks = load_tasks(pm_path)

    status_counts = aggregate_task_status(tasks)

    # Phase progress
    phase_info = []
    for phase in project.phases:
        p = calculate_phase_progress(tasks, phase)
        p["progress"] = f"{p['done']}/{p['total']}" if p["total"] > 0 else "0/0"
        p["progress_pct"] = p.pop("pct")
        phase_info.append(p)

    # Blockers
    blockers = [_task_summary(t) for t in tasks if t.status == TaskStatus.BLOCKED]

    # Active tasks (in_progress)
    active_tasks = [_task_summary(t) for t in tasks if t.status == TaskStatus.IN_PROGRESS]

    # CLAUDE.md status (legacy v0.4.x key, unchanged) +
    # multi-host rule files status (PMSERV-044, additive).
    from .claudemd import get_claudemd_status
    from .rules import get_rules_status

    root = resolve_project_path(project_path)

    # Hooks status — auto-install if missing
    from .hooks import get_hooks_status, install_hooks

    hooks_status = get_hooks_status()
    if not hooks_status["installed"]:
        install_hooks()
        hooks_status = get_hooks_status()

    # Next PM actions — contextual reminders for the LLM
    next_actions = _build_next_actions(active_tasks, tasks)

    # ADR-019 / WF-028 — Desktop outbox pending count, Claude Code mode only.
    # Hidden under PM_LENS=1 to avoid implying "you should merge" on a host
    # that has no merger tools registered. Surfacing only when there are
    # entries to merge keeps pm_status output uncluttered.
    diagnostics: dict = {
        "utils_fingerprint": get_utils_fingerprint(),
        "builtin_templates_dir": get_builtin_templates_dir_status(),
    }
    if not PM_LENS_ENABLED:
        try:
            outbox_pending = get_outbox_store(db_path=default_outbox_db_path()).get_pending_count()
        except Exception:
            outbox_pending = 0
        diagnostics["outbox_pending"] = outbox_pending
        if outbox_pending > 0:
            next_actions = [
                *next_actions,
                f"Desktop outbox has {outbox_pending} pending entries — "
                "call pm_outbox_pending to review and pm_outbox_merge to promote",
            ]

    return {
        "project": {
            "name": project.name,
            "display_name": project.display_name,
            "version": project.version,
            "status": project.status.value,
        },
        "tasks": {
            "total": len(tasks),
            **status_counts,
        },
        "phases": phase_info,
        "blockers": blockers,
        "active_tasks": active_tasks,
        "health": project.health.model_dump(),
        "claudemd": get_claudemd_status(root),
        "rules": get_rules_status(root),
        "hooks": hooks_status,
        "next_pm_actions": next_actions,
        "diagnostics": diagnostics,
    }


@_tool()
def pm_tasks(
    project_path: str | None = None,
    status: str | None = None,
    phase: str | None = None,
    priority: str | None = None,
    tag: str | None = None,
    parent_id: str | None = None,
) -> list:
    """List tasks with optional filters.

    Filter by status (todo/in_progress/review/done/blocked),
    phase ID, priority (P0-P3), tag, or parent_id.
    Use parent_id to list child issues of a specific task.
    """
    pm_path = _get_pm_path(project_path)
    tasks = load_tasks(pm_path)

    if status:
        tasks = [t for t in tasks if t.status.value == status]
    if phase:
        tasks = [t for t in tasks if t.phase == phase]
    if priority:
        tasks = [t for t in tasks if t.priority.value == priority]
    if tag:
        tasks = [t for t in tasks if tag in t.tags]
    if parent_id:
        tasks = [t for t in tasks if t.parent_id == parent_id]

    return [_task_summary(t) for t in tasks]


@_tool()
def pm_add_task(
    title: str,
    phase: str,
    priority: str = "P1",
    description: str = "",
    project_path: str | None = None,
    depends_on: list[str] | None = None,
    tags: list[str] | None = None,
    estimate_hours: float | None = None,
    acceptance_criteria: list[str] | None = None,
) -> dict:
    """Add a new task. ID is auto-generated.

    priority: P0 (critical) | P1 (important) | P2 (nice-to-have) | P3 (someday)
    """
    pm_path = _get_pm_path(project_path)
    project = load_project(pm_path)
    number = next_task_number(pm_path)
    task_id = generate_task_id(project.name, number)

    task = Task(
        id=task_id,
        title=title,
        phase=phase,
        priority=Priority(priority),
        description=description,
        depends_on=depends_on or [],
        tags=tags or [],
        estimate_hours=estimate_hours,
        acceptance_criteria=acceptance_criteria or [],
    )
    add_task(pm_path, task)

    return {"status": "created", "task": _task_summary(task)}


@_tool()
def pm_update_task(
    task_id: str,
    status: str | None = None,
    priority: str | None = None,
    actual_hours: float | None = None,
    notes: str | None = None,
    blocked_by: list[str] | None = None,
    project_path: str | None = None,
) -> dict:
    """Update a task's fields. task_id format: PREFIX-001."""
    pm_path = _get_pm_path(project_path)

    updates: dict = {}
    if status:
        updates["status"] = TaskStatus(status)
    if priority:
        updates["priority"] = Priority(priority)
    if actual_hours is not None:
        updates["actual_hours"] = actual_hours
    if notes is not None:
        updates["notes"] = notes
    if blocked_by is not None:
        updates["blocked_by"] = blocked_by

    task = update_task(pm_path, task_id, **updates)
    result: dict = {"status": "updated", "task": _task_summary(task)}

    # Check if all sibling issues are done → suggest closing parent
    if status == "done" and task.parent_id:
        all_tasks = load_tasks(pm_path)
        siblings = [t for t in all_tasks if t.parent_id == task.parent_id]
        if siblings and all(s.status == TaskStatus.DONE for s in siblings):
            result["all_issues_resolved"] = True
            result["parent_id"] = task.parent_id
            result["message"] = (
                f"All issues for {task.parent_id} are resolved. "
                f"Consider marking {task.parent_id} as done."
            )

    return result


@_tool()
def pm_next(project_path: str | None = None, count: int = 3) -> list:
    """Recommend next tasks based on priority, dependencies, and phase.

    Returns up to `count` actionable tasks, sorted by urgency.
    """
    pm_path = _get_pm_path(project_path)
    tasks = load_tasks(pm_path)
    project = load_project(pm_path)

    # Only actionable tasks (todo, not blocked by incomplete tasks)
    done_ids = {t.id for t in tasks if t.status == TaskStatus.DONE}
    candidates = []

    for t in tasks:
        if t.status != TaskStatus.TODO:
            continue
        # Skip tasks with explicit blockers
        if t.blocked_by:
            continue
        # Check all dependencies are done
        if t.depends_on and not all(dep in done_ids for dep in t.depends_on):
            continue
        candidates.append(t)

    # Score: P0=100, P1=75, P2=50, P3=25, active phase bonus +50
    priority_scores = {"P0": 100, "P1": 75, "P2": 50, "P3": 25}
    active_phases = {p.id for p in project.phases if p.status == PhaseStatus.ACTIVE}

    def score(task: Task) -> int:
        s = priority_scores.get(task.priority.value, 50)
        if task.phase in active_phases:
            s += 50
        return s

    candidates.sort(key=score, reverse=True)
    return [{**_task_summary(t), "score": score(t)} for t in candidates[:count]]


@_tool()
def pm_blockers(project_path: str | None = None) -> list:
    """List all blocked tasks and their blockers."""
    pm_path = _get_pm_path(project_path)
    tasks = load_tasks(pm_path)
    blocked = [t for t in tasks if t.status == TaskStatus.BLOCKED]
    return [
        {
            **_task_summary(t),
            "blocked_by": t.blocked_by,
            "days_blocked": (_dt.date.today() - t.updated).days,
        }
        for t in blocked
    ]


def _build_warning(level: str, code: str, message: str, remediation: str | None = None) -> dict:
    """Build a structured warning entry for MCP tool responses.

    Warnings surface non-fatal side effects Claude must relay to the user.
    Shape: {level, code, message, remediation?}.
    """
    warning: dict[str, str] = {"level": level, "code": code, "message": message}
    if remediation:
        warning["remediation"] = remediation
    return warning


@_tool()
def pm_add_issue(
    parent_id: str,
    title: str,
    priority: str = "P1",
    description: str = "",
    tags: list[str] | None = None,
    severity: str = "defect",
    project_path: str | None = None,
) -> dict:
    """Add an issue (child task) to an existing task.

    Use when a defect is found during review/verification of a task, OR when an
    enhancement idea surfaces that logically belongs under the parent.

    severity gates the auto-revert behavior:
      defect      (default) → if parent is 'done', it is moved back to 'review'.
                               A 'parent_reverted' warning is emitted.
      enhancement           → parent's status is never changed. Pure backlog link.

    For *independent* backlog items not logically tied to a parent, use
    pm_add_task instead — the parent/child link is discoverable from data, so
    do not abuse pm_add_issue to create an arbitrary hierarchy.

    The response always contains a 'warnings' list. Callers (Claude) MUST
    surface any non-empty warnings to the user verbatim.

    parent_id: The ID of the parent task (e.g. 'PROJ-001').
    priority: P0 (critical) | P1 (important) | P2 (nice-to-have) | P3 (someday)
    severity: defect | enhancement
    """
    try:
        severity_enum = IssueSeverity(severity)
    except ValueError as e:
        raise PmServerError(
            f"Invalid severity {severity!r}. Must be one of: "
            f"{', '.join(s.value for s in IssueSeverity)}"
        ) from e

    pm_path = _get_pm_path(project_path)
    project = load_project(pm_path)

    # PMSERV-065 / ADR-012: compound op (child append + conditional parent
    # revert) を単一 _yaml_transaction("tasks.yaml") に統合し TOCTOU を解消。
    # storage.py docstring の規約に従い、内部で add_task / update_task を
    # 再呼び出しせず raw load_tasks / save_tasks を使う (filelock の
    # self-deadlock を回避)。next_task_number も lock 内 fresh list から
    # 計算するため id 衝突 race も同時に閉じる。
    parent_reverted = False
    with _yaml_transaction(pm_path, "tasks.yaml"):
        tasks = load_tasks(pm_path)
        parent = next((t for t in tasks if t.id == parent_id), None)
        if parent is None:
            raise TaskNotFoundError(f"Parent task {parent_id} not found")

        number = _next_task_number_from_list(tasks)
        task_id = generate_task_id(project.name, number)

        child = Task(
            id=task_id,
            title=title,
            phase=parent.phase,
            priority=Priority(priority),
            description=description,
            tags=tags or [],
            parent_id=parent_id,
            severity=severity_enum,
        )
        tasks.append(child)

        if severity_enum == IssueSeverity.DEFECT and parent.status == TaskStatus.DONE:
            parent.status = TaskStatus.REVIEW
            parent.updated = _dt.date.today()
            parent_reverted = True

        save_tasks(pm_path, tasks)

    warnings: list[dict] = []
    if parent_reverted:
        warnings.append(
            _build_warning(
                level="info",
                code="parent_reverted",
                message=(
                    f"親タスク {parent_id} を 'done' → 'review' に自動で戻しました"
                    f"（severity=defect のため）"
                ),
                remediation=(
                    f"{parent_id} の完了要件を再確認し、"
                    "この欠陥を潰してから再度 done に戻してください"
                ),
            )
        )

    result: dict = {
        "status": "created",
        "task": _task_summary(child),
        "warnings": warnings,
    }
    # Legacy fields (deprecated, kept additive for 0.4.x; scheduled for removal in 0.5.0)
    if parent_reverted:
        result["parent_reverted"] = True
        result["message"] = f"{parent_id} was 'done' → automatically moved to 'review'"
    return result


# ─── Memory ──────────────────────────────────────────


@_tool()
def pm_remember(
    content: str,
    type: str = "observation",
    task_id: str | None = None,
    decision_id: str | None = None,
    tags: str | None = None,
    project_path: str | None = None,
) -> dict:
    """Save a memory tied to the current session context.

    Memories are searchable and persist across sessions.
    Link to task_id or decision_id for structured context.
    If task_id is omitted, auto-links to the active in-progress task.
    type: observation | insight | lesson
    tags: comma-separated string (e.g. "auth,api,refactor")
    """
    store = _get_memory_store(project_path)
    pm_path = _get_pm_path(project_path)
    project = load_project(pm_path)

    # Auto-infer task_id from active in-progress task
    auto_linked = False
    if task_id is None and decision_id is None:
        active = _get_active_tasks(pm_path)
        if len(active) == 1:
            task_id = active[0].id
            auto_linked = True

    tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else []

    memory = Memory(
        session_id=_current_session_id,
        type=MemoryType(type),
        content=content,
        task_id=task_id,
        decision_id=decision_id,
        tags=tag_list,
        project=project.name,
    )
    memory_id = store.save(memory)
    result = {
        "status": "saved",
        "memory_id": memory_id,
        "session_id": _current_session_id,
        "type": type,
    }
    if auto_linked:
        result["auto_linked_task"] = task_id
    return result


@_tool()
def pm_recall(
    query: str | None = None,
    task_id: str | None = None,
    type: str | None = None,
    limit: int = 5,
    cross_project: bool = False,
    project_path: str | None = None,
) -> dict:
    """Recall memories relevant to the current context.

    With no arguments: returns last session summary + recent memories.
    With query: full-text search (FTS5).
    With task_id: memories linked to that task.
    type filter: observation | insight | lesson
    cross_project: search across all projects (Phase 3).
    """
    if cross_project:
        store = _get_memory_store(project_path)
        if not query:
            return {"status": "error", "message": "query is required for cross_project search"}
        results = store.search_global(query, limit=limit)
        return {
            "current_session_id": _current_session_id,
            "query": query,
            "cross_project": True,
            "results": results,
        }

    store = _get_memory_store(project_path)

    def _memory_dict(m: Memory) -> dict:
        return {
            "id": m.id,
            "type": m.type.value,
            "content": m.content,
            "task_id": m.task_id,
            "decision_id": m.decision_id,
            "tags": m.tags,
            "created_at": m.created_at,
            "session_id": m.session_id,
        }

    # Default: last session summary + recent memories
    if query is None and task_id is None:
        summary = store.get_latest_summary()
        recent = store.get_recent(limit=limit)
        if type:
            recent = [m for m in recent if m.type.value == type]

        ambiguity, candidates = _detect_session_ambiguity(
            store, _current_session_id, window_minutes=_get_ambiguity_window()
        )

        last_session_dict = (
            {
                "session_id": summary.session_id,
                "summary": summary.summary,
                "goals": summary.goals,
                "pending": summary.pending,
                "created_at": summary.created_at,
                "updated_at": summary.updated_at,
            }
            if summary
            else None
        )

        response: dict = {
            "current_session_id": _current_session_id,
            "last_session": last_session_dict,
            "recent_memories": [_memory_dict(m) for m in recent],
            "ambiguity_detected": ambiguity,
        }
        if ambiguity:
            response["last_session_candidates"] = [
                {
                    "session_id": c.session_id,
                    "summary_excerpt": (c.summary[:200] + ("..." if len(c.summary) > 200 else "")),
                    "created_at": c.created_at,
                    "updated_at": c.updated_at,
                    "is_current_session": c.session_id == _current_session_id,
                }
                for c in candidates
            ]
        return _maybe_add_lens_note(response, store)

    # Search by query
    if query:
        results = store.search(query, type=type, limit=limit)
        return _maybe_add_lens_note(
            {"query": query, "results": [_memory_dict(m) for m in results]}, store
        )

    # Search by task_id
    if task_id:
        results = store.get_by_task(task_id)
        if type:
            results = [m for m in results if m.type.value == type]
        return _maybe_add_lens_note(
            {"task_id": task_id, "results": [_memory_dict(m) for m in results[:limit]]},
            store,
        )

    return _maybe_add_lens_note({"results": []}, store)


@_tool()
def pm_session_summary(
    action: str = "save",
    summary: str | None = None,
    goals: str | None = None,
    pending: str | None = None,
    project_path: str | None = None,
) -> dict:
    """Manage session summaries for cross-session continuity.

    action:
      - save: Store a summary for the current session (summary required)
      - get: Retrieve the most recent session summary
      - list: Show all session summaries
    """
    store = _get_memory_store(project_path)

    match action:
        case "save":
            if not summary:
                return {"status": "error", "message": "summary is required for save action"}
            pm_path = _get_pm_path(project_path)
            project = load_project(pm_path)
            pending_list = [p.strip() for p in pending.split(",") if p.strip()] if pending else []
            sess = SessionSummary(
                session_id=_current_session_id,
                summary=summary,
                goals=goals or "",
                pending=pending_list,
                project=project.name,
            )
            summary_id = store.save_session_summary(sess)
            return {
                "status": "saved",
                "summary_id": summary_id,
                "session_id": _current_session_id,
            }

        case "get":
            latest = store.get_latest_summary()
            if latest is None:
                return {"status": "empty", "message": "No session summaries found"}
            return {
                "session_id": latest.session_id,
                "summary": latest.summary,
                "goals": latest.goals,
                "tasks_done": latest.tasks_done,
                "decisions": latest.decisions,
                "pending": latest.pending,
                "created_at": latest.created_at,
                "updated_at": latest.updated_at,
            }

        case "list":
            summaries = store.list_summaries(limit=10)
            return {
                "count": len(summaries),
                "summaries": [
                    {
                        "session_id": s.session_id,
                        "summary": s.summary[:100] + ("..." if len(s.summary) > 100 else ""),
                        "created_at": s.created_at,
                        "updated_at": s.updated_at,
                    }
                    for s in summaries
                ],
            }

        case _:
            return {"status": "error", "message": f"Unknown action: {action}. Use save/get/list"}


@_tool()
def pm_memory_search(
    query: str,
    type: str | None = None,
    tags: str | None = None,
    task_id: str | None = None,
    limit: int = 10,
    cross_project: bool = False,
    project_path: str | None = None,
) -> dict:
    """Advanced memory search with multiple filters.

    query: Full-text search query (required).
    type: Filter by memory type (observation | insight | lesson).
    tags: Comma-separated tags for AND filtering.
    task_id: Filter by associated task.
    cross_project: Search across all projects.
    """
    store = _get_memory_store(project_path)

    if cross_project:
        results = store.search_global(query, limit=limit)
        if tags:
            tag_set = {t.strip() for t in tags.split(",") if t.strip()}
            results = [r for r in results if tag_set.issubset(set(r.get("tags", [])))]
        return {"query": query, "cross_project": True, "results": results[:limit]}

    results = store.search(query, type=type, limit=limit * 2)

    # Apply additional filters
    if tags:
        tag_set = {t.strip() for t in tags.split(",") if t.strip()}
        results = [m for m in results if tag_set.issubset(set(m.tags))]
    if task_id:
        results = [m for m in results if m.task_id == task_id]

    def _result_dict(m: Memory) -> dict:
        return {
            "id": m.id,
            "type": m.type.value,
            "content": m.content,
            "task_id": m.task_id,
            "decision_id": m.decision_id,
            "tags": m.tags,
            "created_at": m.created_at,
            "session_id": m.session_id,
        }

    return _maybe_add_lens_note(
        {
            "query": query,
            "filters": {"type": type, "tags": tags, "task_id": task_id},
            "results": [_result_dict(m) for m in results[:limit]],
        },
        store,
    )


# ─── Memory Operations ──────────────────────────────


@_tool()
def pm_memory_stats(project_path: str | None = None) -> dict:
    """Show memory statistics for the current project.

    Returns total count, breakdown by type, session count,
    summary count, date range, and DB size.
    """
    store = _get_memory_store(project_path)
    stats = store.get_stats()

    # Add human-readable DB size
    size = stats["db_size_bytes"]
    if size < 1024:
        stats["db_size"] = f"{size} B"
    elif size < 1024 * 1024:
        stats["db_size"] = f"{size / 1024:.1f} KB"
    else:
        stats["db_size"] = f"{size / (1024 * 1024):.1f} MB"

    return _maybe_add_lens_note(stats, store)


@_tool()
def pm_memory_cleanup(
    older_than_days: int | None = None,
    keep_latest: int | None = None,
    session_id: str | None = None,
    dry_run: bool = True,
    project_path: str | None = None,
) -> dict:
    """Clean up old memories.

    Specify at least one criterion:
      older_than_days: Delete memories older than N days.
      keep_latest: Keep only the latest N memories, delete rest.
      session_id: Delete all memories from a specific session.

    dry_run (default True): Preview what would be deleted without deleting.
    Set dry_run=False to actually delete.
    """
    store = _get_memory_store(project_path)
    return store.cleanup(
        older_than_days=older_than_days,
        keep_latest=keep_latest,
        session_id=session_id,
        dry_run=dry_run,
    )


# ─── Recording ───────────────────────────────────────


@_tool()
def pm_log(
    entry: str,
    category: str = "progress",
    task_id: str | None = None,
    project_path: str | None = None,
) -> dict:
    """Add an entry to today's daily log.

    category: progress | decision | blocker | note | milestone
    If task_id is omitted, auto-links to the active in-progress task.
    """
    pm_path = _get_pm_path(project_path)

    # Auto-infer task_id from active in-progress task
    auto_linked = False
    if task_id is None:
        active = _get_active_tasks(pm_path)
        if len(active) == 1:
            task_id = active[0].id
            auto_linked = True

    # Prepend task_id to entry for traceability
    log_text = f"[{task_id}] {entry}" if task_id else entry

    now = _dt.datetime.now()
    log_entry = DailyLogEntry(
        time=now.strftime("%H:%M"),
        category=LogCategory(category),
        entry=log_text,
    )
    log = add_daily_log(pm_path, log_entry)
    result: dict = {
        "status": "logged",
        "date": log.date.isoformat(),
        "entries_today": len(log.entries),
    }
    if auto_linked:
        result["auto_linked_task"] = task_id
    return result


# ─── Desktop Outbox tools (ADR-019, WF-028) ──────────
# Reach pattern:
# - pm_outbox_remember / pm_outbox_log : Desktop RW under PM_LENS=1 +
#   PM_DESKTOP_WRITE=1 (OUTBOX_WRITE_ALLOWLIST). Claude Code can also call
#   them under PM_LENS=0 (dual-use, no-op gating).
# - Note field embeds next-action guidance per UX principle established in
#   v0.7.1 hotfix (memory:148): "状況 + 次行動 を tool response に embed".

_OUTBOX_REMEMBER_NOTE = (
    "Saved to ~/.pm/desktop/desktop.db (Desktop outbox). "
    "In Claude Code, call pm_outbox_pending to review and "
    "pm_outbox_merge to promote into the project's main memory store."
)

_OUTBOX_LOG_NOTE = (
    "Logged to Desktop outbox. "
    "Merge in Claude Code via pm_outbox_merge to append to daily/YYYY-MM-DD.yaml."
)


@_tool()
def pm_outbox_remember(
    content: str,
    type: str = "memory",
    source_project: str | None = None,
    tags: str | None = None,
) -> dict:
    """Capture a memory or lesson from Claude Desktop into the cross-host outbox.

    Writes to ~/.pm/desktop/desktop.db (separate from any project's main
    memory store). Use pm_outbox_pending + pm_outbox_merge from Claude Code
    to promote entries into the target project's memory.db.

    type: memory | lesson (artifact is reserved for Phase 2.2)
    source_project: optional project path hint for the merger
    tags: comma-separated string
    """
    if type not in {"memory", "lesson"}:
        return {
            "status": "error",
            "code": "invalid_type",
            "message": f"type must be 'memory' or 'lesson', got {type!r}",
        }
    store = get_outbox_store(db_path=default_outbox_db_path())
    outbox_id = store.append(
        host_id=_outbox_host_id(),
        source_session=_current_session_id,
        type=type,  # type: ignore[arg-type]
        content=content,
        source_project=source_project,
        tags=tags,
    )
    return {
        "status": "saved",
        "outbox_id": outbox_id,
        "session_id": _current_session_id,
        "type": type,
        "note": _OUTBOX_REMEMBER_NOTE,
    }


@_tool()
def pm_outbox_log(
    entry: str,
    category: str = "progress",
    source_project: str | None = None,
) -> dict:
    """Capture a daily-log entry from Claude Desktop into the cross-host outbox.

    Stored as type='log' so that pm_outbox_merge can later append it to the
    target project's daily/YYYY-MM-DD.yaml. category mirrors pm_log
    (progress | decision | blocker | note | milestone) and is embedded into
    the content for downstream parsing.
    """
    valid_categories = {"progress", "decision", "blocker", "note", "milestone"}
    if category not in valid_categories:
        return {
            "status": "error",
            "code": "invalid_category",
            "message": f"category must be one of {sorted(valid_categories)}, got {category!r}",
        }
    store = get_outbox_store(db_path=default_outbox_db_path())
    # Prefix with category for traceability after merge into the daily log.
    log_content = f"[{category}] {entry}"
    outbox_id = store.append(
        host_id=_outbox_host_id(),
        source_session=_current_session_id,
        type="log",
        content=log_content,
        source_project=source_project,
        tags=category,
    )
    return {
        "status": "saved",
        "outbox_id": outbox_id,
        "session_id": _current_session_id,
        "type": "log",
        "category": category,
        "note": _OUTBOX_LOG_NOTE,
    }


# Mapping from outbox row.type → main MemoryType used by pm_outbox_merge.
# 'memory' is treated as an observation by default; the user can re-tag
# during/after merge if a different MemoryType is appropriate.
_OUTBOX_TYPE_TO_MEMORY_TYPE: dict[str, MemoryType] = {
    "memory": MemoryType.OBSERVATION,
    "lesson": MemoryType.LESSON,
}


def _parse_log_prefix(content: str, fallback_category: str = "progress") -> tuple[str, str]:
    """Extract [category] prefix from a stored log content string.

    pm_outbox_log stores ``[<category>] <entry>`` for traceability; on merge
    we want the original category back so the DailyLogEntry preserves it.
    Returns (category, entry). If parse fails, returns (fallback, content).
    """
    if content.startswith("[") and "] " in content:
        head, rest = content.split("] ", 1)
        category = head[1:]
        valid = {"progress", "decision", "blocker", "note", "milestone"}
        if category in valid:
            return category, rest
    return fallback_category, content


@_tool()
def pm_outbox_pending(
    filter_project: str | None = None,
    filter_type: str | None = None,
    filter_status: str = "pending",
    limit: int = 50,
    offset: int = 0,
) -> dict:
    """List entries in the Desktop outbox (~/.pm/desktop/desktop.db).

    Pagination (amendment f8) is mandatory: limit defaults to 50 and offset
    starts at 0. Response includes total + has_more + next_offset so the
    caller can iterate without hanging on hundreds of pending entries.

    filter_status: 'pending' (default) | 'merged' | 'rejected' | 'all'
    filter_type: 'memory' | 'log' | 'lesson' | 'artifact'
    """
    if filter_type is not None and filter_type not in {"memory", "log", "lesson", "artifact"}:
        return {
            "status": "error",
            "code": "invalid_filter_type",
            "message": (
                f"filter_type must be one of memory/log/lesson/artifact, got {filter_type!r}"
            ),
        }
    if filter_status not in {"pending", "merged", "rejected", "all"}:
        return {
            "status": "error",
            "code": "invalid_filter_status",
            "message": (
                f"filter_status must be one of pending/merged/rejected/all, got {filter_status!r}"
            ),
        }
    if limit < 0 or offset < 0:
        return {
            "status": "error",
            "code": "invalid_pagination",
            "message": "limit and offset must be non-negative",
        }

    store = get_outbox_store(db_path=default_outbox_db_path())
    page = store.pending(
        filter_project=filter_project,
        filter_type=filter_type,  # type: ignore[arg-type]
        filter_status=filter_status,
        limit=limit,
        offset=offset,
    )
    return {
        "status": "ok",
        **page,
    }


@_tool()
def pm_outbox_merge(
    ids: list[int],
    target_project: str | None = None,
) -> dict:
    """Promote pending outbox entries into the target project's main store.

    type='memory' or 'lesson' → MemoryStore.save() on .pm/memory.db.
    type='log' → add_daily_log() on .pm/daily/YYYY-MM-DD.yaml.
    type='artifact' → not yet supported (Phase 2.2); recorded as warning.

    target_project (project path) overrides each row's source_project hint.
    Already-merged / already-rejected ids are silently skipped and surface
    via warnings[] so the caller can distinguish skips from successful
    merges (idempotent guarantee).
    """
    store = get_outbox_store(db_path=default_outbox_db_path())
    merged: list[dict] = []
    skipped: list[dict] = []
    warnings: list[dict] = []

    for outbox_id in ids:
        row = store.get(outbox_id)
        if row is None:
            warnings.append({"id": outbox_id, "reason": "not_found"})
            continue
        if row["status"] != "pending":
            skipped.append(
                {"id": outbox_id, "status": row["status"], "reason": "already_processed"}
            )
            continue

        project_path = target_project or row["source_project"]
        if not project_path:
            warnings.append(
                {
                    "id": outbox_id,
                    "reason": "no_target_project",
                    "remediation": "pass target_project or set source_project on insert",
                }
            )
            continue

        row_type = row["type"]
        if row_type in ("memory", "lesson"):
            try:
                memory_store = _get_memory_store(project_path)
                pm_path = _get_pm_path(project_path)
                project = load_project(pm_path)
                memory = Memory(
                    session_id=_current_session_id,
                    type=_OUTBOX_TYPE_TO_MEMORY_TYPE[row_type],
                    content=row["content"],
                    tags=[t.strip() for t in (row["tags"] or "").split(",") if t.strip()],
                    project=project.name,
                )
                new_id = memory_store.save(memory)
            except (PmServerError, ProjectNotFoundError) as exc:
                warnings.append(
                    {"id": outbox_id, "reason": "target_store_unavailable", "detail": str(exc)}
                )
                continue
            store.mark_merged(outbox_id, new_id, str(_get_pm_path(project_path) / "memory.db"))
            merged.append(
                {
                    "id": outbox_id,
                    "type": row_type,
                    "merged_to_id": new_id,
                    "target_project": project_path,
                }
            )
        elif row_type == "log":
            try:
                pm_path = _get_pm_path(project_path)
                category, entry_text = _parse_log_prefix(row["content"])
                now = _dt.datetime.now()
                log_entry = DailyLogEntry(
                    time=now.strftime("%H:%M"),
                    category=LogCategory(category),
                    entry=entry_text,
                )
                log = add_daily_log(pm_path, log_entry)
            except (PmServerError, ProjectNotFoundError) as exc:
                warnings.append(
                    {"id": outbox_id, "reason": "target_log_unavailable", "detail": str(exc)}
                )
                continue
            target_path = f"daily/{log.date.isoformat()}.yaml"
            store.mark_merged(outbox_id, None, target_path)
            merged.append(
                {
                    "id": outbox_id,
                    "type": "log",
                    "target_path": target_path,
                    "target_project": project_path,
                }
            )
        elif row_type == "artifact":
            warnings.append(
                {
                    "id": outbox_id,
                    "reason": "artifact_merge_phase_2_2",
                    "remediation": "Phase 2.2 will add pm_outbox_merge_artifact",
                }
            )
        else:
            warnings.append({"id": outbox_id, "reason": "unknown_type", "type": row_type})

    return {
        "status": "ok" if merged or not warnings else "partial",
        "merged": merged,
        "skipped": skipped,
        "warnings": warnings,
    }


@_tool()
def pm_outbox_reject(
    ids: list[int],
    reason: str,
) -> dict:
    """Reject pending outbox entries with an auditable reason.

    Already-processed (merged / rejected) ids are silently skipped and
    surface via warnings[]. Reason is required (empty string raises a
    user-facing error response).
    """
    if not reason or not reason.strip():
        return {
            "status": "error",
            "code": "reason_required",
            "message": "reason is required and must be non-empty",
        }
    store = get_outbox_store(db_path=default_outbox_db_path())
    rejected: list[int] = []
    skipped: list[dict] = []
    warnings: list[dict] = []

    for outbox_id in ids:
        row = store.get(outbox_id)
        if row is None:
            warnings.append({"id": outbox_id, "reason": "not_found"})
            continue
        if row["status"] != "pending":
            skipped.append(
                {"id": outbox_id, "status": row["status"], "reason": "already_processed"}
            )
            continue
        if store.mark_rejected(outbox_id, reason):
            rejected.append(outbox_id)
        else:
            # Race: status changed between get() and mark_rejected().
            warnings.append({"id": outbox_id, "reason": "race_condition_skipped"})

    return {
        "status": "ok" if rejected or not warnings else "partial",
        "rejected": rejected,
        "skipped": skipped,
        "warnings": warnings,
    }


@_tool()
def pm_add_decision(
    title: str,
    context: str,
    decision: str,
    consequences_positive: list[str] | None = None,
    consequences_negative: list[str] | None = None,
    project_path: str | None = None,
) -> dict:
    """Record an Architecture Decision Record (ADR). ID is auto-generated."""
    pm_path = _get_pm_path(project_path)
    number = next_decision_number(pm_path)
    decision_id = generate_decision_id(number)

    adr = Decision(
        id=decision_id,
        title=title,
        context=context,
        decision=decision,
        consequences=Consequences(
            positive=consequences_positive or [],
            negative=consequences_negative or [],
        ),
    )
    add_decision(pm_path, adr)
    return {"status": "recorded", "decision_id": decision_id, "title": title}


# ─── Analysis ────────────────────────────────────────


@_tool()
def pm_velocity(project_path: str | None = None, weeks: int = 4) -> dict:
    """Calculate velocity over the past N weeks. Includes trend analysis."""
    pm_path = _get_pm_path(project_path)
    return calculate_velocity(pm_path, weeks)


@_tool()
def pm_risks(project_path: str | None = None) -> list:
    """List all risks and auto-detected issues.

    Auto-detects: blocked tasks, stale in-progress tasks, overdue estimates.
    Also includes manually registered risks.
    """
    pm_path = _get_pm_path(project_path)

    # Auto-detected risks
    auto_risks = detect_risks(pm_path)

    # Manually registered risks
    manual_risks = load_risks(pm_path)
    manual = [
        {
            "type": "manual",
            "risk_id": r.id,
            "title": r.title,
            "severity": r.severity.value,
            "status": r.status.value,
            "description": r.description,
        }
        for r in manual_risks
        if r.status == RiskStatus.OPEN
    ]

    return auto_risks + manual


# ─── Visualization ───────────────────────────────────


@_tool()
def pm_dashboard(project_path: str | None = None, format: str = "html") -> str:
    """Generate a project dashboard.

    project_path specified: single project view.
    project_path=None with no .pm/ in cwd: portfolio view of all registered projects.
    format: html | text
    """
    from .dashboard import render_portfolio_dashboard, render_project_dashboard

    if format == "text":
        if project_path or _has_pm_dir():
            pm_path = _get_pm_path(project_path)
            return render_project_dashboard(pm_path, format="text")
        return render_portfolio_dashboard(format="text")

    # HTML
    if project_path or _has_pm_dir():
        pm_path = _get_pm_path(project_path)
        return render_project_dashboard(pm_path, format="html")
    return render_portfolio_dashboard(format="html")


def _has_pm_dir() -> bool:
    """Check if there's a .pm/ directory accessible from cwd."""
    try:
        resolve_project_path()
        return True
    except ProjectNotFoundError:
        return False


# ─── Discovery & Management ──────────────────────────


@_tool()
def pm_discover(scan_path: str = ".") -> dict:
    """Scan for projects with ``.pm/`` directories and register them.

    The filesystem walk is performed without holding the registry lock so
    that long scans do not block concurrent sessions. The diff between the
    scan result and the current registry is then committed inside a single
    ``_yaml_transaction(GLOBAL_PM_DIR, "registry")`` (PMSERV-066):

    1. ``load_registry()`` runs **inside** the lock, closing the
       pre-existing TOCTOU window where a lock-free snapshot was used to
       decide whether to append new entries.
    2. New entries are accumulated in memory and committed via a single
       ``save_registry`` call, replacing the per-project lock acquire/
       release loop that previously took N filelocks for N projects.

    This is the same "lift the lock one level up and call raw load/save
    directly" idiom established by ADR-012 for ``pm_add_issue`` — see
    ``storage.py`` module docstring for the compound-op discipline.
    """
    found = discover_projects(Path(scan_path))
    if not found:
        return {
            "scanned": scan_path,
            "found": 0,
            "newly_registered": 0,
            "projects": [],
        }

    newly_registered: list[dict] = []
    with _yaml_transaction(GLOBAL_PM_DIR, "registry"):
        registry = load_registry()
        registered_paths = {p.path for p in registry.projects}
        for proj in found:
            resolved = str(Path(proj["path"]).resolve())
            if resolved in registered_paths:
                continue
            registry.projects.append(RegistryEntry(path=resolved, name=proj["name"]))
            registered_paths.add(resolved)
            newly_registered.append(proj)
        if newly_registered:
            save_registry(registry)

    return {
        "scanned": scan_path,
        "found": len(found),
        "newly_registered": len(newly_registered),
        "projects": newly_registered,
    }


@_tool()
def pm_cleanup() -> dict:
    """Health-check the registry. Detect and remove invalid paths.

    Also detects orphan project files in the global ~/.pm/ directory
    that may have been created by the cwd-resolution bug.
    """
    registry = load_registry()
    valid = []
    invalid = []

    for entry in registry.projects:
        pm_path = Path(entry.path) / ".pm"
        if pm_path.is_dir() and (pm_path / "project.yaml").exists():
            valid.append(entry)
        else:
            invalid.append({"path": entry.path, "name": entry.name})

    if invalid:
        with _yaml_transaction(GLOBAL_PM_DIR, "registry"):
            registry.projects = valid
            save_registry(registry)

    # Detect orphan project files in global ~/.pm/
    orphan_files: list[str] = []
    project_only_files = [
        "tasks.yaml",
        "decisions.yaml",
        "risks.yaml",
        "milestones.yaml",
    ]
    global_pm = _storage.GLOBAL_PM_DIR
    for filename in project_only_files:
        if (global_pm / filename).exists():
            orphan_files.append(filename)

    return {
        "valid": len(valid),
        "removed": len(invalid),
        "invalid_entries": invalid,
        "orphan_files_in_global": orphan_files,
    }


@_tool()
def pm_update_claudemd(project_path: str | None = None) -> dict:
    """Update the PM Server rules section in CLAUDE.md to the latest version.

    Creates CLAUDE.md if it doesn't exist.
    Uses markers to identify and replace only the PM Server section.
    Other content in CLAUDE.md is preserved.

    .. deprecated:: 0.6.0
        Backward-compat alias preserved through the v0.5.x → v1.0.0
        deprecation timeline (ADR-008 amendment 2026-04-30). New code
        should use :func:`pm_update_rules` instead. The dict response
        shape (status, message, template_version, before, after) is
        byte-stable with v0.4.x.
    """
    from .rules import TEMPLATE_VERSION, get_claudemd_status, inject_pm_rules

    root = resolve_project_path(project_path)
    before = get_claudemd_status(root)
    summary = inject_pm_rules(root, target="claude-code")
    after = get_claudemd_status(root)

    # Single-host invocation always yields exactly one result.
    legacy_message = summary.results[0].message if summary.results else ""

    # Status field hard-coded to "updated" preserves v0.4.x parity:
    # callers rely on this exact literal regardless of whether the
    # underlying transition was create/append/update (cross-check R3).
    return {
        "status": "updated",
        "message": legacy_message,
        "template_version": TEMPLATE_VERSION,
        "before": before,
        "after": after,
    }


@_tool()
def pm_update_rules(
    project_path: str | None = None,
    target: str = "auto",
    dry_run: bool = False,
) -> dict:
    """Inject PM Server rules into CLAUDE.md and/or AGENTS.md.

    Args:
        project_path: Project root. Auto-detected if omitted.
        target: One of ``"auto"`` (default; detect installed hosts via
            filesystem + marker + CLAUDECODE), ``"all"`` (force every
            known host), ``"claude-code"`` (only CLAUDE.md), or
            ``"codex"`` (only AGENTS.md).
        dry_run: If True, report what would happen without writing.

    Returns a dict with: ``overall_status``, ``detected_hosts``,
    ``detection_source`` (``"filesystem+marker+env"`` |
    ``"explicit"`` | ``"fallback"``), ``created``, ``updated``,
    ``is_dry_run``, ``results`` (per-host detail), and ``warnings``
    (surfaced when detection falls back to claude-code without any
    positive signal — pass ``target="codex"`` explicitly to opt into
    AGENTS.md when running outside a Codex-aware shell).
    """
    from .rules import inject_pm_rules

    root = resolve_project_path(project_path)
    summary = inject_pm_rules(root, target=target, dry_run=dry_run)

    warnings: list[dict] = []
    if summary.detection_source == "fallback":
        warnings.append(
            {
                "code": "host_detection_fallback",
                "message": (
                    "No host could be detected from filesystem, markers, or env. "
                    "Defaulted to claude-code only — pass target=codex explicitly "
                    "if running under Codex CLI."
                ),
                "remediation": "pm_update_rules(target='codex')",
            }
        )

    return {
        "overall_status": summary.overall_status,
        "detected_hosts": summary.detected_hosts,
        "detection_source": summary.detection_source,
        "created": summary.created,
        "updated": summary.updated,
        "is_dry_run": dry_run,
        "results": [
            {
                "target_file": r.target_file,
                "host": r.host,
                "status": r.status,
                "message": r.message,
                "backup_path": str(r.backup_path) if r.backup_path else None,
                "is_dry_run": r.is_dry_run,
            }
            for r in summary.results
        ],
        "warnings": warnings,
    }


# ─── Knowledge Records ─────────────────────────────


@_tool()
def pm_record(
    category: str,
    title: str,
    findings: str = "",
    conclusion: str = "",
    confidence: str = "medium",
    sources: list[str] | None = None,
    tags: str | None = None,
    task_id: str | None = None,
    workflow_id: str | None = None,
    project_path: str | None = None,
) -> dict:
    """Record a structured knowledge finding.

    Use this for research results, requirements, trade-off analyses, specs, etc.
    Sits between casual pm_remember (memory) and formal pm_add_decision (ADR).

    category: research | market | spike | requirement | constraint |
              tradeoff | risk_analysis | spec | api_design
    confidence: high | medium | low
    tags: comma-separated string (e.g. "auth,api,security")
    """
    pm_path = _get_pm_path(project_path)
    number = next_knowledge_number(pm_path)
    record_id = f"KR-{number:03d}"

    # Auto-infer task_id from active in-progress task
    auto_linked = False
    if task_id is None:
        active = _get_active_tasks(pm_path)
        if len(active) == 1:
            task_id = active[0].id
            auto_linked = True

    # Auto-infer workflow_id from active workflow
    auto_linked_wf = False
    if workflow_id is None:
        from .workflow import get_active_workflow

        active_wf = get_active_workflow(pm_path)
        if active_wf:
            workflow_id = active_wf.id
            auto_linked_wf = True

    tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else []

    record = KnowledgeRecord(
        id=record_id,
        category=KnowledgeCategory(category),
        title=title,
        confidence=ConfidenceLevel(confidence),
        findings=findings,
        conclusion=conclusion,
        sources=sources or [],
        tags=tag_list,
        task_id=task_id,
        workflow_id=workflow_id,
    )
    add_knowledge(pm_path, record)

    result: dict = {
        "status": "recorded",
        "record_id": record_id,
        "category": category,
        "title": title,
    }
    if auto_linked:
        result["auto_linked_task"] = task_id
    if auto_linked_wf:
        result["auto_linked_workflow"] = workflow_id
    return result


def _query_knowledge(
    pm_path: Path,
    action: str,
    record_id: str | None = None,
    category: str | None = None,
    status: str | None = None,
    tag: str | None = None,
    task_id: str | None = None,
    workflow_id: str | None = None,
) -> dict:
    """Read-only knowledge query logic shared by ``pm_knowledge`` and ``pm_knowledge_query``."""
    match action:
        case "list":
            records = load_knowledge(pm_path)
            if category:
                records = [r for r in records if r.category.value == category]
            if status:
                records = [r for r in records if r.status.value == status]
            if tag:
                records = [r for r in records if tag in r.tags]
            if task_id:
                records = [r for r in records if r.task_id == task_id]
            if workflow_id:
                records = [r for r in records if r.workflow_id == workflow_id]
            return {
                "count": len(records),
                "records": [_knowledge_summary(r) for r in records],
            }

        case "get":
            if not record_id:
                return {"status": "error", "message": "record_id required for get"}
            records = load_knowledge(pm_path)
            for r in records:
                if r.id == record_id:
                    return _knowledge_detail(r)
            return {"status": "error", "message": f"{record_id} not found"}

        case "summary":
            records = load_knowledge(pm_path)
            by_category: dict[str, int] = {}
            by_status: dict[str, int] = {}
            for r in records:
                by_category[r.category.value] = by_category.get(r.category.value, 0) + 1
                by_status[r.status.value] = by_status.get(r.status.value, 0) + 1
            return {
                "total": len(records),
                "by_category": by_category,
                "by_status": by_status,
            }

        case _:
            return {
                "status": "error",
                "message": f"Unknown action: {action}. Use list/get/summary",
            }


@_tool()
def pm_knowledge(
    action: str = "list",
    record_id: str | None = None,
    category: str | None = None,
    status: str | None = None,
    tag: str | None = None,
    task_id: str | None = None,
    workflow_id: str | None = None,
    new_status: str | None = None,
    confidence: str | None = None,
    conclusion: str | None = None,
    project_path: str | None = None,
) -> dict:
    """Query and manage knowledge records.

    action:
      - list: List records with optional filters (category, status, tag, task_id)
      - get: Get a specific record by record_id
      - update: Update a record's status/confidence/conclusion (record_id required)
      - summary: Get category-wise summary counts

    category filter: research | market | spike | requirement | constraint |
                     tradeoff | risk_analysis | spec | api_design
    status filter: draft | validated | superseded
    """
    pm_path = _get_pm_path(project_path)

    if action == "update":
        if not record_id:
            return {"status": "error", "message": "record_id required for update"}
        updates: dict = {}
        if new_status:
            updates["status"] = KnowledgeStatus(new_status)
        if confidence:
            updates["confidence"] = ConfidenceLevel(confidence)
        if conclusion:
            updates["conclusion"] = conclusion
        record = update_knowledge(pm_path, record_id, **updates)
        return {"status": "updated", "record": _knowledge_summary(record)}

    return _query_knowledge(
        pm_path,
        action,
        record_id=record_id,
        category=category,
        status=status,
        tag=tag,
        task_id=task_id,
        workflow_id=workflow_id,
    )


@_tool()
def pm_knowledge_query(
    action: str = "list",
    record_id: str | None = None,
    category: str | None = None,
    status: str | None = None,
    tag: str | None = None,
    task_id: str | None = None,
    workflow_id: str | None = None,
    project_path: str | None = None,
) -> dict:
    """Read-only knowledge query (PMSERV-079 / WF-025 / ADR-018).

    Lens-safe variant of ``pm_knowledge`` exposing only read actions.
    ``pm_knowledge`` の read 部分 (list/get/summary) のみを提供する。
    update mutator は ``pm_knowledge`` 側に分離し、本ツールでは到達不能。

    action: list | get | summary (no update — use pm_knowledge for mutations).
    """
    if action == "update":
        return {
            "status": "error",
            "message": "pm_knowledge_query is read-only. Use pm_knowledge for update.",
        }
    pm_path = _get_pm_path(project_path)
    return _query_knowledge(
        pm_path,
        action,
        record_id=record_id,
        category=category,
        status=status,
        tag=tag,
        task_id=task_id,
        workflow_id=workflow_id,
    )


def _knowledge_summary(r: KnowledgeRecord) -> dict:
    """Concise dict for knowledge record listing."""
    result: dict = {
        "id": r.id,
        "category": r.category.value,
        "title": r.title,
        "status": r.status.value,
        "confidence": r.confidence.value,
        "tags": r.tags,
    }
    if r.task_id:
        result["task_id"] = r.task_id
    if r.workflow_id:
        result["workflow_id"] = r.workflow_id
    return result


def _knowledge_detail(r: KnowledgeRecord) -> dict:
    """Full dict for a single knowledge record."""
    return {
        "id": r.id,
        "category": r.category.value,
        "title": r.title,
        "status": r.status.value,
        "confidence": r.confidence.value,
        "findings": r.findings,
        "conclusion": r.conclusion,
        "sources": r.sources,
        "tags": r.tags,
        "task_id": r.task_id,
        "workflow_id": r.workflow_id,
        "created": r.created.isoformat(),
        "updated": r.updated.isoformat(),
    }


# ─── Workflow ───────────────────────────────────────


@_tool()
def pm_workflow_start(
    feature: str,
    template: str = "development",
    project_path: str | None = None,
) -> dict:
    """Start a new workflow for a feature.

    Creates a workflow instance from a template and activates the first step.
    Returns guidance for what to do in the first step.

    feature: Short description of what you're building (e.g. "add user auth").
    template: Workflow template name. Use pm_workflow_templates to see available ones.
              Default: "development" (ADR → tasks → spec → implement → test → quality).
    """
    pm_path = _get_pm_path(project_path)
    return start_workflow(pm_path, feature, template)


@_tool()
def pm_workflow_status(
    workflow_id: str | None = None,
    project_path: str | None = None,
) -> dict:
    """Get workflow status with step details and guidance.

    Shows progress, current step, completed steps, and what to do next.
    Auto-detects the active workflow if workflow_id is omitted.
    """
    pm_path = _get_pm_path(project_path)
    return workflow_status(pm_path, workflow_id)


@_tool()
def pm_workflow_advance(
    workflow_id: str | None = None,
    proceed: bool = True,
    artifacts: list[str] | None = None,
    notes: str | None = None,
    skip: bool = False,
    project_path: str | None = None,
) -> dict:
    """Advance the current workflow step.

    Marks the current step as done and activates the next step.
    Returns guidance for the next step (tool/skill/agent hints).

    workflow_id: Specific workflow. Auto-detects active workflow if omitted.
    proceed: For loop steps — True exits the loop, False loops back for another iteration.
    artifacts: IDs of artifacts produced (ADR, task, KR IDs). Tracked per step.
    notes: Free-text notes for this step.
    skip: Skip the current step (marks as SKIPPED instead of DONE).
    """
    pm_path = _get_pm_path(project_path)
    return advance_step(pm_path, workflow_id, proceed, artifacts, notes, skip)


@_tool()
def pm_workflow_abandon(
    workflow_id: str | None = None,
    notes: str | None = None,
    project_path: str | None = None,
) -> dict:
    """Abandon a workflow (transition to ABANDONED, preserve history).

    Use when you decide to stop a workflow before completion (research dead-end,
    requirements changed, scope dropped). The workflow's step state, artifacts,
    and progress are preserved; only the container status changes. Subsequent
    pm_workflow_advance calls will return an error.

    workflow_id: Specific workflow. Auto-detects the most recent active workflow
                 if omitted. To abandon a PAUSED workflow, pass its id explicitly.
    notes: Optional reason for abandonment. Appended to the current step's notes
           for retrospective context. Skipped if the workflow has no current step.

    Cannot abandon COMPLETED workflows. Re-abandoning an ABANDONED workflow is
    idempotent (returns status="already_abandoned").
    """
    pm_path = _get_pm_path(project_path)
    return abandon_workflow(pm_path, workflow_id, notes)


@_tool()
def pm_workflow_list(
    status: str | None = None,
    project_path: str | None = None,
) -> dict:
    """List all workflow instances for the project.

    status: Filter by workflow status (active/completed/paused/abandoned).
            Returns all workflows if omitted.
    """
    pm_path = _get_pm_path(project_path)
    workflows = load_workflows(pm_path)

    if status:
        wf_status = WorkflowStatus(status)
        workflows = [w for w in workflows if w.status == wf_status]

    return {
        "count": len(workflows),
        "workflows": [
            {
                "id": w.id,
                "name": w.name,
                "feature": w.feature,
                "template": w.template,
                "status": w.status.value,
                "current_step_index": w.current_step_index,
                "total_steps": len(w.steps),
                "created": w.created.isoformat(),
                "updated": w.updated.isoformat(),
            }
            for w in workflows
        ],
    }


@_tool()
def pm_workflow_templates(project_path: str | None = None) -> dict:
    """List available workflow templates.

    Shows both built-in and custom templates.
    Custom templates in .pm/workflow_templates/ override built-in ones with the same name.

    The response includes a ``warnings`` list. If the built-in templates directory
    has gone stale post-import (PMSERV-068 — typically ``pip install -e .``
    uninstalling the wheel under a running MCP server), a
    ``builtin_templates_dir_missing`` warning is emitted. Callers (Claude) MUST
    surface any non-empty warnings to the user verbatim.
    """
    pm_path = _get_pm_path(project_path)
    templates = list_workflow_templates(pm_path)

    warnings: list[dict] = []
    builtin_status = get_builtin_templates_dir_status()
    if builtin_status["stale"]:
        warnings.append(
            _build_warning(
                level="warn",
                code="builtin_templates_dir_missing",
                message=(
                    f"組み込みテンプレートディレクトリ {builtin_status['path']!r} が "
                    f"起動後に消失したため、組み込みテンプレートが list に含まれていません "
                    f"(2026-05-08 incident と同根: pip install -e . 等で wheel が "
                    f"uninstall された可能性が高い)"
                ),
                remediation=(
                    "この MCP サーバー (該当の Claude Code セッション) を再起動してください。"
                    "再起動後、import 時に新しい __file__ ベースのパスが解決されます。"
                ),
            )
        )

    return {
        "count": len(templates),
        "templates": templates,
        "warnings": warnings,
    }


@_tool()
def pm_list() -> list:
    """List all registered projects with summary info."""
    registry = load_registry()
    projects = []

    for entry in registry.projects:
        pm_path = Path(entry.path) / ".pm"
        info: dict = {
            "path": entry.path,
            "name": entry.name,
            "registered": entry.registered.isoformat(),
        }

        if (pm_path / "project.yaml").exists():
            project = load_project(pm_path)
            tasks = load_tasks(pm_path)
            done = sum(1 for t in tasks if t.status == TaskStatus.DONE)
            info.update(
                {
                    "display_name": project.display_name,
                    "status": project.status.value,
                    "tasks_total": len(tasks),
                    "tasks_done": done,
                    "blockers": sum(1 for t in tasks if t.status == TaskStatus.BLOCKED),
                }
            )
        else:
            info["status"] = "missing_data"

        projects.append(info)

    return projects
