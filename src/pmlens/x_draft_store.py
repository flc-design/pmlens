"""Per-project X-content draft staging store (PMSERV-113, ADR-024).

Append-only staging buffer for build-in-public X drafts derived from ``.pm``
by-products (lessons / insights / ADRs / defects). Lives at
``<project>/.pm/x_drafts.db`` — **per-project**, NOT the global
``~/.pm/desktop/desktop.db`` outbox — so accounts/projects never co-mingle.

Safety model (ADR-024): pm-server holds NO X credentials and performs NO
network / post action, so "never auto-post" is a *structural* fact, not a
gate. The human reviews redacted drafts and posts manually on X, outside the
system. This store therefore enforces only the invariants that actually
matter:

* **Provenance is immutable** — an append-only trigger freezes the columns
  that tie a draft back to its ``.pm`` source (so a draft is always
  reconstructible from the SSoT and can never silently rewrite its origin).
* **The review queue exposes ONLY redacted fields** — :meth:`pending` never
  returns ``raw_content`` or the un-redacted ``hook``/``body_json`` (PMSERV-113
  cross-check must-fix #1). The human copy-pastes from the queue, so the queue
  must never carry the unscrubbed concentrate.

Cloned from :class:`pmlens.outbox.DesktopOutboxStore`, but with a
**db_path-keyed factory** (mirroring ``server._memory_stores``) instead of the
outbox's single-slot module global, because a process may touch several
projects' stores (cross-check must-fix #2).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Literal

from .memory import _apply_pragmas

# Frozen enums. SQLite cannot ALTER a CHECK constraint and there is no
# migration framework (user_version pinned at 1), so the value sets below are
# fixed at first ship — trimmed to only the states the code can actually
# produce (cross-check must-fix #4). In particular there is no 'approved'
# status and no 'skill_passed'/'cleared' redaction_status: the simplified
# design (ADR-024) has no approval transition for those to come from.
XDraftStatus = Literal["draft", "redacted", "rejected", "posted"]
XDraftSignalType = Literal["lesson", "insight", "adr", "mistake"]
XDraftKind = Literal["single", "thread"]
RedactionStatus = Literal["unreviewed", "auto_flagged"]

_VALID_SIGNAL_TYPES: frozenset[str] = frozenset({"lesson", "insight", "adr", "mistake"})
_VALID_STATUSES: frozenset[str] = frozenset({"draft", "redacted", "rejected", "posted"})
_VALID_KINDS: frozenset[str] = frozenset({"single", "thread"})

# Columns safe to expose in the review queue / any postable surface. Note the
# DELIBERATE omissions: ``raw_content`` (the unscrubbed concentrate) and the
# pre-redaction ``hook``/``body_json``. Listing columns explicitly — rather
# than ``SELECT *`` — is what structurally enforces must-fix #1.
_SAFE_COLUMNS: tuple[str, ...] = (
    "id",
    "workflow_id",
    "signal_type",
    "source_refs",
    "kind",
    "status",
    "redaction_status",
    "redacted_hook",
    "redacted_body_json",
    "hashtags",
    "redaction_report",
    "reject_reason",
    "edit_notes",
    "created_at",
    "posted_at",
)
_SAFE_SELECT: str = ", ".join(_SAFE_COLUMNS)

# Statuses that count as "still in the review pipeline" — used for the pending
# diagnostic and for source-refs dedupe (a rejected draft does not block a
# re-draft of the same source).
_LIVE_STATUSES: tuple[str, ...] = ("draft", "redacted", "posted")


def default_x_draft_db_path(pm_path: Path) -> Path:
    """Resolve ``<pm_path>/x_drafts.db`` (per-project, beside ``memory.db``)."""
    return Path(pm_path) / "x_drafts.db"


# ─── Schema SQL (PRAGMA user_version=1, independent of memory.db versioning) ──

_SCHEMA_SQL = """\
CREATE TABLE IF NOT EXISTS x_drafts (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    workflow_id        TEXT,
    signal_type        TEXT NOT NULL
                       CHECK(signal_type IN ('lesson', 'insight', 'adr', 'mistake')),
    source_refs        TEXT NOT NULL,
    kind               TEXT NOT NULL DEFAULT 'thread'
                       CHECK(kind IN ('single', 'thread')),
    raw_content        TEXT NOT NULL,
    hook               TEXT,
    body_json          TEXT,
    redacted_hook      TEXT,
    redacted_body_json TEXT,
    hashtags           TEXT,
    status             TEXT NOT NULL DEFAULT 'draft'
                       CHECK(status IN ('draft', 'redacted', 'rejected', 'posted')),
    redaction_status   TEXT NOT NULL DEFAULT 'unreviewed'
                       CHECK(redaction_status IN ('unreviewed', 'auto_flagged')),
    redaction_report   TEXT,
    reject_reason      TEXT,
    edit_notes         TEXT,
    created_at         TEXT NOT NULL DEFAULT (datetime('now')),
    posted_at          TEXT
);

CREATE INDEX IF NOT EXISTS idx_x_drafts_status_created
    ON x_drafts(status, created_at);
CREATE INDEX IF NOT EXISTS idx_x_drafts_signal_status
    ON x_drafts(signal_type, status);
CREATE INDEX IF NOT EXISTS idx_x_drafts_source_refs
    ON x_drafts(source_refs);
"""

# Append-only trigger: any UPDATE touching a provenance column raises and
# aborts. Permitted-to-mutate columns (hook / body_json / redacted_* /
# hashtags / status / redaction_status / redaction_report / reject_reason /
# edit_notes / posted_at) are excluded from the OF list so legitimate
# draft-edit and state transitions succeed. Mirrors trg_outbox_append_only but
# scoped to provenance only (cross-check: freeze source, allow in-place review
# edits before the human posts).
#
# Post-freeze trigger (PMSERV-121): once a draft is 'posted', its scrubbed,
# already-published content (redacted_hook / redacted_body_json /
# redaction_report) is frozen too — you cannot silently rewrite what was made
# public. This is defense-in-depth: the API never re-redacts a posted row
# (set_redacted requires status='draft'), and mark_posted touches only
# status/posted_at (so it does NOT trip this trigger), but a raw UPDATE could —
# this closes that gap. CREATE TRIGGER IF NOT EXISTS is additive, so existing
# x_drafts.db files gain the trigger on next open (no migration; user_version
# stays 1).
_TRIGGER_SQL = """\
CREATE TRIGGER IF NOT EXISTS trg_x_drafts_append_only
BEFORE UPDATE OF
    id, workflow_id, signal_type, source_refs, raw_content, created_at
ON x_drafts
BEGIN
    SELECT RAISE(ABORT, 'x_drafts provenance is append-only');
END;

CREATE TRIGGER IF NOT EXISTS trg_x_drafts_freeze_posted
BEFORE UPDATE OF redacted_hook, redacted_body_json, redaction_report
ON x_drafts
WHEN OLD.status = 'posted'
BEGIN
    SELECT RAISE(ABORT, 'x_drafts redacted_* is frozen once posted');
END;
"""


def _ensure_schema(db_path: Path) -> None:
    """Create the x_drafts schema if missing.

    ``PRAGMA page_size=8192`` must be set before any write commits the DB
    header (it is sticky thereafter); we detect first-init via file
    non-existence and apply it first. Mirrors ``outbox._ensure_schema``.
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


def normalize_source_refs(source_refs: list[str]) -> str:
    """Normalize a list of source refs into a stable comma string for dedupe.

    Refs are stripped, de-duplicated, and sorted so that the same set of
    sources always produces the same stored key regardless of input order —
    this is the dedupe key ``pm_draft_x`` checks against (cross-check:
    "spec the dedupe key explicitly").
    """
    cleaned = sorted({ref.strip() for ref in source_refs if ref and ref.strip()})
    return ",".join(cleaned)


class XDraftStore:
    """Per-project append-only staging store for X-content drafts.

    Lifecycle: ``append`` (status=draft) → ``set_redacted`` (status=redacted)
    → human reviews via the queue and posts manually → optional ``mark_posted``
    (bookkeeping only). ``mark_rejected`` discards at any pre-post stage with a
    mandatory reason. Provenance columns are immutable after insert.
    """

    def __init__(self, db_path: Path) -> None:
        self.db_path: Path = Path(db_path)
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
        signal_type: XDraftSignalType,
        source_refs: str,
        raw_content: str,
        hook: str | None = None,
        body_json: str | None = None,
        kind: XDraftKind = "thread",
        hashtags: str | None = None,
        workflow_id: str | None = None,
    ) -> int:
        """Append a new draft (status=draft). Returns the new row id.

        ``source_refs`` should already be normalized via
        :func:`normalize_source_refs`. ``raw_content`` is the unscrubbed
        concentrate assembled from ``.pm``; it is frozen by the trigger and
        never surfaced by :meth:`pending`.
        """
        if signal_type not in _VALID_SIGNAL_TYPES:
            raise ValueError(
                f"invalid signal_type: {signal_type!r} "
                f"(expected one of {sorted(_VALID_SIGNAL_TYPES)})"
            )
        if kind not in _VALID_KINDS:
            raise ValueError(f"invalid kind: {kind!r} (expected one of {sorted(_VALID_KINDS)})")
        conn = self._connect()
        try:
            cur = conn.execute(
                """
                INSERT INTO x_drafts
                    (workflow_id, signal_type, source_refs, kind,
                     raw_content, hook, body_json, hashtags)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    workflow_id,
                    signal_type,
                    source_refs,
                    kind,
                    raw_content,
                    hook,
                    body_json,
                    hashtags,
                ),
            )
            conn.commit()
            return int(cur.lastrowid or 0)
        finally:
            conn.close()

    def set_redacted(
        self,
        id: int,
        redacted_hook: str,
        redacted_body_json: str,
        redaction_report: str,
        flagged: bool,
    ) -> bool:
        """Transition draft → redacted, writing the scrubbed fields.

        ``flagged`` reflects whether redaction raised findings the human should
        review: True → redaction_status='auto_flagged', False → 'unreviewed'
        (status=redacted already conveys that redaction ran, so 'unreviewed'
        here means "redacted, nothing auto-flagged"). Returns True if updated,
        False if the row was not in 'draft' (idempotent skip).
        """
        new_redaction_status = "auto_flagged" if flagged else "unreviewed"
        conn = self._connect()
        try:
            cur = conn.execute(
                """
                UPDATE x_drafts
                   SET status = 'redacted',
                       redacted_hook = ?,
                       redacted_body_json = ?,
                       redaction_report = ?,
                       redaction_status = ?
                 WHERE id = ? AND status = 'draft'
                """,
                (redacted_hook, redacted_body_json, redaction_report, new_redaction_status, id),
            )
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()

    def mark_rejected(self, id: int, reason: str) -> bool:
        """Discard a draft (status → rejected) with a mandatory reason.

        Allowed from 'draft' or 'redacted'. Returns True if updated, False if
        already rejected/posted (idempotent skip).
        """
        if not reason or not reason.strip():
            raise ValueError("reason is required for rejection")
        conn = self._connect()
        try:
            cur = conn.execute(
                """
                UPDATE x_drafts
                   SET status = 'rejected',
                       reject_reason = ?
                 WHERE id = ? AND status IN ('draft', 'redacted')
                """,
                (reason, id),
            )
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()

    def mark_posted(self, id: int) -> bool:
        """Stamp a human-set 'posted' marker (bookkeeping only, NOT a control).

        Allowed only from 'redacted'. Carries no network/credential meaning —
        the human posts on X manually; this just records that they did.
        Returns True if updated, False otherwise (idempotent skip).
        """
        conn = self._connect()
        try:
            cur = conn.execute(
                """
                UPDATE x_drafts
                   SET status = 'posted',
                       posted_at = datetime('now')
                 WHERE id = ? AND status = 'redacted'
                """,
                (id,),
            )
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()

    # ─── Reads ──────────────────────────────────────────

    def get(self, id: int) -> dict | None:
        """Fetch a full row by id (INCLUDING raw_content), or None.

        For INTERNAL pipeline use only (e.g. ``pm_redact_draft`` reads
        ``hook``/``body_json`` to scrub them). The tool layer must NOT return
        ``raw_content`` to the user — use :meth:`pending` for any user-facing
        listing.
        """
        conn = self._connect()
        try:
            row = conn.execute("SELECT * FROM x_drafts WHERE id = ?", (id,)).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def pending(
        self,
        filter_status: str = "redacted",
        limit: int = 50,
        offset: int = 0,
    ) -> dict:
        """Paginated review queue — exposes ONLY safe columns (must-fix #1).

        ``raw_content`` and the un-redacted ``hook``/``body_json`` are never in
        the projection, so a caller cannot copy-paste unscrubbed text from the
        queue. ``filter_status='all'`` lists across every status.

        Returns {"items": [...], "total": N, "has_more": bool, "next_offset": int}.
        """
        if limit < 0 or offset < 0:
            raise ValueError("limit and offset must be non-negative")
        if filter_status != "all" and filter_status not in _VALID_STATUSES:
            raise ValueError(f"invalid filter_status: {filter_status!r}")

        clauses: list[str] = []
        params: list[object] = []
        if filter_status != "all":
            clauses.append("status = ?")
            params.append(filter_status)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""

        conn = self._connect()
        try:
            total = int(conn.execute(f"SELECT COUNT(*) FROM x_drafts{where}", params).fetchone()[0])
            rows = conn.execute(
                f"SELECT {_SAFE_SELECT} FROM x_drafts{where} "
                "ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
                [*params, limit, offset],
            ).fetchall()
        finally:
            conn.close()

        items = [dict(r) for r in rows]
        next_offset = offset + len(items)
        # ``has_more`` must imply the page advanced, or a caller paginating on
        # next_offset spins forever. With limit=0 the query returns zero rows so
        # next_offset == offset (no progress) while rows remain — guard against
        # that by requiring a non-empty page (PMSERV-121). We keep limit=0 a
        # VALID input (count-only probe) for parity with the outbox, rather than
        # rejecting it at the boundary; we just never claim "more" on a 0-row page.
        return {
            "items": items,
            "total": total,
            "has_more": bool(items) and next_offset < total,
            "next_offset": next_offset,
        }

    def find_live_by_source_refs(self, source_refs: str) -> list[dict]:
        """Return non-rejected drafts that share the exact normalized source_refs.

        Used by ``pm_draft_x`` to dedupe: if a draft/redacted/posted row already
        exists for the same sources, a re-trigger (e.g. after a compaction-
        induced re-record of the same lesson) should skip-with-warning rather
        than create a duplicate. Safe columns only.
        """
        placeholders = ", ".join("?" for _ in _LIVE_STATUSES)
        conn = self._connect()
        try:
            rows = conn.execute(
                f"SELECT {_SAFE_SELECT} FROM x_drafts "
                f"WHERE source_refs = ? AND status IN ({placeholders}) "
                "ORDER BY id DESC",
                [source_refs, *_LIVE_STATUSES],
            ).fetchall()
        finally:
            conn.close()
        return [dict(r) for r in rows]

    def recent_live_drafts(self, within_seconds: int) -> list[dict]:
        """Return live (non-rejected) drafts created within the last N seconds.

        Used by ``pm_draft_x``'s debounce (PMSERV-121): if the session just
        produced a draft, a second *distinct* draft is suppressed (nudging the
        author to batch this session's lessons into one) unless forced. Safe
        columns only. ``created_at`` is a UTC ``'YYYY-MM-DD HH:MM:SS'`` string,
        so a lexical ``>=`` against ``datetime('now', '-N seconds')`` is a valid
        recency test.
        """
        if within_seconds < 0:
            raise ValueError("within_seconds must be non-negative")
        placeholders = ", ".join("?" for _ in _LIVE_STATUSES)
        conn = self._connect()
        try:
            rows = conn.execute(
                f"SELECT {_SAFE_SELECT} FROM x_drafts "
                f"WHERE status IN ({placeholders}) "
                "AND created_at >= datetime('now', ?) "
                "ORDER BY id DESC",
                [*_LIVE_STATUSES, f"-{within_seconds} seconds"],
            ).fetchall()
        finally:
            conn.close()
        return [dict(r) for r in rows]

    def get_pending_count(self) -> int:
        """Count drafts still awaiting review (status in draft/redacted).

        Used by the pm_status x_drafts_pending diagnostic.
        """
        conn = self._connect()
        try:
            return int(
                conn.execute(
                    "SELECT COUNT(*) FROM x_drafts WHERE status IN ('draft', 'redacted')"
                ).fetchone()[0]
            )
        finally:
            conn.close()

    def close(self) -> None:
        """No-op for the short-lived connection design; symmetry with
        DesktopOutboxStore / MemoryStore and a clear factory shutdown hook."""
        return None


# ─── db_path-keyed factory (cross-check must-fix #2) ───────────────────────
# Unlike outbox's single-slot module global, x_drafts are per-project: one
# process may legitimately serve several projects' stores, so the cache is
# keyed by resolved db_path (mirrors server._memory_stores: dict[str, ...]).

_x_draft_stores: dict[str, XDraftStore] = {}


def get_x_draft_store(db_path: Path) -> XDraftStore:
    """Return the per-db_path XDraftStore, creating it on first use.

    Keyed by the resolved db_path so projectA's drafts never surface under
    projectB within one process.
    """
    # Resolve so two spellings of the same file (symlinks, '..' segments) map
    # to ONE store — mirrors server._memory_stores' resolved keying. Without
    # this the per-project isolation guarantee can be silently violated.
    key = str(Path(db_path).resolve())
    if key not in _x_draft_stores:
        _x_draft_stores[key] = XDraftStore(Path(db_path))
    return _x_draft_stores[key]


def clear_x_draft_store() -> None:
    """Reset the whole factory cache (ALL keys).

    Required by pytest fixtures so tests do not leak x_drafts.db handles across
    cases — a single-key clear would leave other projects' stale handles
    pointing at deleted tmp_path dirs (cross-check must-fix #2).
    """
    for store in _x_draft_stores.values():
        store.close()
    _x_draft_stores.clear()
