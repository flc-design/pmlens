"""YAML file storage for PM Server.

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
"""

from __future__ import annotations

import datetime as _dt
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import yaml
from filelock import FileLock
from filelock import Timeout as FileLockTimeout

from .models import (
    DailyLog,
    DailyLogEntry,
    Decision,
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
_LOCKS_DIR = ".locks"
_LOCKS_GITIGNORE = "*\n!.gitignore\n"


# ─── Internal helpers ────────────────────────────────


def _yaml_header(filename: str) -> str:
    return f"# PM Server - {filename}\n"


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


@contextmanager
def _yaml_transaction(
    base_dir: Path,
    filename: str,
    *,
    timeout: float = DEFAULT_LOCK_TIMEOUT_S,
) -> Iterator[None]:
    """Acquire an exclusive lock for a yaml file's read-modify-write cycle.

    The lock file lives at ``base_dir/.locks/{stem}.lock`` where ``stem`` is
    ``filename`` with any ``.yaml`` suffix stripped (so callers can pass either
    ``"tasks.yaml"`` or a plain label like ``"registry"``).

    Raises ``PmServerError`` if the lock cannot be acquired within ``timeout``
    seconds.

    Implementation note: ``filelock.FileLock`` is reentrant within the same
    instance but two distinct ``FileLock(same_path)`` calls in the same process
    deadlock. The convention for callers: do not nest mutators — i.e. inside
    a ``with _yaml_transaction(...):`` block, do not call another public
    mutator that would acquire the same lock.
    """
    lock_dir = _ensure_locks_dir(base_dir)
    stem = filename.removesuffix(".yaml")
    lock_path = lock_dir / f"{stem}.lock"
    lock = FileLock(str(lock_path), timeout=timeout)
    try:
        with lock:
            yield
    except FileLockTimeout as e:
        raise PmServerError(
            f"Failed to acquire lock on {filename}: timeout after {timeout}s"
        ) from e


def _model_dump(model) -> dict:
    """Dump a Pydantic model to a JSON-serializable dict."""
    return model.model_dump(mode="json")


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
    if not tasks:
        return 1
    numbers = []
    for t in tasks:
        parts = t.id.rsplit("-", 1)
        if len(parts) == 2 and parts[1].isdigit():
            numbers.append(int(parts[1]))
    return max(numbers, default=0) + 1


def next_task_number(pm_path: Path) -> int:
    """Return the next available task number."""
    return _next_task_number_from_list(load_tasks(pm_path))


# ─── Decisions ───────────────────────────────────────


def load_decisions(pm_path: Path) -> list[Decision]:
    """Load all ADRs from decisions.yaml."""
    data = _load_yaml(pm_path / "decisions.yaml")
    if data is None or not isinstance(data, dict) or "decisions" not in data:
        return []
    return [Decision(**d) for d in data["decisions"]]


def _save_decisions(pm_path: Path, decisions: list[Decision]) -> None:
    """Save all decisions to decisions.yaml."""
    _save_yaml(
        pm_path / "decisions.yaml",
        {"decisions": [_model_dump(d) for d in decisions]},
        "decisions.yaml",
    )


def add_decision(pm_path: Path, decision: Decision) -> Decision:
    """Append a new ADR and save."""
    with _yaml_transaction(pm_path, "decisions.yaml"):
        decisions = load_decisions(pm_path)
        decisions.append(decision)
        _save_decisions(pm_path, decisions)
    return decision


def next_decision_number(pm_path: Path) -> int:
    """Return the next available ADR number."""
    decisions = load_decisions(pm_path)
    if not decisions:
        return 1
    numbers = []
    for d in decisions:
        parts = d.id.rsplit("-", 1)
        if len(parts) == 2 and parts[1].isdigit():
            numbers.append(int(parts[1]))
    return max(numbers, default=0) + 1


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
    """Save all knowledge records to knowledge.yaml."""
    _save_yaml(
        pm_path / "knowledge.yaml",
        {"knowledge": [_model_dump(r) for r in records]},
        "knowledge.yaml",
    )


def add_knowledge(pm_path: Path, record: KnowledgeRecord) -> KnowledgeRecord:
    """Append a new knowledge record and save."""
    with _yaml_transaction(pm_path, "knowledge.yaml"):
        records = load_knowledge(pm_path)
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
    """Return the next available knowledge record number."""
    records = load_knowledge(pm_path)
    if not records:
        return 1
    numbers = []
    for r in records:
        parts = r.id.rsplit("-", 1)
        if len(parts) == 2 and parts[1].isdigit():
            numbers.append(int(parts[1]))
    return max(numbers, default=0) + 1


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
    """Return the next available workflow number."""
    workflows = load_workflows(pm_path)
    if not workflows:
        return 1
    numbers = []
    for w in workflows:
        parts = w.id.rsplit("-", 1)
        if len(parts) == 2 and parts[1].isdigit():
            numbers.append(int(parts[1]))
    return max(numbers, default=0) + 1


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
