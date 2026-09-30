"""Tests for DraftStore (PMSERV-113 / PMSERV-114, ADR-024).

Cloned-from-outbox staging store for publication drafts. The two load-bearing
guarantees from the discovery cross-check (memory:192) get dedicated tests:

* must-fix #1 — :meth:`DraftStore.pending` never exposes ``raw_content`` or
  the un-redacted ``hook``/``body_json`` (the human copy-pastes from the queue).
* must-fix #2 — the factory is keyed by db_path, so two projects get two
  distinct stores in one process (NOT the outbox single-slot behavior).
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from pmlens.draft_store import (
    DraftStore,
    DraftStoreConflictError,
    clear_draft_store,
    default_draft_db_path,
    get_draft_store,
    normalize_source_refs,
)


@pytest.fixture(autouse=True)
def _isolate_draft_factory():
    """Reset the module-level factory cache between tests."""
    clear_draft_store()
    yield
    clear_draft_store()


@pytest.fixture
def store(tmp_path: Path) -> DraftStore:
    """A fresh DraftStore backed by a temp file per test."""
    return DraftStore(tmp_path / ".pm" / "drafts.db")


def _append_draft(store: DraftStore, **kw) -> int:
    """Append a draft with sensible defaults."""
    params = dict(
        signal_type="lesson",
        source_refs="memory:190",
        raw_content="raw concentrate",
        hook="hook text",
        body_json=json.dumps(["seg1", "seg2"]),
        kind="thread",
        hashtags="#buildinpublic",
        workflow_id="WF-031",
    )
    params.update(kw)
    return store.append(**params)


# ─── Schema / PRAGMA ────────────────────────────────


def test_schema_creates_table_indexes_and_trigger(tmp_path: Path) -> None:
    db = tmp_path / "drafts.db"
    DraftStore(db)
    conn = sqlite3.connect(str(db))
    try:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "x_drafts" in tables
        indexes = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_x_drafts%'"
            )
        }
        assert indexes == {
            "idx_x_drafts_status_created",
            "idx_x_drafts_signal_status",
            "idx_x_drafts_source_refs",
        }
        triggers = {
            r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger'")
        }
        assert "trg_x_drafts_append_only" in triggers
        assert "trg_x_drafts_freeze_posted" in triggers
    finally:
        conn.close()


def test_pragmas_page_size_wal_user_version(tmp_path: Path) -> None:
    db = tmp_path / "drafts.db"
    DraftStore(db)
    conn = sqlite3.connect(str(db))
    try:
        assert conn.execute("PRAGMA page_size").fetchone()[0] == 8192
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
    finally:
        conn.close()


def test_ensure_schema_is_idempotent(tmp_path: Path) -> None:
    db = tmp_path / "drafts.db"
    DraftStore(db)
    DraftStore(db)  # second init must not error or duplicate objects
    conn = sqlite3.connect(str(db))
    try:
        triggers = {
            r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger'")
        }
        assert triggers == {"trg_x_drafts_append_only", "trg_x_drafts_freeze_posted"}
    finally:
        conn.close()


# ─── append / validation ────────────────────────────


def test_append_returns_increasing_ids(store: DraftStore) -> None:
    id1 = _append_draft(store)
    id2 = _append_draft(store, source_refs="memory:191")
    assert id1 >= 1
    assert id2 == id1 + 1


def test_append_persists_fields_and_defaults(store: DraftStore) -> None:
    rid = _append_draft(store, source_refs="memory:190,ADR-024")
    row = store.get(rid)
    assert row is not None
    assert row["signal_type"] == "lesson"
    assert row["source_refs"] == "memory:190,ADR-024"
    assert row["raw_content"] == "raw concentrate"
    assert row["hook"] == "hook text"
    assert row["kind"] == "thread"
    assert row["workflow_id"] == "WF-031"
    assert row["status"] == "draft"
    assert row["redaction_status"] == "unreviewed"
    assert row["created_at"]


def test_append_invalid_signal_type_raises(store: DraftStore) -> None:
    with pytest.raises(ValueError, match="invalid signal_type"):
        _append_draft(store, signal_type="bogus")


def test_append_invalid_kind_raises(store: DraftStore) -> None:
    with pytest.raises(ValueError, match="invalid kind"):
        _append_draft(store, kind="carousel")


# ─── set_redacted ────────────────────────────────────


def test_set_redacted_transitions_and_flags(store: DraftStore) -> None:
    rid = _append_draft(store)
    ok = store.set_redacted(
        rid,
        redacted_hook="hook <REDACTED:email>",
        redacted_body_json=json.dumps(["seg1", "seg2"]),
        redaction_report=json.dumps({"email": 1}),
        flagged=True,
    )
    assert ok is True
    row = store.get(rid)
    assert row is not None
    assert row["status"] == "redacted"
    assert row["redaction_status"] == "auto_flagged"
    assert row["redacted_hook"] == "hook <REDACTED:email>"


def test_set_redacted_clean_stays_unreviewed(store: DraftStore) -> None:
    rid = _append_draft(store)
    store.set_redacted(rid, "clean hook", json.dumps(["clean"]), json.dumps({}), flagged=False)
    row = store.get(rid)
    assert row is not None
    assert row["status"] == "redacted"
    assert row["redaction_status"] == "unreviewed"


def test_set_redacted_idempotent_skip_non_draft(store: DraftStore) -> None:
    rid = _append_draft(store)
    assert store.set_redacted(rid, "h", "[]", "{}", flagged=False) is True
    # Second call: row is no longer 'draft' → no-op.
    assert store.set_redacted(rid, "h2", "[]", "{}", flagged=False) is False


# ─── mark_rejected / mark_posted ─────────────────────


def test_mark_rejected_from_draft(store: DraftStore) -> None:
    rid = _append_draft(store)
    assert store.mark_rejected(rid, "off-topic") is True
    assert store.mark_rejected(rid, "again") is False
    row = store.get(rid)
    assert row is not None
    assert row["status"] == "rejected"
    assert row["reject_reason"] == "off-topic"


def test_mark_rejected_from_redacted(store: DraftStore) -> None:
    rid = _append_draft(store)
    store.set_redacted(rid, "h", "[]", "{}", flagged=False)
    assert store.mark_rejected(rid, "changed my mind") is True


def test_mark_rejected_requires_reason(store: DraftStore) -> None:
    rid = _append_draft(store)
    with pytest.raises(ValueError, match="reason"):
        store.mark_rejected(rid, "  ")


def test_mark_posted_only_from_redacted(store: DraftStore) -> None:
    rid = _append_draft(store)
    # Cannot post a draft that has not been redacted.
    assert store.mark_posted(rid) is False
    store.set_redacted(rid, "h", "[]", "{}", flagged=False)
    assert store.mark_posted(rid) is True
    row = store.get(rid)
    assert row is not None
    assert row["status"] == "posted"
    assert row["posted_at"]
    # Idempotent.
    assert store.mark_posted(rid) is False


# ─── must-fix #1: pending never exposes raw_content / un-redacted body ──


def test_pending_never_exposes_raw_content(store: DraftStore) -> None:
    """The single most important invariant: the review queue must never carry
    the unscrubbed concentrate, or the human copy-pastes a leak."""
    secret = "SECRET /Users/flc001/key nakashin09@gmail.com ghp_DEADBEEF"
    rid = _append_draft(store, raw_content=secret, hook=secret, body_json=json.dumps([secret]))
    store.set_redacted(rid, "clean hook", json.dumps(["clean seg"]), json.dumps({"email": 1}), True)

    page = store.pending(filter_status="redacted")
    assert page["total"] == 1
    item = page["items"][0]
    # raw_content / hook / body_json keys must be ABSENT from the projection.
    assert "raw_content" not in item
    assert "hook" not in item
    assert "body_json" not in item
    # And the secret must appear nowhere in the serialized item.
    assert secret not in json.dumps(item)
    assert "ghp_DEADBEEF" not in json.dumps(item)
    # The safe redacted fields ARE present.
    assert item["redacted_hook"] == "clean hook"


def test_pending_draft_row_exposes_no_postable_body(store: DraftStore) -> None:
    """A not-yet-redacted draft must expose no postable body at all
    (redacted_* are NULL, raw/hook/body absent)."""
    rid = _append_draft(store, raw_content="dirty", hook="dirty hook")
    page = store.pending(filter_status="all")
    item = next(i for i in page["items"] if i["id"] == rid)
    assert "raw_content" not in item
    assert "hook" not in item
    assert item["redacted_hook"] is None
    assert item["redacted_body_json"] is None


def test_pending_shape_and_pagination(store: DraftStore) -> None:
    for i in range(5):
        _append_draft(store, source_refs=f"memory:{i}")
    page1 = store.pending(filter_status="all", limit=2, offset=0)
    assert set(page1.keys()) == {"items", "total", "has_more", "next_offset"}
    assert page1["total"] == 5
    assert page1["has_more"] is True
    assert page1["next_offset"] == 2
    page3 = store.pending(filter_status="all", limit=2, offset=4)
    assert len(page3["items"]) == 1
    assert page3["has_more"] is False


def test_pending_invalid_pagination_raises(store: DraftStore) -> None:
    with pytest.raises(ValueError):
        store.pending(limit=-1)
    with pytest.raises(ValueError):
        store.pending(offset=-1)


def test_pending_limit_zero_does_not_claim_has_more(store: DraftStore) -> None:
    """PMSERV-121: limit=0 is a valid count-only probe, but a 0-row page must
    NOT report has_more — otherwise a next_offset-driven loop never advances."""
    for i in range(3):
        _append_draft(store, source_refs=f"memory:{i}")
    page = store.pending(filter_status="all", limit=0)
    assert page["total"] == 3  # count still reported
    assert page["items"] == []
    assert page["has_more"] is False  # no infinite-pagination trap
    assert page["next_offset"] == 0  # did not advance


def test_pending_invalid_status_raises(store: DraftStore) -> None:
    with pytest.raises(ValueError, match="invalid filter_status"):
        store.pending(filter_status="bogus")


# ─── dedupe / counts ─────────────────────────────────


def test_find_live_by_source_refs_excludes_rejected(store: DraftStore) -> None:
    refs = normalize_source_refs(["memory:190", "ADR-024"])
    a = store.append("lesson", refs, "raw a")
    store.append("lesson", refs, "raw b")
    store.mark_rejected(a, "dup")
    live = store.find_live_by_source_refs(refs)
    # The rejected one is excluded; one live draft remains.
    assert len(live) == 1
    assert all("raw_content" not in row for row in live)  # safe columns only


def test_get_pending_count_counts_draft_and_redacted_only(store: DraftStore) -> None:
    a = _append_draft(store, source_refs="m:1")
    b = _append_draft(store, source_refs="m:2")
    c = _append_draft(store, source_refs="m:3")
    store.set_redacted(a, "h", "[]", "{}", flagged=False)  # redacted → counts
    store.mark_rejected(b, "noise")  # rejected → excluded
    store.set_redacted(c, "h", "[]", "{}", flagged=False)
    store.mark_posted(c)  # posted → excluded
    assert store.get_pending_count() == 1  # only 'a' (redacted, not posted)


def test_recent_live_drafts_window_and_liveness(store: DraftStore) -> None:
    """PMSERV-121 debounce source: recent + live only (old / rejected excluded)."""
    fresh1 = _append_draft(store, source_refs="memory:1")
    fresh2 = _append_draft(store, source_refs="memory:2")
    # A back-dated draft (inserted directly) is outside the window → excluded.
    conn = sqlite3.connect(str(store.db_path))
    try:
        conn.execute(
            "INSERT INTO x_drafts (signal_type, source_refs, raw_content, status, created_at) "
            "VALUES ('lesson', 'memory:old', 'r', 'draft', '2020-01-01 00:00:00')"
        )
        conn.commit()
    finally:
        conn.close()
    assert {r["id"] for r in store.recent_live_drafts(3600)} == {fresh1, fresh2}
    # A rejected fresh draft drops out (not live).
    store.mark_rejected(fresh1, "noise")
    assert {r["id"] for r in store.recent_live_drafts(3600)} == {fresh2}
    # Safe columns only — never the raw concentrate.
    assert all("raw_content" not in r for r in store.recent_live_drafts(3600))


def test_recent_live_drafts_negative_raises(store: DraftStore) -> None:
    with pytest.raises(ValueError, match="within_seconds"):
        store.recent_live_drafts(-1)


# ─── append-only trigger ─────────────────────────────


@pytest.mark.parametrize("col", ["raw_content", "source_refs", "signal_type", "workflow_id"])
def test_trigger_rejects_provenance_update(store: DraftStore, col: str) -> None:
    rid = _append_draft(store)
    conn = sqlite3.connect(str(store.db_path))
    try:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute(f"UPDATE x_drafts SET {col} = 'hijack' WHERE id = ?", (rid,))
    finally:
        conn.close()


def test_trigger_allows_redact_and_state_transitions(store: DraftStore) -> None:
    """The trigger must NOT block legitimate review edits / transitions."""
    rid = _append_draft(store)
    # set_redacted touches redacted_*/status/redaction_status — must succeed.
    assert store.set_redacted(rid, "h", "[]", "{}", flagged=True) is True
    assert store.mark_posted(rid) is True


@pytest.mark.parametrize("col", ["redacted_hook", "redacted_body_json", "redaction_report"])
def test_freeze_posted_blocks_redacted_rewrite(store: DraftStore, col: str) -> None:
    """PMSERV-121: once posted, the published redacted content is immutable —
    a raw UPDATE to redacted_* must abort."""
    rid = _append_draft(store)
    store.set_redacted(rid, "clean hook", json.dumps(["clean"]), json.dumps({}), flagged=False)
    store.mark_posted(rid)
    conn = sqlite3.connect(str(store.db_path))
    try:
        with pytest.raises(sqlite3.IntegrityError, match="frozen once posted"):
            conn.execute(f"UPDATE x_drafts SET {col} = 'rewrite' WHERE id = ?", (rid,))
    finally:
        conn.close()


def test_freeze_posted_allows_redacted_edit_before_post(store: DraftStore) -> None:
    """The freeze fires ONLY on posted rows — a pre-post 'redacted' row can
    still be corrected (the trigger is scoped by OLD.status = 'posted')."""
    rid = _append_draft(store)
    store.set_redacted(rid, "h", "[]", "{}", flagged=False)  # status='redacted'
    conn = sqlite3.connect(str(store.db_path))
    try:
        conn.execute("UPDATE x_drafts SET redacted_hook = 'fixed' WHERE id = ?", (rid,))
        conn.commit()
    finally:
        conn.close()
    assert store.get(rid)["redacted_hook"] == "fixed"


# ─── normalize_source_refs ───────────────────────────


def test_normalize_source_refs_sorts_dedups_strips() -> None:
    assert normalize_source_refs([" ADR-024 ", "memory:190", "ADR-024", ""]) == "ADR-024,memory:190"
    assert normalize_source_refs([]) == ""


# ─── must-fix #2: db_path-keyed factory (per-project isolation) ─────────


def test_factory_distinct_instances_per_db_path(tmp_path: Path) -> None:
    """Unlike the outbox single-slot factory, x_drafts stores are per-project:
    two different db_paths MUST yield two distinct instances in one process."""
    a = get_draft_store(tmp_path / "a" / "drafts.db")
    b = get_draft_store(tmp_path / "b" / "drafts.db")
    assert a is not b
    assert a.db_path != b.db_path
    # Same path returns the cached instance.
    assert get_draft_store(tmp_path / "a" / "drafts.db") is a


def test_factory_isolation_no_cross_project_leak(tmp_path: Path) -> None:
    a = get_draft_store(tmp_path / "a" / "drafts.db")
    b = get_draft_store(tmp_path / "b" / "drafts.db")
    a.append("lesson", "m:1", "raw a")
    assert a.get_pending_count() == 1
    assert b.get_pending_count() == 0  # b never saw a's write


def test_clear_draft_store_clears_all_keys(tmp_path: Path) -> None:
    a = get_draft_store(tmp_path / "a" / "drafts.db")
    get_draft_store(tmp_path / "b" / "drafts.db")
    clear_draft_store()
    a2 = get_draft_store(tmp_path / "a" / "drafts.db")
    assert a2 is not a  # cache fully reset


def test_default_draft_db_path_does_not_create_files(tmp_path: Path) -> None:
    pm_path = tmp_path / "new-project" / ".pm"
    assert default_draft_db_path(pm_path) == pm_path / "drafts.db"
    assert not pm_path.exists()


@pytest.mark.parametrize("filename", ["drafts.db", "x_drafts.db"])
def test_default_draft_db_path_reuses_existing_store(tmp_path: Path, filename: str) -> None:
    pm_path = tmp_path / ".pm"
    pm_path.mkdir()
    db = pm_path / filename
    db.write_bytes(b"resolver must not open this file")
    before = db.stat()
    assert default_draft_db_path(pm_path) == db
    assert db.stat().st_mtime_ns == before.st_mtime_ns
    assert list(pm_path.iterdir()) == [db]


def test_default_draft_db_path_refuses_two_stores(tmp_path: Path) -> None:
    pm_path = tmp_path / ".pm"
    pm_path.mkdir()
    for filename in ("drafts.db", "x_drafts.db"):
        (pm_path / filename).write_bytes(filename.encode())
    with pytest.raises(DraftStoreConflictError, match="Both .* exist"):
        default_draft_db_path(pm_path)
    assert {p.name: p.read_bytes() for p in pm_path.iterdir()} == {
        "drafts.db": b"drafts.db",
        "x_drafts.db": b"x_drafts.db",
    }


def test_legacy_symlink_does_not_start_a_second_store(tmp_path: Path) -> None:
    legacy = tmp_path / "x_drafts.db"
    try:
        legacy.symlink_to(tmp_path / "unavailable.db")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable on this host")
    assert default_draft_db_path(tmp_path) == legacy
    (tmp_path / "drafts.db").touch()
    with pytest.raises(DraftStoreConflictError):
        default_draft_db_path(tmp_path)


def test_legacy_imports_share_the_canonical_factory(tmp_path: Path) -> None:
    from pmlens import draft_store, x_draft_store

    assert x_draft_store.XDraftStore is DraftStore
    for old, new in (
        ("XDraftKind", "DraftKind"),
        ("XDraftSignalType", "DraftSignalType"),
        ("XDraftStatus", "DraftStatus"),
        ("RedactionStatus", "RedactionStatus"),
        ("normalize_source_refs", "normalize_source_refs"),
    ):
        assert getattr(x_draft_store, old) is getattr(draft_store, new)
    db = x_draft_store.default_x_draft_db_path(tmp_path)
    assert db == tmp_path / "drafts.db"
    old = x_draft_store.get_x_draft_store(db)
    assert old is get_draft_store(db)
    rid = _append_draft(old)
    x_draft_store.clear_x_draft_store()
    new = get_draft_store(db)
    assert new is not old
    assert new.get(rid) == old.get(rid)


def test_existing_legacy_wal_and_all_states_survive_path_resolution(tmp_path: Path) -> None:
    """Read committed WAL data in place, retaining IDs, provenance and triggers."""
    db = tmp_path / "x_drafts.db"
    legacy = DraftStore(db)
    # Hold a snapshot so subsequent committed rows must remain in the WAL.
    reader = sqlite3.connect(db)
    try:
        reader.execute("BEGIN")
        assert reader.execute("SELECT COUNT(*) FROM x_drafts").fetchone()[0] == 0
        expected = {}
        for state in ("draft", "redacted", "rejected", "posted"):
            rid = _append_draft(legacy, source_refs=f"m:{state}")
            if state in {"redacted", "posted"}:
                legacy.set_redacted(rid, "safe hook", '["safe body"]', "{}", flagged=False)
            if state == "rejected":
                legacy.mark_rejected(rid, "not for publication")
            if state == "posted":
                legacy.mark_posted(rid)
            expected[rid] = legacy.get(rid)
        assert Path(f"{db}-wal").stat().st_size > 32

        current = get_draft_store(default_draft_db_path(tmp_path))
        assert current.db_path == db
        assert not (tmp_path / "drafts.db").exists()
        assert {rid: current.get(rid) for rid in expected} == expected
        assert current.get_pending_count() == 2
        assert current.pending(filter_status="all")["total"] == 4
        assert _append_draft(current, source_refs="m:new") > max(expected)
        # The old connection still has its original snapshot; nothing moved.
        assert reader.execute("SELECT COUNT(*) FROM x_drafts").fetchone()[0] == 0
        assert legacy.get_pending_count() == 3
        with sqlite3.connect(db) as conn:
            assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                conn.execute("UPDATE x_drafts SET raw_content = 'changed' WHERE id = 1")
            with pytest.raises(sqlite3.IntegrityError, match="frozen once posted"):
                conn.execute(
                    "UPDATE x_drafts SET redacted_hook = 'changed' WHERE status = 'posted'"
                )
    finally:
        reader.close()
