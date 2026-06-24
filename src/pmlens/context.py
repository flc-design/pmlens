"""Session context injection via CLI.

Prints a context block to stdout for Claude Code injection.
Designed for future SessionStart hook integration.
Currently used via `pm-server context-inject` CLI command.
"""

from __future__ import annotations

import sys
from pathlib import Path

from .memory import MemoryStore
from .recall import ContextBuilder
from .utils import resolve_project_path


def inject_context(project_path: Path | None = None) -> None:
    """Print context block to stdout for Claude Code injection.

    Resolves the project, builds a context block from memories,
    and prints it to stdout. If no memories exist, prints nothing.
    """
    try:
        root = resolve_project_path(str(project_path) if project_path else None)
    except Exception:
        return

    pm_path = root / ".pm"
    db_path = pm_path / "memory.db"
    if not db_path.exists():
        return

    store = MemoryStore(db_path, global_db_path=None)
    try:
        builder = ContextBuilder(store, pm_path)
        context = builder.build_session_context()
        if context:
            sys.stdout.write(context + "\n")
    finally:
        store.close()
