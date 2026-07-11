"""Tests for DesktopOutboxStore (ADR-019, WF-028, PMSERV-101)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from pmlens import outbox as _outbox
from pmlens.models import PmServerError
from pmlens.outbox import (
    DesktopOutboxStore,
    clear_outbox_store,
    default_outbox_db_path,
    get_outbox_store,
)


@pytest.fixture(autouse=True)
def _isolate_outbox_factory():
    """Reset the module-level factory cache between tests (amendment f5)."""
    clear_outbox_store()
    yield
    clear_outbox_store()


@pytest.fixture
def outbox_store(tmp_path: Path) -> DesktopOutboxStore:
    """A fresh DesktopOutboxStore backed by a temp file per test."""
    db = tmp_path / "desktop" / "desktop.db"
    return DesktopOutboxStore(db)


# ─── Schema / PRAGMA ────────────────────────────────


def test_schema_creates_table_indexes_and_trigger(tmp_path: Path) -> None:
    db = tmp_path / "desktop.db"
    DesktopOutboxStore(db)
    conn = sqlite3.connect(str(db))
    try:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "desktop_outbox" in tables
        indexes = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_outbox%'"
            )
        }
        assert indexes == {
            "idx_outbox_status_created",
            "idx_outbox_project_status",
            "idx_outbox_type_status",
            "idx_outbox_tags",
        }
        triggers = {
            r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger'")
        }
        assert "trg_outbox_append_only" in triggers
    finally:
        conn.close()


def test_pragmas_page_size_wal_user_version(tmp_path: Path) -> None:
    db = tmp_path / "desktop.db"
    DesktopOutboxStore(db)
    conn = sqlite3.connect(str(db))
    try:
        assert conn.execute("PRAGMA page_size").fetchone()[0] == 8192
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
    finally:
        conn.close()


def test_ensure_schema_is_idempotent(tmp_path: Path) -> None:
    db = tmp_path / "desktop.db"
    DesktopOutboxStore(db)
    DesktopOutboxStore(db)  # second init must not error or duplicate objects
    conn = sqlite3.connect(str(db))
    try:
        triggers = {
            r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger'")
        }
        assert triggers == {"trg_outbox_append_only"}
    finally:
        conn.close()


# ─── Append / type validation ───────────────────────


def test_append_returns_increasing_ids(outbox_store: DesktopOutboxStore) -> None:
    id1 = outbox_store.append("claude-desktop", "sess-a", "memory", "first")
    id2 = outbox_store.append("claude-desktop", "sess-a", "log", "second")
    assert id1 >= 1
    assert id2 == id1 + 1


def test_append_invalid_type_raises(outbox_store: DesktopOutboxStore) -> None:
    with pytest.raises(ValueError, match="invalid type"):
        outbox_store.append("h", "s", "invalid", "x")  # type: ignore[arg-type]


def test_append_persists_all_fields(outbox_store: DesktopOutboxStore) -> None:
    rid = outbox_store.append(
        "claude-desktop",
        "sess-x",
        "memory",
        "hello",
        source_project="/abs/project",
        tags="a,b,c",
    )
    row = outbox_store.get(rid)
    assert row is not None
    assert row["host_id"] == "claude-desktop"
    assert row["source_session"] == "sess-x"
    assert row["type"] == "memory"
    assert row["content"] == "hello"
    assert row["source_project"] == "/abs/project"
    assert row["tags"] == "a,b,c"
    assert row["status"] == "pending"
    assert row["created_at"]  # truthy datetime string


# ─── pending() pagination + filters ─────────────────


def test_pending_returns_shape(outbox_store: DesktopOutboxStore) -> None:
    outbox_store.append("h", "s", "memory", "one")
    res = outbox_store.pending()
    assert set(res.keys()) == {"items", "total", "has_more", "next_offset"}
    assert isinstance(res["items"], list)
    assert isinstance(res["total"], int)
    assert isinstance(res["has_more"], bool)
    assert isinstance(res["next_offset"], int)


def test_pending_pagination_limit_and_offset(outbox_store: DesktopOutboxStore) -> None:
    for i in range(5):
        outbox_store.append("h", "s", "memory", f"n={i}")
    page1 = outbox_store.pending(limit=2, offset=0)
    page2 = outbox_store.pending(limit=2, offset=2)
    page3 = outbox_store.pending(limit=2, offset=4)
    assert page1["total"] == 5
    assert page1["has_more"] is True
    assert page1["next_offset"] == 2
    assert len(page1["items"]) == 2
    assert len(page2["items"]) == 2
    assert page2["has_more"] is True
    assert len(page3["items"]) == 1
    assert page3["has_more"] is False
    assert page3["next_offset"] == 5


def test_pending_pagination_overshoot_returns_empty(outbox_store: DesktopOutboxStore) -> None:
    outbox_store.append("h", "s", "memory", "only")
    res = outbox_store.pending(limit=10, offset=100)
    assert res["items"] == []
    assert res["has_more"] is False
    assert res["total"] == 1


def test_pending_limit_zero_does_not_claim_has_more(outbox_store: DesktopOutboxStore) -> None:
    """PMSERV-122: limit=0 is a valid count-only probe, but a 0-row page must
    NOT report has_more — otherwise a next_offset-driven loop never advances
    (same infinite-pagination trap fixed in x_draft_store under PMSERV-121)."""
    for i in range(3):
        outbox_store.append("h", "s", "memory", f"n={i}")
    page = outbox_store.pending(limit=0)
    assert page["total"] == 3  # count still reported
    assert page["items"] == []
    assert page["has_more"] is False  # no infinite-pagination trap
    assert page["next_offset"] == 0  # did not advance


def test_pending_filters_project_type_and_status(outbox_store: DesktopOutboxStore) -> None:
    a = outbox_store.append("h", "s", "memory", "in proj-a", source_project="proj-a")
    outbox_store.append("h", "s", "log", "in proj-b", source_project="proj-b")
    outbox_store.append("h", "s", "memory", "in proj-a log", source_project="proj-a")
    outbox_store.mark_merged(a, None, None)  # one of proj-a is now merged

    pending_a_memory = outbox_store.pending(filter_project="proj-a", filter_type="memory")
    assert pending_a_memory["total"] == 1  # the merged one is excluded by default

    all_a_memory = outbox_store.pending(
        filter_project="proj-a", filter_type="memory", filter_status="all"
    )
    assert all_a_memory["total"] == 2

    only_merged = outbox_store.pending(filter_status="merged")
    assert only_merged["total"] == 1


def test_pending_filter_since_narrows_by_created_at(
    outbox_store: DesktopOutboxStore,
) -> None:
    """PMSERV-145 (ADR-039 T2): filter_since adds a created_at >= ? clause.

    Rows are inserted directly with explicit created_at values (rather than
    via append(), which always stamps datetime('now')) so the boundary is
    deterministic instead of racing real-clock second resolution.
    """
    conn = sqlite3.connect(str(outbox_store.db_path))
    try:
        conn.execute(
            "INSERT INTO desktop_outbox (host_id, source_session, type, content, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("h", "s", "memory", "old", "2026-01-01T00:00:00"),
        )
        conn.execute(
            "INSERT INTO desktop_outbox (host_id, source_session, type, content, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("h", "s", "memory", "new", "2026-06-01T00:00:00"),
        )
        conn.commit()
    finally:
        conn.close()

    assert outbox_store.pending()["total"] == 2

    since_page = outbox_store.pending(filter_since="2026-03-01T00:00:00")
    assert since_page["total"] == 1
    assert since_page["items"][0]["content"] == "new"

    since_all = outbox_store.pending(filter_since="2000-01-01T00:00:00")
    assert since_all["total"] == 2


def test_pending_invalid_pagination_raises(outbox_store: DesktopOutboxStore) -> None:
    with pytest.raises(ValueError):
        outbox_store.pending(limit=-1)
    with pytest.raises(ValueError):
        outbox_store.pending(offset=-1)


# ─── mark_merged / mark_rejected idempotency ─────────


def test_mark_merged_first_true_second_false(outbox_store: DesktopOutboxStore) -> None:
    rid = outbox_store.append("h", "s", "memory", "x")
    assert outbox_store.mark_merged(rid, 99, "/path/to") is True
    assert outbox_store.mark_merged(rid, 99, "/path/to") is False
    row = outbox_store.get(rid)
    assert row is not None
    assert row["status"] == "merged"
    assert row["merged_to_id"] == 99
    assert row["merged_to_path"] == "/path/to"
    assert row["merged_at"]


def test_mark_rejected_idempotent_with_reason(outbox_store: DesktopOutboxStore) -> None:
    rid = outbox_store.append("h", "s", "memory", "y")
    assert outbox_store.mark_rejected(rid, "out of scope") is True
    assert outbox_store.mark_rejected(rid, "different reason") is False
    row = outbox_store.get(rid)
    assert row is not None
    assert row["status"] == "rejected"
    assert row["reject_reason"] == "out of scope"


def test_mark_rejected_requires_reason(outbox_store: DesktopOutboxStore) -> None:
    rid = outbox_store.append("h", "s", "memory", "z")
    with pytest.raises(ValueError, match="reason"):
        outbox_store.mark_rejected(rid, "   ")
    with pytest.raises(ValueError, match="reason"):
        outbox_store.mark_rejected(rid, "")


# ─── Append-only trigger enforcement ────────────────


def test_trigger_rejects_content_update(outbox_store: DesktopOutboxStore) -> None:
    rid = outbox_store.append("h", "s", "memory", "original")
    conn = sqlite3.connect(str(outbox_store.db_path))
    try:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute("UPDATE desktop_outbox SET content = 'hijack' WHERE id = ?", (rid,))
    finally:
        conn.close()


def test_trigger_rejects_type_update(outbox_store: DesktopOutboxStore) -> None:
    rid = outbox_store.append("h", "s", "memory", "x")
    conn = sqlite3.connect(str(outbox_store.db_path))
    try:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute("UPDATE desktop_outbox SET type = 'log' WHERE id = ?", (rid,))
    finally:
        conn.close()


def test_trigger_allows_status_transition_via_mark_merged(outbox_store: DesktopOutboxStore) -> None:
    """The append-only trigger must NOT block legitimate status transitions
    (mark_merged uses a permitted column set: status / merged_to_id /
    merged_to_path / merged_at)."""
    rid = outbox_store.append("h", "s", "memory", "x")
    # Should not raise.
    assert outbox_store.mark_merged(rid, 42, "/somewhere") is True


# ─── get_pending_count ───────────────────────────────


def test_get_pending_count_excludes_merged_and_rejected(outbox_store: DesktopOutboxStore) -> None:
    a = outbox_store.append("h", "s", "memory", "one")
    b = outbox_store.append("h", "s", "memory", "two")
    outbox_store.append("h", "s", "memory", "three")
    outbox_store.mark_merged(a, None, None)
    outbox_store.mark_rejected(b, "noise")
    assert outbox_store.get_pending_count() == 1


# ─── pending_source_project_counts (PMSERV-147, ADR-039 T4) ─────────


def test_pending_source_project_counts_groups_by_project(
    outbox_store: DesktopOutboxStore,
) -> None:
    outbox_store.append("h", "s", "memory", "a", source_project="/proj/x")
    outbox_store.append("h", "s", "memory", "b", source_project="/proj/x")
    outbox_store.append("h", "s", "memory", "c", source_project="/proj/y")
    outbox_store.append("h", "s", "memory", "d")  # no source_project

    counts = dict(outbox_store.pending_source_project_counts())
    assert counts == {"/proj/x": 2, "/proj/y": 1}


def test_pending_source_project_counts_excludes_merged_and_rejected(
    outbox_store: DesktopOutboxStore,
) -> None:
    a = outbox_store.append("h", "s", "memory", "a", source_project="/proj/x")
    outbox_store.append("h", "s", "memory", "b", source_project="/proj/x")
    outbox_store.mark_merged(a, None, None)
    assert dict(outbox_store.pending_source_project_counts()) == {"/proj/x": 1}


def test_pending_source_project_counts_missing_db_returns_empty(tmp_path: Path) -> None:
    store = DesktopOutboxStore(tmp_path / "nope" / "desktop.db", readonly=True)
    assert store.pending_source_project_counts() == []


# ─── Factory (f5) — pytest test isolation ────────────


def test_factory_returns_same_instance(tmp_path: Path) -> None:
    db = tmp_path / "factory.db"
    first = get_outbox_store(db_path=db)
    second = get_outbox_store(db_path=tmp_path / "ignored.db")  # path ignored after cache fill
    assert first is second


def test_clear_outbox_store_resets_cache(tmp_path: Path) -> None:
    db_a = tmp_path / "a.db"
    db_b = tmp_path / "b.db"
    first = get_outbox_store(db_path=db_a)
    assert first.db_path == db_a
    clear_outbox_store()
    second = get_outbox_store(db_path=db_b)
    assert second is not first
    assert second.db_path == db_b


def test_default_path_honors_monkeypatched_global_pm_dir(monkeypatch, tmp_path: Path) -> None:
    """default_outbox_db_path() must resolve dynamically so test fixtures
    that monkeypatch GLOBAL_PM_DIR (conftest.py:isolated_registry) reach
    a temp location rather than the real ~/.pm/desktop/desktop.db."""
    fake = tmp_path / "fake_global"
    fake.mkdir()
    monkeypatch.setattr(_outbox._storage, "GLOBAL_PM_DIR", fake)
    assert default_outbox_db_path() == fake / "desktop" / "desktop.db"


# ─── Read-pure / readonly store (PMSERV-142, ADR-039 T1) ────────────


def _assert_no_db_artifacts(db_path: Path) -> None:
    """Assert the DB file and its WAL/SHM sidecars were never created."""
    assert not db_path.exists()
    assert not db_path.with_name(db_path.name + "-wal").exists()
    assert not db_path.with_name(db_path.name + "-shm").exists()


def test_readonly_missing_db_reads_return_empty_shapes_without_creating_file(
    tmp_path: Path,
) -> None:
    db = tmp_path / "desktop" / "desktop.db"
    ro_store = DesktopOutboxStore(db, readonly=True)

    page = ro_store.pending()
    assert page == {"items": [], "total": 0, "has_more": False, "next_offset": 0}
    assert ro_store.get(1) is None
    assert ro_store.get_pending_count() == 0

    _assert_no_db_artifacts(db)


def test_readonly_construction_alone_does_not_create_file(tmp_path: Path) -> None:
    """Unlike the RW constructor (_ensure_schema), readonly=True must never
    create the DB file/schema just from instantiation."""
    db = tmp_path / "desktop.db"
    DesktopOutboxStore(db, readonly=True)
    _assert_no_db_artifacts(db)


def test_readonly_store_append_raises_pm_server_error(tmp_path: Path) -> None:
    db = tmp_path / "desktop.db"
    ro_store = DesktopOutboxStore(db, readonly=True)
    with pytest.raises(PmServerError, match="read-only"):
        ro_store.append("h", "s", "memory", "x")
    assert not db.exists()


def test_readonly_store_mark_merged_and_mark_rejected_raise(tmp_path: Path) -> None:
    db = tmp_path / "desktop.db"
    ro_store = DesktopOutboxStore(db, readonly=True)
    with pytest.raises(PmServerError, match="read-only"):
        ro_store.mark_merged(1, None, None)
    with pytest.raises(PmServerError, match="read-only"):
        ro_store.mark_rejected(1, "some reason")
    assert not db.exists()


def test_cache_staleness_ro_store_sees_entries_appended_after_construction(
    tmp_path: Path,
) -> None:
    """Cross-check BLOCKER regression: missing-ness must be evaluated on
    every read call, never decided/cached at __init__. Sequence: (1) a RO
    store is constructed while desktop.db is absent and confirms empty,
    (2) a separate RW store then creates the DB and appends an entry, (3)
    the SAME RO store instance from step 1 must see the new entry — proving
    the RO store did not freeze "missing" at construction time."""
    db = tmp_path / "desktop.db"

    ro_store = DesktopOutboxStore(db, readonly=True)
    assert ro_store.pending()["total"] == 0
    assert ro_store.get_pending_count() == 0
    assert not db.exists()

    rw_store = DesktopOutboxStore(db, readonly=False)
    rid = rw_store.append("claude-desktop", "sess-a", "memory", "late arrival")
    assert db.exists()

    page = ro_store.pending()
    assert page["total"] == 1
    assert page["items"][0]["id"] == rid
    assert ro_store.get(rid) is not None
    assert ro_store.get(rid)["content"] == "late arrival"
    assert ro_store.get_pending_count() == 1


def test_get_outbox_store_readonly_factory_returns_readonly_instance(tmp_path: Path) -> None:
    db = tmp_path / "desktop.db"
    store = get_outbox_store(db_path=db, readonly=True)
    assert isinstance(store, DesktopOutboxStore)
    assert store.readonly is True
    assert not db.exists()  # RO factory call must not create the DB


def test_get_outbox_store_default_signature_still_rw(tmp_path: Path) -> None:
    """Existing db_path=-only call sites (server.py's 5 outbox tools) must
    keep working unchanged: readonly defaults to False (RW, schema created
    eagerly on construction)."""
    db = tmp_path / "desktop.db"
    store = get_outbox_store(db_path=db)
    assert store.readonly is False
    assert db.exists()


def test_get_outbox_store_rw_and_ro_are_separate_cache_slots(tmp_path: Path) -> None:
    db = tmp_path / "desktop.db"
    rw = get_outbox_store(db_path=db)
    ro = get_outbox_store(db_path=db, readonly=True)
    assert rw is not ro
    assert rw.readonly is False
    assert ro.readonly is True
    # A second call to each slot returns the cached instance for that slot.
    assert get_outbox_store(db_path=db) is rw
    assert get_outbox_store(db_path=db, readonly=True) is ro


def test_clear_outbox_store_resets_both_rw_and_ro_slots(tmp_path: Path) -> None:
    db = tmp_path / "desktop.db"
    rw_first = get_outbox_store(db_path=db)
    ro_first = get_outbox_store(db_path=db, readonly=True)

    clear_outbox_store()

    rw_second = get_outbox_store(db_path=db)
    ro_second = get_outbox_store(db_path=db, readonly=True)

    assert rw_second is not rw_first
    assert ro_second is not ro_first


# ─── Tool-level passthrough tests (PMSERV-101 final) ──────────────────


def _make_project(tmp_path: Path, name: str = "proj") -> Path:
    """Minimal project root with .pm/project.yaml + .pm/tasks.yaml + daily/."""
    proj = tmp_path / name
    (proj / ".pm" / "daily").mkdir(parents=True)
    (proj / ".pm" / "project.yaml").write_text(
        f"name: {name}\n"
        f"display_name: {name}\n"
        "version: 0.0.1\n"
        "status: development\n"
        "started: 2026-01-01\n"
        "description: outbox tool tests\n"
        "phases: []\n",
        encoding="utf-8",
    )
    (proj / ".pm" / "tasks.yaml").write_text("[]\n", encoding="utf-8")
    return proj


def test_pm_outbox_remember_invalid_type_returns_error(tmp_path: Path) -> None:
    """Tool boundary: invalid type returns a structured error (not raises)."""
    import pmlens.server as srv

    res = srv.pm_outbox_remember(content="x", type="bogus")
    assert res["status"] == "error"
    assert res["code"] == "invalid_type"


def test_pm_outbox_log_invalid_category_returns_error(tmp_path: Path) -> None:
    import pmlens.server as srv

    res = srv.pm_outbox_log(entry="x", category="bogus")
    assert res["status"] == "error"
    assert res["code"] == "invalid_category"


def test_pm_outbox_pending_invalid_pagination_returns_error(tmp_path: Path) -> None:
    import pmlens.server as srv

    res = srv.pm_outbox_pending(limit=-1)
    assert res["status"] == "error"
    assert res["code"] == "invalid_pagination"


def test_pm_outbox_reject_requires_reason(tmp_path: Path) -> None:
    import pmlens.server as srv

    res = srv.pm_outbox_reject(ids=[1], reason="")
    assert res["status"] == "error"
    assert res["code"] == "reason_required"


def test_pm_outbox_merge_missing_db_short_circuits_not_found(tmp_path: Path) -> None:
    """PMSERV-142 T1-6 / AD-6: with no desktop.db at all, pm_outbox_merge
    must return not_found for every requested id without ever constructing
    an RW store (which would call _ensure_schema and create the file)."""
    import pmlens.server as srv

    db = default_outbox_db_path()
    assert not db.exists()

    res = srv.pm_outbox_merge(ids=[1, 2, 3], target_project=str(tmp_path))

    assert res["merged"] == []
    assert res["warnings"] == [
        {"id": 1, "reason": "not_found"},
        {"id": 2, "reason": "not_found"},
        {"id": 3, "reason": "not_found"},
    ]
    assert not db.exists()


def test_pm_outbox_reject_missing_db_short_circuits_not_found(tmp_path: Path) -> None:
    """Mirror of the merge guard: pm_outbox_reject must not construct an RW
    store (and therefore not create desktop.db) when it is absent."""
    import pmlens.server as srv

    db = default_outbox_db_path()
    assert not db.exists()

    res = srv.pm_outbox_reject(ids=[1, 2], reason="not relevant")

    assert res["rejected"] == []
    assert res["warnings"] == [
        {"id": 1, "reason": "not_found"},
        {"id": 2, "reason": "not_found"},
    ]
    assert not db.exists()


def test_pm_outbox_merge_routes_memory_and_log_to_target(tmp_path: Path) -> None:
    """End-to-end tool: pm_outbox_remember (memory) + pm_outbox_log (log) →
    pm_outbox_merge promotes both into the right targets."""
    import pmlens.server as srv

    proj = _make_project(tmp_path)
    mem_res = srv.pm_outbox_remember(content="from tool", type="memory", source_project=str(proj))
    log_res = srv.pm_outbox_log(entry="ship it", category="milestone", source_project=str(proj))
    assert mem_res["status"] == "saved"
    assert log_res["status"] == "saved"

    merge = srv.pm_outbox_merge(
        ids=[mem_res["outbox_id"], log_res["outbox_id"]], target_project=str(proj)
    )
    assert merge["status"] == "ok"
    assert len(merge["merged"]) == 2
    assert merge["skipped"] == []
    assert merge["warnings"] == []

    # Verify the project's main memory.db received the memory entry.
    db = proj / ".pm" / "memory.db"
    assert db.exists()
    conn = sqlite3.connect(str(db))
    try:
        types = [r[0] for r in conn.execute("SELECT type FROM memories")]
    finally:
        conn.close()
    assert "observation" in types

    # Verify the daily log received the log entry (with category recovered
    # from the "[milestone] ship it" prefix written by pm_outbox_log).
    import yaml as _yaml

    daily_files = list((proj / ".pm" / "daily").glob("*.yaml"))
    assert len(daily_files) == 1
    log_data = _yaml.safe_load(daily_files[0].read_text(encoding="utf-8"))
    assert log_data["entries"][0]["category"] == "milestone"
    assert log_data["entries"][0]["entry"] == "ship it"


def test_pm_outbox_merge_idempotent_skip_already_processed(tmp_path: Path) -> None:
    """Re-merging an already-merged id surfaces in skipped[], not merged[]."""
    import pmlens.server as srv

    proj = _make_project(tmp_path)
    rid = srv.pm_outbox_remember(content="once", type="memory", source_project=str(proj))[
        "outbox_id"
    ]
    first = srv.pm_outbox_merge(ids=[rid], target_project=str(proj))
    second = srv.pm_outbox_merge(ids=[rid], target_project=str(proj))
    assert len(first["merged"]) == 1
    assert second["merged"] == []
    assert len(second["skipped"]) == 1
    assert second["skipped"][0]["reason"] == "already_processed"


def test_pm_outbox_merge_no_target_project_yields_warning(tmp_path: Path) -> None:
    """Outbox entries without source_project and without target_project arg
    cannot be merged — must surface in warnings[] (not raise)."""
    import pmlens.server as srv

    rid = srv.pm_outbox_remember(content="no target", type="memory")["outbox_id"]
    res = srv.pm_outbox_merge(ids=[rid])
    assert res["merged"] == []
    assert any(w["reason"] == "no_target_project" for w in res["warnings"])


def test_pm_outbox_remember_response_includes_pending_total(tmp_path: Path) -> None:
    """PMSERV-145 (ADR-039 T2): remember echoes back the DB-wide pending
    count (via the same already-open RW store), not scoped to this insert."""
    import pmlens.server as srv

    first = srv.pm_outbox_remember(content="a", type="memory")
    assert first["pending_total"] == 1
    second = srv.pm_outbox_remember(content="b", type="memory")
    assert second["pending_total"] == 2


def test_pm_outbox_log_response_includes_pending_total(tmp_path: Path) -> None:
    """Mirror of the remember test for pm_outbox_log; pending_total counts
    across BOTH remember and log entries (DB-wide, not type-scoped)."""
    import pmlens.server as srv

    srv.pm_outbox_remember(content="a", type="memory")
    log_res = srv.pm_outbox_log(entry="did stuff")
    assert log_res["pending_total"] == 2


def test_pm_outbox_pending_filter_since_narrows_results(tmp_path: Path) -> None:
    """Tool-level: filter_since threads through to the store's WHERE clause.

    An old-dated row is inserted directly (bypassing the tool, which always
    timestamps with datetime('now')) so the boundary is deterministic.
    """
    import pmlens.server as srv

    srv.pm_outbox_remember(content="via tool", type="memory")

    db_path = default_outbox_db_path()
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "INSERT INTO desktop_outbox (host_id, source_session, type, content, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("h", "s", "memory", "ancient", "2000-01-01T00:00:00"),
        )
        conn.commit()
    finally:
        conn.close()

    unfiltered = srv.pm_outbox_pending()
    assert unfiltered["total"] == 2

    filtered = srv.pm_outbox_pending(filter_since="2020-01-01T00:00:00")
    assert filtered["total"] == 1
    assert filtered["items"][0]["content"] == "via tool"


def test_pm_outbox_pending_does_not_create_db(tmp_path: Path) -> None:
    """PMSERV-145 (ADR-039 T2): pm_outbox_pending now opens the store with
    readonly=True — desktop.db must not be created when absent."""
    import pmlens.server as srv

    db_path = default_outbox_db_path()
    assert not db_path.exists()

    res = srv.pm_outbox_pending()
    assert res["status"] == "ok"
    assert res["items"] == []
    assert not db_path.exists()


def test_pm_status_diagnostics_includes_outbox_pending(tmp_path: Path) -> None:
    """pm_status.diagnostics.outbox_pending surfaces in Claude Code mode and
    triggers a next_pm_actions hint when N > 0."""
    import pmlens.server as srv

    proj = _make_project(tmp_path, name="statproj")
    # Inject two pending entries via the desktop writer.
    srv.pm_outbox_remember(content="a", type="memory")
    srv.pm_outbox_remember(content="b", type="memory")

    status = srv.pm_status(project_path=str(proj))
    assert "outbox_pending" in status["diagnostics"]
    assert status["diagnostics"]["outbox_pending"] == 2
    assert any(
        "Desktop outbox" in line and "2 pending" in line for line in status["next_pm_actions"]
    )


def test_pm_status_diagnostics_outbox_zero_omits_hint(tmp_path: Path) -> None:
    """When outbox_pending == 0, the next_pm_actions hint must NOT appear so
    pm_status stays uncluttered."""
    import pmlens.server as srv

    proj = _make_project(tmp_path, name="emptystatproj")
    status = srv.pm_status(project_path=str(proj))
    assert status["diagnostics"]["outbox_pending"] == 0
    assert not any("Desktop outbox" in line for line in status["next_pm_actions"])


# ─── T4: unregistered-project guidance + merge guard (PMSERV-147, ADR-039) ──


def test_pm_outbox_remember_unregistered_source_project_warns(tmp_path: Path) -> None:
    """source_project pointing at a path without .pm/project.yaml: the save
    still succeeds, but a warnings[] entry with code=unregistered_project is
    added (dual-audience message/remediation)."""
    import pmlens.server as srv

    unregistered = tmp_path / "not_pm_init_yet"
    unregistered.mkdir()

    res = srv.pm_outbox_remember(content="x", type="memory", source_project=str(unregistered))
    assert res["status"] == "saved"
    assert "warnings" in res
    assert len(res["warnings"]) == 1
    warning = res["warnings"][0]
    assert warning["code"] == "unregistered_project"
    assert warning["project"] == str(unregistered)
    assert "pm_init" in warning["remediation"]
    assert "pm_outbox_pending" in warning["remediation"] or "pm_recall" in warning["remediation"]


def test_pm_outbox_remember_registered_source_project_no_warnings(tmp_path: Path) -> None:
    """A pm_init'd (registered) source_project produces no warnings[]."""
    import pmlens.server as srv

    proj = _make_project(tmp_path, name="registered")
    res = srv.pm_outbox_remember(content="x", type="memory", source_project=str(proj))
    assert res["status"] == "saved"
    assert "warnings" not in res


def test_pm_outbox_remember_no_source_project_gets_hint_not_warning(tmp_path: Path) -> None:
    """Omitting source_project entirely is not an error condition — no
    warnings[], but the note gains guidance about adding source_project."""
    import pmlens.server as srv

    res = srv.pm_outbox_remember(content="x", type="memory")
    assert res["status"] == "saved"
    assert "warnings" not in res
    assert "source_project" in res["note"]
    # Original guidance must still be present (folded in, not replaced).
    assert "pm_outbox_pending" in res["note"]


def test_pm_outbox_log_unregistered_source_project_warns(tmp_path: Path) -> None:
    """Mirror of the remember test for pm_outbox_log."""
    import pmlens.server as srv

    unregistered = tmp_path / "also_not_pm_init"
    unregistered.mkdir()

    res = srv.pm_outbox_log(entry="did stuff", source_project=str(unregistered))
    assert res["status"] == "saved"
    assert "warnings" in res
    assert res["warnings"][0]["code"] == "unregistered_project"
    assert res["warnings"][0]["project"] == str(unregistered)


def test_pm_outbox_log_registered_source_project_no_warnings(tmp_path: Path) -> None:
    import pmlens.server as srv

    proj = _make_project(tmp_path, name="registered_log")
    res = srv.pm_outbox_log(entry="did stuff", source_project=str(proj))
    assert res["status"] == "saved"
    assert "warnings" not in res


def test_pm_outbox_merge_unregistered_target_skipped_no_pm_dir_created(
    tmp_path: Path,
) -> None:
    """AD-6 (critical empirical check): merging against an unregistered
    target must skip with a remediation entry AND must never create a .pm
    directory anywhere under that path."""
    import pmlens.server as srv

    unregistered = tmp_path / "unreg_merge_target"
    unregistered.mkdir()

    rid = srv.pm_outbox_remember(content="orphan", type="memory")["outbox_id"]
    res = srv.pm_outbox_merge(ids=[rid], target_project=str(unregistered))

    assert res["merged"] == []
    assert any(w["reason"] == "unregistered_project" and w["id"] == rid for w in res["warnings"])
    unreg_warning = next(w for w in res["warnings"] if w["id"] == rid)
    assert "pm_init" in unreg_warning["remediation"]

    # The critical empirical check: no .pm anywhere under the target path.
    assert not (unregistered / ".pm").exists()
    for p in unregistered.rglob(".pm"):
        raise AssertionError(f"pm_outbox_merge created a .pm dir: {p}")


def test_pm_outbox_merge_unregistered_source_project_row_skipped(tmp_path: Path) -> None:
    """Same guard, but reached via the row's own source_project (no explicit
    target_project passed to pm_outbox_merge)."""
    import pmlens.server as srv

    unregistered = tmp_path / "unreg_via_row"
    unregistered.mkdir()

    rid = srv.pm_outbox_remember(
        content="orphan2", type="memory", source_project=str(unregistered)
    )["outbox_id"]
    res = srv.pm_outbox_merge(ids=[rid])

    assert res["merged"] == []
    assert any(w["reason"] == "unregistered_project" for w in res["warnings"])
    assert not (unregistered / ".pm").exists()


def test_pm_outbox_merge_registered_target_still_merges(tmp_path: Path) -> None:
    """Regression guard: the new pre-flight check must not block merges into
    an already pm_init'd project."""
    import pmlens.server as srv

    proj = _make_project(tmp_path, name="already_registered")
    rid = srv.pm_outbox_remember(content="fine", type="memory")["outbox_id"]
    res = srv.pm_outbox_merge(ids=[rid], target_project=str(proj))
    assert len(res["merged"]) == 1
    assert res["warnings"] == []


def test_pm_outbox_pending_unregistered_projects_aggregation(tmp_path: Path) -> None:
    """pm_outbox_pending surfaces {project, count} pairs for distinct
    source_project values that fail is_initialized_project, computed across
    ALL pending entries (not just the current page)."""
    import pmlens.server as srv

    registered = _make_project(tmp_path, name="reg_for_agg")
    unreg_a = tmp_path / "unreg_a"
    unreg_a.mkdir()
    unreg_b = tmp_path / "unreg_b"
    unreg_b.mkdir()

    srv.pm_outbox_remember(content="1", type="memory", source_project=str(unreg_a))
    srv.pm_outbox_remember(content="2", type="memory", source_project=str(unreg_a))
    srv.pm_outbox_remember(content="3", type="memory", source_project=str(unreg_b))
    srv.pm_outbox_remember(content="4", type="memory", source_project=str(registered))

    res = srv.pm_outbox_pending()
    by_project = {e["project"]: e["count"] for e in res["unregistered_projects"]}
    assert by_project == {str(unreg_a): 2, str(unreg_b): 1}
    assert str(registered) not in by_project


def test_pm_outbox_pending_unregistered_projects_empty_when_none(tmp_path: Path) -> None:
    import pmlens.server as srv

    proj = _make_project(tmp_path, name="all_registered")
    srv.pm_outbox_remember(content="x", type="memory", source_project=str(proj))

    res = srv.pm_outbox_pending()
    assert res["unregistered_projects"] == []


def test_pm_outbox_merge_mixed_batch_preserves_all_reasons(tmp_path: Path) -> None:
    """PMSERV-147 regression: a single pm_outbox_merge call mixing a
    registered id, an unregistered-project id, a no-target-project id, an
    already-processed id, and a not_found id must resolve each id
    independently — the new T4 pre-flight guard (inserted mid-loop) must
    not disturb any of the pre-existing per-id outcomes (spec item 3:
    'preserve every other existing behavior in this function's per-id loop
    untouched')."""
    import pmlens.server as srv

    registered = _make_project(tmp_path, name="mixed_registered")
    unregistered = tmp_path / "mixed_unregistered"
    unregistered.mkdir()

    ok_id = srv.pm_outbox_remember(content="ok", type="memory", source_project=str(registered))[
        "outbox_id"
    ]
    unreg_id = srv.pm_outbox_remember(
        content="unreg", type="memory", source_project=str(unregistered)
    )["outbox_id"]
    no_target_id = srv.pm_outbox_remember(content="no target", type="memory")["outbox_id"]
    already_id = srv.pm_outbox_remember(
        content="already", type="memory", source_project=str(registered)
    )["outbox_id"]
    # Pre-merge `already_id` in its own call so the batch below hits the
    # already_processed (skip) path rather than merging it twice.
    pre = srv.pm_outbox_merge(ids=[already_id])
    assert len(pre["merged"]) == 1
    missing_id = 999999

    res = srv.pm_outbox_merge(ids=[ok_id, unreg_id, no_target_id, already_id, missing_id])

    assert [m["id"] for m in res["merged"]] == [ok_id]
    assert res["merged"][0]["target_project"] == str(registered)

    assert [s["id"] for s in res["skipped"]] == [already_id]
    assert res["skipped"][0]["reason"] == "already_processed"

    warning_by_id = {w["id"]: w for w in res["warnings"]}
    assert warning_by_id[unreg_id]["reason"] == "unregistered_project"
    assert warning_by_id[no_target_id]["reason"] == "no_target_project"
    assert warning_by_id[missing_id]["reason"] == "not_found"
    assert not (unregistered / ".pm").exists()
