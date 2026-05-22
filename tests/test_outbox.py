"""Tests for DesktopOutboxStore (ADR-019, WF-028, PMSERV-101)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from pm_server import outbox as _outbox
from pm_server.outbox import (
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
        triggers = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
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
        triggers = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
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
        "claude-desktop", "sess-x", "memory", "hello",
        source_project="/abs/project", tags="a,b,c",
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
