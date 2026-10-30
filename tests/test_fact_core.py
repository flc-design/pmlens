"""Decision Lineage S2: the fact core (PMSERV-228).

What this file pins, by the S2 specification's numbering
(docs/issues/DESIGN_decision-lineage-s2.md):

* The vocabularies and the fact id grammar (§2.2, §2.9).
* The fact_core argument (§3.1 step 1): shape, vocabulary, length, the
  combination rule and the version argument.
* Writing a version (§2.2): numbering by kind (never reusing a number), the
  required items, ``source`` always proposal, ``since`` and
  ``previous_values`` (every field, redacted), ``unchanged``, copy-on-write.
* Redacted comparison and D11 for the fact core (§2.2, §6.2).
* The reversibility ratchet, changes made outside the tools and their
  absorption (§2.2, Q12), the version range and the item-level refusals
  (§2.7), traces of a removed fact core.
* The fix by adoption (Q13): ``adopted_fact_core_version`` and its three
  adoption states, ``fact_core_after_adoption`` and the refusal order (§3.1),
  and the adoption's check for an explanation of the current version.
* ``changed_ids`` / ``stale`` from the fact core alone (§2.3, D6 for 228).
* The S2 view of the fact core, its caps and unknown key names (§2.6), the
  event type rules.
* Through the tools (§3.1, §3.6, §5.2): pm_update_decision's arguments, the
  refusal order, the response keys (S1 calls keep theirs), every error code
  leaving the files' bytes alone, D5 with two threads, S1 writers not stopped
  by a broken fact core, D11 for the fact core, pm_decision_query get with
  its notices and character budget, and sizes counted as written with CRLF
  newlines (§2.8).
"""

from __future__ import annotations

import copy
import json
import os
import threading
from collections.abc import Callable
from pathlib import Path

import pytest
import yaml

from pmlens import lineage, storage
from pmlens.lineage import (
    FACT_ID_RE,
    LineageChange,
    RawLineage,
    anchor_for,
    apply_change,
    derive_lifecycle,
    dump_lineage,
    explanation_staleness,
    fact_core_notices,
    fact_core_refusal,
    floor_reversibility,
    lineage_view,
    lowers_reversibility,
    read_lineage_raw,
)
from pmlens.models import Decision, DecisionStatus, FactSource, Reversibility
from pmlens.server import pm_add_decision, pm_decision_query, pm_update_decision
from pmlens.storage import _save_decisions, change_decision_lineage

NOW = "2026-10-30T01:00:00Z"
SECRET = "AKIA" + "Z" * 16
ADR_ID = "ADR-900"
OTHER_ID = "ADR-901"

BASE: list[dict] = [
    {"kind": "what", "text": "Send the booking mail without the queue"},
    {"kind": "change", "text": "Before: through the queue. After: inside the request"},
    {
        "kind": "revert",
        "text": "Revert the setting and deploy again",
        "reversibility": "unknown",
    },
    {"kind": "weak", "text": "A slow mail server slows every booking"},
    {"kind": "uncertain", "text": "Peak volume was not measured", "confidence": "medium"},
]


@pytest.fixture(autouse=True)
def _fixed_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(lineage, "_utc_now", lambda: NOW)


def _adr(status: DecisionStatus = DecisionStatus.PROPOSED, adr_id: str = ADR_ID) -> Decision:
    return Decision(
        id=adr_id,
        title=f"title {adr_id}",
        date="2026-10-23",
        status=status,
        context="c",
        decision="d",
    )


def _doc(adr: Decision, *, decision_kind: str = "technical", **overrides: object) -> dict:
    """A lineage as pm_add_decision writes it (created event)."""
    lifecycle = derive_lifecycle(adr.status)
    doc = {
        "schema": 1,
        "decision_id": adr.id,
        "anchor": anchor_for(adr),
        "recorded_at": NOW,
        "declared": {
            "origin": "ai_auto",
            "recorded_timing": "before_impl",
            "decision_kind": decision_kind,
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


def _facts(*extra: dict, **replace: dict) -> list[dict]:
    """BASE with items replaced by kind (``revert={...}``) and ``extra`` appended."""
    items = [dict(replace.get(item["kind"], item)) for item in BASE]
    return items + [dict(item) for item in extra]


def _as_argument(fact_core: dict) -> list[dict]:
    """The stored items as a caller hands them back (ids, texts as stored)."""
    out = []
    for item in fact_core["items"]:
        entry = {"id": item["id"], "text": item["text"]}
        for name in ("confidence", "reversibility"):
            if name in item:
                entry[name] = item[name]
        out.append(entry)
    return out


def _codes(entries: list[dict]) -> list[str]:
    return [entry["code"] for entry in entries]


class _Lineage:
    """One ADR's lineage document, changed only through ``apply_change``."""

    def __init__(
        self,
        status: DecisionStatus = DecisionStatus.PROPOSED,
        *,
        decision_kind: str = "technical",
        started: bool = False,
    ) -> None:
        self.adr = _adr(status)
        self.doc: dict | None = None if started else _doc(self.adr, decision_kind=decision_kind)

    @property
    def fact_core(self) -> dict | None:
        return self.doc.get("fact_core") if self.doc else None

    @property
    def version(self) -> int:
        return self.fact_core["version"] if self.fact_core else 0

    def call(self, change: LineageChange) -> lineage.ChangeOutcome:
        outcome = apply_change(self.doc, self.adr, change, [ADR_ID, OTHER_ID], NOW)
        if outcome.error is None and outcome.changed:
            self.doc = outcome.doc
            if outcome.new_status != self.adr.status:
                self.adr = self.adr.model_copy(
                    update={"status": DecisionStatus(outcome.new_status)}
                )
        return outcome

    def facts(
        self, items: list[dict], expected: int | None = None, **kwargs: object
    ) -> lineage.ChangeOutcome:
        return self.call(
            LineageChange(
                fact_core=items,
                expected_fact_core_version=self.version if expected is None else expected,
                **kwargs,
            )
        )

    def write(self, items: list[dict], **kwargs: object) -> lineage.ChangeOutcome:
        outcome = self.facts(items, **kwargs)
        assert outcome.error is None, outcome.error
        assert outcome.changed
        return outcome

    def move(self, target: str, **kwargs: object) -> lineage.ChangeOutcome:
        outcome = self.call(LineageChange(lifecycle=target, reason="the user decided", **kwargs))
        assert outcome.error is None, outcome.error
        return outcome

    def view(self) -> lineage.LineageView:
        raw = RawLineage(self.adr.id, exists=self.doc is not None, data=self.doc)
        return lineage_view(self.adr, raw, include_s2=True)

    def refused(self, items: list[dict], code: str, **kwargs: object) -> dict:
        before = copy.deepcopy(self.doc)
        outcome = self.facts(items, **kwargs)
        assert outcome.error is not None and outcome.error["code"] == code, outcome.error
        assert outcome.doc is None and self.doc == before  # nothing changed in place
        return outcome.error


# ─── Vocabularies and ids (S2 spec §2.2, §2.9) ──────────────────────────


class TestVocabulary:
    def test_source_and_reversibility(self):
        assert [v.value for v in FactSource] == ["proposal", "observed"]
        assert [v.value for v in Reversibility] == [
            "code_revertable",
            "external_irreversible",
            "unknown",
        ]
        assert lineage.REVERSIBILITIES == ("code_revertable", "external_irreversible", "unknown")
        assert lineage.CONFIDENCES == ("high", "medium", "low")

    @pytest.mark.parametrize(
        "value",
        ["F-what", "F-change", "F-revert", "F-weak", "F-uncertain"]
        + ["F-alt.1", "F-conditions.10", "F-premise.99"],
    )
    def test_fact_ids(self, value: str):
        assert lineage.is_fact_id(value)
        assert FACT_ID_RE.fullmatch(value)

    @pytest.mark.parametrize(
        "value",
        ["F-alt.0", "F-alt.100", "F-alt.01", "F-what.1", "F-alt", "f-what", "F-what\n"]
        + ["F-alt.١", "F-other", "", None, 5, b"F-what"],
    )
    def test_not_fact_ids(self, value: object):
        assert not lineage.is_fact_id(value)

    def test_kind_of_an_id(self):
        assert lineage.fact_kind("F-premise.12") == "premise"
        assert lineage.fact_kind("F-uncertain") == "uncertain"


# ─── The fact_core argument (S2 spec §3.1 step 1) ───────────────────────


def _validate(**kwargs: object) -> dict | None:
    kwargs.setdefault("expected_fact_core_version", 0)
    return lineage.validate_change(LineageChange(**kwargs))


class TestArguments:
    def test_the_base_list_is_accepted(self):
        assert _validate(fact_core=_facts()) is None
        assert _validate(fact_core=_facts(), reason="first draft") is None

    @pytest.mark.parametrize(
        ("name", "value"),
        [
            ("lifecycle", "adopted"),
            ("add_links", {}),
            ("remove_links", {"amends": []}),
            ("evaluation", "tests pass"),
            ("note", "n"),
            ("origin", "ai_auto"),
            ("recorded_timing", "post_hoc"),
        ],
    )
    def test_fact_core_is_not_combinable(self, name: str, value: object):
        problem = _validate(fact_core=_facts(), reason="r", **{name: value})
        assert problem["code"] == "fact_core_not_combinable"
        assert name in problem["message"]

    def test_the_expected_version(self):
        missing = lineage.validate_change(LineageChange(fact_core=_facts()))
        assert missing["code"] == "fact_core_version_required"
        for value in (-1, True, "1", 1.0):
            problem = _validate(fact_core=_facts(), expected_fact_core_version=value)
            assert problem["code"] == "invalid_fact_core_version", value
        alone = lineage.validate_change(LineageChange(note="n", expected_fact_core_version=0))
        assert alone["code"] == "invalid_fact_core_version"

    @pytest.mark.parametrize(
        ("fact_core", "index"),
        [
            ("F-what", None),
            ({"kind": "what", "text": "t"}, None),
            (_facts({"text": "no id or kind"}), 5),
            ([*_facts(), "not a mapping"], 5),
            (_facts({"kind": "alt", "text": "t", "source": "observed"}), 5),
            (_facts({"kind": "alt", "text": "t", "since": 1}), 5),
            (_facts({"kind": "alt", "text": "t", f"key-{SECRET}": 1}), 5),
            (_facts({"kind": "alt"}), 5),
            (_facts({"kind": "alt", "text": 5}), 5),
            (_facts({"kind": "alt", "text": "   "}), 5),
            (_facts({"kind": "bogus", "text": "t"}), 5),
            (_facts({"kind": 3, "text": "t"}), 5),
            (_facts(what={"id": "F-what", "kind": "change", "text": "t"}), 0),
            (_facts({"id": "F-alt.1", "kind": "premise", "text": "t"}), 5),
            (_facts(uncertain={"kind": "uncertain", "text": "t"}), 4),
            (_facts(uncertain={"kind": "uncertain", "text": "t", "confidence": "sure"}), 4),
            (_facts(what={"kind": "what", "text": "t", "confidence": "high"}), 0),
            (_facts(revert={"kind": "revert", "text": "t"}), 2),
            (_facts(revert={"kind": "revert", "text": "t", "reversibility": "maybe"}), 2),
            (_facts(weak={"kind": "weak", "text": "t", "reversibility": "unknown"}), 3),
        ],
    )
    def test_invalid_fact_core(self, fact_core: object, index: int | None):
        problem = _validate(fact_core=fact_core)
        assert problem["code"] == "invalid_fact_core"
        assert problem.get("item_index") == index
        assert SECRET not in problem["message"]  # key names are not echoed

    @pytest.mark.parametrize("value", ["F-alt.0", "F-bogus", "F-what.1", 5, ""])
    def test_invalid_fact_core_id(self, value: object):
        problem = _validate(fact_core=_facts({"id": value, "text": "t"}))
        assert problem["code"] == "invalid_fact_core_id"
        assert problem["item_index"] == 5

    @pytest.mark.parametrize(
        "extra",
        [
            {"id": "F-what", "text": "again"},
            {"kind": "what", "text": "again"},
        ],
    )
    def test_duplicates(self, extra: dict):
        problem = _validate(fact_core=_facts(extra))
        assert problem["code"] == "fact_core_id_duplicate"
        assert problem["item_index"] == 5
        numbered = [{"id": "F-alt.1", "text": "a"}, {"id": "F-alt.1", "text": "b"}]
        assert _validate(fact_core=_facts(*numbered))["code"] == "fact_core_id_duplicate"

    def test_sizes(self):
        many = _facts(*({"kind": "alt", "text": "a"} for _ in range(26)))
        assert len(many) == 31
        assert _validate(fact_core=many)["code"] == "fact_core_too_large"
        assert _validate(fact_core=many[:30]) is None
        long = _facts(*({"kind": "alt", "text": "x" * 500} for _ in range(12)))
        assert _validate(fact_core=long)["code"] == "fact_core_too_large"
        one = _validate(fact_core=_facts({"kind": "alt", "text": "x" * 501}))
        assert one["code"] == "text_too_long"
        assert one["field"] == "fact_core" and one["item_index"] == 5
        assert _validate(fact_core=_facts({"kind": "alt", "text": "x" * 500})) is None

    def test_a_long_reason_names_its_field(self):
        # Every text_too_long of update carries ``field`` (S2 spec §4).
        error = _validate(fact_core=_facts(), reason="r" * 4_001)
        assert error["code"] == "text_too_long" and error["field"] == "reason"
        for name in ("note", "evaluation"):
            error = lineage.validate_change(LineageChange(**{name: "x" * 4_001}))
            assert error["code"] == "text_too_long" and error["field"] == name

    def test_a_fact_core_call_appends(self):
        # It stops at MAX_APPEND_BYTES like notes (S2 spec §2.8, §5.2).
        assert lineage.appends(LineageChange(fact_core=[], expected_fact_core_version=0))
        assert not lineage.appends(LineageChange(lifecycle="adopted", reason="r"))

    def test_an_empty_list_passes_the_argument_check_and_is_refused_by_content(self):
        assert _validate(fact_core=[]) is None
        error = _Lineage().refused([], "fact_core_item_required")
        assert error["missing"] == list(lineage.FACT_SINGLE_KINDS)


# ─── Writing a version (S2 spec §2.2) ───────────────────────────────────


class TestFirstVersion:
    def test_the_first_version(self):
        adr = _Lineage()
        outcome = adr.write(
            _facts({"kind": "alt", "text": "Keep the queue with one worker"}),
            reason="first draft",
        )
        ids = ["F-what", "F-change", "F-revert", "F-weak", "F-uncertain", "F-alt.1"]
        assert adr.fact_core == {
            "version": 1,
            "recorded_reversibility": "unknown",
            "items": [
                {"id": "F-what", "text": BASE[0]["text"], "source": "proposal", "since": 1},
                {"id": "F-change", "text": BASE[1]["text"], "source": "proposal", "since": 1},
                {
                    "id": "F-revert",
                    "text": BASE[2]["text"],
                    "reversibility": "unknown",
                    "source": "proposal",
                    "since": 1,
                },
                {"id": "F-weak", "text": BASE[3]["text"], "source": "proposal", "since": 1},
                {
                    "id": "F-uncertain",
                    "text": BASE[4]["text"],
                    "confidence": "medium",
                    "source": "proposal",
                    "since": 1,
                },
                {
                    "id": "F-alt.1",
                    "text": "Keep the queue with one worker",
                    "source": "proposal",
                    "since": 1,
                },
            ],
            "retired_ids": [],
            "previous_values": [],
        }
        assert adr.doc["events"][-1] == {
            "at": NOW,
            "kind": "fact_core",
            "version": 1,
            "changed_ids": ids,
            "reason": "first draft",
            "via": "pm_update_decision",
        }
        # The new section sits before the events (S2 spec §2.1).
        assert list(adr.doc)[-2:] == ["fact_core", "events"]
        assert outcome.changes == {
            "fact_core": {
                "from_version": 0,
                "to_version": 1,
                "changed_ids": ids,
                "added_ids": ids,
                "retired_ids": [],
            }
        }
        assert outcome.fact_core == {"version": 1, "ids": ids}
        assert outcome.events_added == ["fact_core"]
        assert outcome.lifecycle == "proposed" and outcome.new_status == "proposed"
        assert _codes(outcome.notices) == ["decision_fact_core_no_conditions"]

    def test_the_first_version_expects_zero(self):
        error = _Lineage().refused(_facts(), "fact_core_version_mismatch", expected=1)
        assert error["current_version"] == 0

    def test_new_items_are_numbered_by_kind_in_order(self):
        adr = _Lineage(decision_kind="premise_dependent")
        outcome = adr.write(
            _facts(
                {"kind": "alt", "text": "a"},
                {"kind": "premise", "text": "p"},
                {"kind": "alt", "text": "b"},
                {"kind": "conditions", "text": "c"},
            )
        )
        assert outcome.fact_core["ids"][5:] == [
            "F-alt.1",
            "F-premise.1",
            "F-alt.2",
            "F-conditions.1",
        ]
        assert _codes(outcome.notices) == []  # it has a condition
        # Single items may come by id as well as by kind.
        by_id = _Lineage()
        listed = [{"id": f"F-{item['kind']}", **item} for item in BASE]
        assert by_id.write(listed).fact_core["ids"] == list(lineage.REQUIRED_FACT_IDS)

    def test_the_reason_is_optional_and_redacted(self):
        adr = _Lineage()
        adr.write(_facts())
        assert adr.doc["events"][-1]["reason"] == ""
        second = adr.write(_facts(weak={"kind": "weak", "text": "w2"}), reason=f"key {SECRET}")
        assert adr.doc["events"][-1]["reason"] == "key <REDACTED:secret>"
        assert "decision_lineage_secrets_redacted" in _codes(second.notices)

    def test_conditions_are_noticed_only_on_the_first_version(self):
        adr = _Lineage()
        adr.write(_facts())
        second = adr.write(_facts(weak={"kind": "weak", "text": "w2"}))
        assert "decision_fact_core_no_conditions" not in _codes(second.notices)

    def test_an_adr_without_a_lineage_is_started(self):
        adr = _Lineage(DecisionStatus.ACCEPTED, started=True)
        outcome = adr.write(_facts())
        assert outcome.events_added == ["lineage_started", "fact_core"]
        assert [e["kind"] for e in adr.doc["events"]] == ["lineage_started", "fact_core"]
        codes = _codes(outcome.notices)
        assert "decision_lineage_started" in codes
        assert "decision_fact_core_changed_after_adoption" in codes


class TestNumbering:
    def test_numbers_are_never_reused(self):
        adr = _Lineage()
        adr.write(_facts({"kind": "alt", "text": "a1"}, {"kind": "alt", "text": "a2"}))
        dropped = adr.write(_facts({"id": "F-alt.1", "text": "a1"}))
        assert dropped.changes["fact_core"]["retired_ids"] == ["F-alt.2"]
        assert adr.fact_core["retired_ids"] == ["F-alt.2"]
        third = adr.write(_facts({"id": "F-alt.1", "text": "a1"}, {"kind": "alt", "text": "a3"}))
        assert third.fact_core["ids"][-1] == "F-alt.3"
        assert third.changes["fact_core"]["added_ids"] == ["F-alt.3"]

    def test_previous_values_count_as_used_numbers(self):
        adr = _Lineage()
        adr.write(_facts({"kind": "premise", "text": "p1"}))
        adr.doc["fact_core"]["previous_values"].append(
            {"id": "F-premise.7", "last_version": 1, "text": "moved", "source": "proposal"}
        )
        outcome = adr.write(
            _facts({"id": "F-premise.1", "text": "p1"}, {"kind": "premise", "text": "p"})
        )
        assert outcome.fact_core["ids"][-1] == "F-premise.8"

    def test_unknown_and_retired_ids(self):
        adr = _Lineage()
        adr.write(_facts({"kind": "alt", "text": "a1"}, {"kind": "alt", "text": "a2"}))
        adr.write(_facts({"id": "F-alt.1", "text": "a1"}))
        error = adr.refused(_facts({"id": "F-alt.2", "text": "back"}), "fact_core_id_retired")
        assert error["item_index"] == 5
        adr.refused(_facts({"id": "F-alt.9", "text": "new"}), "fact_core_id_unknown")
        adr.refused(_facts({"id": "F-premise.1", "text": "new"}), "fact_core_id_unknown")

    @pytest.mark.parametrize("where", ["items", "retired_ids"])
    def test_ninety_nine_is_the_last_number(self, where: str):
        adr = _Lineage()
        adr.write(_facts({"kind": "conditions", "text": "c"}))
        if where == "items":
            adr.doc["fact_core"]["items"][-1]["id"] = "F-conditions.99"
            keep = [{"id": "F-conditions.99", "text": "c"}]
        else:
            adr.doc["fact_core"]["retired_ids"].append("F-conditions.99")
            keep = [{"id": "F-conditions.1", "text": "c"}]
        adr.refused(
            _facts(*keep, {"kind": "conditions", "text": "more"}), "fact_core_ids_exhausted"
        )
        # Other kinds still have numbers.
        adr.write(_facts(*keep, {"kind": "alt", "text": "a"}))


class TestRequiredItems:
    @pytest.mark.parametrize("dropped", ["what", "change", "revert", "weak", "uncertain"])
    def test_every_single_item_is_required(self, dropped: str):
        items = [item for item in _facts() if item["kind"] != dropped]
        error = _Lineage().refused(items, "fact_core_item_required")
        # Kinds, the values an item's ``kind`` takes, not ids (S2 spec §2.2, §3.1).
        assert error["missing"] == [dropped]
        assert all(kind in lineage.FACT_KINDS for kind in error["missing"])

    def test_a_premise_dependent_adr_needs_a_premise(self):
        adr = _Lineage(decision_kind="premise_dependent")
        error = adr.refused(_facts({"kind": "conditions", "text": "c"}), "fact_core_item_required")
        assert error["missing"] == ["premise"]
        both = adr.refused(_facts()[1:], "fact_core_item_required")
        assert both["missing"] == ["what", "premise"]
        # A caller can pass the missing kinds straight back as ``kind``.
        assert (
            _validate(fact_core=[{"kind": kind, "text": "t"} for kind in both["missing"]]) is None
        )
        adr.write(_facts({"kind": "premise", "text": "At most 30 users"}))

    def test_premises_and_conditions_are_optional_otherwise(self):
        for kind in ("technical", "spec_policy", "unknown"):
            _Lineage(decision_kind=kind).write(_facts())


class TestRevision:
    def test_a_changed_item_moves_to_previous_values_with_every_field(self):
        adr = _Lineage(decision_kind="premise_dependent")
        adr.write(_facts({"kind": "premise", "text": "At most 10 users"}))
        outcome = adr.write(
            _facts(
                {"id": "F-premise.1", "text": "At most 30 users"},
                uncertain={"kind": "uncertain", "text": BASE[4]["text"], "confidence": "low"},
                revert={**BASE[2], "reversibility": "external_irreversible"},
            ),
            reason="the user corrected the premise",
        )
        fact_core = adr.fact_core
        assert fact_core["version"] == 2
        since = {item["id"]: item["since"] for item in fact_core["items"]}
        assert since == {
            "F-what": 1,
            "F-change": 1,
            "F-revert": 2,
            "F-weak": 1,
            "F-uncertain": 2,
            "F-premise.1": 2,
        }
        assert fact_core["recorded_reversibility"] == "external_irreversible"
        assert fact_core["previous_values"] == [
            {
                "id": "F-revert",
                "last_version": 1,
                "text": BASE[2]["text"],
                "source": "proposal",
                "reversibility": "unknown",
            },
            {
                "id": "F-uncertain",
                "last_version": 1,
                "text": BASE[4]["text"],
                "source": "proposal",
                "confidence": "medium",
            },
            {
                "id": "F-premise.1",
                "last_version": 1,
                "text": "At most 10 users",
                "source": "proposal",
            },
        ]
        changed = ["F-revert", "F-uncertain", "F-premise.1"]
        assert outcome.changes["fact_core"] == {
            "from_version": 1,
            "to_version": 2,
            "changed_ids": changed,
            "added_ids": [],
            "retired_ids": [],
        }
        assert adr.doc["events"][-1]["changed_ids"] == changed
        assert adr.doc["events"][-1]["reason"] == "the user corrected the premise"
        assert _codes(outcome.notices) == []

    def test_a_removed_item_is_retired_and_kept_as_a_previous_value(self):
        adr = _Lineage()
        adr.write(_facts({"kind": "alt", "text": "a1"}))
        outcome = adr.write(_facts())
        assert adr.fact_core["retired_ids"] == ["F-alt.1"]
        assert adr.fact_core["previous_values"] == [
            {"id": "F-alt.1", "last_version": 1, "text": "a1", "source": "proposal"}
        ]
        assert outcome.changes["fact_core"]["changed_ids"] == ["F-alt.1"]
        assert outcome.changes["fact_core"]["retired_ids"] == ["F-alt.1"]
        assert outcome.fact_core["ids"] == list(lineage.REQUIRED_FACT_IDS)

    def test_the_same_items_are_unchanged_in_any_order(self):
        adr = _Lineage()
        adr.write(_facts({"kind": "alt", "text": "a1"}))
        before = copy.deepcopy(adr.doc)
        items = _as_argument(adr.fact_core)
        for listed in (items, list(reversed(items))):
            outcome = adr.facts(listed, reason="nothing new")
            assert outcome.error is None and not outcome.changed and outcome.doc is None
            assert outcome.changes == {
                "fact_core": {
                    "from_version": 1,
                    "to_version": 1,
                    "changed_ids": [],
                    "added_ids": [],
                    "retired_ids": [],
                }
            }
            assert outcome.fact_core == {"version": 1, "ids": [i["id"] for i in items]}
            assert outcome.notices == []
        assert adr.doc == before

    def test_source_is_always_proposal_and_never_compared(self):
        adr = _Lineage()
        adr.write(_facts())
        adr.doc["fact_core"]["items"][0]["source"] = "observed"  # by hand
        assert not adr.facts(_as_argument(adr.fact_core)).changed
        adr.write(_facts(weak={"kind": "weak", "text": "w2"}))
        what = adr.fact_core["items"][0]
        assert what["source"] == "proposal" and what["since"] == 1
        assert all(item["source"] == "proposal" for item in adr.fact_core["items"])

    def test_a_since_that_is_not_a_version_counts_from_the_current_one(self):
        adr = _Lineage()
        adr.write(_facts())
        adr.write(_facts(weak={"kind": "weak", "text": "w2"}))
        adr.doc["fact_core"]["items"][0]["since"] = "one"
        adr.doc["fact_core"]["items"][1]["since"] = 9
        adr.write(_facts(weak={"kind": "weak", "text": "w3"}))
        since = [item["since"] for item in adr.fact_core["items"]]
        assert since == [2, 2, 1, 3, 1]

    def test_copy_on_write(self):
        adr = _Lineage()
        adr.write(_facts({"kind": "alt", "text": "a1"}))
        adr.doc["fact_core"]["future"] = {"kept": [1, 2]}
        shared_items = adr.doc["fact_core"]["items"]
        adr.doc["mirror"] = shared_items  # what a YAML alias gives
        adr.doc["fact_core_alias"] = adr.doc["fact_core"]
        before = copy.deepcopy(adr.doc)
        old = adr.doc
        outcome = adr.write(_facts())
        assert old == before  # nothing was changed in place
        new = outcome.doc
        assert new["mirror"] is shared_items and new["mirror"] == before["mirror"]
        assert new["fact_core_alias"] is old["fact_core"]
        assert new["fact_core"] is not old["fact_core"]
        assert new["fact_core"]["future"] == {"kept": [1, 2]}
        assert new["events"] is not old["events"] and len(new["events"]) == len(old["events"]) + 1


# ─── Redaction (S2 spec §2.2, §6.2, D11) ────────────────────────────────


class TestRedaction:
    def test_secrets_in_the_argument_are_removed_before_saving(self):
        adr = _Lineage()
        outcome = adr.write(_facts(weak={"kind": "weak", "text": f"leaks {SECRET}"}))
        assert adr.fact_core["items"][3]["text"] == "leaks <REDACTED:secret>"
        redacted = [n for n in outcome.notices if n["code"] == "decision_lineage_secrets_redacted"]
        assert redacted and redacted[0]["level"] == "warning"
        assert redacted[0]["message"].startswith("1 ")
        for value in (adr.doc, outcome.changes, outcome.notices, outcome.fact_core):
            assert SECRET not in json.dumps(value)

    def test_a_stored_secret_compares_as_redacted(self):
        adr = _Lineage()
        adr.write(_facts())
        adr.doc["fact_core"]["items"][1]["text"] = f"change with {SECRET}"  # by hand
        shown = adr.view().as_dict()["fact_core"]
        assert SECRET not in json.dumps(shown)
        # Handing back what get shows is no change: nothing is written.
        outcome = adr.facts(_as_argument(shown))
        assert outcome.error is None and not outcome.changed
        # A change elsewhere rewrites the item redacted, without moving its since.
        items = _as_argument(shown)
        items[3]["text"] = "w2"
        changed = adr.facts(items)
        assert changed.changes["fact_core"]["changed_ids"] == ["F-weak"]
        assert adr.fact_core["items"][1] == {
            "id": "F-change",
            "text": "change with <REDACTED:secret>",
            "source": "proposal",
            "since": 1,
        }
        assert "decision_lineage_secrets_redacted" in _codes(changed.notices)
        assert SECRET not in dump_lineage(adr.doc, ADR_ID)

    def test_secrets_passed_again_are_reported_when_nothing_changes(self):
        # Step 7 counts what was removed from the items passed in, before the
        # unchanged outcome of step 9 (S2 spec §3.1); a stored reason is not.
        adr = _Lineage()
        items = _facts(weak={"kind": "weak", "text": f"leaks {SECRET}"})
        adr.write(items)
        before = copy.deepcopy(adr.doc)
        outcome = adr.facts(items, reason=f"again {SECRET}")
        assert outcome.error is None and not outcome.changed
        assert adr.doc == before
        redacted = [n for n in outcome.notices if n["code"] == "decision_lineage_secrets_redacted"]
        assert len(redacted) == 1 and redacted[0]["level"] == "warning"
        assert redacted[0]["message"].startswith("1 ")
        assert "nothing was written" in redacted[0]["message"]
        for value in (outcome.changes, outcome.notices, outcome.fact_core):
            assert SECRET not in json.dumps(value)
        # Handing back the redacted text removes nothing, so nothing is reported.
        quiet = adr.facts(_as_argument(adr.fact_core))
        assert not quiet.changed
        assert "decision_lineage_secrets_redacted" not in _codes(quiet.notices)

    def test_hand_written_values_are_copied_redacted(self):
        adr = _Lineage()
        adr.write(_facts())
        items = adr.doc["fact_core"]["items"]
        items[0]["source"] = f"wiki {SECRET}"
        items[2]["reversibility"] = f"maybe {SECRET}"
        items[4]["confidence"] = f"sure {SECRET}"
        outcome = adr.write(
            _facts(
                what={"kind": "what", "text": "w"},
                revert={**BASE[2], "reversibility": "external_irreversible"},
                uncertain={**BASE[4], "confidence": "low"},
            )
        )
        text = dump_lineage(adr.doc, ADR_ID)
        assert SECRET not in text
        assert all(item["source"] == "proposal" for item in adr.fact_core["items"])
        previous = {(p["id"], p.get("reversibility")): p for p in adr.fact_core["previous_values"]}
        assert previous[("F-what", None)]["source"] == "wiki <REDACTED:secret>"
        assert previous[("F-revert", "maybe <REDACTED:secret>")]["source"] == "proposal"
        assert previous[("F-uncertain", None)]["confidence"] == "sure <REDACTED:secret>"
        # The recorded value (unknown) was taken in first, then the hand value.
        reverts = [p for p in adr.fact_core["previous_values"] if p["id"] == "F-revert"]
        assert [p["reversibility"] for p in reverts] == ["unknown", "maybe <REDACTED:secret>"]
        count = next(n for n in outcome.notices if n["code"] == "decision_lineage_secrets_redacted")
        assert count["message"].startswith("3 ")
        for value in (outcome.changes, outcome.notices, outcome.fact_core):
            assert SECRET not in json.dumps(value)

    def test_a_kept_items_source_is_counted_when_it_leaves_the_file(self):
        # Its source is replaced by proposal, not copied, but the secret it
        # held is still counted so the user is told (S2 spec §3.1 step 7).
        adr = _Lineage()
        adr.write(_facts())
        adr.doc["fact_core"]["items"][0]["source"] = f"wiki {SECRET}"  # F-what, kept
        outcome = adr.write(_facts(weak={"kind": "weak", "text": "w2"}))
        assert outcome.changes["fact_core"]["changed_ids"] == ["F-weak"]
        assert SECRET not in dump_lineage(adr.doc, ADR_ID)
        assert adr.fact_core["items"][0]["source"] == "proposal"
        count = next(n for n in outcome.notices if n["code"] == "decision_lineage_secrets_redacted")
        assert count["message"].startswith("1 ")

    def test_each_stored_value_is_counted_once(self):
        # An F-revert taken in as changed outside the tools and also edited
        # gives two previous_values entries, but its values are counted once.
        adr = _Lineage()
        adr.write(_facts())
        adr.doc["fact_core"]["items"][2]["text"] = f"revert {SECRET}"
        adr.doc["fact_core"]["items"][2]["reversibility"] = "external_irreversible"
        outcome = adr.write(
            _facts(revert={"id": "F-revert", "text": "r2", "reversibility": "unknown"})
        )
        reverts = [p for p in adr.fact_core["previous_values"] if p["id"] == "F-revert"]
        assert len(reverts) == 2
        count = next(n for n in outcome.notices if n["code"] == "decision_lineage_secrets_redacted")
        assert count["message"].startswith("1 ")


# ─── The reversibility ratchet (Q12) ───────────────────────────────────

_BEFORE = ["code_revertable", "external_irreversible", "unknown", "maybe"]


class TestRatchet:
    @pytest.mark.parametrize("before", _BEFORE)
    @pytest.mark.parametrize("after", list(lineage.REVERSIBILITIES))
    def test_only_lowering_to_code_revertable_is_refused(self, before: str, after: str):
        adr = _Lineage()
        first = "unknown" if before == "maybe" else before
        adr.write(_facts(revert={**BASE[2], "reversibility": first}))
        if before == "maybe":
            adr.doc["fact_core"]["items"][2]["reversibility"] = "maybe"  # by hand
        items = _facts(revert={**BASE[2], "reversibility": after})
        if before != "code_revertable" and after == "code_revertable":
            error = adr.refused(items, "fact_core_reversibility_lowering_refused")
            label = "out_of_vocabulary" if before == "maybe" else before
            assert (error["from"], error["to"]) == (label, "code_revertable")
            assert "maybe" not in json.dumps(error)
            assert "only the user may change" in error["remediation"]
        else:
            outcome = adr.facts(items)
            assert outcome.error is None
            assert outcome.changed == (after != before)

    def test_an_f_revert_past_the_item_cap_is_named_as_the_floor_reads_it(self):
        # A hand-written list of more than MAX_FACT_ITEMS items is readable and
        # writable; the floor reads only the first MAX_FACT_ITEMS, so an F-revert
        # past them is absent to the ratchet, and the refusal names it so.
        adr = _Lineage()
        adr.write(_facts(revert={**BASE[2], "reversibility": "code_revertable"}))
        items = adr.doc["fact_core"]["items"]
        revert = items.pop(2)
        for n in range(1, lineage.MAX_FACT_ITEMS - 3):
            items.append({"id": f"F-alt.{n}", "text": f"a{n}", "source": "proposal", "since": 1})
        items.append(revert)  # by hand: entry MAX_FACT_ITEMS + 1
        assert len(items) == lineage.MAX_FACT_ITEMS + 1
        assert lineage.fact_core_refusal(adr.doc) is None
        assert lineage.floor_reversibility(adr.fact_core) == "unknown"
        argument = [
            entry
            for entry in _as_argument(adr.fact_core)
            if entry["id"] != f"F-alt.{lineage.MAX_FACT_ITEMS - 4}"
        ]
        assert len(argument) == lineage.MAX_FACT_ITEMS
        error = adr.refused(argument, "fact_core_reversibility_lowering_refused")
        assert (error["from"], error["to"]) == ("unreadable", "code_revertable")
        assert "from code_revertable" not in error["message"]
        assert "change the one reversibility line" not in error["remediation"]
        assert f"first {lineage.MAX_FACT_ITEMS} items" in error["remediation"]
        # The view names the same value as the current one.
        outside = next(
            n
            for n in fact_core_notices(adr.view())
            if n["code"] == "decision_reversibility_changed_outside_tools"
        )
        assert f"now {error['from']})" in outside["message"]

    @pytest.mark.parametrize("value", list(lineage.REVERSIBILITIES))
    def test_the_first_version_declares_any_value(self, value: str):
        _Lineage().write(_facts(revert={**BASE[2], "reversibility": value}))

    def test_a_text_fix_keeps_the_value(self):
        adr = _Lineage()
        adr.write(_facts(revert={**BASE[2], "reversibility": "external_irreversible"}))
        adr.write(
            _facts(
                revert={
                    **BASE[2],
                    "text": "No way back once sent",
                    "reversibility": "external_irreversible",
                }
            )
        )

    @pytest.mark.parametrize(
        "broken",
        ["no_revert", "no_reversibility", b"code_revertable", {"value": "code_revertable"}],
    )
    def test_an_unreadable_stored_value_is_refused_and_never_reads_as_revertable(self, broken):
        adr = _Lineage()
        adr.write(_facts())
        items = adr.doc["fact_core"]["items"]
        if broken == "no_revert":
            del items[2]
        elif broken == "no_reversibility":
            del items[2]["reversibility"]
        else:
            items[2]["reversibility"] = broken
        # The ratchet functions on their own read it as not code_revertable.
        assert floor_reversibility(adr.fact_core) == "unknown"
        assert lowers_reversibility(adr.fact_core, "code_revertable")
        assert not lowers_reversibility(adr.fact_core, "unknown")
        # The writer refuses before it gets there, for every value.
        for value in lineage.REVERSIBILITIES:
            error = adr.refused(
                _facts(revert={**BASE[2], "reversibility": value}), "decision_lineage_unreadable"
            )
            assert "code_revertable" not in error["message"]
        if broken != "no_revert":
            assert "one reversibility line of F-revert" in error["remediation"]
        else:
            assert "has no F-revert" in error["message"]

    def test_the_ratchet_functions(self):
        assert floor_reversibility(None) == "unknown"
        assert not lowers_reversibility(None, "code_revertable")  # version 0
        assert lowers_reversibility({"version": "broken"}, "code_revertable")
        revertable = {
            "version": 1,
            "items": [{"id": "F-revert", "text": "t", "reversibility": "code_revertable"}],
        }
        assert floor_reversibility(revertable) == "code_revertable"
        assert not lowers_reversibility(revertable, "code_revertable")


class TestOutsideTools:
    def _lowered_by_hand(self) -> _Lineage:
        adr = _Lineage()
        adr.write(_facts())  # recorded unknown
        adr.doc["fact_core"]["items"][2]["reversibility"] = "code_revertable"  # the user's edit
        return adr

    def test_a_hand_lowered_value_is_shown_and_followed(self):
        adr = self._lowered_by_hand()
        view = adr.view()
        assert view.fact_core["reversibility_changed_outside_tools"] is True
        assert view.fact_core["recorded_reversibility"] == "unknown"
        assert floor_reversibility(adr.fact_core) == "code_revertable"
        notices = fact_core_notices(view)
        assert _codes(notices) == ["decision_reversibility_changed_outside_tools"]
        assert "recorded unknown, now code_revertable" in notices[0]["message"]
        assert "next fact core revision" in notices[0]["remediation"]
        stale = explanation_staleness(adr.fact_core, 1)
        assert stale.stale and stale.changed_ids == ["F-revert"]

    def test_the_next_revision_takes_it_in(self):
        adr = self._lowered_by_hand()
        # The same items (code_revertable as stored now) are not "unchanged"
        # and not lowering: the base is the stored value.
        outcome = adr.facts(_as_argument(adr.fact_core))
        assert outcome.error is None and outcome.changed
        fact_core = adr.fact_core
        assert fact_core["version"] == 2
        assert fact_core["recorded_reversibility"] == "code_revertable"
        assert fact_core["items"][2]["since"] == 2
        assert fact_core["previous_values"] == [
            {
                "id": "F-revert",
                "last_version": 1,
                "text": BASE[2]["text"],
                "source": "proposal",
                "reversibility": "unknown",
            }
        ]
        event = adr.doc["events"][-1]
        assert event["edited_outside_tools"] == ["F-revert"]
        assert event["changed_ids"] == ["F-revert"]
        assert list(event) == [
            "at",
            "kind",
            "version",
            "changed_ids",
            "reason",
            "edited_outside_tools",
            "via",
        ]
        assert outcome.changes["fact_core"]["edited_outside_tools"] == ["F-revert"]
        view = adr.view()
        assert view.fact_core["reversibility_changed_outside_tools"] is False
        assert fact_core_notices(view) == []
        assert not explanation_staleness(adr.fact_core, 2).stale

    def test_raising_a_hand_lowered_value_keeps_both_earlier_values(self):
        adr = self._lowered_by_hand()
        adr.write(_facts(revert={**BASE[2], "reversibility": "external_irreversible"}))
        reverts = [p for p in adr.fact_core["previous_values"] if p["id"] == "F-revert"]
        assert [(p["last_version"], p["reversibility"]) for p in reverts] == [
            (1, "unknown"),
            (1, "code_revertable"),
        ]
        shown = adr.view().fact_core["previous_values"]
        assert [(p["id"], p["reversibility"]) for p in shown] == [("F-revert", "code_revertable")]

    @pytest.mark.parametrize("recorded", ["missing", "maybe", b"unknown", None])
    def test_a_missing_or_unknown_recorded_value_is_taken_in(self, recorded: object):
        adr = _Lineage()
        adr.write(_facts())
        if recorded == "missing":
            del adr.doc["fact_core"]["recorded_reversibility"]
        else:
            adr.doc["fact_core"]["recorded_reversibility"] = recorded
        assert fact_core_refusal(adr.doc) is None  # not a refusal
        view = adr.view()
        assert view.fact_core["reversibility_changed_outside_tools"] is True
        assert view.fact_core["recorded_reversibility"] is None
        label = "out_of_vocabulary" if recorded == "maybe" else "unreadable"
        assert f"recorded {label}, now unknown" in fact_core_notices(view)[0]["message"]
        assert explanation_staleness(adr.fact_core, 1).stale
        outcome = adr.facts(_as_argument(adr.fact_core))
        assert outcome.changed
        assert adr.fact_core["previous_values"] == []  # the earlier value is not known
        assert adr.fact_core["recorded_reversibility"] == "unknown"
        assert adr.doc["events"][-1]["edited_outside_tools"] == ["F-revert"]

    def test_an_unreadable_current_value_says_the_user_must_fix_the_line(self):
        adr = _Lineage()
        adr.write(_facts())
        adr.doc["fact_core"]["items"][2]["reversibility"] = ["code_revertable"]
        notices = fact_core_notices(adr.view())
        assert "now unreadable" in notices[0]["message"]
        assert "Until the user fixes" in notices[0]["remediation"]

    @pytest.mark.parametrize(
        ("field", "value"), [("text", 5), ("source", [SECRET]), ("confidence", None)]
    )
    def test_an_f_revert_skipped_for_another_field_is_no_change_outside_the_tools(
        self, field: str, value: object
    ):
        # Only the reversibility itself is compared (S2 spec §2.2); a broken
        # text or source is the writer's item-level refusal, not a change.
        adr = _Lineage()
        adr.write(_facts())  # recorded unknown
        adr.doc["fact_core"]["items"][2][field] = value
        assert adr.doc["fact_core"]["items"][2]["reversibility"] == "unknown"
        view = adr.view()
        assert "F-revert" not in [item["id"] for item in view.fact_core["items"]]
        assert view.fact_core["reversibility_changed_outside_tools"] is False
        assert "decision_lineage_items_skipped" in view.note_codes
        assert fact_core_notices(view) == []
        stale = explanation_staleness(adr.fact_core, 1)
        assert not stale.stale and stale.changed_ids == []
        assert floor_reversibility(adr.fact_core) == "unknown"  # skipped reads as absent
        error = adr.refused(_facts(), "decision_lineage_unreadable")
        assert "entry 3 of fact_core.items" in error["message"]

    def test_a_skipped_f_revert_with_a_changed_value_says_what_it_holds(self):
        adr = _Lineage()
        adr.write(_facts())
        item = adr.doc["fact_core"]["items"][2]
        item["text"] = 5
        item["reversibility"] = "code_revertable"
        notices = fact_core_notices(adr.view())
        assert _codes(notices) == ["decision_reversibility_changed_outside_tools"]
        assert "recorded unknown, now code_revertable" in notices[0]["message"]
        assert "Until the user fixes" not in notices[0]["remediation"]
        assert floor_reversibility(adr.fact_core) == "unknown"

    def test_a_removed_f_revert_says_the_user_must_fix_it(self):
        adr = _Lineage()
        adr.write(_facts())
        del adr.doc["fact_core"]["items"][2]
        notices = fact_core_notices(adr.view())
        assert "now unreadable" in notices[0]["message"]
        assert "Until the user fixes" in notices[0]["remediation"]
        assert explanation_staleness(adr.fact_core, 1).stale

    def test_a_repeat_after_a_broken_f_revert_is_skipped_too(self):
        # An id repeated after a broken entry is still a repeat, so the floor
        # does not read the hand-added copy (S2 spec §2.7).
        adr = _Lineage()
        adr.write(_facts(revert={**BASE[2], "reversibility": "external_irreversible"}))
        items = adr.doc["fact_core"]["items"]
        items[2]["text"] = 123
        items.append({"id": "F-revert", "text": "x", "reversibility": "code_revertable"})
        view = adr.view()
        assert "F-revert" not in [item["id"] for item in view.fact_core["items"]]
        assert ("decision_lineage_items_skipped", 2) in [
            (note["code"], note.get("count")) for note in view.notes
        ]
        assert floor_reversibility(adr.fact_core) == "unknown"
        assert lineage.lowers_reversibility(adr.fact_core, "code_revertable")

    def test_a_repeat_after_a_usable_f_revert_makes_the_floor_unknown(self):
        # A skipped F-revert entry reads as no F-revert for the floor, even
        # when an earlier one is shown (S2 spec §2.7).
        adr = _Lineage()
        adr.write(_facts(revert={**BASE[2], "reversibility": "code_revertable"}))
        adr.doc["fact_core"]["items"].append(
            {"id": "F-revert", "text": "x", "reversibility": "external_irreversible"}
        )
        view = adr.view()
        shown = [item for item in view.fact_core["items"] if item["id"] == "F-revert"]
        assert len(shown) == 1 and shown[0]["reversibility"] == "code_revertable"
        assert floor_reversibility(adr.fact_core) == "unknown"


class TestVersionRange:
    @pytest.mark.parametrize("version", [0, -1, True, "1", 1.5, None])
    def test_a_fact_core_without_a_valid_version_is_refused_and_not_shown(self, version):
        adr = _Lineage()
        adr.write(_facts())
        adr.doc["fact_core"]["version"] = version
        for expected in (0, 1):
            error = adr.refused(_facts(), "decision_lineage_unreadable", expected=expected)
            assert "fact_core.version" in error["message"]
        view = adr.view()
        assert view.fact_core is None
        assert "decision_lineage_items_skipped" in view.note_codes

    @pytest.mark.parametrize(
        ("value", "where"),
        [([1], "fact_core"), ({"version": 1, "items": {}}, "fact_core.items")],
    )
    def test_containers_of_the_wrong_type(self, value: object, where: str):
        adr = _Lineage()
        adr.doc["fact_core"] = value
        error = adr.refused(_facts(), "decision_lineage_unreadable", expected=0)
        assert error["message"].startswith(f"{ADR_ID}: {where} ")
        assert adr.view().fact_core is None


def _broken(kind: str, doc: dict) -> None:
    fact_core = doc["fact_core"]
    items = fact_core["items"]
    match kind:
        case "not_mapping":
            items.append(f"text {SECRET}")
        case "bad_id":
            items[0]["id"] = f"F-{SECRET}"
        case "duplicate":
            items.append({"id": "F-what", "text": "again", "source": "proposal", "since": 1})
        case "text":
            items[1]["text"] = {"nested": SECRET}
        case "source":
            items[0]["source"] = SECRET.encode()
        case "confidence":
            items[4]["confidence"] = [SECRET]
        case "no_confidence":
            del items[4]["confidence"]
        case "no_weak":
            del items[3]
        case "retired":
            fact_core["retired_ids"] = [f"F-alt.{SECRET}"]
        case "retired_type":
            fact_core["retired_ids"] = SECRET
        case "previous":
            fact_core["previous_values"] = [{"id": "F-what", "text": SECRET, "source": "p"}]
        case "previous_type":
            fact_core["previous_values"] = {"id": SECRET}


_BROKEN = [
    "not_mapping",
    "bad_id",
    "duplicate",
    "text",
    "source",
    "confidence",
    "no_confidence",
    "no_weak",
    "retired",
    "retired_type",
    "previous",
    "previous_type",
]


class TestItemLevelRefusal:
    @pytest.mark.parametrize("kind", _BROKEN)
    def test_the_writer_refuses_and_names_no_value(self, kind: str):
        adr = _Lineage()
        adr.write(_facts())
        _broken(kind, adr.doc)
        assert fact_core_refusal(adr.doc) is not None
        error = adr.refused(
            _facts(weak={"kind": "weak", "text": "w2"}), "decision_lineage_unreadable"
        )
        assert SECRET not in json.dumps(error)
        assert "fact_core" in error["message"] and error["remediation"]
        view = adr.view()
        assert SECRET not in json.dumps(view.as_dict())
        if kind != "no_weak":  # a missing item is nothing to skip
            assert "decision_lineage_items_skipped" in view.note_codes

    def test_entries_are_named_by_position(self):
        adr = _Lineage()
        adr.write(_facts())
        _broken("source", adr.doc)
        error = fact_core_refusal(adr.doc)
        assert error["message"].startswith(
            f"{ADR_ID}: entry 1 of fact_core.items in the lineage file has a source that is "
            "not a string"
        )

    def test_the_view_skips_only_the_broken_items(self):
        adr = _Lineage()
        adr.write(_facts())
        _broken("text", adr.doc)
        view = adr.view()
        assert [item["id"] for item in view.fact_core["items"]] == [
            "F-what",
            "F-revert",
            "F-weak",
            "F-uncertain",
        ]
        assert view.fact_core["items_total"] == 5

    @pytest.mark.parametrize("kind", _BROKEN)
    def test_s1_writers_are_not_stopped(self, kind: str):
        adr = _Lineage()
        adr.write(_facts())
        _broken(kind, adr.doc)
        section = copy.deepcopy(adr.doc["fact_core"])
        note = adr.call(LineageChange(note="a small decision"))
        assert note.error is None and note.changed
        adopted = adr.move("adopted")
        assert adr.doc["fact_core"] == section
        event = adr.doc["events"][-1]
        assert event["to"] == "adopted" and "fact_core_version" not in event
        skipped = [
            n for n in adopted.notices if n["code"] == "decision_adoption_explanation_check_skipped"
        ]
        assert skipped and "not fixed" in skipped[0]["message"]
        assert SECRET not in json.dumps(adopted.notices)


class TestTracesOfARemovedFactCore:
    @pytest.mark.parametrize(
        ("section", "value"),
        [
            ("explanations", {"items": [{"id": "ADR-900.EX-1", "fact_core_version": 1}]}),
            ("feedback", {"items": [{"id": "ADR-900.FB-1", "fact_core_version": 2}]}),
        ],
    )
    def test_explanations_and_feedback(self, section: str, value: dict):
        adr = _Lineage()
        adr.doc[section] = value
        error = adr.refused(_facts(), "decision_lineage_unreadable")
        assert "no fact_core" in error["message"] and section in error["message"]

    def test_a_fact_core_event(self):
        adr = _Lineage()
        adr.write(_facts())
        del adr.doc["fact_core"]
        error = adr.refused(_facts(), "decision_lineage_unreadable")
        assert "its events" in error["message"]
        adr.doc["fact_core"] = None  # an empty key is the same as none
        adr.refused(_facts(), "decision_lineage_unreadable")

    @pytest.mark.parametrize("version", [0, True, "1", -1])
    def test_what_is_not_a_trace(self, version: object):
        adr = _Lineage()
        adr.doc["events"].append({"at": NOW, "kind": "fact_core", "version": version})
        adr.doc["explanations"] = {
            "items": [{"id": "ADR-900.EX-1", "fact_core_version": version}, "junk"]
        }
        adr.doc["feedback"] = ["a broken section is not looked into"]
        adr.write(_facts())

    @pytest.mark.parametrize(
        ("section", "entry"),
        [
            ("explanations", {"fact_core_version": 1}),
            ("explanations", {"id": "junk", "fact_core_version": 1}),
            ("explanations", {"id": 7, "fact_core_version": 1}),
            ("explanations", {"id": "ADR-900.FB-1", "fact_core_version": 1}),
            ("feedback", {"fact_core_version": 3}),
            ("feedback", {"id": "nope", "fact_core_version": 3}),
            ("feedback", {"id": 5, "fact_core_version": 3}),
            ("feedback", {"id": "ADR-900.EX-1", "fact_core_version": 3}),
        ],
    )
    def test_an_entry_readers_skip_is_not_a_trace(self, section: str, entry: dict):
        # Only readable entries count: an id that does not match the section's
        # pattern makes readers skip the entry (S2 spec §2.7).
        adr = _Lineage()
        adr.doc[section] = {"items": [entry]}
        assert lineage._fact_core_traces(adr.doc) is None
        adr.write(_facts())
        assert adr.fact_core["version"] == 1


# ─── The fix by adoption (Q13) ──────────────────────────────────────────


def _adopted_with_facts() -> _Lineage:
    adr = _Lineage()
    adr.write(_facts())
    adr.write(_facts(weak={"kind": "weak", "text": "w2"}))
    adr.move("adopted")
    return adr


class TestAdoptionFix:
    def test_the_adoption_records_the_version(self):
        adr = _adopted_with_facts()
        event = adr.doc["events"][-1]
        assert event == {
            "at": NOW,
            "kind": "lifecycle",
            "from": "proposed",
            "to": "adopted",
            "status": "accepted",
            "reason": "the user decided",
            "fact_core_version": 2,
            "via": "pm_update_decision",
        }
        assert lineage.adopted_fact_core_version(adr.doc) == (2, False, "adopted")

    def test_revising_unchanged_and_recreating_are_refused(self):
        adr = _adopted_with_facts()
        error = adr.refused(_facts(weak={"kind": "weak", "text": "w3"}), "fact_core_after_adoption")
        assert (
            error["adopted_fact_core_version"],
            error["current_version"],
            error["lifecycle"],
        ) == (
            2,
            2,
            "adopted",
        )
        adr.refused(_as_argument(adr.fact_core), "fact_core_after_adoption")
        del adr.doc["fact_core"]  # removed by hand: the fix stays
        error = adr.refused(_facts(), "fact_core_after_adoption", expected=0)
        assert error["current_version"] == 0

    def test_the_fix_comes_before_every_later_refusal(self):
        adr = _adopted_with_facts()
        lowering = _facts(revert={**BASE[2], "reversibility": "code_revertable"})
        adr.refused(lowering, "fact_core_after_adoption")
        adr.refused(_facts(), "fact_core_after_adoption", expected=7)
        adr.refused(_facts()[1:], "fact_core_after_adoption")
        del adr.doc["fact_core"]
        adr.doc["explanations"] = {"items": [{"id": "ADR-900.EX-1", "fact_core_version": 2}]}
        adr.refused(_facts(), "fact_core_after_adoption", expected=0)

    def test_moving_back_to_proposed_lifts_it_and_a_new_adoption_fixes_again(self):
        adr = _adopted_with_facts()
        adr.move("proposed")
        assert lineage.adopted_fact_core_version(adr.doc) == (None, False, "not_adopted")
        outcome = adr.write(_facts(weak={"kind": "weak", "text": "w3"}))
        assert _codes(outcome.notices) == []
        adr.move("adopted")
        assert adr.doc["events"][-1]["fact_core_version"] == 3
        adr.refused(_facts(weak={"kind": "weak", "text": "w4"}), "fact_core_after_adoption")

    @pytest.mark.parametrize("target", ["deprecated", "superseded", "reverted"])
    def test_later_lifecycles_stay_fixed(self, target: str):
        adr = _adopted_with_facts()
        links = {"add_links": {"superseded_by": [OTHER_ID]}} if target == "superseded" else {}
        adr.move(target, **links)
        error = adr.refused(_facts(weak={"kind": "weak", "text": "w3"}), "fact_core_after_adoption")
        assert error["lifecycle"] == target

    def test_adopted_again_from_deprecated_fixes_the_version_then(self):
        adr = _Lineage(DecisionStatus.ACCEPTED)  # adopted when recorded, no facts
        adr.write(_facts())
        adr.move("deprecated")
        adr.write(_facts(weak={"kind": "weak", "text": "w2"}))
        adr.move("adopted")
        assert adr.doc["events"][-1]["fact_core_version"] == 2
        adr.refused(_facts(weak={"kind": "weak", "text": "w3"}), "fact_core_after_adoption")
        # adopted -> adopted writes nothing, so no version either.
        same = adr.call(LineageChange(lifecycle="adopted", reason="again"))
        assert same.error is None and not same.changed

    def test_an_adoption_without_a_fact_core_keeps_the_s1_event(self):
        adr = _Lineage()
        outcome = adr.move("adopted")
        assert adr.doc["events"][-1] == {
            "at": NOW,
            "kind": "lifecycle",
            "from": "proposed",
            "to": "adopted",
            "status": "accepted",
            "reason": "the user decided",
            "via": "pm_update_decision",
        }
        assert _codes(outcome.notices) == ["decision_lifecycle_changed"]
        assert outcome.fact_core is None and "fact_core" not in outcome.changes

    def test_a_missing_recorded_value_still_fixes(self):
        adr = _Lineage()
        adr.write(_facts())
        del adr.doc["fact_core"]["recorded_reversibility"]
        outcome = adr.move("adopted")
        assert adr.doc["events"][-1]["fact_core_version"] == 1
        assert "decision_adoption_explanation_check_skipped" not in _codes(outcome.notices)
        adr.refused(_as_argument(adr.fact_core), "fact_core_after_adoption")

    def test_a_broken_fact_core_is_not_fixed_and_stays_refused(self):
        adr = _Lineage()
        adr.write(_facts())
        adr.doc["fact_core"]["items"][0]["source"] = b"\xff"
        outcome = adr.move("adopted")
        assert "fact_core_version" not in adr.doc["events"][-1]
        skipped = next(
            n for n in outcome.notices if n["code"] == "decision_adoption_explanation_check_skipped"
        )
        assert skipped["level"] == "info" and "not fixed" in skipped["message"]
        assert lineage.adopted_fact_core_version(adr.doc) == (None, False, "adopted")
        # The next revision meets the broken section, not the fix.
        adr.refused(_facts(weak={"kind": "weak", "text": "w2"}), "decision_lineage_unreadable")


def _explanation(number: int, version: object) -> dict:
    """An explanation entry placed by hand (pm_add_explanation is PMSERV-230)."""
    return {
        "id": f"{ADR_ID}.EX-{number}",
        "at": NOW,
        "via": "pm_add_explanation",
        "fact_core_version": version,
        "fact_ids_present": True,
        "paragraphs": [{"fact_ids": ["F-what"], "text": "p"}],
    }


_ADOPTED_WITHOUT = "decision_adopted_without_current_explanation"
_CHECK_SKIPPED = "decision_adoption_explanation_check_skipped"


class TestAdoptionExplanationCheck:
    """The adoption's explanation check (S2 spec §3.1, the second S1-path change)."""

    def _adopt(self, explanations: object = None, *, version: int = 2) -> tuple[_Lineage, list]:
        adr = _Lineage()
        adr.write(_facts())
        if version == 2:
            adr.write(_facts(weak={"kind": "weak", "text": "w2"}))
        if explanations is not None:
            adr.doc["explanations"] = explanations
        outcome = adr.move("adopted")
        return adr, outcome.notices

    def test_an_adoption_without_explanations_says_so(self):
        adr, notices = self._adopt()
        assert _codes(notices) == [_ADOPTED_WITHOUT, "decision_lifecycle_changed"]
        warning = notices[0]
        assert warning["level"] == "info"
        assert "version 2" in warning["message"]
        assert "no explanation written from that version" in warning["message"]
        assert adr.doc["events"][-1]["fact_core_version"] == 2  # adopted and fixed all the same

    def test_only_explanations_of_older_versions_say_so(self):
        _, notices = self._adopt({"last_number": 1, "items": [_explanation(1, 1)]})
        assert _ADOPTED_WITHOUT in _codes(notices)

    def test_an_explanation_of_the_current_version_says_nothing(self):
        explanations = {"last_number": 2, "items": [_explanation(1, 1), _explanation(2, 2)]}
        adr, notices = self._adopt(explanations)
        assert _codes(notices) == ["decision_lifecycle_changed"]
        assert adr.doc["events"][-1]["fact_core_version"] == 2

    def test_a_change_outside_the_tools_makes_the_current_one_stale(self):
        adr = _Lineage()
        adr.write(_facts())
        adr.doc["explanations"] = {"items": [_explanation(1, 1)]}
        adr.doc["fact_core"]["items"][2]["reversibility"] = "external_irreversible"
        notices = adr.move("adopted").notices
        warning = next(n for n in notices if n["code"] == _ADOPTED_WITHOUT)
        assert "changed outside pmlens's tools" in warning["message"]

    def test_an_adr_without_a_fact_core_gets_neither(self):
        adr = _Lineage()
        adr.doc["explanations"] = "broken"
        outcome = adr.move("adopted")
        assert _codes(outcome.notices) == ["decision_lifecycle_changed"]

    @pytest.mark.parametrize(
        "explanations",
        [
            SECRET,
            [SECRET],
            {"items": SECRET},
            {"last_number": 1},
            {"items": [SECRET]},
            {"items": [{**_explanation(1, 2), "id": f"{ADR_ID}.EX-{SECRET}"}]},
            {"items": [_explanation(1, True)]},
            {"items": [_explanation(1, "2")]},
            {"items": [_explanation(1, -1)]},
        ],
    )
    def test_broken_explanations_skip_the_check_and_still_fix(self, explanations: object):
        adr, notices = self._adopt(explanations)
        assert _ADOPTED_WITHOUT not in _codes(notices)
        skipped = next(n for n in notices if n["code"] == _CHECK_SKIPPED)
        assert skipped["level"] == "info"
        assert skipped["message"].startswith(f"{ADR_ID}: explanations in the lineage file")
        assert "fact_core" not in skipped["message"] and "not fixed" not in skipped["message"]
        assert SECRET not in json.dumps(notices)
        assert adr.doc["events"][-1]["fact_core_version"] == 2

    def test_a_broken_fact_core_and_broken_explanations_are_both_named(self):
        adr = _Lineage()
        adr.write(_facts())
        adr.doc["fact_core"]["items"][1]["text"] = 5
        adr.doc["explanations"] = SECRET
        notices = adr.move("adopted").notices
        assert _ADOPTED_WITHOUT not in _codes(notices)
        skipped = next(n for n in notices if n["code"] == _CHECK_SKIPPED)
        assert "fact_core and explanations" in skipped["message"]
        assert "not fixed" in skipped["message"]
        assert SECRET not in json.dumps(notices)
        assert "fact_core_version" not in adr.doc["events"][-1]

    def test_through_the_tool(self, tmp_project: Path):
        _revised(tmp_project)
        result = _update(tmp_project, lifecycle="adopted", reason="the user adopted it")
        assert result["status"] == "updated", result
        assert _ADOPTED_WITHOUT in _codes(result["warnings"])


class TestAdoptionStates:
    def test_adopted_without_facts_stays_editable_and_says_so(self):
        adr = _Lineage(DecisionStatus.ACCEPTED)
        first = adr.write(_facts())
        notice = next(
            n for n in first.notices if n["code"] == "decision_fact_core_changed_after_adoption"
        )
        assert "adopted before it had a fact core" in notice["message"]
        second = adr.write(_facts(weak={"kind": "weak", "text": "w2"}))
        notice = next(
            n for n in second.notices if n["code"] == "decision_fact_core_changed_after_adoption"
        )
        assert "without a recorded fact core" in notice["message"]
        view = adr.view()
        assert view.adopted_fact_core_version is None
        assert view.as_dict()["adopted_fact_core_version"] is None
        notices = fact_core_notices(view)
        assert _codes(notices) == ["decision_fact_core_written_after_adoption"]
        assert "written after the ADR was adopted" in notices[0]["message"]

    def test_a_lineage_started_as_adopted(self):
        adr = _Lineage(DecisionStatus.ACCEPTED, started=True)
        first = adr.write(_facts())
        assert lineage.adopted_fact_core_version(adr.doc) == (None, False, "adopted")
        notice = next(
            n for n in first.notices if n["code"] == "decision_fact_core_changed_after_adoption"
        )
        assert "adopted before it had a fact core" in notice["message"]
        # Revisions stay open and say so every time (S2 spec §6.1, 228 (4)).
        for version, text in ((2, "w2"), (3, "w3")):
            outcome = adr.write(_facts(weak={"kind": "weak", "text": text}))
            assert adr.version == version
            notice = next(
                n
                for n in outcome.notices
                if n["code"] == "decision_fact_core_changed_after_adoption"
            )
            assert "without a recorded fact core" in notice["message"]
        assert lineage.adopted_fact_core_version(adr.doc) == (None, False, "adopted")
        assert _codes(fact_core_notices(adr.view())) == [
            "decision_fact_core_written_after_adoption"
        ]

    @pytest.mark.parametrize("status", [DecisionStatus.DEPRECATED, DecisionStatus.SUPERSEDED])
    def test_a_lineage_started_from_a_later_status_is_unknown(self, status: DecisionStatus):
        adr = _Lineage(status, started=True)
        outcome = adr.write(_facts())
        assert lineage.adopted_fact_core_version(adr.doc) == (None, False, "unknown")
        notice = next(
            n for n in outcome.notices if n["code"] == "decision_fact_core_changed_after_adoption"
        )
        assert "not known from its record" in notice["message"]
        shown = fact_core_notices(adr.view())
        assert _codes(shown) == ["decision_fact_core_written_after_adoption"]
        assert "not known from its record" in shown[0]["message"]

    def test_never_adopted_adrs_get_no_adoption_notice(self):
        recorded = _Lineage(DecisionStatus.SUPERSEDED)  # recorded as superseded
        outcome = recorded.write(_facts())
        assert lineage.adopted_fact_core_version(recorded.doc) == (None, False, "not_adopted")
        assert "decision_fact_core_changed_after_adoption" not in _codes(outcome.notices)
        assert fact_core_notices(recorded.view()) == []

        straight = _Lineage()  # proposed -> superseded directly
        straight.move("superseded", add_links={"superseded_by": [OTHER_ID]})
        outcome = straight.write(_facts())
        assert lineage.adopted_fact_core_version(straight.doc)[2] == "not_adopted"
        assert "decision_fact_core_changed_after_adoption" not in _codes(outcome.notices)
        assert fact_core_notices(straight.view()) == []

    def test_declared_after_review(self):
        revertable = _facts(revert={**BASE[2], "reversibility": "code_revertable"})
        quiet = _Lineage().write(revertable)
        assert "decision_reversibility_declared_after_review" not in _codes(quiet.notices)
        adopted = _Lineage(DecisionStatus.ACCEPTED).write(revertable)
        assert "decision_reversibility_declared_after_review" in _codes(adopted.notices)
        with_feedback = _Lineage()
        with_feedback.doc["feedback"] = {"items": [{"id": "ADR-900.FB-1"}]}
        outcome = with_feedback.write(revertable)
        assert "decision_reversibility_declared_after_review" in _codes(outcome.notices)
        unknown = _Lineage(DecisionStatus.ACCEPTED).write(_facts())
        assert "decision_reversibility_declared_after_review" not in _codes(unknown.notices)


class TestMalformedAdoptedVersion:
    @pytest.mark.parametrize("value", [True, "2", 0, -1, 5, None, 2.0])
    def test_the_writer_refuses_and_the_view_shows_null(self, value: object):
        adr = _adopted_with_facts()
        adr.doc["events"][-1]["fact_core_version"] = value
        assert lineage.adopted_fact_core_version(adr.doc) == (None, True, "adopted")
        error = adr.refused(
            _facts(weak={"kind": "weak", "text": "w3"}), "decision_lineage_unreadable"
        )
        assert f"entry {len(adr.doc['events'])} of events" in error["message"]
        assert repr(value) not in error["message"].split("events")[1]
        view = adr.view()
        assert view.adopted_fact_core_version is None
        assert "decision_lineage_items_skipped" in view.note_codes

    @pytest.mark.parametrize("value", [3, 7])
    def test_a_value_above_the_current_version_is_not_shown_in_the_events(self, value: int):
        # The same row of S2 spec §2.7 as adopted_fact_core_version: the
        # value itself is not shown, and is counted.
        adr = _adopted_with_facts()
        adr.doc["events"][-1]["fact_core_version"] = value
        view = adr.view()
        assert view.adopted_fact_core_version is None
        assert view.events[-1]["to"] == "adopted"
        assert "fact_core_version" not in view.events[-1]
        assert ("decision_lineage_items_skipped", 2) in [
            (note["code"], note.get("count")) for note in view.notes
        ]

    def test_a_version_in_range_or_without_a_fact_core_is_shown(self):
        adr = _adopted_with_facts()
        assert adr.view().events[-1]["fact_core_version"] == 2
        adr.doc["events"][-1]["fact_core_version"] = 5
        del adr.doc["fact_core"]  # a fact core removed by hand keeps the adoption
        view = adr.view()
        assert view.events[-1]["fact_core_version"] == 5
        assert view.adopted_fact_core_version == 5

    def test_an_older_broken_value_does_not_matter(self):
        adr = _adopted_with_facts()
        adr.doc["events"][-1]["fact_core_version"] = "junk"
        adr.move("proposed")
        adr.write(_facts(weak={"kind": "weak", "text": "w3"}))
        assert lineage.adopted_fact_core_version(adr.doc) == (None, False, "not_adopted")

    def test_a_hand_changed_version_after_the_adoption_is_noticed(self):
        adr = _adopted_with_facts()
        adr.doc["events"][-1]["fact_core_version"] = 1
        view = adr.view()
        assert view.adopted_fact_core_version == 1
        notices = fact_core_notices(view)
        assert _codes(notices) == ["decision_fact_core_changed_since_adoption"]
        del adr.doc["fact_core"]
        assert lineage.adopted_fact_core_version(adr.doc) == (1, False, "adopted")


class TestAdoptionScan:
    def _doc(self, *events: dict) -> dict:
        return {"events": list(events)}

    def test_the_latest_adopting_or_proposing_event_decides(self):
        created = {"kind": "created", "lifecycle": "proposed"}
        adopt = {"kind": "lifecycle", "from": "proposed", "to": "adopted", "fact_core_version": 1}
        deprecate = {"kind": "lifecycle", "from": "adopted", "to": "deprecated"}
        back = {"kind": "lifecycle", "from": "adopted", "to": "proposed"}
        note = {"kind": "note", "text": "n"}
        fact_core = {"fact_core": {"version": 1, "items": []}}
        scan = lineage.adopted_fact_core_version
        assert scan({**self._doc(created, adopt, deprecate, note), **fact_core}) == (
            1,
            False,
            "adopted",
        )
        assert scan({**self._doc(created, adopt, back), **fact_core}) == (
            None,
            False,
            "not_adopted",
        )
        assert scan(self._doc(created)) == (None, False, "not_adopted")
        assert scan(self._doc({"kind": "created", "lifecycle": "superseded"})) == (
            None,
            False,
            "not_adopted",
        )
        started = {"kind": "lineage_started", "lifecycle": "deprecated"}
        assert scan(self._doc(started, deprecate)) == (None, False, "unknown")
        assert scan(self._doc()) == (None, False, "unknown")
        assert scan({"events": "junk"}) == (None, False, "unknown")
        assert scan(self._doc("junk", {"kind": ["x"], "to": {}})) == (None, False, "unknown")
        # Above the current version is malformed; without a fact core it stands.
        high = {**adopt, "fact_core_version": 3}
        assert scan({**self._doc(created, high), **fact_core}) == (None, True, "adopted")
        assert scan(self._doc(created, high)) == (3, False, "adopted")


# ─── changed_ids and stale (S2 spec §2.3, D6 for 228) ───────────────────


class TestStaleness:
    def test_versions_and_changed_ids(self):
        adr = _Lineage(decision_kind="premise_dependent")
        adr.write(_facts({"kind": "premise", "text": "10 users"}, {"kind": "alt", "text": "a"}))
        adr.write(_facts({"id": "F-premise.1", "text": "30 users"}))
        explanations = [
            {"id": "ADR-900.EX-1", "fact_core_version": 1},
            {"id": "ADR-900.EX-2", "fact_core_version": 2},
        ]
        adr.doc["explanations"] = {"last_number": 2, "items": explanations}
        old = explanation_staleness(adr.fact_core, 1)
        assert old.stale and old.changed_ids == ["F-premise.1", "F-alt.1"]
        assert not old.changed_ids_truncated
        current = explanation_staleness(adr.fact_core, 2)
        assert current == lineage.Staleness(stale=False, changed_ids=[])

    def test_more_than_thirty_ids_are_all_listed(self):
        adr = _Lineage()
        many = [{"kind": "alt", "text": f"alt {n}"} for n in range(13)] + [
            {"kind": "conditions", "text": f"conditions {n}"} for n in range(12)
        ]
        adr.write(_facts(*many))  # F-alt.1-13, F-conditions.1-12
        # A new item by kind is a new id: F-alt.14 joins, the 25 above retire.
        adr.write(_facts({"kind": "alt", "text": "alt again"}))
        # F-alt.15 joins and F-alt.14 retires; 20 premises join; every single
        # item changes its text.
        singles = {item["kind"]: {**item, "text": f"{item['text']} v3"} for item in BASE}
        adr.write(
            _facts(
                {"kind": "alt", "text": "alt once more"},
                *({"kind": "premise", "text": f"p{n}"} for n in range(20)),
                **singles,
            )
        )
        single_ids = [f"F-{kind}" for kind in lineage.FACT_SINGLE_KINDS]
        retired = [f"F-alt.{n}" for n in range(1, 15)] + [f"F-conditions.{n}" for n in range(1, 13)]
        assert sorted(adr.fact_core["retired_ids"]) == sorted(retired)
        expected = {
            *retired,  # previous values (last_version 1 or 2)
            *single_ids,  # since 3, and previous values of version 2
            "F-alt.15",
            *(f"F-premise.{n}" for n in range(1, 21)),
        }
        assert len(expected) == 52
        result = explanation_staleness(adr.fact_core, 1)
        assert len(set(result.changed_ids)) == len(result.changed_ids)
        assert set(result.changed_ids) == expected
        assert not result.changed_ids_truncated
        # 31 ids have a previous value, past the view's display cap of 30;
        # changed_ids is derived before that cap.
        view = adr.view()
        assert view.fact_core["previous_values_total"] == 31
        assert len(view.fact_core["previous_values"]) == lineage.VIEW_PREVIOUS_VALUES == 30
        shown = {entry["id"] for entry in view.fact_core["previous_values"]}
        hidden = {entry["id"] for entry in adr.fact_core["previous_values"]} - shown
        assert len(hidden) == 1 and hidden <= set(result.changed_ids)

    def test_the_scan_and_list_caps(self):
        previous = [
            {"id": f"F-alt.{n % 99 + 1}", "last_version": 1, "text": "t", "source": "proposal"}
            for n in range(lineage.SCAN_PREVIOUS_VALUES + 1)
        ]
        items = [{"id": f"F-{k}", "text": "t", "since": 1} for k in lineage.FACT_SINGLE_KINDS]
        items[2]["reversibility"] = "unknown"
        items[4]["confidence"] = "low"
        fact_core = {
            "version": 2,
            "recorded_reversibility": "unknown",
            "items": items,
            "previous_values": previous,
        }
        result = explanation_staleness(fact_core, 1)
        assert result.changed_ids_truncated
        assert len(result.changed_ids) == 99
        ids = [f"F-{kind}.{n}" for kind in lineage.FACT_NUMBERED_KINDS for n in range(1, 100)]
        fact_core["previous_values"] = [
            {"id": fact_id, "last_version": 1, "text": "t"} for fact_id in ids
        ] + [
            {"id": f"F-{kind}", "last_version": 1, "text": "t"}
            for kind in lineage.FACT_SINGLE_KINDS
        ]
        # Every id the grammar allows, once each: the cap is the whole id space.
        capped = explanation_staleness(fact_core, 1)
        assert len(capped.changed_ids) == lineage.MAX_CHANGED_IDS == 302
        assert not capped.changed_ids_truncated

    def test_without_a_readable_fact_core(self):
        assert explanation_staleness(None, 1) == lineage.Staleness(stale=True)
        assert explanation_staleness({"version": 0, "items": []}, 0).stale is False


# ─── The S2 view (S2 spec §2.6) and events (§2.5) ───────────────────────


class TestView:
    def test_the_fact_core_view(self):
        adr = _Lineage()
        adr.write(_facts({"kind": "alt", "text": "a1"}, {"kind": "alt", "text": "a2"}))
        adr.write(
            _facts(
                {"id": "F-alt.1", "text": "a1 changed"}, uncertain={**BASE[4], "confidence": "high"}
            )
        )
        shown = adr.view().as_dict()
        fact_core = shown["fact_core"]
        assert fact_core["version"] == 2 and fact_core["items_total"] == 6
        assert fact_core["items"][4] == {
            "id": "F-uncertain",
            "text": BASE[4]["text"],
            "source": "proposal",
            "since": 2,
            "confidence": "high",
        }
        assert fact_core["items"][2]["reversibility"] == "unknown"
        assert fact_core["retired_ids"] == ["F-alt.2"] and fact_core["retired_ids_total"] == 1
        assert [p["id"] for p in fact_core["previous_values"]] == [
            "F-alt.2",
            "F-alt.1",
            "F-uncertain",
        ]
        assert fact_core["previous_values_total"] == 3
        assert fact_core["recorded_reversibility"] == "unknown"
        assert fact_core["reversibility_changed_outside_tools"] is False
        assert shown["adopted_fact_core_version"] is None
        assert shown["notes"] == []  # nothing skipped, nothing noted
        assert fact_core_notices(adr.view()) == []
        # The S1 shape without include_s2.
        plain = lineage_view(adr.adr, RawLineage(ADR_ID, exists=True, data=adr.doc)).as_dict()
        assert "fact_core" not in plain and "adopted_fact_core_version" not in plain
        assert "fact_core" not in plain["unknown_keys"]

    def test_unknown_keys_inside_the_fact_core_are_named_only(self):
        adr = _Lineage()
        adr.write(_facts())
        adr.write(_facts(weak={"kind": "weak", "text": "w2"}))
        fact_core = adr.doc["fact_core"]
        fact_core["future_section_key"] = {"x": SECRET}
        fact_core["items"][0]["observed_ref"] = SECRET
        fact_core["previous_values"][0]["prev_extra"] = [SECRET]
        view = adr.view()
        shown = view.fact_core
        assert shown["unknown_fields"] == ["future_section_key"]
        assert shown["items"][0]["unknown_fields"] == ["observed_ref"]
        assert "unknown_fields" not in shown["items"][1]
        assert shown["previous_values"][0]["unknown_fields"] == ["prev_extra"]
        assert SECRET not in json.dumps(view.as_dict())
        assert view.notes == [] and "fact_core" not in view.unknown_keys
        # A clean fact core carries no unknown_fields at any level.
        del fact_core["future_section_key"], fact_core["items"][0]["observed_ref"]
        del fact_core["previous_values"][0]["prev_extra"]
        assert "unknown_fields" not in json.dumps(adr.view().as_dict())

    def test_unknown_key_names_share_one_cap_and_are_redacted(self):
        adr = _Lineage()
        adr.write(_facts())
        fact_core = adr.doc["fact_core"]
        fact_core[f"key {SECRET}"] = 1
        for item in fact_core["items"]:
            for n in range(4):
                item[f"extra{n}"] = n
        view = adr.view()
        shown = view.fact_core
        names = [shown.get("unknown_fields", [])] + [
            item.get("unknown_fields", []) for item in shown["items"]
        ]
        assert sum(len(found) for found in names) == lineage.MAX_UNKNOWN_FIELDS
        assert SECRET not in json.dumps(view.as_dict())
        assert names[1] == ["extra0", "extra1", "extra2", "extra3"]
        # 1 + 5 * 4 names, 10 shown; the rest are counted, never shown.
        assert view.notes == [{"code": "decision_lineage_items_skipped", "count": 11}]

    def test_an_empty_key_is_no_fact_core(self):
        adr = _Lineage()
        adr.doc["fact_core"] = None
        view = adr.view()
        assert view.fact_core is None and view.notes == []
        assert fact_core_refusal(adr.doc) is None
        adr.write(_facts())  # a first version: it is written where the key was
        assert adr.fact_core["version"] == 1 and list(adr.doc)[-1] == "fact_core"

    def test_a_derived_view(self):
        view = _Lineage(DecisionStatus.ACCEPTED, started=True).view()
        assert view.derived and view.fact_core is None
        assert view.as_dict()["fact_core"] is None
        assert view.as_dict()["adopted_fact_core_version"] is None
        assert fact_core_notices(view) == []

    def test_caps_on_hand_written_lists(self):
        adr = _Lineage()
        adr.write(_facts())
        fact_core = adr.doc["fact_core"]
        long_item = {"id": "F-alt.1", "text": "x" * 600, "source": "proposal", "since": 1}
        fact_core["items"] = fact_core["items"] + [
            {**long_item, "id": f"F-alt.{n}"} for n in range(1, 36)
        ]
        fact_core["retired_ids"] = ["F-premise.1"] * 400
        fact_core["previous_values"] = [
            {"id": f"F-conditions.{n}", "last_version": 1, "text": "y" * 300} for n in range(1, 41)
        ]
        view = adr.view()
        shown = view.fact_core
        assert len(shown["items"]) == lineage.MAX_FACT_ITEMS and shown["items_total"] == 40
        assert sum(len(item["text"]) for item in shown["items"]) <= lineage.MAX_FACT_CORE_CHARS
        assert all(len(item["text"]) <= lineage.MAX_FACT_TEXT_CHARS for item in shown["items"])
        assert shown["items"][-1]["truncated"] is True
        assert len(shown["retired_ids"]) == 297 and shown["retired_ids_total"] == 400
        assert len(shown["previous_values"]) == 30 and shown["previous_values_total"] == 40
        assert all(len(p["text"]) == lineage.PREVIOUS_TEXT_CHARS for p in shown["previous_values"])
        assert "decision_lineage_items_skipped" in view.note_codes
        assert len(json.dumps(view.as_dict(), ensure_ascii=False)) < 32_000

    def test_an_alias_inflated_file_stays_bounded(self):
        adr = _Lineage()
        adr.write(_facts())
        fact_core = adr.doc["fact_core"]
        element = {"id": "F-alt.1", "last_version": 1, "text": "y" * 300, "source": "proposal"}
        # What safe_load gives for ``*alias`` repeated: one object, many times.
        fact_core["items"] = fact_core["items"] + [fact_core["items"][0]] * 6_000
        fact_core["previous_values"] = [element] * 6_000
        fact_core["retired_ids"] = ["F-alt.1"] * 6_000
        text = dump_lineage(adr.doc, ADR_ID)
        assert len(text.encode()) < lineage.MAX_LINEAGE_BYTES  # small on disk
        data = yaml.safe_load(text)
        view = lineage_view(adr.adr, RawLineage(ADR_ID, exists=True, data=data), include_s2=True)
        shown = view.fact_core
        assert shown["items_total"] == 6_005 and len(shown["items"]) == 5
        assert shown["previous_values_total"] == 6_000 and len(shown["previous_values"]) == 1
        assert shown["retired_ids_total"] == 6_000 and len(shown["retired_ids"]) == 297
        assert "decision_lineage_items_skipped" in view.note_codes
        assert len(json.dumps(view.as_dict(), ensure_ascii=False)) < 32_000
        assert explanation_staleness(data["fact_core"], 1).changed_ids_truncated

    def test_event_fields_of_the_new_types(self):
        adr = _Lineage()
        events = [
            {
                "at": NOW,
                "kind": "fact_core",
                "version": 2,
                "changed_ids": ["F-what", f"F-{SECRET}", 5, "F-alt.1"],
                "edited_outside_tools": "F-revert",
                "reason": "",
                "via": "pm_update_decision",
            },
            {"at": NOW, "kind": "fact_core", "version": True, "changed_ids": ["F-what"] * 400},
            {"at": NOW, "kind": "lifecycle", "to": "adopted", "fact_core_version": 2},
            {"at": NOW, "kind": "lifecycle", "to": "adopted", "fact_core_version": 0},
            {"at": NOW, "kind": "created", "lifecycle": "adopted", "fact_core_version": 1},
        ]
        adr.doc["events"] = events
        view = adr.view()
        first, second, third, fourth, fifth = view.events
        assert first == {
            "at": NOW,
            "kind": "fact_core",
            "via": "pm_update_decision",
            "version": 2,
            "changed_ids": ["F-what", "F-alt.1"],
            "reason": "",
        }
        assert "version" not in second and len(second["changed_ids"]) == lineage.MAX_CHANGED_IDS
        assert third["fact_core_version"] == 2
        assert "fact_core_version" not in fourth
        assert fifth["unknown_fields"] == ["fact_core_version"]
        assert SECRET not in json.dumps(view.as_dict())


# ─── Through the storage layer ──────────────────────────────────────────


def _seed(pm_path: Path, status: DecisionStatus = DecisionStatus.PROPOSED) -> Decision:
    adr = _adr(status)
    _save_decisions(pm_path, [adr, _adr(DecisionStatus.ACCEPTED, OTHER_ID)])
    return adr


def _lineage_path(pm_path: Path) -> Path:
    return pm_path / "decision_lineage" / f"{ADR_ID}.yaml"


class TestThroughStorage:
    def test_a_written_version_reads_back(self, tmp_pm_path: Path):
        adr = _seed(tmp_pm_path)
        result = change_decision_lineage(
            tmp_pm_path,
            ADR_ID,
            LineageChange(
                fact_core=_facts(revert={**BASE[2], "reversibility": Reversibility.UNKNOWN}),
                expected_fact_core_version=0,
            ),
        )
        assert result.status == "updated" and result.error is None
        assert result.events_added == ["lineage_started", "fact_core"]
        raw = read_lineage_raw(tmp_pm_path, ADR_ID)
        assert raw.error is None
        view = lineage_view(adr, raw, include_s2=True)
        assert view.fact_core["version"] == 1
        assert view.fact_core["recorded_reversibility"] == "unknown"

    @pytest.mark.parametrize(
        ("change", "code"),
        [
            (
                LineageChange(fact_core=_facts(), expected_fact_core_version=1),
                "fact_core_version_mismatch",
            ),
            (
                LineageChange(
                    fact_core=_facts(revert={**BASE[2], "reversibility": "code_revertable"}),
                    expected_fact_core_version=2,
                ),
                "fact_core_reversibility_lowering_refused",
            ),
            (
                LineageChange(fact_core=_facts()[1:], expected_fact_core_version=2),
                "fact_core_item_required",
            ),
            (
                LineageChange(fact_core=_facts(), expected_fact_core_version=2, note="n"),
                "fact_core_not_combinable",
            ),
            (
                LineageChange(
                    fact_core=_facts({"id": "F-alt.4", "text": "a"}), expected_fact_core_version=2
                ),
                "fact_core_id_unknown",
            ),
        ],
    )
    def test_errors_leave_the_files_alone(self, tmp_pm_path: Path, change, code: str):
        _seed(tmp_pm_path)
        for version, weak in ((0, BASE[3]), (1, {"kind": "weak", "text": "w2"})):
            step = LineageChange(fact_core=_facts(weak=weak), expected_fact_core_version=version)
            assert change_decision_lineage(tmp_pm_path, ADR_ID, step).status == "updated"
        before = _lineage_path(tmp_pm_path).read_bytes()
        decisions = (tmp_pm_path / "decisions.yaml").read_bytes()
        result = change_decision_lineage(tmp_pm_path, ADR_ID, change)
        assert result.error is not None and result.error["code"] == code
        assert _lineage_path(tmp_pm_path).read_bytes() == before
        assert (tmp_pm_path / "decisions.yaml").read_bytes() == decisions


# ─── Through the tools (S2 spec §3.1, §3.6, §5.2) ───────────────────────

TOOL_ID = "ADR-001"
# The S1 response of pm_update_decision (tests/test_decision_update.py).
_S1_KEYS = {
    "status",
    "decision_id",
    "lifecycle",
    "decision_status",
    "changes",
    "links",
    "events_added",
    "warnings",
}
_W3 = {"kind": "weak", "text": "w3"}
_LOWERING = _facts(revert={**BASE[2], "reversibility": "code_revertable"})


def _record(project: Path, *, status: str = "proposed", decision_kind: str = "technical") -> str:
    # Recording as accepted needs an origin in which the user decided.
    origin = "ai_auto" if status == "proposed" else "ai_proposed_human_decided"
    result = pm_add_decision(
        title="Send the booking mail without the queue",
        context="c",
        decision="d",
        status=status,
        origin=origin,
        recorded_timing="before_impl",
        decision_kind=decision_kind,
        project_path=str(project),
    )
    assert result["status"] == "recorded", result
    return result["decision_id"]


def _update(project: Path, decision_id: str = TOOL_ID, **kwargs: object) -> dict:
    return pm_update_decision(decision_id=decision_id, project_path=str(project), **kwargs)


def _revise(project: Path, items: list[dict], expected: int, **kwargs: object) -> dict:
    return _update(project, fact_core=items, expected_fact_core_version=expected, **kwargs)


def _get(project: Path, decision_id: str = TOOL_ID) -> dict:
    return pm_decision_query(action="get", decision_id=decision_id, project_path=str(project))


def _tool_lineage(project: Path, decision_id: str = TOOL_ID) -> Path:
    return project / ".pm" / "decision_lineage" / f"{decision_id}.yaml"


def _load(project: Path) -> dict:
    return yaml.safe_load(_tool_lineage(project).read_text(encoding="utf-8"))


def _store(project: Path, doc: dict) -> None:
    _tool_lineage(project).write_text(dump_lineage(doc, TOOL_ID), encoding="utf-8")


def _edit(project: Path, change: Callable[[dict], object]) -> None:
    """Change the lineage file by hand, as a person with an editor would."""
    doc = _load(project)
    change(doc)
    _store(project, doc)


def _snapshot(project: Path) -> dict[str, bytes]:
    """Every file under ``.pm`` but the lock files (taking a lock creates them)."""
    root = project / ".pm"
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file() and ".locks" not in path.relative_to(root).parts
    }


def _revised(project: Path) -> None:
    """ADR-001 at fact core version 2; F-alt.1 was retired by version 2."""
    _record(project)
    first = _revise(project, _facts({"kind": "alt", "text": "Keep the queue"}), 0)
    assert first["status"] == "updated", first
    second = _revise(project, _facts(weak={"kind": "weak", "text": "w2"}), 1)
    assert second["status"] == "updated", second


def _adopt(project: Path) -> None:
    result = _update(project, lifecycle="adopted", reason="the user adopted it")
    assert result["status"] == "updated", result


class TestToolArguments:
    @pytest.mark.parametrize(
        ("name", "value"),
        [
            ("lifecycle", "adopted"),
            ("add_links", {"amends": ["ADR-002"]}),
            ("remove_links", {"amends": ["ADR-002"]}),
            ("evaluation", "tests pass"),
            ("note", "a small decision"),
            ("origin", "ai_auto"),
            ("recorded_timing", "post_hoc"),
        ],
    )
    def test_fact_core_is_not_combinable(self, tmp_project: Path, name: str, value: object):
        _revised(tmp_project)
        before = _snapshot(tmp_project)
        result = _revise(tmp_project, _facts(weak=_W3), 2, reason="r", **{name: value})
        assert result["status"] == "error"
        assert result["code"] == "fact_core_not_combinable"
        assert name in result["message"]
        assert _snapshot(tmp_project) == before

    def test_the_arguments_are_checked_before_the_project_is_found(self, tmp_path: Path):
        nowhere = str(tmp_path / "no-project")
        for kwargs, code in (
            ({"fact_core": _facts(), "note": "n"}, "fact_core_not_combinable"),
            ({"fact_core": _facts()}, "fact_core_version_required"),
            (
                {"fact_core": _facts(), "expected_fact_core_version": -1},
                "invalid_fact_core_version",
            ),
            ({"expected_fact_core_version": 0}, "invalid_fact_core_version"),
            (
                {"fact_core": [{"kind": "what"}], "expected_fact_core_version": 0},
                "invalid_fact_core",
            ),
        ):
            result = pm_update_decision(decision_id=TOOL_ID, project_path=nowhere, **kwargs)
            assert result["code"] == code, (kwargs, result)

    def test_a_reason_goes_with_a_revision(self, tmp_project: Path):
        _revised(tmp_project)
        result = _revise(tmp_project, _facts(weak=_W3), 2, reason="the user corrected it")
        assert result["status"] == "updated"
        assert _load(tmp_project)["events"][-1]["reason"] == "the user corrected it"


class TestToolResponses:
    def test_a_fact_core_call_returns_its_version_and_ids(self, tmp_project: Path):
        _record(tmp_project)
        first = _revise(tmp_project, _facts({"kind": "premise", "text": "At most 30 users"}), 0)
        assert set(first) == _S1_KEYS | {"fact_core"}
        assert first["status"] == "updated"
        assert (first["lifecycle"], first["decision_status"]) == ("proposed", "proposed")
        assert first["fact_core"] == {
            "version": 1,
            "ids": ["F-what", "F-change", "F-revert", "F-weak", "F-uncertain", "F-premise.1"],
        }
        assert first["changes"] == {
            "fact_core": {
                "from_version": 0,
                "to_version": 1,
                "changed_ids": first["fact_core"]["ids"],
                "added_ids": first["fact_core"]["ids"],
                "retired_ids": [],
            }
        }
        assert first["events_added"] == ["fact_core"]
        assert first["links"] == {"supersedes": [], "superseded_by": [], "amends": []}
        assert _codes(first["warnings"]) == ["decision_fact_core_no_conditions"]
        assert list(first)[:6] == [
            "status",
            "decision_id",
            "lifecycle",
            "decision_status",
            "changes",
            "fact_core",
        ]
        json.dumps(first)

        # A new numbered item is passed by kind and its id comes back.
        second = _revise(
            tmp_project,
            _facts({"kind": "alt", "text": "Keep the queue"}),
            1,
        )
        assert second["fact_core"]["version"] == 2
        assert second["fact_core"]["ids"][-1] == "F-alt.1"
        assert second["changes"]["fact_core"]["retired_ids"] == ["F-premise.1"]
        assert second["warnings"] == []

    def test_a_fact_core_call_never_rewrites_the_status(self, tmp_project: Path):
        _record(tmp_project)
        decisions = tmp_project / ".pm" / "decisions.yaml"
        before = decisions.read_bytes()
        assert _revise(tmp_project, _facts(), 0)["status"] == "updated"
        assert decisions.read_bytes() == before
        # A status that disagrees with the lineage is reported, not projected.
        _edit(tmp_project, lambda doc: doc.update(lifecycle="adopted"))
        result = _revise(tmp_project, _facts(weak=_W3), 1)
        assert result["status"] == "updated"
        assert result["decision_status"] == "proposed"
        assert "decision_status_mismatch" in _codes(result["warnings"])
        assert decisions.read_bytes() == before

    def test_an_unchanged_call_still_returns_the_fact_core(self, tmp_project: Path):
        _revised(tmp_project)
        before = _snapshot(tmp_project)
        stored = _load(tmp_project)["fact_core"]
        result = _revise(tmp_project, _as_argument(stored), 2)
        assert result["status"] == "unchanged"
        assert set(result) == _S1_KEYS | {"fact_core"}
        assert result["fact_core"] == {"version": 2, "ids": [i["id"] for i in stored["items"]]}
        assert result["changes"]["fact_core"]["from_version"] == 2
        assert result["changes"]["fact_core"]["to_version"] == 2
        assert result["events_added"] == []
        assert _snapshot(tmp_project) == before

    def test_s1_calls_keep_the_s1_keys(self, tmp_project: Path):
        _revised(tmp_project)
        _record(tmp_project)  # ADR-002, a link target
        for kwargs in (
            {"note": "a small decision"},
            {"evaluation": "tests pass", "evaluation_kind": "test"},
            {"add_links": {"amends": ["ADR-002"]}},
            {"lifecycle": "adopted", "reason": "the user adopted it"},
            {},
        ):
            result = _update(tmp_project, **kwargs)
            assert set(result) == _S1_KEYS, (kwargs, result)
            assert "fact_core" not in result["changes"]
        # The adoption fixed version 2 (Q13); the event says so.
        event = _load(tmp_project)["events"][-1]
        assert event["to"] == "adopted" and event["fact_core_version"] == 2

    def test_an_adoption_without_facts_keeps_the_s1_event(self, tmp_project: Path):
        _record(tmp_project)
        result = _update(tmp_project, lifecycle="adopted", reason="the user adopted it")
        assert set(result) == _S1_KEYS
        assert _codes(result["warnings"]) == ["decision_lifecycle_changed"]
        event = _load(tmp_project)["events"][-1]
        assert event == {
            "at": NOW,
            "kind": "lifecycle",
            "from": "proposed",
            "to": "adopted",
            "status": "accepted",
            "reason": "the user adopted it",
            "via": "pm_update_decision",
        }


def _put_back(doc: dict) -> Callable[[dict], None]:
    """A hand edit that restores ``doc``'s fact core."""

    def restore(current: dict) -> None:
        current["fact_core"] = copy.deepcopy(doc["fact_core"])

    return restore


class TestRefusalOrderThroughTheTool:
    """Each check of S2 spec §3.1 is met before the next, and none writes."""

    def _refused(self, project: Path, code: str, items: list[dict], expected: int, **kw) -> dict:
        before = _snapshot(project)
        result = _revise(project, items, expected, **kw)
        assert result.get("code") == code, result
        assert _snapshot(project) == before
        return result

    def test_the_order(self, tmp_project: Path):
        _revised(tmp_project)
        whole = _load(tmp_project)
        lowering_without_what = _LOWERING[1:]

        # Arguments, before the file is read.
        _edit(tmp_project, lambda doc: doc["fact_core"]["items"][0].update(source=b"\xff"))
        self._refused(tmp_project, "fact_core_not_combinable", _LOWERING, 7, note="n")
        # The writer's own refusal (§2.7), before the version and the ratchet.
        self._refused(tmp_project, "decision_lineage_unreadable", lowering_without_what, 7)

        # The fix by adoption, before the version, the items and the ratchet.
        _edit(tmp_project, _put_back(whole))
        _adopt(tmp_project)
        self._refused(tmp_project, "fact_core_after_adoption", lowering_without_what, 7)
        # ... and before the traces of a fact core removed by hand,
        _edit(tmp_project, lambda doc: doc.pop("fact_core"))
        self._refused(tmp_project, "fact_core_after_adoption", _facts(), 0)
        # ... but after the writer's refusal of a broken fact core.
        _edit(tmp_project, _put_back(whole))
        _edit(tmp_project, lambda doc: doc["fact_core"].update(version="2"))
        self._refused(tmp_project, "decision_lineage_unreadable", _facts(weak=_W3), 2)

        # Moved back to proposed: the traces of a removed fact core come next.
        _edit(tmp_project, _put_back(whole))
        assert _update(tmp_project, lifecycle="proposed", reason="the user asked")["status"] == (
            "updated"
        )
        _edit(tmp_project, lambda doc: doc.pop("fact_core"))
        for expected in (0, 2):
            error = self._refused(tmp_project, "decision_lineage_unreadable", _facts(), expected)
            assert "no fact_core" in error["message"]

        # Then the version, the ids and required items, the ratchet, unchanged.
        _edit(tmp_project, _put_back(whole))
        self._refused(tmp_project, "fact_core_version_mismatch", lowering_without_what, 7)
        self._refused(tmp_project, "fact_core_item_required", lowering_without_what, 2)
        self._refused(
            tmp_project, "fact_core_id_unknown", _LOWERING + [{"id": "F-alt.7", "text": "a"}], 2
        )
        self._refused(tmp_project, "fact_core_reversibility_lowering_refused", _LOWERING, 2)
        unchanged = _revise(tmp_project, _as_argument(whole["fact_core"]), 2)
        assert unchanged["status"] == "unchanged"


def _too_many_items() -> list[dict]:
    return _facts(*({"kind": "alt", "text": f"a{n}"} for n in range(26)))


def _too_many_chars() -> list[dict]:
    return _facts(*({"kind": "alt", "text": "a" * 500} for _ in range(12)))


def _hand(change: Callable[[dict], object]) -> Callable[[Path], None]:
    return lambda project: _edit(project, change)


def _adopted_then(change: Callable[[dict], object]) -> Callable[[Path], None]:
    def prepare(project: Path) -> None:
        _adopt(project)
        _edit(project, change)

    return prepare


def _set_event(index: int, **values: object) -> Callable[[dict], None]:
    return lambda doc: doc["events"][index].update(values)


# (id, hand edit before the call, arguments, code). Every code a fact core
# call can return (S2 spec §4), plus the S1 codes it shares.
_TOOL_ERRORS = [
    (
        "source_argument",
        None,
        {"fact_core": _facts({"kind": "alt", "text": "a", "source": "observed"})},
        "invalid_fact_core",
    ),
    ("bad_kind", None, {"fact_core": _facts({"kind": "risk", "text": "a"})}, "invalid_fact_core"),
    ("bad_id", None, {"fact_core": _facts({"id": "F-other", "text": "a"})}, "invalid_fact_core_id"),
    (
        "duplicate",
        None,
        {"fact_core": _facts({"kind": "weak", "text": "again"})},
        "fact_core_id_duplicate",
    ),
    (
        "unknown_id",
        None,
        {"fact_core": _facts({"id": "F-alt.7", "text": "a"})},
        "fact_core_id_unknown",
    ),
    (
        "retired_id",
        None,
        {"fact_core": _facts({"id": "F-alt.1", "text": "a"})},
        "fact_core_id_retired",
    ),
    ("required", None, {"fact_core": _facts()[1:]}, "fact_core_item_required"),
    ("items", None, {"fact_core": _too_many_items()}, "fact_core_too_large"),
    ("chars", None, {"fact_core": _too_many_chars()}, "fact_core_too_large"),
    (
        "exhausted",
        _hand(lambda doc: doc["fact_core"]["retired_ids"].append("F-alt.99")),
        {"fact_core": _facts({"kind": "alt", "text": "a"})},
        "fact_core_ids_exhausted",
    ),
    ("no_version", None, {"expected_fact_core_version": None}, "fact_core_version_required"),
    ("negative", None, {"expected_fact_core_version": -1}, "invalid_fact_core_version"),
    ("version_alone", None, {"fact_core": None}, "invalid_fact_core_version"),
    ("mismatch", None, {"expected_fact_core_version": 1}, "fact_core_version_mismatch"),
    ("combined", None, {"note": "n"}, "fact_core_not_combinable"),
    ("lowering", None, {"fact_core": _LOWERING}, "fact_core_reversibility_lowering_refused"),
    ("after_adoption", _adopted_then(lambda doc: None), {}, "fact_core_after_adoption"),
    ("item_text", None, {"fact_core": _facts({"kind": "alt", "text": "a" * 501})}, "text_too_long"),
    ("reason", None, {"reason": "r" * 4_001}, "text_too_long"),
    (
        "broken_item",
        _hand(lambda doc: doc["fact_core"]["items"][0].update(source=SECRET.encode())),
        {},
        "decision_lineage_unreadable",
    ),
    (
        "broken_version",
        _hand(lambda doc: doc["fact_core"].update(version=True)),
        {},
        "decision_lineage_unreadable",
    ),
    (
        "broken_adopted_version",
        _adopted_then(_set_event(-1, fact_core_version="2")),
        {},
        "decision_lineage_unreadable",
    ),
    (
        "removed_by_hand",
        _hand(lambda doc: doc.pop("fact_core")),
        {"expected_fact_core_version": 0},
        "decision_lineage_unreadable",
    ),
    (
        "too_large",
        _hand(lambda doc: doc.update(padding="x" * 245_000)),
        {},
        "decision_lineage_too_large",
    ),
    # The S1 writer refusals come before every fact core step (S2 spec §3.1).
    ("schema", _hand(lambda doc: doc.update(schema=2)), {}, "decision_lineage_schema_unsupported"),
    (
        "lifecycle_unknown",
        _hand(lambda doc: doc.update(lifecycle="weird")),
        {},
        "decision_lineage_lifecycle_unknown",
    ),
    (
        "anchor_mismatch",
        _hand(lambda doc: doc["anchor"].update(title_sha256="0" * 64)),
        {},
        "decision_lineage_anchor_mismatch",
    ),
    ("invalid_id", None, {"decision_id": "ADR-x"}, "invalid_decision_id"),
    ("not_found", None, {"decision_id": "ADR-777"}, "decision_not_found"),
]


class TestToolErrorsLeaveTheFilesAlone:
    @pytest.mark.parametrize(
        ("prepare", "kwargs", "code"),
        [
            pytest.param(prepare, kwargs, code, id=name)
            for name, prepare, kwargs, code in _TOOL_ERRORS
        ],
    )
    def test_nothing_is_written(
        self,
        tmp_project: Path,
        prepare: Callable[[Path], None] | None,
        kwargs: dict,
        code: str,
    ):
        _revised(tmp_project)
        if prepare is not None:
            prepare(tmp_project)
        before = _snapshot(tmp_project)
        arguments = {
            "fact_core": _facts(weak=_W3),
            "expected_fact_core_version": 2,
            **kwargs,
        }
        result = _update(tmp_project, **arguments)
        assert result["status"] == "error", result
        assert result["code"] == code, result
        assert result["message"]
        if code == "text_too_long":
            assert result["field"] in ("fact_core", "reason")  # S2 spec §4
        assert SECRET not in json.dumps(result)
        assert _snapshot(tmp_project) == before

    def test_every_code_of_the_table_is_reached(self):
        codes = {code for *_, code in _TOOL_ERRORS}
        assert {
            "invalid_fact_core",
            "invalid_fact_core_id",
            "fact_core_id_duplicate",
            "fact_core_id_unknown",
            "fact_core_id_retired",
            "fact_core_item_required",
            "fact_core_too_large",
            "fact_core_ids_exhausted",
            "fact_core_version_required",
            "invalid_fact_core_version",
            "fact_core_version_mismatch",
            "fact_core_not_combinable",
            "fact_core_reversibility_lowering_refused",
            "fact_core_after_adoption",
            "text_too_long",
            "decision_lineage_unreadable",
            "decision_lineage_schema_unsupported",
            "decision_lineage_lifecycle_unknown",
            "decision_lineage_anchor_mismatch",
            "decision_lineage_too_large",
            "invalid_decision_id",
            "decision_not_found",
        } <= codes

    def test_a_revision_refused_near_the_cap_is_told_to_trim_first(self, tmp_project: Path):
        # The append cap stops fact core versions too (S2 spec §2.8); the
        # remediation must not offer only advice for notes and evaluations.
        _revised(tmp_project)
        _edit(tmp_project, lambda doc: doc.update(padding="x" * 245_000))
        result = _revise(tmp_project, _facts(weak=_W3), 2)
        assert result["code"] == "decision_lineage_too_large"
        remediation = result["remediation"]
        assert "without fact_core" in remediation
        assert "a new fact core version" in remediation
        assert "keeping its lifecycle, link, declared and fact_core events" in remediation
        assert "then make the call again" in remediation

    def test_the_first_version_expects_zero(self, tmp_project: Path):
        _record(tmp_project)
        before = _snapshot(tmp_project)
        result = _revise(tmp_project, _facts(), 1)
        assert (result["code"], result["current_version"]) == ("fact_core_version_mismatch", 0)
        assert _snapshot(tmp_project) == before


class TestConcurrentRevisions:
    def test_only_one_of_two_revisions_from_the_same_version_is_written(
        self, tmp_project: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setenv("PM_LOCK_TIMEOUT_S", "30")
        _record(tmp_project)
        assert _revise(tmp_project, _facts(), 0)["status"] == "updated"
        barrier = threading.Barrier(2)
        results: dict[int, dict] = {}

        def revise(n: int) -> None:
            barrier.wait()
            results[n] = _revise(tmp_project, _facts(weak={"kind": "weak", "text": f"w-{n}"}), 1)

        threads = [threading.Thread(target=revise, args=(n,)) for n in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(60)
        assert sorted(result["status"] for result in results.values()) == ["error", "updated"]
        [winner] = [n for n, result in results.items() if result["status"] == "updated"]
        refused = results[1 - winner]
        assert refused["code"] == "fact_core_version_mismatch"
        assert refused["current_version"] == 2
        doc = _load(tmp_project)
        assert doc["fact_core"]["version"] == 2
        assert [event["kind"] for event in doc["events"]].count("fact_core") == 2
        weak = next(item for item in doc["fact_core"]["items"] if item["id"] == "F-weak")
        assert weak["text"] == f"w-{winner}"


class TestS1WritersThroughTheTool:
    @pytest.mark.parametrize(
        "breakage",
        [
            lambda doc: doc["fact_core"]["items"][0].update(source=SECRET.encode()),
            lambda doc: doc["fact_core"].update(version=f"1 {SECRET}"),
            lambda doc: doc["fact_core"]["items"][2].pop("reversibility"),
        ],
        ids=["bytes_source", "version", "no_reversibility"],
    )
    def test_a_broken_fact_core_stops_no_s1_call(
        self, tmp_project: Path, breakage: Callable[[dict], object]
    ):
        _revised(tmp_project)
        _record(tmp_project)  # ADR-002, a link target
        _edit(tmp_project, breakage)
        section = _load(tmp_project)["fact_core"]
        for kwargs in (
            {"note": "a small decision"},
            {"evaluation": "tests pass", "evaluation_kind": "test"},
            {"add_links": {"amends": ["ADR-002"]}},
            {"lifecycle": "adopted", "reason": "the user adopted it"},
        ):
            result = _update(tmp_project, **kwargs)
            assert result["status"] == "updated", (kwargs, result)
            assert set(result) == _S1_KEYS
            assert SECRET not in json.dumps(result)
            assert _load(tmp_project)["fact_core"] == section
        skipped = next(
            w
            for w in result["warnings"]
            if w["code"] == "decision_adoption_explanation_check_skipped"
        )
        assert skipped["level"] == "info" and "not fixed" in skipped["message"]
        event = _load(tmp_project)["events"][-1]
        assert event["to"] == "adopted" and "fact_core_version" not in event
        # Only the fact core writer refuses it.
        before = _snapshot(tmp_project)
        refused = _revise(tmp_project, _facts(weak=_W3), 2)
        assert refused["code"] == "decision_lineage_unreadable"
        assert SECRET not in json.dumps(refused)
        assert _snapshot(tmp_project) == before


class TestRedactionThroughTheTools:
    def test_secrets_in_the_facts_and_the_reason_are_never_saved_or_returned(
        self, tmp_project: Path
    ):
        _record(tmp_project)
        items = _facts(what={"kind": "what", "text": f"Use the key {SECRET}"})
        result = _revise(tmp_project, items, 0, reason=f"pasted {SECRET}")
        assert result["status"] == "updated"
        assert SECRET not in json.dumps(result)
        warning = next(
            w for w in result["warnings"] if w["code"] == "decision_lineage_secrets_redacted"
        )
        assert warning["level"] == "warning"
        assert warning["message"].startswith("2 secret-like string(s)")
        assert SECRET not in _tool_lineage(tmp_project).read_text(encoding="utf-8")
        got = _get(tmp_project)
        assert SECRET not in json.dumps(got)
        assert got["lineage"]["fact_core"]["items"][0]["text"] == "Use the key <REDACTED:secret>"

    def test_hand_written_secrets_are_copied_redacted_and_source_becomes_proposal(
        self, tmp_project: Path
    ):
        _record(tmp_project)
        _revise(tmp_project, _facts(), 0)

        def plant(doc: dict) -> None:
            items = doc["fact_core"]["items"]
            items[0]["source"] = f"observed {SECRET}"
            items[1]["text"] = f"Before: the queue {SECRET}"
            items[2]["reversibility"] = f"unknown {SECRET}"
            items[4]["confidence"] = f"medium {SECRET}"

        _edit(tmp_project, plant)
        got = _get(tmp_project)
        assert SECRET not in json.dumps(got)
        assert "decision_text_secrets_redacted" in _codes(got["warnings"])
        # Hand back what get shows, with vocabulary values where the file has none.
        argument = _as_argument(got["lineage"]["fact_core"])
        argument[2]["reversibility"] = "unknown"
        argument[4]["confidence"] = "medium"
        result = _revise(tmp_project, argument, 1)
        assert result["status"] == "updated"
        assert SECRET not in json.dumps(result)
        redacted = next(
            w for w in result["warnings"] if w["code"] == "decision_lineage_secrets_redacted"
        )
        # One per planted secret, F-what's dropped source included (S2 spec §3.1 step 7).
        assert redacted["message"].startswith("4 secret-like string(s)")
        # The text that came back redacted is no change; F-what's source is never compared.
        assert sorted(result["changes"]["fact_core"]["changed_ids"]) == ["F-revert", "F-uncertain"]
        assert SECRET not in _tool_lineage(tmp_project).read_text(encoding="utf-8")
        fact_core = _load(tmp_project)["fact_core"]
        assert {item["source"] for item in fact_core["items"]} == {"proposal"}
        assert fact_core["items"][1]["text"] == "Before: the queue <REDACTED:secret>"
        previous = {
            (p["id"], p.get("reversibility"), p.get("confidence"))
            for p in fact_core["previous_values"]
        }
        assert ("F-revert", "unknown <REDACTED:secret>", None) in previous
        assert ("F-uncertain", None, "medium <REDACTED:secret>") in previous
        assert SECRET not in json.dumps(_get(tmp_project))

    @pytest.mark.parametrize("label", ["RSA PRIVATE KEY", "OPENSSH PRIVATE KEY"])
    def test_a_private_key_in_a_fact_is_removed_with_its_body(self, tmp_project: Path, label: str):
        # D11 for the fact core (S2 spec §6.1): a key's body must go with its
        # BEGIN line, as catalog v3 does for S1 fields.
        body = "\n".join(["Zm9v" * 16] * 5)
        key = f"-----BEGIN {label}-----\n{body}\n-----END {label}-----"
        _record(tmp_project)
        items = _facts(weak={"kind": "weak", "text": f"deploy key:\n{key}\nrotate it"})
        result = _revise(tmp_project, items, 0)
        assert result["status"] == "updated", result
        assert "decision_lineage_secrets_redacted" in _codes(result["warnings"])
        written = _tool_lineage(tmp_project).read_text(encoding="utf-8")
        weak = _load(tmp_project)["fact_core"]["items"][3]
        assert weak["text"] == "deploy key:\n<REDACTED:secret>\nrotate it"
        got = _get(tmp_project)
        for shown in (written, json.dumps(result), json.dumps(got)):
            assert "Zm9v" not in shown and "PRIVATE KEY" not in shown

    def test_a_hand_written_private_key_is_never_shown_or_carried_over(self, tmp_project: Path):
        body = "\n".join(["YmFy" * 16] * 5)
        key = f"-----BEGIN RSA PRIVATE KEY-----\n{body}\n-----END RSA PRIVATE KEY-----"
        _record(tmp_project)
        _revise(tmp_project, _facts(), 0)

        def plant(doc: dict) -> None:
            doc["fact_core"]["items"][1]["text"] = f"Before: {key}"
            doc["fact_core"]["items"][0]["source"] = key

        _edit(tmp_project, plant)
        got = _get(tmp_project)
        assert "YmFy" not in json.dumps(got) and "PRIVATE KEY" not in json.dumps(got)
        result = _revise(tmp_project, _facts(weak=_W3), 1)
        assert result["status"] == "updated", result
        written = _tool_lineage(tmp_project).read_text(encoding="utf-8")
        for shown in (written, json.dumps(result), json.dumps(_get(tmp_project))):
            assert "YmFy" not in shown and "PRIVATE KEY" not in shown

    def test_a_hand_placed_id_with_a_secret_is_never_echoed(self, tmp_project: Path):
        _record(tmp_project)
        _revise(tmp_project, _facts(), 0)
        _edit(
            tmp_project,
            lambda doc: doc["fact_core"]["items"].append(
                {"id": f"F-alt.{SECRET}", "text": "a", "source": "proposal", "since": 1}
            ),
        )
        before = _snapshot(tmp_project)
        result = _revise(tmp_project, _facts(weak=_W3), 1)
        assert result["code"] == "decision_lineage_unreadable"
        assert SECRET not in json.dumps(result)
        assert _snapshot(tmp_project) == before
        assert SECRET not in json.dumps(_get(tmp_project))


class TestGetThroughTheTool:
    def test_the_fact_core_and_the_adopted_version(self, tmp_project: Path):
        _record(tmp_project)
        _revise(tmp_project, _facts({"kind": "conditions", "text": "One mail server"}), 0)
        got = _get(tmp_project)
        fact_core = got["lineage"]["fact_core"]
        assert fact_core["version"] == 1
        assert [item["id"] for item in fact_core["items"]] == [
            "F-what",
            "F-change",
            "F-revert",
            "F-weak",
            "F-uncertain",
            "F-conditions.1",
        ]
        assert fact_core["items"][2]["reversibility"] == "unknown"
        assert fact_core["recorded_reversibility"] == "unknown"
        assert fact_core["reversibility_changed_outside_tools"] is False
        assert got["lineage"]["adopted_fact_core_version"] is None
        assert got["warnings"] == []

        _adopt(tmp_project)
        got = _get(tmp_project)
        assert got["lineage"]["adopted_fact_core_version"] == 1
        assert got["lineage"]["events"][-1]["fact_core_version"] == 1
        assert got["warnings"] == []

        # Changed by hand after the adoption.
        _edit(tmp_project, lambda doc: doc["fact_core"].update(version=2))
        got = _get(tmp_project)
        assert _codes(got["warnings"]) == ["decision_fact_core_changed_since_adoption"]
        # A broken adopted version is not shown and is counted.
        _edit(tmp_project, _set_event(-1, fact_core_version="1"))
        got = _get(tmp_project)
        assert got["lineage"]["adopted_fact_core_version"] is None
        assert "decision_lineage_items_skipped" in {n["code"] for n in got["lineage"]["notes"]}

    def test_facts_written_after_an_adoption_are_marked(self, tmp_project: Path):
        _record(tmp_project, status="accepted")
        result = _revise(tmp_project, _facts(), 0)
        assert "decision_fact_core_changed_after_adoption" in _codes(result["warnings"])
        got = _get(tmp_project)
        assert got["lineage"]["adopted_fact_core_version"] is None
        assert "decision_fact_core_written_after_adoption" in _codes(got["warnings"])

    def test_a_hand_lowered_reversibility_is_shown_until_a_revision_takes_it_in(
        self, tmp_project: Path
    ):
        _record(tmp_project)
        _revise(tmp_project, _facts(), 0)
        _edit(
            tmp_project,
            lambda doc: doc["fact_core"]["items"][2].update(reversibility="code_revertable"),
        )
        for _ in range(2):  # every time
            got = _get(tmp_project)
            [warning] = [
                w
                for w in got["warnings"]
                if w["code"] == "decision_reversibility_changed_outside_tools"
            ]
            assert warning["level"] == "info"
            assert "recorded unknown, now code_revertable" in warning["message"]
        assert got["lineage"]["fact_core"]["reversibility_changed_outside_tools"] is True
        # The same items handed back take the change into a new version.
        result = _revise(tmp_project, _as_argument(got["lineage"]["fact_core"]), 1)
        assert result["status"] == "updated"
        assert result["changes"]["fact_core"]["changed_ids"] == ["F-revert"]
        assert result["changes"]["fact_core"]["edited_outside_tools"] == ["F-revert"]
        got = _get(tmp_project)
        assert "decision_reversibility_changed_outside_tools" not in _codes(got["warnings"])
        assert got["lineage"]["fact_core"]["recorded_reversibility"] == "code_revertable"

    def test_the_s2_sections_stay_within_the_character_budget(self, tmp_project: Path):
        _record(tmp_project)
        _revise(tmp_project, _facts(), 0)

        def inflate(doc: dict) -> None:
            fact_core = doc["fact_core"]
            # Each control character is escaped to six in JSON.
            fact_core["previous_values"] = [
                {"id": f"F-alt.{n}", "last_version": 1, "text": "\x01" * 200, "source": "p"}
                for n in range(1, 31)
            ]
            fact_core["items"][0]["text"] = "日本語の事実" * 80  # counted as characters

        _edit(tmp_project, inflate)
        got = _get(tmp_project)
        lineage_part = got["lineage"]
        sections = {key: lineage_part[key] for key in ("fact_core", "adopted_fact_core_version")}
        assert len(json.dumps(sections, ensure_ascii=False)) <= 32_000
        [warning] = [w for w in got["warnings"] if w["code"] == "decision_get_truncated"]
        assert warning["level"] == "info" and "previous value(s)" in warning["message"]
        shown = lineage_part["fact_core"]["previous_values"]
        assert 0 < len(shown) < 30
        # The newest are kept; the items are never cut.
        assert [entry["id"] for entry in shown] == [
            f"F-alt.{n}" for n in range(30, 30 - len(shown), -1)
        ]
        assert lineage_part["fact_core"]["previous_values_total"] == 30
        assert len(lineage_part["fact_core"]["items"]) == 5
        assert lineage_part["fact_core"]["items"][0]["text"] == ("日本語の事実" * 80)[:500]

    def test_an_alias_inflated_fact_core_stays_bounded(self, tmp_project: Path):
        _record(tmp_project)
        _revise(tmp_project, _facts(), 0)
        previous = {"id": "F-alt.1", "last_version": 1, "text": "x" * 200, "source": "proposal"}

        def inflate(doc: dict) -> None:
            doc["fact_core"]["items"] = doc["fact_core"]["items"] * 400  # dict aliases
            doc["fact_core"]["previous_values"] = [previous] * 6_000
            doc["fact_core"]["retired_ids"] = ["F-alt.1"] * 6_000

        _edit(tmp_project, inflate)
        assert _tool_lineage(tmp_project).stat().st_size < lineage.MAX_LINEAGE_BYTES
        got = _get(tmp_project)
        text = json.dumps(got, ensure_ascii=False)
        assert len(text) <= 64_000
        fact_core = got["lineage"]["fact_core"]
        assert len(json.dumps(fact_core, ensure_ascii=False)) <= 32_000
        assert len(fact_core["items"]) <= lineage.MAX_FACT_ITEMS
        assert fact_core["items_total"] == 2_000
        assert fact_core["previous_values_total"] == 6_000
        assert len(fact_core["retired_ids"]) <= lineage.VIEW_RETIRED_IDS
        assert fact_core["retired_ids_total"] == 6_000
        assert "decision_lineage_items_skipped" in {n["code"] for n in got["lineage"]["notes"]}

    def test_the_docstrings_name_the_fact_core(self):
        query = " ".join((pm_decision_query.__doc__ or "").split())
        assert "recent events and the fact core" in query
        update = " ".join((pm_update_decision.__doc__ or "").split())
        for phrase in (
            "never infer a premise",
            "never invent one",
            "when unsure, write unknown, even in the first version",
            "this tool never lowers it to code_revertable",
            "never edit lineage files yourself",
            "It cannot be combined with any other change but reason",
            "its fact_core is fixed until the ADR is moved back to proposed",
        ):
            assert phrase in update, phrase


# ─── Sizes as written (S2 spec §2.8) ────────────────────────────────────


_PAD_LINE = "p" * 60


class TestSizesAsWritten:
    def test_newlines_count_as_the_platform_writes_them(self, monkeypatch: pytest.MonkeyPatch):
        text = "a: 1\nb: 日本\n"
        lf = len(text.encode("utf-8"))
        assert storage.written_size(text) == lf
        monkeypatch.setattr(os, "linesep", "\r\n")
        assert storage.written_size(text) == lf + 2
        assert storage.written_size("ascii\n") == 7

    def test_the_capped_dump_counts_crlf(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(os, "linesep", "\r\n")
        adr = _adr()
        doc = _doc(adr)
        text = dump_lineage(doc, adr.id)
        size = len(text.encode("utf-8")) + text.count("\n")
        assert storage._dump_yaml_capped(doc, lineage.lineage_header_name(adr.id), size) == text
        assert storage._dump_lineage_capped(doc, adr.id, size) == text
        assert storage._dump_lineage_capped(doc, adr.id, size - 1) is None

    @pytest.mark.parametrize(
        ("change", "cap"),
        [
            (
                LineageChange(lifecycle="adopted", reason="the user adopted it"),
                lineage.MAX_LINEAGE_BYTES,
            ),
            (
                LineageChange(fact_core=_facts(), expected_fact_core_version=0),
                lineage.MAX_APPEND_BYTES,
            ),
        ],
        ids=["lifecycle", "fact_core"],
    )
    def test_a_lineage_accepted_near_the_cap_reads_back(
        self,
        tmp_pm_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        change: LineageChange,
        cap: int,
    ):
        adr = _seed(tmp_pm_path)
        path = _lineage_path(tmp_pm_path)
        path.parent.mkdir(parents=True, exist_ok=True)

        def padded(lines: int) -> dict:
            return _doc(adr, padding=[_PAD_LINE] * lines)

        def written(lines: int) -> tuple[int, int]:
            outcome = apply_change(padded(lines), adr, change, [ADR_ID, OTHER_ID], NOW)
            text = dump_lineage(outcome.doc, ADR_ID)
            lf = len(text.encode("utf-8"))
            return lf, lf + text.count("\n")

        base = 1_000
        lf_base, crlf_base = written(base)
        lf_step = written(base + 1)[0] - lf_base
        crlf_step = written(base + 1)[1] - crlf_base
        # The most lines whose LF text fits: what counting LF bytes accepted.
        lf_fits = base + (cap - lf_base) // lf_step
        # The most lines whose text as written with CRLF fits.
        crlf_fits = base + (cap - crlf_base) // crlf_step
        lf, crlf = written(lf_fits)
        assert lf <= cap < crlf
        lf, crlf = written(crlf_fits)
        assert cap - crlf_step < crlf <= cap

        monkeypatch.setattr(os, "linesep", "\r\n")
        # Refused: counted in LF bytes it would have fitted, but not as written.
        path.write_bytes(dump_lineage(padded(lf_fits), ADR_ID).encode("utf-8"))
        before = path.read_bytes()
        refused = change_decision_lineage(tmp_pm_path, ADR_ID, change)
        assert refused.error is not None
        assert refused.error["code"] == "decision_lineage_too_large"
        assert path.read_bytes() == before
        # Accepted just below the cap as written, and read back.
        path.write_bytes(dump_lineage(padded(crlf_fits), ADR_ID).encode("utf-8"))
        accepted = change_decision_lineage(tmp_pm_path, ADR_ID, change)
        assert accepted.status == "updated", accepted.error
        data = path.read_bytes()
        assert b"\r\n" in data
        assert cap - crlf_step < len(data) <= cap
        raw = read_lineage_raw(tmp_pm_path, ADR_ID)
        assert raw.error is None and raw.data["padding"] == [_PAD_LINE] * crlf_fits
