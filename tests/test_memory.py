"""Tests for memory.py — SQLite MemoryStore + FTS5 search."""

from __future__ import annotations

import multiprocessing as mp
import time
from pathlib import Path

import pytest

from pm_server.memory import (
    _BUSY_TIMEOUT_MS,
    MemoryStore,
    _apply_pragmas,
    _connect_readonly,
    _sanitize_fts_query,
    _str_to_tags,
    _tags_to_str,
)
from pm_server.models import Memory, MemoryType, SessionSummary

# ─── Tag conversion helpers ────────────────────────────


class TestTagConversion:
    def test_tags_to_str(self):
        assert _tags_to_str(["auth", "api"]) == "auth,api"

    def test_tags_to_str_empty(self):
        assert _tags_to_str([]) == ""

    def test_str_to_tags(self):
        assert _str_to_tags("auth,api") == ["auth", "api"]

    def test_str_to_tags_with_spaces(self):
        assert _str_to_tags(" auth , api ") == ["auth", "api"]

    def test_str_to_tags_empty(self):
        assert _str_to_tags("") == []

    def test_str_to_tags_none(self):
        assert _str_to_tags("") == []

    def test_roundtrip(self):
        tags = ["memory", "sqlite", "fts5"]
        assert _str_to_tags(_tags_to_str(tags)) == tags


# ─── FTS5 query sanitization ─────────────────────────────


class TestSanitizeFtsQuery:
    def test_plain_words_unchanged(self):
        assert _sanitize_fts_query("memory search") == "memory search"

    def test_hyphenated_word_quoted(self):
        assert _sanitize_fts_query("pm-server") == '"pm-server"'

    def test_colon_word_quoted(self):
        assert _sanitize_fts_query("col:value") == '"col:value"'

    def test_already_quoted_preserved(self):
        assert _sanitize_fts_query('"exact phrase"') == '"exact phrase"'

    def test_mixed_tokens(self):
        result = _sanitize_fts_query('memory "exact phrase" pm-server')
        assert result == 'memory "exact phrase" "pm-server"'

    def test_empty_query(self):
        assert _sanitize_fts_query("") == ""

    def test_multiple_hyphens(self):
        assert _sanitize_fts_query("a-b-c") == '"a-b-c"'


# ─── MemoryStore initialization ────────────────────────


class TestMemoryStoreInit:
    def test_creates_db_file(self, tmp_path: Path):
        db_path = tmp_path / "subdir" / "memory.db"
        store = MemoryStore(db_path)
        assert db_path.exists()
        store.close()

    def test_schema_is_idempotent(self, memory_store: MemoryStore):
        # Calling _ensure_schema again should not raise
        memory_store._ensure_schema()

    def test_tables_exist(self, memory_store: MemoryStore):
        cur = memory_store._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
        tables = {row["name"] for row in cur.fetchall()}
        assert "memories" in tables
        assert "session_summaries" in tables
        assert "memories_fts" in tables

    def test_per_project_db_user_version_is_one(self, memory_store: MemoryStore):
        version = memory_store._conn.execute("PRAGMA user_version").fetchone()[0]
        assert version == 1

    def test_global_db_user_version_is_one_after_sync(
        self, memory_store: MemoryStore, tmp_path: Path
    ):
        import sqlite3

        mem = Memory(
            session_id="sess-schema-001",
            type=MemoryType.OBSERVATION,
            content="trigger global sync",
            project="testproj",
        )
        memory_store.save(mem)

        global_path = tmp_path / "global_pm" / "memory.db"
        assert global_path.exists()
        conn = sqlite3.connect(str(global_path))
        try:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
        finally:
            conn.close()
        assert version == 1

    def test_default_global_db_path_is_none(self, tmp_path: Path):
        # PMSERV-080 I-1: the default no longer touches Path.home() at import
        # time. Callers (server.py) compute the global path explicitly.
        db_path = tmp_path / "lonely.db"
        store = MemoryStore(db_path)
        try:
            assert store.global_db_path is None
            assert store.readonly is False
        finally:
            store.close()


# ─── PMSERV-080 R5: Read-only Lens connection ──────────


class TestReadOnlyConnection:
    def _seed(self, db_path: Path) -> None:
        store = MemoryStore(db_path)
        store.save(
            Memory(
                session_id="sess-lens-001",
                type=MemoryType.OBSERVATION,
                content="lens-mode payload",
                project="lensproj",
                tags=["lens", "ro"],
            )
        )
        store.close()

    def test_connect_readonly_returns_row_factory(self, tmp_path: Path):
        db_path = tmp_path / "ro.db"
        self._seed(db_path)
        conn = _connect_readonly(db_path)
        try:
            row = conn.execute("SELECT content FROM memories LIMIT 1").fetchone()
            assert row["content"] == "lens-mode payload"
        finally:
            conn.close()

    def test_connect_readonly_rejects_writes(self, tmp_path: Path):
        import sqlite3

        db_path = tmp_path / "ro.db"
        self._seed(db_path)
        conn = _connect_readonly(db_path)
        try:
            with pytest.raises(sqlite3.OperationalError):
                conn.execute(
                    "INSERT INTO memories (session_id, type, content, project)"
                    " VALUES ('x', 'observation', 'nope', 'x')"
                )
        finally:
            conn.close()

    def test_readonly_store_does_not_create_wal_shm(self, tmp_path: Path):
        db_path = tmp_path / "ro.db"
        self._seed(db_path)
        # Erase any sidecars left from the seed step so we observe only what
        # the readonly store creates.
        for suffix in ("-wal", "-shm"):
            sidecar = db_path.with_name(db_path.name + suffix)
            if sidecar.exists():
                sidecar.unlink()

        store = MemoryStore(db_path, readonly=True)
        try:
            mems = store.search("lens-mode")
            assert len(mems) == 1
            assert mems[0].content == "lens-mode payload"
        finally:
            store.close()

        assert not db_path.with_name(db_path.name + "-wal").exists()
        assert not db_path.with_name(db_path.name + "-shm").exists()

    def test_readonly_store_save_raises(self, tmp_path: Path):
        import sqlite3

        db_path = tmp_path / "ro.db"
        self._seed(db_path)
        store = MemoryStore(db_path, readonly=True)
        try:
            with pytest.raises(sqlite3.OperationalError):
                store.save(
                    Memory(
                        session_id="sess-ro",
                        type=MemoryType.OBSERVATION,
                        content="must not persist",
                        project="lensproj",
                    )
                )
        finally:
            store.close()

    def test_readonly_store_ignores_global_db_path(self, tmp_path: Path):
        # readonly=True must force global sync off — Lens host cannot write
        # to the cross-project index either.
        db_path = tmp_path / "ro.db"
        self._seed(db_path)
        bogus_global = tmp_path / "should_not_appear" / "memory.db"
        store = MemoryStore(db_path, global_db_path=bogus_global, readonly=True)
        try:
            assert store.global_db_path is None
            assert store.readonly is True
        finally:
            store.close()
        assert not bogus_global.exists()


# ─── Memory CRUD ───────────────────────────────────────


class TestMemorySave:
    def test_save_returns_id(self, memory_store: MemoryStore):
        mem = Memory(
            session_id="sess-test-001",
            content="Test memory",
            project="testproj",
        )
        mem_id = memory_store.save(mem)
        assert isinstance(mem_id, int)
        assert mem_id >= 1

    def test_save_multiple(self, memory_store: MemoryStore):
        ids = []
        for i in range(3):
            mem = Memory(
                session_id="sess-test-001",
                content=f"Memory {i}",
                project="testproj",
            )
            ids.append(memory_store.save(mem))
        assert len(set(ids)) == 3  # all unique

    def test_save_with_task_id(self, memory_store: MemoryStore):
        mem = Memory(
            session_id="sess-test-001",
            content="Auth implementation notes",
            task_id="TEST-001",
            project="testproj",
        )
        mem_id = memory_store.save(mem)
        results = memory_store.get_by_task("TEST-001")
        assert len(results) == 1
        assert results[0].id == mem_id
        assert results[0].task_id == "TEST-001"

    def test_save_with_decision_id(self, memory_store: MemoryStore):
        mem = Memory(
            session_id="sess-test-001",
            content="JWT decision rationale",
            decision_id="ADR-001",
            project="testproj",
        )
        memory_store.save(mem)
        results = memory_store.get_by_decision("ADR-001")
        assert len(results) == 1
        assert results[0].decision_id == "ADR-001"

    def test_save_with_tags(self, memory_store: MemoryStore):
        mem = Memory(
            session_id="sess-test-001",
            content="Tagged memory",
            tags=["auth", "api"],
            project="testproj",
        )
        memory_store.save(mem)
        recent = memory_store.get_recent(limit=1)
        assert recent[0].tags == ["auth", "api"]

    def test_save_all_types(self, memory_store: MemoryStore):
        for mtype in MemoryType:
            mem = Memory(
                session_id="sess-test-001",
                type=mtype,
                content=f"Memory of type {mtype.value}",
                project="testproj",
            )
            memory_store.save(mem)
        recent = memory_store.get_recent(limit=3)
        types = {m.type for m in recent}
        assert types == {MemoryType.OBSERVATION, MemoryType.INSIGHT, MemoryType.LESSON}


# ─── FTS5 Search ───────────────────────────────────────


class TestFTS5Search:
    @pytest.fixture(autouse=True)
    def _seed_memories(self, memory_store: MemoryStore):
        memories = [
            ("User authentication API implemented with JWT tokens", "auth,jwt"),
            ("Database migration script for user table", "database,migration"),
            ("Refactored error handling to use custom exceptions", "refactor,error"),
            ("Performance optimization for search queries", "performance,search"),
            ("Added unit tests for storage module", "test,storage"),
        ]
        for content, tags in memories:
            mem = Memory(
                session_id="sess-seed",
                content=content,
                tags=tags.split(","),
                project="testproj",
            )
            memory_store.save(mem)

    def test_search_single_word(self, memory_store: MemoryStore):
        results = memory_store.search("authentication")
        assert len(results) >= 1
        assert any("authentication" in r.content.lower() for r in results)

    def test_search_multiple_words(self, memory_store: MemoryStore):
        results = memory_store.search("database migration")
        assert len(results) >= 1

    def test_search_no_results(self, memory_store: MemoryStore):
        results = memory_store.search("nonexistent_term_xyz")
        assert len(results) == 0

    def test_search_with_type_filter(self, memory_store: MemoryStore):
        # Save an insight
        mem = Memory(
            session_id="sess-seed",
            type=MemoryType.INSIGHT,
            content="JWT tokens need short expiry for security",
            project="testproj",
        )
        memory_store.save(mem)

        results = memory_store.search("JWT", type="insight")
        assert all(r.type == MemoryType.INSIGHT for r in results)

    def test_search_limit(self, memory_store: MemoryStore):
        results = memory_store.search("user", limit=1)
        assert len(results) <= 1

    def test_search_japanese(self, memory_store: MemoryStore):
        """FTS5 unicode61 tokenizes CJK characters individually."""
        mem = Memory(
            session_id="sess-jp",
            content="ユーザー認証APIの実装を完了した",
            tags=["認証", "API"],
            project="testproj",
        )
        memory_store.save(mem)
        results = memory_store.search("認証")
        assert len(results) >= 1
        assert any("認証" in r.content for r in results)

    def test_search_hyphenated_term(self, memory_store: MemoryStore):
        """Hyphenated terms must not crash FTS5 with column-filter error."""
        mem = Memory(
            session_id="sess-hyp",
            content="Deployed pm-server to staging environment",
            tags=["deploy"],
            project="testproj",
        )
        memory_store.save(mem)
        results = memory_store.search("pm-server")
        assert len(results) >= 1
        assert any("pm-server" in r.content for r in results)

    def test_search_hyphenated_no_match(self, memory_store: MemoryStore):
        """Hyphenated term that doesn't match should return empty, not error."""
        results = memory_store.search("no-such-term")
        assert len(results) == 0

    def test_search_japanese_tags(self, memory_store: MemoryStore):
        mem = Memory(
            session_id="sess-jp",
            content="リファクタリングを実施",
            tags=["リファクタ", "改善"],
            project="testproj",
        )
        memory_store.save(mem)
        results = memory_store.search("リファクタ")
        assert len(results) >= 1


# ─── get_by_task / get_by_decision ─────────────────────


class TestGetByAssociation:
    def test_get_by_task_multiple(self, memory_store: MemoryStore):
        for i in range(3):
            mem = Memory(
                session_id="sess-test",
                content=f"Task note {i}",
                task_id="TEST-001",
                project="testproj",
            )
            memory_store.save(mem)
        results = memory_store.get_by_task("TEST-001")
        assert len(results) == 3

    def test_get_by_task_empty(self, memory_store: MemoryStore):
        results = memory_store.get_by_task("NONEXIST-999")
        assert results == []

    def test_get_by_decision_empty(self, memory_store: MemoryStore):
        results = memory_store.get_by_decision("ADR-999")
        assert results == []


# ─── get_recent ────────────────────────────────────────


class TestGetRecent:
    def test_get_recent_order(self, memory_store: MemoryStore):
        for i in range(5):
            mem = Memory(
                session_id="sess-test",
                content=f"Memory {i}",
                project="testproj",
            )
            memory_store.save(mem)
        recent = memory_store.get_recent(limit=5)
        # Most recent first (highest ID = most recent by created_at default)
        ids = [m.id for m in recent]
        assert ids == sorted(ids, reverse=True)

    def test_get_recent_limit(self, memory_store: MemoryStore):
        for i in range(10):
            mem = Memory(
                session_id="sess-test",
                content=f"Memory {i}",
                project="testproj",
            )
            memory_store.save(mem)
        recent = memory_store.get_recent(limit=3)
        assert len(recent) == 3

    def test_get_recent_empty_db(self, memory_store: MemoryStore):
        recent = memory_store.get_recent()
        assert recent == []


# ─── Session Summaries ─────────────────────────────────


class TestSessionSummaries:
    def test_save_and_get_latest(self, memory_store: MemoryStore):
        summary = SessionSummary(
            session_id="sess-001",
            summary="Implemented auth module",
            goals="Complete JWT auth",
            tasks_done=["TEST-001"],
            decisions=["ADR-001"],
            pending=["Review needed"],
            project="testproj",
        )
        sid = memory_store.save_session_summary(summary)
        assert isinstance(sid, int)

        latest = memory_store.get_latest_summary()
        assert latest is not None
        assert latest.session_id == "sess-001"
        assert latest.summary == "Implemented auth module"
        assert latest.tasks_done == ["TEST-001"]
        assert latest.decisions == ["ADR-001"]
        assert latest.pending == ["Review needed"]

    def test_get_latest_returns_most_recent(self, memory_store: MemoryStore):
        for i in range(3):
            summary = SessionSummary(
                session_id=f"sess-{i:03d}",
                summary=f"Session {i}",
                project="testproj",
            )
            memory_store.save_session_summary(summary)
            # Small delay to ensure different created_at
            time.sleep(0.01)

        latest = memory_store.get_latest_summary()
        assert latest is not None
        assert latest.session_id == "sess-002"

    def test_get_latest_empty_db(self, memory_store: MemoryStore):
        assert memory_store.get_latest_summary() is None

    def test_list_summaries(self, memory_store: MemoryStore):
        for i in range(5):
            summary = SessionSummary(
                session_id=f"sess-{i:03d}",
                summary=f"Session {i}",
                project="testproj",
            )
            memory_store.save_session_summary(summary)
        summaries = memory_store.list_summaries(limit=3)
        assert len(summaries) == 3

    def test_list_summaries_empty_db(self, memory_store: MemoryStore):
        assert memory_store.list_summaries() == []

    def test_save_replaces_existing_session(self, memory_store: MemoryStore):
        """INSERT OR REPLACE should update if session_id exists."""
        s1 = SessionSummary(
            session_id="sess-same",
            summary="First version",
            project="testproj",
        )
        memory_store.save_session_summary(s1)

        s2 = SessionSummary(
            session_id="sess-same",
            summary="Updated version",
            project="testproj",
        )
        memory_store.save_session_summary(s2)

        summaries = memory_store.list_summaries()
        assert len(summaries) == 1
        assert summaries[0].summary == "Updated version"

    def test_summary_with_empty_lists(self, memory_store: MemoryStore):
        summary = SessionSummary(
            session_id="sess-empty",
            summary="Minimal session",
            project="testproj",
        )
        memory_store.save_session_summary(summary)
        latest = memory_store.get_latest_summary()
        assert latest is not None
        assert latest.tasks_done == []
        assert latest.decisions == []
        assert latest.pending == []


class TestBranchAwareSummaries:
    """PMSERV-124 / ADR-028: branch-scoped session continuity."""

    def test_branch_round_trip(self, memory_store: MemoryStore):
        memory_store.save_session_summary(
            SessionSummary(
                session_id="sess-paper", summary="paper work", project="p", branch="paper"
            )
        )
        summary, matched = memory_store.get_latest_summary_by_branch("paper")
        assert matched is True
        assert summary is not None
        assert summary.session_id == "sess-paper"
        assert summary.branch == "paper"

    def test_branch_scopes_to_its_own_line(self, memory_store: MemoryStore):
        memory_store.save_session_summary(
            SessionSummary(session_id="sess-main", summary="main", project="p", branch="main")
        )
        memory_store.save_session_summary(
            SessionSummary(session_id="sess-edu", summary="edu", project="p", branch="edu")
        )
        main, _ = memory_store.get_latest_summary_by_branch("main")
        edu, _ = memory_store.get_latest_summary_by_branch("edu")
        assert main.session_id == "sess-main"
        assert edu.session_id == "sess-edu"

    def test_unknown_branch_falls_back_to_overall_latest(self, memory_store: MemoryStore):
        memory_store.save_session_summary(
            SessionSummary(session_id="sess-main", summary="main", project="p", branch="main")
        )
        summary, matched = memory_store.get_latest_summary_by_branch("does-not-exist")
        assert matched is False
        assert summary is not None
        assert summary.session_id == "sess-main"

    def test_empty_db_branch_query_returns_none_unmatched(self, memory_store: MemoryStore):
        summary, matched = memory_store.get_latest_summary_by_branch("main")
        assert summary is None
        assert matched is False

    def test_latest_on_branch_is_most_recently_worked_not_highest_id(
        self, memory_store: MemoryStore
    ):
        """ORDER BY updated_at DESC: re-touching an older row makes it the
        branch's latest even though a higher-id row exists (UPSERT keeps id)."""
        memory_store.save_session_summary(
            SessionSummary(session_id="sess-old", summary="old", project="p", branch="main")
        )
        memory_store.save_session_summary(
            SessionSummary(session_id="sess-new", summary="new", project="p", branch="main")
        )
        # Force sess-old (lower id) to be the most recently *worked* row.
        memory_store._conn.execute(
            "UPDATE session_summaries SET updated_at = datetime('now', '+1 hour')"
            " WHERE session_id = ?",
            ("sess-old",),
        )
        memory_store._conn.commit()

        summary, matched = memory_store.get_latest_summary_by_branch("main")
        assert matched is True
        assert summary.session_id == "sess-old"
        # The plain (id-ordered) latest still returns the highest-id row.
        assert memory_store.get_latest_summary().session_id == "sess-new"

    def test_nondestructive_branch_on_transient_miss(self, memory_store: MemoryStore):
        """A re-save with empty branch must NOT clobber a recorded branch
        (COALESCE/NULLIF guard); a real new branch DOES update it."""
        memory_store.save_session_summary(
            SessionSummary(session_id="sess-x", summary="v1", project="p", branch="paper")
        )
        # Transient detection miss (branch="") on re-save → branch preserved.
        memory_store.save_session_summary(
            SessionSummary(session_id="sess-x", summary="v2", project="p", branch="")
        )
        kept, matched = memory_store.get_latest_summary_by_branch("paper")
        assert matched is True
        assert kept.session_id == "sess-x"
        assert kept.summary == "v2"
        # An actual checkout to a new branch DOES move the session.
        memory_store.save_session_summary(
            SessionSummary(session_id="sess-x", summary="v3", project="p", branch="edu")
        )
        moved, moved_matched = memory_store.get_latest_summary_by_branch("edu")
        assert moved_matched is True
        assert moved.session_id == "sess-x"

    def test_list_distinct_branches(self, memory_store: MemoryStore):
        for sid, br in [("a", "main"), ("b", "feat/x"), ("c", "main"), ("d", "")]:
            memory_store.save_session_summary(
                SessionSummary(session_id=sid, summary="s", project="p", branch=br)
            )
        branches = set(memory_store.list_distinct_branches())
        # Distinct, non-empty only ("" excluded).
        assert branches == {"main", "feat/x"}

    def test_latest_in_branches_picks_most_recent_across_set(self, memory_store: MemoryStore):
        # A logical line spanning several branches (PMSERV-125 resolution).
        for sid, br in [
            ("s1", "feat/p3-a"),
            ("s2", "feat/p3-b"),
            ("s3", "research/wave-1"),
        ]:
            memory_store.save_session_summary(
                SessionSummary(session_id=sid, summary="s", project="p", branch=br)
            )
        # Force s2 to be the most recently worked across the set.
        memory_store._conn.execute(
            "UPDATE session_summaries SET updated_at = datetime('now', '+1 hour')"
            " WHERE session_id = ?",
            ("s2",),
        )
        memory_store._conn.commit()
        summary, matched = memory_store.get_latest_summary_in_branches(
            ["feat/p3-a", "feat/p3-b", "research/wave-1"]
        )
        assert matched is True
        assert summary.session_id == "s2"

    def test_latest_in_branches_empty_falls_back(self, memory_store: MemoryStore):
        memory_store.save_session_summary(
            SessionSummary(session_id="s1", summary="s", project="p", branch="main")
        )
        summary, matched = memory_store.get_latest_summary_in_branches([])
        assert matched is False
        assert summary.session_id == "s1"

    def test_latest_in_branches_no_match_falls_back(self, memory_store: MemoryStore):
        memory_store.save_session_summary(
            SessionSummary(session_id="s1", summary="s", project="p", branch="main")
        )
        summary, matched = memory_store.get_latest_summary_in_branches(["ghost"])
        assert matched is False
        assert summary.session_id == "s1"

    def test_branch_queries_tolerate_missing_column_readonly(self, tmp_path):
        """Old DB (no branch column) opened read-only under PM_LENS must not
        raise OperationalError from the branch-scoped queries — they degrade to
        the overall-latest fallback (regression guard for the RO Lens path)."""
        import sqlite3

        from pm_server.memory import MemoryStore

        db_path = tmp_path / "legacy.db"
        conn = sqlite3.connect(str(db_path))
        conn.executescript(
            """
            CREATE TABLE memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
                type TEXT NOT NULL, content TEXT NOT NULL, task_id TEXT,
                decision_id TEXT, tags TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now')), project TEXT NOT NULL
            );
            CREATE TABLE session_summaries (
                id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL UNIQUE,
                summary TEXT NOT NULL, goals TEXT, tasks_done TEXT, decisions TEXT,
                pending TEXT, created_at TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now')), project TEXT NOT NULL
            );
            INSERT INTO session_summaries (session_id, summary, goals, tasks_done,
                decisions, pending, project)
            VALUES ('sess-legacy', 'old work', '', '[]', '[]', '[]', 'p');
            """
        )
        conn.commit()
        conn.close()

        # readonly=True => _ensure_schema (and the branch migration) never runs.
        store = MemoryStore(db_path, readonly=True)
        try:
            assert store._has_branch_col is False
            # None of these may raise; all degrade gracefully.
            assert store.list_distinct_branches() == []
            s, matched = store.get_latest_summary_by_branch("main")
            assert matched is False
            assert s is not None and s.session_id == "sess-legacy"
            s2, m2 = store.get_latest_summary_in_branches(["main"])
            assert m2 is False
            assert s2.session_id == "sess-legacy"
            # Branch-scoped ambiguity scan must also be safe on an old DB.
            assert store.list_summaries_within(branches=["main"]) == []
        finally:
            store.close()


# ─── Server tool integration ───────────────────────────


class TestServerToolIntegration:
    """Test pm_remember / pm_recall / pm_session_summary via server functions."""

    @pytest.fixture(autouse=True)
    def _setup_project(self, tmp_project: Path, monkeypatch):
        """Set up a project with project.yaml for server tool calls."""
        from pm_server.models import Project
        from pm_server.storage import _save_project

        pm_path = tmp_project / ".pm"
        project = Project(name="testproj", display_name="Test")
        _save_project(pm_path, project)
        monkeypatch.chdir(tmp_project)

        # Clear cached memory stores between tests
        import pm_server.server

        pm_server.server._memory_stores.clear()

    def test_remember_and_recall(self):
        from pm_server.server import pm_recall, pm_remember

        result = pm_remember(content="JWT tokens expire in 15 minutes", type="insight")
        assert result["status"] == "saved"
        assert "memory_id" in result

        recall_result = pm_recall(query="JWT")
        assert len(recall_result["results"]) >= 1
        assert any("JWT" in r["content"] for r in recall_result["results"])

    def test_recall_default_no_args(self):
        from pm_server.server import pm_recall, pm_remember

        pm_remember(content="Some observation")
        result = pm_recall()
        assert "last_session" in result
        assert "recent_memories" in result
        assert len(result["recent_memories"]) >= 1

    def test_recall_default_with_type_filter(self):
        from pm_server.server import pm_recall, pm_remember

        pm_remember(content="An observation", type="observation")
        pm_remember(content="A lesson learned", type="lesson")
        pm_remember(content="An insight gained", type="insight")

        result = pm_recall(type="lesson")
        assert all(m["type"] == "lesson" for m in result["recent_memories"])
        assert len(result["recent_memories"]) == 1

    def test_recall_by_task_id(self):
        from pm_server.server import pm_recall, pm_remember

        pm_remember(content="Task note", task_id="TEST-001")
        result = pm_recall(task_id="TEST-001")
        assert len(result["results"]) == 1
        assert result["results"][0]["task_id"] == "TEST-001"

    def test_recall_cross_project_requires_query(self):
        from pm_server.server import pm_recall

        result = pm_recall(cross_project=True)
        assert result["status"] == "error"

    def test_recall_cross_project_with_query(self):
        from pm_server.server import pm_recall, pm_remember

        pm_remember(content="Cross project test data")
        result = pm_recall(query="Cross project", cross_project=True)
        assert result["cross_project"] is True
        assert "results" in result

    def test_session_summary_save_get(self):
        from pm_server.server import pm_session_summary

        save_result = pm_session_summary(
            action="save",
            summary="Completed auth module implementation",
            goals="JWT auth working",
            pending="Code review,Deploy",
        )
        assert save_result["status"] == "saved"

        get_result = pm_session_summary(action="get")
        assert get_result["summary"] == "Completed auth module implementation"
        assert get_result["pending"] == ["Code review", "Deploy"]

    def test_session_summary_list(self):
        from pm_server.server import pm_session_summary

        pm_session_summary(action="save", summary="Session 1")
        result = pm_session_summary(action="list")
        assert result["count"] >= 1

    def test_session_summary_save_requires_summary(self):
        from pm_server.server import pm_session_summary

        result = pm_session_summary(action="save")
        assert result["status"] == "error"

    def test_session_summary_get_empty(self):
        from pm_server.server import pm_session_summary

        result = pm_session_summary(action="get")
        assert result["status"] == "empty"

    def test_session_summary_invalid_action(self):
        from pm_server.server import pm_session_summary

        result = pm_session_summary(action="invalid")
        assert result["status"] == "error"


# ─── SQLite concurrency pragmas (PMSERV-047) ─────────────


class TestSqlitePragmas:
    """Verify _apply_pragmas sets WAL + NORMAL synchronous + 5s busy_timeout."""

    def test_constant_matches_filelock_timeout(self):
        # PMSERV-048 filelock uses 5s; PMSERV-047 mirrors that budget.
        assert _BUSY_TIMEOUT_MS == 5000

    def test_journal_mode_is_wal(self, memory_store: MemoryStore):
        mode = memory_store._conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode == "wal"

    def test_busy_timeout_is_set(self, memory_store: MemoryStore):
        timeout = memory_store._conn.execute("PRAGMA busy_timeout").fetchone()[0]
        assert timeout == _BUSY_TIMEOUT_MS

    def test_synchronous_is_normal(self, memory_store: MemoryStore):
        # NORMAL = 1 (FULL = 2, OFF = 0). Safe under WAL per SQLite docs.
        sync = memory_store._conn.execute("PRAGMA synchronous").fetchone()[0]
        assert sync == 1

    def test_apply_pragmas_is_idempotent(self, memory_store: MemoryStore):
        # Re-applying should not raise and should leave settings unchanged.
        _apply_pragmas(memory_store._conn)
        _apply_pragmas(memory_store._conn)
        assert memory_store._conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert memory_store._conn.execute("PRAGMA busy_timeout").fetchone()[0] == _BUSY_TIMEOUT_MS

    def test_global_db_uses_wal(self, tmp_path: Path):
        """sync_to_global should set WAL on the global DB too."""
        import sqlite3

        db_path = tmp_path / "memory.db"
        global_path = tmp_path / "global" / "memory.db"
        store = MemoryStore(db_path, global_db_path=global_path)
        store.save(
            Memory(
                session_id="sess-test",
                type=MemoryType.OBSERVATION,
                content="trigger global sync",
                project="test-proj",
            )
        )
        store.close()

        # Open the global DB with a fresh connection and verify WAL persisted.
        conn = sqlite3.connect(str(global_path))
        try:
            assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        finally:
            conn.close()


# ─── Multi-process concurrency (PMSERV-047 / phase-9) ───
#
# These workers must be defined at module level so the spawn context can pickle
# them. Pattern reused from PMSERV-048's tests/test_concurrent.py.


def _worker_check_pragmas(db_path_str: str, result_path: str) -> None:
    """Open a fresh MemoryStore in a child process and report the 3 pragmas."""
    from pm_server.memory import MemoryStore

    store = MemoryStore(Path(db_path_str), global_db_path=None)
    journal = store._conn.execute("PRAGMA journal_mode").fetchone()[0]
    busy = store._conn.execute("PRAGMA busy_timeout").fetchone()[0]
    sync = store._conn.execute("PRAGMA synchronous").fetchone()[0]
    Path(result_path).write_text(f"{journal},{busy},{sync}")
    store.close()


def _worker_write_n_memories(
    db_path_str: str, project: str, prefix: str, count: int, ready_path: str
) -> None:
    """Write `count` memories rapidly, then touch the ready file."""
    from pm_server.memory import MemoryStore
    from pm_server.models import Memory, MemoryType

    store = MemoryStore(Path(db_path_str), global_db_path=None)
    for i in range(count):
        store.save(
            Memory(
                session_id=f"sess-{prefix}",
                type=MemoryType.OBSERVATION,
                content=f"{prefix}-{i:03d}",
                project=project,
            )
        )
    store.close()
    Path(ready_path).touch()


class TestSqliteWalConcurrency:
    """Validate WAL + busy_timeout under real multi-process contention.

    Uses spawn context (cross-platform safe; fork inherits import state and
    can race in unhelpful ways). Workers are module-level for pickling.
    """

    def test_wal_persists_across_processes(self, tmp_path: Path):
        """journal_mode=WAL is DB-persistent (file header) — a fresh process
        opening the same .db sees WAL without needing the application to set
        it again. Per-connection pragmas are still re-applied in __init__.
        """
        ctx = mp.get_context("spawn")
        db_path = tmp_path / "wal_persist.db"
        result_path = tmp_path / "pragma_result"

        # Seed with one MemoryStore in the parent (writes WAL into file header).
        seed = MemoryStore(db_path, global_db_path=None)
        seed.close()

        proc = ctx.Process(target=_worker_check_pragmas, args=(str(db_path), str(result_path)))
        proc.start()
        proc.join(timeout=10.0)
        assert proc.exitcode == 0, "child process failed to open WAL DB"

        journal, busy, sync = result_path.read_text().split(",")
        assert journal == "wal"
        assert int(busy) == _BUSY_TIMEOUT_MS
        assert int(sync) == 1  # NORMAL

    def test_concurrent_writers_dont_deadlock(self, tmp_path: Path):
        """Two processes writing in parallel both succeed.

        With WAL + busy_timeout=5000 they serialize at the WAL frame level but
        neither raises SQLITE_BUSY. Pre-WAL (or busy_timeout=0) the second
        writer would fail immediately on contention.
        """
        ctx = mp.get_context("spawn")
        db_path = tmp_path / "concurrent_writers.db"

        # Seed to flip WAL on the file header before children open it.
        seed = MemoryStore(db_path, global_db_path=None)
        seed.close()

        ready_a = tmp_path / "ready_a"
        ready_b = tmp_path / "ready_b"

        proc_a = ctx.Process(
            target=_worker_write_n_memories,
            args=(str(db_path), "proj-a", "writer-a", 30, str(ready_a)),
        )
        proc_b = ctx.Process(
            target=_worker_write_n_memories,
            args=(str(db_path), "proj-b", "writer-b", 30, str(ready_b)),
        )
        proc_a.start()
        proc_b.start()
        proc_a.join(timeout=15.0)
        proc_b.join(timeout=15.0)

        assert proc_a.exitcode == 0, "writer A failed (likely SQLITE_BUSY)"
        assert proc_b.exitcode == 0, "writer B failed (likely SQLITE_BUSY)"
        assert ready_a.exists() and ready_b.exists()

        # All 60 inserts must have landed.
        verify = MemoryStore(db_path, global_db_path=None)
        try:
            total = verify._conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
        finally:
            verify.close()
        assert total == 60
