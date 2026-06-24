"""Shared fixtures for PM Lens tests."""

import datetime as _dt
import json
from dataclasses import dataclass
from pathlib import Path

import pytest

import pm_server.server
import pm_server.storage
from pm_server.models import (
    Consequences,
    Decision,
    Phase,
    PhaseStatus,
    Priority,
    Project,
    ProjectStatus,
    Task,
    TaskStatus,
)


@pytest.fixture(autouse=True)
def isolated_registry(tmp_path, monkeypatch):
    """Isolate all tests from the real ~/.pm/ registry.

    Patches GLOBAL_PM_DIR so that any registry function called without
    an explicit registry_dir will write to a temp directory instead of
    the user's real ~/.pm/.
    """
    fake_global_pm = tmp_path / "fake_global_pm"
    fake_global_pm.mkdir()
    monkeypatch.setattr(pm_server.storage, "GLOBAL_PM_DIR", fake_global_pm)
    # PMSERV-066: server.py also imports GLOBAL_PM_DIR at module-import time
    # (server.py:from .storage import GLOBAL_PM_DIR), so the storage-side
    # monkeypatch alone leaves the server-side binding pointing at the real
    # ~/.pm/. Patch both to keep all GLOBAL_PM_DIR consumers in lock-step.
    monkeypatch.setattr(pm_server.server, "GLOBAL_PM_DIR", fake_global_pm)
    # ADR-019 / WF-028: clear the module-level outbox factory so each test
    # starts with a fresh DesktopOutboxStore. Otherwise the cached store
    # from a previous test points at a tmp_path that pytest has deleted,
    # and any test that calls pm_status (which probes outbox_pending) gets
    # a stale handle. The factory's first call after this fixture re-binds
    # to the current monkeypatched GLOBAL_PM_DIR via default_outbox_db_path().
    from pm_server.outbox import clear_outbox_store

    clear_outbox_store()
    # PMSERV-113 / PMSERV-114: same rationale for the per-project x_drafts
    # factory — clear the db_path-keyed cache so a store bound to a now-deleted
    # tmp_path does not leak into the next test (e.g. via pm_status probing
    # x_drafts_pending). The factory re-binds on next use.
    from pm_server.x_draft_store import clear_x_draft_store

    clear_x_draft_store()


@pytest.fixture
def tmp_project(tmp_path: Path) -> Path:
    """Create a temporary project directory with .pm/ initialized."""
    pm_path = tmp_path / ".pm"
    pm_path.mkdir()
    (pm_path / "daily").mkdir()
    return tmp_path


@pytest.fixture
def tmp_pm_path(tmp_project: Path) -> Path:
    """Return the .pm/ path inside the temp project."""
    return tmp_project / ".pm"


@pytest.fixture
def tmp_registry_dir(tmp_path: Path) -> Path:
    """Create a temp directory for the global registry."""
    reg_dir = tmp_path / "pm-registry"
    reg_dir.mkdir()
    return reg_dir


@pytest.fixture
def sample_project() -> Project:
    return Project(
        name="testproj",
        display_name="Test Project",
        version="1.0.0",
        status=ProjectStatus.DEVELOPMENT,
        started=_dt.date(2026, 4, 1),
        description="A test project",
        phases=[
            Phase(id="phase-0", name="Design", status=PhaseStatus.COMPLETED),
            Phase(
                id="phase-1",
                name="Core",
                status=PhaseStatus.ACTIVE,
                target_date=_dt.date(2026, 5, 1),
            ),
        ],
    )


@pytest.fixture
def sample_tasks() -> list[Task]:
    return [
        Task(
            id="TEST-001",
            title="Setup project",
            phase="phase-0",
            status=TaskStatus.DONE,
            priority=Priority.P0,
            created=_dt.date(2026, 4, 1),
            updated=_dt.date(2026, 4, 2),
        ),
        Task(
            id="TEST-002",
            title="Implement core",
            phase="phase-1",
            status=TaskStatus.TODO,
            priority=Priority.P0,
            tags=["core"],
            estimate_hours=8.0,
        ),
        Task(
            id="TEST-003",
            title="Write docs",
            phase="phase-1",
            status=TaskStatus.TODO,
            priority=Priority.P2,
            depends_on=["TEST-002"],
        ),
        Task(
            id="TEST-004",
            title="Fix blocked issue",
            phase="phase-1",
            status=TaskStatus.BLOCKED,
            priority=Priority.P1,
            blocked_by=["TEST-002"],
            updated=_dt.date(2026, 3, 20),
        ),
    ]


@pytest.fixture
def memory_store(tmp_path: Path):
    """Create a MemoryStore backed by a temp database.

    Global sync is pointed at a temp directory to avoid touching ~/.pm/.
    """
    from pm_server.memory import MemoryStore

    db_path = tmp_path / "test_memory.db"
    global_path = tmp_path / "global_pm" / "memory.db"
    store = MemoryStore(db_path, global_db_path=global_path)
    yield store
    store.close()


@pytest.fixture
def sample_decision() -> Decision:
    return Decision(
        id="ADR-001",
        title="Use YAML for storage",
        date=_dt.date(2026, 4, 1),
        context="Need human-readable, git-friendly format.",
        decision="Use YAML with safe_load only.",
        consequences=Consequences(
            positive=["Git-friendly diffs"],
            negative=["Slower than binary formats"],
        ),
    )


@pytest.fixture
def clean_host_env(monkeypatch):
    """Clear host-detection environment variables for deterministic tests.

    Use this fixture in tests that exercise ``rules.detect_hosts`` or
    related auto-target logic. It is intentionally NOT autouse to avoid
    breaking existing installer tests that monkeypatch ``shutil.which``
    or rely on inherited Claude Code env vars.
    """
    for var in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT"):
        monkeypatch.delenv(var, raising=False)


# ─── PMSERV-137 Phase-3 rename migration: legacy-user environment ──────────
#
# Scaffolding for the pm_server → pmlens identity-rename migration tests
# (ADR-034 / ADR-032). Builds a realistic *pre-Phase-3* user environment so the
# future ``pmlens migrate-from-pm-server`` updater can be tested end-to-end:
# a CLAUDE.md carrying the (invariant) marker block, a settings.json with the
# manual post-commit hook plus the three auto-approve perms that a naive key
# flip would silently break, and a Codex config.toml whose user-authored
# ``tools.*`` sub-tables the re-key must preserve byte-for-byte. Opt-in (NOT
# autouse), so it never perturbs the existing suite.


@dataclass(frozen=True)
class LegacyUserEnv:
    """Paths + canonical expectations for a pre-rename user (see fixture)."""

    home: Path
    project_root: Path
    claude_md: Path
    settings_json: Path
    codex_config: Path
    perm_entries: tuple[str, ...]
    hook_command: str
    codex_tool_subtables: tuple[str, ...]


@pytest.fixture
def legacy_user_env(tmp_path, monkeypatch) -> LegacyUserEnv:
    """A pre-Phase-3 'legacy user' still on the old pm-server identity.

    Returns a :class:`LegacyUserEnv` describing the four surfaces the rename
    migration must touch without data loss, and points ``$HOME`` / ``Path.home``
    at a tmp fake home so settings.json and the Codex config resolve there.
    Step 1 only asserts the fixture is well-formed; steps 3-6 drive the actual
    ``migrate-from-pm-server`` against it.
    """
    from pm_server.rules import BEGIN_MARKER, END_MARKER, TEMPLATE_VERSION

    fake_home = tmp_path / "legacy_home"
    (fake_home / ".claude").mkdir(parents=True)
    (fake_home / ".codex").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setattr(Path, "home", lambda: fake_home)

    # 1. project CLAUDE.md with the INVARIANT marker block — the rename must
    #    upgrade it in place via the marker, never append a duplicate block.
    project_root = tmp_path / "legacy_project"
    project_root.mkdir()
    marker_block = (
        f"{BEGIN_MARKER.format(version=TEMPLATE_VERSION)}\n"
        "## PM Lens 自動行動ルール（必ず従うこと）\n"
        f"{END_MARKER}"
    )
    claude_md = project_root / "CLAUDE.md"
    claude_md.write_text(f"# Legacy project notes\n\n{marker_block}\n", encoding="utf-8")

    # 2. settings.json: manual post-commit hook + the 3 auto-approve perms that a
    #    key flip would silently revert to prompting (the SILENT-breakage face).
    perm_entries = (
        "mcp__pm-server__pm_add_task",
        "mcp__pm-server__pm_update_task",
        "mcp__pm-server__pm_remember",
    )
    hook_command = "pm-server hook post-tool-use"
    settings_json = fake_home / ".claude" / "settings.json"
    settings_json.write_text(
        json.dumps(
            {
                "permissions": {"allow": list(perm_entries)},
                "hooks": {
                    "PostToolUse": [
                        {
                            "matcher": "Bash",
                            "hooks": [{"type": "command", "command": hook_command}],
                        }
                    ]
                },
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    # 3. Codex config.toml: user-authored tools.* sub-tables that the re-key must
    #    deep-copy from [mcp_servers.pm-server] to [mcp_servers.pmlens] intact.
    codex_tool_subtables = ("pm_init", "pm_status")
    codex_config = fake_home / ".codex" / "config.toml"
    codex_config.write_text(
        "[mcp_servers.pm-server]\n"
        'command = "/old/path/to/pm-server"\n'
        'args = ["serve"]\n'
        "startup_timeout_sec = 30\n\n"
        "[mcp_servers.pm-server.tools.pm_init]\n"
        'approval_mode = "approve"\n\n'
        "[mcp_servers.pm-server.tools.pm_status]\n"
        'approval_mode = "approve"\n',
        encoding="utf-8",
    )

    return LegacyUserEnv(
        home=fake_home,
        project_root=project_root,
        claude_md=claude_md,
        settings_json=settings_json,
        codex_config=codex_config,
        perm_entries=perm_entries,
        hook_command=hook_command,
        codex_tool_subtables=codex_tool_subtables,
    )
