"""Tests for the pm_status x_drafts_pending diagnostic (PMSERV-118).

Mirrors the outbox_pending diagnostic: Claude-Code-only, surfaces a hint when
> 0, and (must-fix #3 corollary) never creates x_drafts.db just by probing.
"""

from __future__ import annotations

from pathlib import Path

import pmlens.server as srv


def _make_project(tmp_path: Path, name: str = "statxproj") -> Path:
    proj = tmp_path / name
    (proj / ".pm" / "daily").mkdir(parents=True)
    (proj / ".pm" / "project.yaml").write_text(
        f"name: {name}\ndisplay_name: {name}\nversion: 0.0.1\n"
        "status: development\nstarted: 2026-01-01\ndescription: status test\nphases: []\n",
        encoding="utf-8",
    )
    (proj / ".pm" / "tasks.yaml").write_text("[]\n", encoding="utf-8")
    return proj


def test_pm_status_x_drafts_zero_does_not_create_db(tmp_path: Path) -> None:
    """A project that has never used the pipeline reports 0 AND no x_drafts.db
    is created just by calling pm_status."""
    proj = _make_project(tmp_path)
    status = srv.pm_status(project_path=str(proj))
    assert status["diagnostics"]["x_drafts_pending"] == 0
    assert not (proj / ".pm" / "x_drafts.db").exists()
    assert not any("X draft" in line for line in status["next_pm_actions"])


def test_pm_status_x_drafts_pending_counts_and_hints(tmp_path: Path) -> None:
    proj = _make_project(tmp_path)
    srv.pm_draft_x(
        signal_type="lesson",
        source_refs=["memory:190"],
        raw_content="raw",
        hook="a hook",
        body=["seg"],
        project_path=str(proj),
    )
    status = srv.pm_status(project_path=str(proj))
    assert status["diagnostics"]["x_drafts_pending"] == 1
    assert any("X draft" in line and "pending review" in line for line in status["next_pm_actions"])
