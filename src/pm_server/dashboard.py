"""Dashboard generation — HTML (Jinja2 + Chart.js) and text fallback for PM Server."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from .models import TaskStatus, WorkflowStatus, WorkflowStepStatus
from .storage import (
    load_decisions,
    load_knowledge,
    load_project,
    load_registry,
    load_risks,
    load_tasks,
    load_workflows,
)
from .utils import aggregate_task_status, calculate_phase_progress
from .velocity import calculate_velocity, detect_risks

TEMPLATES_DIR = Path(__file__).parent / "templates"


def _get_jinja_env() -> Environment:
    return Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=select_autoescape(["html"]),
    )


# ─── Single Project Dashboard ────────────────────────


def render_project_dashboard(pm_path: Path, format: str = "html") -> str:
    """Render a single project dashboard."""
    project = load_project(pm_path)
    tasks = load_tasks(pm_path)
    decisions = load_decisions(pm_path)
    risks_manual = load_risks(pm_path)
    velocity = calculate_velocity(pm_path)
    auto_risks = detect_risks(pm_path)

    status_counts = aggregate_task_status(tasks)

    # Phase progress
    phases = []
    for phase in project.phases:
        p = calculate_phase_progress(tasks, phase)
        p["target_date"] = p["target_date"] or "—"
        phases.append(p)

    # Blocked tasks
    blocked = [t for t in tasks if t.status == TaskStatus.BLOCKED]

    # Workflows
    workflows = load_workflows(pm_path)
    active_workflows = [w for w in workflows if w.status == WorkflowStatus.ACTIVE]
    workflow_data = []
    for wf in workflows:
        done_steps = sum(
            1 for s in wf.steps if s.status in (WorkflowStepStatus.DONE, WorkflowStepStatus.SKIPPED)
        )
        total_steps = len(wf.steps)
        pct = round(done_steps / total_steps * 100) if total_steps > 0 else 0
        current_step_name = ""
        if wf.status == WorkflowStatus.ACTIVE and wf.current_step_index < total_steps:
            current_step_name = wf.steps[wf.current_step_index].name
        workflow_data.append(
            {
                "id": wf.id,
                "name": wf.name,
                "feature": wf.feature,
                "status": wf.status.value,
                "done_steps": done_steps,
                "total_steps": total_steps,
                "pct": pct,
                "current_step": current_step_name,
                "steps": [
                    {
                        "name": s.name,
                        "status": s.status.value,
                    }
                    for s in wf.steps
                ],
            }
        )

    # Knowledge records
    knowledge = load_knowledge(pm_path)
    knowledge_by_category: dict[str, int] = {}
    knowledge_by_status: dict[str, int] = {}
    for kr in knowledge:
        knowledge_by_category[kr.category.value] = (
            knowledge_by_category.get(kr.category.value, 0) + 1
        )
        knowledge_by_status[kr.status.value] = knowledge_by_status.get(kr.status.value, 0) + 1

    context = {
        "project": project,
        "tasks": tasks,
        "status_counts": status_counts,
        "phases": phases,
        "blocked": blocked,
        "decisions": decisions,
        "velocity": velocity,
        "risks": auto_risks
        + [
            {"type": "manual", "title": r.title, "severity": r.severity.value} for r in risks_manual
        ],
        "workflows": workflow_data,
        "active_workflows": len(active_workflows),
        "knowledge_total": len(knowledge),
        "knowledge_by_category": knowledge_by_category,
        "knowledge_by_status": knowledge_by_status,
        "today": date.today().isoformat(),
    }

    if format == "text":
        return _render_project_text(context)

    env = _get_jinja_env()
    template = env.get_template("dashboard_single.html")
    return template.render(**context)


def _render_project_text(ctx: dict) -> str:
    """Plain-text dashboard for a single project."""
    project = ctx["project"]
    sc = ctx["status_counts"]
    lines = [
        f"{'=' * 50}",
        f"  {project.display_name or project.name}",
        f"  Status: {project.status.value} | v{project.version}",
        f"{'=' * 50}",
        "",
        "Tasks:",
        f"  TODO: {sc.get('todo', 0)}  |  In Progress: {sc.get('in_progress', 0)}  |  "
        f"Review: {sc.get('review', 0)}  |  Done: {sc.get('done', 0)}  |  "
        f"Blocked: {sc.get('blocked', 0)}",
        "",
    ]

    if ctx["phases"]:
        lines.append("Phases:")
        for p in ctx["phases"]:
            bar_len = 20
            filled = round(p["pct"] / 100 * bar_len)
            bar = "█" * filled + "░" * (bar_len - filled)
            lines.append(f"  [{bar}] {p['pct']:3d}% {p['name']} ({p['done']}/{p['total']})")
        lines.append("")

    if ctx["blocked"]:
        lines.append("Blockers:")
        for t in ctx["blocked"]:
            lines.append(f"  ! {t.id}: {t.title} (blocked by: {', '.join(t.blocked_by) or '?'})")
        lines.append("")

    vel = ctx["velocity"]
    lines.append(f"Velocity: {vel['average']} tasks/week ({vel['trend']})")

    if ctx.get("workflows"):
        lines.append("")
        lines.append("Workflows:")
        for wf in ctx["workflows"]:
            status_icon = "*" if wf["status"] == "active" else " "
            lines.append(
                f"  {status_icon} {wf['id']}: {wf['feature']} "
                f"({wf['done_steps']}/{wf['total_steps']}) [{wf['status']}]"
            )
            if wf["current_step"]:
                lines.append(f"    Current: {wf['current_step']}")

    if ctx.get("knowledge_total", 0) > 0:
        lines.append("")
        lines.append(f"Knowledge Records: {ctx['knowledge_total']}")
        for cat, count in sorted(ctx.get("knowledge_by_category", {}).items()):
            lines.append(f"  {cat}: {count}")

    if ctx["risks"]:
        lines.append("")
        lines.append("Risks:")
        for r in ctx["risks"]:
            lines.append(f"  [{r.get('severity', 'medium')}] {r['title']}")

    return "\n".join(lines)


# ─── Portfolio Dashboard ─────────────────────────────


def render_portfolio_dashboard(format: str = "html") -> str:
    """Render a portfolio dashboard across all registered projects."""
    registry = load_registry()
    projects_data = []

    for entry in registry.projects:
        pm_path = Path(entry.path).resolve() / ".pm"
        if not (pm_path / "project.yaml").exists():
            continue

        project = load_project(pm_path)
        tasks = load_tasks(pm_path)
        done = sum(1 for t in tasks if t.status == TaskStatus.DONE)
        blocked = sum(1 for t in tasks if t.status == TaskStatus.BLOCKED)
        total = len(tasks)

        workflows = load_workflows(pm_path)
        active_wf = sum(1 for w in workflows if w.status == WorkflowStatus.ACTIVE)

        knowledge = load_knowledge(pm_path)

        projects_data.append(
            {
                "name": project.name,
                "display_name": project.display_name or project.name,
                "status": project.status.value,
                "path": entry.path,
                "tasks_total": total,
                "tasks_done": done,
                "tasks_blocked": blocked,
                "progress_pct": round(done / total * 100) if total > 0 else 0,
                "active_workflows": active_wf,
                "knowledge_count": len(knowledge),
            }
        )

    context = {
        "projects": projects_data,
        "total_projects": len(projects_data),
        "today": date.today().isoformat(),
    }

    if format == "text":
        return _render_portfolio_text(context)

    env = _get_jinja_env()
    template = env.get_template("dashboard_portfolio.html")
    return template.render(**context)


def _render_portfolio_text(ctx: dict) -> str:
    """Plain-text portfolio dashboard."""
    lines = [
        f"{'=' * 60}",
        f"  PM Server — Portfolio Dashboard ({ctx['today']})",
        f"  {ctx['total_projects']} projects registered",
        f"{'=' * 60}",
        "",
    ]

    if not ctx["projects"]:
        lines.append("  No projects registered. Run pm_init to get started.")
        return "\n".join(lines)

    # Header
    hdr = f"  {'Project':<25} {'Status':<14} {'Progress':<12} {'Blocked':>7} {'WF':>3} {'KR':>3}"
    sep = f"  {'─' * 25} {'─' * 14} {'─' * 12} {'─' * 7} {'─' * 3} {'─' * 3}"
    lines.append(hdr)
    lines.append(sep)

    for p in ctx["projects"]:
        name = (p["display_name"] or p["name"])[:24]
        prog = f"{p['tasks_done']}/{p['tasks_total']}"
        wf = p.get("active_workflows", 0)
        kr = p.get("knowledge_count", 0)
        lines.append(
            f"  {name:<25} {p['status']:<14} {prog:<12} {p['tasks_blocked']:>7} {wf:>3} {kr:>3}"
        )

    return "\n".join(lines)
