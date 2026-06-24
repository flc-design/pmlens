"""F1 Lens invariant: main .pm/memory.db stays untouched under Phase 2.

ADR-019 / WF-028 amendment F1. Even with PM_LENS=1 + PM_DESKTOP_WRITE=1
active and Desktop outbox writes happening, the main per-project
``.pm/memory.db`` (and the global ``~/.pm/memory.db`` cross-project index)
must NOT be mutated. This is the structural compliment to the test_outbox
PM_LENS gating cases: those verify *visibility* of the writers, this one
verifies their *write target*.

Implemented via subprocess so the pm-server module is loaded with PM_LENS=1
+ PM_DESKTOP_WRITE=1 set at import time (the env vars are evaluated at
module load and never re-checked). A regression that accidentally routed
outbox writes through MemoryStore would show up as a mtime change on a DB
that PM_LENS=1 is supposed to keep read-only.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import textwrap
from pathlib import Path


def _seed_pm_server_memories_db(db_path: Path) -> None:
    """Create a minimal pm-server-compatible memories DB on disk.

    Mirrors the schema-guard semantics (PMSERV-093): the file must contain
    a ``memories`` table so PM_LENS=1 will open it read-only (immutable=1)
    rather than fall back to an in-memory empty store.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(
            """
            CREATE TABLE memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                type TEXT NOT NULL,
                content TEXT NOT NULL,
                task_id TEXT,
                decision_id TEXT,
                tags TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                project TEXT NOT NULL
            );
            INSERT INTO memories (session_id, type, content, project)
            VALUES ('sess-seed', 'observation', 'seed', 'seed-proj');
            """
        )
        conn.commit()
    finally:
        conn.close()


def test_outbox_remember_does_not_mutate_main_memory_dbs(tmp_path: Path) -> None:
    """F1 invariant: pm_outbox_remember under PM_LENS=1 + PM_DESKTOP_WRITE=1
    must not touch the project's .pm/memory.db nor the global ~/.pm/memory.db.

    Catches regressions where Phase 2 outbox writes accidentally get routed
    through MemoryStore.save instead of DesktopOutboxStore.append.
    """
    # 1. Fake HOME so ~/.pm resolves under tmp_path (storage.GLOBAL_PM_DIR is
    #    Path.home() / ".pm" — Python evaluates that at module import time,
    #    so the subprocess will pick up the redirected HOME on its own import).
    fake_home = tmp_path / "fake_home"
    fake_home.mkdir()
    global_db = fake_home / ".pm" / "memory.db"
    _seed_pm_server_memories_db(global_db)

    # 2. Fake project with a seeded per-project memory.db.
    project_root = tmp_path / "myproject"
    project_db = project_root / ".pm" / "memory.db"
    _seed_pm_server_memories_db(project_db)
    # Minimal project.yaml so resolve_project_path / load_project succeed if
    # the outbox path ever depends on them (defensive — current impl does
    # not, but a future refactor should not silently break this test).
    (project_root / ".pm" / "project.yaml").write_text(
        "name: myproject\n"
        "display_name: MyProject\n"
        "version: 0.0.1\n"
        "status: development\n"
        "started: 2026-01-01\n"
        "description: lens invariant fixture\n"
        "phases: []\n",
        encoding="utf-8",
    )

    project_mtime_before = project_db.stat().st_mtime_ns
    global_mtime_before = global_db.stat().st_mtime_ns

    # 3. Subprocess: import pmlens.server under PM_LENS=1 + PM_DESKTOP_WRITE=1
    #    and call pm_outbox_remember. The decorator gating reads env at import.
    script = textwrap.dedent("""
        import json
        import sys

        import pmlens.server as srv

        # Sanity: both flags engaged in the child.
        assert srv.PM_LENS_ENABLED is True, "PM_LENS not picked up in subprocess"
        assert srv.PM_DESKTOP_WRITE_ENABLED is True, (
            "PM_DESKTOP_WRITE not picked up in subprocess"
        )

        result = srv.pm_outbox_remember(
            content="lens invariant write",
            type="memory",
            source_project=sys.argv[1],
        )
        print(json.dumps(result))
    """)

    env = {
        **os.environ,
        "HOME": str(fake_home),
        "PM_LENS": "1",
        "PM_DESKTOP_WRITE": "1",
    }
    # Avoid pyenv shim / user-site interference when subprocessing.
    env.pop("VIRTUAL_ENV", None)

    proc = subprocess.run(
        [sys.executable, "-c", script, str(project_root)],
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
        cwd=str(tmp_path),
    )
    assert proc.returncode == 0, (
        f"subprocess failed: stderr={proc.stderr!r}, stdout={proc.stdout!r}"
    )

    response = json.loads(proc.stdout.strip().splitlines()[-1])
    assert response["status"] == "saved", response
    assert "outbox_id" in response and response["outbox_id"] >= 1

    # 4. F1 invariant assertions: neither main DB was touched.
    project_mtime_after = project_db.stat().st_mtime_ns
    global_mtime_after = global_db.stat().st_mtime_ns
    assert project_mtime_after == project_mtime_before, (
        "project .pm/memory.db mutated under PM_LENS=1 + PM_DESKTOP_WRITE=1; "
        "Phase 2 outbox writes leaked into the main store"
    )
    assert global_mtime_after == global_mtime_before, (
        "global ~/.pm/memory.db mutated under PM_LENS=1 + PM_DESKTOP_WRITE=1; "
        "Phase 2 outbox writes leaked into the cross-project index"
    )

    # 5. Sanity: the outbox DB was actually created and populated (otherwise
    #    the test would silently pass even if pm_outbox_remember did nothing).
    outbox_db = fake_home / ".pm" / "desktop" / "desktop.db"
    assert outbox_db.exists(), "desktop.db not created — outbox write did not run"
    conn = sqlite3.connect(str(outbox_db))
    try:
        count = conn.execute("SELECT COUNT(*) FROM desktop_outbox").fetchone()[0]
    finally:
        conn.close()
    assert count == 1, f"expected 1 outbox row, got {count}"


def test_outbox_log_does_not_mutate_main_memory_dbs(tmp_path: Path) -> None:
    """F1 invariant sibling: pm_outbox_log under the same Phase 2 env must
    not mutate any project's main memory.db either. The append-only outbox
    is the only writable surface."""
    fake_home = tmp_path / "fake_home"
    fake_home.mkdir()
    global_db = fake_home / ".pm" / "memory.db"
    _seed_pm_server_memories_db(global_db)

    project_root = tmp_path / "logproject"
    project_db = project_root / ".pm" / "memory.db"
    _seed_pm_server_memories_db(project_db)
    (project_root / ".pm" / "daily").mkdir(parents=True, exist_ok=True)

    project_mtime_before = project_db.stat().st_mtime_ns
    global_mtime_before = global_db.stat().st_mtime_ns

    script = textwrap.dedent("""
        import json
        import sys

        import pmlens.server as srv

        result = srv.pm_outbox_log(
            entry="lens invariant log",
            category="note",
            source_project=sys.argv[1],
        )
        print(json.dumps(result))
    """)

    env = {
        **os.environ,
        "HOME": str(fake_home),
        "PM_LENS": "1",
        "PM_DESKTOP_WRITE": "1",
    }
    env.pop("VIRTUAL_ENV", None)

    proc = subprocess.run(
        [sys.executable, "-c", script, str(project_root)],
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
        cwd=str(tmp_path),
    )
    assert proc.returncode == 0, proc.stderr

    assert project_db.stat().st_mtime_ns == project_mtime_before
    assert global_db.stat().st_mtime_ns == global_mtime_before

    # Also: no daily/*.yaml was created in the project dir — outbox stores
    # the log entry instead, and only pm_outbox_merge from Claude Code may
    # later append it to daily/.
    daily_files = list((project_root / ".pm" / "daily").glob("*.yaml"))
    assert daily_files == [], (
        f"daily YAML created under Lens — outbox should defer to merge: {daily_files}"
    )


# ─── X content pipeline Lens gating (PMSERV-113 / PMSERV-116) ──────────────
# The x_drafts tools are Claude-Code-only: NOT in RO_ALLOWLIST and NOT in
# OUTBOX_WRITE_ALLOWLIST, so PM_LENS=1 hides them entirely (mirroring the
# pm_outbox_* review tools). This is how must-fix #3 is satisfied — no
# x_drafts mutator is even reachable on a Lens host, so x_drafts.db can never
# be created/written there.

_X_DRAFT_TOOLS = ("pm_draft_x", "pm_redact_draft", "pm_reject_draft", "pm_x_drafts_pending")


def test_x_draft_tools_registered_in_claude_code_mode() -> None:
    """Sanity (PM_LENS=0, the normal test process): all four x_drafts tools
    ARE registered, so the Lens=1 hidden assertion below is meaningful."""
    import pmlens.server as srv

    assert srv.PM_LENS_ENABLED is False
    for name in _X_DRAFT_TOOLS:
        assert name in srv.REGISTERED_TOOLS, f"{name} should be registered in Claude Code mode"


def test_x_draft_tools_hidden_under_lens(tmp_path: Path) -> None:
    """Under PM_LENS=1 none of the x_drafts tools may register with MCP — they
    are mutators not in any allowlist, so the @_tool() gate returns the bare
    function and never adds them to REGISTERED_TOOLS."""
    script = textwrap.dedent("""
        import pmlens.server as srv

        assert srv.PM_LENS_ENABLED is True, "PM_LENS not picked up in subprocess"
        hidden = [t for t in (
            "pm_draft_x", "pm_redact_draft", "pm_reject_draft", "pm_x_drafts_pending",
        ) if t in srv.REGISTERED_TOOLS]
        assert hidden == [], f"x_drafts tools leaked into Lens registration: {hidden}"
        # The bare functions still exist as module attributes (just not MCP-registered).
        assert callable(srv.pm_draft_x)
        print("ok")
    """)

    env = {**os.environ, "HOME": str(tmp_path / "fake_home"), "PM_LENS": "1"}
    env.pop("VIRTUAL_ENV", None)
    env.pop("PM_DESKTOP_WRITE", None)
    (tmp_path / "fake_home").mkdir()

    proc = subprocess.run(
        [sys.executable, "-c", script],
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
        cwd=str(tmp_path),
    )
    assert proc.returncode == 0, f"stderr={proc.stderr!r}, stdout={proc.stdout!r}"
    assert proc.stdout.strip().splitlines()[-1] == "ok"
