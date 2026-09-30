"""SQLite-based memory storage for pm-server.

Per-project memory in .pm/memory.db.
Global cross-project index in ~/.pm/memory.db.
FTS5 full-text search support with unicode61 tokenizer.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
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

# The single definition of "where does a query term begin and end". Shared by
# the FTS5 MATCH path and the LIKE fallback (PMSERV-175) so the two can never
# drift apart: a quoted phrase is one term, otherwise whitespace separates.
_QUERY_TOKEN_RE = re.compile(r'"[^"]*"|\S+')


def _sanitize_fts_query(query: str) -> str:
    """Sanitize a user query for safe FTS5 MATCH usage.

    Wraps tokens that contain hyphens or colons in double quotes
    so FTS5 does not misinterpret them as column-filter syntax
    (e.g. ``pm-server`` → ``"pm-server"``).  Already-quoted phrases
    are preserved as-is.
    """
    parts: list[str] = []
    for m in _QUERY_TOKEN_RE.finditer(query):
        token = m.group()
        if token.startswith('"'):
            parts.append(token)
        elif _FTS5_SPECIAL_RE.search(token):
            parts.append(f'"{token}"')
        else:
            parts.append(token)
    return " ".join(parts)


# ─── LIKE fallback term splitting (PMSERV-175) ───────

# Cap on how many terms the LIKE fallback will AND together. A pasted
# paragraph would otherwise build an unbounded WHERE clause plus one
# existence probe per term. Terms past the cap are dropped from the AND and
# reported in ``dropped_terms`` — a silent truncation would read as "these
# terms were considered and matched", which is the exact confusion this whole
# change exists to remove.
_MAX_FALLBACK_TERMS = 12


def _split_query_terms(query: str) -> list[str]:
    """Split a query into the terms the LIKE fallback should AND together.

    Uses the SAME token regex as :func:`_sanitize_fts_query`, deliberately:
    if the two paths disagreed on where a term begins and ends, the reported
    ``matched_terms`` would describe a different split than the one FTS5
    actually attempted, and the diagnostics would mislead rather than explain.

    Quoted phrases stay a single term with the quotes stripped — LIKE wants
    the literal text, not FTS5's phrase syntax.
    """
    terms: list[str] = []
    for m in _QUERY_TOKEN_RE.finditer(query):
        token = m.group()
        if token.startswith('"'):
            token = token[1:-1] if len(token) >= 2 and token.endswith('"') else token[1:]
        if token:
            terms.append(token)
    return terms


def _like_pattern(term: str) -> str:
    """Build an escaped ``%term%`` pattern for a LIKE ... ESCAPE '\\' clause."""
    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


@dataclass(frozen=True)
class SearchDiagnostics:
    """Why a search returned what it returned (PMSERV-175).

    ``strategy`` alone cannot distinguish "nothing relevant is stored" from
    "this query could never have matched anything", and that ambiguity is not
    hypothetical: a caller hit an empty ``like_fallback`` result, concluded
    the search path was broken, read ``.pm/memory.db`` with sqlite3 directly,
    and recorded that unverified conclusion as a memory. These fields exist so
    the answer is visible at the call site.

    Attributes:
        strategy: ``"fts"`` or ``"like_fallback"`` (unchanged semantics).
        fallback_reason: ``None`` when FTS produced the results, otherwise
            ``"fts_no_match"`` (MATCH was empty so the LIKE fallback ran),
            ``"global_index_absent"`` or ``"fts_error"`` (cross-project
            graceful degradation — those keep ``strategy="fts"`` for backward
            compatibility, so the reason is the ONLY way to tell them apart
            from a genuine empty FTS hit).
        matched_terms: fallback terms that exist somewhere in the store.
        unmatched_terms: fallback terms that exist nowhere — a non-empty list
            here is the direct answer to "why did I get zero rows".
        dropped_terms: terms beyond ``_MAX_FALLBACK_TERMS``, never silently
            discarded.
    """

    strategy: str
    fallback_reason: str | None = None
    matched_terms: tuple[str, ...] = field(default_factory=tuple)
    unmatched_terms: tuple[str, ...] = field(default_factory=tuple)
    dropped_terms: tuple[str, ...] = field(default_factory=tuple)


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


def _checkpoint_passive(conn: sqlite3.Connection) -> str | None:
    """Fold committed WAL frames into the main DB file. Never raises.

    Why the WRITE path checkpoints at all (PMSERV-176): a PM_LENS=1 reader
    opens the DB with ``immutable=1``, which skips WAL processing entirely —
    it sees only what has already reached the main file. Nothing on the read
    path can close that gap: reading a WAL database read-only requires the
    ``-shm`` sidecar, and creating one violates the ADR-028 invariant that a
    Lens host never writes into another project's ``.pm/``. So visibility is
    the writer's job. It owns the DB and may checkpoint; the reader may not.

    Without this, the only checkpoints were SQLite's automatic ones at
    ``wal_autocheckpoint`` (1000 pages ≈ 3.9 MiB by default) and purge's
    explicit TRUNCATE (PMSERV-171). A project that writes steadily but stays
    under the threshold and never purges therefore hid every record behind
    the WAL — measured at three weeks / 16 summaries on this repo, which is
    how PMSERV-176 was found.

    PASSIVE is deliberate: it copies what it can without waiting on readers
    and without blocking writers, so a concurrent session never stalls on our
    convenience. A partial copy is fine — the next save finishes the job.

    Returns an error string instead of raising, mirroring purge's
    ``vacuum_error`` contract (PMSERV-171): failing to checkpoint must never
    fail the save that triggered it.
    """
    try:
        conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
    except sqlite3.Error as e:
        return f"{type(e).__name__}: {e}"
    return None


def _connect_readonly(db_path: Path) -> sqlite3.Connection:
    """Open a SQLite connection in read-only, side-effect-free mode.

    Uses URI form ``file:<path>?mode=ro&immutable=1`` for PM_LENS=1 (Claude
    Desktop/Cowork) where the host must not create ``-wal``/``-shm`` sidecars
    or mutate change counters in another project's ``.pm/memory.db``.
    ``immutable=1`` tells SQLite the file is in stable storage and skips
    WAL processing entirely; the reader sees only the committed snapshot in
    the main file. Trade-off: writes still pending in WAL (un-checkpointed)
    are invisible until the owning pm-server next checkpoints — acceptable
    for a passive viewer.

    pragmas like ``journal_mode=WAL`` are deliberately NOT applied because
    they would attempt to write the file header and fail with OperationalError
    on a read-only connection.
    """
    uri = f"file:{db_path}?mode=ro&immutable=1"
    conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def _has_pm_server_schema(db_path: Path) -> bool:
    """Return True iff the SQLite DB has pm-server's ``memories`` table.

    Used by ``server._get_memory_store`` under PM_LENS=1 to distinguish
    "DB file exists but is uninitialized" (e.g. touched by an older install,
    a partial init, or unrelated SQLite content) from "DB file exists with
    a valid pm-server schema". Returns False on any SQLite error so the
    caller can fall back to an in-memory empty store rather than letting
    OperationalError propagate to read tools (PMSERV-093).
    """
    try:
        conn = _connect_readonly(db_path)
        try:
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='memories'"
            ).fetchone()
            return row is not None
        finally:
            conn.close()
    except sqlite3.Error:
        return False


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
    project     TEXT NOT NULL,
    branch      TEXT
);
"""

# Millisecond-precision "now" for session_summaries timestamps (PMSERV-161).
# SQLite's %f is "SS.SSS", so the format has no %S; output is fixed-width
# "YYYY-MM-DD HH:MM:SS.SSS". Lexicographic TEXT comparison stays correct
# against legacy second-precision values because 'HH:MM:SS' is a strict
# prefix of 'HH:MM:SS.mmm'. datetime('now') is second-precision, which made
# same-second saves tie on the effective timestamp and degrade "most
# recently worked" to id DESC (= most recently started). The DDL defaults
# above deliberately stay at datetime('now'): save_session_summary always
# writes updated_at explicitly so the default never orders anything, and
# keeping the DDL text identical avoids schema-text drift between DBs
# created by old and new binaries (design review, PMSERV-161).
_MS_NOW_SQL = "strftime('%Y-%m-%d %H:%M:%f','now')"

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
    created_at   TEXT NOT NULL,
    source       TEXT NOT NULL DEFAULT 'pm',
    source_path  TEXT,
    content_hash TEXT
);
"""

#: Provenance columns added for auto-memory ingest (PMSERV-156 / ADR-045).
#: Existing global DBs predate them, so every writer runs
#: :func:`_ensure_global_columns` before touching the table. ``source_path``
#: holds the note's ABSOLUTE path — deliberately not named ``source_file``,
#: which the overlay already uses for the basename (adversarial review: one
#: key name with two meanings).
_GLOBAL_PROVENANCE_COLUMNS: dict[str, str] = {
    "source": "TEXT NOT NULL DEFAULT 'pm'",
    "source_path": "TEXT",
    "content_hash": "TEXT",
}

#: ``source`` value marking a row ingested from Claude Code's auto-memory
#: store rather than promoted from a project ledger.
AUTO_MEMORY_SOURCE = "auto_memory"


def _ensure_global_columns(conn: sqlite3.Connection) -> None:
    """Add the provenance columns to a pre-PMSERV-156 global index.

    ``ADD COLUMN`` is backward compatible in both directions: existing rows
    default to ``source='pm'`` and an older binary keeps working because it
    never names the new columns. The FTS5 index is external-content over
    ``content``/``tags``/``project`` only, so widening the table does not
    touch it.
    """
    have = {r[1] for r in conn.execute("PRAGMA table_info(memory_index)")}
    if not have:
        # No memory_index table at all (a file someone connected to without
        # ever running DDL) — nothing to migrate; ALTER would raise
        # "no such table" here.
        return
    for name, decl in _GLOBAL_PROVENANCE_COLUMNS.items():
        if name not in have:
            conn.execute(f"ALTER TABLE memory_index ADD COLUMN {name} {decl}")  # noqa: S608


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
            None disables global sync. Callers (e.g. server.py) compute the
            default path from ``_storage.GLOBAL_PM_DIR`` so HOME monkeypatching
            in tests reaches the right location; module-import-time default was
            removed in PMSERV-080 (I-1).
        readonly: Open the DB with ``?mode=ro&immutable=1`` (no WAL/SHM
            sidecars, no schema mutations). Used by PM_LENS=1 to keep
            Desktop/Cowork passive on other projects' ``.pm/memory.db``.
        lens_fallback: Set True when the store was created as a Lens fallback
            (DB absent OR exists-but-uninitialized; see
            ``server._get_memory_store``). Read tools surface an explanatory
            ``note`` field so users can distinguish "no records yet" from
            "store unavailable" (PMSERV-091/093).
    """

    def __init__(
        self,
        db_path: Path,
        global_db_path: Path | None = None,
        *,
        readonly: bool = False,
        lens_fallback: bool = False,
        global_readonly: bool | None = None,
    ) -> None:
        self.db_path = db_path
        # The global index path is kept even for a readonly (Lens) store:
        # cross-project SEARCH is a read and stays available — it opens the
        # index with mode=ro&immutable=1, so no -wal/-shm sidecars are ever
        # created in ~/.pm (the RO invariant's actual guarantee, ADR-028).
        # Writes to the global index check global_readonly instead
        # (defense-in-depth on top of the tool not being registered under
        # PM_LENS). Pre-PMSERV-156 this was `None if readonly else ...`,
        # which silently killed Lens cross-project search entirely
        # (adversarial review, confirmed).
        self.global_db_path = global_db_path
        self.readonly = readonly
        self.global_readonly = readonly if global_readonly is None else global_readonly
        # PMSERV-091/093: signals "Lens fallback to in-memory" (DB absent OR
        # exists-but-uninitialized) so server.py read tools can add an
        # explanatory note distinguishing "no records" from "store unavailable".
        self.lens_fallback = lens_fallback
        # PMSERV-176: last PASSIVE checkpoint outcome, for diagnostics only.
        # None means "no checkpoint attempted yet, or it succeeded" — a
        # checkpoint failure is never fatal to the write that triggered it.
        self.last_checkpoint_error: str | None = None
        if readonly:
            self._conn = _connect_readonly(db_path)
        else:
            db_path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            _apply_pragmas(self._conn)
            self._ensure_schema()
        # Branch-aware recall needs the session_summaries.branch column. Under
        # PM_LENS=1 the store opens read-only and _ensure_schema (hence the
        # branch migration) never runs, so an older DB may lack the column.
        # Probe ONCE here so the branch-scoped queries can short-circuit to the
        # overall-latest fallback instead of raising OperationalError from an
        # allowlisted read-only tool (PMSERV-124/125).
        self._has_branch_col = self._column_exists("session_summaries", "branch")
        # PMSERV-158: session_summaries.updated_at is NULL on migrated DBs (the
        # ALTER-added column has no default) and absent entirely on pre-PMSERV-049
        # DBs opened read-only (the migration is skipped). Probe once and derive
        # the effective-timestamp expression every recency read uses: COALESCE
        # heals NULL/empty (→ created_at); when the column is missing we fall back
        # to created_at directly so read-only tools never hit
        # "no such column: updated_at". _ts_expr is a fixed internal constant
        # (one of two literals) — never interpolated from user input.
        self._has_updated_at_col = self._column_exists("session_summaries", "updated_at")
        self._ts_expr = (
            "COALESCE(NULLIF(updated_at, ''), created_at)"
            if self._has_updated_at_col
            else "created_at"
        )

    def _column_exists(self, table: str, column: str) -> bool:
        """Return True iff ``table`` has ``column`` (via PRAGMA table_info)."""
        try:
            cols = [
                row["name"] for row in self._conn.execute(f"PRAGMA table_info({table})").fetchall()
            ]
        except sqlite3.Error:
            return False
        return column in cols

    def _ensure_schema(self) -> None:
        """Create tables, FTS index, and triggers if they don't exist."""
        cur = self._conn.cursor()
        cur.executescript(_SCHEMA_SQL)
        cur.executescript(_FTS_SCHEMA_SQL)
        cur.executescript(_TRIGGER_SQL)
        cur.execute("PRAGMA user_version = 1")
        self._conn.commit()
        self._migrate_session_summaries_updated_at()
        self._migrate_session_summaries_branch()
        self._migrate_session_summaries_recency_indexes()

    def _migrate_session_summaries_updated_at(self) -> None:
        """Add updated_at column for DBs created before PMSERV-049.

        Idempotent: skips ALTER if the column already exists. Two heals then
        run on EVERY open, each a cheap no-op once the data is clean:

        - PMSERV-158 backfill: ``updated_at = created_at`` for NULL/empty rows.
          Older binaries whose INSERT omitted updated_at could leave
          post-migration rows NULL — which the original one-shot backfill
          (nested inside the ``if``) never healed.
        - PMSERV-160 clamp: a future ``updated_at`` (a save that ran while the
          system clock was ahead — NTP skew, restored VM snapshot) is clamped
          to now. Under the effective-timestamp order such a row would stay
          "latest" until real time caught up, whereas the bare-id order it
          replaced self-healed on the very next save; the clamp restores that
          property at open time. Runs after the backfill so a future
          created_at copied into updated_at is clamped in the same pass.
          Both sides of the clamp use the millisecond ``_MS_NOW_SQL``
          (PMSERV-161): with a second-precision comparator a legitimate row
          written at HH:MM:SS.mmm and healed within the same second compares
          ``'...SS.mmm' > '...SS'`` = TRUE and would be spuriously clamped,
          destroying its sub-second ordering.

        Always (re)creates the supporting index — safe because the column is
        guaranteed to exist after the ALTER guard.
        """
        cur = self._conn.cursor()
        cols = [
            row["name"] for row in cur.execute("PRAGMA table_info(session_summaries)").fetchall()
        ]
        if "updated_at" not in cols:
            cur.execute("ALTER TABLE session_summaries ADD COLUMN updated_at TEXT")
        cur.execute(
            "UPDATE session_summaries SET updated_at = created_at"
            " WHERE updated_at IS NULL OR updated_at = ''"
        )
        cur.execute(
            f"UPDATE session_summaries SET updated_at = {_MS_NOW_SQL}"
            f" WHERE updated_at > {_MS_NOW_SQL}"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_session_summaries_updated_at"
            " ON session_summaries(updated_at)"
        )
        self._conn.commit()

    def _migrate_session_summaries_branch(self) -> None:
        """Add branch column for branch-aware recall (PMSERV-124 / ADR-028).

        Idempotent: skips ALTER if the column already exists. Unlike the
        updated_at migration there is NO backfill — pre-feature rows
        legitimately have no branch (NULL), and
        ``get_latest_summary_by_branch`` falls back to the overall-latest for
        them so existing DBs keep working on day one. The composite index
        ``(branch, updated_at DESC)`` narrows the branch-scoped queries via its
        equality prefix; order is supplied by the expression indexes of
        :meth:`_migrate_session_summaries_recency_indexes` (PMSERV-162). This
        older index is deliberately KEPT: older binaries sharing the DB
        recreate it IF NOT EXISTS on every open, so dropping it here would
        churn (drop/recreate per alternating open) until the fleet is
        single-version.

        The column is added as plain nullable ``TEXT`` (no NOT NULL / DEFAULT)
        because SQLite forbids ``ALTER ... ADD COLUMN NOT NULL`` without a
        constant default; the fresh-DB DDL declares it nullable too so migrated
        and freshly-created DBs agree.
        """
        cur = self._conn.cursor()
        cols = [
            row["name"] for row in cur.execute("PRAGMA table_info(session_summaries)").fetchall()
        ]
        if "branch" not in cols:
            cur.execute("ALTER TABLE session_summaries ADD COLUMN branch TEXT")
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_session_summaries_branch"
            " ON session_summaries(branch, updated_at DESC)"
        )
        self._conn.commit()

    def _migrate_session_summaries_recency_indexes(self) -> None:
        """Expression indexes matching the effective-timestamp order (PMSERV-162).

        The recency reads order by ``COALESCE(NULLIF(updated_at, ''),
        created_at) DESC, id DESC`` (``_ts_expr``; PMSERV-158/159), which no
        plain column index can supply — without these, every recall does a
        full SCAN + TEMP B-TREE sort (measured 74.6µs @ 66 rows, 2.1ms @ 50k
        rows; session_summaries has no automatic pruning, so it only grows).
        SQLite matches indexed expressions structurally, so the reads use
        these by construction — the branch-prefixed one serves the
        ``branch =`` getter, the bare one serves the overall getters and the
        window query.

        Residual sorts are accepted: ``branch IN (...)`` queries
        (logical-track resolution) still finish with a TEMP B-TREE because
        SQLite cannot merge index-ordered output across IN probes — bounded
        to the per-branch slices, negligible (design review, PMSERV-162).

        Must run AFTER both column migrations (the expressions reference
        updated_at and branch). Runs only via ``_ensure_schema``, i.e. never
        on readonly opens — pre-updated_at DBs under Lens keep degrading
        ``_ts_expr`` to created_at and never see these indexes (ADR-028).
        Write amplification: two extra index updates per save — one UPSERT
        per session, negligible.

        Version floor: expression indexes need SQLite >= 3.9.0, which adds
        NOTHING for this store's writer — _ensure_schema already creates an
        FTS5 virtual table unconditionally (FTS5 shipped in the same 3.9.0)
        and save_session_summary uses UPSERT (>= 3.24), so any build that
        reaches this point clears 3.9 by construction (adversarial review
        refuted an earlier <3.9 runtime guard here as dead code). The real
        commitment is for READERS of the file: once these indexes exist,
        any tool linking SQLite < 3.9 fails with "malformed database
        schema" on the whole DB — recorded in ADR-043.
        """
        cur = self._conn.cursor()
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_session_summaries_effective_ts"
            " ON session_summaries(COALESCE(NULLIF(updated_at, ''), created_at) DESC, id DESC)"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_session_summaries_branch_ts"
            " ON session_summaries"
            "(branch, COALESCE(NULLIF(updated_at, ''), created_at) DESC, id DESC)"
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

    def search_full(
        self,
        query: str,
        type: str | None = None,
        limit: int = 5,
    ) -> tuple[list[Memory], SearchDiagnostics]:
        """Full-text search using FTS5, with a LIKE fallback when FTS finds nothing.

        PMSERV-143 (ADR-039 T5): ``memories_fts`` uses ``tokenize='unicode61'``,
        which segments CJK text on unicode category boundaries rather than real
        word boundaries. Multi-token/compound Japanese queries (e.g. "経営戦略",
        "2つのエンジン") can legitimately MATCH zero rows even though the
        characters are present in stored content — see
        ``docs/reports/ja-fts-baseline.md`` for the measured hit/miss numbers.
        The exact recall rate is SQLite-version-dependent (FTS5 tokenizer
        behaviour has shifted across releases); if ``sqlite3.sqlite_version``
        differs from the environment the baseline report was measured on,
        re-run ``tests/test_memory_ja_fts.py`` and update the baseline/report
        if the numbers moved.

        When the FTS5 MATCH query returns zero rows, this falls back to a
        substring scan (``content LIKE ? OR tags LIKE ?``, with ``%`` and
        ``_`` escaped) so those queries still surface something instead of a
        hard empty result. PMSERV-175: the query is split into terms (same
        rule as the MATCH path, see :func:`_split_query_terms`) and the terms
        are AND-ed, one ``(content LIKE ? OR tags LIKE ?)`` clause each.
        Before that change the ENTIRE query string was a single literal
        pattern, so a multi-word query hit only when those words appeared
        verbatim AND adjacent in stored text — e.g. "egress ホスティング形態"
        returned 0 rows even though both terms live in the same row, stored as
        "…ホスティング形態でegress…". The net was widest exactly where CJK FTS
        is weakest, i.e. it did not catch what it existed to catch.

        This is still a recall safety net, not a tokenizer fix — trigram
        tokenizer migration is intentionally out of scope here (see ADR-039
        AD-8) and tracked as a separate future issue (PMSERV-150).

        Args:
            query: Search query string.
            type: Filter by memory type.
            limit: Maximum results.

        Returns:
            ``(memories, diagnostics)``; see :class:`SearchDiagnostics`. The
            ``type`` filter is applied as a post-filter *after* LIMIT in both
            branches — matching this method's pre-existing (pre-T5) behaviour
            exactly, so switching strategies never changes when the type
            filter is applied.
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
        diag = SearchDiagnostics(strategy="fts")

        if not rows:
            rows, diag = self._like_fallback(
                query,
                limit,
                table="memories",
                conn=self._conn,
            )

        memories = [self._row_to_memory(r) for r in rows]
        if type:
            memories = [m for m in memories if m.type.value == type]
        return memories, diag

    @staticmethod
    def _like_fallback(
        query: str,
        limit: int,
        table: str,
        conn: sqlite3.Connection,
    ) -> tuple[list[sqlite3.Row], SearchDiagnostics]:
        """Run the AND-ed LIKE fallback and report which terms carried it.

        Shared by :meth:`search_full` and :meth:`search_global_full` so the
        per-project and cross-project paths cannot drift — they degrade for
        the same reason (unicode61 vs CJK) and must degrade the same way.

        ``table`` is an internal literal (``memories`` / ``memory_index``),
        never caller input, and the WHERE clause is a fixed string repeated
        per term; every value is bound. No user text reaches the SQL text.
        """
        terms = _split_query_terms(query)
        if not terms:
            # Whitespace-only query: preserve the pre-PMSERV-175 single-pattern
            # behaviour rather than inventing a new empty-query semantics.
            terms = [query]
        used, dropped = terms[:_MAX_FALLBACK_TERMS], terms[_MAX_FALLBACK_TERMS:]

        matched: list[str] = []
        unmatched: list[str] = []
        for term in used:
            pattern = _like_pattern(term)
            hit = conn.execute(
                f"""SELECT 1 FROM {table}
                    WHERE (content LIKE ? ESCAPE '\\' OR tags LIKE ? ESCAPE '\\')
                    LIMIT 1""",  # noqa: S608 - table is an internal literal
                (pattern, pattern),
            ).fetchone()
            (matched if hit else unmatched).append(term)

        clause = " AND ".join(
            ["(content LIKE ? ESCAPE '\\' OR tags LIKE ? ESCAPE '\\')"] * len(used)
        )
        params: list[object] = []
        for term in used:
            pattern = _like_pattern(term)
            params.extend((pattern, pattern))
        params.append(limit)
        rows = conn.execute(
            f"""SELECT * FROM {table}
                WHERE {clause}
                ORDER BY created_at DESC
                LIMIT ?""",  # noqa: S608 - clause is built from a fixed literal
            params,
        ).fetchall()

        return rows, SearchDiagnostics(
            strategy="like_fallback",
            fallback_reason="fts_no_match",
            matched_terms=tuple(matched),
            unmatched_terms=tuple(unmatched),
            dropped_terms=tuple(dropped),
        )

    def search_ex(
        self,
        query: str,
        type: str | None = None,
        limit: int = 5,
    ) -> tuple[list[Memory], str]:
        """Full-text search returning only the strategy label.

        Thin delegation to :meth:`search_full`, preserving the
        pre-PMSERV-175 two-tuple return for existing callers.

        Args:
            query: Search query string.
            type: Filter by memory type.
            limit: Maximum results.
        """
        memories, diag = self.search_full(query, type=type, limit=limit)
        return memories, diag.strategy

    def search(
        self,
        query: str,
        type: str | None = None,
        limit: int = 5,
    ) -> list[Memory]:
        """Full-text search using FTS5. Thin delegation to :meth:`search_ex`.

        Args:
            query: Search query string.
            type: Filter by memory type.
            limit: Maximum results.
        """
        return self.search_ex(query, type=type, limit=limit)[0]

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

        On first save: created_at defaults via the DDL; updated_at is written
        explicitly in the INSERT. PMSERV-158: the migration-added updated_at
        column has no default (SQLite ``ALTER`` cannot add ``NOT NULL DEFAULT``
        with a non-constant), so relying on the column default left
        single-saved rows NULL on migrated DBs — always set it here. Both
        writes use the millisecond ``_MS_NOW_SQL`` (PMSERV-161) so saves
        landing within the same wall-clock second still order by actual save
        time instead of degrading to the id tiebreak.
        On re-save (same session_id): preserves created_at, refreshes updated_at.
        Returns the row id of the inserted-or-updated row.
        """
        self._conn.execute(
            f"""INSERT INTO session_summaries
               (session_id, summary, goals, tasks_done, decisions, pending, project,
                branch, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, {_MS_NOW_SQL})
               ON CONFLICT(session_id) DO UPDATE SET
                   summary = excluded.summary,
                   goals = excluded.goals,
                   tasks_done = excluded.tasks_done,
                   decisions = excluded.decisions,
                   pending = excluded.pending,
                   updated_at = {_MS_NOW_SQL},
                   branch = COALESCE(NULLIF(excluded.branch, ''), session_summaries.branch)""",  # noqa: S608
            (
                summary.session_id,
                summary.summary,
                summary.goals,
                _list_to_json(summary.tasks_done),
                _list_to_json(summary.decisions),
                _list_to_json(summary.pending),
                summary.project,
                summary.branch,
            ),
        )
        self._conn.commit()
        # PMSERV-176: publish this row to immutable=1 Lens readers. The
        # session summary is THE continuity datum pm_recall returns at session
        # start, so a summary stranded in the WAL is precisely the failure
        # being guarded against — a new session silently restoring weeks-old
        # context. Checkpointing here (rather than on every save) keeps the
        # cost on the one write that matters for continuity.
        self.last_checkpoint_error = _checkpoint_passive(self._conn)
        row = self._conn.execute(
            "SELECT id FROM session_summaries WHERE session_id = ?",
            (summary.session_id,),
        ).fetchone()
        return row["id"]

    def get_summary(self, session_id: str) -> SessionSummary | None:
        """Return the summary saved under ``session_id``, if any."""
        row = self._conn.execute(
            "SELECT * FROM session_summaries WHERE session_id = ?", (session_id,)
        ).fetchone()
        return None if row is None else self._row_to_summary(row)

    def get_latest_summary(self) -> SessionSummary | None:
        """Get the most recent session summary (across all branches).

        "Most recent" means most recently *worked* — ordered by the effective
        timestamp with an id tiebreak, exactly like the branch-scoped getters
        (PMSERV-159). A bare ``id DESC`` would mean "most recently started":
        :meth:`save_session_summary` is an UPSERT that preserves id, so an
        older session re-saving after a newer one begins would be passed over
        by every no-track consumer — pm_recall's default path, the track-miss
        fallback, pm_session_summary get/list, and the context-inject CLI's
        Layer-1 (recall.py). Effective-
        timestamp ties keep the id DESC order, so single-save flows behave as
        before.
        """
        row = self._conn.execute(
            f"SELECT * FROM session_summaries"  # noqa: S608
            f" ORDER BY {self._ts_expr} DESC, id DESC LIMIT 1",
        ).fetchone()
        if row is None:
            return None
        return self._row_to_summary(row)

    def get_latest_summary_by_branch(self, branch: str) -> tuple[SessionSummary | None, bool]:
        """Latest summary for a git branch / track, with graceful fallback.

        Returns ``(summary, track_matched)`` where:

        - ``track_matched=True`` — a summary recorded on ``branch`` exists and
          is returned.
        - ``track_matched=False`` — no summary was recorded on ``branch`` yet,
          so we fall back to the overall-latest summary (or ``None`` if the DB
          is empty). This keeps branch-aware recall useful on day one: existing
          DBs predate the branch column, so every legacy row has
          ``branch IS NULL`` and would otherwise match nothing (PMSERV-124 /
          ADR-028).

        The branch-scoped query orders by the effective timestamp
        ``COALESCE(NULLIF(updated_at, ''), created_at) DESC, id DESC`` so "latest
        on this line" means *most recently worked*, not highest insert id —
        important because :meth:`save_session_summary` is an UPSERT that preserves
        the original id on re-save. PMSERV-158: the COALESCE guards against
        migrated-DB rows whose updated_at is NULL, which a bare ``updated_at
        DESC`` would sink below an older re-saved row (silent stale recall).
        """
        if not self._has_branch_col:
            # Old DB opened read-only under PM_LENS (migration could not run):
            # no branch column to filter on — degrade to the overall-latest
            # instead of raising OperationalError from a read-only tool.
            return self.get_latest_summary(), False
        row = self._conn.execute(
            f"SELECT * FROM session_summaries WHERE branch = ?"  # noqa: S608
            f" ORDER BY {self._ts_expr} DESC, id DESC LIMIT 1",
            (branch,),
        ).fetchone()
        if row is not None:
            return self._row_to_summary(row), True
        return self.get_latest_summary(), False

    def list_distinct_branches(self) -> list[str]:
        """Distinct non-empty branches recorded across session summaries.

        Used to resolve a logical track's branch globs (PMSERV-125): the small
        candidate set is glob-matched in Python (fnmatch) by the caller. Returns
        ``[]`` when the branch column is absent (old DB under read-only Lens).
        """
        if not self._has_branch_col:
            return []
        rows = self._conn.execute(
            "SELECT DISTINCT branch FROM session_summaries"
            " WHERE branch IS NOT NULL AND branch != ''"
        ).fetchall()
        return [row["branch"] for row in rows]

    def get_latest_summary_in_branches(
        self, branches: list[str]
    ) -> tuple[SessionSummary | None, bool]:
        """Latest summary across a set of branches (logical track resolution).

        Returns ``(summary, track_matched)``. ``track_matched=True`` when at
        least one summary exists on the given branches. Falls back to the
        overall-latest with ``track_matched=False`` when ``branches`` is empty
        or none match — mirroring :meth:`get_latest_summary_by_branch` so a
        logical track with no recorded work yet still yields useful context
        (PMSERV-125 / ADR-028 / SynapticLedger ADR-035). Orders by the effective
        timestamp ``COALESCE(NULLIF(updated_at, ''), created_at) DESC, id DESC``
        (most-recently-worked across the line; PMSERV-158 NULL-safe).
        """
        if branches and self._has_branch_col:
            placeholders = ",".join("?" for _ in branches)
            row = self._conn.execute(
                f"SELECT * FROM session_summaries WHERE branch IN ({placeholders})"  # noqa: S608
                f" ORDER BY {self._ts_expr} DESC, id DESC LIMIT 1",
                tuple(branches),
            ).fetchone()
            if row is not None:
                return self._row_to_summary(row), True
        return self.get_latest_summary(), False

    def list_summaries(self, limit: int = 10) -> list[SessionSummary]:
        """List session summaries, newest first.

        Newest-first by the effective timestamp (id DESC tiebreak), matching
        :meth:`get_latest_summary` — see there for the PMSERV-159 rationale.
        """
        rows = self._conn.execute(
            f"SELECT * FROM session_summaries"  # noqa: S608
            f" ORDER BY {self._ts_expr} DESC, id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [self._row_to_summary(r) for r in rows]

    def list_summaries_within(
        self,
        window_minutes: int = 30,
        limit: int = 10,
        branches: list[str] | None = None,
    ) -> list[SessionSummary]:
        """Return session summaries updated within the last N minutes (UTC).

        Window comparison uses SQLite ``datetime('now', '-N minutes')`` which
        evaluates in UTC. The effective timestamp
        ``COALESCE(NULLIF(updated_at, ''), created_at)`` reflects the latest save
        for each session, so still-active sessions are captured even when their
        initial summary was created long ago. PMSERV-158: the COALESCE is
        essential here — a bare ``updated_at >= …`` predicate EXCLUDES rows whose
        updated_at is NULL (``NULL >= x`` is not true), silently dropping recent
        single-save summaries on migrated DBs from ambiguity detection. When the
        column is absent (pre-PMSERV-049 read-only DB) ``_ts_expr`` degrades to
        ``created_at`` so no ``no such column`` error is raised. Boundary is
        inclusive: a summary updated exactly N minutes ago is included. Used by
        pm_recall ambiguity detection (PMSERV-049).

        ``branches`` (PMSERV-125): when given, restrict to summaries recorded on
        those branches — used to scope ambiguity detection to a single work line
        under ``pm_recall(track=...)``. An empty list matches nothing (returns
        ``[]``); ``None`` (default) is unscoped. Ignored if the branch column is
        absent (old DB under read-only Lens).

        Ties on the effective timestamp break by ``id DESC`` like every other
        recency read (PMSERV-161 — this was the one site missing the tiebreak,
        leaving same-second ties in scan order).
        """
        if branches is not None:
            if not branches or not self._has_branch_col:
                return []
            placeholders = ",".join("?" for _ in branches)
            rows = self._conn.execute(
                f"""SELECT * FROM session_summaries
                    WHERE {self._ts_expr} >= datetime('now', ?)
                      AND branch IN ({placeholders})
                    ORDER BY {self._ts_expr} DESC, id DESC LIMIT ?""",  # noqa: S608
                (f"-{window_minutes} minutes", *branches, limit),
            ).fetchall()
            return [self._row_to_summary(r) for r in rows]
        rows = self._conn.execute(
            f"""SELECT * FROM session_summaries
               WHERE {self._ts_expr} >= datetime('now', ?)
               ORDER BY {self._ts_expr} DESC, id DESC LIMIT ?""",  # noqa: S608
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
        branch = (row["branch"] if "branch" in row.keys() else None) or ""
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
            branch=branch,
        )

    # ─── Global cross-project sync ──────────────────

    def sync_to_global(self, memory: Memory, memory_id: int) -> None:
        """Sync a memory to the global cross-project index.

        Silently skips if global sync is disabled or fails.
        The per-project DB is the source of truth.
        """
        if self.global_db_path is None or self.global_readonly:
            return
        try:
            self.global_db_path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(self.global_db_path))
            _apply_pragmas(conn)
            conn.executescript(_GLOBAL_SCHEMA_SQL)
            conn.executescript(_GLOBAL_FTS_SQL)
            conn.executescript(_GLOBAL_TRIGGER_SQL)
            _ensure_global_columns(conn)
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

    def ingest_auto_memory(
        self,
        entries: list[dict],
        scanned_dirs: list[Path],
        dry_run: bool = False,
        present_but_skipped: list[str] | None = None,
    ) -> dict:
        """Index Claude Code auto-memory notes into the global index.

        PMSERV-156 / ADR-045. The rows land ONLY in ``memory_index`` — the
        derived cross-project index — never in a project's ``memories`` table,
        so ``pm_remember`` stays the single source of truth for PM knowledge
        and the ``.md`` files stay the source of truth for auto-memory
        (PMSERV-111's no-dual-write rule).

        Re-ingest is **DELETE then INSERT**, never ``UPDATE``: the global FTS5
        table is external-content with only ``memory_index_ai`` (after insert)
        and ``memory_index_ad`` (after delete) triggers. An ``UPDATE`` of
        ``content`` would leave the FTS row holding the previous text — a
        silent search inconsistency with no error anywhere.

        ``present_but_skipped`` lists source files that exist on disk but were
        deliberately not read this run — over the PMSERV-169 size caps, or
        unparseable. They are exempt from pruning: "absent from entries"
        otherwise reads as "the file is gone", and a resource guard that
        deletes the index rows of the notes it declined to re-read would be
        strictly worse than having no guard.

        Pruning is scoped to ``scanned_dirs``: a row survives unless its
        source file lived in a directory this call actually scanned (and the
        collector only lists a directory as scanned when READING it
        succeeded). Without that scoping a ``scope="project"`` ingest would
        delete every other project's indexed notes as "no longer present".

        ``dry_run`` is a pure preview: it never creates the DB file, never
        flips journal modes, and never migrates schema — it opens an existing
        file with a plain connection and only SELECTs (the original
        implementation ALTERed on dry_run; adversarial review, confirmed).
        The plan (existing-row scan) and the DELETE+INSERT run inside ONE
        ``BEGIN IMMEDIATE`` transaction: planning in autocommit reopens the
        PMSERV-162 TOCTOU class where a concurrent ingest's insert between
        plan and write leaves duplicate rows.
        """
        result: dict = {
            "ingested": 0,
            "unchanged": 0,
            "pruned": 0,
            "scanned_dirs": len(scanned_dirs),
            "projects": sorted({e.get("project") or "" for e in entries}),
            "dry_run": dry_run,
        }
        if self.global_db_path is None:
            result["error"] = "global index is not configured (global_db_path is None)"
            return result
        if self.global_readonly:
            # Defense-in-depth: the tool is not registered under PM_LENS=1,
            # but the store method must refuse on its own (PMSERV-144).
            result["error"] = "global index is read-only on this host (PM_LENS)"
            return result

        scanned = {str(Path(d).resolve()) for d in scanned_dirs}
        by_path = {e["source_path"]: e for e in entries}
        protected = {str(p) for p in (present_but_skipped or ())}

        def _plan(executor) -> tuple[list[int], list[int], list[dict], int]:
            existing: dict[str, tuple[int, str | None]] = {}
            try:
                rows = executor.execute(
                    "SELECT id, source_path, content_hash FROM memory_index WHERE source = ?",
                    (AUTO_MEMORY_SOURCE,),
                ).fetchall()
            except sqlite3.OperationalError:
                # Pre-ingest schema (no table / no provenance columns): no
                # auto-memory rows can exist yet.
                rows = []
            for row in rows:
                src = row[1] or ""
                if str(Path(src).parent) in scanned:
                    existing[src] = (row[0], row[2])
            # "Not in entries" normally means the file is gone. It can also
            # mean the collector refused to read it this run — an over-cap note
            # (PMSERV-169) or one that failed to parse. Those files still
            # exist, so pruning their rows would delete a good index entry as a
            # side effect of a resource guard.
            stale = [
                rid
                for src, (rid, _h) in existing.items()
                if src not in by_path and src not in protected
            ]
            replace: list[int] = []
            to_write: list[dict] = []
            unchanged = 0
            for src, entry in by_path.items():
                prev = existing.get(src)
                if prev is not None and prev[1] == entry.get("content_hash"):
                    unchanged += 1
                    continue
                if prev is not None:
                    replace.append(prev[0])
                to_write.append(entry)
            return stale, replace, to_write, unchanged

        conn: sqlite3.Connection | None = None
        try:
            if dry_run:
                if not self.global_db_path.exists():
                    result["ingested"] = len(by_path)
                    return result
                conn = sqlite3.connect(str(self.global_db_path))
                stale, _replace, to_write, unchanged = _plan(conn)
                result["ingested"] = len(to_write)
                result["unchanged"] = unchanged
                result["pruned"] = len(stale)
                return result

            self.global_db_path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(self.global_db_path))
            _apply_pragmas(conn)
            conn.executescript(_GLOBAL_SCHEMA_SQL)
            conn.executescript(_GLOBAL_FTS_SQL)
            conn.executescript(_GLOBAL_TRIGGER_SQL)
            _ensure_global_columns(conn)
            conn.commit()

            cur = conn.cursor()
            cur.execute("BEGIN IMMEDIATE")
            try:
                stale, replace, to_write, unchanged = _plan(cur)
                cur.executemany(
                    "DELETE FROM memory_index WHERE id = ?",
                    [(i,) for i in stale + replace],
                )
                cur.executemany(
                    """INSERT INTO memory_index
                       (project, project_path, memory_id, type, content, tags, task_id,
                        created_at, source, source_path, content_hash)
                       VALUES (?, ?, 0, ?, ?, ?, NULL, COALESCE(?, datetime('now')), ?, ?, ?)""",
                    [
                        (
                            e.get("project") or e.get("origin_dir") or "",
                            e.get("project_path") or e.get("origin_dir") or "",
                            # NOT the AUTO_MEMORY_SOURCE literal: a type that
                            # collides with the source column invents a fake
                            # "auto_memory" type category (the overlay
                            # honestly returns None for a type-less note).
                            e.get("type") or "unknown",
                            e.get("content") or "",
                            _tags_to_str(e.get("tags")),
                            e.get("created_at"),
                            AUTO_MEMORY_SOURCE,
                            e["source_path"],
                            e.get("content_hash"),
                        )
                        for e in to_write
                    ],
                )
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
            result["ingested"] = len(to_write)
            result["unchanged"] = unchanged
            result["pruned"] = len(stale)
            return result
        except (sqlite3.Error, OSError) as exc:
            result["error"] = f"{type(exc).__name__}: {exc}"
            return result
        finally:
            if conn is not None:
                conn.close()

    def purge_auto_memory(
        self,
        scanned_dirs: list[Path] | None = None,
        project_root: Path | str | None = None,
        dry_run: bool = False,
        vacuum: bool = False,
    ) -> dict:
        """Remove ingested auto-memory rows from the global index.

        The undo for :meth:`ingest_auto_memory` — ADR-045 requires one,
        because a single ``scope="all"`` run publishes other projects' notes
        and there must be a way to take that back. ``scanned_dirs=None`` with
        ``project_root=None`` purges every ingested row; otherwise a row is
        targeted when its source directory is in ``scanned_dirs`` OR its
        recorded ``project_path`` resolves to ``project_root``. The second
        clause matters because the source directory can vanish (encoding
        drift, a deleted repo): rows still carry ``project_path``, so a
        project-scoped purge keeps working instead of silently returning
        ``purged: 0`` and leaving ``scope="all"`` as the only recourse
        (adversarial review, two lenses independently).

        Rows promoted from project ledgers (``source='pm'``) are never
        touched, and DELETE (not UPDATE) keeps the external-content FTS in
        sync via ``memory_index_ad``. The match and the DELETE share one
        ``BEGIN IMMEDIATE`` transaction; ``dry_run`` reads under a plain
        transaction and performs no schema work at all.

        DELETE can leave the row's text readable in the file (PMSERV-171), for
        LARGE rows and on SOME builds — measured rather than assumed, because
        both the general claim and its opposite are false. A row that fits in
        one SQLite page is rewritten in place and the WAL checkpoint copies the
        post-delete version over the original, so nothing survives anywhere. A
        row that spills onto **overflow pages** (past ~4 KB) depends on the
        compiled ``PRAGMA secure_delete``: at ``2`` (fast — Apple's system
        SQLite) freed pages are NOT zeroed and keep their contents; at ``1``
        (full — typical Linux builds) they are. Free-form auto-memory notes
        routinely clear the size bar, which is precisely the content purge
        exists to take back — so on macOS this matters and on Linux it usually
        does not.

        ``vacuum=True`` follows the DELETE with ``VACUUM`` (rebuilds the file,
        so free pages are not carried over) and
        ``PRAGMA wal_checkpoint(TRUNCATE)`` (drops the WAL's copy). It runs
        AFTER the commit — VACUUM cannot execute inside a transaction — and is
        skipped entirely on ``dry_run`` and when nothing matched. VACUUM needs
        an exclusive lock, so a concurrent session can make it fail; that is
        reported in ``vacuum_error`` and does NOT fail the purge, because the
        rows really are gone and pretending otherwise would push callers to
        re-run a destructive operation. ``vacuumed`` says what actually
        happened.

        This reduces residual plaintext; it does not securely erase it. Copies
        outside this file — backups, Time Machine, an SSD's remapped blocks,
        an earlier ``.bak``, the source ``.md`` notes themselves — are
        untouched. Treat it as hygiene, not as a guarantee.
        """
        count_key = "would_purge" if dry_run else "purged"
        result: dict = {count_key: 0, "projects": [], "dry_run": dry_run}
        if self.global_db_path is None or not self.global_db_path.exists():
            return result
        if self.global_readonly:
            result["error"] = "global index is read-only on this host (PM_LENS)"
            return result
        conn: sqlite3.Connection | None = None
        try:
            conn = sqlite3.connect(str(self.global_db_path))
            if not dry_run:
                _apply_pragmas(conn)
            cur = conn.cursor()
            cur.execute("BEGIN" if dry_run else "BEGIN IMMEDIATE")
            try:
                try:
                    rows = cur.execute(
                        "SELECT id, source_path, project, project_path"
                        " FROM memory_index WHERE source = ?",
                        (AUTO_MEMORY_SOURCE,),
                    ).fetchall()
                except sqlite3.OperationalError:
                    rows = []  # pre-ingest schema — nothing was ever ingested
                if scanned_dirs is None and project_root is None:
                    matched = list(rows)
                else:
                    scanned = {str(Path(d).resolve()) for d in (scanned_dirs or [])}
                    root = str(Path(project_root).resolve()) if project_root is not None else None
                    matched = []
                    for r in rows:
                        src_parent = str(Path(r[1] or "").parent)
                        row_root = r[3] or ""
                        try:
                            row_root = str(Path(row_root).resolve()) if row_root else ""
                        except (OSError, RuntimeError, ValueError):
                            pass
                        if src_parent in scanned or (root is not None and row_root == root):
                            matched.append(r)
                result[count_key] = len(matched)
                result["projects"] = sorted({r[2] or "" for r in matched})
                if not dry_run and matched:
                    cur.executemany(
                        "DELETE FROM memory_index WHERE id = ?", [(r[0],) for r in matched]
                    )
                conn.commit()
            except BaseException:
                conn.rollback()
                raise

            # After the commit, never inside it: VACUUM cannot run in a
            # transaction. Nothing was deleted on a dry run or an empty match,
            # so there is nothing to reclaim and no reason to take an
            # exclusive lock.
            if vacuum and not dry_run and matched:
                result["vacuumed"] = False
                try:
                    conn.execute("VACUUM")
                    # VACUUM rebuilds the main file, but in WAL mode it does so
                    # THROUGH the WAL — and the pre-purge pages may still be
                    # sitting there too. Truncating is what actually drops
                    # them; without this the sidecar keeps the plaintext the
                    # caller asked to be rid of.
                    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                    result["vacuumed"] = True
                except sqlite3.Error as exc:
                    # The purge itself succeeded. Reporting this as an error
                    # would invite a re-run of a destructive operation to fix
                    # a reclamation step.
                    result["vacuum_error"] = f"{type(exc).__name__}: {exc}"
            return result
        except (sqlite3.Error, OSError) as exc:
            result["error"] = f"{type(exc).__name__}: {exc}"
            return result
        finally:
            if conn is not None:
                conn.close()

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
            keep_latest: Keep only the latest N memories, delete rest. Must be
                >= 1 (see below).
            session_id: Delete all memories from a specific session.
            dry_run: If True, return what would be deleted without deleting.

        ``keep_latest < 1`` is rejected rather than executed (PMSERV-164 —
        the floor :meth:`cleanup_summaries` already enforced was missing
        here). Both out-of-range values are traps rather than useful inputs:
        0 builds ``id NOT IN (SELECT ... LIMIT 0)``, an empty keep-set that
        matches EVERY memory and wipes the ledger, while SQLite reads a
        negative LIMIT as "no limit", so -1 quietly deletes nothing while
        reporting a successful prune. The check applies even alongside other
        criteria: they are ANDed, so keep_latest=0 still means "keep none of
        what the other filters matched".

        Returns:
            Dict with count of deleted (or would-be-deleted) memories.
        """
        if keep_latest is not None and keep_latest < 1:
            return {
                "deleted": 0,
                "dry_run": dry_run,
                "error": (
                    "keep_latest must be >= 1 (0 would delete every memory; "
                    "a negative value is read as 'no limit' by SQLite and deletes none)"
                ),
            }

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

        if dry_run:
            return {"would_delete": count, "dry_run": True}
        if count == 0:
            # Honest flag even when nothing matched: this used to return
            # dry_run=True, which made the composite pm_memory_cleanup
            # response claim "dry-run" while the summaries path had actually
            # deleted rows (PMSERV-162 adversarial review).
            return {"deleted": 0, "dry_run": False}

        # Delete from FTS first (triggers handle this, but be explicit for cleanup)
        cur.execute(f"DELETE FROM memories WHERE {where}", params)  # noqa: S608
        self._conn.commit()

        return {"deleted": count, "dry_run": False}

    def cleanup_summaries(
        self,
        keep_latest: int,
        dry_run: bool = False,
        window_minutes: int = 30,
        force: bool = False,
    ) -> dict:
        """Prune session_summaries down to the newest ``keep_latest`` rows.

        session_summaries was the only table never DELETEd (memories has
        :meth:`cleanup`), so it grows one row per session forever
        (PMSERV-162). "Newest" is the effective-timestamp order every recency
        read uses — ``(_ts_expr DESC, id DESC)``, NOT bare id or updated_at,
        which would reintroduce the PMSERV-158/159 bug class on the pruning
        path (an UPSERT-resaved row deleted in favor of a stale higher-id
        row).

        Protection rules — the call may therefore retain MORE than
        ``keep_latest`` rows:

        - The newest row of every branch group survives, with NULL/'' branch
          as one pseudo-group. This keeps branch-aware recall (track=) and
          tracks.yaml glob resolution (:meth:`list_distinct_branches`) able
          to return each line's last context, including on non-git projects
          where every row has no branch.
        - ``keep_latest < 1`` is rejected: on an all-NULL-branch DB the
          per-branch protection is a single pseudo-group, so 0 would delete
          every summary and permanently erase pm_recall's context.

        Deletion is irreversible in a subtle way: session_id is UNIQUE and
        :meth:`save_session_summary` is an id-preserving UPSERT, so a pruned
        session that later re-saves is re-INSERTed with a fresh id and
        created_at — its history restarts. The DB file does not shrink (no
        VACUUM here).

        Returns counts plus ``recent_deleted`` / ``recent_would_delete`` —
        deletions whose effective timestamp falls inside the ambiguity
        window (``window_minutes``, the tool layer passes the same
        env-configurable value pm_recall's detection uses): pruning those
        silently disables concurrent-session detection, so the tool layer
        surfaces a warnings[] entry.

        Those recent deletions are also a **gate**, not just a notice
        (PMSERV-163): with ``force=False`` (the default) a real run whose
        delete set touches the window refuses entirely — ``blocked: True``,
        ``deleted: 0``, nothing removed — and reports ``recent_blocking`` /
        ``blocked_would_delete``. The warning alone was post-hoc: a direct
        ``dry_run=False`` call destroyed a concurrent session's context and
        only then said so, which is no safeguard for an irreversible act.
        The refusal is all-or-nothing because the delete set is one
        predicate; ``force=True`` executes it unchanged (the warning then
        applies as before). ``dry_run`` never blocks — it reports
        ``would_block`` so a preview predicts the refusal instead of
        walking the caller into a surprise. The gate is evaluated on the
        same ``window_minutes`` as the count, inside the same transaction as
        the DELETE it guards, so a save landing mid-call cannot slip past
        the check.

        Concurrency (adversarial review of PMSERV-162): the delete set is a
        single SQL predicate (top-N subquery + correlated branch-latest
        EXISTS, one bound variable total) evaluated inside one
        ``BEGIN IMMEDIATE`` transaction with the counts. A snapshot-then-
        ``DELETE ... id NOT IN (<ids>)`` implementation had two confirmed
        defects: a concurrent process's save landing between the snapshot
        and the DELETE was itself deleted (breaking the per-branch survival
        guarantee), and the id list could exceed
        SQLITE_MAX_VARIABLE_NUMBER (999 on pre-3.32 builds) and abort every
        query including the dry-run count.
        """
        count_key = "would_delete" if dry_run else "deleted"
        recent_key = "recent_would_delete" if dry_run else "recent_deleted"
        if keep_latest < 1:
            return {
                count_key: 0,
                recent_key: 0,
                "protected": 0,
                "dry_run": dry_run,
                "error": "keep_latest must be >= 1 (0 would delete every summary)",
            }
        ts = self._ts_expr

        def ts_of(alias: str) -> str:
            if self._has_updated_at_col:
                return f"COALESCE(NULLIF({alias}.updated_at, ''), {alias}.created_at)"
            return f"{alias}.created_at"

        # Delete-eligible = NOT in the newest keep_latest rows AND NOT the
        # newest row of its branch group (a strictly newer same-group row
        # exists). Exactly one bound variable (the LIMIT) regardless of
        # table size or keep_latest.
        predicate = (
            f"id NOT IN (SELECT id FROM session_summaries ORDER BY {ts} DESC, id DESC LIMIT ?)"
        )
        if self._has_branch_col:
            ts_outer = ts_of("session_summaries")
            ts_inner = ts_of("t")
            predicate += (
                f" AND EXISTS (SELECT 1 FROM session_summaries AS t"
                f" WHERE COALESCE(NULLIF(t.branch, ''), '') ="
                f" COALESCE(NULLIF(session_summaries.branch, ''), '')"
                f" AND ({ts_inner} > {ts_outer}"
                f" OR ({ts_inner} = {ts_outer} AND t.id > session_summaries.id)))"
            )
        cur = self._conn.cursor()
        # BEGIN IMMEDIATE takes the write lock up front so the counts and
        # the DELETE see one consistent state and no concurrent save can
        # slip in between them; the dry-run path uses a plain transaction
        # for a consistent read snapshot without blocking writers.
        cur.execute("BEGIN" if dry_run else "BEGIN IMMEDIATE")
        try:
            total = cur.execute("SELECT COUNT(*) FROM session_summaries").fetchone()[0]
            count = cur.execute(
                f"SELECT COUNT(*) FROM session_summaries WHERE {predicate}",  # noqa: S608
                (keep_latest,),
            ).fetchone()[0]
            recent = cur.execute(
                f"SELECT COUNT(*) FROM session_summaries"  # noqa: S608
                f" WHERE {predicate} AND {ts} >= datetime('now', ?)",
                (keep_latest, f"-{window_minutes} minutes"),
            ).fetchone()[0]
            # Gate inside the same transaction as the DELETE it guards, so a
            # concurrent save cannot land between the check and the write.
            blocked = bool(recent) and not force and not dry_run
            if not dry_run and not blocked:
                cur.execute(
                    f"DELETE FROM session_summaries WHERE {predicate}",  # noqa: S608
                    (keep_latest,),
                )
            self._conn.commit()
        except BaseException:
            self._conn.rollback()
            raise
        if blocked:
            return {
                count_key: 0,
                recent_key: 0,
                "protected": total,
                "dry_run": False,
                "blocked": True,
                "recent_blocking": recent,
                "blocked_would_delete": count,
                "error": (
                    f"{recent} summary/summaries inside the {window_minutes}-minute "
                    "ambiguity window would be pruned; nothing was deleted. "
                    "Pass force=True to override."
                ),
            }
        result = {
            count_key: count,
            recent_key: recent,
            "protected": total - count,
            "dry_run": dry_run,
        }
        if dry_run:
            result["would_block"] = bool(recent) and not force
        else:
            result["blocked"] = False
        return result

    def search_global_full(
        self,
        query: str,
        limit: int = 10,
    ) -> tuple[list[dict], SearchDiagnostics]:
        """Cross-project search with a LIKE fallback when FTS finds nothing.

        PMSERV-153 (ADR-039 followup): the cross-project sibling of
        :meth:`search_full`. ``memory_index_fts`` uses the same
        ``tokenize='unicode61'`` as the per-project ``memories_fts``, so the
        identical CJK caveat applies — compound Japanese queries (e.g.
        "経営戦略") can MATCH zero rows even though the characters are present.
        See ``docs/reports/ja-fts-baseline.md`` (cross-project section) for the
        measured hit/miss numbers, and ``tests/test_memory_ja_fts.py`` for the
        golden-query lock; both are SQLite-version-dependent.

        When the FTS5 MATCH returns zero rows, fall back to a substring scan
        over ``memory_index`` via the shared :meth:`_like_fallback` — the same
        AND-ed, per-term scan the per-project path uses (PMSERV-175), so both
        indexes degrade identically. A recall safety net, not a tokenizer fix
        (trigram migration is PMSERV-150, out of scope here).

        Returns:
            ``(results, diagnostics)``; see :class:`SearchDiagnostics`. The
            graceful-degradation empty results (global index absent, or a
            ``sqlite3.Error``) keep ``strategy="fts"`` for backward
            compatibility — ``fallback_reason`` is what distinguishes them
            from a genuine empty FTS hit.
        """
        if self.global_db_path is None or not self.global_db_path.exists():
            return [], SearchDiagnostics(strategy="fts", fallback_reason="global_index_absent")
        conn: sqlite3.Connection | None = None
        try:
            if self.global_readonly:
                # Lens host: read the shared index without side effects —
                # mode=ro&immutable=1 creates no -wal/-shm sidecars in ~/.pm
                # (RO invariant, ADR-028). Trade-off: un-checkpointed WAL
                # content is invisible until the owning writer checkpoints,
                # exactly like the Lens project-DB reads.
                conn = sqlite3.connect(f"file:{self.global_db_path}?mode=ro&immutable=1", uri=True)
                conn.row_factory = sqlite3.Row
            else:
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
            diag = SearchDiagnostics(strategy="fts")
            if not rows:
                rows, diag = self._like_fallback(
                    query,
                    limit,
                    table="memory_index",
                    conn=conn,
                )
            # Provenance is optional: a global index that predates PMSERV-156
            # has no source columns, and this is a read path that must not
            # raise on an un-migrated DB (ingest is what migrates it).
            has_source = bool(rows) and "source" in rows[0].keys()
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
                    **(
                        {"source": r["source"] or "pm", "source_path": r["source_path"]}
                        if has_source
                        else {}
                    ),
                }
                for r in rows
            ], diag
        except sqlite3.Error:
            return [], SearchDiagnostics(strategy="fts", fallback_reason="fts_error")
        finally:
            if conn is not None:
                conn.close()

    def search_global_ex(
        self,
        query: str,
        limit: int = 10,
    ) -> tuple[list[dict], str]:
        """Cross-project search returning only the strategy label.

        Thin delegation to :meth:`search_global_full`, preserving the
        pre-PMSERV-175 two-tuple return for existing callers.
        """
        results, diag = self.search_global_full(query, limit=limit)
        return results, diag.strategy

    def search_global(
        self,
        query: str,
        limit: int = 10,
    ) -> list[dict]:
        """Search across all projects using the global index.

        Thin delegation to :meth:`search_global_ex` that drops the strategy
        label, preserving the pre-PMSERV-153 list-only return for existing
        callers. Returns dicts with project info included.
        """
        return self.search_global_ex(query, limit=limit)[0]
