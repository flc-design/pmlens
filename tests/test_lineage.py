"""Decision Lineage storage layer (ADR-056 S1, PMSERV-221).

What this file pins, by the design document's numbering
(docs/issues/DESIGN_decision-lineage-s1.md):

* D1 (second half): the lifecycle vocabulary, the projection onto the four
  DecisionStatus values (total), the reverse mapping and the transition table.
* The lenient read view (§2.7), one test per row of its table, without I/O.
* The bounded reader: FIFO, symlink, directory, oversize, bad UTF-8 / YAML.
* Writes: create (§5.1) and change (§5.2), their refusals (§2.6), unknown-key
  preservation, the anchor, numbering past orphaned lineages, the lock file
  location and the run-time lock order check.
* D3: an older pmlens rewriting decisions.yaml keeps the lineage and the
  projected status. D7 (S1 part): concurrent writers lose nothing.
* The module boundary: lineage.py reads but never writes, and does not import
  storage (an AST check, since the static Lens reachability test only knows
  the four ledger-write helpers).
"""

from __future__ import annotations

import ast
import dataclasses
import datetime as dt
import json
import multiprocessing as mp
import os
import threading
import time
from pathlib import Path

import pytest
import yaml
from pydantic import BaseModel

import pmlens
from pmlens import lineage, storage
from pmlens.lineage import (
    LIFECYCLES,
    MAX_LABEL_CHARS,
    MAX_LINEAGE_BYTES,
    LineageChange,
    LineageWriteRefused,
    RawLineage,
    allowed_to,
    anchor_for,
    apply_change,
    derive_lifecycle,
    dump_lineage,
    effective_lifecycle,
    error_summary,
    lineage_view,
    project_status,
    read_lineage_raw,
    scrub_label,
    scrub_view,
)
from pmlens.models import (
    Decision,
    DecisionKind,
    DecisionLifecycle,
    DecisionOrigin,
    DecisionStatus,
    EvaluationKind,
    PmServerError,
    RecordedTiming,
)
from pmlens.storage import (
    _yaml_transaction,
    add_decision_with_lineage,
    add_decision_with_next_id,
    change_decision_lineage,
    load_decisions,
    next_decision_number,
)

NOW = "2026-10-05T03:12:00Z"
SECRET = "AKIA" + "Z" * 16
ADR_DATE = dt.date(2026, 10, 1)


@pytest.fixture(autouse=True)
def _fixed_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(lineage, "_utc_now", lambda: NOW)


def _adr(
    adr_id: str = "ADR-001",
    *,
    title: str | None = None,
    status: DecisionStatus | str = DecisionStatus.ACCEPTED,
) -> Decision:
    return Decision(
        id=adr_id,
        title=title or f"title {adr_id}",
        date=ADR_DATE,
        status=status,
        context="c",
        decision="d",
    )


def _doc(adr: Decision, **overrides: object) -> dict:
    """A well-formed lineage document for ``adr`` (as S1 writes it)."""
    lifecycle = derive_lifecycle(adr.status) or "adopted"
    doc = {
        "schema": 1,
        "decision_id": adr.id,
        "anchor": anchor_for(adr),
        "recorded_at": NOW,
        "declared": {
            "origin": "ai_auto",
            "recorded_timing": "before_impl",
            "decision_kind": "technical",
        },
        "lifecycle": lifecycle,
        "links": {"supersedes": [], "superseded_by": [], "amends": []},
        "events": [
            {
                "at": NOW,
                "kind": "created",
                "lifecycle": lifecycle,
                "status": str(adr.status),
                "via": "pm_add_decision",
            }
        ],
    }
    doc.update(overrides)
    return doc


def _raw(adr: Decision, doc: object) -> RawLineage:
    return RawLineage(adr.id, exists=True, data=doc)


def _path(pm_path: Path, adr_id: str = "ADR-001") -> Path:
    return pm_path / "decision_lineage" / f"{adr_id}.yaml"


def _put(pm_path: Path, adr_id: str, content: dict | str) -> Path:
    path = _path(pm_path, adr_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = content if isinstance(content, str) else dump_lineage(content, adr_id)
    path.write_text(text, encoding="utf-8")
    return path


def _seed(pm_path: Path, *adrs: Decision) -> None:
    storage._save_decisions(pm_path, list(adrs))


def _build(status: DecisionStatus = DecisionStatus.PROPOSED, title: str = "t"):
    def build(number: int) -> Decision:
        return Decision(
            id=f"ADR-{number:03d}",
            title=f"{title}{number}",
            date=ADR_DATE,
            status=status,
        )

    return build


def _snapshot(root: Path) -> dict[str, object]:
    out: dict[str, object] = {}
    for path in sorted(root.rglob("*")):
        key = str(path.relative_to(root))
        if path.is_symlink():
            out[key] = ("link", os.readlink(path))
        elif path.is_dir():
            out[key] = "dir"
        else:
            out[key] = path.read_bytes()
    return out


def _codes(view_or_notes) -> set[str]:
    notes = view_or_notes.notes if hasattr(view_or_notes, "notes") else view_or_notes
    return {entry["code"] for entry in notes}


def _note_count(view, code: str) -> int:
    return sum(entry["count"] for entry in view.notes if entry["code"] == code)


# ─── D1: vocabularies, projection, transitions ───────────────────────────


class TestVocabulary:
    def test_lifecycle_vocabulary_is_fixed(self):
        assert [v.value for v in DecisionLifecycle] == [
            "proposed",
            "adopted",
            "deprecated",
            "superseded",
            "rejected",
            "reverted",
        ]
        assert list(LIFECYCLES) == [v.value for v in DecisionLifecycle]

    def test_declared_and_evaluation_vocabularies(self):
        assert [v.value for v in DecisionOrigin] == [
            "ai_auto",
            "ai_proposed_human_decided",
            "human",
            "unknown",
        ]
        assert [v.value for v in RecordedTiming] == [
            "before_impl",
            "during_impl",
            "post_hoc",
            "unknown",
        ]
        assert [v.value for v in DecisionKind] == [
            "spec_policy",
            "premise_dependent",
            "technical",
            "unknown",
        ]
        assert [v.value for v in EvaluationKind] == ["test", "ai_review", "outcome", "other"]

    def test_projection_table(self):
        none: list[str] = []
        succ = ["ADR-002"]
        assert project_status("proposed", none) is DecisionStatus.PROPOSED
        assert project_status("adopted", none) is DecisionStatus.ACCEPTED
        assert project_status("deprecated", none) is DecisionStatus.DEPRECATED
        assert project_status("superseded", succ) is DecisionStatus.SUPERSEDED
        assert project_status("rejected", none) is DecisionStatus.DEPRECATED
        assert project_status("reverted", succ) is DecisionStatus.SUPERSEDED
        assert project_status("reverted", none) is DecisionStatus.DEPRECATED

    @pytest.mark.parametrize(
        "lifecycle", [*LIFECYCLES, "archived", "", None, 5, ["adopted"], {"a": 1}]
    )
    @pytest.mark.parametrize("successors", [[], ["ADR-002"], None, "x"])
    def test_projection_is_total_and_stays_in_the_four_values(self, lifecycle, successors):
        assert project_status(lifecycle, successors) in set(DecisionStatus)

    def test_reverse_mapping_covers_every_status_and_round_trips(self):
        for status in DecisionStatus:
            lifecycle = derive_lifecycle(status)
            assert lifecycle in LIFECYCLES
            assert project_status(lifecycle, []) is status
            assert derive_lifecycle(status.value) == lifecycle
        assert derive_lifecycle("adopted") is None
        assert derive_lifecycle(None) is None

    _ALLOWED = {
        ("proposed", "adopted"),
        ("proposed", "superseded"),
        ("proposed", "rejected"),
        ("adopted", "proposed"),
        ("adopted", "deprecated"),
        ("adopted", "superseded"),
        ("adopted", "reverted"),
        ("deprecated", "adopted"),
        ("deprecated", "superseded"),
        ("superseded", "adopted"),
        ("superseded", "deprecated"),
        ("rejected", "proposed"),
        ("reverted", "proposed"),
    }

    @pytest.mark.parametrize("source", LIFECYCLES)
    @pytest.mark.parametrize("target", LIFECYCLES)
    def test_transition_table_through_apply_change(self, source, target):
        """All 36 cells of design §4.3, diagonal included (an allowed no-op)."""
        successors = ["ADR-002"] if source == "superseded" else []
        adr = _adr(status=project_status(source, successors))
        doc = _doc(adr, lifecycle=source, links={"superseded_by": successors})
        add = (
            {"superseded_by": ["ADR-002"]}
            if target == "superseded" and source != "superseded"
            else None
        )
        remove = (
            {"superseded_by": ["ADR-002"]}
            if successors and target not in ("superseded", "reverted")
            else None
        )
        change = LineageChange(lifecycle=target, reason="r", add_links=add, remove_links=remove)

        outcome = apply_change(doc, adr, change, {"ADR-001", "ADR-002"}, NOW)

        if source == target or (source, target) in self._ALLOWED:
            assert outcome.error is None
            assert outcome.lifecycle == target
            assert outcome.new_status == project_status(target, outcome.links["superseded_by"])
            assert outcome.changed is (source != target)
        else:
            assert outcome.error["code"] == "transition_not_allowed"
            assert outcome.error["allowed_to"] == allowed_to(source)

    def test_allowed_to_lists_targets_in_vocabulary_order(self):
        assert allowed_to("adopted") == ["proposed", "deprecated", "superseded", "reverted"]
        assert allowed_to("rejected") == ["proposed"]
        assert allowed_to("bogus") == []


# ─── The lenient read view, one test per row of design §2.7 ──────────────


class TestLineageView:
    def test_no_file_is_derived_with_nothing_recorded(self):
        adr = _adr(status=DecisionStatus.PROPOSED)
        view = lineage_view(adr, RawLineage(adr.id, exists=False))
        assert view.derived is True
        assert view.lifecycle == view.effective_lifecycle == "proposed"
        assert view.recorded_at is None
        assert view.declared == dict.fromkeys(lineage.DECLARED_FIELDS, "unknown")
        assert view.not_recorded == ["recorded_at", "origin", "recorded_timing", "decision_kind"]
        assert view.notes == []

    def test_accepted_without_lineage_derives_adopted(self):
        view = lineage_view(_adr(), RawLineage("ADR-001", exists=False))
        assert view.effective_lifecycle == "adopted"

    def test_unreadable_file_is_derived_and_noted(self):
        raw = RawLineage("ADR-001", exists=True, error="decision_lineage_unreadable")
        view = lineage_view(_adr(), raw)
        assert view.derived and _codes(view) == {"decision_lineage_unreadable"}

    @pytest.mark.parametrize("data", [["a"], "text", None, 5])
    def test_a_document_that_is_not_a_mapping_is_unreadable(self, data):
        view = lineage_view(_adr(), RawLineage("ADR-001", exists=True, data=data))
        assert view.derived and _codes(view) == {"decision_lineage_unreadable"}

    @pytest.mark.parametrize("decision_id", ["ADR-002", None, ["ADR-001"]])
    def test_a_document_for_another_id_is_unreadable(self, decision_id):
        adr = _adr()
        view = lineage_view(adr, _raw(adr, _doc(adr, decision_id=decision_id)))
        assert view.derived and _codes(view) == {"decision_lineage_unreadable"}

    def test_anchor_mismatch_hides_the_lineage(self):
        adr = _adr(status=DecisionStatus.ACCEPTED)
        other = _adr(title="a different ADR that reused the number")
        doc = _doc(other, lifecycle="rejected", recorded_at=NOW)
        view = lineage_view(adr, _raw(adr, doc))
        assert view.derived is True
        assert view.lifecycle == "adopted" and view.recorded_at is None
        assert view.declared["origin"] == "unknown"
        assert _codes(view) == {"decision_lineage_anchor_mismatch"}

    @pytest.mark.parametrize(
        "anchor",
        [
            {"date": "2026-10-02", "title_sha256": None},
            {"date": "2026-10-01", "title_sha256": "0" * 64},
            "not a mapping",
        ],
    )
    def test_any_anchor_difference_is_a_mismatch(self, anchor):
        adr = _adr()
        view = lineage_view(adr, _raw(adr, _doc(adr, anchor=anchor)))
        assert _codes(view) == {"decision_lineage_anchor_mismatch"}

    def test_an_unquoted_anchor_date_still_matches(self):
        adr = _adr()
        anchor = {"date": ADR_DATE, "title_sha256": lineage.title_sha256(adr.title)}
        view = lineage_view(adr, _raw(adr, _doc(adr, anchor=anchor)))
        assert view.derived is False and view.notes == []

    def test_missing_anchor_is_used_unchecked(self):
        adr = _adr()
        doc = _doc(adr)
        del doc["anchor"]
        view = lineage_view(adr, _raw(adr, doc))
        assert view.derived is False
        assert view.declared["origin"] == "ai_auto"
        assert _codes(view) == {"decision_lineage_anchor_missing"}

    def test_duplicate_id_is_never_attributed(self):
        adr = _adr()
        view = lineage_view(adr, _raw(adr, _doc(adr, lifecycle="rejected")), duplicate=True)
        assert view.derived and view.lifecycle == "adopted"
        assert _codes(view) == {"decision_id_duplicate"}
        assert effective_lifecycle(adr, _raw(adr, _doc(adr)), duplicate=True) == "adopted"

    @pytest.mark.parametrize("bad_id", ["ADR-١٢٣", "ADR-001\n", "../ADR-001", "ADR-1234567"])
    def test_invalid_decision_id_is_not_read(self, bad_id):
        adr = _adr(bad_id)
        view = lineage_view(adr, RawLineage(bad_id, exists=True, data=_doc(adr)))
        assert view.derived and _codes(view) == {"decision_id_invalid"}

    @pytest.mark.parametrize("schema", [2, "1", 1.0, True, None])
    def test_unsupported_schema_is_shown_as_far_as_possible(self, schema):
        adr = _adr()
        view = lineage_view(adr, _raw(adr, _doc(adr, schema=schema)))
        assert view.derived is False
        assert view.declared["origin"] == "ai_auto"
        assert _codes(view) == {"decision_lineage_schema_unsupported"}
        assert view.schema == (2 if schema == 2 and schema is not True else None)

    def test_older_or_missing_schema_is_fine(self):
        adr = _adr()
        doc = _doc(adr)
        del doc["schema"]
        assert lineage_view(adr, _raw(adr, doc)).notes == []
        assert lineage_view(adr, _raw(adr, _doc(adr, schema=0))).notes == []

    def test_unknown_lifecycle_string_is_shown_cut_and_redacted(self):
        adr = _adr(status=DecisionStatus.PROPOSED)
        value = f"archived {SECRET} " + "x" * 300
        view = lineage_view(adr, _raw(adr, _doc(adr, lifecycle=value)))
        assert view.derived is False
        assert SECRET not in view.lifecycle and "<REDACTED:secret>" in view.lifecycle
        assert len(view.lifecycle) <= lineage.MAX_LABEL_CHARS
        assert view.effective_lifecycle == "proposed"
        assert view.status_mismatch is False and view.projected_status is None
        assert _codes(view) == {"decision_lineage_lifecycle_unknown"}
        assert view.redactions == 1

    @pytest.mark.parametrize("value", [None, 3, ["adopted"]])
    def test_missing_or_non_string_lifecycle_is_filled_from_status(self, value):
        adr = _adr(status=DecisionStatus.DEPRECATED)
        doc = _doc(adr, lifecycle=value)
        if value is None:
            del doc["lifecycle"]
        view = lineage_view(adr, _raw(adr, doc))
        assert view.lifecycle == view.effective_lifecycle == "deprecated"
        assert _codes(view) == {"decision_lineage_lifecycle_unknown"}

    def test_superseded_without_successor_is_noted(self):
        adr = _adr(status=DecisionStatus.SUPERSEDED)
        view = lineage_view(adr, _raw(adr, _doc(adr, lifecycle="superseded")))
        assert view.lifecycle == "superseded"
        assert _codes(view) == {"decision_lineage_superseded_without_successor"}
        derived = lineage_view(adr, RawLineage(adr.id, exists=False))
        assert _codes(derived) == {"decision_lineage_superseded_without_successor"}

    def test_wrongly_typed_items_are_skipped_and_counted(self):
        adr = _adr()
        doc = _doc(
            adr,
            declared={"origin": 5, "recorded_timing": "post_hoc", "decision_kind": ["x"]},
            links={
                "supersedes": ["ADR-002", "ADR-²", 7, "ADR-002"],
                "superseded_by": "ADR-003",
                "amends": ["adr-004", "ADR-005"],
            },
            events=[{"at": NOW, "kind": "note", "text": "ok", "via": "t"}, "junk", 3],
        )
        view = lineage_view(adr, _raw(adr, doc))
        assert view.declared == {
            "origin": "unknown",
            "recorded_timing": "post_hoc",
            "decision_kind": "unknown",
        }
        assert view.links == {"supersedes": ["ADR-002"], "superseded_by": [], "amends": ["ADR-005"]}
        assert len(view.events) == 1 and view.events_total == 3
        # origin, decision_kind, ADR-², 7, the duplicate, superseded_by, adr-004, 2 events
        assert _note_count(view, "decision_lineage_items_skipped") == 9

    def test_declared_links_and_events_of_the_wrong_container_type(self):
        adr = _adr()
        doc = _doc(adr, declared=["ai_auto"], links=["ADR-002"], events={"kind": "note"})
        view = lineage_view(adr, _raw(adr, doc))
        assert view.declared["origin"] == "unknown"
        assert view.links == {"supersedes": [], "superseded_by": [], "amends": []}
        assert view.events == [] and view.events_total == 0
        assert _note_count(view, "decision_lineage_items_skipped") == 3

    def test_timestamps_of_other_types(self):
        adr = _adr()
        stamp = dt.datetime(2026, 10, 5, 3, 12, tzinfo=dt.UTC)
        doc = _doc(
            adr,
            recorded_at=dt.date(2026, 10, 5),
            events=[
                {"at": stamp, "kind": "note", "text": "a"},
                {"at": 12345, "kind": "note", "text": "b"},
                {"kind": "note", "text": "c"},
            ],
        )
        view = lineage_view(adr, _raw(adr, doc))
        assert view.recorded_at == "2026-10-05"
        assert [e["at"] for e in view.events] == ["2026-10-05T03:12:00+00:00", None, None]
        assert _note_count(view, "decision_lineage_items_skipped") == 1
        odd = lineage_view(adr, _raw(adr, _doc(adr, recorded_at=["x"])))
        assert odd.recorded_at is None and "recorded_at" in odd.not_recorded
        assert _codes(odd) == {"decision_lineage_items_skipped"}

    def test_unknown_top_level_keys_are_names_only(self):
        adr = _adr()
        doc = _doc(adr, fact_core={"secret": SECRET}, feedback=[SECRET])
        doc[f"key-{SECRET}"] = 1
        doc[7] = "seven"
        doc["k" * 300] = None
        for i in range(60):
            doc[f"extra{i}"] = i
        view = lineage_view(adr, _raw(adr, doc))
        assert view.unknown_keys[:4] == [
            "fact_core",
            "feedback",
            "key-<REDACTED:secret>",
            "7",
        ]
        assert view.unknown_keys[4] == "k" * lineage.MAX_LABEL_CHARS
        assert len(view.unknown_keys) == lineage.MAX_UNKNOWN_KEYS
        assert SECRET not in json.dumps(view.as_dict())

    def test_events_follow_the_per_kind_allow_list(self):
        adr = _adr()
        events = [
            {
                "at": NOW,
                "kind": "evaluation",
                "evaluation_kind": "ai_review",
                "text": "looks fine",
                "via": "pm_update_decision",
                "caused_by": b"\xff",
                SECRET: "x",
            },
            {"at": NOW, "kind": "lifecycle", "from": "proposed", "to": "adopted", "reason": {}},
            {"at": NOW, "kind": "future_kind", "via": "x", "payload": [1, 2], "text": "hidden"},
            {"at": NOW, "kind": "note", "text": "y" * (lineage.MAX_TEXT_CHARS + 5)},
        ]
        view = lineage_view(adr, _raw(adr, _doc(adr, events=events)))
        evaluation, transition, future, note = view.events
        assert evaluation == {
            "at": NOW,
            "kind": "evaluation",
            "via": "pm_update_decision",
            "evaluation_kind": "ai_review",
            "text": "looks fine",
            "recorded_as": "assistant_recorded_unverified",
            "unknown_fields": ["caused_by", "<REDACTED:secret>"],
        }
        assert transition == {"at": NOW, "kind": "lifecycle", "from": "proposed", "to": "adopted"}
        assert future == {
            "at": NOW,
            "kind": "future_kind",
            "via": "x",
            "unknown_fields": ["payload", "text"],
        }
        assert len(note["text"]) == lineage.MAX_TEXT_CHARS and note["truncated"] is True
        assert _note_count(view, "decision_lineage_items_skipped") == 1  # the dict reason
        json.dumps(view.as_dict())

    def test_only_the_last_twenty_events_are_shown(self):
        adr = _adr()
        events = [{"at": NOW, "kind": "note", "text": str(i)} for i in range(45)]
        view = lineage_view(adr, _raw(adr, _doc(adr, events=events)))
        assert view.events_total == 45
        assert [e["text"] for e in view.events] == [str(i) for i in range(25, 45)]

    def test_not_recorded_rules(self):
        adr = _adr()
        omitted = _doc(adr, declared=dict.fromkeys(lineage.DECLARED_FIELDS, "unknown"))
        assert lineage_view(adr, _raw(adr, omitted)).not_recorded == [
            "origin",
            "recorded_timing",
            "decision_kind",
        ]
        started = lineage.started_doc(adr, NOW)
        assert lineage_view(adr, _raw(adr, started)).not_recorded == [
            "recorded_at",
            "origin",
            "recorded_timing",
            "decision_kind",
        ]
        assert lineage_view(adr, _raw(adr, _doc(adr))).not_recorded == []

    def test_declared_later_lists_backfilled_fields(self):
        adr = _adr()
        events = [
            {"at": NOW, "kind": "declared", "field": "origin", "value": "ai_auto"},
            {"at": NOW, "kind": "declared", "field": "origin", "value": "ai_auto"},
            {"at": NOW, "kind": "declared", "field": "bogus"},
            {"at": NOW, "kind": "declared", "field": "recorded_timing"},
        ]
        view = lineage_view(adr, _raw(adr, _doc(adr, events=events)))
        assert view.declared_later == ["origin", "recorded_timing"]

    def test_status_mismatch_is_detected(self):
        adr = _adr(status=DecisionStatus.PROPOSED)
        view = lineage_view(adr, _raw(adr, _doc(adr, lifecycle="adopted")))
        assert view.status_mismatch is True and view.projected_status == "accepted"
        assert view.effective_lifecycle == "adopted"
        same = lineage_view(adr, _raw(adr, _doc(adr, lifecycle="proposed")))
        assert same.status_mismatch is False

    def test_effective_lifecycle(self):
        adr = _adr(status=DecisionStatus.ACCEPTED)
        assert effective_lifecycle(adr, _raw(adr, _doc(adr, lifecycle="rejected"))) == "rejected"
        assert effective_lifecycle(adr, _raw(adr, _doc(adr, lifecycle="zzz"))) == "adopted"
        assert effective_lifecycle(adr, RawLineage(adr.id, exists=False)) == "adopted"
        odd = _adr(status="archived")
        assert effective_lifecycle(odd, RawLineage(odd.id, exists=False)) is None

    def test_alias_bomb_and_recursion_are_not_expanded(self):
        bomb = ["a0: &a0 [" + ", ".join(["x"] * 9) + "]"]
        bomb += [f"a{i}: &a{i} [" + ", ".join([f"*a{i - 1}"] * 9) + "]" for i in range(1, 8)]
        adr = _adr()
        text = dump_lineage(_doc(adr), adr.id) + "\n".join(bomb) + "\n"
        text += "events: *a7\nloop: &l [*l]\n"
        data = yaml.safe_load(text)
        started = time.monotonic()
        view = lineage_view(adr, _raw(adr, data))
        assert time.monotonic() - started < 1
        assert len(json.dumps(view.as_dict())) < 10_000
        assert view.events_total == 9 and view.events == []


# ─── Helpers that keep untrusted text out of messages ────────────────────


class TestScrubbing:
    def test_error_summary_never_quotes_the_input(self):
        text = f'decisions:\n- id: ADR-001\n  title: "{SECRET}\n'
        with pytest.raises(yaml.YAMLError) as caught:
            yaml.safe_load(text)
        summary = error_summary(caught.value)
        assert SECRET not in summary
        assert summary.startswith("ScannerError at line ")

        wrapped = PmServerError("Failed to parse decisions.yaml: " + str(caught.value))
        wrapped.__cause__ = caught.value
        assert error_summary(wrapped) == summary

        class Model(BaseModel):
            title: str

        with pytest.raises(Exception) as invalid:
            Model(title=[SECRET])  # type: ignore[arg-type]
        assert error_summary(invalid.value) == "ValidationError"
        assert error_summary(PermissionError(13, SECRET)) == "PermissionError"

    def test_scrub_label_redacts_then_cuts(self):
        label = scrub_label("x" * 90 + SECRET + "y" * 50)
        assert SECRET not in label and "AKIA" not in label
        assert label.endswith("…") and len(label) == lineage.MAX_LABEL_CHARS + 1
        assert scrub_label(DecisionStatus.ACCEPTED) == "accepted"

    def test_the_coarse_cut_never_leaves_part_of_a_secret(self):
        # A long value is cut to ~limit*4 before redaction (some patterns are
        # quadratic). Redaction then shrinks what precedes that cut (a long
        # connection string becomes "<REDACTED:conn>"), so a token split by the
        # cut would surface inside the final limit. The cut lands before
        # whitespace instead, never inside a token.
        label = scrub_label("postgres://" + "u" * 634 + " " + SECRET)
        assert label == "<REDACTED:conn>…"
        text, cut, count = lineage._scrub_clip("postgres://" + "u" * 16_234 + " " + SECRET, 4_000)
        assert "AKIA" not in text and text == "<REDACTED:conn>" and cut and count == 1
        # Whitespace-separated text keeps its content up to the limit.
        text, cut, _ = lineage._scrub_clip("word " * 2_000 + SECRET, 100)
        assert text == ("word " * 20)[:100] and cut
        # No whitespace before the coarse cut: nothing is kept rather than a part.
        assert scrub_label("u" * 700 + SECRET) == "…"
        assert scrub_label("u" * 640 + SECRET + " tail") == "…"

    def test_scrub_view_walks_everything_and_counts(self):
        response = {"a": SECRET, "b": [f"x {SECRET}", {"c": SECRET}], "n": 3, "none": None}
        scrubbed, count = scrub_view(response)
        assert count == 3
        assert SECRET not in json.dumps(scrubbed)
        assert scrubbed["n"] == 3 and scrubbed["none"] is None
        assert response["a"] == SECRET  # not mutated


# ─── Bounded read (I/O) ──────────────────────────────────────────────────


class TestBoundedRead:
    def test_missing_lineage_reads_as_absent_and_creates_nothing(self, tmp_pm_path: Path):
        before = _snapshot(tmp_pm_path)
        raw = read_lineage_raw(tmp_pm_path, "ADR-001")
        assert raw.exists is False and raw.error is None
        assert _snapshot(tmp_pm_path) == before
        assert not (tmp_pm_path / ".locks").exists()
        assert not (tmp_pm_path / "decision_lineage").exists()

    def test_a_good_file_is_read_without_side_effects(self, tmp_pm_path: Path):
        adr = _adr()
        _put(tmp_pm_path, adr.id, _doc(adr))
        before = _snapshot(tmp_pm_path)
        raw = read_lineage_raw(tmp_pm_path, adr.id)
        assert raw.error is None and raw.data == _doc(adr)
        assert _snapshot(tmp_pm_path) == before

    @pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs FIFOs")
    def test_a_fifo_does_not_block(self, tmp_pm_path: Path):
        path = _path(tmp_pm_path)
        path.parent.mkdir()
        os.mkfifo(path)
        result: list[RawLineage] = []
        reader = threading.Thread(
            target=lambda: result.append(read_lineage_raw(tmp_pm_path, "ADR-001")), daemon=True
        )
        reader.start()
        reader.join(2)
        assert not reader.is_alive(), "reading a FIFO blocked"
        assert result[0].error == "decision_lineage_unreadable"
        adr = _adr()
        assert _codes(lineage_view(adr, result[0])) == {"decision_lineage_unreadable"}

    def test_a_symlinked_file_is_not_followed(self, tmp_pm_path: Path, tmp_path: Path):
        outside = tmp_path / "outside.yaml"
        outside.write_text(dump_lineage(_doc(_adr()), "ADR-001"), encoding="utf-8")
        path = _path(tmp_pm_path)
        path.parent.mkdir()
        path.symlink_to(outside)
        raw = read_lineage_raw(tmp_pm_path, "ADR-001")
        assert raw.exists and raw.error == "decision_lineage_unreadable" and raw.data is None

    def test_a_symlinked_directory_is_not_followed(self, tmp_pm_path: Path, tmp_path: Path):
        outside = tmp_path / "elsewhere"
        outside.mkdir()
        (outside / "ADR-001.yaml").write_text(dump_lineage(_doc(_adr()), "ADR-001"))
        (tmp_pm_path / "decision_lineage").symlink_to(outside, target_is_directory=True)
        raw = read_lineage_raw(tmp_pm_path, "ADR-001")
        assert raw.error == "decision_lineage_unreadable" and raw.data is None

    def test_a_directory_is_unreadable(self, tmp_pm_path: Path):
        _path(tmp_pm_path).mkdir(parents=True)
        assert read_lineage_raw(tmp_pm_path, "ADR-001").error == "decision_lineage_unreadable"

    def test_the_size_limit_is_inclusive(self, tmp_pm_path: Path):
        head = "decision_id: ADR-001\n# "
        exact = head + "x" * (MAX_LINEAGE_BYTES - len(head) - 1) + "\n"
        assert len(exact.encode()) == MAX_LINEAGE_BYTES
        _put(tmp_pm_path, "ADR-001", exact)
        assert read_lineage_raw(tmp_pm_path, "ADR-001").data == {"decision_id": "ADR-001"}
        _put(tmp_pm_path, "ADR-001", exact + "#")
        assert read_lineage_raw(tmp_pm_path, "ADR-001").error == "decision_lineage_unreadable"

    @pytest.mark.parametrize(
        "content",
        [
            b"\xff\xfe not utf-8",
            f'lifecycle: "{SECRET}\n'.encode(),
            b"when: 2026-13-45\n",  # safe_load raises ValueError for this date
            b"x: !!python/object:os.system {}\n",
        ],
    )
    def test_undecodable_content_is_unreadable_without_its_text(
        self, tmp_pm_path: Path, content: bytes
    ):
        path = _path(tmp_pm_path)
        path.parent.mkdir()
        path.write_bytes(content)
        raw = read_lineage_raw(tmp_pm_path, "ADR-001")
        assert raw.error == "decision_lineage_unreadable"
        assert SECRET not in (raw.detail or "")

    @pytest.mark.parametrize("bad_id", ["../decisions", "ADR-1/../../x", "ADR-²", ""])
    def test_an_invalid_id_never_becomes_a_path(self, tmp_pm_path: Path, bad_id: str):
        raw = read_lineage_raw(tmp_pm_path, bad_id)
        assert raw.error == "decision_id_invalid" and raw.exists is False


# ─── Create (design §5.1) ────────────────────────────────────────────────


def _read_doc(pm_path: Path, adr_id: str = "ADR-001") -> dict:
    return yaml.safe_load(_path(pm_path, adr_id).read_text(encoding="utf-8"))


class TestCreate:
    def test_records_the_adr_and_its_lineage(self, tmp_pm_path: Path):
        result = add_decision_with_lineage(
            tmp_pm_path,
            _build(),
            declared={"origin": "ai_auto", "recorded_timing": "before_impl"},
        )

        assert result.error is None and result.lineage_state == "written"
        assert result.decision.id == "ADR-001"
        assert result.recorded_at == NOW and result.lifecycle == "proposed"
        assert [d.id for d in load_decisions(tmp_pm_path)] == ["ADR-001"]
        text = _path(tmp_pm_path).read_text(encoding="utf-8")
        assert text.startswith("# PM Lens - decision_lineage/ADR-001.yaml\n")
        assert yaml.safe_load(text) == {
            "schema": 1,
            "decision_id": "ADR-001",
            "anchor": {"date": "2026-10-01", "title_sha256": lineage.title_sha256("t1")},
            "recorded_at": NOW,
            "declared": {
                "origin": "ai_auto",
                "recorded_timing": "before_impl",
                "decision_kind": "unknown",
            },
            "lifecycle": "proposed",
            "links": {"supersedes": [], "superseded_by": [], "amends": []},
            "events": [
                {
                    "at": NOW,
                    "kind": "created",
                    "lifecycle": "proposed",
                    "status": "proposed",
                    "via": "pm_add_decision",
                }
            ],
        }
        assert f"recorded_at: '{NOW}'" in text  # quoted: stays a string on reload

    def test_accepted_starts_adopted(self, tmp_pm_path: Path):
        result = add_decision_with_lineage(tmp_pm_path, _build(DecisionStatus.ACCEPTED))
        assert result.lifecycle == "adopted"
        assert _read_doc(tmp_pm_path)["lifecycle"] == "adopted"

    @pytest.mark.parametrize(
        ("declared", "message"),
        [
            ({"origin": "human-ish"}, "origin"),
            ({"recorded_timing": "later"}, "recorded_timing"),
            ({"decision_kind": 3}, "decision_kind"),
        ],
    )
    def test_declared_values_are_validated_before_anything_is_written(
        self, tmp_pm_path: Path, declared, message
    ):
        before = _snapshot(tmp_pm_path)
        with pytest.raises(PmServerError, match=message):
            add_decision_with_lineage(tmp_pm_path, _build(), declared=declared)
        assert _snapshot(tmp_pm_path) == before

    def test_an_existing_lineage_file_is_left_alone(self, tmp_pm_path: Path):
        existing = _put(tmp_pm_path, "ADR-002", "decision_id: ADR-002\nlifecycle: rejected\n")
        before = existing.read_bytes()

        def build(_number: int) -> Decision:
            return Decision(id="ADR-002", title="new", date=ADR_DATE, status="proposed")

        result = add_decision_with_lineage(tmp_pm_path, build)

        assert result.lineage_state == "preexisting" and result.recorded_at is None
        assert existing.read_bytes() == before
        assert [d.id for d in load_decisions(tmp_pm_path)] == ["ADR-002"]
        view = lineage_view(result.decision, read_lineage_raw(tmp_pm_path, "ADR-002"))
        assert view.derived is False  # no anchor in that file: used unchecked, but noted
        assert "decision_lineage_anchor_missing" in _codes(view)

    def test_a_symlink_at_the_lineage_path_is_not_written_through(
        self, tmp_pm_path: Path, tmp_path: Path
    ):
        outside = tmp_path / "target.yaml"
        outside.write_text("keep\n")
        path = _path(tmp_pm_path)
        path.parent.mkdir()
        path.symlink_to(outside)

        def build(_number: int) -> Decision:
            return Decision(id="ADR-001", title="new", date=ADR_DATE, status="proposed")

        result = add_decision_with_lineage(tmp_pm_path, build)

        assert result.lineage_state == "preexisting"
        assert outside.read_text() == "keep\n" and path.is_symlink()
        # Numbering counts the symlink as a lineage, so the next ADR goes past it.
        assert add_decision_with_lineage(tmp_pm_path, _build()).decision.id == "ADR-002"

    def test_a_symlinked_lineage_directory_refuses_before_anything_is_written(
        self, tmp_pm_path: Path, tmp_path: Path
    ):
        # Checked under the lineage lock but before decisions.yaml is saved, so
        # the refusal leaves no ADR without a lineage behind (design §2.1).
        outside = tmp_path / "elsewhere"
        outside.mkdir()
        (tmp_pm_path / "decision_lineage").symlink_to(outside, target_is_directory=True)
        before = _snapshot(tmp_pm_path)

        with pytest.raises(LineageWriteRefused) as refused:
            add_decision_with_lineage(tmp_pm_path, _build())

        assert refused.value.code == "decision_lineage_unreadable"
        assert "symbolic link" in str(refused.value)
        assert list(outside.iterdir()) == []
        after = _snapshot(tmp_pm_path)
        assert {k: v for k, v in after.items() if not k.startswith(".locks")} == before
        assert load_decisions(tmp_pm_path) == []

    def test_an_adr_built_without_a_date_is_anchored_with_the_date_it_is_saved_with(
        self, tmp_pm_path: Path
    ):
        def build(number: int) -> Decision:
            return Decision(id=f"ADR-{number:03d}", title="undated", status="proposed")

        add_decision_with_lineage(tmp_pm_path, build)

        stored = load_decisions(tmp_pm_path)[0]
        assert "date" in stored.model_fields_set  # the dump writes the default out
        doc = _read_doc(tmp_pm_path)
        assert doc["anchor"]["date"] == stored.date.isoformat()
        assert lineage.anchor_state(doc, stored) == "match"

    def test_os_error_on_the_lineage_keeps_the_adr_and_a_later_change_starts_one(
        self, tmp_pm_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        real_save = storage._save_yaml

        def failing(path: Path, data, header_name: str) -> None:
            if path.parent.name == "decision_lineage":
                raise OSError(28, "No space left on device")
            real_save(path, data, header_name)

        monkeypatch.setattr(storage, "_save_yaml", failing)
        result = add_decision_with_lineage(tmp_pm_path, _build())
        assert result.lineage_state == "not_written" and result.recorded_at is None
        assert [d.id for d in load_decisions(tmp_pm_path)] == ["ADR-001"]
        assert not _path(tmp_pm_path).exists()

        monkeypatch.setattr(storage, "_save_yaml", real_save)
        changed = change_decision_lineage(tmp_pm_path, "ADR-001", LineageChange(note="later"))
        assert changed.status == "updated"
        assert changed.events_added == ["lineage_started", "note"]
        doc = _read_doc(tmp_pm_path)
        assert doc["recorded_at"] is None and doc["lifecycle"] == "proposed"

    def test_lineage_lock_timeout_writes_nothing(
        self, tmp_pm_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        _seed(tmp_pm_path, _adr("ADR-007"))
        _path(tmp_pm_path, "ADR-007").parent.mkdir()
        before = (tmp_pm_path / "decisions.yaml").read_bytes()
        holding = threading.Event()
        release = threading.Event()

        def hold() -> None:
            with _yaml_transaction(tmp_pm_path, "decision_lineage-ADR-008"):
                holding.set()
                release.wait(10)

        holder = threading.Thread(target=hold)
        holder.start()
        try:
            assert holding.wait(5)
            monkeypatch.setenv("PM_LOCK_TIMEOUT_S", "0.3")
            with pytest.raises(PmServerError, match="timeout"):
                add_decision_with_lineage(tmp_pm_path, _build())
        finally:
            release.set()
            holder.join(10)
        assert (tmp_pm_path / "decisions.yaml").read_bytes() == before
        assert list(_path(tmp_pm_path).parent.iterdir()) == []

    def test_numbers_skip_orphaned_lineages(self, tmp_pm_path: Path):
        _seed(tmp_pm_path, _adr("ADR-001"))
        _put(tmp_pm_path, "ADR-007", "decision_id: ADR-007\n")
        assert next_decision_number(tmp_pm_path) == 8
        assert add_decision_with_lineage(tmp_pm_path, _build()).decision.id == "ADR-008"
        older = add_decision_with_next_id(
            tmp_pm_path, lambda n: Decision(id=f"ADR-{n:03d}", title="x")
        )
        assert older.id == "ADR-009"

    def test_stems_that_are_not_adr_ids_are_ignored(self, tmp_pm_path: Path):
        _seed(tmp_pm_path, _adr("ADR-002"))
        directory = tmp_pm_path / "decision_lineage"
        directory.mkdir()
        for name in ("ADR-².yaml", "ADR-١٢٣.yaml", "ADR-1234567.yaml", "tmpab12.tmp", "x.yaml"):
            (directory / name).write_text("x: 1\n", encoding="utf-8")

        assert next_decision_number(tmp_pm_path) == 3
        assert add_decision_with_lineage(tmp_pm_path, _build()).decision.id == "ADR-003"

    def test_the_last_number_is_refused_before_anything_is_written(self, tmp_pm_path: Path):
        _seed(tmp_pm_path, _adr("ADR-999999"))
        before = _snapshot(tmp_pm_path)
        result = add_decision_with_lineage(tmp_pm_path, _build())
        assert result.error == "decision_id_exhausted" and result.decision is None
        after = _snapshot(tmp_pm_path)
        after.pop(".locks", None)
        after.pop(".locks/.gitignore", None)
        after.pop(".locks/decisions.lock", None)
        assert after == before

    def test_a_lineage_alone_can_exhaust_the_numbers(self, tmp_pm_path: Path):
        _put(tmp_pm_path, "ADR-999999", "decision_id: ADR-999999\n")
        assert add_decision_with_lineage(tmp_pm_path, _build()).error == "decision_id_exhausted"
        assert not (tmp_pm_path / "decisions.yaml").exists()

    def test_build_must_return_an_adr_id(self, tmp_pm_path: Path):
        def build(_number: int) -> Decision:
            return Decision(id="../escape", title="x", status="proposed")

        with pytest.raises(PmServerError, match="ADR-NNN"):
            add_decision_with_lineage(tmp_pm_path, build)
        assert not (tmp_pm_path / "decisions.yaml").exists()

    def test_lock_files_live_flat_in_the_locks_directory(self, tmp_pm_path: Path):
        add_decision_with_lineage(tmp_pm_path, _build())
        assert (tmp_pm_path / ".locks" / "decision_lineage-ADR-001.lock").exists()
        assert (tmp_pm_path / ".locks" / "decisions.lock").exists()
        assert sorted(p.name for p in (tmp_pm_path / "decision_lineage").iterdir()) == [
            "ADR-001.yaml"
        ]
        assert not (tmp_pm_path / "decision_lineage" / ".locks").exists()

    def test_the_lineage_is_written_while_both_locks_are_held(
        self, tmp_pm_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        real_save = storage._save_yaml
        seen: dict[str, bool] = {}

        def probe(label: str) -> bool:
            outcome: list[bool] = []

            def attempt() -> None:
                try:
                    with _yaml_transaction(tmp_pm_path, label, timeout=0.2):
                        outcome.append(False)
                except PmServerError:
                    outcome.append(True)

            other = threading.Thread(target=attempt)
            other.start()
            other.join()
            return outcome[0]

        def probed(path: Path, data, header_name: str) -> None:
            if path.parent.name == "decision_lineage":
                seen["decisions"] = probe("decisions.yaml")
                seen["lineage"] = probe("decision_lineage-ADR-001")
            real_save(path, data, header_name)

        monkeypatch.setattr(storage, "_save_yaml", probed)
        add_decision_with_lineage(tmp_pm_path, _build())
        assert seen == {"decisions": True, "lineage": True}


# ─── Change (design §5.2) ────────────────────────────────────────────────


def _created(pm_path: Path, status: DecisionStatus = DecisionStatus.PROPOSED) -> Decision:
    return add_decision_with_lineage(pm_path, _build(status)).decision


class TestChange:
    def test_adopting_projects_the_status_and_says_so(self, tmp_pm_path: Path):
        _created(tmp_pm_path)
        result = change_decision_lineage(
            tmp_pm_path, "ADR-001", LineageChange(lifecycle="adopted", reason="user approved")
        )
        assert result.status == "updated"
        assert result.lifecycle == "adopted" and result.decision_status == "accepted"
        assert result.changes == {
            "lifecycle": {"from": "proposed", "to": "adopted"},
            "decision_status": {"from": "proposed", "to": "accepted"},
        }
        assert {w["code"] for w in result.warnings} == {"decision_lifecycle_changed"}
        assert load_decisions(tmp_pm_path)[0].status is DecisionStatus.ACCEPTED
        assert _read_doc(tmp_pm_path)["events"][-1] == {
            "at": NOW,
            "kind": "lifecycle",
            "from": "proposed",
            "to": "adopted",
            "status": "accepted",
            "reason": "user approved",
            "via": "pm_update_decision",
        }

    def test_events_of_one_call_share_a_time_and_follow_the_table_order(self, tmp_pm_path: Path):
        _seed(tmp_pm_path, _adr("ADR-001"), _adr("ADR-002"))
        result = change_decision_lineage(
            tmp_pm_path,
            "ADR-001",
            LineageChange(
                note="n",
                evaluation="e",
                evaluation_kind="test",
                origin="ai_auto",
                lifecycle="superseded",
                add_links={"superseded_by": ["ADR-002"]},
                reason="replaced",
            ),
        )
        assert result.events_added == [
            "lineage_started",
            "lifecycle",
            "link",
            "declared",
            "evaluation",
            "note",
        ]
        events = _read_doc(tmp_pm_path)["events"]
        assert {e["at"] for e in events} == {NOW}
        assert load_decisions(tmp_pm_path)[0].status is DecisionStatus.SUPERSEDED
        assert "decision_lineage_started" in {w["code"] for w in result.warnings}

    def test_an_empty_call_writes_nothing(self, tmp_pm_path: Path):
        _seed(tmp_pm_path, _adr())
        before = _snapshot(tmp_pm_path)
        result = change_decision_lineage(tmp_pm_path, "ADR-001", LineageChange(lifecycle="adopted"))
        assert result.status == "unchanged" and result.events_added == []
        after = _snapshot(tmp_pm_path)
        assert {k: v for k, v in after.items() if not k.startswith(".locks")} == before

    def test_a_note_only_call_leaves_a_hand_edited_status_alone(self, tmp_pm_path: Path):
        _created(tmp_pm_path, DecisionStatus.ACCEPTED)
        decisions = load_decisions(tmp_pm_path)
        decisions[0].status = DecisionStatus.DEPRECATED
        _seed(tmp_pm_path, *decisions)

        result = change_decision_lineage(tmp_pm_path, "ADR-001", LineageChange(note="n"))

        assert result.status == "updated" and result.decision_status == "deprecated"
        mismatch = next(w for w in result.warnings if w["code"] == "decision_status_mismatch")
        assert "lifecycle=adopted" in mismatch["remediation"]
        assert load_decisions(tmp_pm_path)[0].status is DecisionStatus.DEPRECATED

        again = change_decision_lineage(tmp_pm_path, "ADR-001", LineageChange(lifecycle="adopted"))
        assert again.events_added == ["status_reprojected"]
        assert {w["code"] for w in again.warnings} == {"decision_status_mismatch_resolved"}
        assert load_decisions(tmp_pm_path)[0].status is DecisionStatus.ACCEPTED

    def test_an_unknown_status_is_redacted_and_cut_in_change_results(self, tmp_pm_path: Path):
        # decisions.yaml keeps a status outside the four values as the raw string
        # (S0). With a lineage present, a call reports it back: in decision_status
        # (left as it was by a note), changes.decision_status.from and the
        # mismatch messages. Each must be the redacted, 100-character label.
        adr = _adr(status=DecisionStatus.ACCEPTED)
        _put(tmp_pm_path, adr.id, _doc(adr))  # lifecycle adopted
        hostile = "approved " + SECRET + "x" * 500
        _seed(tmp_pm_path, _adr(status=hostile))
        assert load_decisions(tmp_pm_path)[0].status == hostile

        noted = change_decision_lineage(tmp_pm_path, "ADR-001", LineageChange(note="n"))
        assert noted.status == "updated"
        assert load_decisions(tmp_pm_path)[0].status == hostile  # a note leaves it alone
        repaired = change_decision_lineage(
            tmp_pm_path, "ADR-001", LineageChange(lifecycle="adopted")
        )
        assert repaired.status == "updated" and repaired.events_added == ["status_reprojected"]

        for result, code in (
            (noted, "decision_status_mismatch"),
            (repaired, "decision_status_mismatch_resolved"),
        ):
            assert code in {w["code"] for w in result.warnings}
            dumped = json.dumps(dataclasses.asdict(result))
            assert SECRET not in dumped and "AKIA" not in dumped
            assert "x" * MAX_LABEL_CHARS not in dumped
        assert noted.decision_status.startswith("approved <REDACTED:secret>")
        assert len(noted.decision_status) == MAX_LABEL_CHARS + 1  # cut, marked "…"
        assert repaired.decision_status == "accepted"
        shown = repaired.changes["decision_status"]
        assert shown["from"] == noted.decision_status and shown["to"] == "accepted"
        written = _path(tmp_pm_path).read_text(encoding="utf-8")
        assert SECRET not in written and "AKIA" not in written
        assert _read_doc(tmp_pm_path)["events"][-1]["from_status"] == noted.decision_status
        assert load_decisions(tmp_pm_path)[0].status is DecisionStatus.ACCEPTED

    @pytest.mark.parametrize("lifecycle", LIFECYCLES)
    def test_the_diagonal_repairs_a_stale_status(self, lifecycle: str):
        successors = ["ADR-002"] if lifecycle == "superseded" else []
        projected = project_status(lifecycle, successors)
        stale = next(s for s in DecisionStatus if s is not projected)
        adr = _adr(status=stale)
        doc = _doc(adr, lifecycle=lifecycle, links={"superseded_by": successors})
        outcome = apply_change(doc, adr, LineageChange(lifecycle=lifecycle), {"ADR-002"}, NOW)
        assert outcome.events_added == ["status_reprojected"]
        assert outcome.new_status == projected.value
        assert outcome.changes == {"decision_status": {"from": stale.value, "to": projected.value}}

    def test_superseded_needs_a_successor_and_reverted_keeps_one(self, tmp_pm_path: Path):
        _seed(tmp_pm_path, _adr("ADR-001"), _adr("ADR-002"))
        refused = change_decision_lineage(
            tmp_pm_path, "ADR-001", LineageChange(lifecycle="superseded", reason="r")
        )
        assert refused.error["code"] == "superseded_by_required"
        not_allowed = change_decision_lineage(
            tmp_pm_path, "ADR-001", LineageChange(add_links={"superseded_by": ["ADR-002"]})
        )
        assert not_allowed.error["code"] == "superseded_by_not_allowed"
        reverted = change_decision_lineage(
            tmp_pm_path,
            "ADR-001",
            LineageChange(
                lifecycle="reverted", reason="r", add_links={"superseded_by": ["ADR-002"]}
            ),
        )
        assert reverted.decision_status == "superseded"
        assert not _path(tmp_pm_path, "ADR-002").exists()  # only ADR-001's lineage is written
        assert "decision_lineage_link_asymmetric" in {w["code"] for w in reverted.warnings}
        assert "ADR-002 does not list ADR-001 in supersedes" in reverted.next

    def test_superseded_without_successor_still_takes_other_changes(self, tmp_pm_path: Path):
        _seed(tmp_pm_path, _adr("ADR-001", status=DecisionStatus.SUPERSEDED), _adr("ADR-002"))
        result = change_decision_lineage(
            tmp_pm_path, "ADR-001", LineageChange(note="n", add_links={"amends": ["ADR-002"]})
        )
        assert result.status == "updated"
        assert "decision_lineage_superseded_without_successor" in {
            w["code"] for w in result.warnings
        }
        to_deprecated = change_decision_lineage(
            tmp_pm_path, "ADR-001", LineageChange(lifecycle="deprecated", reason="no successor")
        )
        assert to_deprecated.decision_status == "deprecated"

    def test_supersedes_returns_a_hint_for_the_other_side(self, tmp_pm_path: Path):
        _seed(tmp_pm_path, _adr("ADR-001"), _adr("ADR-002"))
        result = change_decision_lineage(
            tmp_pm_path, "ADR-002", LineageChange(add_links={"supersedes": ["ADR-001"]})
        )
        assert "ADR-001's lifecycle and superseded_by were not changed" in result.next
        assert "decision_lineage_link_asymmetric" in {w["code"] for w in result.warnings}
        change_decision_lineage(
            tmp_pm_path,
            "ADR-001",
            LineageChange(
                lifecycle="superseded", reason="r", add_links={"superseded_by": ["ADR-002"]}
            ),
        )
        settled = change_decision_lineage(
            tmp_pm_path, "ADR-002", LineageChange(add_links={"amends": ["ADR-001"]})
        )
        assert settled.next is None

    @pytest.mark.parametrize(
        ("change", "code"),
        [
            (LineageChange(lifecycle="approved"), "invalid_lifecycle"),
            (
                LineageChange(evaluation="e", evaluation_kind="human_review"),
                "invalid_evaluation_kind",
            ),
            (LineageChange(origin="robot", reason="r"), "invalid_origin"),
            (LineageChange(recorded_timing="soon", reason="r"), "invalid_recorded_timing"),
            (LineageChange(origin="human", reason="r"), "declared_backfill_value_not_allowed"),
            (
                LineageChange(origin="ai_proposed_human_decided", reason="r"),
                "declared_backfill_value_not_allowed",
            ),
            (
                LineageChange(recorded_timing="unknown", reason="r"),
                "declared_backfill_value_not_allowed",
            ),
            (LineageChange(origin="ai_auto"), "reason_required"),
            (LineageChange(lifecycle="rejected"), "reason_required"),
            (LineageChange(remove_links={"amends": ["ADR-002"]}), "reason_required"),
            (LineageChange(add_links={"related": ["ADR-002"]}), "invalid_link_type"),
            (LineageChange(add_links={"amends": "ADR-002"}), "invalid_link_target"),
            (LineageChange(add_links={"amends": ["adr-002"]}), "invalid_link_target"),
            (LineageChange(add_links={"amends": ["ADR-404"]}), "link_target_not_found"),
            (LineageChange(add_links={"amends": ["ADR-001"]}), "self_link"),
            (LineageChange(note="x" * 4001), "text_too_long"),
            (LineageChange(lifecycle="reverted", reason="r"), "transition_not_allowed"),
        ],
    )
    def test_refused_changes_write_nothing(self, tmp_pm_path: Path, change, code):
        _seed(tmp_pm_path, _adr("ADR-001", status=DecisionStatus.PROPOSED), _adr("ADR-002"))
        before = _snapshot(tmp_pm_path)
        result = change_decision_lineage(tmp_pm_path, "ADR-001", change)
        assert result.status == "error" and result.error["code"] == code
        after = {k: v for k, v in _snapshot(tmp_pm_path).items() if not k.startswith(".locks")}
        assert after == before

    def test_too_many_links(self):
        ids = [f"ADR-{i:03d}" for i in range(2, 54)]
        adr = _adr()
        outcome = apply_change(
            _doc(adr), adr, LineageChange(add_links={"amends": ids}), {"ADR-001", *ids}, NOW
        )
        assert outcome.error["code"] == "too_many_links"

    def test_backfill_fills_an_unknown_value_once(self, tmp_pm_path: Path):
        _created(tmp_pm_path, DecisionStatus.ACCEPTED)
        path = _path(tmp_pm_path)
        refused = change_decision_lineage(
            tmp_pm_path, "ADR-001", LineageChange(origin="human", reason="the user said so")
        )
        assert refused.error["code"] == "declared_backfill_value_not_allowed"

        filled = change_decision_lineage(
            tmp_pm_path,
            "ADR-001",
            LineageChange(origin="ai_auto", recorded_timing="post_hoc", reason="from the log"),
        )
        assert filled.changes["declared"] == {
            "origin": {"from": "unknown", "to": "ai_auto"},
            "recorded_timing": {"from": "unknown", "to": "post_hoc"},
        }
        doc = _read_doc(tmp_pm_path)
        assert doc["declared"]["origin"] == "ai_auto"
        assert doc["events"][-1]["basis"] == "backfill"
        before = path.read_bytes()
        again = change_decision_lineage(
            tmp_pm_path, "ADR-001", LineageChange(origin="ai_auto", reason="again")
        )
        assert again.error["code"] == "declared_already_set"
        assert path.read_bytes() == before
        view = lineage_view(
            load_decisions(tmp_pm_path)[0], read_lineage_raw(tmp_pm_path, "ADR-001")
        )
        assert view.declared_later == ["origin", "recorded_timing"]

    def test_secrets_are_redacted_before_saving(self, tmp_pm_path: Path):
        _seed(tmp_pm_path, _adr("ADR-001", status=DecisionStatus.PROPOSED))
        result = change_decision_lineage(
            tmp_pm_path,
            "ADR-001",
            LineageChange(
                lifecycle="adopted",
                reason=f"approved {SECRET}",
                note=f"note {SECRET}",
                evaluation=f"eval {SECRET} {SECRET}",
            ),
        )
        assert SECRET not in _path(tmp_pm_path).read_text(encoding="utf-8")
        redacted = next(
            w for w in result.warnings if w["code"] == "decision_lineage_secrets_redacted"
        )
        assert redacted["message"].startswith("4 ")
        assert SECRET not in json.dumps(result.warnings)

    def test_duplicate_ids_are_refused(self, tmp_pm_path: Path):
        path = tmp_pm_path / "decisions.yaml"
        path.write_text(
            yaml.safe_dump(
                {"decisions": [{"id": "ADR-001", "title": "a"}, {"id": "ADR-001", "title": "b"}]}
            ),
            encoding="utf-8",
        )
        result = change_decision_lineage(tmp_pm_path, "ADR-001", LineageChange(note="n"))
        assert result.error["code"] == "decision_id_duplicate"
        assert not _path(tmp_pm_path).exists()

    @pytest.mark.parametrize("bad_id", ["../ADR-001", "ADR-001\n", "ADR-١", "adr-001"])
    def test_invalid_ids_are_refused(self, tmp_pm_path: Path, bad_id: str):
        result = change_decision_lineage(tmp_pm_path, bad_id, LineageChange(note="n"))
        assert result.error["code"] == "invalid_decision_id"
        assert not (tmp_pm_path / ".locks").exists()

    def test_not_found(self, tmp_pm_path: Path):
        _seed(tmp_pm_path, _adr("ADR-001"))
        result = change_decision_lineage(tmp_pm_path, "ADR-002", LineageChange(note="n"))
        assert result.error["code"] == "decision_not_found"

    def test_an_unknown_status_without_lineage_cannot_start_one(self, tmp_pm_path: Path):
        (tmp_pm_path / "decisions.yaml").write_text(
            "decisions:\n- id: ADR-001\n  title: t\n  status: archived\n", encoding="utf-8"
        )
        result = change_decision_lineage(tmp_pm_path, "ADR-001", LineageChange(note="n"))
        assert result.error["code"] == "decision_status_unknown"

    def test_a_broken_projection_is_refused_before_writing(
        self, tmp_pm_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        _created(tmp_pm_path)
        before = _snapshot(tmp_pm_path)
        monkeypatch.setattr(lineage, "project_status", lambda *_args: "adopted")
        with pytest.raises(PmServerError, match="not one of"):
            change_decision_lineage(
                tmp_pm_path, "ADR-001", LineageChange(lifecycle="adopted", reason="r")
            )
        assert _snapshot(tmp_pm_path) == before

    def test_a_failed_projection_is_reported_and_repaired_by_a_rerun(
        self, tmp_pm_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        _created(tmp_pm_path)
        decisions_before = (tmp_pm_path / "decisions.yaml").read_bytes()

        def failing(*_args) -> None:
            raise OSError(5, "I/O error")

        real = storage._save_decisions
        monkeypatch.setattr(storage, "_save_decisions", failing)
        result = change_decision_lineage(
            tmp_pm_path, "ADR-001", LineageChange(lifecycle="adopted", reason="r")
        )
        assert result.status == "updated" and result.decision_status == "proposed"
        assert "decision_status" not in result.changes
        assert "decision_status_not_projected" in {w["code"] for w in result.warnings}
        assert (tmp_pm_path / "decisions.yaml").read_bytes() == decisions_before
        assert _read_doc(tmp_pm_path)["lifecycle"] == "adopted"

        monkeypatch.setattr(storage, "_save_decisions", real)
        rerun = change_decision_lineage(tmp_pm_path, "ADR-001", LineageChange(lifecycle="adopted"))
        assert rerun.events_added == ["status_reprojected"]
        assert load_decisions(tmp_pm_path)[0].status is DecisionStatus.ACCEPTED

    def test_only_the_status_line_of_the_adr_changes(self, tmp_pm_path: Path):
        _seed(
            tmp_pm_path,
            _adr("ADR-001"),
            _adr("ADR-002", status=DecisionStatus.PROPOSED),
            _adr("ADR-003"),
        )
        before = (tmp_pm_path / "decisions.yaml").read_text(encoding="utf-8").splitlines()
        change_decision_lineage(
            tmp_pm_path, "ADR-002", LineageChange(lifecycle="rejected", reason="r")
        )
        after = (tmp_pm_path / "decisions.yaml").read_text(encoding="utf-8").splitlines()
        assert len(after) == len(before)
        diff = [(b, a) for b, a in zip(before, after, strict=True) if b != a]
        assert diff == [("  status: proposed", "  status: deprecated")]


# ─── Write refusals (design §2.6) and the size cap ───────────────────────


class TestWriteRefusal:
    @pytest.mark.parametrize(
        ("mutate", "code"),
        [
            (lambda d: ["not", "a", "mapping"], "decision_lineage_unreadable"),
            (lambda d: {**d, "decision_id": "ADR-002"}, "decision_lineage_unreadable"),
            (lambda d: {**d, "schema": 2}, "decision_lineage_schema_unsupported"),
            (lambda d: {**d, "schema": "1"}, "decision_lineage_schema_unsupported"),
            (lambda d: {**d, "lifecycle": "archived"}, "decision_lineage_lifecycle_unknown"),
            (
                lambda d: {k: v for k, v in d.items() if k != "lifecycle"},
                "decision_lineage_lifecycle_unknown",
            ),
            (lambda d: {**d, "declared": ["x"]}, "decision_lineage_unreadable"),
            (lambda d: {**d, "links": ["ADR-002"]}, "decision_lineage_unreadable"),
            (
                lambda d: {**d, "links": {"supersedes": "ADR-002"}},
                "decision_lineage_unreadable",
            ),
            (
                lambda d: {**d, "links": {"amends": ["ADR-002", 5]}},
                "decision_lineage_unreadable",
            ),
            (lambda d: {**d, "events": {"kind": "note"}}, "decision_lineage_unreadable"),
            (
                lambda d: {**d, "anchor": {"date": "1999-01-01", "title_sha256": "x"}},
                "decision_lineage_anchor_mismatch",
            ),
        ],
    )
    def test_each_refusal_code(self, tmp_pm_path: Path, mutate, code):
        adr = _adr()
        _seed(tmp_pm_path, adr, _adr("ADR-002"))
        path = _put(tmp_pm_path, adr.id, mutate(_doc(adr)))
        before = path.read_bytes()
        decisions_before = (tmp_pm_path / "decisions.yaml").read_bytes()

        result = change_decision_lineage(tmp_pm_path, adr.id, LineageChange(note="n"))

        assert result.error["code"] == code
        assert path.read_bytes() == before
        assert (tmp_pm_path / "decisions.yaml").read_bytes() == decisions_before

    @pytest.mark.parametrize("content", ['lifecycle: "open\n', "x" * (MAX_LINEAGE_BYTES + 1)])
    def test_unreadable_files_are_not_overwritten(self, tmp_pm_path: Path, content: str):
        _seed(tmp_pm_path, _adr())
        path = _put(tmp_pm_path, "ADR-001", content)
        before = path.read_bytes()
        result = change_decision_lineage(tmp_pm_path, "ADR-001", LineageChange(note="n"))
        assert result.error["code"] == "decision_lineage_unreadable"
        assert path.read_bytes() == before

    def test_a_symlinked_file_is_refused(self, tmp_pm_path: Path, tmp_path: Path):
        adr = _adr()
        _seed(tmp_pm_path, adr)
        outside = tmp_path / "outside.yaml"
        outside.write_text(dump_lineage(_doc(adr), adr.id), encoding="utf-8")
        before = outside.read_bytes()
        path = _path(tmp_pm_path)
        path.parent.mkdir()
        path.symlink_to(outside)
        result = change_decision_lineage(tmp_pm_path, adr.id, LineageChange(note="n"))
        assert result.error["code"] == "decision_lineage_unreadable"
        assert outside.read_bytes() == before and path.is_symlink()

    def test_a_change_that_would_exceed_the_cap_is_refused(self, tmp_pm_path: Path):
        adr = _adr()
        _seed(tmp_pm_path, adr)
        doc = _doc(adr, pad="")
        room = MAX_LINEAGE_BYTES - len(dump_lineage(doc, adr.id).encode()) - 1_000
        doc["pad"] = "p" * room
        path = _put(tmp_pm_path, adr.id, doc)
        assert path.stat().st_size < MAX_LINEAGE_BYTES
        before = path.read_bytes()

        result = change_decision_lineage(tmp_pm_path, adr.id, LineageChange(note="n" * 4_000))

        assert result.error["code"] == "decision_lineage_too_large"
        assert path.read_bytes() == before
        small = change_decision_lineage(tmp_pm_path, adr.id, LineageChange(note="fits"))
        assert small.status == "updated"


# ─── Unknown keys survive a rewrite (design §2.6) ────────────────────────


class TestUnknownKeysSurvive:
    def test_s2_keys_binary_aliases_and_recursion(self, tmp_pm_path: Path):
        adr = _adr()
        _seed(tmp_pm_path, adr)
        head = dump_lineage(_doc(adr, links={**_doc(adr)["links"], "related": ["ADR-009"]}), adr.id)
        head = head.replace(
            "  via: pm_add_decision\n", "  via: pm_add_decision\n  caused_by: !!binary /w==\n"
        )
        extra = (
            "fact_core: &fc\n  version: 1\n  blob: !!binary /w==\n  when: 2026-10-01\n"
            "explanations:\n- *fc\n- *fc\n"
            "feedback:\n  loop: &l\n  - *l\n"
        )
        _put(tmp_pm_path, adr.id, head + extra)

        result = change_decision_lineage(tmp_pm_path, adr.id, LineageChange(note="n"))

        assert result.status == "updated"
        doc = _read_doc(tmp_pm_path)
        assert doc["fact_core"]["blob"] == b"\xff"
        assert doc["fact_core"]["when"] == dt.date(2026, 10, 1)
        assert doc["explanations"][0] is doc["fact_core"]  # still an alias
        assert doc["feedback"]["loop"][0] is doc["feedback"]["loop"]
        assert doc["events"][0]["caused_by"] == b"\xff"
        assert doc["links"]["related"] == ["ADR-009"]
        assert [e["kind"] for e in doc["events"]] == ["created", "note"]
        assert list(doc)[:3] == ["schema", "decision_id", "anchor"]

    def test_an_alias_bomb_is_not_expanded_by_a_rewrite(self, tmp_pm_path: Path):
        adr = _adr()
        _seed(tmp_pm_path, adr)
        bomb = ["a0: &a0 [" + ", ".join(["x"] * 9) + "]"]
        bomb += [f"a{i}: &a{i} [" + ", ".join([f"*a{i - 1}"] * 9) + "]" for i in range(1, 8)]
        _put(tmp_pm_path, adr.id, dump_lineage(_doc(adr), adr.id) + "\n".join(bomb) + "\n")

        started = time.monotonic()
        result = change_decision_lineage(tmp_pm_path, adr.id, LineageChange(note="n"))

        assert result.status == "updated"
        assert time.monotonic() - started < 5
        assert _path(tmp_pm_path).stat().st_size < 20_000


# ─── Anchor (design §2.2) ────────────────────────────────────────────────


class TestAnchor:
    def test_a_missing_anchor_is_put_back_on_the_next_write(self, tmp_pm_path: Path):
        adr = _adr()
        _seed(tmp_pm_path, adr)
        doc = _doc(adr)
        del doc["anchor"]
        _put(tmp_pm_path, adr.id, doc)

        result = change_decision_lineage(tmp_pm_path, adr.id, LineageChange(note="n"))

        assert "decision_lineage_anchor_missing" in {w["code"] for w in result.warnings}
        written = _read_doc(tmp_pm_path)
        assert written["anchor"] == anchor_for(adr)
        assert list(written)[:3] == ["schema", "decision_id", "anchor"]

    def test_a_hand_edited_title_is_recovered_by_removing_the_anchor(self, tmp_pm_path: Path):
        _created(tmp_pm_path)
        decisions = load_decisions(tmp_pm_path)
        decisions[0].title = "renamed by hand"
        _seed(tmp_pm_path, *decisions)
        refused = change_decision_lineage(tmp_pm_path, "ADR-001", LineageChange(note="n"))
        assert refused.error["code"] == "decision_lineage_anchor_mismatch"
        view = lineage_view(decisions[0], read_lineage_raw(tmp_pm_path, "ADR-001"))
        assert view.derived and _codes(view) == {"decision_lineage_anchor_mismatch"}

        doc = _read_doc(tmp_pm_path)
        del doc["anchor"]
        _put(tmp_pm_path, "ADR-001", doc)
        fixed = change_decision_lineage(tmp_pm_path, "ADR-001", LineageChange(note="n"))
        assert fixed.status == "updated"
        assert _read_doc(tmp_pm_path)["anchor"]["title_sha256"] == lineage.title_sha256(
            "renamed by hand"
        )

    def test_a_null_anchor_date_is_not_compared(self):
        undated = Decision.model_validate({"id": "ADR-001", "title": "t"})
        assert "date" not in undated.model_fields_set  # the model filled in today
        anchor = anchor_for(undated)
        assert anchor == {"date": None, "title_sha256": lineage.title_sha256("t")}
        later = Decision(id="ADR-001", title="t", date=dt.date(2030, 1, 1))
        for decision in (undated, _adr(title="t"), later):
            assert lineage.anchor_state({"anchor": anchor}, decision) == "match"
        assert lineage.anchor_state({"anchor": anchor}, _adr(title="u")) == "mismatch"
        # A dated anchor still pins the date; an anchor without a date key is a
        # hand edit and does not match.
        assert lineage.anchor_state({"anchor": anchor_for(_adr(title="t"))}, later) == "mismatch"
        no_key = {"anchor": {"title_sha256": lineage.title_sha256("t")}}
        assert lineage.anchor_state(no_key, _adr(title="t")) == "mismatch"

    def test_an_adr_without_a_date_keeps_its_lineage_on_later_days(self, tmp_pm_path: Path):
        # Without a date key, Decision.date defaults to today: it moves every day,
        # and any pmlens that rewrites decisions.yaml freezes it at that day. An
        # anchor holding such a date would detach the lineage the next day.
        (tmp_pm_path / "decisions.yaml").write_text(
            "decisions:\n- id: ADR-001\n  title: no date\n  status: accepted\n",
            encoding="utf-8",
        )
        first = change_decision_lineage(tmp_pm_path, "ADR-001", LineageChange(note="n"))
        assert first.status == "updated"
        assert _read_doc(tmp_pm_path)["anchor"] == {
            "date": None,
            "title_sha256": lineage.title_sha256("no date"),
        }
        on_file = yaml.safe_load((tmp_pm_path / "decisions.yaml").read_text(encoding="utf-8"))
        assert "date" not in on_file["decisions"][0]  # a note does not rewrite decisions.yaml

        # A later day: some writer rewrote decisions.yaml and froze another date.
        _seed(tmp_pm_path, Decision(id="ADR-001", title="no date", date=dt.date(2026, 11, 2)))
        stored = load_decisions(tmp_pm_path)[0]
        view = lineage_view(stored, read_lineage_raw(tmp_pm_path, "ADR-001"))
        assert view.derived is False and view.notes == []
        again = change_decision_lineage(tmp_pm_path, "ADR-001", LineageChange(note="m"))
        assert again.status == "updated" and again.events_added == ["note"]

        # The title still tells ADRs apart.
        _seed(tmp_pm_path, Decision(id="ADR-001", title="another", date=dt.date(2026, 11, 2)))
        refused = change_decision_lineage(tmp_pm_path, "ADR-001", LineageChange(note="o"))
        assert refused.error["code"] == "decision_lineage_anchor_mismatch"

    def test_a_reused_number_is_not_attributed(self, tmp_pm_path: Path):
        """An older pmlens numbers from decisions.yaml only and can reuse an
        orphaned lineage's id; the anchor keeps the old lineage off the new ADR."""
        _created(tmp_pm_path)
        (tmp_pm_path / "decisions.yaml").write_text("decisions: []\n", encoding="utf-8")
        newcomer = Decision(id="ADR-001", title="something else", date=dt.date(2026, 11, 1))
        _seed(tmp_pm_path, newcomer)
        view = lineage_view(newcomer, read_lineage_raw(tmp_pm_path, "ADR-001"))
        assert view.derived and view.lifecycle == "adopted"
        assert _codes(view) == {"decision_lineage_anchor_mismatch"}


# ─── D3: an older pmlens rewriting decisions.yaml ────────────────────────


class _OldConsequences(BaseModel):
    positive: list[str] = []
    negative: list[str] = []
    mitigations: list[str] = []


class _OldDecision(BaseModel):
    """decisions.yaml as pmlens 0.16.0 modelled it: extra keys ignored, strict status."""

    id: str
    title: str
    date: dt.date
    status: DecisionStatus = DecisionStatus.ACCEPTED
    context: str = ""
    decision: str = ""
    consequences: _OldConsequences = _OldConsequences()


def _old_version_rewrite(pm_path: Path) -> None:
    path = pm_path / "decisions.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    records = [_OldDecision(**d).model_dump(mode="json") for d in data["decisions"]]
    path.write_text(
        "# PM Lens - decisions.yaml\n"
        + yaml.safe_dump({"decisions": records}, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


class TestOlderWriters:
    def _state(self, pm_path: Path) -> tuple[bytes, dict]:
        lineage_bytes = _path(pm_path).read_bytes()
        decision = next(d for d in load_decisions(pm_path) if d.id == "ADR-001")
        view = lineage_view(decision, read_lineage_raw(pm_path, "ADR-001"))
        return lineage_bytes, {
            "lifecycle": view.lifecycle,
            "status": decision.status,
            "mismatch": view.status_mismatch,
            "derived": view.derived,
        }

    def test_current_add_decision_keeps_lineage_and_projection(self, tmp_pm_path: Path):
        _created(tmp_pm_path)
        change_decision_lineage(
            tmp_pm_path, "ADR-001", LineageChange(lifecycle="rejected", reason="r")
        )
        before = self._state(tmp_pm_path)
        storage.add_decision(tmp_pm_path, Decision(id="ADR-002", title="next"))
        assert self._state(tmp_pm_path) == before
        assert before[1] == {
            "lifecycle": "rejected",
            "status": DecisionStatus.DEPRECATED,
            "mismatch": False,
            "derived": False,
        }

    def test_a_0_16_style_rewrite_keeps_lineage_and_projection(self, tmp_pm_path: Path):
        _created(tmp_pm_path)
        change_decision_lineage(
            tmp_pm_path, "ADR-001", LineageChange(lifecycle="adopted", reason="r")
        )
        before = self._state(tmp_pm_path)
        _old_version_rewrite(tmp_pm_path)
        assert self._state(tmp_pm_path) == before
        assert before[1]["status"] is DecisionStatus.ACCEPTED


# ─── Lock order (design §5.1) ────────────────────────────────────────────


class TestLockOrder:
    def test_decisions_inside_a_lineage_lock_fails_at_once(self, tmp_pm_path: Path):
        with _yaml_transaction(tmp_pm_path, "decision_lineage-ADR-001"):
            started = time.monotonic()
            with pytest.raises(PmServerError, match="lock order violation"):
                with _yaml_transaction(tmp_pm_path, "decisions.yaml", timeout=5):
                    pass
            assert time.monotonic() - started < 1

    def test_two_lineage_locks_fail_at_once(self, tmp_pm_path: Path):
        with _yaml_transaction(tmp_pm_path, "decision_lineage-ADR-001"):
            with pytest.raises(PmServerError, match="lock order violation"):
                with _yaml_transaction(tmp_pm_path, "decision_lineage-ADR-002", timeout=5):
                    pass

    def test_decisions_twice_fails_instead_of_waiting(self, tmp_pm_path: Path):
        with _yaml_transaction(tmp_pm_path, "decisions.yaml"):
            with pytest.raises(PmServerError, match="lock order violation"):
                with _yaml_transaction(tmp_pm_path, "decisions", timeout=5):
                    pass

    def test_decisions_then_lineage_is_allowed_and_the_stack_unwinds(self, tmp_pm_path: Path):
        with _yaml_transaction(tmp_pm_path, "decisions.yaml"):
            with _yaml_transaction(tmp_pm_path, "decision_lineage-ADR-001"):
                assert [label for label, _ in storage._held_ranked_locks()] == [
                    "decisions",
                    "decision_lineage-ADR-001",
                ]
        assert storage._held_ranked_locks() == []
        with pytest.raises(RuntimeError):
            with _yaml_transaction(tmp_pm_path, "decision_lineage-ADR-001"):
                raise RuntimeError("boom")
        assert storage._held_ranked_locks() == []
        with _yaml_transaction(tmp_pm_path, "decisions.yaml"):
            pass

    def test_unranked_labels_nest_as_before(self, tmp_pm_path: Path):
        with _yaml_transaction(tmp_pm_path, "tasks.yaml"):
            with _yaml_transaction(tmp_pm_path, "decisions.yaml"):
                with _yaml_transaction(tmp_pm_path, "decision_lineage-ADR-001"):
                    with _yaml_transaction(tmp_pm_path, "knowledge.yaml"):
                        pass
        with _yaml_transaction(tmp_pm_path, "decision_lineage-ADR-001"):
            with _yaml_transaction(tmp_pm_path, "registry"):
                pass

    def test_the_order_is_tracked_per_thread(self, tmp_pm_path: Path):
        errors: list[BaseException] = []
        with _yaml_transaction(tmp_pm_path, "decision_lineage-ADR-001"):

            def other() -> None:
                try:
                    with _yaml_transaction(tmp_pm_path, "decision_lineage-ADR-002", timeout=2):
                        pass
                except BaseException as exc:  # noqa: BLE001 - reported below
                    errors.append(exc)

            worker = threading.Thread(target=other)
            worker.start()
            worker.join()
        assert errors == []


# ─── D7 (S1 part): concurrent writers ────────────────────────────────────


def _worker_notes(pm_path_str: str, prefix: str, count: int) -> None:
    pm_path = Path(pm_path_str)
    for i in range(count):
        result = change_decision_lineage(pm_path, "ADR-001", LineageChange(note=f"{prefix}-{i}"))
        assert result.status == "updated", result


def _worker_create(pm_path_str: str, count: int) -> None:
    pm_path = Path(pm_path_str)
    for i in range(count):
        add_decision_with_lineage(
            pm_path,
            lambda n, i=i: Decision(
                id=f"ADR-{n:03d}", title=f"p{os.getpid()}-{i}", status="proposed"
            ),
        )


class TestConcurrency:
    def _note_texts(self, pm_path: Path) -> list[str]:
        return [e["text"] for e in _read_doc(pm_path)["events"] if e["kind"] == "note"]

    def test_notes_from_threads_are_all_kept(
        self, tmp_pm_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setenv("PM_LOCK_TIMEOUT_S", "30")
        _created(tmp_pm_path)
        threads = [
            threading.Thread(target=_worker_notes, args=(str(tmp_pm_path), f"t{n}", 3))
            for n in range(8)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(60)
        texts = self._note_texts(tmp_pm_path)
        assert sorted(texts) == sorted(f"t{n}-{i}" for n in range(8) for i in range(3))

    def test_notes_from_processes_are_all_kept(
        self, tmp_pm_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setenv("PM_LOCK_TIMEOUT_S", "30")
        _created(tmp_pm_path)
        ctx = mp.get_context("spawn")
        procs = [
            ctx.Process(target=_worker_notes, args=(str(tmp_pm_path), f"p{n}", 5)) for n in range(2)
        ]
        for proc in procs:
            proc.start()
        for proc in procs:
            proc.join(60)
        assert [proc.exitcode for proc in procs] == [0, 0]
        assert len(self._note_texts(tmp_pm_path)) == 10

    def test_concurrent_creates_get_distinct_ids_and_matching_lineages(
        self, tmp_pm_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setenv("PM_LOCK_TIMEOUT_S", "30")
        ctx = mp.get_context("spawn")
        procs = [ctx.Process(target=_worker_create, args=(str(tmp_pm_path), 8)) for _ in range(2)]
        for proc in procs:
            proc.start()
        for proc in procs:
            proc.join(60)
        assert [proc.exitcode for proc in procs] == [0, 0]
        decisions = load_decisions(tmp_pm_path)
        ids = [d.id for d in decisions]
        assert len(ids) == 16 and len(set(ids)) == 16
        for decision in decisions:
            raw = read_lineage_raw(tmp_pm_path, decision.id)
            assert raw.data["decision_id"] == decision.id
            assert lineage.anchor_state(raw.data, decision) == "match"


# ─── Module boundary (AST) ───────────────────────────────────────────────
#
# The static Lens reachability test (tests/test_ro_surface_disjoint.py) only
# knows the ledger-write helpers and process launches, so the promise "lineage.py
# never writes" is pinned here: imports are an allow-list (no shutil, io,
# tempfile, subprocess …), ``os`` may only be used for the bounded read, the one
# ``open`` is ``os.open`` in ``_read_bounded`` with read-only flags, write-shaped
# method names are banned (``str.replace`` and ``Path.replace`` cannot be told
# apart statically, so lineage.py uses ``re.sub``; likewise ``dict(x)`` instead
# of ``x.copy()``), and ``yaml.safe_dump`` may only return a string.

_LINEAGE_SOURCE = (Path(pmlens.__file__).parent / "lineage.py").read_text(encoding="utf-8")
_LINEAGE_TREE = ast.parse(_LINEAGE_SOURCE)
# Absolute module names, or "." + name for this package's modules.
_ALLOWED_IMPORTS = frozenset(
    {
        "__future__",
        "datetime",
        "hashlib",
        "os",
        "re",
        "stat",
        "collections.abc",
        "dataclasses",
        "pathlib",
        "yaml",
        ".models",
        ".redaction",
    }
)
_ALLOWED_OS_CALLS = frozenset({"open", "fstat", "read", "close"})
_BANNED_BUILTINS = frozenset({"open", "exec", "eval", "compile", "__import__"})
_BANNED_CALLS = frozenset(
    {
        "write_text",
        "write_bytes",
        "write",
        "writelines",
        "fdopen",
        "truncate",
        "ftruncate",
        "mkdir",
        "makedirs",
        "touch",
        "unlink",
        "remove",
        "rename",
        "replace",
        "rmdir",
        "rmtree",
        "chmod",
        "chown",
        "utime",
        "symlink",
        "symlink_to",
        "link",
        "hardlink_to",
        "copy",
        "copy2",
        "copyfile",
        "copyfileobj",
        "copytree",
        "copy_into",
        "move",
        "move_into",
        "_save_yaml",
        "_atomic_write_text",
        "_yaml_transaction",
        "_ensure_locks_dir",
    }
)
_YAML_DUMPS = frozenset({"dump", "safe_dump", "dump_all", "safe_dump_all"})
_ALLOWED_OPEN_FLAGS = frozenset({"O_RDONLY", "O_NOFOLLOW", "O_NONBLOCK", "O_CLOEXEC"})


def _call_name(call: ast.Call) -> str | None:
    if isinstance(call.func, ast.Name):
        return call.func.id
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    return None


def _import_names(tree: ast.AST) -> list[str]:
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.append("." * node.level + (node.module or ""))
    return names


def _enclosing_function(tree: ast.AST) -> dict[int, str]:
    """Node id -> name of the innermost function around it."""
    owner: dict[int, str] = {}
    for fn in ast.walk(tree):  # breadth-first: inner functions overwrite outer ones
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for node in ast.walk(fn):
                owner[id(node)] = fn.name
    return owner


def _write_violations(tree: ast.AST) -> list[str]:
    """Every way the source could write, open for writing or reach a writer."""
    found = [f"import {name}" for name in _import_names(tree) if name not in _ALLOWED_IMPORTS]
    owner = _enclosing_function(tree)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = _call_name(node)
        on_os = (
            isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and func.value.id == "os"
        )
        if isinstance(func, ast.Name) and name in _BANNED_BUILTINS:
            found.append(f"{name}()")
        if name in _BANNED_CALLS:
            found.append(f"{name}()")
        if isinstance(func, ast.Attribute) and name == "open":
            if not (on_os and owner.get(id(node)) == "_read_bounded"):
                found.append("open() outside os.open in _read_bounded")
        if on_os and name not in _ALLOWED_OS_CALLS:
            found.append(f"os.{name}()")
        if (
            isinstance(func, ast.Name)
            and name == "getattr"
            and node.args
            and isinstance(node.args[0], ast.Name)
            and node.args[0].id == "os"
        ):
            attr = node.args[1] if len(node.args) > 1 else None
            if not (
                isinstance(attr, ast.Constant)
                and isinstance(attr.value, str)
                and attr.value.startswith("O_")
            ):
                found.append("getattr(os, <not an O_ flag>)")
        if name in _YAML_DUMPS and (
            len(node.args) != 1 or any(kw.arg == "stream" for kw in node.keywords)
        ):
            found.append(f"{name}() with a stream")
    return found


class TestModuleBoundary:
    def test_lineage_does_not_import_storage(self):
        for node in ast.walk(_LINEAGE_TREE):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                names = {alias.name for alias in node.names}
                assert "storage" not in module.split("."), ast.dump(node)
                assert "storage" not in names, ast.dump(node)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    assert "storage" not in alias.name.split("."), ast.dump(node)

    def test_lineage_makes_no_write_calls(self):
        assert _write_violations(_LINEAGE_TREE) == []

    def test_os_open_is_one_read_only_call_in_the_bounded_reader(self):
        opens = []
        for fn in ast.walk(_LINEAGE_TREE):
            if not isinstance(fn, ast.FunctionDef):
                continue
            for node in ast.walk(fn):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "open"
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "os"
                ):
                    opens.append(fn.name)
        assert opens == ["_read_bounded"]
        reader = next(
            fn
            for fn in ast.walk(_LINEAGE_TREE)
            if isinstance(fn, ast.FunctionDef) and fn.name == "_read_bounded"
        )
        flags = set()
        for node in ast.walk(reader):
            if isinstance(node, ast.Attribute) and node.attr.startswith("O_"):
                flags.add(node.attr)
            if isinstance(node, ast.Constant) and str(node.value).startswith("O_"):
                flags.add(node.value)
        assert flags and flags <= _ALLOWED_OPEN_FLAGS

    @pytest.mark.parametrize(
        "source",
        [
            "def f(p):\n    p.write_text('x')\n",
            "def f(p):\n    open(p, 'w')\n",
            "def f(p):\n    p.open('w').write('x')\n",
            "import io\ndef f(p):\n    io.open(p, 'w')\n",
            "import os\ndef f(fd):\n    os.write(fd, b'x')\n",
            "import os\ndef f(fd):\n    os.fdopen(fd, 'w')\n",
            "import os\ndef f(p):\n    os.truncate(p, 0)\n",
            "import os\ndef f(a, b):\n    os.link(a, b)\n",
            "import os\ndef f(p):\n    os.open(p, os.O_WRONLY | os.O_CREAT)\n",
            "import os\ndef f():\n    getattr(os, 'write')(1, b'x')\n",
            "import shutil\n",
            "from shutil import copyfile\n",
            "import subprocess\n",
            "def f(a, b):\n    a.copy(b)\n",
            "import yaml\ndef f(d, s):\n    yaml.safe_dump(d, s)\n",
            "import yaml\ndef f(d, s):\n    yaml.safe_dump(d, stream=s)\n",
        ],
    )
    def test_the_checks_have_teeth(self, source: str):
        assert _write_violations(ast.parse(source)) != []
