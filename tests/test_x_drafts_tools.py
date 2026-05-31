"""Tool-level tests for the X content pipeline MCP tools (PMSERV-116).

pm_draft_x / pm_redact_draft / pm_reject_draft / pm_x_drafts_pending. The
end-to-end must-fix #1 check lives here: a secret put into a draft must never
surface through the review queue.
"""

from __future__ import annotations

import json
from pathlib import Path

import pm_server.server as srv

# Assembled at runtime so the literal never appears in source (GitHub secret
# scanning flags secret-shaped literals; this is FAKE test data).
_FAKE_AWS = "AKIA" + "A" * 16


def _make_project(tmp_path: Path, name: str = "xproj") -> Path:
    """Minimal project root with .pm/project.yaml + tasks.yaml + daily/."""
    proj = tmp_path / name
    (proj / ".pm" / "daily").mkdir(parents=True)
    (proj / ".pm" / "project.yaml").write_text(
        f"name: {name}\n"
        f"display_name: {name}\n"
        "version: 0.0.1\n"
        "status: development\n"
        "started: 2026-01-01\n"
        "description: x-drafts tool tests\n"
        "phases: []\n",
        encoding="utf-8",
    )
    (proj / ".pm" / "tasks.yaml").write_text("[]\n", encoding="utf-8")
    return proj


# ─── pm_draft_x ──────────────────────────────────────


def test_pm_draft_x_saves(tmp_path: Path) -> None:
    proj = _make_project(tmp_path)
    res = srv.pm_draft_x(
        signal_type="lesson",
        source_refs=["memory:190"],
        raw_content="raw concentrate",
        hook="we shipped X",
        body=["seg one", "seg two"],
        project_path=str(proj),
    )
    assert res["status"] == "saved"
    assert isinstance(res["draft_id"], int)
    assert res["source_refs"] == "memory:190"


def test_pm_draft_x_invalid_signal_type(tmp_path: Path) -> None:
    proj = _make_project(tmp_path)
    res = srv.pm_draft_x(
        signal_type="bogus",
        source_refs=["memory:1"],
        raw_content="r",
        hook="h",
        project_path=str(proj),
    )
    assert res["status"] == "error"
    assert res["code"] == "invalid_signal_type"


def test_pm_draft_x_invalid_kind(tmp_path: Path) -> None:
    proj = _make_project(tmp_path)
    res = srv.pm_draft_x(
        signal_type="lesson",
        source_refs=["memory:1"],
        raw_content="r",
        hook="h",
        kind="carousel",
        project_path=str(proj),
    )
    assert res["status"] == "error"
    assert res["code"] == "invalid_kind"


def test_pm_draft_x_empty_source_refs(tmp_path: Path) -> None:
    proj = _make_project(tmp_path)
    res = srv.pm_draft_x(
        signal_type="lesson", source_refs=[], raw_content="r", hook="h", project_path=str(proj)
    )
    assert res["status"] == "error"
    assert res["code"] == "source_refs_required"


def test_pm_draft_x_dedupes_same_source_refs(tmp_path: Path) -> None:
    proj = _make_project(tmp_path)
    first = srv.pm_draft_x(
        signal_type="lesson",
        source_refs=["ADR-024", "memory:190"],
        raw_content="r",
        hook="h",
        project_path=str(proj),
    )
    # Re-trigger with the same sources in a different order → deduped.
    second = srv.pm_draft_x(
        signal_type="lesson",
        source_refs=["memory:190", "ADR-024"],
        raw_content="r2",
        hook="h2",
        project_path=str(proj),
    )
    assert first["status"] == "saved"
    assert second["status"] == "skipped"
    assert second["warnings"][0]["reason"] == "duplicate_source_refs"
    assert first["draft_id"] in second["warnings"][0]["existing_ids"]


# ─── pm_redact_draft ─────────────────────────────────


def test_pm_redact_draft_scrubs_and_reports(tmp_path: Path) -> None:
    proj = _make_project(tmp_path)
    rid = srv.pm_draft_x(
        signal_type="lesson",
        source_refs=["memory:190"],
        raw_content="raw",
        hook=f"leak {_FAKE_AWS}",
        body=["email nakashin09@gmail.com"],
        project_path=str(proj),
    )["draft_id"]
    res = srv.pm_redact_draft(draft_id=rid, project_path=str(proj))
    assert res["status"] == "redacted"
    assert res["flagged"] is True
    # count-only report, no cleartext.
    blob = json.dumps(res["report"])
    assert _FAKE_AWS not in blob
    assert "nakashin09@gmail.com" not in blob
    assert res["report"]["total"] == 2


def test_pm_redact_draft_not_found(tmp_path: Path) -> None:
    proj = _make_project(tmp_path)
    res = srv.pm_redact_draft(draft_id=999, project_path=str(proj))
    assert res["status"] == "error"
    assert res["code"] == "not_found"


def test_pm_redact_draft_skips_non_draft(tmp_path: Path) -> None:
    proj = _make_project(tmp_path)
    rid = srv.pm_draft_x(
        signal_type="lesson",
        source_refs=["memory:1"],
        raw_content="r",
        hook="h",
        project_path=str(proj),
    )["draft_id"]
    srv.pm_redact_draft(draft_id=rid, project_path=str(proj))
    again = srv.pm_redact_draft(draft_id=rid, project_path=str(proj))
    assert again["status"] == "skipped"
    assert again["warnings"][0]["reason"] == "not_in_draft_status"


# ─── pm_reject_draft ─────────────────────────────────


def test_pm_reject_draft(tmp_path: Path) -> None:
    proj = _make_project(tmp_path)
    rid = srv.pm_draft_x(
        signal_type="lesson",
        source_refs=["memory:1"],
        raw_content="r",
        hook="h",
        project_path=str(proj),
    )["draft_id"]
    res = srv.pm_reject_draft(draft_id=rid, reason="off-topic", project_path=str(proj))
    assert res["status"] == "rejected"
    # Now the same source_refs are free again (rejected is not 'live').
    again = srv.pm_draft_x(
        signal_type="lesson",
        source_refs=["memory:1"],
        raw_content="r",
        hook="h",
        project_path=str(proj),
    )
    assert again["status"] == "saved"


def test_pm_reject_draft_requires_reason(tmp_path: Path) -> None:
    proj = _make_project(tmp_path)
    res = srv.pm_reject_draft(draft_id=1, reason="  ", project_path=str(proj))
    assert res["status"] == "error"
    assert res["code"] == "reason_required"


def test_pm_reject_draft_not_found(tmp_path: Path) -> None:
    proj = _make_project(tmp_path)
    res = srv.pm_reject_draft(draft_id=999, reason="x", project_path=str(proj))
    assert res["status"] == "error"
    assert res["code"] == "not_found"


# ─── pm_x_drafts_pending ─────────────────────────────


def test_pm_x_drafts_pending_invalid_status(tmp_path: Path) -> None:
    proj = _make_project(tmp_path)
    res = srv.pm_x_drafts_pending(filter_status="bogus", project_path=str(proj))
    assert res["status"] == "error"
    assert res["code"] == "invalid_filter_status"


def test_pm_x_drafts_pending_invalid_pagination(tmp_path: Path) -> None:
    proj = _make_project(tmp_path)
    res = srv.pm_x_drafts_pending(limit=-1, project_path=str(proj))
    assert res["status"] == "error"
    assert res["code"] == "invalid_pagination"


def test_pm_x_drafts_pending_never_leaks_raw_content(tmp_path: Path) -> None:
    """End-to-end must-fix #1: a secret put into a draft must never surface via
    the review queue the human copy-pastes from."""
    proj = _make_project(tmp_path)
    secret = _FAKE_AWS
    rid = srv.pm_draft_x(
        signal_type="lesson",
        source_refs=["memory:190"],
        raw_content=f"concentrate with {secret} and /Users/flc001/x",
        hook=f"hook with {secret}",
        body=[f"segment with {secret}"],
        project_path=str(proj),
    )["draft_id"]
    srv.pm_redact_draft(draft_id=rid, project_path=str(proj))

    page = srv.pm_x_drafts_pending(filter_status="redacted", project_path=str(proj))
    assert page["status"] == "ok"
    assert page["total"] == 1
    item = page["items"][0]
    # raw_content / hook / body_json keys absent; secret nowhere in the payload.
    assert "raw_content" not in item
    assert "hook" not in item
    assert "body_json" not in item
    assert secret not in json.dumps(page)
    assert "/Users/flc001" not in json.dumps(page)
    # redacted field IS present and scrubbed.
    assert "<REDACTED:secret>" in item["redacted_hook"]


def test_pm_redact_draft_handles_non_list_body_json(tmp_path: Path) -> None:
    """Guard: a legacy/malformed non-list body_json must not be iterated
    character-by-character (it would produce hundreds of 1-char segments)."""
    proj = _make_project(tmp_path)
    # Inject a draft whose body_json is a bare JSON string, not a list.
    store = srv._get_x_draft_store(str(proj))
    rid = store.append(
        signal_type="lesson",
        source_refs="m:1",
        raw_content="r",
        hook="h",
        body_json='"a bare string not a list"',
    )
    res = srv.pm_redact_draft(draft_id=rid, project_path=str(proj))
    assert res["status"] == "redacted"
    page = srv.pm_x_drafts_pending(filter_status="redacted", project_path=str(proj))
    segs = json.loads(page["items"][0]["redacted_body_json"])
    assert isinstance(segs, list)
    assert len(segs) == 1  # wrapped, not exploded into characters
