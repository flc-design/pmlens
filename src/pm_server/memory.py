"""SQLite-based memory storage for pm-server.

Per-project memory in .pm/memory.db.
Global cross-project index in ~/.pm/memory.db.
FTS5 full-text search support with unicode61 tokenizer.
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

from .models import Memory, MemoryType, SessionSummary

# ─── Tag helpers ────────────────────────────────────


def _tags_to_str(tags: list[str]) -> str:
    """Convert tag list to comma-separated string for DB storage."""
    return ",".join(tags) if tags else ""


def _str_to_tags(s: str) -> list[str]:
    """Convert comma-separated string back to tag list."""
    return [t.strip() for t in s.split(",") if t.strip()] if s else []


# ─── JSON helpers for list fields ───────────────────


def _list_to_json(items: list[str]) -> str:
    """Convert list to JSON string for DB storage."""
    return json.dumps(items, ensure_ascii=False) if items else "[]"


def _json_to_list(s: str | None) -> list[str]:
    """Convert JSON string back to list."""
    if not s:
        return []
    try:
        return json.loads(s)
    except (json.JSONDecodeError, TypeError):
        return []


# ─── FTS5 query helpers ──────────────────────────

_FTS5_SPECIAL_RE = re.compile(r"[-:]")


def _sanitize_fts_query(query: str) -> str:
    """Sanitize a user query for safe FTS5 MATCH usage.

    Wraps tokens that contain hyphens or colons in double quotes
    so FTS5 does not misinterpret them as column-filter syntax
    (e.g. ``pm-server`` → ``"pm-server"``).  Already-quoted phrases
    are preserved as-is.
    """
    parts: list[str] = []
    for m in re.finditer(r'"[^"]*"|\S+', query):
        token = m.group()
        if token.startswith('"'):
            parts.append(token)
        elif _FTS5_SPECIAL_RE.search(token):
            parts.append(f'"{token}"')
        else:
            parts.append(token)
    return " ".join(parts)


# ─── SQLite concurrency pragmas (PMSERV-047) ────────

_BUSY_TIMEOUT_MS = 5000


def _apply_pragmas(conn: sqlite3.Connection) -> None:
    """Apply WAL mode + concurrency-friendly pragmas to a SQLite connection.

    journal_mode=WAL is DB-persistent (stored in the file header) and unlocks
    snapshot isolation: readers do not block writers and vice versa, which is
    the key win for the multi-process pm-server pattern (Claude Code + Codex
    CLI sessions sharing one .pm/memory.db).

    synchronous=NORMAL is connection-scoped and safe under WAL — torn writes
    are still prevented by WAL frame design, but per-commit fsync is skipped
    in favour of fsync at checkpoint. SQLite official guidance.

    busy_timeout=5000 ms matches the PMSERV-048 filelock timeout so the
    YAML and SQLite layers share the same wait budget.
    """
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")


# ─── Schema SQL ─────────────────────────────────────

_SCHEMA_SQL = """\
CREATE TABLE IF NOT EXISTS memories (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT NOT NULL,
    type        TEXT NOT NULL,
    content     TEXT NOT NULL,
    task_id     TEXT,
    decision_id TEXT,
    tags        TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    project     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS session_summaries (
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
);
"""

_FTS_SCHEMA_SQL = """\
CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
    content,
    tags,
    content=memories,
    content_rowid=id,
    tokenize='unicode61'
);
"""

_TRIGGER_SQL = """\
CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
    INSERT INTO memories_fts(rowid, content, tags)
    VALUES (new.id, new.content, new.tags);
END;

CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, content, tags)
    VALUES ('delete', old.id, old.content, old.tags);
END;
"""

# ─── Global index schema ───────────────────────────

_GLOBAL_SCHEMA_SQL = """\
CREATE TABLE IF NOT EXISTS memory_index (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    project      TEXT NOT NULL,
    project_path TEXT NOT NULL,
    memory_id    INTEGER NOT NULL,
    type         TEXT NOT NULL,
    content      TEXT NOT NULL,
    tags         TEXT,
    task_id      TEXT,
    created_at   TEXT NOT NULL
);
"""

_GLOBAL_FTS_SQL = """\
CREATE VIRTUAL TABLE IF NOT EXISTS memory_index_fts USING fts5(
    content,
    tags,
    project,
    content=memory_index,
    content_rowid=id,
    tokenize='unicode61'
);
"""

_GLOBAL_TRIGGER_SQL = """\
CREATE TRIGGER IF NOT EXISTS memory_index_ai AFTER INSERT ON memory_index BEGIN
    INSERT INTO memory_index_fts(rowid, content, tags, project)
    VALUES (new.id, new.content, new.tags, new.project);
END;

CREATE TRIGGER IF NOT EXISTS memory_index_ad AFTER DELETE ON memory_index BEGIN
    INSERT INTO memory_index_fts(memory_index_fts, rowid, content, tags, project)
    VALUES ('delete', old.id, old.content, old.tags, old.project);
END;
"""


# ─── MemoryStore ────────────────────────────────────


class MemoryStore:
    """SQLite memory store with FTS5 search.

    Args:
        db_path: Path to the SQLite database file.
        global_db_path: Path to the global cross-project index.
            Defaults to ~/.pm/memory.db. Set to None to disable sync.
    """

    def __init__(
        self,
        db_path: Path,
        global_db_path: Path | None = Path.home() / ".pm" / "memory.db",
    ) -> None:
        self.db_path = db_path
        self.global_db_path = global_db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        _apply_pragmas(self._conn)
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        """Create tables, FTS index, and triggers if they don't exist."""
        cur = self._conn.cursor()
        cur.executescript(_SCHEMA_SQL)
        cur.executescript(_FTS_SCHEMA_SQL)
        cur.executescript(_TRIGGER_SQL)
        cur.execute("PRAGMA user_version = 1")
        self._conn.commit()
        self._migrate_session_summaries_updated_at()

    def _migrate_session_summaries_updated_at(self) -> None:
        """Add updated_at column for DBs created before PMSERV-049.

        Idempotent: skips ALTER if the column already exists. Backfills
        updated_at with created_at so pre-migration rows have a sane
        latest-save timestamp. Always (re)creates the supporting index
        — safe because the column is guaranteed to exist after this method.
        """
        cur = self._conn.cursor()
        cols = [
            row["name"] for row in cur.execute("PRAGMA table_info(session_summaries)").fetchall()
        ]
        if "updated_at" not in cols:
            cur.execute("ALTER TABLE session_summaries ADD COLUMN updated_at TEXT")
            cur.execute(
                "UPDATE session_summaries SET updated_at = created_at WHERE updated_at IS NULL"
            )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_session_summaries_updated_at"
            " ON session_summaries(updated_at)"
        )
        self._conn.commit()

    def close(self) -> None:
        """Close the database connection."""
        self._conn.close()

    # ─── Memory CRUD ────────────────────────────────

    def save(self, memory: Memory) -> int:
        """Save a memory and return its auto-generated ID.

        Also syncs to the global cross-project index if configured.
        """
        cur = self._conn.execute(
            """INSERT INTO memories
               (session_id, type, content, task_id, decision_id, tags, project)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                memory.session_id,
                memory.type.value if isinstance(memory.type, MemoryType) else memory.type,
                memory.content,
                memory.task_id,
                memory.decision_id,
                _tags_to_str(memory.tags),
                memory.project,
            ),
        )
        self._conn.commit()
        memory_id: int = cur.lastrowid  # type: ignore[assignment]

        # Sync to global index
        self.sync_to_global(memory, memory_id)

        return memory_id

    def search(
        self,
        query: str,
        type: str | None = None,
        limit: int = 5,
    ) -> list[Memory]:
        """Full-text search using FTS5.

        Args:
            query: Search query string.
            type: Filter by memory type.
            limit: Maximum results.
        """
        safe_query = _sanitize_fts_query(query)
        rows = self._conn.execute(
            """SELECT m.* FROM memories m
               JOIN memories_fts f ON m.id = f.rowid
               WHERE memories_fts MATCH ?
               ORDER BY rank
               LIMIT ?""",
            (safe_query, limit),
        ).fetchall()

        memories = [self._row_to_memory(r) for r in rows]
        if type:
            memories = [m for m in memories if m.type.value == type]
        return memories

    def get_by_task(self, task_id: str) -> list[Memory]:
        """Get all memories linked to a task."""
        rows = self._conn.execute(
            "SELECT * FROM memories WHERE task_id = ? ORDER BY id DESC",
            (task_id,),
        ).fetchall()
        return [self._row_to_memory(r) for r in rows]

    def get_by_decision(self, decision_id: str) -> list[Memory]:
        """Get all memories linked to a decision."""
        rows = self._conn.execute(
            "SELECT * FROM memories WHERE decision_id = ? ORDER BY id DESC",
            (decision_id,),
        ).fetchall()
        return [self._row_to_memory(r) for r in rows]

    def get_recent(self, limit: int = 10) -> list[Memory]:
        """Get most recent memories."""
        rows = self._conn.execute(
            "SELECT * FROM memories ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [self._row_to_memory(r) for r in rows]

    # ─── Session Summaries ──────────────────────────

    def save_session_summary(self, summary: SessionSummary) -> int:
        """Save a session summary via UPSERT.

        On first save: created_at and updated_at both default to datetime('now').
        On re-save (same session_id): preserves created_at, refreshes updated_at.
        Returns the row id of the inserted-or-updated row.
        """
        self._conn.execute(
            """INSERT INTO session_summaries
               (session_id, summary, goals, tasks_done, decisions, pending, project)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(session_id) DO UPDATE SET
                   summary = excluded.summary,
                   goals = excluded.goals,
                   tasks_done = excluded.tasks_done,
                   decisions = excluded.decisions,
                   pending = excluded.pending,
                   updated_at = datetime('now')""",
            (
                summary.session_id,
                summary.summary,
                summary.goals,
                _list_to_json(summary.tasks_done),
                _list_to_json(summary.decisions),
                _list_to_json(summary.pending),
                summary.project,
            ),
        )
        self._conn.commit()
        row = self._conn.execute(
            "SELECT id FROM session_summaries WHERE session_id = ?",
            (summary.session_id,),
        ).fetchone()
        return row["id"]

    def get_latest_summary(self) -> SessionSummary | None:
        """Get the most recent session summary."""
        row = self._conn.execute(
            "SELECT * FROM session_summaries ORDER BY id DESC LIMIT 1",
        ).fetchone()
        if row is None:
            return None
        return self._row_to_summary(row)

    def list_summaries(self, limit: int = 10) -> list[SessionSummary]:
        """List session summaries, newest first."""
        rows = self._conn.execute(
            "SELECT * FROM session_summaries ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [self._row_to_summary(r) for r in rows]

    def list_summaries_within(
        self,
        window_minutes: int = 30,
        limit: int = 10,
    ) -> list[SessionSummary]:
        """Return session summaries updated within the last N minutes (UTC).

        Window comparison uses SQLite ``datetime('now', '-N minutes')`` which
        evaluates in UTC. ``updated_at`` reflects the latest save for each
        session, so still-active sessions are captured even when their initial
        summary was created long ago. Boundary is inclusive: a summary updated
        exactly N minutes ago is included. Used by pm_recall ambiguity
        detection (PMSERV-049).
        """
        rows = self._conn.execute(
            """SELECT * FROM session_summaries
               WHERE updated_at >= datetime('now', ?)
               ORDER BY updated_at DESC LIMIT ?""",
            (f"-{window_minutes} minutes", limit),
        ).fetchall()
        return [self._row_to_summary(r) for r in rows]

    # ─── Row converters ─────────────────────────────

    @staticmethod
    def _row_to_memory(row: sqlite3.Row) -> Memory:
        """Convert a database row to a Memory model."""
        return Memory(
            id=row["id"],
            session_id=row["session_id"],
            type=MemoryType(row["type"]),
            content=row["content"],
            task_id=row["task_id"],
            decision_id=row["decision_id"],
            tags=_str_to_tags(row["tags"]),
            created_at=row["created_at"],
            project=row["project"],
        )

    @staticmethod
    def _row_to_summary(row: sqlite3.Row) -> SessionSummary:
        """Convert a database row to a SessionSummary model.

        Falls back to created_at when updated_at is missing (defensive: the
        migration backfills updated_at from created_at, so this only matters
        if a row is somehow inserted before the migration runs).
        """
        updated_at = row["updated_at"] if "updated_at" in row.keys() else None
        return SessionSummary(
            id=row["id"],
            session_id=row["session_id"],
            summary=row["summary"],
            goals=row["goals"] or "",
            tasks_done=_json_to_list(row["tasks_done"]),
            decisions=_json_to_list(row["decisions"]),
            pending=_json_to_list(row["pending"]),
            created_at=row["created_at"],
            updated_at=updated_at or row["created_at"],
            project=row["project"],
        )

    # ─── Global cross-project sync ──────────────────

    def sync_to_global(self, memory: Memory, memory_id: int) -> None:
        """Sync a memory to the global cross-project index.

        Silently skips if global sync is disabled or fails.
        The per-project DB is the source of truth.
        """
        if self.global_db_path is None:
            return
        try:
            self.global_db_path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(self.global_db_path))
            _apply_pragmas(conn)
            conn.executescript(_GLOBAL_SCHEMA_SQL)
            conn.executescript(_GLOBAL_FTS_SQL)
            conn.executescript(_GLOBAL_TRIGGER_SQL)
            conn.execute("PRAGMA user_version = 1")
            conn.execute(
                """INSERT INTO memory_index
                   (project, project_path, memory_id, type, content, tags, task_id, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))""",
                (
                    memory.project,
                    str(self.db_path.parent.parent),
                    memory_id,
                    memory.type.value if isinstance(memory.type, MemoryType) else memory.type,
                    memory.content,
                    _tags_to_str(memory.tags),
                    memory.task_id,
                ),
            )
            conn.commit()
            conn.close()
        except (sqlite3.Error, OSError):
            pass  # Global sync is best-effort

    # ─── Stats & Cleanup ─────────────────────────────

    def get_stats(self) -> dict:
        """Return memory statistics."""
        cur = self._conn.cursor()

        total = cur.execute("SELECT COUNT(*) FROM memories").fetchone()[0]

        type_counts = {}
        for row in cur.execute(
            "SELECT type, COUNT(*) as cnt FROM memories GROUP BY type"
        ).fetchall():
            type_counts[row["type"]] = row["cnt"]

        session_count = cur.execute("SELECT COUNT(DISTINCT session_id) FROM memories").fetchone()[0]

        summary_count = cur.execute("SELECT COUNT(*) FROM session_summaries").fetchone()[0]

        oldest = cur.execute("SELECT MIN(created_at) FROM memories").fetchone()[0]

        newest = cur.execute("SELECT MAX(created_at) FROM memories").fetchone()[0]

        db_size = self.db_path.stat().st_size if self.db_path.exists() else 0

        return {
            "total_memories": total,
            "by_type": type_counts,
            "sessions": session_count,
            "session_summaries": summary_count,
            "oldest": oldest,
            "newest": newest,
            "db_size_bytes": db_size,
        }

    def cleanup(
        self,
        older_than_days: int | None = None,
        keep_latest: int | None = None,
        session_id: str | None = None,
        dry_run: bool = False,
    ) -> dict:
        """Delete memories matching the given criteria.

        Args:
            older_than_days: Delete memories older than N days.
            keep_latest: Keep only the latest N memories, delete rest.
            session_id: Delete all memories from a specific session.
            dry_run: If True, return what would be deleted without deleting.

        Returns:
            Dict with count of deleted (or would-be-deleted) memories.
        """
        cur = self._conn.cursor()
        conditions: list[str] = []
        params: list[object] = []

        if older_than_days is not None:
            conditions.append("created_at < datetime('now', ?)")
            params.append(f"-{older_than_days} days")

        if session_id is not None:
            conditions.append("session_id = ?")
            params.append(session_id)

        if keep_latest is not None:
            # Delete everything except the latest N
            conditions.append(
                f"id NOT IN (SELECT id FROM memories ORDER BY id DESC LIMIT {keep_latest})"
            )

        if not conditions:
            return {"deleted": 0, "dry_run": dry_run, "error": "No cleanup criteria specified"}

        where = " AND ".join(conditions)

        # Count affected rows
        count = cur.execute(
            f"SELECT COUNT(*) FROM memories WHERE {where}",
            params,  # noqa: S608
        ).fetchone()[0]

        if dry_run or count == 0:
            return {"would_delete": count, "dry_run": True}

        # Delete from FTS first (triggers handle this, but be explicit for cleanup)
        cur.execute(f"DELETE FROM memories WHERE {where}", params)  # noqa: S608
        self._conn.commit()

        return {"deleted": count, "dry_run": False}

    def search_global(
        self,
        query: str,
        limit: int = 10,
    ) -> list[dict]:
        """Search across all projects using the global index.

        Returns dicts with project info included.
        """
        if self.global_db_path is None or not self.global_db_path.exists():
            return []
        try:
            conn = sqlite3.connect(str(self.global_db_path))
            conn.row_factory = sqlite3.Row
            _apply_pragmas(conn)
            safe_query = _sanitize_fts_query(query)
            rows = conn.execute(
                """SELECT m.* FROM memory_index m
                   JOIN memory_index_fts f ON m.id = f.rowid
                   WHERE memory_index_fts MATCH ?
                   ORDER BY rank
                   LIMIT ?""",
                (safe_query, limit),
            ).fetchall()
            conn.close()
            return [
                {
                    "id": r["id"],
                    "project": r["project"],
                    "project_path": r["project_path"],
                    "memory_id": r["memory_id"],
                    "type": r["type"],
                    "content": r["content"],
                    "tags": _str_to_tags(r["tags"]),
                    "task_id": r["task_id"],
                    "created_at": r["created_at"],
                }
                for r in rows
            ]
        except sqlite3.Error:
            return []
