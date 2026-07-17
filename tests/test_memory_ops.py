"""Tests for pm_memory_stats and pm_memory_cleanup tools."""

from __future__ import annotations

from pmlens.memory import MemoryStore
from pmlens.models import Memory, MemoryType

# ─── MemoryStore.get_stats ────────────────────────────


class TestMemoryStats:
    def test_stats_empty_db(self, memory_store: MemoryStore):
        stats = memory_store.get_stats()
        assert stats["total_memories"] == 0
        assert stats["by_type"] == {}
        assert stats["sessions"] == 0
        assert stats["session_summaries"] == 0
        assert stats["oldest"] is None
        assert stats["newest"] is None
        assert stats["db_size_bytes"] > 0  # DB file exists even when empty

    def test_stats_with_memories(self, memory_store: MemoryStore):
        for i in range(3):
            memory_store.save(
                Memory(
                    session_id="sess-001",
                    type=MemoryType.OBSERVATION,
                    content=f"Obs {i}",
                    project="testproj",
                )
            )
        memory_store.save(
            Memory(
                session_id="sess-001",
                type=MemoryType.INSIGHT,
                content="Insight 1",
                project="testproj",
            )
        )
        memory_store.save(
            Memory(
                session_id="sess-002",
                type=MemoryType.LESSON,
                content="Lesson 1",
                project="testproj",
            )
        )

        stats = memory_store.get_stats()
        assert stats["total_memories"] == 5
        assert stats["by_type"]["observation"] == 3
        assert stats["by_type"]["insight"] == 1
        assert stats["by_type"]["lesson"] == 1
        assert stats["sessions"] == 2
        assert stats["oldest"] is not None
        assert stats["newest"] is not None


# ─── MemoryStore.cleanup ──────────────────────────────


class TestMemoryCleanup:
    def _seed(self, store: MemoryStore, count: int = 10) -> list[int]:
        ids = []
        for i in range(count):
            mid = store.save(
                Memory(
                    session_id=f"sess-{i % 3:03d}",
                    content=f"Memory {i}",
                    project="testproj",
                )
            )
            ids.append(mid)
        return ids

    def test_cleanup_no_criteria(self, memory_store: MemoryStore):
        self._seed(memory_store)
        result = memory_store.cleanup()
        assert "error" in result

    def test_cleanup_dry_run(self, memory_store: MemoryStore):
        self._seed(memory_store, 10)
        result = memory_store.cleanup(keep_latest=3, dry_run=True)
        assert result["dry_run"] is True
        assert result["would_delete"] == 7
        # Verify nothing actually deleted
        assert memory_store.get_stats()["total_memories"] == 10

    def test_cleanup_keep_latest(self, memory_store: MemoryStore):
        self._seed(memory_store, 10)
        result = memory_store.cleanup(keep_latest=3, dry_run=False)
        assert result["deleted"] == 7
        assert result["dry_run"] is False
        assert memory_store.get_stats()["total_memories"] == 3

    def test_cleanup_by_session(self, memory_store: MemoryStore):
        self._seed(memory_store, 9)  # 3 sessions × 3 each
        result = memory_store.cleanup(session_id="sess-000", dry_run=False)
        assert result["deleted"] == 3
        assert memory_store.get_stats()["total_memories"] == 6

    def test_cleanup_older_than_days(self, memory_store: MemoryStore):
        ids = self._seed(memory_store, 5)
        # Backdate the first 3 memories by 2 days. created_at has only
        # second resolution (datetime('now')), so an older_than_days=0
        # cutoff ("== now") is sub-second race-prone: it depends on whether
        # the wall clock ticks a second between _seed() and cleanup(). A
        # 2-day backdate vs a 1-day cutoff is unambiguous and deterministic.
        placeholders = ",".join("?" * 3)
        memory_store._conn.execute(
            f"UPDATE memories SET created_at = datetime('now', '-2 days') "
            f"WHERE id IN ({placeholders})",
            ids[:3],
        )
        memory_store._conn.commit()

        # Positive path: with a 1-day cutoff only the 3 backdated memories
        # are "older than 1 day"; the 2 just-created ones are not.
        result = memory_store.cleanup(older_than_days=1, dry_run=True)
        assert result["would_delete"] == 3

        # Negative path: a far-past cutoff (365 days) matches nothing,
        # since even the backdated memories are only 2 days old.
        result_none = memory_store.cleanup(older_than_days=365, dry_run=True)
        assert result_none["would_delete"] == 0

    def test_cleanup_empty_db(self, memory_store: MemoryStore):
        result = memory_store.cleanup(keep_latest=5, dry_run=False)
        # Zero matches must still report honestly as a non-dry-run: the old
        # {"would_delete": 0, "dry_run": True} shape made the composite
        # pm_memory_cleanup response claim "dry-run" while its summaries
        # path had actually deleted rows (PMSERV-162 adversarial review).
        assert result["deleted"] == 0
        assert result["dry_run"] is False


# ─── Server tool integration ─────────────────────────


class TestServerToolMemoryStats:
    def _setup_project(self, tmp_path, monkeypatch):
        from pmlens.models import Project
        from pmlens.storage import _save_project

        pm_path = tmp_path / ".pm"
        pm_path.mkdir(exist_ok=True)
        (pm_path / "daily").mkdir(exist_ok=True)
        project = Project(name="statsproj", display_name="Stats Test")
        _save_project(pm_path, project)
        monkeypatch.chdir(tmp_path)

        import pmlens.server

        pmlens.server._memory_stores.clear()

    def test_stats_returns_db_size(self, tmp_path, monkeypatch):
        self._setup_project(tmp_path, monkeypatch)
        from pmlens.server import pm_memory_stats, pm_remember

        pm_remember(content="Test memory for stats")
        result = pm_memory_stats()
        assert result["total_memories"] >= 1
        assert "db_size" in result
        assert "db_size_bytes" in result

    def test_stats_type_breakdown(self, tmp_path, monkeypatch):
        self._setup_project(tmp_path, monkeypatch)
        from pmlens.server import pm_memory_stats, pm_remember

        pm_remember(content="Observation 1", type="observation")
        pm_remember(content="Insight 1", type="insight")
        pm_remember(content="Lesson 1", type="lesson")

        result = pm_memory_stats()
        assert result["by_type"].get("observation", 0) >= 1
        assert result["by_type"].get("insight", 0) >= 1
        assert result["by_type"].get("lesson", 0) >= 1


class TestServerToolMemoryCleanup:
    def _setup_project(self, tmp_path, monkeypatch):
        from pmlens.models import Project
        from pmlens.storage import _save_project

        pm_path = tmp_path / ".pm"
        pm_path.mkdir(exist_ok=True)
        (pm_path / "daily").mkdir(exist_ok=True)
        project = Project(name="cleanupproj", display_name="Cleanup Test")
        _save_project(pm_path, project)
        monkeypatch.chdir(tmp_path)

        import pmlens.server

        pmlens.server._memory_stores.clear()

    def test_cleanup_default_dry_run(self, tmp_path, monkeypatch):
        self._setup_project(tmp_path, monkeypatch)
        from pmlens.server import pm_memory_cleanup, pm_remember

        for i in range(5):
            pm_remember(content=f"Memory {i}")

        result = pm_memory_cleanup(keep_latest=2)
        assert result["dry_run"] is True
        assert result["would_delete"] == 3

    def test_cleanup_actual_delete(self, tmp_path, monkeypatch):
        self._setup_project(tmp_path, monkeypatch)
        from pmlens.server import pm_memory_cleanup, pm_memory_stats, pm_remember

        for i in range(5):
            pm_remember(content=f"Memory {i}")

        pm_memory_cleanup(keep_latest=2, dry_run=False)
        stats = pm_memory_stats()
        assert stats["total_memories"] == 2

    # ── PMSERV-162: summaries pruning via the tool layer ──

    def _seed_summaries(self, tmp_path, rows):
        """Pre-populate .pm/memory.db with stamped summaries (the tool will
        reopen the same file through _get_memory_store)."""
        from pmlens.models import SessionSummary

        store = MemoryStore(tmp_path / ".pm" / "memory.db", global_db_path=None)
        try:
            for sid, updated in rows:
                store.save_session_summary(
                    SessionSummary(session_id=sid, summary=sid, project="p", branch="main")
                )
                store._conn.execute(
                    "UPDATE session_summaries SET created_at = ?, updated_at = ?"
                    " WHERE session_id = ?",
                    ("2026-01-01 00:00:00", updated, sid),
                )
            store._conn.commit()
        finally:
            store.close()

    def test_summaries_only_call_is_a_valid_criterion(self, tmp_path, monkeypatch):
        """summaries_keep_latest alone must NOT hit the 'No cleanup criteria'
        error (that check lives in the memories path). RED before fix: the
        parameter does not exist."""
        self._setup_project(tmp_path, monkeypatch)
        from pmlens.server import pm_memory_cleanup

        self._seed_summaries(
            tmp_path,
            [("s1", "2026-03-01 00:00:00"), ("s2", "2026-04-01 00:00:00")],
        )
        result = pm_memory_cleanup(summaries_keep_latest=1, dry_run=True)
        assert "error" not in result
        assert result["summaries"]["would_delete"] == 1
        assert result["summaries"]["dry_run"] is True

    def test_summaries_prune_composes_with_memories_cleanup(self, tmp_path, monkeypatch):
        """Namespacing: memories result stays top-level (backward compat),
        summaries result nests under 'summaries'."""
        self._setup_project(tmp_path, monkeypatch)
        from pmlens.server import pm_memory_cleanup, pm_remember

        for i in range(3):
            pm_remember(content=f"Memory {i}")
        self._seed_summaries(
            tmp_path,
            [("s1", "2026-03-01 00:00:00"), ("s2", "2026-04-01 00:00:00")],
        )
        result = pm_memory_cleanup(keep_latest=1, summaries_keep_latest=1, dry_run=False)
        assert result["deleted"] == 2
        assert result["summaries"]["deleted"] == 1

    def test_summaries_recent_prune_surfaces_warning(self, tmp_path, monkeypatch):
        """Deleting a summary still inside the 30-minute ambiguity window
        silently disables concurrent-session detection — the tool must say so
        via warnings[] (repo convention: never silent side effects)."""
        self._setup_project(tmp_path, monkeypatch)
        from pmlens.server import pm_memory_cleanup

        store = MemoryStore(tmp_path / ".pm" / "memory.db", global_db_path=None)
        now = store._conn.execute("SELECT datetime('now') AS n").fetchone()["n"]
        store.close()
        self._seed_summaries(tmp_path, [("active-1", now), ("active-2", now)])
        result = pm_memory_cleanup(summaries_keep_latest=1, dry_run=False)
        assert result["summaries"]["deleted"] == 1
        codes = [w["code"] for w in result.get("warnings", [])]
        assert "summaries_pruned_recent" in codes

    def test_summaries_keep_latest_below_one_errors_without_deleting(self, tmp_path, monkeypatch):
        self._setup_project(tmp_path, monkeypatch)
        from pmlens.server import pm_memory_cleanup

        self._seed_summaries(tmp_path, [("only", "2026-03-01 00:00:00")])
        result = pm_memory_cleanup(summaries_keep_latest=0, dry_run=False)
        assert "error" in result["summaries"]

    def test_summaries_warning_honors_env_ambiguity_window(self, tmp_path, monkeypatch):
        """The warning must use the same env-configurable window pm_recall's
        ambiguity detection uses (adversarial review: the hardcoded 30 made
        the warning silently miss deletions inside a widened window)."""
        self._setup_project(tmp_path, monkeypatch)
        monkeypatch.setenv("PM_SERVER_RECALL_AMBIGUITY_WINDOW_MIN", "120")
        from pmlens.server import pm_memory_cleanup

        store = MemoryStore(tmp_path / ".pm" / "memory.db", global_db_path=None)
        minus45 = store._conn.execute("SELECT datetime('now','-45 minutes') AS t").fetchone()["t"]
        now = store._conn.execute("SELECT datetime('now') AS t").fetchone()["t"]
        store.close()
        self._seed_summaries(tmp_path, [("mid-aged", minus45), ("newest", now)])
        result = pm_memory_cleanup(summaries_keep_latest=1, dry_run=False)
        assert result["summaries"]["recent_deleted"] == 1
        warning = next(w for w in result["warnings"] if w["code"] == "summaries_pruned_recent")
        assert "120分" in warning["message"]

    def test_composite_zero_match_memories_reports_honest_dry_run(self, tmp_path, monkeypatch):
        """Composite-response contradiction (adversarial review, confirmed):
        with dry_run=False, zero matching memories used to return the legacy
        {"would_delete": 0, "dry_run": True} at top level while the summaries
        path had ACTUALLY deleted rows — the response claimed a dry-run it
        did not perform."""
        self._setup_project(tmp_path, monkeypatch)
        from pmlens.server import pm_memory_cleanup

        self._seed_summaries(
            tmp_path,
            [("s1", "2026-03-01 00:00:00"), ("s2", "2026-04-01 00:00:00")],
        )
        result = pm_memory_cleanup(keep_latest=5, summaries_keep_latest=1, dry_run=False)
        assert result["dry_run"] is False
        assert result["deleted"] == 0
        assert result["summaries"]["deleted"] == 1
        assert result["summaries"]["dry_run"] is False


# ─── PMSERV-049: session_summaries upsert / list_within / migration ─────


class TestSessionSummaryUpsert:
    """UPSERT preserves created_at, refreshes updated_at on re-save."""

    def test_save_session_summary_preserves_created_at_on_resave(self, memory_store: MemoryStore):
        from pmlens.models import SessionSummary

        memory_store.save_session_summary(
            SessionSummary(session_id="sess-A", summary="first", project="p")
        )
        original = memory_store.get_latest_summary()
        assert original is not None
        original_created = original.created_at

        import time

        time.sleep(1.1)  # wait so datetime('now') ticks at least 1 second

        memory_store.save_session_summary(
            SessionSummary(session_id="sess-A", summary="second", project="p")
        )
        after = memory_store.get_latest_summary()
        assert after is not None
        # created_at must survive the UPSERT path
        assert after.created_at == original_created
        # And the new content is reflected
        assert after.summary == "second"

    def test_save_session_summary_updates_updated_at_on_resave(self, memory_store: MemoryStore):
        from pmlens.models import SessionSummary

        memory_store.save_session_summary(
            SessionSummary(session_id="sess-B", summary="first", project="p")
        )
        original = memory_store.get_latest_summary()
        assert original is not None
        first_updated = original.updated_at
        # On first save both stamp the same instant: created_at via the DDL
        # default (second precision) and updated_at via the INSERT's ms
        # expression (PMSERV-161) — 'now' is stable within one statement, so
        # the second-precision value prefixes the millisecond one.
        assert original.updated_at.startswith(original.created_at)

        import time

        time.sleep(1.1)

        memory_store.save_session_summary(
            SessionSummary(session_id="sess-B", summary="second", project="p")
        )
        after = memory_store.get_latest_summary()
        assert after is not None
        # updated_at must move forward — string-comparable since SQLite uses
        # ISO-like 'YYYY-MM-DD HH:MM:SS' UTC literals
        assert after.updated_at > first_updated


class TestListSummariesWithin:
    """list_summaries_within filters by updated_at (UTC)."""

    def test_list_summaries_within_window_filters_by_updated_at(self, memory_store: MemoryStore):
        from pmlens.models import SessionSummary

        memory_store.save_session_summary(
            SessionSummary(session_id="sess-recent", summary="recent", project="p")
        )

        # Plant an old row directly; bypass save_session_summary so we can
        # set updated_at into the past.
        memory_store._conn.execute(
            """INSERT INTO session_summaries
               (session_id, summary, goals, tasks_done, decisions, pending,
                project, created_at, updated_at)
               VALUES (?, ?, '', '[]', '[]', '[]', ?,
                       datetime('now', '-2 hours'),
                       datetime('now', '-2 hours'))""",
            ("sess-old", "old summary", "p"),
        )
        memory_store._conn.commit()

        within_30 = memory_store.list_summaries_within(window_minutes=30, limit=10)
        within_30_ids = {s.session_id for s in within_30}
        assert "sess-recent" in within_30_ids
        assert "sess-old" not in within_30_ids

        within_180 = memory_store.list_summaries_within(window_minutes=180, limit=10)
        within_180_ids = {s.session_id for s in within_180}
        assert "sess-recent" in within_180_ids
        assert "sess-old" in within_180_ids


class TestSchemaMigration:
    """Existing v0.4.x DB without updated_at column auto-migrates on open."""

    def test_existing_db_without_updated_at_column_migrates_correctly(self, tmp_path):
        import sqlite3

        from pmlens.memory import MemoryStore
        from pmlens.models import SessionSummary

        # Build a legacy-shaped DB (no updated_at column) by hand
        db_path = tmp_path / "legacy.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            """CREATE TABLE session_summaries (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id  TEXT NOT NULL UNIQUE,
                summary     TEXT NOT NULL,
                goals       TEXT,
                tasks_done  TEXT,
                decisions   TEXT,
                pending     TEXT,
                created_at  TEXT NOT NULL DEFAULT (datetime('now')),
                project     TEXT NOT NULL
            )"""
        )
        conn.execute(
            """INSERT INTO session_summaries
               (session_id, summary, goals, tasks_done, decisions, pending, project)
               VALUES ('sess-legacy', 'old', '', '[]', '[]', '[]', 'p')"""
        )
        conn.commit()
        cols_before = [
            r[1] for r in conn.execute("PRAGMA table_info(session_summaries)").fetchall()
        ]
        assert "updated_at" not in cols_before
        conn.close()

        # Opening with MemoryStore should run the migration
        store = MemoryStore(db_path, global_db_path=None)
        try:
            cols_after = [
                row["name"]
                for row in store._conn.execute("PRAGMA table_info(session_summaries)").fetchall()
            ]
            assert "updated_at" in cols_after

            row = store._conn.execute(
                "SELECT created_at, updated_at FROM session_summaries WHERE session_id = ?",
                ("sess-legacy",),
            ).fetchone()
            assert row is not None
            assert row["updated_at"] == row["created_at"]

            # And the UPSERT path works on the migrated DB
            store.save_session_summary(
                SessionSummary(session_id="sess-new", summary="post-migration", project="p")
            )
            latest = store.get_latest_summary()
            assert latest is not None
            assert latest.session_id == "sess-new"
        finally:
            store.close()

    def test_existing_db_without_branch_column_migrates_correctly(self, tmp_path):
        """PMSERV-124 / ADR-028: legacy DB (no branch column) auto-migrates,
        and branch-aware recall falls back gracefully for pre-feature rows."""
        import sqlite3

        from pmlens.memory import MemoryStore
        from pmlens.models import SessionSummary

        # Build a pre-branch DB by hand (has updated_at but no branch column).
        db_path = tmp_path / "legacy.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            """CREATE TABLE session_summaries (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id  TEXT NOT NULL UNIQUE,
                summary     TEXT NOT NULL,
                goals       TEXT,
                tasks_done  TEXT,
                decisions   TEXT,
                pending     TEXT,
                created_at  TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at  TEXT NOT NULL DEFAULT (datetime('now')),
                project     TEXT NOT NULL
            )"""
        )
        conn.execute(
            """INSERT INTO session_summaries
               (session_id, summary, goals, tasks_done, decisions, pending, project)
               VALUES ('sess-legacy', 'old work', '', '[]', '[]', '[]', 'p')"""
        )
        conn.commit()
        cols_before = [
            r[1] for r in conn.execute("PRAGMA table_info(session_summaries)").fetchall()
        ]
        assert "branch" not in cols_before
        conn.close()

        store = MemoryStore(db_path, global_db_path=None)
        try:
            cols_after = [
                row["name"]
                for row in store._conn.execute("PRAGMA table_info(session_summaries)").fetchall()
            ]
            assert "branch" in cols_after

            # Legacy row keeps branch NULL -> "" via _row_to_summary.
            legacy = store.get_latest_summary()
            assert legacy is not None
            assert legacy.branch == ""

            # Day-one back-compat: querying a real branch matches no legacy row,
            # so it falls back to the overall-latest with track_matched=False.
            summary, matched = store.get_latest_summary_by_branch("main")
            assert matched is False
            assert summary is not None
            assert summary.session_id == "sess-legacy"

            # A new save WITH a branch is then retrievable by that branch.
            store.save_session_summary(
                SessionSummary(
                    session_id="sess-new", summary="paper work", project="p", branch="paper"
                )
            )
            paper, paper_matched = store.get_latest_summary_by_branch("paper")
            assert paper_matched is True
            assert paper is not None
            assert paper.session_id == "sess-new"
            assert paper.branch == "paper"
        finally:
            store.close()
