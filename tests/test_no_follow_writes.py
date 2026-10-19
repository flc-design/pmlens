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
