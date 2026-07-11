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

from . import storage as _storage
from .memory import _BUSY_TIMEOUT_MS, _apply_pragmas
from .models import PmServerError

OutboxType = Literal["memory", "log", "lesson", "artifact"]
OutboxStatus = Literal["pending", "merged", "rejected"]

_VALID_TYPES: frozenset[str] = frozenset({"memory", "log", "lesson", "artifact"})
_VALID_STATUSES: frozenset[str] = frozenset({"pending", "merged", "rejected"})


def default_outbox_db_path() -> Path:
    """Resolve ~/.pm/desktop/desktop.db dynamically (ADR-019).

    Looked up via the ``pmlens.storage`` module so test fixtures that
    monkeypatch ``GLOBAL_PM_DIR`` are honored at call time (mirrors the
    ``server.py`` GLOBAL_PM_DIR double-patch pattern in conftest.py).
    """
    return _storage.GLOBAL_PM_DIR / "desktop" / "desktop.db"


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
    SELECT RAISE(ABORT, 'desktop_outbox append-only; only state/merge updates allowed');
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

    readonly (PMSERV-142, AD-2/AD-3): when True, the constructor never
    creates the DB file/schema and write methods raise PmServerError. The
    "does desktop.db exist" question is deliberately NOT decided here and
    cached — it is re-evaluated on every read call (see the read methods
    below) so a store that is constructed while the file is absent still
    picks up entries written later by a concurrent RW store in the same
    process (cross-check BLOCKER: caching missing at __init__ time would
    reintroduce "file exists but reads keep returning empty").
    """

    def __init__(self, db_path: Path | None = None, readonly: bool = False) -> None:
        self.db_path: Path = Path(db_path) if db_path is not None else default_outbox_db_path()
        self.readonly: bool = readonly
        if not readonly:
            _ensure_schema(self.db_path)

    def _connect(self) -> sqlite3.Connection:
        """Open a short-lived connection.

        RW: current behaviour — WAL + concurrency pragmas via
        ``_apply_pragmas`` (this connection is what creates/maintains the
        schema).

        RO (AD-3): ``file:<path>?mode=ro`` — deliberately WITHOUT
        ``immutable=1``. Unlike memory.db's Lens RO path (see
        ``memory._connect_readonly``), desktop.db always has an active
        writer (Desktop outbox host), so ``immutable=1`` risks stale or
        corrupt reads against a file mid-WAL-write. Only
        ``PRAGMA busy_timeout`` is applied — ``journal_mode``/``synchronous``
        would attempt to write the file header and fail with
        OperationalError on a read-only connection.
        """
        if self.readonly:
            uri = f"file:{self.db_path}?mode=ro"
            conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
            return conn
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
        if self.readonly:
            raise PmServerError("outbox store is read-only")
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
        if self.readonly:
            raise PmServerError("outbox store is read-only")
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
        if self.readonly:
            raise PmServerError("outbox store is read-only")
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
        """Fetch a single row by id, or None if not found.

        Missing-DB (PMSERV-142, evaluated fresh on every call — never
        cached at __init__) and SQLite-error paths both return None without
        touching sqlite or raising. connect() and execute() are wrapped in
        one try because sqlite3 errors surface at first execute rather than
        at connect (e.g. a crashed writer's stale -shm/-wal producing
        SQLITE_READONLY_RECOVERY, or a corrupt file producing
        DatabaseError).
        """
        if not self.db_path.exists():
            return None
        conn = None
        try:
            conn = self._connect()
            row = conn.execute("SELECT * FROM desktop_outbox WHERE id = ?", (id,)).fetchone()
            return dict(row) if row else None
        except sqlite3.Error:
            return None
        finally:
            if conn is not None:
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

        Missing-DB (PMSERV-142, evaluated fresh on every call) and SQLite-error
        paths both return the same empty shape without touching sqlite or
        raising — see get() for why connect()+execute() share one try/except.
        """
        if limit < 0 or offset < 0:
            raise ValueError("limit and offset must be non-negative")
        if filter_type is not None and filter_type not in _VALID_TYPES:
            raise ValueError(f"invalid filter_type: {filter_type!r}")
        if filter_status != "all" and filter_status not in _VALID_STATUSES:
            raise ValueError(f"invalid filter_status: {filter_status!r}")

        empty_page = {"items": [], "total": 0, "has_more": False, "next_offset": offset}
        if not self.db_path.exists():
            return empty_page

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

        conn = None
        try:
            conn = self._connect()
            total = int(
                conn.execute(f"SELECT COUNT(*) FROM desktop_outbox{where}", params).fetchone()[0]
            )
            rows = conn.execute(
                f"SELECT * FROM desktop_outbox{where} "
                "ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
                [*params, limit, offset],
            ).fetchall()
        except sqlite3.Error:
            return empty_page
        finally:
            if conn is not None:
                conn.close()

        items = [dict(r) for r in rows]
        next_offset = offset + len(items)
        # ``has_more`` must imply the page advanced, or a caller paginating on
        # next_offset spins forever. With limit=0 the query returns zero rows so
        # next_offset == offset (no progress) while rows remain — guard against
        # that by requiring a non-empty page (PMSERV-122, mirroring the
        # x_draft_store fix). limit=0 stays a VALID count-only probe rather than
        # a boundary rejection; we just never claim "more" on a 0-row page.
        return {
            "items": items,
            "total": total,
            "has_more": bool(items) and next_offset < total,
            "next_offset": next_offset,
        }

    def get_pending_count(self) -> int:
        """Return the count of pending entries (used by pm_status diagnostics).

        Missing-DB (PMSERV-142, evaluated fresh on every call) and
        SQLite-error paths both return 0 without touching sqlite or raising.
        """
        if not self.db_path.exists():
            return 0
        conn = None
        try:
            conn = self._connect()
            return int(
                conn.execute(
                    "SELECT COUNT(*) FROM desktop_outbox WHERE status = 'pending'"
                ).fetchone()[0]
            )
        except sqlite3.Error:
            return 0
        finally:
            if conn is not None:
                conn.close()

    def close(self) -> None:
        """No-op for short-lived connection design; provided for symmetry
        with MemoryStore and to give the factory a clear shutdown hook."""
        return None


# ─── Module-level factory (f5: pytest test isolation) ──────────────────────

_outbox_store: DesktopOutboxStore | None = None
_outbox_store_ro: DesktopOutboxStore | None = None


def get_outbox_store(db_path: Path | None = None, readonly: bool = False) -> DesktopOutboxStore:
    """Return the process-wide DesktopOutboxStore, creating it on first call.

    db_path is only honored on the *first* call within a process **for the
    requested cache slot**. RW (readonly=False, default — existing
    ``db_path=``-only call sites keep working unchanged) and RO
    (readonly=True) instances live in separate slots (PMSERV-142) so a
    readonly caller never shares — or accidentally upgrades — a writer's
    connection, and vice versa. Subsequent calls to the same slot return the
    cached instance regardless of db_path — tests that need a fresh store
    with a different path must call clear_outbox_store() first.
    """
    global _outbox_store, _outbox_store_ro
    if readonly:
        if _outbox_store_ro is None:
            _outbox_store_ro = DesktopOutboxStore(db_path, readonly=True)
        return _outbox_store_ro
    if _outbox_store is None:
        _outbox_store = DesktopOutboxStore(db_path)
    return _outbox_store


def clear_outbox_store() -> None:
    """Reset both module-level caches (RW and RO — PMSERV-142). Required by
    pytest fixtures so tests do not leak desktop.db state across cases
    (factory pattern, amendment f5)."""
    global _outbox_store, _outbox_store_ro
    if _outbox_store is not None:
        _outbox_store.close()
        _outbox_store = None
    if _outbox_store_ro is not None:
        _outbox_store_ro.close()
        _outbox_store_ro = None
