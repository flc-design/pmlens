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
