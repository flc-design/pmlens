"""Shared fixtures for PM Lens tests."""

import datetime as _dt
import json
import os
from dataclasses import dataclass
from pathlib import Path

import pytest

import pmlens.server
import pmlens.storage
from pmlens.models import (
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

# ─── The real host configs must survive the test suite ────────────────────
#
# pmlens writes to files in $HOME: ~/.codex/config.toml, ~/.cursor/mcp.json,
# ~/.grok/config.toml, ~/.claude/settings.json. Every writer test redirects
# those paths at tmp_path — but a redirect that MISSES does not merely fail an
# assertion, it edits the developer's real editor configuration. That happened
# while PMSERV-165 was being written: an installer refactor resolved its config
# path through the host registry instead of the module-level seam the fixture
# patched, and a plain `pytest -q` registered pmlens into the real ~/.grok and
# ~/.cursor. It was noticed only because the assertion downstream happened to
# fail; a passing test would have been silent.
#
# Captured at import time, before any fixture can monkeypatch HOME.
_REAL_HOME = Path.home()

# Config files whose CONTENT is watched. Nothing on a normal dev machine
# rewrites these while a test run is in flight — but a Cursor window toggling an
# MCP server, or a second agent running `pmlens install`, can. That would surface
# as a failure blaming whichever test was executing. The trade is deliberate:
# a rare confusing failure is much cheaper than a silent rewrite of a real
# config, which is what this guard exists to make impossible.
_PROTECTED_HOST_FILES: tuple[Path, ...] = (
    _REAL_HOME / ".codex" / "config.toml",
    _REAL_HOME / ".cursor" / "mcp.json",
    _REAL_HOME / ".grok" / "config.toml",
)

# Files owned by a Claude Code process that may be running RIGHT NOW — quite
# possibly the one running this very suite. Their content churns for reasons
# that have nothing to do with the tests, so comparing it blames whichever test
# happened to be executing (observed: an unrelated FTS test failed because the
# live session touched ~/.claude.json mid-run). Only pmlens's own signature is
# watched here — see _backup_names.
_BACKUP_ONLY_HOST_FILES: tuple[Path, ...] = (
    _REAL_HOME / ".claude" / "settings.json",
    _REAL_HOME / ".claude.json",
)


def _backup_names(path: Path) -> list[str]:
    """Timestamped backups sitting next to ``path``.

    pmlens always writes ``<name>.bak.<ts>`` before editing a host config, and
    nothing else on the machine creates that pattern. So a new backup is an
    unambiguous fingerprint of a pmlens writer that escaped its sandbox —
    including the install-then-uninstall case, where the file content ends up
    byte-identical and only the litter gives it away.
    """
    try:
        return sorted(p.name for p in path.parent.glob(f"{path.name}.bak.*"))
    except OSError:
        return []


def _host_config_fingerprint() -> dict[str, object]:
    """Snapshot of everything a stray pmlens write would disturb.

    An unreadable file records the exception rather than ``None``, so
    "permission denied both times" cannot masquerade as "unchanged" the way a
    shared sentinel would — the same class of bug as ``pathlib.glob``
    swallowing PermissionError and returning empty.
    """
    snapshot: dict[str, object] = {}
    for path in _PROTECTED_HOST_FILES:
        try:
            snapshot[str(path)] = path.read_bytes()
        except FileNotFoundError:
            snapshot[str(path)] = "<absent>"
        except OSError as e:
            snapshot[str(path)] = f"<unreadable: {e.__class__.__name__}: {e}>"
        snapshot[f"{path}::backups"] = _backup_names(path)
    for path in _BACKUP_ONLY_HOST_FILES:
        snapshot[f"{path}::backups"] = _backup_names(path)
    return snapshot


def pytest_configure(config):
    """Point ``$HOME`` at a session sandbox before ANYTHING else runs.

    The per-test :func:`isolated_home` fixture cannot cover collection-time
    code, module- or session-scoped fixtures, or ``pytest_*`` hooks — all of
    which run before any function-scoped fixture. Anything resolving
    ``Path.home()`` at that layer would reach the developer's real home, and
    the per-test detection guard could not see it either, because its "before"
    snapshot is taken afterwards. Setting HOME here closes that window; the
    per-test fixture then narrows it further to one directory per test.

    ``_REAL_HOME`` was captured at import time, above, so the detection guards
    still watch the true home.
    """
    import tempfile

    sandbox = Path(tempfile.mkdtemp(prefix="pmlens-session-home-"))
    os.environ["HOME"] = str(sandbox)
    os.environ["USERPROFILE"] = str(sandbox)
    config._pmlens_session_home = sandbox
    config._pmlens_session_fingerprint = _host_config_fingerprint()


def pytest_sessionfinish(session, exitstatus):
    """Session-scoped half of the detection guard.

    Catches a real-home mutation made outside any test function — by a
    module-scoped fixture, a collection-time import, or a plugin hook — which
    the function-scoped guard structurally cannot observe.
    """
    before = getattr(session.config, "_pmlens_session_fingerprint", None)
    if before is None:  # pragma: no cover - only if pytest_configure was skipped
        return
    changed = [key for key, value in before.items() if _host_config_fingerprint()[key] != value]
    if changed:
        raise pytest.UsageError(
            "the test SESSION modified the real host configuration in $HOME "
            f"outside any test function: {changed}. Something at module or "
            "collection scope resolved a real-home path — the per-test "
            "isolated_home fixture cannot cover that layer."
        )


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """Point ``$HOME`` at a per-test sandbox — the PREVENTION half.

    ``Path.home()`` resolves through ``$HOME`` on POSIX, so every host config
    path pmlens derives (``~/.codex``, ``~/.cursor``, ``~/.grok``,
    ``~/.claude``) lands under ``tmp_path`` by default. A test that forgets to
    redirect a writer now writes into its own sandbox instead of the
    developer's real configuration.

    Tests that need a specific home still set ``HOME`` themselves; a later
    ``monkeypatch.setenv`` simply wins. This is the same two-lever isolation
    the plugin's manual test harness uses (``CLAUDE_CONFIG_DIR`` + ``env.HOME``),
    applied to the suite itself, and it pairs with the detection guard below:
    prevention for anything that goes through ``$HOME``, detection for anything
    that does not.
    """
    # Deliberately not "fake_home": several suites already create a directory
    # by that name under tmp_path for their own HOME fixtures.
    sandbox = tmp_path / "_isolated_home"
    sandbox.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(sandbox))
    monkeypatch.setenv("USERPROFILE", str(sandbox))  # Windows equivalent
    return sandbox


@pytest.fixture(autouse=True)
def real_host_configs_untouched():
    """Fail loudly if a test mutates a REAL host config outside its sandbox.

    Converts "your ~/.grok was silently rewritten" into a named test failure.
    Deliberately autouse and unconditional: the tests that need this guard are
    exactly the ones whose author did not realise they needed it. This is the
    DETECTION half — it catches writers that bypass ``$HOME`` entirely (an
    absolute path, a cached module-level constant) which :func:`isolated_home`
    cannot prevent.
    """
    before = _host_config_fingerprint()
    yield
    after = _host_config_fingerprint()
    changed = [key for key in before if before[key] != after[key]]
    assert not changed, (
        "this test modified the REAL host configuration in $HOME instead of a "
        f"sandbox: {changed}. A writer resolved its config path outside the "
        "monkeypatched seam — patch `pmlens.installer._codex_config_path` / "
        "`_grok_config_path` / `_cursor_config_path` (or set HOME to tmp_path) "
        "so the write lands in tmp_path."
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
    monkeypatch.setattr(pmlens.storage, "GLOBAL_PM_DIR", fake_global_pm)
    # PMSERV-066: server.py also imports GLOBAL_PM_DIR at module-import time
    # (server.py:from .storage import GLOBAL_PM_DIR), so the storage-side
    # monkeypatch alone leaves the server-side binding pointing at the real
    # ~/.pm/. Patch both to keep all GLOBAL_PM_DIR consumers in lock-step.
    monkeypatch.setattr(pmlens.server, "GLOBAL_PM_DIR", fake_global_pm)
    # ADR-019 / WF-028: clear the module-level outbox factory so each test
    # starts with a fresh DesktopOutboxStore. Otherwise the cached store
    # from a previous test points at a tmp_path that pytest has deleted,
    # and any test that calls pm_status (which probes outbox_pending) gets
    # a stale handle. The factory's first call after this fixture re-binds
    # to the current monkeypatched GLOBAL_PM_DIR via default_outbox_db_path().
    from pmlens.outbox import clear_outbox_store

    clear_outbox_store()
    # PMSERV-113 / PMSERV-114: same rationale for the per-project drafts
    # factory — clear the db_path-keyed cache so a store bound to a now-deleted
    # tmp_path does not leak into the next test (e.g. via pm_status probing
    # x_drafts_pending). The factory re-binds on next use.
    from pmlens.draft_store import clear_draft_store

    clear_draft_store()


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
    from pmlens.memory import MemoryStore

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
    from pmlens.rules import BEGIN_MARKER, END_MARKER, TEMPLATE_VERSION

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
