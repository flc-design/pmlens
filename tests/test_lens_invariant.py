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


# ─── T6: full RO+outbox-read surface produces zero FS writes (ADR-039 T6) ──
# NFR-1 / C18: the whole point of PM_LENS gating (RO_ALLOWLIST /
# OUTBOX_READ_ALLOWLIST / OUTBOX_WRITE_ALLOWLIST — see server._tool()) is a
# structural guarantee that a Lens host can only ever write through the
# Desktop outbox, never through ~/.pm or a project's .pm. The per-tool tests
# above spot-check individual writers; this sweep instead enumerates
# srv.REGISTERED_TOOLS itself (whatever it ends up containing under a given
# env) and calls every one of them with default arguments, then asserts a
# full filesystem snapshot of both .pm trees is byte-for-byte unchanged.
# Errors from tools missing required args are expected and ignored — only
# filesystem side effects are being probed here.


def _fs_snapshot(root: Path) -> list[tuple[str, int, int]]:
    """Snapshot every entry under ``root`` as ``(relative_path, size, mtime_ns)``.

    Walks both files and directories so a newly created (even empty) file or
    directory shows up as an added tuple, not just a silent mtime bump on an
    existing entry. Returns ``[]`` if ``root`` itself does not exist yet.
    """
    if not root.exists():
        return []
    entries: list[tuple[str, int, int]] = []
    for p in sorted(root.rglob("*")):
        try:
            st = p.stat()
        except OSError:
            continue
        entries.append((str(p.relative_to(root)), st.st_size, st.st_mtime_ns))
    return entries


def _seed_lens_invariant_fixture(tmp_path: Path) -> tuple[Path, Path]:
    """Build a fake HOME plus a minimal registered project for the T6 sweep.

    Returns ``(fake_home, project_root)``. Mirrors the fixtures used by the
    F1 tests above (schema-valid memory.db so PM_LENS=1 opens it read-only
    instead of falling back to an in-memory empty store; a minimal
    project.yaml so project-scoped RO tools can resolve via cwd walk-up).
    """
    fake_home = tmp_path / "fake_home"
    fake_home.mkdir()
    global_db = fake_home / ".pm" / "memory.db"
    _seed_pm_server_memories_db(global_db)

    project_root = tmp_path / "t6project"
    project_db = project_root / ".pm" / "memory.db"
    _seed_pm_server_memories_db(project_db)
    (project_root / ".pm" / "project.yaml").write_text(
        "name: t6project\n"
        "display_name: T6Project\n"
        "version: 0.0.1\n"
        "status: development\n"
        "started: 2026-01-01\n"
        "description: lens invariant T6 fixture\n"
        "phases: []\n",
        encoding="utf-8",
    )
    return fake_home, project_root


# Runs inside the subprocess: import pmlens.server fresh (so PM_LENS /
# PM_DESKTOP_WRITE gating in server._tool() is evaluated under this env),
# then call every registered tool with zero args, tolerating any exception.
_T6_SWEEP_SCRIPT = textwrap.dedent("""
    import json
    import sys

    import pmlens.server as srv

    assert srv.PM_LENS_ENABLED is True, "PM_LENS not picked up in subprocess"
    expect_desktop_write = sys.argv[1] == "1"
    assert srv.PM_DESKTOP_WRITE_ENABLED is expect_desktop_write, (
        f"PM_DESKTOP_WRITE_ENABLED={srv.PM_DESKTOP_WRITE_ENABLED!r} does not "
        f"match expected {expect_desktop_write!r}"
    )

    tool_names = sorted(srv.REGISTERED_TOOLS)
    errors = {}
    for name in tool_names:
        fn = getattr(srv, name)
        try:
            fn()
        except Exception as e:  # noqa: BLE001 - deliberately broad: a tool
            # erroring on missing required args is expected and fine; only
            # filesystem side effects are under test here, never the
            # per-tool return value or error type.
            errors[name] = f"{type(e).__name__}: {e}"

    print(json.dumps({"tool_names": tool_names, "errors": errors}))
""")


def _run_t6_sweep(fake_home: Path, project_root: Path, *, desktop_write: bool) -> dict:
    env = {**os.environ, "HOME": str(fake_home), "PM_LENS": "1"}
    if desktop_write:
        env["PM_DESKTOP_WRITE"] = "1"
    else:
        env.pop("PM_DESKTOP_WRITE", None)
    env.pop("VIRTUAL_ENV", None)

    proc = subprocess.run(
        [sys.executable, "-c", _T6_SWEEP_SCRIPT, "1" if desktop_write else "0"],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        cwd=str(project_root),
    )
    assert proc.returncode == 0, (
        f"subprocess failed: stderr={proc.stderr!r}, stdout={proc.stdout!r}"
    )
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_lens_pure_viewer_zero_fs_writes(tmp_path: Path) -> None:
    """T6 (ADR-039, NFR-1/C18): the pure Lens-viewer case (PM_LENS=1, no
    PM_DESKTOP_WRITE) — calling every tool that registers under this env with
    default arguments must produce ZERO filesystem writes under ~/.pm or the
    project's .pm. This is the primary DoD4 target: "writes from a Lens host
    are impossible outside the Desktop outbox" — and in the pure-viewer case
    there should be no writable surface reachable at all.
    """
    fake_home, project_root = _seed_lens_invariant_fixture(tmp_path)
    project_pm = project_root / ".pm"
    desktop_db = fake_home / ".pm" / "desktop" / "desktop.db"
    claude_settings = fake_home / ".claude" / "settings.json"
    assert not desktop_db.exists()
    assert not claude_settings.exists()

    # Snapshot the WHOLE fake HOME, not just ~/.pm: a Lens read must write
    # nothing anywhere under HOME, including the host-config dir ~/.claude.
    # Scoping the old snapshot to ~/.pm is precisely what let PMSERV-144
    # (pm_status auto-installing hooks into ~/.claude/settings.json) slip past.
    before = {"home": _fs_snapshot(fake_home), "project": _fs_snapshot(project_pm)}
    result = _run_t6_sweep(fake_home, project_root, desktop_write=False)
    after = {"home": _fs_snapshot(fake_home), "project": _fs_snapshot(project_pm)}

    # Sanity: the sweep actually exercised a meaningful RO surface — otherwise
    # this test would trivially pass even if REGISTERED_TOOLS were empty.
    assert len(result["tool_names"]) >= 10, result["tool_names"]
    assert "pm_status" in result["tool_names"]
    assert "pm_outbox_pending" in result["tool_names"]
    # Pure viewer: the outbox WRITE tools must not even be registered.
    assert "pm_outbox_remember" not in result["tool_names"]
    assert "pm_outbox_log" not in result["tool_names"]

    diff = {
        "home_added": [e for e in after["home"] if e not in before["home"]],
        "home_removed": [e for e in before["home"] if e not in after["home"]],
        "project_added": [e for e in after["project"] if e not in before["project"]],
        "project_removed": [e for e in before["project"] if e not in after["project"]],
    }
    assert after == before, (
        "PM_LENS=1 (pure viewer) full-tool sweep mutated the filesystem under "
        f"HOME (~/.pm, ~/.claude, …) or project .pm: {diff}"
    )

    # PMSERV-144: pm_status must not auto-install hooks under a Lens host.
    assert not claude_settings.exists(), (
        "pm_status wrote ~/.claude/settings.json during a pure-viewer Lens "
        "sweep — hook auto-install leaked past the read-only boundary"
    )

    # Explicit desktop.db (+ WAL/SHM sidecar) non-creation assertion.
    assert not desktop_db.exists(), "desktop.db created by a pure-viewer Lens sweep"
    assert not desktop_db.with_name("desktop.db-wal").exists()
    assert not desktop_db.with_name("desktop.db-shm").exists()


def test_lens_desktop_outbox_host_zero_fs_writes(tmp_path: Path) -> None:
    """T6 sibling: the Desktop outbox host case (PM_LENS=1 + PM_DESKTOP_WRITE=1)
    must ALSO produce zero filesystem writes when every registered tool is
    called with default (i.e. no) arguments. pm_outbox_remember/pm_outbox_log
    are reachable here, but both require a positional arg (content/entry) the
    zero-arg call never supplies, so they raise before ever touching
    desktop.db — confirming the outbox-host build doesn't accidentally widen
    the writable surface for THIS call shape either.
    """
    fake_home, project_root = _seed_lens_invariant_fixture(tmp_path)
    project_pm = project_root / ".pm"
    desktop_db = fake_home / ".pm" / "desktop" / "desktop.db"
    claude_settings = fake_home / ".claude" / "settings.json"
    assert not desktop_db.exists()
    assert not claude_settings.exists()

    # Snapshot the whole fake HOME (see the pure-viewer sibling): the outbox
    # host may only ever write to desktop.db via an explicit outbox writer —
    # never to ~/.pm, ~/.claude, or the project's .pm on a bare read sweep.
    before = {"home": _fs_snapshot(fake_home), "project": _fs_snapshot(project_pm)}
    result = _run_t6_sweep(fake_home, project_root, desktop_write=True)
    after = {"home": _fs_snapshot(fake_home), "project": _fs_snapshot(project_pm)}

    assert len(result["tool_names"]) >= 10, result["tool_names"]
    # Outbox host: RO tools + outbox-read + outbox-write tools are all visible.
    assert "pm_outbox_remember" in result["tool_names"]
    assert "pm_outbox_log" in result["tool_names"]
    assert "pm_outbox_pending" in result["tool_names"]
    # Both outbox writers must have errored (content/entry are required
    # positional args) — confirms the "zero writes" result below isn't a
    # false negative from the writers silently no-op'ing on empty input.
    assert "pm_outbox_remember" in result["errors"], result["errors"]
    assert "pm_outbox_log" in result["errors"], result["errors"]

    diff = {
        "home_added": [e for e in after["home"] if e not in before["home"]],
        "home_removed": [e for e in before["home"] if e not in after["home"]],
        "project_added": [e for e in after["project"] if e not in before["project"]],
        "project_removed": [e for e in before["project"] if e not in after["project"]],
    }
    assert after == before, (
        "PM_LENS=1 + PM_DESKTOP_WRITE=1 full-tool sweep (default-arg calls "
        f"only) mutated the filesystem under HOME (~/.pm, ~/.claude, …) or "
        f"project .pm: {diff}"
    )

    # PMSERV-144: hook auto-install must be gated off on a Lens host here too.
    assert not claude_settings.exists(), (
        "pm_status wrote ~/.claude/settings.json during a Lens outbox-host sweep"
    )

    assert not desktop_db.exists(), (
        "desktop.db created even though both outbox writers errored on "
        "missing required args before reaching store.append()"
    )
    assert not desktop_db.with_name("desktop.db-wal").exists()
    assert not desktop_db.with_name("desktop.db-shm").exists()


# ─── PMSERV-144: pm_status must not auto-install hooks under a Lens host ────
# pm_status() carries a convenience auto-install (get_hooks_status → if not
# installed, install_hooks()) that writes ~/.claude/settings.json — a
# host-config file OUTSIDE the .pm trees the T6 sweep historically snapshotted,
# which is exactly how this read-path write leaked past the invariant sweep.
# This test pins the host-config surface directly and asserts the read still
# reports hook status truthfully (installed=False) without touching disk.

_PM_STATUS_HOOKS_SCRIPT = textwrap.dedent("""
    import json

    import pmlens.server as srv

    assert srv.PM_LENS_ENABLED is True, "PM_LENS not picked up in subprocess"
    result = srv.pm_status()
    # A "hooks" key only exists if pm_status ran to completion — i.e. it
    # actually reached the auto-install block being guarded (line ~483).
    print(json.dumps({"hooks": result.get("hooks")}))
""")


def test_lens_pm_status_does_not_write_claude_settings(tmp_path: Path) -> None:
    """PMSERV-144: pm_status() under PM_LENS=1 must not create or mutate
    ~/.claude/settings.json. A Lens viewer reports hook status read-only;
    the auto-install convenience is Claude-Code-only (PM_LENS=0)."""
    fake_home, project_root = _seed_lens_invariant_fixture(tmp_path)
    claude_dir = fake_home / ".claude"
    settings = claude_dir / "settings.json"
    # Precondition: a fresh Lens machine with no host-config file yet.
    assert not settings.exists()

    env = {**os.environ, "HOME": str(fake_home), "PM_LENS": "1"}
    env.pop("VIRTUAL_ENV", None)
    env.pop("PM_DESKTOP_WRITE", None)

    proc = subprocess.run(
        [sys.executable, "-c", _PM_STATUS_HOOKS_SCRIPT],
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
        cwd=str(project_root),
    )
    assert proc.returncode == 0, (
        f"subprocess failed: stderr={proc.stderr!r}, stdout={proc.stdout!r}"
    )

    # Invariant: the Lens read created neither the host-config file nor its dir.
    assert not settings.exists(), (
        "pm_status under PM_LENS=1 wrote ~/.claude/settings.json — hook "
        "auto-install leaked through the Lens read-only boundary (PMSERV-144)"
    )
    assert not claude_dir.exists(), (
        "pm_status under PM_LENS=1 created ~/.claude/ — a Lens read must not "
        "touch the host-config dir (PMSERV-144)"
    )

    # And the read still reports status truthfully: hooks are NOT installed.
    payload = json.loads(proc.stdout.strip().splitlines()[-1])
    assert payload["hooks"] == {"installed": False, "path": str(settings)}, payload
