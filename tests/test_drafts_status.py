"""Tests for the pm_status x_drafts_pending diagnostic (PMSERV-118).

Mirrors the outbox_pending diagnostic: Claude-Code-only, surfaces a hint when
> 0, and (must-fix #3 corollary) never creates a draft database just by probing.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import pmlens.server as srv
from pmlens.draft_store import DraftStore


def _make_project(tmp_path: Path, name: str = "statdraftproj") -> Path:
    proj = tmp_path / name
    (proj / ".pm" / "daily").mkdir(parents=True)
    (proj / ".pm" / "project.yaml").write_text(
        f"name: {name}\ndisplay_name: {name}\nversion: 0.0.1\n"
        "status: development\nstarted: 2026-01-01\ndescription: status test\nphases: []\n",
        encoding="utf-8",
    )
    (proj / ".pm" / "tasks.yaml").write_text("[]\n", encoding="utf-8")
    return proj


def test_pm_status_drafts_zero_does_not_create_db(tmp_path: Path) -> None:
    """A project that has never used the pipeline reports 0 AND no draft database
    is created just by calling pm_status."""
    proj = _make_project(tmp_path)
    status = srv.pm_status(project_path=str(proj))
    assert status["diagnostics"]["x_drafts_pending"] == 0
    assert not (proj / ".pm" / "x_drafts.db").exists()
    assert not (proj / ".pm" / "drafts.db").exists()
    assert not any("content draft" in line for line in status["next_pm_actions"])


def test_pm_status_drafts_pending_counts_and_hints(tmp_path: Path) -> None:
    proj = _make_project(tmp_path)
    srv.pm_draft_content(
        signal_type="lesson",
        source_refs=["memory:190"],
        raw_content="raw",
        hook="a hook",
        body=["seg"],
        project_path=str(proj),
    )
    status = srv.pm_status(project_path=str(proj))
    assert status["diagnostics"]["x_drafts_pending"] == 1
    assert any(
        "content draft" in line and "pm_drafts_pending" in line
        for line in status["next_pm_actions"]
    )


@pytest.mark.parametrize("filename", ["drafts.db", "x_drafts.db"])
def test_pm_status_counts_the_existing_database(tmp_path: Path, filename: str) -> None:
    proj = _make_project(tmp_path)
    store = DraftStore(proj / ".pm" / filename)
    store.append("lesson", "m:1", "raw")
    status = srv.pm_status(project_path=str(proj))
    assert status["diagnostics"]["x_drafts_pending"] == 1
    other = "drafts.db" if filename == "x_drafts.db" else "x_drafts.db"
    assert not (proj / ".pm" / other).exists()


def test_pm_status_reports_conflict_instead_of_an_empty_queue(tmp_path: Path) -> None:
    proj = _make_project(tmp_path)
    paths = [proj / ".pm" / name for name in ("drafts.db", "x_drafts.db")]
    for path in paths:
        path.write_bytes(b"this database must not be opened")
    before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in paths}
    status = srv.pm_status(project_path=str(proj))
    assert status["diagnostics"]["x_drafts_pending"] is None
    warnings = [w for w in status["warnings"] if w.get("code") == "draft_store_conflict"]
    assert len(warnings) == 1
    assert "back up both databases" in warnings[0]["remediation"]
    assert warnings[0]["remediation"] in status["next_pm_actions"]
    assert {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in paths} == before


def test_lens_status_does_not_probe_draft_paths(tmp_path: Path, monkeypatch) -> None:
    proj = _make_project(tmp_path)

    def forbid_probe(*args):
        raise AssertionError("Lens must not resolve or open draft stores")

    monkeypatch.setattr(srv, "PM_LENS_ENABLED", True)
    monkeypatch.setattr(srv, "default_draft_db_path", forbid_probe)
    status = srv.pm_status(project_path=str(proj))
    assert "x_drafts_pending" not in status["diagnostics"]
