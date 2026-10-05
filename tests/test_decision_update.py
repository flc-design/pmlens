"""pm_update_decision: an ADR's lifecycle, links and follow-up record (ADR-056 S1, PMSERV-224).

What this file pins, by the design document's numbering
(docs/issues/DESIGN_decision-lineage-s1.md, §4.3 and §9.1 row 4), through the
tool itself rather than the storage functions under it:

* The transition table: all 36 cells, the diagonal included (an allowed no-op
  that leaves the files alone, and that re-projects a stale status).
* Invariants 1 and 2 are checked only for what a call changes: a superseded
  ADR without a successor still takes notes and amends links, and can move to
  deprecated. Invariants 3 to 5 always apply.
* The status projection (adopted → accepted, rejected → deprecated, reverted
  with a successor → superseded), and, byte for byte, that only the ADR's
  ``status:`` line of decisions.yaml changes.
* lineage_started, the status mismatch rule (§3.3 rule 4), duplicate ids,
  decision_status_not_projected (a fault injected into _save_decisions) and the
  explicit _require_known_status check before anything is written.
* The warnings table: decision_lifecycle_changed, the supersedes hint
  (``next``) and its info, anchor_missing, unchanged calls.
* The restricted backfill of declared values (ADR-059 Q2).
* D8 (a)-(d): no argument sets accuracy, verification, human confirmation or a
  changed decision_kind; pm_update_decision's arguments are exactly the
  design's; origin can only be backfilled as ai_auto.
* D11: secrets in reason / note / evaluation never reach the lineage file or
  the response, and a broken decisions.yaml is reported without its text.
* Every error code returns the error dict and leaves ``.pm`` byte-for-byte as
  it was.
"""

from __future__ import annotations

import ast
import datetime as dt
import inspect
import json
import re
from collections.abc import Callable
from pathlib import Path

import pytest
import yaml

from pmlens import lineage, server, storage
from pmlens.lineage import (
    LIFECYCLES,
    MAX_LINEAGE_BYTES,
    allowed_to,
    anchor_for,
    dump_lineage,
    new_lineage_doc,
    project_status,
)
from pmlens.models import Consequences, Decision, DecisionStatus, PmServerError
from pmlens.server import pm_add_decision, pm_decision_query, pm_update_decision

NOW = "2026-10-05T03:12:00Z"
SECRET = "AKIA" + "Q" * 16
ADR_DATE = dt.date(2026, 10, 1)


@pytest.fixture(autouse=True)
def _fixed_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(lineage, "_utc_now", lambda: NOW)


# ─── Helpers ─────────────────────────────────────────


def _pm(project: Path) -> Path:
    return project / ".pm"


def _update(project: Path, decision_id: str = "ADR-001", **kwargs) -> dict:
    return pm_update_decision(decision_id=decision_id, project_path=str(project), **kwargs)


def _adr(
    adr_id: str = "ADR-001", status: DecisionStatus | str = DecisionStatus.ACCEPTED, **extra
) -> Decision:
    return Decision(
        id=adr_id,
        title=extra.pop("title", f"title {adr_id}"),
        date=ADR_DATE,
        status=status,
        context=extra.pop("context", "c"),
        decision=extra.pop("decision", "d"),
        **extra,
    )


def _seed(project: Path, *adrs: Decision) -> None:
    storage._save_decisions(_pm(project), list(adrs))


def _lineage_path(project: Path, adr_id: str = "ADR-001") -> Path:
    return _pm(project) / "decision_lineage" / f"{adr_id}.yaml"


def _doc(adr: Decision, lifecycle: str, superseded_by: list[str] | None = None) -> dict:
    """A lineage document as S1 writes it for ``adr``, at ``lifecycle``."""
    known = isinstance(adr.status, DecisionStatus)
    doc = new_lineage_doc(adr if known else adr.model_copy(update={"status": "accepted"}), {}, NOW)
    doc["anchor"] = anchor_for(adr)
    doc["lifecycle"] = lifecycle
    doc["links"]["superseded_by"] = list(superseded_by or [])
    doc["events"][0]["lifecycle"] = lifecycle
    return doc


def _put_lineage(project: Path, adr_id: str, content: dict | str) -> Path:
    path = _lineage_path(project, adr_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = content if isinstance(content, str) else dump_lineage(content, adr_id)
    path.write_text(text, encoding="utf-8")
    return path


def _with_lineage(
    project: Path, lifecycle: str, *, superseded_by: list[str] | None = None, others: int = 1
) -> None:
    """ADR-001 at ``lifecycle`` with its projected status, plus ADR-002.. as link targets."""
    successors = list(superseded_by or [])
    adr = _adr("ADR-001", project_status(lifecycle, successors))
    rest = [_adr(f"ADR-{n:03d}") for n in range(2, 2 + others)]
    _seed(project, adr, *rest)
    _put_lineage(project, "ADR-001", _doc(adr, lifecycle, successors))


def _read_lineage(project: Path, adr_id: str = "ADR-001") -> dict:
    return yaml.safe_load(_lineage_path(project, adr_id).read_text(encoding="utf-8"))


def _status(project: Path, adr_id: str = "ADR-001") -> DecisionStatus | str:
    [adr] = [d for d in storage.load_decisions(_pm(project)) if d.id == adr_id]
    return adr.status


def _decisions_lines(project: Path) -> list[str]:
    return (_pm(project) / "decisions.yaml").read_text(encoding="utf-8").splitlines()


def _snapshot(root: Path) -> dict[str, object]:
    """Every file under ``.pm`` but the lock files (taking a lock creates them)."""
    out: dict[str, object] = {}
    for path in sorted(root.rglob("*")):
        key = str(path.relative_to(root))
        if key == ".locks" or key.startswith(".locks/"):
            continue
        out[key] = "dir" if path.is_dir() else path.read_bytes()
    return out


def _codes(result: dict) -> list[str]:
    return [warning["code"] for warning in result.get("warnings", [])]


def _warning(result: dict, code: str) -> dict:
    [found] = [warning for warning in result.get("warnings", []) if warning["code"] == code]
    return found


def _add(project: Path, title: str = "Use SQLite", **kwargs) -> dict:
    result = pm_add_decision(
        title=title, context="c", decision="d", project_path=str(project), **kwargs
    )
    assert result["status"] == "recorded", result
    return result


# ─── The transition table (design §4.3) ──────────────


# ○ cells of the design's table, written out from the document (rows are the
# source). The diagonal (◎) is an allowed no-op and is not listed.
_DESIGN_TABLE: dict[str, set[str]] = {
    "proposed": {"adopted", "superseded", "rejected"},
    "adopted": {"proposed", "deprecated", "superseded", "reverted"},
    "deprecated": {"adopted", "superseded"},
    "superseded": {"adopted", "deprecated"},
    "rejected": {"proposed"},
    "reverted": {"proposed"},
}


def test_allowed_to_is_the_design_table():
    assert set(_DESIGN_TABLE) == set(LIFECYCLES)
    for source, targets in _DESIGN_TABLE.items():
        assert set(allowed_to(source)) == targets, source


@pytest.mark.parametrize("target", LIFECYCLES)
@pytest.mark.parametrize("source", LIFECYCLES)
def test_every_cell_of_the_transition_table(tmp_project: Path, source: str, target: str):
    """All 36 cells through the tool: allowed moves project the status, others write nothing."""
    successors = ["ADR-002"] if source == "superseded" else []
    _with_lineage(tmp_project, source, superseded_by=successors)
    kwargs: dict = {"lifecycle": target, "reason": "the user decided"}
    if target == "superseded" and source != "superseded":
        kwargs["add_links"] = {"superseded_by": ["ADR-002"]}
    if successors and target not in ("superseded", "reverted"):
        kwargs["remove_links"] = {"superseded_by": ["ADR-002"]}
    before = _snapshot(_pm(tmp_project))
    lines_before = _decisions_lines(tmp_project)

    result = _update(tmp_project, **kwargs)

    if source == target:
        # The diagonal is an allowed no-op: nothing to change, nothing written.
        assert result["status"] == "unchanged", result
        assert result["lifecycle"] == source
        assert result["changes"] == {} and result["events_added"] == []
        assert _snapshot(_pm(tmp_project)) == before
    elif target in _DESIGN_TABLE[source]:
        assert result["status"] == "updated", result
        expected = project_status(target, result["links"]["superseded_by"])
        assert result["lifecycle"] == target
        assert result["decision_status"] == expected.value
        assert result["changes"]["lifecycle"] == {"from": source, "to": target}
        assert "lifecycle" in result["events_added"]
        doc = _read_lineage(tmp_project)
        assert doc["lifecycle"] == target
        assert doc["events"][-1]["kind"] in ("lifecycle", "link")
        assert _status(tmp_project) == expected
        changed = [
            (b, a)
            for b, a in zip(lines_before, _decisions_lines(tmp_project), strict=True)
            if b != a
        ]
        old = project_status(source, successors).value
        if expected.value == old:
            assert changed == []
        else:
            assert changed == [(f"  status: {old}", f"  status: {expected.value}")]
    else:
        assert result["status"] == "error"
        assert result["code"] == "transition_not_allowed"
        assert result["allowed_to"] == allowed_to(source)
        assert _snapshot(_pm(tmp_project)) == before


@pytest.mark.parametrize("lifecycle", LIFECYCLES)
def test_the_diagonal_re_projects_a_stale_status(tmp_project: Path, lifecycle: str):
    """Lineage new, status old (design §5.3): naming the same lifecycle repairs the status."""
    successors = ["ADR-002"] if lifecycle == "superseded" else []
    projected = project_status(lifecycle, successors)
    stale = next(status for status in DecisionStatus if status is not projected)
    adr = _adr("ADR-001", stale)
    _seed(tmp_project, adr, _adr("ADR-002"))
    _put_lineage(tmp_project, "ADR-001", _doc(adr, lifecycle, successors))

    result = _update(tmp_project, lifecycle=lifecycle)  # no reason needed on the diagonal

    assert result["status"] == "updated", result
    assert result["lifecycle"] == lifecycle
    assert result["decision_status"] == projected.value
    assert result["events_added"] == ["status_reprojected"]
    assert result["changes"] == {"decision_status": {"from": stale.value, "to": projected.value}}
    assert "decision_status_mismatch_resolved" in _codes(result)
    assert _status(tmp_project) == projected
    doc = _read_lineage(tmp_project)
    assert doc["lifecycle"] == lifecycle
    assert doc["events"][-1] == {
        "at": NOW,
        "kind": "status_reprojected",
        "from_status": stale.value,
        "to_status": projected.value,
        "via": "pm_update_decision",
    }


# ─── Invariants: only what the call changes (design §4.3) ───


class TestInvariants:
    def test_superseded_needs_a_successor(self, tmp_project: Path):
        _with_lineage(tmp_project, "proposed")
        result = _update(tmp_project, lifecycle="superseded", reason="replaced")
        assert result["code"] == "superseded_by_required"

    def test_superseded_by_is_only_kept_while_superseded_or_reverted(self, tmp_project: Path):
        _with_lineage(tmp_project, "adopted")
        result = _update(tmp_project, add_links={"superseded_by": ["ADR-002"]})
        assert result["code"] == "superseded_by_not_allowed"
        reverted = _update(
            tmp_project,
            lifecycle="reverted",
            reason="rolled back for ADR-002",
            add_links={"superseded_by": ["ADR-002"]},
        )
        assert reverted["status"] == "updated"
        assert reverted["decision_status"] == "superseded"

    def test_leaving_superseded_must_drop_the_successor_in_the_same_call(self, tmp_project: Path):
        _with_lineage(tmp_project, "superseded", superseded_by=["ADR-002"])
        kept = _update(tmp_project, lifecycle="adopted", reason="ADR-002 was withdrawn")
        assert kept["code"] == "superseded_by_not_allowed"
        dropped = _update(
            tmp_project,
            lifecycle="adopted",
            reason="ADR-002 was withdrawn",
            remove_links={"superseded_by": ["ADR-002"]},
        )
        assert dropped["status"] == "updated"
        assert dropped["links"]["superseded_by"] == []
        assert _status(tmp_project) is DecisionStatus.ACCEPTED

    def test_a_superseded_adr_without_successor_still_takes_notes_and_amends(
        self, tmp_project: Path
    ):
        # The real-data case: status superseded by hand, no lineage, no successor.
        _seed(tmp_project, _adr("ADR-001", DecisionStatus.SUPERSEDED), _adr("ADR-002"))
        decisions_before = (_pm(tmp_project) / "decisions.yaml").read_bytes()

        result = _update(tmp_project, note="kept for history", add_links={"amends": ["ADR-002"]})

        assert result["status"] == "updated", result
        assert result["lifecycle"] == "superseded"
        assert result["events_added"] == ["lineage_started", "link", "note"]
        assert {"decision_lineage_started", "decision_lineage_superseded_without_successor"} <= set(
            _codes(result)
        )
        info = _warning(result, "decision_lineage_superseded_without_successor")
        assert info["level"] == "info"
        assert (_pm(tmp_project) / "decisions.yaml").read_bytes() == decisions_before

        no_reason = _update(tmp_project, lifecycle="deprecated")
        assert no_reason["code"] == "reason_required"
        retired = _update(tmp_project, lifecycle="deprecated", reason="no successor was recorded")
        assert retired["decision_status"] == "deprecated"
        assert _status(tmp_project) is DecisionStatus.DEPRECATED

    def test_value_checks_apply_even_when_the_lifecycle_is_left_alone(self, tmp_project: Path):
        _seed(tmp_project, _adr("ADR-001", DecisionStatus.SUPERSEDED), _adr("ADR-002"))
        assert _update(tmp_project, note="x" * 4_001)["code"] == "text_too_long"
        assert _update(tmp_project, add_links={"amends": ["ADR-001"]})["code"] == "self_link"
        assert (
            _update(tmp_project, remove_links={"amends": ["ADR-002"]})["code"] == "reason_required"
        )


# ─── Projection and byte-level invariance (design §3.1, §9.1) ───


def _rich_decisions(project: Path) -> None:
    """decisions.yaml as _save_decisions writes it, with text a rewrite must not touch."""
    text = yaml.safe_dump(
        {
            "decisions": [
                {
                    "id": "ADR-001",
                    "title": "Use YAML — human readable",
                    "date": "2026-09-01",
                    "status": "accepted",
                    "context": "Line one.\nLine two: with a colon.\n",
                    "decision": "safe_load only",
                    "consequences": {
                        "positive": ["git-friendly"],
                        "negative": ["slower"],
                        "mitigations": ["cache"],
                        "custom": {"kept": True},
                    },
                    "custom_key": ["kept", 1],
                },
                {
                    "id": "ADR-002",
                    "title": "記録は日本語でもよい",
                    "date": "2026-09-02",
                    "status": "proposed",
                    "context": "c",
                    "decision": "d",
                },
                {
                    "id": "ADR-003",
                    "title": "Third",
                    "date": "2026-09-03",
                    "status": "accepted",
                    "context": "c",
                    "decision": "d",
                },
            ],
            "sibling_key": {"left": "alone"},
        },
        allow_unicode=True,
        sort_keys=False,
    )
    path = _pm(project) / "decisions.yaml"
    path.write_text(text, encoding="utf-8")
    storage._save_decisions(_pm(project), storage.load_decisions(_pm(project)))


class TestProjection:
    @pytest.mark.parametrize(
        ("adr_id", "calls", "old", "new"),
        [
            ("ADR-002", [{"lifecycle": "adopted"}], "proposed", "accepted"),
            ("ADR-002", [{"lifecycle": "rejected"}], "proposed", "deprecated"),
            ("ADR-001", [{"lifecycle": "deprecated"}], "accepted", "deprecated"),
            ("ADR-001", [{"lifecycle": "proposed"}], "accepted", "proposed"),
            ("ADR-001", [{"lifecycle": "reverted"}], "accepted", "deprecated"),
            (
                "ADR-001",
                [{"lifecycle": "reverted", "add_links": {"superseded_by": ["ADR-003"]}}],
                "accepted",
                "superseded",
            ),
            (
                "ADR-001",
                [{"lifecycle": "superseded", "add_links": {"superseded_by": ["ADR-003"]}}],
                "accepted",
                "superseded",
            ),
        ],
        ids=[
            "adopted->accepted",
            "rejected->deprecated",
            "deprecated",
            "back-to-proposed",
            "reverted-alone->deprecated",
            "reverted-with-successor->superseded",
            "superseded",
        ],
    )
    def test_only_the_status_line_of_the_adr_changes(
        self, tmp_project: Path, adr_id: str, calls: list[dict], old: str, new: str
    ):
        _rich_decisions(tmp_project)
        before = _decisions_lines(tmp_project)
        for call in calls:
            result = _update(tmp_project, adr_id, reason="the user decided", **call)
            assert result["status"] == "updated", result
        after = _decisions_lines(tmp_project)
        assert len(after) == len(before)
        diff = [(b, a) for b, a in zip(before, after, strict=True) if b != a]
        assert diff == [(f"  status: {old}", f"  status: {new}")]
        # The ADR line that changed is the target's: its id is the nearest one above.
        index = next(i for i, (b, a) in enumerate(zip(before, after, strict=True)) if b != a)
        owner = next(line for line in reversed(before[:index]) if line.startswith("- id: "))
        assert owner == f"- id: {adr_id}"

    def test_calls_that_leave_the_status_alone_leave_the_bytes_alone(self, tmp_project: Path):
        _rich_decisions(tmp_project)
        before = (_pm(tmp_project) / "decisions.yaml").read_bytes()
        for call in (
            {"note": "chose the faster parser"},
            {"evaluation": "tests pass", "evaluation_kind": "test"},
            {"add_links": {"amends": ["ADR-003"]}},
            {"origin": "ai_auto", "reason": "the session log says so"},
        ):
            result = _update(tmp_project, "ADR-002", **call)
            assert result["status"] == "updated", (call, result)
            assert "decision_status" not in result["changes"]
        assert (_pm(tmp_project) / "decisions.yaml").read_bytes() == before


# ─── lineage_started, mismatch, duplicates (design §3.3, §4.3) ───


class TestLineageStarted:
    def test_a_first_call_starts_the_lineage_from_the_status(self, tmp_project: Path):
        _seed(tmp_project, _adr("ADR-001", DecisionStatus.ACCEPTED))
        decisions_before = (_pm(tmp_project) / "decisions.yaml").read_bytes()

        result = _update(tmp_project, note="picked the default port")

        assert result["status"] == "updated"
        assert result["lifecycle"] == "adopted" and result["decision_status"] == "accepted"
        assert result["events_added"] == ["lineage_started", "note"]
        started = _warning(result, "decision_lineage_started")
        assert started["level"] == "info"
        doc = _read_lineage(tmp_project)
        assert doc["recorded_at"] is None
        assert doc["declared"] == {
            "origin": "unknown",
            "recorded_timing": "unknown",
            "decision_kind": "unknown",
        }
        assert doc["anchor"] == anchor_for(storage.load_decisions(_pm(tmp_project))[0])
        assert [event["kind"] for event in doc["events"]] == ["lineage_started", "note"]
        assert doc["events"][0] == {
            "at": NOW,
            "kind": "lineage_started",
            "basis": "derived_from_status",
            "status": "accepted",
            "lifecycle": "adopted",
            "via": "pm_update_decision",
        }
        assert (_pm(tmp_project) / "decisions.yaml").read_bytes() == decisions_before
        got = pm_decision_query(action="get", decision_id="ADR-001", project_path=str(tmp_project))
        assert got["lineage"]["derived"] is False
        assert got["lineage"]["not_recorded"] == [
            "recorded_at",
            "origin",
            "recorded_timing",
            "decision_kind",
        ]

    def test_an_unchanged_call_does_not_start_a_lineage(self, tmp_project: Path):
        _seed(tmp_project, _adr("ADR-001", DecisionStatus.ACCEPTED))
        before = _snapshot(_pm(tmp_project))
        result = _update(tmp_project, lifecycle="adopted")
        assert result["status"] == "unchanged"
        assert result["warnings"] == [] and result["events_added"] == []
        assert _snapshot(_pm(tmp_project)) == before
        assert not _lineage_path(tmp_project).exists()


class TestMismatch:
    def _hand_edited(self, project: Path, status: DecisionStatus) -> None:
        _add(project)  # proposed, lineage proposed
        decisions = storage.load_decisions(_pm(project))
        decisions[0].status = status
        storage._save_decisions(_pm(project), decisions)

    def test_a_note_leaves_a_hand_edited_status_and_says_so(self, tmp_project: Path):
        self._hand_edited(tmp_project, DecisionStatus.ACCEPTED)
        decisions_before = (_pm(tmp_project) / "decisions.yaml").read_bytes()

        result = _update(tmp_project, note="n")

        assert result["status"] == "updated"
        assert result["decision_status"] == "accepted" and result["lifecycle"] == "proposed"
        assert "decision_status" not in result["changes"]
        warning = _warning(result, "decision_status_mismatch")
        assert warning["level"] == "warning"
        assert "status=accepted" in warning["message"] and "proposed" in warning["message"]
        assert "Ask the user" in warning["remediation"]
        assert "lifecycle=proposed" in warning["remediation"]  # follow the lineage
        assert "adopted" in warning["remediation"]  # or move the lifecycle to match
        assert (_pm(tmp_project) / "decisions.yaml").read_bytes() == decisions_before

    def test_naming_a_lifecycle_rewrites_the_status(self, tmp_project: Path):
        self._hand_edited(tmp_project, DecisionStatus.DEPRECATED)
        same = _update(tmp_project, lifecycle="proposed")
        assert same["events_added"] == ["status_reprojected"]
        assert _warning(same, "decision_status_mismatch_resolved")["level"] == "warning"
        assert _status(tmp_project) is DecisionStatus.PROPOSED

        self._reset(tmp_project, DecisionStatus.DEPRECATED)
        moved = _update(tmp_project, lifecycle="adopted", reason="the user accepted it")
        assert moved["changes"] == {
            "lifecycle": {"from": "proposed", "to": "adopted"},
            "decision_status": {"from": "deprecated", "to": "accepted"},
        }
        assert "decision_status_mismatch_resolved" in _codes(moved)
        assert _status(tmp_project) is DecisionStatus.ACCEPTED
        listed = pm_decision_query(project_path=str(tmp_project))
        assert "decision_status_mismatch" not in _codes(listed)

    def _reset(self, project: Path, status: DecisionStatus) -> None:
        decisions = storage.load_decisions(_pm(project))
        decisions[0].status = status
        storage._save_decisions(_pm(project), decisions)


# ─── Failures: projection fault, explicit status check ───


class TestFailures:
    def test_a_failed_projection_is_reported_and_repaired_by_the_same_lifecycle(
        self, tmp_project: Path, monkeypatch: pytest.MonkeyPatch
    ):
        _add(tmp_project)
        decisions_before = (_pm(tmp_project) / "decisions.yaml").read_bytes()
        real = storage._save_decisions

        def failing(*_args) -> None:
            raise OSError(28, "No space left on device")

        monkeypatch.setattr(storage, "_save_decisions", failing)
        result = _update(tmp_project, lifecycle="adopted", reason="the user approved")

        assert result["status"] == "updated"
        assert result["lifecycle"] == "adopted"
        assert result["decision_status"] == "proposed"  # what decisions.yaml still says
        assert "decision_status" not in result["changes"]
        warning = _warning(result, "decision_status_not_projected")
        assert warning["level"] == "warning"
        assert "lifecycle" in warning["remediation"]
        assert (_pm(tmp_project) / "decisions.yaml").read_bytes() == decisions_before
        assert _read_lineage(tmp_project)["lifecycle"] == "adopted"
        got = pm_decision_query(action="get", decision_id="ADR-001", project_path=str(tmp_project))
        assert "decision_status_mismatch" in _codes(got)

        monkeypatch.setattr(storage, "_save_decisions", real)
        rerun = _update(tmp_project, lifecycle="adopted")
        assert rerun["events_added"] == ["status_reprojected"]
        assert "decision_status_mismatch_resolved" in _codes(rerun)
        assert _status(tmp_project) is DecisionStatus.ACCEPTED

    @pytest.mark.parametrize(
        ("lineage_lifecycle", "named", "events", "changes"),
        [
            # The diagonal: the call only re-projects the stale status.
            ("adopted", "adopted", ["status_reprojected"], {}),
            # A move: the lifecycle is saved, the status is not.
            (
                "proposed",
                "adopted",
                ["lifecycle"],
                {"lifecycle": {"from": "proposed", "to": "adopted"}},
            ),
        ],
    )
    def test_a_failed_projection_does_not_claim_a_prior_mismatch_was_resolved(
        self,
        tmp_project: Path,
        monkeypatch: pytest.MonkeyPatch,
        lineage_lifecycle: str,
        named: str,
        events: list[str],
        changes: dict,
    ):
        # A mismatch from before the call, a named lifecycle (which resolves
        # it, design §3.3 rule 4) and a decisions.yaml that cannot be written:
        # decision_status_mismatch_resolved means "rewritten to match the
        # lineage" (§4.3), so it must not sit next to decision_status_not_projected.
        _with_lineage(tmp_project, lineage_lifecycle)
        decisions = storage.load_decisions(_pm(tmp_project))
        decisions[0].status = DecisionStatus.DEPRECATED
        storage._save_decisions(_pm(tmp_project), decisions)
        decisions_before = (_pm(tmp_project) / "decisions.yaml").read_bytes()

        def failing(*_args) -> None:
            raise OSError(28, "No space left on device")

        monkeypatch.setattr(storage, "_save_decisions", failing)
        result = _update(tmp_project, lifecycle=named, reason="the user decided")

        assert result["status"] == "updated"
        assert result["events_added"] == events
        assert result["changes"] == changes
        assert result["decision_status"] == "deprecated"  # what decisions.yaml still says
        codes = _codes(result)
        assert "decision_status_mismatch_resolved" not in codes
        assert codes.count("decision_status_not_projected") == 1
        assert (_pm(tmp_project) / "decisions.yaml").read_bytes() == decisions_before

    def test_a_broken_projection_is_refused_before_anything_is_written(
        self, tmp_project: Path, monkeypatch: pytest.MonkeyPatch
    ):
        _add(tmp_project)
        before = _snapshot(_pm(tmp_project))
        monkeypatch.setattr(lineage, "project_status", lambda *_args: "adopted")
        with pytest.raises(PmServerError, match="not one of"):
            _update(tmp_project, lifecycle="adopted", reason="r")
        assert _snapshot(_pm(tmp_project)) == before

    def test_the_new_status_is_checked_explicitly_before_the_lineage_is_written(
        self, tmp_project: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # _save_decisions does not check the status, so change_decision_lineage
        # must call _require_known_status itself, on the ADR carrying the new
        # status, before either file is written.
        _add(tmp_project)
        lineage_before = _lineage_path(tmp_project).read_bytes()
        decisions_before = (_pm(tmp_project) / "decisions.yaml").read_bytes()
        seen: list[tuple[object, bool, bool]] = []
        real = storage._require_known_status

        def spy(decision: Decision) -> None:
            seen.append(
                (
                    decision.status,
                    _lineage_path(tmp_project).read_bytes() == lineage_before,
                    (_pm(tmp_project) / "decisions.yaml").read_bytes() == decisions_before,
                )
            )
            real(decision)

        monkeypatch.setattr(storage, "_require_known_status", spy)
        result = _update(tmp_project, lifecycle="rejected", reason="the user turned it down")
        assert result["status"] == "updated"
        assert seen == [(DecisionStatus.DEPRECATED, True, True)]

        # A call that leaves the status alone has nothing to check.
        seen.clear()
        _update(tmp_project, note="n")
        assert seen == []

        def refusing(decision: Decision) -> None:
            raise PmServerError("refused")

        monkeypatch.setattr(storage, "_require_known_status", refusing)
        before = _snapshot(_pm(tmp_project))
        with pytest.raises(PmServerError, match="refused"):
            _update(tmp_project, lifecycle="proposed", reason="reconsider")
        assert _snapshot(_pm(tmp_project)) == before


# ─── Warnings (design §4.3) ──────────────────────────


class TestWarnings:
    @pytest.mark.parametrize(
        ("source", "target", "told"),
        [
            ("proposed", "adopted", True),
            ("proposed", "rejected", True),
            ("deprecated", "adopted", True),
            ("superseded", "adopted", True),
            ("adopted", "proposed", False),
            ("adopted", "deprecated", False),
            ("adopted", "reverted", False),
            ("rejected", "proposed", False),
        ],
    )
    def test_adopting_or_rejecting_is_always_reported(
        self, tmp_project: Path, source: str, target: str, told: bool
    ):
        successors = ["ADR-002"] if source == "superseded" else []
        _with_lineage(tmp_project, source, superseded_by=successors)
        kwargs: dict = {"lifecycle": target, "reason": "the user said so"}
        if successors:
            kwargs["remove_links"] = {"superseded_by": successors}
        result = _update(tmp_project, **kwargs)
        assert result["status"] == "updated", result
        if told:
            warning = _warning(result, "decision_lifecycle_changed")
            assert warning["level"] == "info"
            assert "cannot confirm that the user made this decision" in warning["message"]
            assert "tell the user" in warning["message"]
        else:
            assert "decision_lifecycle_changed" not in _codes(result)

    def test_supersedes_points_at_the_other_side_without_writing_it(self, tmp_project: Path):
        _add(tmp_project, "Old approach")
        _add(tmp_project, "New approach")
        old_lineage = _lineage_path(tmp_project, "ADR-001").read_bytes()

        result = _update(tmp_project, "ADR-002", add_links={"supersedes": ["ADR-001"]})

        assert result["links"]["supersedes"] == ["ADR-001"]
        info = _warning(result, "decision_lineage_link_asymmetric")
        assert info["level"] == "info"
        assert "ADR-001's lifecycle and superseded_by were not changed" in result["next"]
        assert "pm_update_decision" in result["next"]
        assert _lineage_path(tmp_project, "ADR-001").read_bytes() == old_lineage
        assert _status(tmp_project, "ADR-001") is DecisionStatus.PROPOSED

        other = _update(
            tmp_project,
            "ADR-001",
            lifecycle="superseded",
            reason="replaced by ADR-002",
            add_links={"superseded_by": ["ADR-002"]},
        )
        assert "decision_lineage_link_asymmetric" not in _codes(other)
        assert "next" not in other
        listed = pm_decision_query(project_path=str(tmp_project))
        assert "decision_lineage_link_asymmetric" not in _codes(listed)

    def test_a_lineage_without_anchor_is_anchored_again(self, tmp_project: Path):
        adr = _adr("ADR-001", DecisionStatus.PROPOSED)
        _seed(tmp_project, adr)
        doc = _doc(adr, "proposed")
        del doc["anchor"]
        _put_lineage(tmp_project, "ADR-001", doc)

        result = _update(tmp_project, note="n")

        assert _warning(result, "decision_lineage_anchor_missing")["level"] == "info"
        assert _read_lineage(tmp_project)["anchor"] == anchor_for(adr)

    def test_unchanged_calls_write_nothing(self, tmp_project: Path):
        _add(tmp_project)
        before = _snapshot(_pm(tmp_project))
        for call in (
            {},
            {"lifecycle": "proposed"},
            {"reason": "only a reason"},
            {"add_links": {"amends": []}},
            {"remove_links": {"amends": []}},
            {"note": "   "},
        ):
            result = _update(tmp_project, **call)
            assert result["status"] == "unchanged", (call, result)
            assert result["changes"] == {} and result["events_added"] == []
            assert result["lifecycle"] == "proposed"
            assert result["decision_status"] == "proposed"
        assert _snapshot(_pm(tmp_project)) == before

    def test_the_response_shape(self, tmp_project: Path):
        _add(tmp_project)
        result = _update(tmp_project, lifecycle="adopted", reason="approved at the check gate")
        assert set(result) == {
            "status",
            "decision_id",
            "lifecycle",
            "decision_status",
            "changes",
            "links",
            "events_added",
            "warnings",
        }
        assert result["decision_id"] == "ADR-001"
        assert result["links"] == {"supersedes": [], "superseded_by": [], "amends": []}
        json.dumps(result)  # plain JSON all the way down


# ─── Backfill of declared values (ADR-059 Q2) ───────


class TestBackfill:
    def test_unknown_values_are_filled_once_with_a_reason(self, tmp_project: Path):
        _add(tmp_project)  # origin and recorded_timing unknown
        result = _update(
            tmp_project,
            origin="ai_auto",
            recorded_timing="before_impl",
            reason="the workflow log shows it was recorded unreviewed before implementation",
        )
        assert result["status"] == "updated"
        assert result["changes"] == {
            "declared": {
                "origin": {"from": "unknown", "to": "ai_auto"},
                "recorded_timing": {"from": "unknown", "to": "before_impl"},
            }
        }
        assert result["events_added"] == ["declared", "declared"]
        doc = _read_lineage(tmp_project)
        assert doc["declared"]["origin"] == "ai_auto"
        assert doc["declared"]["recorded_timing"] == "before_impl"
        assert doc["declared"]["decision_kind"] == "unknown"
        assert [e["basis"] for e in doc["events"] if e["kind"] == "declared"] == [
            "backfill",
            "backfill",
        ]
        got = pm_decision_query(action="get", decision_id="ADR-001", project_path=str(tmp_project))
        assert got["lineage"]["declared_later"] == ["origin", "recorded_timing"]
        assert got["lineage"]["not_recorded"] == ["decision_kind"]

        again = _update(tmp_project, origin="ai_auto", reason="again")
        assert again["code"] == "declared_already_set"

    def test_a_declared_value_is_never_replaced(self, tmp_project: Path):
        _add(tmp_project, origin="human", recorded_timing="post_hoc")
        before = _snapshot(_pm(tmp_project))
        assert _update(tmp_project, origin="ai_auto", reason="r")["code"] == "declared_already_set"
        timing = _update(tmp_project, recorded_timing="before_impl", reason="r")
        assert timing["code"] == "declared_already_set"
        assert _snapshot(_pm(tmp_project)) == before

    def test_a_reason_is_required(self, tmp_project: Path):
        _add(tmp_project)
        assert _update(tmp_project, origin="ai_auto")["code"] == "reason_required"
        assert _update(tmp_project, recorded_timing="post_hoc")["code"] == "reason_required"


# ─── D8: no argument that claims more than pmlens can know ───


def _tool_parameters() -> dict[str, list[str]]:
    """Each ``@_tool()`` function of server.py and its parameter names (from source)."""
    tree = ast.parse(Path(server.__file__).read_text(encoding="utf-8"))
    tools: dict[str, list[str]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if any(
            isinstance(deco, ast.Call)
            and isinstance(deco.func, ast.Name)
            and deco.func.id == "_tool"
            for deco in node.decorator_list
        ):
            tools[node.name] = [arg.arg for arg in (*node.args.args, *node.args.kwonlyargs)]
    return tools


_TOOL_PARAMETERS = _tool_parameters()
_FORBIDDEN_NAME = re.compile(r"accura|verif|confirm|human|reader_hint|anchor|^mode$")

# Design §4.3: the whole argument list of pm_update_decision. Adding one is a
# deliberate change to this set (and to the design).
_UPDATE_PARAMETERS = {
    "decision_id",
    "lifecycle",
    "reason",
    "add_links",
    "remove_links",
    "evaluation",
    "evaluation_kind",
    "note",
    "origin",
    "recorded_timing",
    "project_path",
}


class TestD8:
    def test_the_tool_list_is_read(self):
        assert len(_TOOL_PARAMETERS) >= 40
        assert "pm_update_decision" in _TOOL_PARAMETERS

    def test_a_no_argument_sets_accuracy_verification_or_human_confirmation(self):
        hits = [
            (tool, name)
            for tool, names in _TOOL_PARAMETERS.items()
            for name in names
            if _FORBIDDEN_NAME.search(name)
        ]
        assert hits == []
        # "kind" alone is the content pipeline's draft kind, nothing else.
        assert sorted(t for t, names in _TOOL_PARAMETERS.items() if "kind" in names) == [
            "pm_draft_content",
            "pm_draft_x",
        ]

    def test_a_the_name_check_would_catch_them(self):
        for name in ("accuracy", "verified", "user_confirmed", "human_reviewed", "anchor", "mode"):
            assert _FORBIDDEN_NAME.search(name), name
        assert not _FORBIDDEN_NAME.search("model")

    def test_b_pm_update_decision_takes_exactly_the_designed_arguments(self):
        assert set(_TOOL_PARAMETERS["pm_update_decision"]) == _UPDATE_PARAMETERS
        signature = inspect.signature(pm_update_decision)
        required = [
            name
            for name, parameter in signature.parameters.items()
            if parameter.default is inspect.Parameter.empty
        ]
        assert required == ["decision_id"]
        assert signature.parameters["evaluation_kind"].default == "other"
        for absent in (
            "title",
            "context",
            "decision",
            "consequences_positive",
            "decision_kind",
            "caused_by",
            "dry_run",
            "status",
        ):
            assert absent not in _UPDATE_PARAMETERS

    def test_c_decision_kind_is_only_declared_when_recording(self):
        assert [t for t, names in _TOOL_PARAMETERS.items() if "decision_kind" in names] == [
            "pm_add_decision"
        ]

    @pytest.mark.parametrize("origin", ["human", "ai_proposed_human_decided"])
    @pytest.mark.parametrize("has_lineage", [True, False], ids=["lineage", "no-lineage"])
    def test_d_a_person_cannot_be_declared_after_the_fact(
        self, tmp_project: Path, origin: str, has_lineage: bool
    ):
        if has_lineage:
            _add(tmp_project)
        else:
            _seed(tmp_project, _adr("ADR-001", DecisionStatus.ACCEPTED))
        before = _snapshot(_pm(tmp_project))
        result = _update(tmp_project, origin=origin, reason="the user said they decided it")
        assert result["status"] == "error"
        assert result["code"] == "declared_backfill_value_not_allowed"
        assert _snapshot(_pm(tmp_project)) == before


# ─── D11: secrets never stored or shown ──────────────


class TestD11:
    def test_secrets_in_reason_note_and_evaluation_are_removed_before_saving(
        self, tmp_project: Path
    ):
        _add(tmp_project)
        result = _update(
            tmp_project,
            lifecycle="adopted",
            reason=f"approved; token {SECRET}",
            note=f"note {SECRET}",
            evaluation=f"review {SECRET}",
            evaluation_kind="ai_review",
        )
        assert result["status"] == "updated"
        written = _lineage_path(tmp_project).read_text(encoding="utf-8")
        assert SECRET not in written and "AKIA" not in written
        assert "<REDACTED" in written
        dumped = json.dumps(result)
        assert SECRET not in dumped and "AKIA" not in dumped
        warning = _warning(result, "decision_lineage_secrets_redacted")
        assert warning["level"] == "warning"
        assert warning["message"].startswith("3 ")
        got = pm_decision_query(action="get", decision_id="ADR-001", project_path=str(tmp_project))
        assert SECRET not in json.dumps(got)

    def test_a_private_key_in_a_note_is_removed_with_its_body(self, tmp_project: Path):
        # Catalog v2 removed only the BEGIN line, so the key's base64 body was
        # saved in the lineage file and returned by get under a warning that
        # said the secret had been removed.
        body = "\n".join(["Zm9v" * 16] * 5)
        key = f"-----BEGIN OPENSSH PRIVATE KEY-----\n{body}\n-----END OPENSSH PRIVATE KEY-----"
        _add(tmp_project)
        result = _update(tmp_project, note=f"deploy key:\n{key}\nrotate it")
        assert result["status"] == "updated"
        assert _warning(result, "decision_lineage_secrets_redacted")["message"].startswith("1 ")
        written = _lineage_path(tmp_project).read_text(encoding="utf-8")
        doc = yaml.safe_load(written)
        assert doc["events"][-1]["text"] == "deploy key:\n<REDACTED:secret>\nrotate it"
        got = pm_decision_query(action="get", decision_id="ADR-001", project_path=str(tmp_project))
        for shown in (written, json.dumps(result), json.dumps(got)):
            assert "Zm9v" not in shown and "PRIVATE KEY" not in shown

    def test_a_secret_in_a_link_removal_reason_is_removed(self, tmp_project: Path):
        _add(tmp_project, "A")
        _add(tmp_project, "B")
        _update(tmp_project, "ADR-002", add_links={"amends": ["ADR-001"]})
        result = _update(
            tmp_project, "ADR-002", remove_links={"amends": ["ADR-001"]}, reason=f"x {SECRET}"
        )
        assert "decision_lineage_secrets_redacted" in _codes(result)
        assert SECRET not in _lineage_path(tmp_project, "ADR-002").read_text(encoding="utf-8")

    @pytest.mark.parametrize(
        ("text", "detail"),
        [
            (
                f'decisions:\n- id: ADR-001\n  title: "unterminated {SECRET}\n  status: x\n',
                "at line",
            ),
            (f"decisions:\n- id: ADR-001\n  title: [{SECRET}, 2]\n", "ValidationError"),
        ],
        ids=["yaml-syntax", "validation"],
    )
    def test_a_broken_decisions_yaml_is_reported_without_its_text(
        self, tmp_project: Path, text: str, detail: str
    ):
        (_pm(tmp_project) / "decisions.yaml").write_text(text, encoding="utf-8")
        before = _snapshot(_pm(tmp_project))
        result = _update(tmp_project, note="n")
        assert result["status"] == "error"
        assert result["code"] == "decisions_yaml_unreadable"
        assert detail in result["message"]
        dumped = json.dumps(result)
        assert SECRET not in dumped and "unterminated" not in dumped
        assert _snapshot(_pm(tmp_project)) == before

    def test_a_secret_in_the_id_is_not_echoed(self, tmp_project: Path):
        result = _update(tmp_project, f"ADR-{SECRET}", note="n")
        assert result["code"] == "invalid_decision_id"
        assert SECRET not in json.dumps(result)


# ─── Every error code: the error dict, and nothing written ───


def _good(project: Path) -> None:
    _add(project, "First")
    _add(project, "Second")


def _proposed_without_lineage(project: Path) -> None:
    _seed(project, _adr("ADR-001", DecisionStatus.PROPOSED), _adr("ADR-002"))


def _duplicate(project: Path) -> None:
    (_pm(project) / "decisions.yaml").write_text(
        "decisions:\n- id: ADR-001\n  title: a\n- id: ADR-001\n  title: b\n", encoding="utf-8"
    )


def _unknown_status(project: Path) -> None:
    (_pm(project) / "decisions.yaml").write_text(
        "decisions:\n- id: ADR-001\n  title: t\n  status: archived\n", encoding="utf-8"
    )


def _many(project: Path) -> None:
    _seed(project, *(_adr(f"ADR-{n:03d}") for n in range(1, 53)))


def _declared(project: Path) -> None:
    _add(project, origin="ai_auto")


def _lineage_variant(**overrides: object) -> Callable[[Path], None]:
    def setup(project: Path) -> None:
        adr = _adr("ADR-001", DecisionStatus.PROPOSED)
        _seed(project, adr, _adr("ADR-002"))
        doc = _doc(adr, "proposed")
        doc.update(overrides)
        _put_lineage(project, "ADR-001", doc)

    return setup


def _broken_lineage(project: Path) -> None:
    _seed(project, _adr("ADR-001", DecisionStatus.PROPOSED))
    _put_lineage(project, "ADR-001", 'lifecycle: "open\n')


def _nearly_full_lineage(project: Path) -> None:
    adr = _adr("ADR-001", DecisionStatus.PROPOSED)
    _seed(project, adr)
    doc = _doc(adr, "proposed")
    doc["pad"] = ""
    room = MAX_LINEAGE_BYTES - len(dump_lineage(doc, adr.id).encode()) - 1_000
    doc["pad"] = "p" * room
    _put_lineage(project, "ADR-001", doc)


def _broken_decisions(project: Path) -> None:
    (_pm(project) / "decisions.yaml").write_text("decisions: [\n", encoding="utf-8")


_ERROR_CASES: list[tuple[Callable[[Path], None], dict, str]] = [
    (_good, {"decision_id": "adr-001", "note": "n"}, "invalid_decision_id"),
    (_good, {"decision_id": "ADR-001\n", "note": "n"}, "invalid_decision_id"),
    (_good, {"decision_id": "../ADR-001", "note": "n"}, "invalid_decision_id"),
    (_good, {"decision_id": "ADR-404", "note": "n"}, "decision_not_found"),
    (_duplicate, {"note": "n"}, "decision_id_duplicate"),
    (_unknown_status, {"note": "n"}, "decision_status_unknown"),
    (_good, {"lifecycle": "accepted", "reason": "r"}, "invalid_lifecycle"),
    (_good, {"lifecycle": "reverted", "reason": "r"}, "transition_not_allowed"),
    (_good, {"add_links": {"related": ["ADR-002"]}}, "invalid_link_type"),
    (_good, {"add_links": {"amends": ["ADR-1234567"]}}, "invalid_link_target"),
    (_good, {"add_links": {"amends": "ADR-002"}}, "invalid_link_target"),
    (_good, {"add_links": {"amends": ["ADR-404"]}}, "link_target_not_found"),
    (_good, {"add_links": {"amends": ["ADR-001"]}}, "self_link"),
    (
        _many,
        {"add_links": {"amends": [f"ADR-{n:03d}" for n in range(2, 53)]}},
        "too_many_links",
    ),
    (_good, {"lifecycle": "superseded", "reason": "r"}, "superseded_by_required"),
    (_good, {"add_links": {"superseded_by": ["ADR-002"]}}, "superseded_by_not_allowed"),
    (_good, {"lifecycle": "adopted"}, "reason_required"),
    (_good, {"lifecycle": "adopted", "reason": "   "}, "reason_required"),
    (_good, {"remove_links": {"amends": ["ADR-002"]}}, "reason_required"),
    (_good, {"origin": "ai_auto"}, "reason_required"),
    (_good, {"note": "x" * 4_001}, "text_too_long"),
    (_good, {"evaluation": "x" * 4_001}, "text_too_long"),
    (_good, {"lifecycle": "adopted", "reason": "x" * 4_001}, "text_too_long"),
    (_declared, {"origin": "ai_auto", "reason": "r"}, "declared_already_set"),
    (_good, {"origin": "human", "reason": "r"}, "declared_backfill_value_not_allowed"),
    (_good, {"recorded_timing": "unknown", "reason": "r"}, "declared_backfill_value_not_allowed"),
    (_good, {"origin": "robot", "reason": "r"}, "invalid_origin"),
    (_good, {"recorded_timing": "soon", "reason": "r"}, "invalid_recorded_timing"),
    (_good, {"evaluation": "e", "evaluation_kind": "human_review"}, "invalid_evaluation_kind"),
    (_broken_lineage, {"note": "n"}, "decision_lineage_unreadable"),
    (_lineage_variant(decision_id="ADR-002"), {"note": "n"}, "decision_lineage_unreadable"),
    (_lineage_variant(schema=2), {"note": "n"}, "decision_lineage_schema_unsupported"),
    (_lineage_variant(lifecycle="archived"), {"note": "n"}, "decision_lineage_lifecycle_unknown"),
    (
        _lineage_variant(anchor={"date": "1999-01-01", "title_sha256": "0" * 64}),
        {"note": "n"},
        "decision_lineage_anchor_mismatch",
    ),
    (_nearly_full_lineage, {"note": "n" * 4_000}, "decision_lineage_too_large"),
    (_broken_decisions, {"note": "n"}, "decisions_yaml_unreadable"),
    (_proposed_without_lineage, {"lifecycle": "reverted", "reason": "r"}, "transition_not_allowed"),
]

# Design §4.4: every error code pm_update_decision can return (plus
# decisions_yaml_unreadable, which the design lists for the reader only).
_UPDATE_ERROR_CODES = {
    "invalid_decision_id",
    "decision_not_found",
    "decision_id_duplicate",
    "decision_status_unknown",
    "invalid_lifecycle",
    "transition_not_allowed",
    "invalid_link_type",
    "invalid_link_target",
    "link_target_not_found",
    "self_link",
    "too_many_links",
    "superseded_by_required",
    "superseded_by_not_allowed",
    "reason_required",
    "text_too_long",
    "declared_already_set",
    "declared_backfill_value_not_allowed",
    "invalid_origin",
    "invalid_recorded_timing",
    "invalid_evaluation_kind",
    "decision_lineage_unreadable",
    "decision_lineage_schema_unsupported",
    "decision_lineage_lifecycle_unknown",
    "decision_lineage_anchor_mismatch",
    "decision_lineage_too_large",
    "decisions_yaml_unreadable",
}


def test_every_error_code_is_exercised():
    assert {code for _setup, _kwargs, code in _ERROR_CASES} == _UPDATE_ERROR_CODES


@pytest.mark.parametrize(("setup", "kwargs", "code"), _ERROR_CASES)
def test_every_error_code_returns_an_error_dict_and_writes_nothing(
    tmp_project: Path, setup: Callable[[Path], None], kwargs: dict, code: str
):
    setup(tmp_project)
    before = _snapshot(_pm(tmp_project))
    kwargs = dict(kwargs)  # the parametrize values are shared between runs
    decision_id = kwargs.pop("decision_id", "ADR-001")

    result = _update(tmp_project, decision_id, **kwargs)

    assert result["status"] == "error", result
    assert result["code"] == code
    assert isinstance(result["message"], str) and result["message"]
    if code == "transition_not_allowed":
        assert result["allowed_to"] == ["adopted", "superseded", "rejected"]
    assert _snapshot(_pm(tmp_project)) == before


@pytest.mark.parametrize(
    ("kwargs", "code"),
    [
        ({"decision_id": "nope"}, "invalid_decision_id"),
        ({"decision_id": "ADR-001", "lifecycle": "approved"}, "invalid_lifecycle"),
        (
            {"decision_id": "ADR-001", "origin": "human", "reason": "r"},
            "declared_backfill_value_not_allowed",
        ),
    ],
)
def test_argument_errors_come_before_the_project_is_looked_up(
    tmp_path: Path, kwargs: dict, code: str
):
    missing = tmp_path / "no-project-here"
    result = pm_update_decision(project_path=str(missing), **kwargs)
    assert result["code"] == code
    assert not missing.exists()


def test_consequences_survive_a_lifecycle_change(tmp_project: Path):
    adr = _adr(
        "ADR-001",
        DecisionStatus.PROPOSED,
        consequences=Consequences(positive=["p"], negative=["n"], mitigations=["m"]),
    )
    _seed(tmp_project, adr)
    _update(tmp_project, lifecycle="adopted", reason="approved")
    [after] = storage.load_decisions(_pm(tmp_project))
    assert after.consequences == adr.consequences
    assert (after.title, after.context, after.decision) == (adr.title, adr.context, adr.decision)
