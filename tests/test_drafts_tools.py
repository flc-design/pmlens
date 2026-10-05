"""Tool-level tests for the content pipeline MCP tools (PMSERV-116).

pm_draft_content / pm_redact_draft / pm_reject_draft / pm_drafts_pending. The
end-to-end must-fix #1 check lives here: a secret put into a draft must never
surface through the review queue.

The last section pins the warnings for drafts built on ADRs that are not
adopted (PMSERV-225; docs/issues/DESIGN_decision-lineage-s1.md §6.1 and §9.1
row 5).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

import pmlens.server as srv
from pmlens import lineage
from pmlens.draft_store import DraftStore
from pmlens.models import Decision

# Assembled at runtime so the literal never appears in source (GitHub secret
# scanning flags secret-shaped literals; this is FAKE test data).
_FAKE_AWS = "AKIA" + "A" * 16


def _make_project(tmp_path: Path, name: str = "draftproj") -> Path:
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


# ─── pm_draft_content ──────────────────────────────────────


def test_pm_draft_content_saves(tmp_path: Path) -> None:
    proj = _make_project(tmp_path)
    res = srv.pm_draft_content(
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


def test_pm_draft_content_invalid_signal_type(tmp_path: Path) -> None:
    proj = _make_project(tmp_path)
    res = srv.pm_draft_content(
        signal_type="bogus",
        source_refs=["memory:1"],
        raw_content="r",
        hook="h",
        project_path=str(proj),
    )
    assert res["status"] == "error"
    assert res["code"] == "invalid_signal_type"


def test_pm_draft_content_invalid_kind(tmp_path: Path) -> None:
    proj = _make_project(tmp_path)
    res = srv.pm_draft_content(
        signal_type="lesson",
        source_refs=["memory:1"],
        raw_content="r",
        hook="h",
        kind="carousel",
        project_path=str(proj),
    )
    assert res["status"] == "error"
    assert res["code"] == "invalid_kind"


def test_pm_draft_content_empty_source_refs(tmp_path: Path) -> None:
    proj = _make_project(tmp_path)
    res = srv.pm_draft_content(
        signal_type="lesson", source_refs=[], raw_content="r", hook="h", project_path=str(proj)
    )
    assert res["status"] == "error"
    assert res["code"] == "source_refs_required"


def test_pm_draft_content_dedupes_same_source_refs(tmp_path: Path) -> None:
    proj = _make_project(tmp_path)
    first = srv.pm_draft_content(
        signal_type="lesson",
        source_refs=["ADR-024", "memory:190"],
        raw_content="r",
        hook="h",
        project_path=str(proj),
    )
    # Re-trigger with the same sources in a different order → deduped.
    second = srv.pm_draft_content(
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


def test_pm_draft_content_debounces_recent_distinct_draft(tmp_path: Path) -> None:
    """PMSERV-121: a second distinct draft staged in the same session (within
    the debounce window) is suppressed to avoid one-draft-per-lesson spam."""
    proj = _make_project(tmp_path)
    first = srv.pm_draft_content(
        signal_type="lesson",
        source_refs=["memory:1"],
        raw_content="r1",
        hook="h1",
        project_path=str(proj),
    )
    second = srv.pm_draft_content(
        signal_type="lesson",
        source_refs=["memory:2"],  # DIFFERENT sources — not a dedupe case
        raw_content="r2",
        hook="h2",
        project_path=str(proj),
    )
    assert first["status"] == "saved"
    assert second["status"] == "debounced"
    assert second["warnings"][0]["reason"] == "recent_draft_debounced"
    assert first["draft_id"] in second["warnings"][0]["recent_ids"]


def test_pm_draft_content_force_overrides_debounce(tmp_path: Path) -> None:
    proj = _make_project(tmp_path)
    srv.pm_draft_content(
        signal_type="lesson",
        source_refs=["memory:1"],
        raw_content="r1",
        hook="h1",
        project_path=str(proj),
    )
    forced = srv.pm_draft_content(
        signal_type="lesson",
        source_refs=["memory:2"],
        raw_content="r2",
        hook="h2",
        force=True,
        project_path=str(proj),
    )
    assert forced["status"] == "saved"


# ─── pm_redact_draft ─────────────────────────────────


def test_pm_redact_draft_scrubs_and_reports(tmp_path: Path) -> None:
    proj = _make_project(tmp_path)
    rid = srv.pm_draft_content(
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
    rid = srv.pm_draft_content(
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
    rid = srv.pm_draft_content(
        signal_type="lesson",
        source_refs=["memory:1"],
        raw_content="r",
        hook="h",
        project_path=str(proj),
    )["draft_id"]
    res = srv.pm_reject_draft(draft_id=rid, reason="off-topic", project_path=str(proj))
    assert res["status"] == "rejected"
    # Now the same source_refs are free again (rejected is not 'live').
    again = srv.pm_draft_content(
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


# ─── pm_drafts_pending ─────────────────────────────


def test_pm_drafts_pending_invalid_status(tmp_path: Path) -> None:
    proj = _make_project(tmp_path)
    res = srv.pm_drafts_pending(filter_status="bogus", project_path=str(proj))
    assert res["status"] == "error"
    assert res["code"] == "invalid_filter_status"


def test_pm_drafts_pending_invalid_pagination(tmp_path: Path) -> None:
    proj = _make_project(tmp_path)
    res = srv.pm_drafts_pending(limit=-1, project_path=str(proj))
    assert res["status"] == "error"
    assert res["code"] == "invalid_pagination"


def test_pm_drafts_pending_never_leaks_raw_content(tmp_path: Path) -> None:
    """End-to-end must-fix #1: a secret put into a draft must never surface via
    the review queue the human copy-pastes from."""
    proj = _make_project(tmp_path)
    secret = _FAKE_AWS
    rid = srv.pm_draft_content(
        signal_type="lesson",
        source_refs=["memory:190"],
        raw_content=f"concentrate with {secret} and /Users/flc001/x",
        hook=f"hook with {secret}",
        body=[f"segment with {secret}"],
        project_path=str(proj),
    )["draft_id"]
    srv.pm_redact_draft(draft_id=rid, project_path=str(proj))

    page = srv.pm_drafts_pending(filter_status="redacted", project_path=str(proj))
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
    store = srv._get_draft_store(str(proj))
    rid = store.append(
        signal_type="lesson",
        source_refs="m:1",
        raw_content="r",
        hook="h",
        body_json='"a bare string not a list"',
    )
    res = srv.pm_redact_draft(draft_id=rid, project_path=str(proj))
    assert res["status"] == "redacted"
    page = srv.pm_drafts_pending(filter_status="redacted", project_path=str(proj))
    segs = json.loads(page["items"][0]["redacted_body_json"])
    assert isinstance(segs, list)
    assert len(segs) == 1  # wrapped, not exploded into characters


@pytest.mark.parametrize(
    "create_name, review_name",
    [("pm_draft_content", "pm_x_drafts_pending"), ("pm_draft_x", "pm_drafts_pending")],
)
@pytest.mark.parametrize("db_name", ["drafts.db", "x_drafts.db"])
def test_content_names_share_existing_drafts(
    tmp_path: Path, create_name: str, review_name: str, db_name: str
) -> None:
    """Switching names preserves existing IDs, dedupe and redaction (PMSERV-189)."""
    proj = _make_project(tmp_path)
    store = DraftStore(proj / ".pm" / db_name)
    original_id = store.append(
        signal_type="lesson",
        source_refs="memory:190",
        raw_content=f"private {_FAKE_AWS}",
        hook=f"a lesson {_FAKE_AWS}",
        body_json=json.dumps([f"segment {_FAKE_AWS}"]),
    )
    create = getattr(srv, create_name)
    review = getattr(srv, review_name)
    duplicate = create(
        signal_type="lesson",
        source_refs=["memory:190"],
        raw_content="duplicate",
        hook="duplicate",
        force=True,
        project_path=str(proj),
    )
    assert duplicate["status"] == "skipped"
    assert duplicate["warnings"][0]["existing_ids"] == [original_id]
    assert srv.pm_redact_draft(original_id, project_path=str(proj))["status"] == "redacted"
    page = review(project_path=str(proj))
    assert page["total"] == 1
    assert page["items"][0]["id"] == original_id
    assert _FAKE_AWS not in json.dumps(page)
    assert "raw_content" not in page["items"][0]
    assert page == srv.pm_drafts_pending(project_path=str(proj))
    assert page == srv.pm_x_drafts_pending(project_path=str(proj))

    # Every optional argument must survive either entry point's delegation.
    args = dict(
        signal_type="insight",
        source_refs=["memory:191"],
        raw_content="new concentrate",
        hook="new hook",
        body=["first", "second"],
        kind="single",
        hashtags=["one", "two"],
        workflow_id="WF-001",
        project_path=str(proj),
    )
    assert create(**args)["status"] == "debounced"
    saved = create(**args, force=True)
    row = store.get(saved["draft_id"])
    assert saved["status"] == "saved"
    assert row["id"] > original_id
    assert row["raw_content"] == args["raw_content"]
    assert row["hook"] == args["hook"]
    assert json.loads(row["body_json"]) == args["body"]
    assert row["kind"] == "single"
    assert row["hashtags"] == "one,two"
    assert row["workflow_id"] == "WF-001"
    assert (proj / ".pm" / db_name).is_file()
    other_name = "drafts.db" if db_name == "x_drafts.db" else "x_drafts.db"
    assert not (proj / ".pm" / other_name).exists()


@pytest.mark.parametrize(
    "name, args",
    [
        (
            "pm_draft_content",
            {
                "signal_type": "lesson",
                "source_refs": ["m:new"],
                "raw_content": "raw",
                "hook": "hook",
            },
        ),
        (
            "pm_draft_x",
            {
                "signal_type": "lesson",
                "source_refs": ["m:new"],
                "raw_content": "raw",
                "hook": "hook",
            },
        ),
        ("pm_redact_draft", {"draft_id": 1}),
        ("pm_reject_draft", {"draft_id": 1, "reason": "reject"}),
        ("pm_drafts_pending", {}),
        ("pm_x_drafts_pending", {}),
    ],
)
def test_conflicting_databases_block_tools_without_touching_either(
    tmp_path: Path, name: str, args: dict
) -> None:
    proj = _make_project(tmp_path)
    for filename in ("drafts.db", "x_drafts.db"):
        store = (
            srv._get_draft_store(str(proj))
            if filename == "drafts.db"
            else DraftStore(proj / ".pm" / filename)
        )
        store.append("lesson", "m:existing", f"private {filename}")
    # A cached canonical store must not hide a legacy store appearing later.
    # Both database files and any sidecars must stay byte-identical.
    before = {p.name: p.read_bytes() for p in (proj / ".pm").glob("*.db*")}
    result = getattr(srv, name)(project_path=str(proj), **args)
    assert result["status"] == "error"
    assert result["code"] == "draft_store_conflict"
    assert "back up both databases" in result["message"]
    assert "private" not in json.dumps(result)
    after = {p.name: p.read_bytes() for p in (proj / ".pm").glob("*.db*")}
    assert after == before


# ─── Drafts built on ADRs that are not adopted (PMSERV-225) ─────

_NOT_ADOPTED = "draft_source_decision_not_adopted"
_NOT_FOUND = "draft_source_decision_not_found"
_UNCHECKED = "draft_source_decision_unchecked"
_MISSING = "draft_source_decision_missing"

# The keys of each response when there is nothing to warn about: exactly what
# the tools returned before the guard existed.
_SAVED_KEYS = {"status", "draft_id", "source_refs", "next"}
_REDACTED_KEYS = {"status", "draft_id", "report", "flagged", "skill_hint"}
_PENDING_KEYS = {"status", "items", "total", "has_more", "next_offset"}


def _codes(result: dict) -> list[str | None]:
    return [warning.get("code") for warning in result.get("warnings", [])]


def _move(proj: Path, adr_id: str, lifecycle: str, **extra: object) -> None:
    res = srv.pm_update_decision(
        decision_id=adr_id,
        lifecycle=lifecycle,
        reason="the user decided",
        project_path=str(proj),
        **extra,
    )
    assert res["status"] != "error", res


def _record_adr(proj: Path, lifecycle: str = "proposed") -> str:
    """Record an ADR with pm_add_decision and move it to ``lifecycle`` with the tools."""
    added = srv.pm_add_decision(
        title=f"a {lifecycle} decision", context="c", decision="d", project_path=str(proj)
    )
    assert added["status"] == "recorded"
    adr_id = added["decision_id"]
    if lifecycle in {"adopted", "deprecated", "reverted"}:
        _move(proj, adr_id, "adopted")
    if lifecycle in {"rejected", "deprecated", "reverted"}:
        _move(proj, adr_id, lifecycle)
    if lifecycle == "superseded":
        successor = srv.pm_add_decision(
            title="its successor", context="c", decision="d", project_path=str(proj)
        )["decision_id"]
        _move(proj, adr_id, "superseded", add_links={"superseded_by": [successor]})
    shown = srv.pm_decision_query(action="get", decision_id=adr_id, project_path=str(proj))
    assert shown["lineage"]["lifecycle"] == lifecycle
    assert shown["lineage"]["derived"] is False
    return adr_id


def _entry(adr_id: str, status: str = "accepted", title: str | None = None) -> dict:
    return {
        "id": adr_id,
        "title": title or f"title {adr_id}",
        "date": "2026-10-01",
        "status": status,
        "context": "c",
        "decision": "d",
    }


def _write_decisions(proj: Path, *entries: dict) -> None:
    """decisions.yaml as an older pmlens or a hand edit leaves it (no lineage files)."""
    (proj / ".pm" / "decisions.yaml").write_text(
        yaml.safe_dump({"decisions": list(entries)}, sort_keys=False), encoding="utf-8"
    )


def _draft(
    proj: Path,
    refs: list[str],
    *,
    signal_type: str = "lesson",
    create_name: str = "pm_draft_content",
    force: bool = True,
) -> dict:
    res = getattr(srv, create_name)(
        signal_type=signal_type,
        source_refs=refs,
        raw_content="raw",
        hook="hook",
        force=force,
        project_path=str(proj),
    )
    assert res["status"] == "saved", res
    return res


@pytest.mark.parametrize("create_name", ["pm_draft_content", "pm_draft_x"])
@pytest.mark.parametrize(
    "lifecycle", ["proposed", "rejected", "reverted", "deprecated", "superseded"]
)
def test_draft_built_on_an_adr_that_is_not_adopted_warns(
    tmp_path: Path, lifecycle: str, create_name: str
) -> None:
    proj = _make_project(tmp_path)
    adr_id = _record_adr(proj, lifecycle)
    res = _draft(proj, [adr_id, "memory:1"], create_name=create_name)
    assert set(res) == _SAVED_KEYS | {"warnings"}
    [warning] = res["warnings"]
    assert warning["code"] == _NOT_ADOPTED
    assert warning["level"] == "warning"
    assert warning["message"].startswith(f"Draft {res['draft_id']}: {adr_id} is {lifecycle} ")
    assert "pm_reject_draft" in warning["remediation"]


@pytest.mark.parametrize("signal_type", ["lesson", "insight", "adr", "mistake"])
def test_every_signal_type_is_checked(tmp_path: Path, signal_type: str) -> None:
    proj = _make_project(tmp_path)
    adr_id = _record_adr(proj)
    assert _codes(_draft(proj, [adr_id], signal_type=signal_type)) == [_NOT_ADOPTED]


def test_adopted_and_derived_accepted_adrs_leave_every_response_as_before(
    tmp_path: Path,
) -> None:
    proj = _make_project(tmp_path)
    # ADR-001 has no lineage file; accepted in decisions.yaml reads as adopted.
    _write_decisions(proj, _entry("ADR-001", "accepted"))
    adopted = _record_adr(proj, "adopted")
    legacy = srv.pm_decision_query(action="get", decision_id="ADR-001", project_path=str(proj))
    assert legacy["lineage"]["derived"] is True

    saved = _draft(proj, ["ADR-001", adopted, "memory:1"], signal_type="adr")
    assert set(saved) == _SAVED_KEYS
    redacted = srv.pm_redact_draft(saved["draft_id"], project_path=str(proj))
    assert redacted["status"] == "redacted"
    assert set(redacted) == _REDACTED_KEYS
    page = srv.pm_drafts_pending(project_path=str(proj))
    assert page["total"] == 1
    assert set(page) == _PENDING_KEYS
    assert page == srv.pm_x_drafts_pending(project_path=str(proj))


@pytest.mark.parametrize("status", ["proposed", "deprecated", "superseded"])
def test_an_adr_without_lineage_is_judged_by_its_status(tmp_path: Path, status: str) -> None:
    proj = _make_project(tmp_path)
    _write_decisions(proj, _entry("ADR-007", status))
    [warning] = _draft(proj, ["ADR-007"])["warnings"]
    assert warning["code"] == _NOT_ADOPTED
    assert "ADR-007 is " + status in warning["message"]


def test_an_unknown_status_is_not_adopted_and_not_quoted(tmp_path: Path) -> None:
    proj = _make_project(tmp_path)
    _write_decisions(proj, _entry("ADR-007", f"frozen {_FAKE_AWS}"))
    res = _draft(proj, ["ADR-007"])
    [warning] = res["warnings"]
    assert warning["code"] == _NOT_ADOPTED
    assert "ADR-007 has no known lifecycle" in warning["message"]
    assert _FAKE_AWS not in json.dumps(res)


def test_the_lineage_decides_unless_it_was_written_for_another_adr(tmp_path: Path) -> None:
    proj = _make_project(tmp_path)
    # Both are proposed in decisions.yaml; both lineage files say adopted.
    _write_decisions(proj, _entry("ADR-001", "proposed"), _entry("ADR-002", "proposed"))
    for adr_id in ("ADR-001", "ADR-002"):
        doc = lineage.new_lineage_doc(Decision(**_entry(adr_id)), {}, "2026-10-05T03:12:00Z")
        assert doc["lifecycle"] == "adopted"
        if adr_id == "ADR-002":  # left behind by an ADR with another title
            doc["anchor"]["title_sha256"] = lineage.title_sha256("another title")
        path = proj / ".pm" / "decision_lineage" / f"{adr_id}.yaml"
        path.parent.mkdir(exist_ok=True)
        path.write_text(lineage.dump_lineage(doc, adr_id), encoding="utf-8")

    # ADR-001: the lineage is the source of truth, so it is adopted. ADR-002:
    # the lineage is not attributed, so its status (proposed) decides.
    [warning] = _draft(proj, ["ADR-001", "ADR-002"])["warnings"]
    assert warning["code"] == _NOT_ADOPTED
    assert "ADR-002 is proposed" in warning["message"]


def test_a_ref_not_in_decisions_yaml_is_info(tmp_path: Path) -> None:
    proj = _make_project(tmp_path)
    res = _draft(proj, ["ADR-024", "memory:1"])
    [warning] = res["warnings"]
    assert warning["code"] == _NOT_FOUND
    assert warning["level"] == "info"
    assert warning["message"].startswith(f"Draft {res['draft_id']}: ADR-024 ")

    _write_decisions(proj, _entry("ADR-001"))
    assert _codes(_draft(proj, ["ADR-024", "ADR-001"])) == [_NOT_FOUND]


@pytest.mark.parametrize("ids", [("ADR-059", "ADR-059"), ("ADR-059", "ADR-59")])
def test_an_adr_number_held_by_two_adrs_is_not_adopted(
    tmp_path: Path, ids: tuple[str, str]
) -> None:
    proj = _make_project(tmp_path)
    _write_decisions(
        proj, *(_entry(adr_id, "accepted", title=f"t{n}") for n, adr_id in enumerate(ids))
    )
    [warning] = _draft(proj, ["ADR-059"])["warnings"]
    assert warning["code"] == _NOT_ADOPTED
    assert "matches 2 ADRs" in warning["message"]


@pytest.mark.parametrize("ref", ["ADR-59", "adr-059", "Adr-0059", " ADR-000059 "])
def test_refs_match_by_number_whatever_the_case_or_padding(tmp_path: Path, ref: str) -> None:
    proj = _make_project(tmp_path)
    _write_decisions(proj, _entry("ADR-059", "proposed"), _entry("ADR-060", "accepted"))
    [warning] = _draft(proj, [ref])["warnings"]
    assert warning["code"] == _NOT_ADOPTED
    assert "ADR-059 is proposed" in warning["message"]
    # The same spelling of an adopted ADR finds it too: no warning, not not_found.
    assert "warnings" not in _draft(proj, [ref.replace("59", "60")])
    # Two spellings of one ADR are reported once.
    assert _codes(_draft(proj, [ref, "ADR-059"])) == [_NOT_ADOPTED]


def test_an_adr_draft_without_an_adr_ref_is_reported_missing(tmp_path: Path) -> None:
    proj = _make_project(tmp_path)
    refs = ["memory:1", "PMSERV-225", "ADR-1234567", "ADR-59x", "xADR-059", "ADR-"]
    res = _draft(proj, refs, signal_type="adr")
    [warning] = res["warnings"]
    assert warning["code"] == _MISSING
    assert warning["level"] == "warning"
    assert warning["message"].startswith(f"Draft {res['draft_id']}: ")
    # The same refs on another signal type name no ADR, so nothing is checked.
    assert "warnings" not in _draft(proj, ["memory:2", *refs[1:]], signal_type="lesson")


@pytest.mark.parametrize(
    "broken",
    [
        # A YAML syntax error on a line that holds a secret.
        f'decisions:\n- id: ADR-001\n  title: "unterminated {_FAKE_AWS}\n',
        # A ValidationError whose input_value is a secret.
        f"decisions:\n- id: ADR-001\n  title: [{_FAKE_AWS}]\n",
    ],
)
def test_an_unreadable_decisions_yaml_is_reported_unchecked_without_its_text(
    tmp_path: Path, broken: str
) -> None:
    proj = _make_project(tmp_path)
    (proj / ".pm" / "decisions.yaml").write_text(broken, encoding="utf-8")
    res = _draft(proj, ["ADR-001"])
    [warning] = res["warnings"]
    assert warning["code"] == _UNCHECKED
    assert warning["level"] == "warning"
    assert warning["message"].startswith(f"Draft {res['draft_id']}: ")
    assert _FAKE_AWS not in json.dumps(res)
    # The review queue still lists the draft, with the same warning.
    page = srv.pm_drafts_pending(filter_status="draft", project_path=str(proj))
    assert page["total"] == 1
    assert _codes(page) == [_UNCHECKED]
    assert _FAKE_AWS not in json.dumps(page)
    # A draft that cites no ADR does not depend on decisions.yaml.
    assert "warnings" not in _draft(proj, ["memory:1"])


def test_review_and_redaction_judge_the_adr_when_they_run(tmp_path: Path) -> None:
    proj = _make_project(tmp_path)
    adr_id = _record_adr(proj, "adopted")
    first = _draft(proj, [adr_id])
    assert "warnings" not in first
    assert "warnings" not in srv.pm_redact_draft(first["draft_id"], project_path=str(proj))
    assert "warnings" not in srv.pm_drafts_pending(project_path=str(proj))

    # The user turns the decision down after the draft was staged.
    _move(proj, adr_id, "proposed")
    _move(proj, adr_id, "rejected")
    page = srv.pm_drafts_pending(project_path=str(proj))
    assert set(page) == _PENDING_KEYS | {"warnings"}
    [warning] = page["warnings"]
    assert warning["code"] == _NOT_ADOPTED
    assert warning["message"].startswith(f"Draft {first['draft_id']}: {adr_id} is rejected ")
    assert page == srv.pm_x_drafts_pending(project_path=str(proj))

    second = _draft(proj, [adr_id, "memory:2"])
    assert _codes(second) == [_NOT_ADOPTED]
    redacted = srv.pm_redact_draft(second["draft_id"], project_path=str(proj))
    assert redacted["status"] == "redacted"
    assert set(redacted) == _REDACTED_KEYS | {"warnings"}
    [warning] = redacted["warnings"]
    assert warning["code"] == _NOT_ADOPTED
    assert warning["message"].startswith(f"Draft {second['draft_id']}: {adr_id} is rejected ")


def test_review_queue_names_each_draft_and_reads_decisions_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    proj = _make_project(tmp_path)
    adr_id = _record_adr(proj)
    ids = [_draft(proj, [adr_id, f"memory:{n}"])["draft_id"] for n in range(3)]
    # A rejected draft is out of review; it is not reported.
    assert srv.pm_reject_draft(ids[2], reason="off-topic", project_path=str(proj))["status"] == (
        "rejected"
    )
    reads: list[Path] = []
    real_load = srv.load_decisions

    def counting_load(pm_path: Path) -> list[Decision]:
        reads.append(pm_path)
        return real_load(pm_path)

    monkeypatch.setattr(srv, "load_decisions", counting_load)
    page = srv.pm_drafts_pending(filter_status="all", project_path=str(proj))
    assert page["total"] == 3
    assert _codes(page) == [_NOT_ADOPTED, _NOT_ADOPTED]
    named = {int(w["message"].split(":")[0].removeprefix("Draft ")) for w in page["warnings"]}
    assert named == set(ids[:2])
    assert len(reads) == 1


def test_skipped_and_debounced_responses_are_unchanged(tmp_path: Path) -> None:
    proj = _make_project(tmp_path)
    adr_id = _record_adr(proj)
    first = _draft(proj, [adr_id], force=False)
    assert _codes(first) == [_NOT_ADOPTED]

    def stage(refs: list[str]) -> dict:
        return srv.pm_draft_content(
            signal_type="lesson",
            source_refs=refs,
            raw_content="r",
            hook="h",
            project_path=str(proj),
        )

    skipped = stage([adr_id])
    assert skipped["status"] == "skipped"
    assert set(skipped) == {"status", "source_refs", "warnings"}
    [only] = skipped["warnings"]
    assert only["reason"] == "duplicate_source_refs"
    assert "code" not in only

    debounced = stage([adr_id, "memory:9"])
    assert debounced["status"] == "debounced"
    assert set(debounced) == {"status", "source_refs", "warnings"}
    [only] = debounced["warnings"]
    assert only["reason"] == "recent_draft_debounced"
    assert "code" not in only

    srv.pm_redact_draft(first["draft_id"], project_path=str(proj))
    again = srv.pm_redact_draft(first["draft_id"], project_path=str(proj))
    assert again["status"] == "skipped"
    [only] = again["warnings"]
    assert only["reason"] == "not_in_draft_status"
    assert "code" not in only


def test_adr_ids_from_decisions_yaml_are_stripped_and_bounded_in_messages(
    tmp_path: Path,
) -> None:
    proj = _make_project(tmp_path)
    padding = " " * 5000 + "\n\t"
    _write_decisions(proj, _entry("ADR-059" + padding, "proposed"))
    [warning] = _draft(proj, ["ADR-059"])["warnings"]
    assert warning["code"] == _NOT_ADOPTED
    assert "ADR-059 is proposed (not adopted)" in warning["message"]
    assert len(warning["message"]) < 300

    # Twelve spellings of ADR-059, each padded: the warning names a few of them.
    spellings = [
        f"{prefix}-{'0' * zeros}59"
        for prefix in ("ADR", "adr", "Adr")
        for zeros in range(5)
        if not (prefix == "Adr" and zeros > 1)
    ]
    assert len(spellings) == 12
    _write_decisions(
        proj,
        *(
            _entry(adr_id + padding, "accepted", title=f"t{n}")
            for n, adr_id in enumerate(spellings)
        ),
    )
    [warning] = _draft(proj, ["ADR-059", "memory:1"])["warnings"]
    assert warning["code"] == _NOT_ADOPTED
    assert "matches 12 ADRs" in warning["message"]
    assert f"({', '.join(spellings[:5])} and 7 more)" in warning["message"]
    assert len(warning["message"]) < 300
    assert "  " not in warning["message"]
    assert "\n" not in warning["message"]


@pytest.mark.parametrize("code", [_NOT_ADOPTED, _MISSING, _UNCHECKED])
def test_a_posted_draft_is_still_checked_but_not_sent_to_pm_reject_draft(
    tmp_path: Path, code: str
) -> None:
    proj = _make_project(tmp_path)
    if code == _NOT_ADOPTED:
        refs, signal_type = [_record_adr(proj)], "lesson"
    elif code == _MISSING:
        refs, signal_type = ["memory:1"], "adr"
    else:
        (proj / ".pm" / "decisions.yaml").write_text("decisions: [\n", encoding="utf-8")
        refs, signal_type = ["ADR-001"], "lesson"
    draft_id = _draft(proj, refs, signal_type=signal_type)["draft_id"]
    assert srv.pm_redact_draft(draft_id, project_path=str(proj))["status"] == "redacted"
    [in_review] = srv.pm_drafts_pending(project_path=str(proj))["warnings"]
    assert in_review["code"] == code

    # The user published it by hand and marked it posted: it can no longer be
    # rejected, so the warning stays but its remediation points at the post.
    assert srv._get_draft_store(str(proj)).mark_posted(draft_id)
    page = srv.pm_drafts_pending(filter_status="posted", project_path=str(proj))
    assert page["total"] == 1
    [posted] = page["warnings"]
    assert posted["code"] == code
    assert posted["message"] == in_review["message"]
    for phrase in ("otherwise reject", "stage it again", "before it is published"):
        assert phrase not in posted["remediation"]
    if code != _UNCHECKED:
        assert posted["remediation"] == srv._DRAFT_POSTED_REMEDIATION
    rejected = srv.pm_reject_draft(draft_id, reason="too late", project_path=str(proj))
    assert rejected["status"] == "skipped"
    assert rejected["warnings"][0]["reason"] == "already_terminal"
