"""Velocity calculation and trend analysis."""

from __future__ import annotations

import datetime as _dt
from pathlib import Path

from .models import PhaseStatus, TaskStatus
from .storage import load_project, load_tasks

# ─── Thresholds ─────────────────────────────────────

TREND_IMPROVING_THRESHOLD = 1.2
TREND_DECLINING_THRESHOLD = 0.8
STALE_DAYS_THRESHOLD = 7
ESTIMATE_OVERRUN_FACTOR = 1.5


def calculate_velocity(pm_path: Path, weeks: int = 4) -> dict:
    """Calculate weekly velocity from task completion data.

    Returns weekly breakdown, average, and trend indicator.
    """
    today = _dt.date.today()
    tasks = load_tasks(pm_path)

    # Collect done tasks with their updated dates
    done_tasks = [t for t in tasks if t.status == TaskStatus.DONE]

    weekly_counts: list[dict] = []
    for w in range(weeks):
        week_end = today - _dt.timedelta(weeks=w)
        week_start = week_end - _dt.timedelta(days=6)
        count = sum(1 for t in done_tasks if week_start <= t.updated <= week_end)
        weekly_counts.append(
            {
                "week_start": week_start.isoformat(),
                "week_end": week_end.isoformat(),
                "completed": count,
            }
        )

    weekly_counts.reverse()  # chronological order

    completed_values = [w["completed"] for w in weekly_counts]
    avg = sum(completed_values) / len(completed_values) if completed_values else 0

    # Trend: compare last half to first half
    trend = "stable"
    if len(completed_values) >= 2:
        mid = len(completed_values) // 2
        first_half = sum(completed_values[:mid]) / max(mid, 1)
        second_half = sum(completed_values[mid:]) / max(len(completed_values) - mid, 1)
        if second_half > first_half * TREND_IMPROVING_THRESHOLD:
            trend = "improving"
        elif second_half < first_half * TREND_DECLINING_THRESHOLD:
            trend = "declining"

    return {
        "weeks": weekly_counts,
        "average": round(avg, 1),
        "trend": trend,
        "total_done": len(done_tasks),
    }


def detect_risks(pm_path: Path) -> list[dict]:
    """Auto-detect risks from project state.

    Checks for: overdue phases, long-blocked tasks, stale in-progress tasks.
    """
    tasks = load_tasks(pm_path)
    today = _dt.date.today()
    risks: list[dict] = []

    # Blocked tasks
    blocked = [t for t in tasks if t.status == TaskStatus.BLOCKED]
    for t in blocked:
        days_blocked = (today - t.updated).days
        severity = "high" if days_blocked > STALE_DAYS_THRESHOLD else "medium"
        risks.append(
            {
                "type": "blocked_task",
                "task_id": t.id,
                "title": f"Task {t.id} blocked for {days_blocked} days",
                "severity": severity,
                "details": f"'{t.title}' blocked by: {', '.join(t.blocked_by) or 'unknown'}",
            }
        )

    # Stale in-progress tasks (no update in 7+ days)
    in_progress = [t for t in tasks if t.status == TaskStatus.IN_PROGRESS]
    for t in in_progress:
        days_stale = (today - t.updated).days
        if days_stale >= STALE_DAYS_THRESHOLD:
            risks.append(
                {
                    "type": "stale_task",
                    "task_id": t.id,
                    "title": f"Task {t.id} in progress for {days_stale} days without update",
                    "severity": "medium",
                    "details": f"'{t.title}' last updated {t.updated.isoformat()}",
                }
            )

    # Overdue estimates (actual_hours > estimate_hours * factor)
    for t in tasks:
        if (
            t.estimate_hours
            and t.actual_hours
            and t.actual_hours > t.estimate_hours * ESTIMATE_OVERRUN_FACTOR
            and t.status != TaskStatus.DONE
        ):
            risks.append(
                {
                    "type": "overrun",
                    "task_id": t.id,
                    "title": (
                        f"Task {t.id} exceeding estimate ({t.actual_hours}h / {t.estimate_hours}h)"
                    ),
                    "severity": "medium",
                    "details": (
                        f"'{t.title}' is {t.actual_hours / t.estimate_hours:.0%} of estimate"
                    ),
                }
            )

    # Phase overdue detection
    project = load_project(pm_path)
    for phase in project.phases:
        if (
            phase.status != PhaseStatus.COMPLETED
            and phase.target_date
            and phase.target_date < today
        ):
            days_overdue = (today - phase.target_date).days
            risks.append(
                {
                    "type": "phase_overdue",
                    "phase_id": phase.id,
                    "title": f"Phase '{phase.name}' is {days_overdue} days past target date",
                    "severity": "high" if days_overdue > 14 else "medium",
                    "details": (
                        f"Target: {phase.target_date.isoformat()}, Status: {phase.status.value}"
                    ),
                }
            )

    return risks
