"""Desktop Outbox SQLite store (ADR-019, WF-028).

Append-only outbox bridge for Claude Desktop knowledge capture.
Lives at ~/.pm/desktop/desktop.db, separate from per-project memory.db.
Lens invariant: main .pm/memory.db remains untouched even under
PM_LENS=1 + PM_DESKTOP_WRITE=1.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Literal

from .memory import _apply_pragmas
from .storage import GLOBAL_PM_DIR

OutboxType = Literal["memory", "log", "lesson", "artifact"]
OutboxStatus = Literal["pending", "merged", "rejected"]

_VALID_TYPES: frozenset[str] = frozenset({"memory", "log", "lesson", "artifact"})
_VALID_STATUSES: frozenset[str] = frozenset({"pending", "merged", "rejected"})

# Default location: ~/.pm/desktop/desktop.db (per ADR-019).
DEFAULT_OUTBOX_DB_PATH: Path = GLOBAL_PM_DIR / "desktop" / "desktop.db"


# ─── Schema SQL (PRAGMA user_version=1, independent of memory.db versioning) ──

_SCHEMA_SQL = """\
CREATE TABLE IF NOT EXISTS desktop_outbox (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    host_id           TEXT NOT NULL,
    source_session    TEXT NOT NULL,
    type              TEXT NOT NULL CHECK(type IN ('memory', 'log', 'lesson', 'artifact')),
    content           TEXT NOT NULL,
    source_project    TEXT,
    tags              TEXT,
    artifact_type     TEXT,
    artifact_filename TEXT,
    suggested_path    TEXT,
    status            TEXT NOT NULL DEFAULT 'pending'
                      CHECK(status IN ('pending', 'merged', 'rejected')),
    merged_to_id      INTEGER,
    merged_to_path    TEXT,
    reject_reason     TEXT,
    created_at        TEXT NOT NULL DEFAULT (datetime('now')),
    merged_at         TEXT
);

CREATE INDEX IF NOT EXISTS idx_outbox_status_created
    ON desktop_outbox(status, created_at);
CREATE INDEX IF NOT EXISTS idx_outbox_project_status
    ON desktop_outbox(source_project, status);
CREATE INDEX IF NOT EXISTS idx_outbox_type_status
    ON desktop_outbox(type, status);
CREATE INDEX IF NOT EXISTS idx_outbox_tags
    ON desktop_outbox(tags);
"""

# Append-only trigger: any UPDATE touching the listed immutable columns
# raises and aborts. Permitted columns (status / merged_to_id / merged_to_path
# / merged_at / reject_reason) are excluded from the OF list, so legitimate
# state transitions via mark_merged()/mark_rejected() succeed.
_TRIGGER_SQL = """\
CREATE TRIGGER IF NOT EXISTS trg_outbox_append_only
BEFORE UPDATE OF
    id, host_id, source_session, type, content,
    source_project, tags, artifact_type, artifact_filename,
    suggested_path, created_at
ON desktop_outbox
BEGIN
    SELECT RAISE(ABORT, 'desktop_outbox is append-only; only status/merged_to_*/merged_at/reject_reason updates allowed');
END;
"""


def _ensure_schema(db_path: Path) -> None:
    """Create the desktop.db schema if missing.

    PRAGMA page_size=8192 must be set before any write commits the DB header
    (it is sticky thereafter). We detect first-init via file non-existence and
    apply page_size first; subsequent inits inherit the existing page_size.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    is_new = not db_path.exists()
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    try:
        if is_new:
            conn.execute("PRAGMA page_size = 8192")
        _apply_pragmas(conn)
        conn.executescript(_SCHEMA_SQL)
        conn.executescript(_TRIGGER_SQL)
        conn.execute("PRAGMA user_version = 1")
        conn.commit()
    finally:
        conn.close()


class DesktopOutboxStore:
    """Append-only outbox store for cross-host knowledge capture.

    Designed for two-host coordination: Claude Desktop writes via
    pm_outbox_remember / pm_outbox_log; Claude Code reads via
    pm_outbox_pending and promotes via pm_outbox_merge / pm_outbox_reject.

    Schema enforces append-only semantics via trg_outbox_append_only.
    Only status and merge/reject metadata may be updated after insert.
    """

    def __init__(self, db_path: Path | None = None) -> None:
        self.db_path: Path = Path(db_path) if db_path is not None else DEFAULT_OUTBOX_DB_PATH
        _ensure_schema(self.db_path)

    def _connect(self) -> sqlite3.Connection:
        """Open a short-lived RW connection with WAL pragmas applied."""
        conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        _apply_pragmas(conn)
        return conn

    # ─── Writes ─────────────────────────────────────────

    def append(
        self,
        host_id: str,
        source_session: str,
        type: OutboxType,
        content: str,
        source_project: str | None = None,
        tags: str | None = None,
    ) -> int:
        """Append a new outbox entry. Returns the new row id."""
        if type not in _VALID_TYPES:
            raise ValueError(f"invalid type: {type!r} (expected one of {sorted(_VALID_TYPES)})")
        conn = self._connect()
        try:
            cur = conn.execute(
                """
                INSERT INTO desktop_outbox
                    (host_id, source_session, type, content, source_project, tags)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (host_id, source_session, type, content, source_project, tags),
            )
            conn.commit()
            return int(cur.lastrowid or 0)
        finally:
            conn.close()

    def mark_merged(
        self,
        id: int,
        merged_to_id: int | None,
        merged_to_path: str | None,
    ) -> bool:
        """Transition pending → merged. Returns True if updated, False if already
        non-pending (idempotent skip)."""
        conn = self._connect()
        try:
            cur = conn.execute(
                """
                UPDATE desktop_outbox
                   SET status = 'merged',
                       merged_to_id = ?,
                       merged_to_path = ?,
                       merged_at = datetime('now')
                 WHERE id = ? AND status = 'pending'
                """,
                (merged_to_id, merged_to_path, id),
            )
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()

    def mark_rejected(self, id: int, reason: str) -> bool:
        """Transition pending → rejected. Returns True if updated, False if
        already non-pending (idempotent skip)."""
        if not reason or not reason.strip():
            raise ValueError("reason is required for rejection")
        conn = self._connect()
        try:
            cur = conn.execute(
                """
                UPDATE desktop_outbox
                   SET status = 'rejected',
                       reject_reason = ?,
                       merged_at = datetime('now')
                 WHERE id = ? AND status = 'pending'
                """,
                (reason, id),
            )
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()

    # ─── Reads ──────────────────────────────────────────

    def get(self, id: int) -> dict | None:
        """Fetch a single row by id, or None if not found."""
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM desktop_outbox WHERE id = ?", (id,)
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def pending(
        self,
        filter_project: str | None = None,
        filter_type: OutboxType | None = None,
        filter_status: str = "pending",
        limit: int = 50,
        offset: int = 0,
    ) -> dict:
        """Paginated listing of outbox entries.

        filter_status='all' lists across all statuses (including merged/rejected).
        Returns {"items": [...], "total": N, "has_more": bool, "next_offset": int}.
        """
        if limit < 0 or offset < 0:
            raise ValueError("limit and offset must be non-negative")
        if filter_type is not None and filter_type not in _VALID_TYPES:
            raise ValueError(f"invalid filter_type: {filter_type!r}")
        if filter_status != "all" and filter_status not in _VALID_STATUSES:
            raise ValueError(f"invalid filter_status: {filter_status!r}")

        clauses: list[str] = []
        params: list[object] = []
        if filter_status != "all":
            clauses.append("status = ?")
            params.append(filter_status)
        if filter_project is not None:
            clauses.append("source_project = ?")
            params.append(filter_project)
        if filter_type is not None:
            clauses.append("type = ?")
            params.append(filter_type)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""

        conn = self._connect()
        try:
            total = int(
                conn.execute(
                    f"SELECT COUNT(*) FROM desktop_outbox{where}", params
                ).fetchone()[0]
            )
            rows = conn.execute(
                f"SELECT * FROM desktop_outbox{where} "
                "ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
                [*params, limit, offset],
            ).fetchall()
        finally:
            conn.close()

        items = [dict(r) for r in rows]
        next_offset = offset + len(items)
        return {
            "items": items,
            "total": total,
            "has_more": next_offset < total,
            "next_offset": next_offset,
        }

    def get_pending_count(self) -> int:
        """Return the count of pending entries (used by pm_status diagnostics)."""
        conn = self._connect()
        try:
            return int(
                conn.execute(
                    "SELECT COUNT(*) FROM desktop_outbox WHERE status = 'pending'"
                ).fetchone()[0]
            )
        finally:
            conn.close()

    def close(self) -> None:
        """No-op for short-lived connection design; provided for symmetry
        with MemoryStore and to give the factory a clear shutdown hook."""
        return None


# ─── Module-level factory (f5: pytest test isolation) ──────────────────────

_outbox_store: DesktopOutboxStore | None = None


def get_outbox_store(db_path: Path | None = None) -> DesktopOutboxStore:
    """Return the process-wide DesktopOutboxStore, creating it on first call.

    db_path is only honored on the *first* call within a process. Subsequent
    calls return the cached instance regardless of db_path — tests that need
    a fresh store with a different path must call clear_outbox_store() first.
    """
    global _outbox_store
    if _outbox_store is None:
        _outbox_store = DesktopOutboxStore(db_path)
    return _outbox_store


def clear_outbox_store() -> None:
    """Reset the module-level cache. Required by pytest fixtures so tests
    do not leak desktop.db state across cases (factory pattern, amendment f5)."""
    global _outbox_store
    if _outbox_store is not None:
        _outbox_store.close()
        _outbox_store = None
