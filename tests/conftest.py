"""Shared fixtures for PM Server tests."""

import datetime as _dt
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
