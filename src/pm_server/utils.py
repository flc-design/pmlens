"""Shared utilities for PM Server."""

from __future__ import annotations

import os
import shutil
import tempfile
from datetime import datetime
from pathlib import Path

from .models import Phase, ProjectNotFoundError, Task, TaskStatus


def _is_project_pm_dir(pm_dir: Path) -> bool:
    """Check if a .pm/ directory belongs to an actual project.

    A project .pm/ always contains project.yaml (created by pm_init).
    The global ~/.pm/ only has registry.yaml and memory.db, so it is
    excluded by this check.
    """
    return pm_dir.is_dir() and (pm_dir / "project.yaml").exists()


def resolve_project_path(project_path: str | None = None) -> Path:
    """Resolve the project root directory.

    Priority:
    1. Explicit project_path argument
    2. PM_PROJECT_PATH environment variable
    3. Walk up from cwd looking for .pm/ with project.yaml

    The cwd walk-up skips .pm/ directories without project.yaml
    (e.g. the global ~/.pm/ used for registry).
    """
    if project_path:
        p = Path(project_path).resolve()
        if not (p / ".pm").is_dir():
            raise ProjectNotFoundError(f"No .pm/ directory found at {p}. Run pm_init first.")
        return p

    if env_path := os.environ.get("PM_PROJECT_PATH"):
        p = Path(env_path).resolve()
        if (p / ".pm").is_dir():
            return p

    cwd = Path.cwd()
    for parent in [cwd, *cwd.parents]:
        if _is_project_pm_dir(parent / ".pm"):
            return parent

    raise ProjectNotFoundError(
        "No .pm/ directory found in current directory tree. "
        "Provide project_path or run pm_init first."
    )


def generate_task_id(project_name: str, number: int) -> str:
    """Generate a task ID like PROJ-001 from project name and sequence number."""
    prefix = project_name.upper().replace("-", "").replace("_", "")[:6]
    return f"{prefix}-{number:03d}"


def generate_decision_id(number: int) -> str:
    """Generate an ADR ID like ADR-001."""
    return f"ADR-{number:03d}"


def generate_risk_id(number: int) -> str:
    """Generate a risk ID like RISK-001."""
    return f"RISK-{number:03d}"


def aggregate_task_status(tasks: list[Task]) -> dict[str, int]:
    """Count tasks by status. Returns {status_value: count}."""
    counts = {s.value: 0 for s in TaskStatus}
    for t in tasks:
        counts[t.status.value] += 1
    return counts


def calculate_phase_progress(tasks: list[Task], phase: Phase) -> dict:
    """Calculate progress for a single phase."""
    phase_tasks = [t for t in tasks if t.phase == phase.id]
    done = sum(1 for t in phase_tasks if t.status == TaskStatus.DONE)
    total = len(phase_tasks)
    return {
        "id": phase.id,
        "name": phase.name,
        "status": phase.status.value,
        "done": done,
        "total": total,
        "pct": round(done / total * 100) if total > 0 else 0,
        "target_date": phase.target_date.isoformat() if phase.target_date else None,
    }


# --- MCP host targets (shared by installer.py and rules.py) ---------------

# Tuple order is significant: orchestrators dispatch in this order and the
# CLI prints results in the same sequence. Keep "claude-code" first.
_KNOWN_HOSTS: tuple[str, ...] = ("claude-code", "codex")
TARGET_CHOICES: tuple[str, ...] = ("auto", "all", *_KNOWN_HOSTS)


def _resolve_targets(target: str) -> list[str]:
    """Expand a target spec into a concrete list of host identifiers.

    ``"auto"`` and ``"all"`` are synonyms here (ADR-007 #1: target
    dispatch). The detection-aware ``"auto"`` semantics for rule-file
    injection are layered on top in ``rules.detect_hosts``.
    """
    if target in ("auto", "all"):
        return list(_KNOWN_HOSTS)
    if target in _KNOWN_HOSTS:
        return [target]
    raise ValueError(f"unknown target: {target!r}. Expected one of {TARGET_CHOICES}.")


def _codex_config_path() -> Path:
    """Return the Codex CLI config path (lazy; honors monkeypatched HOME)."""
    return Path.home() / ".codex" / "config.toml"


# --- File mutation helpers (shared by installer.py and rules.py) ----------


def _timestamped_backup(path: Path) -> Path:
    """Create a timestamped ``.bak.<ts>`` copy next to ``path``.

    Uses ``shutil.copy2`` to preserve mtime and permissions. Caller is
    responsible for ensuring ``path`` exists.
    """
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_path = path.with_name(f"{path.name}.bak.{timestamp}")
    shutil.copy2(path, backup_path)
    return backup_path


def _atomic_write_text(path: Path, content: str, *, encoding: str = "utf-8") -> None:
    """Atomically replace ``path`` with ``content`` using mkstemp + rename.

    ``tempfile.mkstemp`` produces a randomised filename so concurrent
    writers cannot collide on a fixed ``.tmp`` suffix. The temp file is
    created in the same directory as ``path`` to keep ``os.replace`` on
    a single filesystem. On exception the temp file is removed.

    File-permission normalisation: ``mkstemp`` opens with mode 0600
    (security default for secrets), but rule files like ``CLAUDE.md`` /
    ``AGENTS.md`` are user content that should follow the conventional
    ``open(...,"w")`` permissions (``0o666 & ~umask``). When the target
    already exists we inherit its mode via ``shutil.copymode`` so users
    who locked down a file keep that lock.
    """
    tmp_fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(tmp_fd, "w", encoding=encoding) as f:
            f.write(content)
        if path.exists():
            shutil.copymode(path, tmp_name)
        else:
            current_umask = os.umask(0)
            os.umask(current_umask)
            os.chmod(tmp_name, 0o666 & ~current_umask)
        os.replace(tmp_name, path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise
