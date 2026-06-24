"""Tests for context.py, global sync, and pm_memory_search."""

from __future__ import annotations

from pathlib import Path

from pmlens.memory import MemoryStore
from pmlens.models import Memory, Project, SessionSummary
from pmlens.storage import _save_project

# ─── Global sync ───────────────────────────────────────


class TestGlobalSync:
    def test_sync_creates_global_db(self, tmp_path: Path):
        global_path = tmp_path / "global" / "memory.db"
        store = MemoryStore(tmp_path / "local.db", global_db_path=global_path)

        mem = Memory(
            session_id="sess-001",
            content="Test sync",
            project="testproj",
        )
        store.save(mem)
        store.close()

        assert global_path.exists()

    def test_sync_data_searchable(self, tmp_path: Path):
        global_path = tmp_path / "global" / "memory.db"
        store = MemoryStore(tmp_path / "local.db", global_db_path=global_path)

        mem = Memory(
            session_id="sess-001",
            content="Synced authentication data",
            project="testproj",
        )
        store.save(mem)

        results = store.search_global("authentication")
        assert len(results) >= 1
        assert results[0]["project"] == "testproj"
        store.close()

    def test_sync_disabled(self, tmp_path: Path):
        store = MemoryStore(tmp_path / "local.db", global_db_path=None)

        mem = Memory(
            session_id="sess-001",
            content="No sync",
            project="testproj",
        )
        store.save(mem)

        results = store.search_global("sync")
        assert results == []
        store.close()

    def test_sync_failure_is_silent(self, tmp_path: Path):
        # Point to a read-only path that can't be created
        store = MemoryStore(
            tmp_path / "local.db",
            global_db_path=Path("/dev/null/impossible/memory.db"),
        )

        mem = Memory(
            session_id="sess-001",
            content="Should not crash",
            project="testproj",
        )
        # Should not raise
        store.save(mem)
        store.close()

    def test_search_global_nonexistent_db(self, tmp_path: Path):
        store = MemoryStore(
            tmp_path / "local.db",
            global_db_path=tmp_path / "nonexistent" / "memory.db",
        )
        results = store.search_global("anything")
        assert results == []
        store.close()

    def test_multi_project_sync(self, tmp_path: Path):
        global_path = tmp_path / "global" / "memory.db"

        # Project A
        store_a = MemoryStore(tmp_path / "a" / "memory.db", global_db_path=global_path)
        store_a.save(Memory(session_id="s1", content="Auth for project A", project="proj-a"))
        store_a.close()

        # Project B
        store_b = MemoryStore(tmp_path / "b" / "memory.db", global_db_path=global_path)
        store_b.save(Memory(session_id="s2", content="Auth for project B", project="proj-b"))

        results = store_b.search_global("Auth")
        assert len(results) == 2
        projects = {r["project"] for r in results}
        assert projects == {"proj-a", "proj-b"}
        store_b.close()


# ─── pm_memory_search server tool ──────────────────────


class TestPmMemorySearch:
    def _setup_project(self, tmp_path, monkeypatch):
        pm_path = tmp_path / ".pm"
        pm_path.mkdir(exist_ok=True)
        (pm_path / "daily").mkdir(exist_ok=True)
        project = Project(name="searchproj", display_name="Search Test")
        _save_project(pm_path, project)
        monkeypatch.chdir(tmp_path)

        import pmlens.server

        pmlens.server._memory_stores.clear()

    def test_search_by_query(self, tmp_path, monkeypatch):
        self._setup_project(tmp_path, monkeypatch)
        from pmlens.server import pm_memory_search, pm_remember

        pm_remember(content="JWT authentication flow", tags="auth,jwt")
        pm_remember(content="Database migration script", tags="database")

        result = pm_memory_search(query="authentication")
        assert len(result["results"]) >= 1

    def test_search_with_tag_filter(self, tmp_path, monkeypatch):
        self._setup_project(tmp_path, monkeypatch)
        from pmlens.server import pm_memory_search, pm_remember

        pm_remember(content="JWT token handling", tags="auth,jwt")
        pm_remember(content="Session cookie approach", tags="auth,cookie")

        result = pm_memory_search(query="auth", tags="jwt")
        assert all("jwt" in r["tags"] for r in result["results"])

    def test_search_with_task_filter(self, tmp_path, monkeypatch):
        self._setup_project(tmp_path, monkeypatch)
        from pmlens.server import pm_memory_search, pm_remember

        pm_remember(content="Task-linked memory", task_id="TEST-001")
        pm_remember(content="Other memory")

        result = pm_memory_search(query="memory", task_id="TEST-001")
        assert all(r["task_id"] == "TEST-001" for r in result["results"])


# ─── context.py inject_context ─────────────────────────


class TestInjectContext:
    def test_inject_with_no_project(self, capsys):
        from pmlens.context import inject_context

        # No .pm/ directory — should silently do nothing
        inject_context(project_path=Path("/tmp/nonexistent"))
        captured = capsys.readouterr()
        assert captured.out == ""

    def test_inject_with_no_memories(self, tmp_path, capsys):
        from pmlens.context import inject_context

        pm_path = tmp_path / ".pm"
        pm_path.mkdir()
        (pm_path / "daily").mkdir()
        project = Project(name="emptyproj")
        _save_project(pm_path, project)

        # No memory.db yet — should silently do nothing
        inject_context(project_path=tmp_path)
        captured = capsys.readouterr()
        assert captured.out == ""

    def test_inject_with_memories(self, tmp_path, capsys):
        from pmlens.context import inject_context

        pm_path = tmp_path / ".pm"
        pm_path.mkdir()
        (pm_path / "daily").mkdir()
        project = Project(name="ctxproj")
        _save_project(pm_path, project)

        # Create memories
        store = MemoryStore(pm_path / "memory.db", global_db_path=None)
        store.save(
            Memory(
                session_id="sess-prev",
                content="Previous session work",
                project="ctxproj",
            )
        )
        store.save_session_summary(
            SessionSummary(
                session_id="sess-prev",
                summary="Completed auth module",
                project="ctxproj",
            )
        )
        store.close()

        inject_context(project_path=tmp_path)
        captured = capsys.readouterr()
        assert "前回セッションからの引き継ぎ" in captured.out
        assert "Completed auth module" in captured.out
