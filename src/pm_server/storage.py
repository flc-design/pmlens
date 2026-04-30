"""YAML file storage for PM Server.

All YAML operations use safe_load / safe_dump only (security).
Output is human-readable with comment headers.
"""

from __future__ import annotations

import datetime as _dt
from pathlib import Path

import yaml

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

PM_DIR = ".pm"
GLOBAL_PM_DIR = Path.home() / ".pm"


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
    path.write_text(_yaml_header(header_name) + body, encoding="utf-8")


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


def save_project(pm_path: Path, project: Project) -> None:
    """Save project to project.yaml."""
    _save_yaml(pm_path / "project.yaml", _model_dump(project), "project.yaml")


# ─── Tasks ───────────────────────────────────────────


def load_tasks(pm_path: Path) -> list[Task]:
    """Load all tasks from tasks.yaml."""
    data = _load_yaml(pm_path / "tasks.yaml")
    if data is None or not isinstance(data, dict) or "tasks" not in data:
        return []
    return [Task(**t) for t in data["tasks"]]


def save_tasks(pm_path: Path, tasks: list[Task]) -> None:
    """Save all tasks to tasks.yaml."""
    _save_yaml(
        pm_path / "tasks.yaml",
        {"tasks": [_model_dump(t) for t in tasks]},
        "tasks.yaml",
    )


def add_task(pm_path: Path, task: Task) -> Task:
    """Append a new task and save."""
    tasks = load_tasks(pm_path)
    tasks.append(task)
    save_tasks(pm_path, tasks)
    return task


def update_task(pm_path: Path, task_id: str, **updates) -> Task:
    """Update fields on an existing task by ID."""
    tasks = load_tasks(pm_path)
    for task in tasks:
        if task.id == task_id:
            for key, value in updates.items():
                if value is not None and hasattr(task, key):
                    setattr(task, key, value)
            task.updated = _dt.date.today()
            save_tasks(pm_path, tasks)
            return task
    raise TaskNotFoundError(f"Task {task_id} not found")


def next_task_number(pm_path: Path) -> int:
    """Return the next available task number."""
    tasks = load_tasks(pm_path)
    if not tasks:
        return 1
    numbers = []
    for t in tasks:
        parts = t.id.rsplit("-", 1)
        if len(parts) == 2 and parts[1].isdigit():
            numbers.append(int(parts[1]))
    return max(numbers, default=0) + 1


# ─── Decisions ───────────────────────────────────────


def load_decisions(pm_path: Path) -> list[Decision]:
    """Load all ADRs from decisions.yaml."""
    data = _load_yaml(pm_path / "decisions.yaml")
    if data is None or not isinstance(data, dict) or "decisions" not in data:
        return []
    return [Decision(**d) for d in data["decisions"]]


def save_decisions(pm_path: Path, decisions: list[Decision]) -> None:
    """Save all decisions to decisions.yaml."""
    _save_yaml(
        pm_path / "decisions.yaml",
        {"decisions": [_model_dump(d) for d in decisions]},
        "decisions.yaml",
    )


def add_decision(pm_path: Path, decision: Decision) -> Decision:
    """Append a new ADR and save."""
    decisions = load_decisions(pm_path)
    decisions.append(decision)
    save_decisions(pm_path, decisions)
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


def save_milestones(pm_path: Path, milestones: list[Milestone]) -> None:
    """Save milestones to milestones.yaml."""
    _save_yaml(
        pm_path / "milestones.yaml",
        {"milestones": [_model_dump(m) for m in milestones]},
        "milestones.yaml",
    )


def add_milestone(pm_path: Path, milestone: Milestone) -> Milestone:
    """Append a new milestone and save."""
    milestones = load_milestones(pm_path)
    milestones.append(milestone)
    save_milestones(pm_path, milestones)
    return milestone


# ─── Risks ───────────────────────────────────────────


def load_risks(pm_path: Path) -> list[Risk]:
    """Load risks from risks.yaml."""
    data = _load_yaml(pm_path / "risks.yaml")
    if data is None or not isinstance(data, dict) or "risks" not in data:
        return []
    return [Risk(**r) for r in data["risks"]]


def save_risks(pm_path: Path, risks: list[Risk]) -> None:
    """Save risks to risks.yaml."""
    _save_yaml(
        pm_path / "risks.yaml",
        {"risks": [_model_dump(r) for r in risks]},
        "risks.yaml",
    )


def add_risk(pm_path: Path, risk: Risk) -> Risk:
    """Append a new risk and save."""
    risks = load_risks(pm_path)
    risks.append(risk)
    save_risks(pm_path, risks)
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


def save_knowledge(pm_path: Path, records: list[KnowledgeRecord]) -> None:
    """Save all knowledge records to knowledge.yaml."""
    _save_yaml(
        pm_path / "knowledge.yaml",
        {"knowledge": [_model_dump(r) for r in records]},
        "knowledge.yaml",
    )


def add_knowledge(pm_path: Path, record: KnowledgeRecord) -> KnowledgeRecord:
    """Append a new knowledge record and save."""
    records = load_knowledge(pm_path)
    records.append(record)
    save_knowledge(pm_path, records)
    return record


def update_knowledge(pm_path: Path, record_id: str, **updates) -> KnowledgeRecord:
    """Update fields on an existing knowledge record by ID."""
    records = load_knowledge(pm_path)
    for rec in records:
        if rec.id == record_id:
            for key, value in updates.items():
                if value is not None and hasattr(rec, key):
                    setattr(rec, key, value)
            rec.updated = _dt.date.today()
            save_knowledge(pm_path, records)
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


def save_registry(registry: Registry, registry_dir: Path | None = None) -> None:
    """Save the global registry."""
    registry_dir = registry_dir or GLOBAL_PM_DIR
    registry_dir.mkdir(parents=True, exist_ok=True)
    _save_yaml(registry_dir / "registry.yaml", _model_dump(registry), "registry.yaml")


def register_project(project_path: Path, name: str, registry_dir: Path | None = None) -> Registry:
    """Register a project in the global registry. Idempotent."""
    registry = load_registry(registry_dir)
    resolved = str(project_path.resolve())
    if any(p.path == resolved for p in registry.projects):
        return registry
    registry.projects.append(RegistryEntry(path=resolved, name=name))
    save_registry(registry, registry_dir)
    return registry


def unregister_project(project_path: Path, registry_dir: Path | None = None) -> Registry:
    """Remove a project from the global registry."""
    registry = load_registry(registry_dir)
    resolved = str(project_path.resolve())
    registry.projects = [p for p in registry.projects if p.path != resolved]
    save_registry(registry, registry_dir)
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


def load_workflows(pm_path: Path) -> list[Workflow]:
    """Load all workflows from workflows.yaml."""
    data = _load_yaml(pm_path / "workflows.yaml")
    if data is None or not isinstance(data, dict) or "workflows" not in data:
        return []
    return [Workflow(**w) for w in data["workflows"]]


def save_workflows(pm_path: Path, workflows: list[Workflow]) -> None:
    """Save all workflows to workflows.yaml."""
    _save_yaml(
        pm_path / "workflows.yaml",
        {"workflows": [_model_dump(w) for w in workflows]},
        "workflows.yaml",
    )


def add_workflow(pm_path: Path, workflow: Workflow) -> Workflow:
    """Append a new workflow and save."""
    workflows = load_workflows(pm_path)
    workflows.append(workflow)
    save_workflows(pm_path, workflows)
    return workflow


def update_workflow(pm_path: Path, workflow_id: str, **updates) -> Workflow:
    """Update fields on an existing workflow by ID."""
    workflows = load_workflows(pm_path)
    for wf in workflows:
        if wf.id == workflow_id:
            for key, value in updates.items():
                if value is not None and hasattr(wf, key):
                    setattr(wf, key, value)
            wf.updated = _dt.date.today()
            save_workflows(pm_path, workflows)
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
