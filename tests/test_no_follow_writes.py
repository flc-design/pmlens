"""Writes into .pm/ follow no symlink a repository shipped there (PMSERV-277).

git checks symlinks out as links, so a cloned repository can put
``.pm/.locks/.gitignore``, ``.pm/.locks``, ``.pm/daily`` or a ledger file in
place as a link. pmlens used to write through them: a dangling ``.gitignore``
link created git's global excludes file with ``*`` in it, a linked ``.locks``
put lock files elsewhere (filelock truncates a same-named file there), and
``pm_log`` wrote into whatever ``daily`` pointed at. A symlinked ``.pm`` itself
is the user's choice and keeps working.
"""

from __future__ import annotations

import datetime as dt
import os
import stat
from pathlib import Path

import pytest

import pmlens.server as srv
from pmlens.ledger_errors import LedgerWriteRefused
from pmlens.models import DailyLogEntry, LogCategory, Task
from pmlens.storage import add_daily_log, add_task, init_pm_directory, load_tasks


def _project(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    root.mkdir()
    pm = init_pm_directory(root)
    (pm / "project.yaml").write_text("name: proj\n", encoding="utf-8")
    return pm


def _task(n: int) -> Task:
    return Task(id=f"T-{n:03d}", title=f"t{n}", phase="p")


def _outside(tmp_path: Path) -> Path:
    outside = tmp_path / "outside"
    outside.mkdir()
    return outside


class TestLocks:
    def test_a_dangling_gitignore_link_is_not_written_through(self, tmp_path):
        # The confirmed case: the link pointed at git's global excludes file.
        pm = _project(tmp_path)
        target = _outside(tmp_path) / "git" / "ignore"
        (pm / ".locks").mkdir(exist_ok=True)
        (pm / ".locks" / ".gitignore").symlink_to(target)

        add_task(pm, _task(1))

        assert not target.exists()
        assert (pm / ".locks" / ".gitignore").is_symlink()
        assert [t.id for t in load_tasks(pm)] == ["T-001"]

    def test_a_gitignore_link_to_an_existing_file_is_left_alone(self, tmp_path):
        pm = _project(tmp_path)
        target = _outside(tmp_path) / "notes.txt"
        target.write_text("ORIGINAL", encoding="utf-8")
        (pm / ".locks").mkdir(exist_ok=True)
        (pm / ".locks" / ".gitignore").symlink_to(target)

        add_task(pm, _task(1))

        assert target.read_text(encoding="utf-8") == "ORIGINAL"

    def test_a_symlinked_locks_directory_is_refused(self, tmp_path):
        pm = _project(tmp_path)
        outside = _outside(tmp_path)
        (outside / "tasks.lock").write_text("precious", encoding="utf-8")
        (pm / ".locks").symlink_to(outside, target_is_directory=True)

        with pytest.raises(LedgerWriteRefused, match=r"\.pm/\.locks") as caught:
            add_task(pm, _task(1))

        assert "symlink" in str(caught.value)
        assert sorted(p.name for p in outside.iterdir()) == ["tasks.lock"]
        assert (outside / "tasks.lock").read_text(encoding="utf-8") == "precious"
        assert load_tasks(pm) == []

    def test_a_symlinked_lock_file_is_refused_cleanly(self, tmp_path):
        pm = _project(tmp_path)
        victim = _outside(tmp_path) / "victim"
        victim.write_text("precious", encoding="utf-8")
        (pm / ".locks").mkdir(exist_ok=True)
        (pm / ".locks" / "tasks.lock").symlink_to(victim)

        with pytest.raises(LedgerWriteRefused, match=r"\.pm/\.locks/tasks\.lock"):
            add_task(pm, _task(1))

        assert victim.read_text(encoding="utf-8") == "precious"

    def test_the_gitignore_is_still_seeded(self, tmp_path):
        pm = _project(tmp_path)
        add_task(pm, _task(1))
        assert (pm / ".locks" / ".gitignore").read_text(encoding="utf-8") == "*\n!.gitignore\n"


class TestLedgers:
    def test_a_symlinked_daily_directory_is_refused(self, tmp_path):
        pm = _project(tmp_path)
        outside = _outside(tmp_path)
        (pm / "daily").rmdir()
        (pm / "daily").symlink_to(outside, target_is_directory=True)
        entry = DailyLogEntry(time="10:00", category=LogCategory.NOTE, entry="x")

        with pytest.raises(LedgerWriteRefused, match=r"\.pm/daily/"):
            add_daily_log(pm, entry, dt.date(2026, 10, 20))

        assert list(outside.iterdir()) == []

    def test_a_symlinked_ledger_file_is_refused_not_replaced(self, tmp_path):
        # It used to be replaced by a regular file on the first write, silently.
        pm = _project(tmp_path)
        other = _outside(tmp_path) / "tasks.yaml"
        other.write_text("tasks: []\n", encoding="utf-8")
        (pm / "tasks.yaml").symlink_to(other)

        with pytest.raises(LedgerWriteRefused, match=r"\.pm/tasks\.yaml"):
            add_task(pm, _task(1))

        assert (pm / "tasks.yaml").is_symlink()
        assert other.read_text(encoding="utf-8") == "tasks: []\n"

    def test_a_symlinked_pm_directory_still_works(self, tmp_path):
        real = tmp_path / "synced" / "pm"
        (real / "daily").mkdir(parents=True)
        (real / "project.yaml").write_text("name: proj\n", encoding="utf-8")
        root = tmp_path / "proj"
        root.mkdir()
        (root / ".pm").symlink_to(real, target_is_directory=True)

        add_task(root / ".pm", _task(1))
        entry = DailyLogEntry(time="10:00", category=LogCategory.NOTE, entry="x")
        add_daily_log(root / ".pm", entry, dt.date(2026, 10, 20))

        assert [t.id for t in load_tasks(real)] == ["T-001"]
        assert (real / "daily" / "2026-10-20.yaml").is_file()

    def test_permissions_are_kept_like_before(self, tmp_path):
        pm = _project(tmp_path)
        add_task(pm, _task(1))
        ledger = pm / "tasks.yaml"
        ledger.chmod(0o600)
        add_task(pm, _task(2))
        assert stat.S_IMODE(ledger.stat().st_mode) == 0o600
        ledger.unlink()
        old = os.umask(0o002)
        try:
            add_task(pm, _task(3))
        finally:
            os.umask(old)
        assert stat.S_IMODE(ledger.stat().st_mode) == 0o664


def test_the_tool_reports_the_refusal_without_raising_raw_os_errors(tmp_path):
    pm = _project(tmp_path)
    (pm / ".locks").symlink_to(_outside(tmp_path), target_is_directory=True)
    with pytest.raises(LedgerWriteRefused) as caught:
        srv.pm_add_task(title="t", phase="p", project_path=str(pm.parent))
    message = str(caught.value)
    assert message.startswith("refusing to write .pm/.locks")
    assert "make .pm itself the symlink" in message


# ─── SQLite stores (memory.db, drafts.db) ────────────


def _sqlite_file(path: Path) -> bytes:
    import sqlite3
    from contextlib import closing

    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute("CREATE TABLE victim (x TEXT)")
        conn.execute("INSERT INTO victim VALUES ('precious')")
    return path.read_bytes()


class TestSqliteStores:
    def test_a_dangling_memory_db_link_is_refused_and_creates_nothing(self, tmp_path):
        from pmlens.memory import MemoryStore

        pm = _project(tmp_path)
        target = _outside(tmp_path) / "loot.db"
        (pm / "memory.db").symlink_to(target)

        with pytest.raises(LedgerWriteRefused, match=r"\.pm/memory\.db"):
            MemoryStore(pm / "memory.db", global_db_path=None)

        assert not target.exists()
        assert sorted(p.name for p in target.parent.iterdir()) == []

    def test_a_memory_db_link_to_another_database_is_refused(self, tmp_path):
        from pmlens.memory import MemoryStore

        pm = _project(tmp_path)
        other = _outside(tmp_path) / "other.db"
        before = _sqlite_file(other)
        (pm / "memory.db").symlink_to(other)

        with pytest.raises(LedgerWriteRefused):
            MemoryStore(pm / "memory.db", global_db_path=None)
        with pytest.raises(LedgerWriteRefused):
            MemoryStore(pm / "memory.db", global_db_path=None, readonly=True)

        assert other.read_bytes() == before

    def test_a_symlinked_sidecar_is_refused(self, tmp_path):
        from pmlens.memory import MemoryStore

        pm = _project(tmp_path)
        MemoryStore(pm / "memory.db", global_db_path=None).close()
        victim = _outside(tmp_path) / "victim"
        victim.write_text("precious", encoding="utf-8")
        for sidecar in ("memory.db-wal", "memory.db-shm"):
            (pm / sidecar).unlink(missing_ok=True)
        (pm / "memory.db-wal").symlink_to(victim)

        with pytest.raises(LedgerWriteRefused, match="-wal"):
            MemoryStore(pm / "memory.db", global_db_path=None)
        assert victim.read_text(encoding="utf-8") == "precious"

    def test_a_link_swapped_in_after_the_check_is_caught_by_sqlite_itself(
        self, tmp_path, monkeypatch
    ):
        import pmlens.safe_sqlite as safe_sqlite
        from pmlens.utils import FileRefusedError

        pm = _project(tmp_path)
        other = _outside(tmp_path) / "other.db"
        before = _sqlite_file(other)
        (pm / "memory.db").symlink_to(other)
        decoy = os.stat(other)  # what a check before the swap would have seen
        monkeypatch.setattr(
            safe_sqlite, "_lstat_at", lambda name, fd, d: decoy if name == "memory.db" else None
        )

        with pytest.raises(FileRefusedError, match="changed while it was being opened"):
            safe_sqlite.connect_store(pm / "memory.db")
        assert other.read_bytes() == before

    def test_regular_stores_and_a_symlinked_pm_still_work(self, tmp_path):
        from pmlens.memory import MemoryStore
        from pmlens.models import Memory

        real = tmp_path / "synced" / "pm"
        real.mkdir(parents=True)
        root = tmp_path / "proj"
        root.mkdir()
        (root / ".pm").symlink_to(real, target_is_directory=True)
        store = MemoryStore(root / ".pm" / "memory.db", global_db_path=None)
        try:
            store.save(Memory(session_id="s", content="kept", project="p"))
            assert [m.content for m in store.get_recent(limit=5)] == ["kept"]
        finally:
            store.close()
        assert (real / "memory.db").is_file()

    def test_a_dangling_drafts_db_link_is_refused_and_creates_nothing(self, tmp_path):
        from pmlens.draft_store import DraftStore
        from pmlens.models import PmServerError

        pm = _project(tmp_path)
        target = _outside(tmp_path) / "stolen.db"
        (pm / "drafts.db").symlink_to(target)

        with pytest.raises(PmServerError, match=r"\.pm/drafts\.db"):
            DraftStore(pm / "drafts.db")
        assert not target.exists()

    def test_pm_status_reports_linked_stores_without_opening_them(self, tmp_path):
        pm = _project(tmp_path)
        outside = _outside(tmp_path)
        target = outside / "stolen.db"
        (pm / "drafts.db").symlink_to(target)
        (pm / "memory.db").symlink_to(outside / "mem.db")

        status = srv.pm_status(project_path=str(pm.parent))

        codes = {w["code"]: w for w in status["warnings"]}
        assert "draft_store_refused" in codes
        assert "memory.db" in codes["symlinks_in_pm_refused"]["message"]
        assert "drafts.db" in codes["symlinks_in_pm_refused"]["message"]
        assert status["diagnostics"]["x_drafts_pending"] is None
        assert not target.exists()

    def test_the_lens_stamp_does_not_block_on_a_fifo(self, tmp_path):
        import time

        pm = _project(tmp_path)
        os.mkfifo(pm / "memory.db")
        started = time.monotonic()
        assert srv._db_stamp(pm / "memory.db") == (False, 0, 0, 0)
        assert time.monotonic() - started < 2


# ─── rule files (CLAUDE.md / AGENTS.md) ──────────────


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    return root


class TestRuleFiles:
    def test_a_link_to_a_file_outside_the_project_is_neither_read_nor_written(self, tmp_path):
        from pmlens.rules import ensure_claudemd, get_rules_status, inject_pm_rules

        root = _repo(tmp_path)
        zshrc = _outside(tmp_path) / ".zshrc"
        zshrc.write_text("export PATH=/usr/bin\n", encoding="utf-8")
        (root / "CLAUDE.md").symlink_to(zshrc)
        (root / "AGENTS.md").symlink_to(zshrc)

        message = ensure_claudemd(root)
        summary = inject_pm_rules(root, target="all")
        status = get_rules_status(root)

        assert message.startswith(
            "left CLAUDE.md unchanged: CLAUDE.md is a symlink to a file outside"
        )
        assert {r.status for r in summary.results} == {"failed"}
        assert all("outside the project" in r.message for r in summary.results)
        assert all("refused" in v for v in status.values())
        assert zshrc.read_text(encoding="utf-8") == "export PATH=/usr/bin\n"
        assert sorted(p.name for p in zshrc.parent.iterdir()) == [".zshrc"]  # no backup there

    def test_a_dangling_link_outside_is_not_created(self, tmp_path):
        from pmlens.rules import inject_pm_rules

        root = _repo(tmp_path)
        target = _outside(tmp_path) / "AGENTS.md"
        (root / "AGENTS.md").symlink_to(target)

        summary = inject_pm_rules(root, target="codex")

        assert summary.results[0].status == "failed"
        assert not target.exists()

    def test_agents_md_linked_to_claude_md_keeps_working(self, tmp_path):
        from pmlens.rules import inject_pm_rules

        root = _repo(tmp_path)
        (root / "CLAUDE.md").write_text("# Team rules\n", encoding="utf-8")
        (root / "AGENTS.md").symlink_to("CLAUDE.md")

        summary = inject_pm_rules(root, target="all")

        assert summary.overall_status != "failed"
        text = (root / "CLAUDE.md").read_text(encoding="utf-8")
        assert text.startswith("# Team rules\n") and text.count("pm-server:begin") == 1
        assert (root / "AGENTS.md").is_symlink()

    def test_a_dangling_link_inside_the_project_creates_its_target(self, tmp_path):
        from pmlens.rules import ensure_claudemd

        root = _repo(tmp_path)
        (root / "CLAUDE.md").symlink_to("AGENTS.md")

        assert ensure_claudemd(root) == "created CLAUDE.md with PM Lens rules"
        assert (root / "CLAUDE.md").is_symlink()
        assert "pm-server:begin" in (root / "AGENTS.md").read_text(encoding="utf-8")

    @pytest.mark.parametrize("into", [".git", ".pm"])
    def test_links_into_git_or_pm_are_refused(self, tmp_path, into):
        from pmlens.rules import inject_pm_rules

        root = _repo(tmp_path)
        (root / into / "hooks").mkdir(parents=True)
        hook = root / into / "hooks" / "pre-commit"
        hook.write_text("#!/bin/sh\n", encoding="utf-8")
        (root / "CLAUDE.md").symlink_to(hook)

        summary = inject_pm_rules(root, target="claude-code")

        assert summary.results[0].status == "failed"
        assert hook.read_text(encoding="utf-8") == "#!/bin/sh\n"

    def test_a_link_through_a_linked_directory_is_refused(self, tmp_path):
        from pmlens.rules import inject_pm_rules

        root = _repo(tmp_path)
        outside = _outside(tmp_path)
        (outside / "rules.md").write_text("ORIGINAL", encoding="utf-8")
        (root / "docs").symlink_to(outside, target_is_directory=True)
        (root / "CLAUDE.md").symlink_to("docs/rules.md")

        summary = inject_pm_rules(root, target="claude-code")

        assert summary.results[0].status == "failed"
        assert (outside / "rules.md").read_text(encoding="utf-8") == "ORIGINAL"

    def test_a_fifo_rule_file_does_not_hang_status(self, tmp_path):
        import time

        from pmlens.rules import get_rules_status

        root = _repo(tmp_path)
        os.mkfifo(root / "CLAUDE.md")
        started = time.monotonic()
        status = get_rules_status(root)
        assert time.monotonic() - started < 2
        assert "refused" in status["claude_code"]

    def test_a_planted_backup_name_is_not_written_through(self, tmp_path, monkeypatch):
        import datetime as real_dt

        import pmlens.rules as rules
        from pmlens.rules import inject_pm_rules

        class Frozen(real_dt.datetime):
            @classmethod
            def now(cls, tz=None):
                return cls(2026, 10, 20, 12, 0, 0)

        monkeypatch.setattr(rules, "datetime", Frozen)
        root = _repo(tmp_path)
        (root / "CLAUDE.md").write_text("# Team rules\n", encoding="utf-8")
        victim = _outside(tmp_path) / "victim"
        victim.write_text("precious", encoding="utf-8")
        (root / "CLAUDE.md.bak.20261020-120000").symlink_to(victim)

        summary = inject_pm_rules(root, target="claude-code")

        assert summary.results[0].status == "appended"
        assert victim.read_text(encoding="utf-8") == "precious"
        assert summary.results[0].backup_path.name == "CLAUDE.md.bak.20261020-120000-1"
        assert summary.results[0].backup_path.read_text(encoding="utf-8") == "# Team rules\n"


# ─── review fixes (PMSERV-277 adversarial review) ────


class TestReviewFixes:
    def test_a_case_variant_of_git_is_refused(self, tmp_path):
        from pmlens.rules import inject_pm_rules

        root = _repo(tmp_path)
        (root / ".git").mkdir()
        config = root / ".git" / "config"
        config.write_text("[core]\n", encoding="utf-8")
        (root / "CLAUDE.md").symlink_to(".GIT/config")

        summary = inject_pm_rules(root, target="claude-code")

        assert summary.results[0].status == "failed"
        assert config.read_text(encoding="utf-8") == "[core]\n"
        assert sorted(p.name for p in (root / ".git").iterdir()) == ["config"]

    def test_a_case_variant_is_refused_by_name_before_git_exists(self, tmp_path):
        # Without a .git to compare identities with, the name check alone holds.
        from pmlens.rules import inject_pm_rules

        root = _repo(tmp_path)
        (root / "CLAUDE.md").symlink_to(".GIT/hooks/pre-commit")

        summary = inject_pm_rules(root, target="claude-code")

        assert summary.results[0].status == "failed"
        assert not (root / ".GIT").exists()

    def test_a_link_into_a_pm_alias_is_refused(self, tmp_path):
        from pmlens.rules import ensure_claudemd

        root = _repo(tmp_path)
        (root / "data").mkdir()
        (root / "data" / "tasks.yaml").write_text("tasks: []\n", encoding="utf-8")
        (root / ".pm").symlink_to("data", target_is_directory=True)
        (root / "CLAUDE.md").symlink_to("data/tasks.yaml")

        assert ensure_claudemd(root).startswith("left CLAUDE.md unchanged")
        assert (root / "data" / "tasks.yaml").read_text(encoding="utf-8") == "tasks: []\n"

    def test_a_crlf_rule_file_that_is_current_is_left_alone(self, tmp_path):
        from pmlens.rules import _render_template, inject_pm_rules

        root = _repo(tmp_path)
        text = "# Team\n\n" + _render_template() + "\n"
        (root / "AGENTS.md").write_bytes(text.replace("\n", "\r\n").encode("utf-8"))
        before = (root / "AGENTS.md").read_bytes()

        summary = inject_pm_rules(root, target="codex")

        assert summary.results[0].status == "skipped"
        assert (root / "AGENTS.md").read_bytes() == before
        assert not [p for p in root.iterdir() if ".bak." in p.name]

    def test_a_backup_keeps_the_originals_permissions_and_times(self, tmp_path):
        from pmlens.rules import inject_pm_rules

        root = _repo(tmp_path)
        claude = root / "CLAUDE.md"
        claude.write_text("# Private\n", encoding="utf-8")
        claude.chmod(0o640)
        os.utime(claude, (1_700_000_000, 1_700_000_000))

        summary = inject_pm_rules(root, target="claude-code")

        backup = summary.results[0].backup_path
        assert stat.S_IMODE(backup.stat().st_mode) == 0o640
        assert int(backup.stat().st_mtime) == 1_700_000_000
        assert stat.S_IMODE(claude.stat().st_mode) == 0o640

    def test_a_directory_without_read_permission_still_takes_writes(self, tmp_path):
        pm = _project(tmp_path)
        (pm / "daily").chmod(0o300)
        entry = DailyLogEntry(time="10:00", category=LogCategory.NOTE, entry="x")
        try:
            add_daily_log(pm, entry, dt.date(2026, 10, 20))
        finally:
            (pm / "daily").chmod(0o755)
        assert (pm / "daily" / "2026-10-20.yaml").is_file()

    def test_sqlite_uris_have_an_empty_authority(self):
        from pathlib import PurePosixPath, PureWindowsPath

        from pmlens.safe_sqlite import _file_uri

        assert _file_uri(PurePosixPath("/tmp/a b/x.db")) == "file:///tmp/a%20b/x.db"
        assert _file_uri(PureWindowsPath(r"C:\p\x.db")) == "file:///C:/p/x.db"
        assert (
            _file_uri(PureWindowsPath(r"\\server\share\p\x.db")) == "file:////server/share/p/x.db"
        )

    def test_pm_status_reports_a_linked_sidecar(self, tmp_path):
        from pmlens.draft_store import DraftStore

        pm = _project(tmp_path)
        DraftStore(pm / "drafts.db").close()
        for sidecar in ("drafts.db-wal", "drafts.db-shm"):
            (pm / sidecar).unlink(missing_ok=True)
        victim = _outside(tmp_path) / "victim"
        victim.write_text("precious", encoding="utf-8")
        (pm / "drafts.db-wal").symlink_to(victim)

        status = srv.pm_status(project_path=str(pm.parent))

        codes = {w["code"]: w for w in status["warnings"]}
        assert "draft_store_refused" in codes
        assert "drafts.db-wal" in codes["symlinks_in_pm_refused"]["message"]
        assert status["diagnostics"]["x_drafts_pending"] is None
        assert victim.read_text(encoding="utf-8") == "precious"

    def test_the_home_registry_keeps_a_linked_locks_directory(self, tmp_path):
        from pmlens.storage import load_registry, register_project

        registry_dir = tmp_path / "home_pm"
        registry_dir.mkdir()
        elsewhere = tmp_path / "tmpfs_locks"
        elsewhere.mkdir()
        (registry_dir / ".locks").symlink_to(elsewhere, target_is_directory=True)
        project = tmp_path / "p"
        project.mkdir()

        register_project(project, "p", registry_dir)

        assert [e.name for e in load_registry(registry_dir).projects] == ["p"]

    def test_locks_swapped_for_a_link_while_waiting_are_caught(self, tmp_path, monkeypatch):
        import pmlens.storage as storage

        pm = _project(tmp_path)
        outside = _outside(tmp_path)
        add_task(pm, _task(1))  # .locks exists as a real directory
        real_acquire = storage.FileLock.acquire

        def swap_then_acquire(self, *args, **kwargs):
            (pm / ".locks").rename(pm / ".locks-moved")
            (pm / ".locks").symlink_to(outside, target_is_directory=True)
            return real_acquire(self, *args, **kwargs)

        monkeypatch.setattr(storage.FileLock, "acquire", swap_then_acquire)
        with pytest.raises(LedgerWriteRefused, match=r"\.pm/\.locks"):
            add_task(pm, _task(2))
        assert [t.id for t in load_tasks(pm)] == ["T-001"]

    def test_context_inject_skips_a_linked_memory_db(self, tmp_path):
        import subprocess
        import sys

        pm = _project(tmp_path)
        other = _outside(tmp_path) / "other.db"
        before = _sqlite_file(other)
        (pm / "memory.db").symlink_to(other)

        run = subprocess.run(
            [sys.executable, "-m", "pmlens", "context-inject"],
            capture_output=True,
            text=True,
            timeout=60,
            cwd=pm.parent,
        )

        assert run.returncode == 0, run.stderr
        assert "Traceback" not in run.stderr
        assert other.read_bytes() == before

    def test_rule_files_work_without_dir_fd(self, tmp_path, monkeypatch):
        # Windows: os.open takes no dir_fd; the lstat fallbacks must still
        # back up and write, and write bytes as given (no CR CR LF).
        import pmlens.rules as rules
        import pmlens.utils as utils
        from pmlens.rules import inject_pm_rules

        monkeypatch.setattr(utils, "_no_dir_fd", lambda: True)
        monkeypatch.setattr(rules, "_no_dir_fd", lambda: True)
        root = _repo(tmp_path)
        (root / "CLAUDE.md").write_bytes(b"# Team\r\n")

        summary = inject_pm_rules(root, target="claude-code")

        assert summary.results[0].status == "appended"
        assert summary.results[0].backup_path.read_bytes() == b"# Team\r\n"
        written = (root / "CLAUDE.md").read_bytes()
        assert written.startswith(b"# Team\n") and b"\r" not in written


class TestClosingRoundFixes:
    @pytest.mark.parametrize("link", ["data/decision_lineage/ADR-001.yaml", "data/a/b/c.md"])
    def test_a_link_through_missing_directories_into_a_pm_alias_is_refused(self, tmp_path, link):
        from pmlens.rules import inject_pm_rules

        root = _repo(tmp_path)
        (root / "data").mkdir()
        (root / "data" / "tasks.yaml").write_text("tasks: []\n", encoding="utf-8")
        (root / ".pm").symlink_to("data", target_is_directory=True)
        (root / "CLAUDE.md").symlink_to(link)

        summary = inject_pm_rules(root, target="claude-code")

        assert summary.results[0].status == "failed"
        assert sorted(p.name for p in (root / "data").iterdir()) == ["tasks.yaml"]

    def test_backups_work_without_descriptor_utime(self, tmp_path, monkeypatch):
        # Windows CPython has neither fchmod nor utime on a descriptor.
        import pmlens.rules as rules
        import pmlens.utils as utils
        from pmlens.rules import inject_pm_rules

        monkeypatch.setattr(utils, "_no_dir_fd", lambda: True)
        monkeypatch.setattr(rules, "_no_dir_fd", lambda: True)
        monkeypatch.setattr(os, "supports_fd", set())
        monkeypatch.delattr(os, "fchmod")
        root = _repo(tmp_path)
        claude = root / "CLAUDE.md"
        claude.write_text("# Team\n", encoding="utf-8")
        claude.chmod(0o640)
        os.utime(claude, (1_700_000_000, 1_700_000_000))

        summary = inject_pm_rules(root, target="claude-code")

        assert summary.results[0].status == "appended"
        backup = summary.results[0].backup_path
        assert stat.S_IMODE(backup.stat().st_mode) == 0o640
        assert int(backup.stat().st_mtime) == 1_700_000_000

    def test_a_search_only_pm_directory_still_takes_writes(self, tmp_path):
        pm = _project(tmp_path)
        add_task(pm, _task(1))
        pm.chmod(0o300)
        try:
            add_task(pm, _task(2))
        finally:
            pm.chmod(0o755)
        assert [t.id for t in load_tasks(pm)] == ["T-001", "T-002"]

    def test_files_get_the_platforms_newlines_as_before(self, tmp_path, monkeypatch):
        from pmlens.rules import ensure_claudemd

        monkeypatch.setattr(os, "linesep", "\r\n")
        pm = _project(tmp_path)
        add_task(pm, _task(1))
        assert b"\r\n" in (pm / "tasks.yaml").read_bytes()
        assert [t.id for t in load_tasks(pm)] == ["T-001"]
        root = _repo(tmp_path)
        ensure_claudemd(root)
        data = (root / "CLAUDE.md").read_bytes()
        assert b"\r\n" in data and b"\r\r" not in data
