"""Decision Lineage S2: explanations, from the pure layer to the tool (PMSERV-230).

What this file pins, by the S2 specification's numbering
(docs/issues/DESIGN_decision-lineage-s2.md):

* The question kinds of an explanation (§2.9).
* The checks of one explanation (§2.3 checks 1-7, D4): shapes, counts and
  lengths; every current fact leading a paragraph that is not only a mermaid
  block; unknown and retired ids; both lists returned together; the
  question's kind, ids and premise. Only ``fact_ids_present`` is stored and
  the accuracy is always shown as unconfirmed (verification #7).
* The lineage-only writer (§3.2, §5.1): the version match, numbering, the
  writer's refusals of §2.7, a missing lineage or fact core, the anchor, a
  symlinked lineage, the append cap counted in bytes as written (§2.8),
  copy-on-write with sections shared through YAML aliases, D11 for
  explanations, only the lineage lock taken (finishing while another thread
  holds the decisions lock), and the lifecycle and decisions.yaml unchanged.
* Stale explanations (§2.3, D6 moved from 228): ``stale`` and
  ``changed_ids`` in the view, ``decision_explanations_stale`` on a
  revision, a change of F-revert outside the tools, the scan cap.
* The S2 view of the explanations (§2.6, §2.7): rows, the one body, the caps,
  broken sections and entries read as the adoption's check reads them.
* The tool (§3.2, §3.6): pm_add_explanation's response and error shapes, its
  argument checks before the project is resolved, its docstring as the spec
  words it; get's explanations, its notice and its character budget.
"""

from __future__ import annotations

import contextlib
import copy
import json
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
import yaml

from pmlens import lineage, server, storage
from pmlens.lineage import (
    LineageChange,
    add_explanation_doc,
    anchor_for,
    dump_lineage,
    explanation_argument_error,
    explanation_refusal,
    lineage_view,
    read_lineage_raw,
)
from pmlens.models import Decision, DecisionStatus
from pmlens.server import pm_add_explanation, pm_decision_query, pm_update_decision
from pmlens.storage import _save_decisions, change_decision_lineage, record_lineage_explanation

NOW = "2026-10-30T01:00:00Z"
SECRET = "AKIA" + "Z" * 16
ADR_ID = "ADR-900"
OTHER_ID = "ADR-901"
CORE_IDS = ["F-what", "F-change", "F-revert", "F-weak", "F-uncertain"]
STALE = "decision_explanations_stale"
SKIPPED = "decision_lineage_items_skipped"

BASE: list[dict] = [
    {"kind": "what", "text": "Send the booking mail without the queue"},
    {"kind": "change", "text": "Before: through the queue. After: inside the request"},
    {"kind": "revert", "text": "Revert the setting and deploy again", "reversibility": "unknown"},
    {"kind": "weak", "text": "A slow mail server slows every booking"},
    {"kind": "uncertain", "text": "Peak volume was not measured", "confidence": "medium"},
]
MERMAID = "```mermaid\ngraph TD\n  A-->B\n```"


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


def _facts(*extra: dict, **replace: dict) -> list[dict]:
    """BASE with items replaced by kind (``weak={...}``) and ``extra`` appended."""
    items = [dict(replace.get(item["kind"], item)) for item in BASE]
    return items + [dict(item) for item in extra]


def _covering(ids: list[str], text: str = "about it") -> list[dict]:
    """One paragraph leading with each id."""
    return [{"fact_ids": [fact_id], "text": f"{text} {fact_id}"} for fact_id in ids]


def _fact_core(ids: list[str] = CORE_IDS, version: int = 1) -> dict:
    """A stored fact core holding ``ids`` (as the writer stores it)."""
    items = []
    for fact_id in ids:
        item = {"id": fact_id, "text": f"text {fact_id}", "source": "proposal", "since": 1}
        if fact_id == "F-revert":
            item["reversibility"] = "unknown"
        if fact_id == "F-uncertain":
            item["confidence"] = "medium"
        items.append(item)
    return {
        "version": version,
        "recorded_reversibility": "unknown",
        "items": items,
        "retired_ids": [],
        "previous_values": [],
    }


def _codes(entries: list[dict]) -> list[str]:
    return [entry["code"] for entry in entries]


# ─── Storage helpers ────────────────────────────────────────────────────


def _seed(pm_path: Path) -> Decision:
    adr = _adr()
    _save_decisions(pm_path, [adr, _adr(DecisionStatus.ACCEPTED, OTHER_ID)])
    return adr


def _path(pm_path: Path) -> Path:
    return pm_path / "decision_lineage" / f"{ADR_ID}.yaml"


def _revise(pm_path: Path, items: list[dict], expected: int) -> storage.LineageChangeResult:
    result = change_decision_lineage(
        pm_path,
        ADR_ID,
        LineageChange(fact_core=items, expected_fact_core_version=expected),
    )
    assert result.status == "updated", result.error
    return result


def _explain(
    pm_path: Path,
    version: object = 1,
    paragraphs: object = None,
    question: object = None,
    decision_id: str = ADR_ID,
) -> storage.LineageAppendResult:
    return record_lineage_explanation(
        pm_path,
        decision_id,
        fact_core_version=version,
        paragraphs=_covering(CORE_IDS) if paragraphs is None else paragraphs,
        question=question,
    )


def _recorded(pm_path: Path, version: int = 1, **kwargs: object) -> storage.LineageAppendResult:
    result = _explain(pm_path, version, **kwargs)
    assert result.status == "recorded", result.error
    return result


def _load(pm_path: Path) -> dict:
    return yaml.safe_load(_path(pm_path).read_text(encoding="utf-8"))


def _store(pm_path: Path, doc: dict | str) -> None:
    text = doc if isinstance(doc, str) else dump_lineage(doc, ADR_ID)
    _path(pm_path).write_text(text, encoding="utf-8")


def _snapshot(pm_path: Path) -> dict[str, bytes]:
    """Every file under ``.pm`` but the lock files (taking a lock creates them)."""
    return {
        str(path.relative_to(pm_path)): path.read_bytes()
        for path in sorted(pm_path.rglob("*"))
        if path.is_file() and ".locks" not in path.relative_to(pm_path).parts
    }


def _view(pm_path: Path, adr: Decision) -> lineage.LineageView:
    return lineage_view(adr, read_lineage_raw(pm_path, ADR_ID), include_s2=True)


def _at_version_one(pm_path: Path) -> Decision:
    adr = _seed(pm_path)
    _revise(pm_path, _facts(), 0)
    return adr


# ─── Vocabulary (S2 spec §2.9) ──────────────────────────────────────────


def test_the_question_kinds_of_an_explanation():
    assert lineage.QUESTION_KINDS == (
        "judgment",
        "premise_check",
        "understanding_check",
        "tech_review",
    )
    assert "judgeable_kinds_question" not in lineage.QUESTION_KINDS


# ─── Checks 1-3 and the question's shape (S2 spec §2.3) ─────────────────


def _shape(paragraphs: object, question: object = None) -> dict | None:
    return explanation_argument_error(paragraphs, question)


class TestShape:
    @pytest.mark.parametrize(
        "paragraphs",
        [
            None,
            "text",
            [],
            [SECRET],
            [{"fact_ids": ["F-what"]}],
            [{"text": "t"}],
            [{"fact_ids": ["F-what"], "text": "t", SECRET: 1}],
            [{"fact_ids": ["F-what"], "text": 5}],
        ],
    )
    def test_invalid_paragraphs(self, paragraphs: object):
        error = _shape(paragraphs)
        assert error["code"] == "invalid_paragraphs"
        assert SECRET not in json.dumps(error)

    def test_more_than_forty_paragraphs_are_too_large(self):
        error = _shape(_covering(["F-what"]) * (lineage.MAX_PARAGRAPHS + 1))
        assert error["code"] == "explanation_too_large"
        assert _shape(_covering(["F-what"]) * lineage.MAX_PARAGRAPHS) is None

    @pytest.mark.parametrize(
        "fact_ids",
        [[], ["F-what", "F-weak", "F-change"], ["F-what", "F-what"], ["F-other"], "F-what", [5]],
    )
    def test_invalid_fact_ids(self, fact_ids: object):
        paragraphs = [{"fact_ids": ["F-what"], "text": "t"}, {"fact_ids": fact_ids, "text": "t"}]
        error = _shape(paragraphs)
        assert error["code"] == "invalid_fact_ids"
        assert error["paragraph_index"] == 1

    def test_two_fact_ids_are_accepted(self):
        assert _shape([{"fact_ids": ["F-what", "F-weak"], "text": "t"}]) is None

    def test_a_blank_paragraph(self):
        error = _shape([{"fact_ids": ["F-what"], "text": " \n\t"}])
        assert error["code"] == "empty_paragraph" and error["paragraph_index"] == 0

    def test_lengths(self):
        long = "x" * (lineage.MAX_PARAGRAPH_CHARS + 1)
        error = _shape([{"fact_ids": ["F-what"], "text": long}])
        assert error["code"] == "text_too_long" and error["field"] == "paragraphs"
        full = "x" * lineage.MAX_PARAGRAPH_CHARS
        fits = lineage.MAX_EXPLANATION_CHARS // lineage.MAX_PARAGRAPH_CHARS
        assert _shape([{"fact_ids": ["F-what"], "text": full}] * fits) is None
        error = _shape([{"fact_ids": ["F-what"], "text": full}] * (fits + 1))
        assert error["code"] == "explanation_too_large"

    @pytest.mark.parametrize(
        "question",
        [
            SECRET,
            {"question_kind": "judgment", "fact_ids": ["F-what"]},
            {"question_kind": "judgment", "fact_ids": ["F-what"], "text": "q", "extra": 1},
            {"question_kind": "judgeable_kinds_question", "fact_ids": ["F-what"], "text": "q"},
            {"question_kind": SECRET, "fact_ids": ["F-what"], "text": "q"},
            {"question_kind": "judgment", "fact_ids": [], "text": "q"},
            {
                "question_kind": "judgment",
                "fact_ids": ["F-what", "F-weak", "F-change"],
                "text": "q",
            },
            {"question_kind": "judgment", "fact_ids": ["F-what"], "text": "  "},
            {"question_kind": "judgment", "fact_ids": ["F-what"], "text": "q" * 501},
            {"question_kind": "premise_check", "fact_ids": ["F-what"], "text": "q"},
        ],
    )
    def test_invalid_questions(self, question: object):
        error = _shape(_covering(["F-what"]), question)
        assert error["code"] == "invalid_question"
        assert SECRET not in json.dumps(error)

    def test_valid_questions(self):
        for question in (
            {"question_kind": "judgment", "fact_ids": ["F-what", "F-weak"], "text": "q"},
            {"question_kind": "premise_check", "fact_ids": ["F-premise.1"], "text": "q" * 500},
        ):
            assert _shape(_covering(["F-what"]), question) is None

    def test_messages_never_quote_the_text(self):
        paragraphs = [{"fact_ids": ["F-what"], "text": SECRET * 200}]
        assert SECRET not in json.dumps(_shape(paragraphs))


# ─── Coverage (S2 spec §2.3 checks 4, 5 and 7: D4) ──────────────────────


class TestCoverage:
    def test_every_fact_leading_a_paragraph_is_accepted(self):
        assert explanation_refusal(_fact_core(), _covering(CORE_IDS), None) is None

    def test_a_missing_fact_is_refused_and_listed(self):
        paragraphs = _covering([i for i in CORE_IDS if i != "F-weak"])
        error = explanation_refusal(_fact_core(), paragraphs, None)
        assert error == {
            "status": "error",
            "code": "explanation_fact_ids_missing",
            "message": "Every fact needs a paragraph that leads with it; these have none: F-weak.",
            "missing_fact_ids": ["F-weak"],
            "unknown_fact_ids": [],
        }

    def test_a_fact_only_mentioned_second_is_missing(self):
        paragraphs = _covering(["F-what", "F-change", "F-revert", "F-uncertain"])
        paragraphs[0]["fact_ids"] = ["F-what", "F-weak"]
        error = explanation_refusal(_fact_core(), paragraphs, None)
        assert error["missing_fact_ids"] == ["F-weak"]

    @pytest.mark.parametrize("fact_id", ["F-alt.3", "F-premise.1"])
    def test_unknown_ids_are_refused_and_listed(self, fact_id: str):
        error = explanation_refusal(_fact_core(), _covering([*CORE_IDS, fact_id]), None)
        assert error["code"] == "explanation_fact_ids_unknown"
        assert error["unknown_fact_ids"] == [fact_id] and error["missing_fact_ids"] == []
        assert fact_id in error["message"]

    def test_a_retired_id_is_unknown(self):
        fact_core = _fact_core()
        fact_core["retired_ids"] = ["F-alt.1"]
        error = explanation_refusal(fact_core, _covering([*CORE_IDS, "F-alt.1"]), None)
        assert error["unknown_fact_ids"] == ["F-alt.1"]

    def test_both_lists_come_together(self):
        paragraphs = _covering(["F-what", "F-change", "F-revert", "F-uncertain", "F-alt.9"])
        paragraphs[0]["fact_ids"] = ["F-what", "F-alt.8"]
        error = explanation_refusal(_fact_core(), paragraphs, None)
        assert error["code"] == "explanation_fact_ids_missing"
        assert error["missing_fact_ids"] == ["F-weak"]
        assert error["unknown_fact_ids"] == ["F-alt.8", "F-alt.9"]

    def test_a_mermaid_only_paragraph_does_not_count(self):
        paragraphs = _covering(["F-what", "F-change", "F-revert", "F-uncertain"])
        paragraphs.append({"fact_ids": ["F-weak"], "text": f"  {MERMAID}\n"})
        error = explanation_refusal(_fact_core(), paragraphs, None)
        assert error["missing_fact_ids"] == ["F-weak"]
        # A diagram with words around it is a paragraph like any other.
        paragraphs[-1]["text"] = f"The slow path:\n{MERMAID}"
        assert explanation_refusal(_fact_core(), paragraphs, None) is None

    def test_a_mermaid_paragraph_still_gets_the_id_checks(self):
        paragraphs = [*_covering(CORE_IDS), {"fact_ids": ["F-alt.2"], "text": MERMAID}]
        error = explanation_refusal(_fact_core(), paragraphs, None)
        assert error["unknown_fact_ids"] == ["F-alt.2"]
        paragraphs[-1]["fact_ids"] = ["F-what", "F-weak", "F-change"]
        assert explanation_refusal(_fact_core(), paragraphs, None)["code"] == "invalid_fact_ids"

    def test_the_question_needs_current_ids(self):
        fact_core = _fact_core([*CORE_IDS, "F-premise.1"])
        paragraphs = _covering([*CORE_IDS, "F-premise.1"])
        question = {"question_kind": "premise_check", "fact_ids": ["F-premise.1"], "text": "q"}
        assert explanation_refusal(fact_core, paragraphs, question) is None
        question["fact_ids"] = ["F-premise.2"]
        assert explanation_refusal(fact_core, paragraphs, question)["code"] == "invalid_question"

    def test_without_a_readable_fact_core_every_id_is_unknown(self):
        for fact_core in (None, {"version": "1", "items": []}):
            error = explanation_refusal(fact_core, _covering(["F-what"]), None)
            assert error["code"] == "explanation_fact_ids_unknown"


# ─── The pure writer (S2 spec §3.2 steps 3-5, §2.7) ─────────────────────


def _doc(adr: Decision, **overrides: object) -> dict:
    doc = {
        "schema": 1,
        "decision_id": adr.id,
        "anchor": anchor_for(adr),
        "recorded_at": NOW,
        "declared": {"origin": "ai_auto", "recorded_timing": "before_impl"},
        "lifecycle": "proposed",
        "links": {"supersedes": [], "superseded_by": [], "amends": []},
        "fact_core": _fact_core(),
        "events": [{"at": NOW, "kind": "created", "lifecycle": "proposed", "via": "x"}],
    }
    doc.update(overrides)
    return doc


def _add(doc: dict, adr: Decision, version: object = 1, **kwargs: object) -> lineage.AppendOutcome:
    kwargs.setdefault("paragraphs", _covering(CORE_IDS))
    return add_explanation_doc(doc, adr, NOW, fact_core_version=version, **kwargs)


class TestPureWriter:
    def test_the_entry_and_the_response_fields(self):
        adr = _adr()
        question = {"question_kind": "judgment", "fact_ids": ["F-what", "F-weak"], "text": "Ok?"}
        outcome = _add(_doc(adr), adr, question=question)
        assert outcome.error is None and outcome.notices == []
        assert outcome.fields == {
            "explanation_id": "ADR-900.EX-1",
            "fact_core_version": 1,
            "fact_ids_present": True,
            "accuracy": "unconfirmed",
            "paragraphs": 5,
        }
        assert outcome.doc["explanations"] == {
            "last_number": 1,
            "items": [
                {
                    "id": "ADR-900.EX-1",
                    "at": NOW,
                    "via": "pm_add_explanation",
                    "fact_core_version": 1,
                    "fact_ids_present": True,
                    "paragraphs": _covering(CORE_IDS),
                    "question": question,
                }
            ],
        }
        # The new section goes after the fact core, before the events (S2 spec §2.1).
        assert list(outcome.doc)[-3:] == ["fact_core", "explanations", "events"]

    @pytest.mark.parametrize(
        ("section", "number"),
        [
            ({"last_number": 5, "items": []}, 6),
            ({"last_number": 0, "items": [{"id": "ADR-900.EX-7", "fact_core_version": 1}]}, 8),
            ({"items": [{"id": "ADR-900.EX-2", "fact_core_version": 1}]}, 3),
            ({"last_number": 9, "items": [{"id": "ADR-900.EX-2", "fact_core_version": 1}]}, 10),
        ],
    )
    def test_numbers_are_never_reused(self, section: dict, number: int):
        adr = _adr()
        outcome = _add(_doc(adr, explanations=section), adr)
        assert outcome.fields["explanation_id"] == f"ADR-900.EX-{number}"
        assert outcome.doc["explanations"]["last_number"] == number

    def test_numbers_run_out(self):
        adr = _adr()
        doc = _doc(adr, explanations={"last_number": lineage.MAX_ENTRY_NUMBER, "items": []})
        assert _add(doc, adr).error["code"] == "explanation_ids_exhausted"
        doc["explanations"]["last_number"] -= 1
        assert _add(doc, adr).fields["explanation_id"] == "ADR-900.EX-999999"

    @pytest.mark.parametrize("version", [0, -1, True, "1", 1.0, None])
    def test_the_version_argument(self, version: object):
        adr = _adr()
        assert _add(_doc(adr), adr, version).error["code"] == "invalid_fact_core_version"

    def test_the_version_must_be_the_current_one(self):
        adr = _adr()
        error = _add(_doc(adr), adr, 2).error
        assert error["code"] == "fact_core_version_mismatch" and error["current_version"] == 1

    def test_the_version_is_checked_before_the_coverage(self):
        adr = _adr()
        error = _add(_doc(adr), adr, 2, paragraphs=_covering(["F-what"])).error
        assert error["code"] == "fact_core_version_mismatch"

    def test_without_a_fact_core(self):
        adr = _adr()
        doc = _doc(adr)
        del doc["fact_core"]
        assert _add(doc, adr).error["code"] == "decision_fact_core_missing"

    def test_nothing_is_changed_in_place_and_aliases_keep_their_value(self):
        adr = _adr()
        section = {"last_number": 1, "items": [{"id": "ADR-900.EX-1", "fact_core_version": 1}]}
        doc = _doc(adr, explanations=section, mirror=section, copy_of_items=section["items"])
        before = copy.deepcopy(doc)
        outcome = _add(doc, adr)
        assert doc == before
        assert doc["mirror"] is section and doc["copy_of_items"] is section["items"]
        assert outcome.doc["mirror"] == before["mirror"]
        assert outcome.doc["copy_of_items"] == before["copy_of_items"]
        assert outcome.doc["explanations"] is not section
        assert len(outcome.doc["explanations"]["items"]) == 2

    def test_a_missing_anchor_is_added(self):
        adr = _adr()
        doc = _doc(adr)
        del doc["anchor"]
        outcome = _add(doc, adr)
        assert outcome.doc["anchor"] == anchor_for(adr)
        assert _codes(outcome.notices) == ["decision_lineage_anchor_missing"]


# ─── Broken sections the writer reads (S2 spec §2.7) ────────────────────


def _broken_entry(**fields: object) -> dict:
    return {"id": "ADR-900.EX-1", "fact_core_version": 1, **fields}


BROKEN_EXPLANATIONS = [
    SECRET,
    [SECRET],
    {"items": SECRET},
    {"last_number": 1},
    {"last_number": 1, "items": [SECRET]},
    {"items": [_broken_entry(id=f"ADR-900.EX-{SECRET}")]},
    {"items": [_broken_entry(id=7)]},
    {"items": [_broken_entry(fact_core_version=True)]},
    {"items": [_broken_entry(fact_core_version="1")]},
    {"items": [_broken_entry(fact_core_version=-1)]},
    {"last_number": "1", "items": []},
    {"last_number": 1.0, "items": []},
    {"last_number": True, "items": []},
    {"last_number": -1, "items": []},
]


class TestWriterRefusals:
    @pytest.mark.parametrize("explanations", BROKEN_EXPLANATIONS)
    def test_a_broken_explanations_section(self, explanations: object):
        adr = _adr()
        doc = _doc(adr, explanations=explanations)
        error = _add(doc, adr).error
        assert error["code"] == "decision_lineage_unreadable"
        assert "explanations" in error["message"] and "Do not edit" in error["remediation"]
        assert SECRET not in json.dumps(error)

    @pytest.mark.parametrize(
        "fact_core",
        [
            SECRET,
            {**_fact_core(), "version": "1"},
            {**_fact_core(), "version": 0},
            {**_fact_core(), "items": SECRET},
            {**_fact_core(), "items": [{"id": "F-what", "text": SECRET}]},
            {**_fact_core(), "previous_values": [SECRET]},
        ],
    )
    def test_a_broken_fact_core(self, fact_core: object):
        adr = _adr()
        error = _add(_doc(adr, fact_core=fact_core), adr).error
        assert error["code"] == "decision_lineage_unreadable"
        assert "so an explanation cannot be written" in error["message"]
        assert SECRET not in json.dumps(error)

    def test_a_broken_section_is_refused_before_the_version(self):
        adr = _adr()
        error = _add(_doc(adr, explanations=SECRET), adr, 5).error
        assert error["code"] == "decision_lineage_unreadable"


# ─── Through the storage layer (S2 spec §3.2, §5.1) ─────────────────────


class TestThroughStorage:
    def test_an_explanation_is_recorded(self, tmp_pm_path: Path):
        _at_version_one(tmp_pm_path)
        decisions = (tmp_pm_path / "decisions.yaml").read_bytes()
        before = _load(tmp_pm_path)
        result = _recorded(tmp_pm_path)
        assert result.fields == {
            "explanation_id": "ADR-900.EX-1",
            "fact_core_version": 1,
            "fact_ids_present": True,
            "accuracy": "unconfirmed",
            "paragraphs": 5,
        }
        assert result.warnings == []
        doc = _load(tmp_pm_path)
        entry = doc["explanations"]["items"][0]
        assert set(entry) == {
            "id",
            "at",
            "via",
            "fact_core_version",
            "fact_ids_present",
            "paragraphs",
        }
        assert "accuracy" not in json.dumps(doc)  # never stored (S2 spec §2.3)
        # The lifecycle, the events and decisions.yaml are left alone.
        assert doc["lifecycle"] == before["lifecycle"] and doc["events"] == before["events"]
        assert doc["fact_core"] == before["fact_core"]
        assert (tmp_pm_path / "decisions.yaml").read_bytes() == decisions
        assert _recorded(tmp_pm_path).fields["explanation_id"] == "ADR-900.EX-2"

    def test_a_question_is_stored_with_the_explanation(self, tmp_pm_path: Path):
        _at_version_one(tmp_pm_path)
        question = {"question_kind": "tech_review", "fact_ids": ["F-change"], "text": "Ok?"}
        _recorded(tmp_pm_path, question=question)
        assert _load(tmp_pm_path)["explanations"]["items"][0]["question"] == question

    @pytest.mark.parametrize(
        ("kwargs", "code"),
        [
            ({"version": 2}, "fact_core_version_mismatch"),
            ({"version": 0}, "invalid_fact_core_version"),
            ({"version": True}, "invalid_fact_core_version"),
            ({"paragraphs": _covering(CORE_IDS[:-1])}, "explanation_fact_ids_missing"),
            ({"paragraphs": _covering([*CORE_IDS, "F-alt.1"])}, "explanation_fact_ids_unknown"),
            ({"paragraphs": []}, "invalid_paragraphs"),
            ({"paragraphs": [{"fact_ids": [], "text": "t"}]}, "invalid_fact_ids"),
            ({"paragraphs": [{"fact_ids": ["F-what"], "text": " "}]}, "empty_paragraph"),
            ({"paragraphs": _covering(["F-what"], "x" * 2001)}, "text_too_long"),
            ({"paragraphs": _covering(["F-what"]) * 41}, "explanation_too_large"),
            (
                {"question": {"question_kind": "x", "fact_ids": ["F-what"], "text": "q"}},
                "invalid_question",
            ),
            ({"decision_id": "ADR-777"}, "decision_not_found"),
            ({"decision_id": "not an id"}, "invalid_decision_id"),
        ],
    )
    def test_errors_leave_the_files_alone(self, tmp_pm_path: Path, kwargs: dict, code: str):
        _at_version_one(tmp_pm_path)
        _recorded(tmp_pm_path)
        before = _snapshot(tmp_pm_path)
        result = _explain(tmp_pm_path, **kwargs)
        assert result.status == "error" and result.error["code"] == code, result.error
        assert _snapshot(tmp_pm_path) == before

    def test_the_mismatch_names_the_current_version(self, tmp_pm_path: Path):
        _at_version_one(tmp_pm_path)
        _revise(tmp_pm_path, _facts(weak={"kind": "weak", "text": "w2"}), 1)
        result = _explain(tmp_pm_path, 1)
        assert result.error["code"] == "fact_core_version_mismatch"
        assert result.error["current_version"] == 2

    def test_no_lineage_or_no_fact_core(self, tmp_pm_path: Path):
        adr = _seed(tmp_pm_path)
        before = _snapshot(tmp_pm_path)
        result = _explain(tmp_pm_path)
        assert result.error["code"] == "decision_fact_core_missing"
        assert "pm_update_decision" in result.error["remediation"]
        assert _snapshot(tmp_pm_path) == before
        assert not _path(tmp_pm_path).exists()
        doc = _doc(adr)
        del doc["fact_core"]
        _path(tmp_pm_path).parent.mkdir()
        _store(tmp_pm_path, doc)
        before = _snapshot(tmp_pm_path)
        assert _explain(tmp_pm_path).error["code"] == "decision_fact_core_missing"
        assert _snapshot(tmp_pm_path) == before

    @pytest.mark.parametrize("explanations", BROKEN_EXPLANATIONS[:6])
    def test_a_broken_section_leaves_the_file_alone(self, tmp_pm_path: Path, explanations):
        _at_version_one(tmp_pm_path)
        doc = _load(tmp_pm_path)
        doc["explanations"] = explanations
        _store(tmp_pm_path, doc)
        before = _snapshot(tmp_pm_path)
        result = _explain(tmp_pm_path)
        assert result.error["code"] == "decision_lineage_unreadable"
        assert SECRET not in json.dumps(result.error)
        assert _snapshot(tmp_pm_path) == before

    def test_a_duplicate_or_unreadable_decisions_yaml(self, tmp_pm_path: Path):
        _at_version_one(tmp_pm_path)
        _save_decisions(tmp_pm_path, [_adr(), _adr()])
        assert _explain(tmp_pm_path).error["code"] == "decision_id_duplicate"
        (tmp_pm_path / "decisions.yaml").write_text(f"decisions: [{SECRET}", encoding="utf-8")
        error = _explain(tmp_pm_path).error
        assert error["code"] == "decisions_yaml_unreadable"
        assert SECRET not in json.dumps(error)

    def test_an_anchor_of_another_adr_is_refused(self, tmp_pm_path: Path):
        _at_version_one(tmp_pm_path)
        doc = _load(tmp_pm_path)
        doc["anchor"] = anchor_for(_adr(adr_id=OTHER_ID))
        _store(tmp_pm_path, doc)
        before = _snapshot(tmp_pm_path)
        assert _explain(tmp_pm_path).error["code"] == "decision_lineage_anchor_mismatch"
        assert _snapshot(tmp_pm_path) == before

    def test_a_symlinked_lineage_is_refused(self, tmp_pm_path: Path, tmp_path: Path):
        _at_version_one(tmp_pm_path)
        outside = tmp_path / "outside.yaml"
        outside.write_bytes(_path(tmp_pm_path).read_bytes())
        _path(tmp_pm_path).unlink()
        _path(tmp_pm_path).symlink_to(outside)
        before = outside.read_bytes()
        result = _explain(tmp_pm_path)
        assert result.error["code"] == "decision_lineage_unreadable"
        assert "symbolic link" in result.error["message"]
        assert outside.read_bytes() == before and _path(tmp_pm_path).is_symlink()

    def test_a_symlinked_lineage_directory_is_refused(self, tmp_pm_path: Path, tmp_path: Path):
        _at_version_one(tmp_pm_path)
        moved = tmp_path / "elsewhere"
        (tmp_pm_path / "decision_lineage").rename(moved)
        (tmp_pm_path / "decision_lineage").symlink_to(moved, target_is_directory=True)
        before = (moved / f"{ADR_ID}.yaml").read_bytes()
        assert _explain(tmp_pm_path).error["code"] == "decision_lineage_unreadable"
        assert (moved / f"{ADR_ID}.yaml").read_bytes() == before

    def test_the_append_cap_counts_bytes(self, tmp_pm_path: Path):
        # Japanese text is 3 bytes a character: 12,000 characters fit in the
        # room left by characters, but not in bytes (S2 spec §2.8).
        _at_version_one(tmp_pm_path)
        doc = _load(tmp_pm_path)
        doc["padding"] = ""
        _store(tmp_pm_path, doc)
        base = _path(tmp_pm_path).stat().st_size
        room = 15_000
        doc["padding"] = "あ" * ((lineage.MAX_APPEND_BYTES - room - base) // 3)
        _store(tmp_pm_path, doc)
        size = _path(tmp_pm_path).stat().st_size
        assert lineage.MAX_APPEND_BYTES - room - 3 <= size <= lineage.MAX_APPEND_BYTES - room
        long = [{"fact_ids": [i], "text": "説" * 2_000} for i in CORE_IDS + ["F-what"]]
        assert sum(len(p["text"]) for p in long) < room < sum(len(p["text"]) * 3 for p in long)
        before = _snapshot(tmp_pm_path)
        result = _explain(tmp_pm_path, paragraphs=long)
        assert result.error["code"] == "decision_lineage_too_large"
        assert "explanations" in result.error["message"]
        assert "last_number" in result.error["remediation"]
        assert _snapshot(tmp_pm_path) == before
        # A short one still fits.
        _recorded(tmp_pm_path, paragraphs=_covering(CORE_IDS, "短い説明"))
        assert _path(tmp_pm_path).stat().st_size <= lineage.MAX_APPEND_BYTES

    def test_an_aliased_section_keeps_its_value_elsewhere(self, tmp_pm_path: Path):
        _at_version_one(tmp_pm_path)
        _recorded(tmp_pm_path)
        text = _path(tmp_pm_path).read_text(encoding="utf-8")
        text = text.replace("explanations:\n", "explanations: &ex\n", 1)
        text = text.replace("  items:\n  - id: ADR-900.EX-1", "  items: &it\n  - id: ADR-900.EX-1")
        _store(tmp_pm_path, text + "mirror: *ex\nmirror_items: *it\n")
        loaded = _load(tmp_pm_path)
        assert loaded["mirror"] is loaded["explanations"]
        _recorded(tmp_pm_path)
        doc = _load(tmp_pm_path)
        assert [e["id"] for e in doc["explanations"]["items"]] == ["ADR-900.EX-1", "ADR-900.EX-2"]
        assert doc["explanations"]["last_number"] == 2
        assert doc["mirror"] == {"last_number": 1, "items": doc["mirror_items"]}
        assert [e["id"] for e in doc["mirror_items"]] == ["ADR-900.EX-1"]

    def test_secrets_are_never_stored_or_shown(self, tmp_pm_path: Path):
        adr = _at_version_one(tmp_pm_path)
        paragraphs = _covering(CORE_IDS, f"key {SECRET}")
        question = {"question_kind": "judgment", "fact_ids": ["F-what"], "text": f"q {SECRET}"}
        result = _recorded(tmp_pm_path, paragraphs=paragraphs, question=question)
        assert SECRET not in _path(tmp_pm_path).read_text(encoding="utf-8")
        assert SECRET not in json.dumps([result.fields, result.warnings])
        warning = next(
            w for w in result.warnings if w["code"] == "decision_lineage_secrets_redacted"
        )
        assert warning["level"] == "warning" and warning["message"].startswith("6 ")
        assert SECRET not in json.dumps(_view(tmp_pm_path, adr).as_dict())
        # A refusal never quotes the text either.
        refused = _explain(tmp_pm_path, paragraphs=paragraphs[:-1])
        assert refused.error["code"] == "explanation_fact_ids_missing"
        assert SECRET not in json.dumps(refused.error)

    def test_a_hand_written_secret_is_not_shown(self, tmp_pm_path: Path):
        adr = _at_version_one(tmp_pm_path)
        _recorded(tmp_pm_path)
        doc = _load(tmp_pm_path)
        entry = doc["explanations"]["items"][0]
        entry["paragraphs"][0]["text"] = SECRET
        entry["question"] = {"question_kind": SECRET, "fact_ids": [SECRET], "text": SECRET}
        entry[SECRET] = SECRET
        doc["explanations"]["items"].append({**entry, "id": f"ADR-900.EX-{SECRET}"})
        _store(tmp_pm_path, doc)
        view = _view(tmp_pm_path, adr)
        assert SECRET not in json.dumps(view.as_dict())
        row = view.explanations["items"][0]
        assert row["question"]["question_kind"] is None and row["question"]["fact_ids"] == []
        assert SKIPPED in view.note_codes

    def test_only_the_lineage_lock_is_taken(
        self, tmp_pm_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        _at_version_one(tmp_pm_path)
        taken: list[str] = []
        real = storage._yaml_transaction

        @contextlib.contextmanager
        def spy(base_dir: Path, filename: str, **kwargs: object) -> Iterator[None]:
            taken.append(filename)
            with real(base_dir, filename, **kwargs):
                yield

        monkeypatch.setattr(storage, "_yaml_transaction", spy)
        _recorded(tmp_pm_path)
        assert taken == [f"decision_lineage-{ADR_ID}"]

    def test_it_finishes_while_another_thread_holds_the_decisions_lock(
        self, tmp_pm_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        _at_version_one(tmp_pm_path)
        monkeypatch.setenv("PM_LOCK_TIMEOUT_S", "3")
        holding = threading.Event()
        release = threading.Event()

        def hold() -> None:
            with storage._yaml_transaction(tmp_pm_path, "decisions.yaml"):
                holding.set()
                release.wait(10)

        holder = threading.Thread(target=hold)
        holder.start()
        try:
            assert holding.wait(5)
            started = time.monotonic()
            result = _explain(tmp_pm_path)
            assert time.monotonic() - started < 2
            assert result.status == "recorded", result.error
        finally:
            release.set()
            holder.join(10)

    def test_a_change_outside_the_tools_is_saved_and_reported(self, tmp_pm_path: Path):
        adr = _at_version_one(tmp_pm_path)
        doc = _load(tmp_pm_path)
        doc["fact_core"]["items"][2]["reversibility"] = "code_revertable"
        _store(tmp_pm_path, doc)
        result = _recorded(tmp_pm_path)
        assert _codes(result.warnings) == ["decision_reversibility_changed_outside_tools"]
        warning = result.warnings[0]
        assert warning["level"] == "info"
        assert "recorded unknown, now code_revertable" in warning["message"]
        assert "pm_update_decision" in warning["remediation"]
        row = _view(tmp_pm_path, adr).explanations["items"][0]
        assert row["stale"] is True and row["changed_ids"] == ["F-revert"]
        assert "paragraphs" not in row

    def test_a_started_lineage_for_a_writer_that_starts_one(self, tmp_pm_path: Path):
        # pm_add_feedback's path (S2 spec §5.1): a lineage is started first.
        _seed(tmp_pm_path)
        seen: list[dict] = []

        def apply(doc: dict, adr: Decision, now: str) -> lineage.AppendOutcome:
            seen.append(doc)
            return lineage.AppendOutcome(doc={**doc, "extra": 1}, fields={"ok": True})

        result = storage._append_to_lineage(
            tmp_pm_path, ADR_ID, apply, when_missing="start", via="pm_x", section="feedback"
        )
        assert result.status == "recorded" and result.fields == {"ok": True}
        assert _codes(result.warnings) == ["decision_lineage_started"]
        assert seen[0]["events"][0]["kind"] == "lineage_started"
        assert seen[0]["events"][0]["via"] == "pm_x"
        assert _load(tmp_pm_path)["extra"] == 1


# ─── Stale explanations (S2 spec §2.3, D6) ──────────────────────────────


class TestStale:
    def test_a_revision_makes_older_explanations_stale(self, tmp_pm_path: Path):
        adr = _at_version_one(tmp_pm_path)
        _recorded(tmp_pm_path)
        result = _revise(tmp_pm_path, _facts(weak={"kind": "weak", "text": "w2"}), 1)
        stale = next(w for w in result.warnings if w["code"] == STALE)
        assert stale["level"] == "info"
        assert "ADR-900.EX-1" in stale["message"] and "version 1" in stale["message"]
        assert "pm_add_explanation" in stale["remediation"]
        _recorded(tmp_pm_path, 2)
        view = _view(tmp_pm_path, adr)
        new, old = view.explanations["items"]
        assert old["id"] == "ADR-900.EX-1" and old["stale"] is True
        assert old["changed_ids"] == ["F-weak"] and "paragraphs" not in old
        assert new["id"] == "ADR-900.EX-2" and new["stale"] is False and new["changed_ids"] == []
        assert new["paragraphs"] == _covering(CORE_IDS)
        # A revision without an explanation of the replaced version says nothing.
        result = _revise(tmp_pm_path, _facts(weak={"kind": "weak", "text": "w3"}), 2)
        assert STALE in _codes(result.warnings)
        result = _revise(tmp_pm_path, _facts(weak={"kind": "weak", "text": "w4"}), 3)
        assert STALE not in _codes(result.warnings)

    def test_the_notice_lists_only_ids_it_can_show(self, tmp_pm_path: Path):
        _at_version_one(tmp_pm_path)
        _recorded(tmp_pm_path)
        doc = _load(tmp_pm_path)
        hand = {"id": f"ADR-900.EX-{SECRET}", "fact_core_version": 1}
        doc["explanations"]["items"] += [
            hand,
            "junk",
            {"id": "ADR-900.EX-9", "fact_core_version": 0},
        ]
        _store(tmp_pm_path, doc)
        result = _revise(tmp_pm_path, _facts(weak={"kind": "weak", "text": "w2"}), 1)
        stale = next(w for w in result.warnings if w["code"] == STALE)
        assert "2 explanation(s)" in stale["message"]
        assert "ADR-900.EX-1 and 1 more" in stale["message"]
        assert "EX-9" not in stale["message"]
        assert SECRET not in json.dumps(result.warnings)

    def test_more_than_thirty_changed_ids_are_all_listed(self, tmp_pm_path: Path):
        adr = _seed(tmp_pm_path)
        many = [{"kind": "alt", "text": f"alt {n}"} for n in range(13)] + [
            {"kind": "conditions", "text": f"conditions {n}"} for n in range(12)
        ]
        first = _revise(tmp_pm_path, _facts(*many), 0)
        ids = first.fact_core["ids"]
        _recorded(tmp_pm_path, paragraphs=_covering(ids))
        renewed = [{"kind": "alt", "text": f"new alt {n}"} for n in range(25)]
        second = _revise(tmp_pm_path, _facts(*renewed), 1)
        row = _view(tmp_pm_path, adr).explanations["items"][0]
        expected = set(second.changes["fact_core"]["changed_ids"])
        assert len(expected) == 50
        assert row["stale"] is True and set(row["changed_ids"]) == expected
        assert "changed_ids_truncated" not in row

    def test_the_scan_cap_marks_changed_ids_truncated(self, tmp_pm_path: Path):
        adr = _at_version_one(tmp_pm_path)
        _recorded(tmp_pm_path)
        _revise(tmp_pm_path, _facts(weak={"kind": "weak", "text": "w2"}), 1)
        text = _path(tmp_pm_path).read_text(encoding="utf-8")
        element = "{id: F-alt.1, last_version: 1, text: t}"
        count = lineage.SCAN_PREVIOUS_VALUES + 1
        aliased = "fact_core_pad: &pv " + element + "\n"
        text = text.replace(
            "  previous_values:\n",
            "  previous_values: [" + ", ".join(["*pv"] * count) + "]\n  old_previous_values:\n",
            1,
        )
        _store(tmp_pm_path, aliased + text)
        row = _view(tmp_pm_path, adr).explanations["items"][0]
        assert row["stale"] is True and row["changed_ids_truncated"] is True
        assert row["changed_ids"] == ["F-weak", "F-alt.1"]  # what the scanned part holds

    def test_passing_back_a_redacted_secret_keeps_the_explanation_current(self, tmp_project: Path):
        # S2 spec §6.1 D6, §2.2: a hand-placed secret is compared after
        # redaction, so every item passed back as get shows it is unchanged.
        pm = tmp_project / ".pm"
        _at_version_one(pm)
        doc = _load(pm)
        doc["fact_core"]["items"][1]["text"] = f"Before: queue {SECRET}"
        _store(pm, doc)
        assert _tool_explain(tmp_project)["status"] == "recorded"
        shown = _tool_get(tmp_project)["lineage"]["fact_core"]["items"]
        assert SECRET not in json.dumps(shown)
        keep = ("id", "text", "reversibility", "confidence")
        items = [{key: item[key] for key in keep if key in item} for item in shown]
        revised = pm_update_decision(
            decision_id=ADR_ID,
            fact_core=items,
            expected_fact_core_version=1,
            project_path=str(tmp_project),
        )
        assert revised["status"] == "unchanged"
        assert STALE not in _codes(revised.get("warnings", []))
        assert _load(pm)["fact_core"]["items"][1]["text"].endswith(SECRET)
        row = _tool_get(tmp_project)["lineage"]["explanations"]["items"][0]
        assert (row["id"], row["stale"], row["changed_ids"]) == (f"{ADR_ID}.EX-1", False, [])


# ─── The S2 view of the explanations (S2 spec §2.6, §2.7) ───────────────


def _hand_view(explanations: object, **overrides: object) -> lineage.LineageView:
    adr = _adr()
    doc = _doc(adr, explanations=explanations, **overrides)
    raw = lineage.RawLineage(adr.id, exists=True, data=doc)
    return lineage_view(adr, raw, include_s2=True)


def _entry(number: int, version: int = 1, **fields: object) -> dict:
    return {
        "id": f"ADR-900.EX-{number}",
        "at": NOW,
        "via": "pm_add_explanation",
        "fact_core_version": version,
        "fact_ids_present": True,
        "paragraphs": _covering(CORE_IDS),
        "question": {"question_kind": "judgment", "fact_ids": ["F-what"], "text": "q"},
        **fields,
    }


class TestView:
    def test_rows_and_the_one_body(self):
        entries = [_entry(n, 0) for n in range(1, 4)] + [_entry(4), _entry(5)]
        view = _hand_view({"last_number": 5, "items": entries})
        section = view.explanations
        assert section["explanations_total"] == 5 and "explanations_total_capped" not in section
        ids = [row["id"] for row in section["items"]]
        assert ids == [f"ADR-900.EX-{n}" for n in (5, 4, 3, 2, 1)]
        newest, *rest = section["items"]
        assert newest == {
            "id": "ADR-900.EX-5",
            "at": NOW,
            "fact_core_version": 1,
            "stale": False,
            "changed_ids": [],
            "fact_ids_present": True,
            "accuracy": "unconfirmed",
            "question_kind": "judgment",
            "paragraphs_total": 5,
            "paragraphs": _covering(CORE_IDS),
            "question": {"question_kind": "judgment", "fact_ids": ["F-what"], "text": "q"},
        }
        # Only the newest current one carries a body; stale ones never do.
        assert all("paragraphs" not in row and "question" not in row for row in rest)
        assert rest[0]["stale"] is False and rest[1]["stale"] is True
        assert all(row["accuracy"] == "unconfirmed" for row in section["items"])
        assert view.notes == []
        assert "explanations" not in view.unknown_keys
        assert view.as_dict()["explanations"] == section

    def test_at_most_twenty_rows(self):
        view = _hand_view({"items": [_entry(n) for n in range(1, 26)]})
        rows = view.explanations["items"]
        assert len(rows) == lineage.VIEW_EXPLANATIONS
        assert rows[0]["id"] == "ADR-900.EX-25" and view.explanations["explanations_total"] == 25
        assert [row for row in rows if "paragraphs" in row] == rows[:1]

    def test_no_section_and_no_lineage(self):
        assert _hand_view(None).explanations == {"items": [], "explanations_total": 0}
        adr = _adr()
        derived = lineage_view(adr, lineage.RawLineage(adr.id, exists=False), include_s2=True)
        assert derived.as_dict()["explanations"] == {"items": [], "explanations_total": 0}
        plain = lineage_view(adr, lineage.RawLineage(adr.id, exists=False))
        assert "explanations" not in plain.as_dict()

    @pytest.mark.parametrize("explanations", BROKEN_EXPLANATIONS[:5])
    def test_a_broken_section_is_shown_empty(self, explanations: object):
        view = _hand_view(explanations)
        assert view.explanations["items"] == []
        assert SKIPPED in view.note_codes
        assert SECRET not in json.dumps(view.as_dict())

    def test_broken_entries_are_skipped(self):
        broken = [
            _broken_entry(id=7),
            _broken_entry(fact_core_version=True),
            _broken_entry(fact_core_version=-1),
            SECRET,
        ]
        view = _hand_view({"items": [_entry(1), *broken]})
        assert [row["id"] for row in view.explanations["items"]] == ["ADR-900.EX-1"]
        assert view.explanations["explanations_total"] == 5
        assert {"code": SKIPPED, "count": 4} in view.notes

    def test_the_scan_cap(self):
        entries = [_entry(1, paragraphs=[])] * (lineage.SCAN_EXPLANATIONS + 5)
        view = _hand_view({"items": entries})
        section = view.explanations
        assert section["explanations_total"] == lineage.SCAN_EXPLANATIONS
        assert section["explanations_total_capped"] is True
        assert len(section["items"]) == lineage.VIEW_EXPLANATIONS
        assert {"code": SKIPPED, "count": 5} in view.notes

    def test_the_body_keeps_the_writer_limits(self):
        long = {"fact_ids": ["F-what", "F-weak", "F-change"], "text": "y" * 2_500}
        entry = _entry(1, paragraphs=[long] * (lineage.MAX_PARAGRAPHS + 3))
        entry["question"] = {"question_kind": "judgment", "fact_ids": ["F-what"], "text": "q" * 900}
        view = _hand_view({"items": [entry]})
        row = view.explanations["items"][0]
        assert row["paragraphs_truncated"] is True
        assert row["paragraphs_total"] == lineage.MAX_PARAGRAPHS + 3
        texts = [p["text"] for p in row["paragraphs"]]
        assert sum(map(len, texts)) == lineage.MAX_EXPLANATION_CHARS
        assert all(len(text) <= lineage.MAX_PARAGRAPH_CHARS for text in texts)
        assert all(p["fact_ids"] == ["F-what", "F-weak"] for p in row["paragraphs"])
        assert len(row["question"]["text"]) == lineage.MAX_QUESTION_CHARS
        assert row["question"]["truncated"] is True
        assert SKIPPED in view.note_codes

    def test_unknown_keys_are_named_only(self):
        entry = _entry(1, **{f"k{n}": SECRET for n in range(12)})
        view = _hand_view({"items": [entry], "extra": SECRET})
        section = view.explanations
        assert section["unknown_fields"] == ["extra"]
        assert section["items"][0]["unknown_fields"] == [f"k{n}" for n in range(9)]
        assert SECRET not in json.dumps(view.as_dict())

    def test_odd_values_of_a_readable_entry(self):
        entry = _entry(1, at=12345, fact_ids_present="yes", paragraphs=SECRET, question=SECRET)
        row = _hand_view({"items": [entry]}).explanations["items"][0]
        assert row["at"] is None and row["fact_ids_present"] is False
        assert row["paragraphs_total"] == 0 and row["paragraphs"] == []
        assert row["question_kind"] is None and row["question"] is None

    def test_an_explanation_without_a_fact_core(self):
        view = _hand_view({"items": [_entry(1, 0), _entry(2, 1)]}, fact_core=None)
        old, zero = view.explanations["items"]
        assert old["stale"] is True and zero["stale"] is False


# ─── The adoption's check points at the writer (S2 spec §3.1) ───────────


def test_the_adoption_check_sees_a_recorded_explanation(tmp_pm_path: Path):
    _at_version_one(tmp_pm_path)
    adopted = change_decision_lineage(
        tmp_pm_path, ADR_ID, LineageChange(lifecycle="adopted", reason="the user adopted it")
    )
    warning = next(
        w for w in adopted.warnings if w["code"] == "decision_adopted_without_current_explanation"
    )
    assert "pm_add_explanation" in warning["remediation"]
    back = change_decision_lineage(
        tmp_pm_path, ADR_ID, LineageChange(lifecycle="proposed", reason="the user asked")
    )
    assert back.status == "updated"
    _recorded(tmp_pm_path)
    again = change_decision_lineage(
        tmp_pm_path, ADR_ID, LineageChange(lifecycle="adopted", reason="the user adopted it")
    )
    assert "decision_adopted_without_current_explanation" not in _codes(again.warnings)
    assert _load(tmp_pm_path)["events"][-1]["fact_core_version"] == 1


# ─── The tool (S2 spec §3.2, §3.6) ──────────────────────────────────────

SPEC = Path(__file__).resolve().parents[1] / "docs" / "issues" / "DESIGN_decision-lineage-s2.md"
QUESTION = {"question_kind": "judgment", "fact_ids": ["F-weak"], "text": "Is it acceptable?"}
NOTICE = "Explanations are checked only for fact coverage; their accuracy is unconfirmed."


def _tool_explain(
    project: Path,
    version: object = 1,
    paragraphs: object = None,
    question: object = None,
    decision_id: str = ADR_ID,
) -> dict:
    return pm_add_explanation(
        decision_id=decision_id,
        fact_core_version=version,
        paragraphs=_covering(CORE_IDS) if paragraphs is None else paragraphs,
        question=question,
        project_path=str(project),
    )


def _tool_get(project: Path) -> dict:
    return pm_decision_query(action="get", decision_id=ADR_ID, project_path=str(project))


def _s2_chars(lineage_part: dict) -> int:
    keys = ("fact_core", "explanations", "adopted_fact_core_version")
    return len(json.dumps({key: lineage_part[key] for key in keys}, ensure_ascii=False))


class TestTool:
    def test_the_response_shape(self, tmp_project: Path):
        _at_version_one(tmp_project / ".pm")
        result = _tool_explain(tmp_project, question=QUESTION)
        assert result == {
            "status": "recorded",
            "decision_id": ADR_ID,
            "explanation_id": f"{ADR_ID}.EX-1",
            "fact_core_version": 1,
            "fact_ids_present": True,
            "accuracy": "unconfirmed",
            "paragraphs": 5,
            "warnings": [],
        }
        assert list(result)[0] == "status" and list(result)[-1] == "warnings"
        assert _tool_explain(tmp_project)["explanation_id"] == f"{ADR_ID}.EX-2"

    def test_a_coverage_refusal_lists_the_ids_and_writes_nothing(self, tmp_project: Path):
        pm = tmp_project / ".pm"
        _at_version_one(pm)
        before = _snapshot(pm)
        paragraphs = _covering(["F-what", "F-change", "F-revert", "F-uncertain"])
        assert _tool_explain(tmp_project, paragraphs=paragraphs) == {
            "status": "error",
            "code": "explanation_fact_ids_missing",
            "message": "Every fact needs a paragraph that leads with it; these have none: F-weak.",
            "missing_fact_ids": ["F-weak"],
            "unknown_fact_ids": [],
        }
        assert _snapshot(pm) == before

    @pytest.mark.parametrize(
        ("version", "decision_id", "code", "extra"),
        [
            (2, ADR_ID, "fact_core_version_mismatch", {"current_version": 1}),
            (1, "ADR-950", "decision_not_found", {}),
        ],
    )
    def test_file_errors_come_back_as_they_are(
        self, tmp_project: Path, version: int, decision_id: str, code: str, extra: dict
    ):
        pm = tmp_project / ".pm"
        _at_version_one(pm)
        before = _snapshot(pm)
        result = _tool_explain(tmp_project, version, decision_id=decision_id)
        assert result["status"] == "error" and result["code"] == code
        assert {key: result[key] for key in extra} == extra
        assert _snapshot(pm) == before

    @pytest.mark.parametrize(
        ("decision_id", "version", "paragraphs", "question", "code"),
        [
            (SECRET, 1, None, None, "invalid_decision_id"),
            (ADR_ID, 0, None, None, "invalid_fact_core_version"),
            (ADR_ID, True, None, None, "invalid_fact_core_version"),
            (ADR_ID, "1", None, None, "invalid_fact_core_version"),
            (ADR_ID, 1, [], None, "invalid_paragraphs"),
            (ADR_ID, 1, [{"fact_ids": [SECRET], "text": "t"}], None, "invalid_fact_ids"),
            (
                ADR_ID,
                1,
                None,
                {"question_kind": "judgeable_kinds", "fact_ids": ["F-what"], "text": SECRET},
                "invalid_question",
            ),
        ],
    )
    def test_argument_errors_come_before_the_project_is_resolved(
        self,
        monkeypatch: pytest.MonkeyPatch,
        decision_id: str,
        version: object,
        paragraphs: object,
        question: object,
        code: str,
    ):
        # S2 spec §3.2 step 1: nothing is resolved, read or locked for them.
        def unreachable(*args: object, **kwargs: object) -> None:
            raise AssertionError("reached past the argument checks")

        monkeypatch.setattr(server, "_get_pm_path", unreachable)
        monkeypatch.setattr(server, "record_lineage_explanation", unreachable)
        result = pm_add_explanation(
            decision_id=decision_id,
            fact_core_version=version,
            paragraphs=_covering(CORE_IDS) if paragraphs is None else paragraphs,
            question=question,
        )
        assert result["status"] == "error" and result["code"] == code
        assert SECRET not in json.dumps(result)

    def test_secrets_are_neither_stored_nor_shown(self, tmp_project: Path):
        pm = tmp_project / ".pm"
        _at_version_one(pm)
        paragraphs = _covering(CORE_IDS)
        paragraphs[0]["text"] = f"the key {SECRET} is not needed"
        result = _tool_explain(tmp_project, paragraphs=paragraphs)
        assert result["status"] == "recorded"
        assert _codes(result["warnings"]) == ["decision_lineage_secrets_redacted"]
        assert SECRET not in json.dumps(result)
        assert SECRET not in _path(pm).read_text(encoding="utf-8")
        assert SECRET not in json.dumps(_tool_get(tmp_project), ensure_ascii=False)

    def test_the_tool_is_full_mode_only(self):
        # Lens registers only RO_ALLOWLIST (S2 spec §6.3, D9); the wire check
        # is in test_launch_surfaces.
        assert "pm_add_explanation" not in server.RO_ALLOWLIST
        assert "pm_add_explanation" not in server.OUTBOX_READ_ALLOWLIST
        assert "pm_add_explanation" not in server.OUTBOX_WRITE_ALLOWLIST

    def test_the_docstring_is_the_one_the_spec_words(self):
        spec = SPEC.read_text(encoding="utf-8")
        start = spec.index("Save an explanation of an ADR that you wrote")
        worded = " ".join(spec[start : spec.index("```", start)].split())
        assert " ".join((pm_add_explanation.__doc__ or "").split()) == worded


class TestGet:
    def test_explanations_are_shown_with_the_stale_ones_marked(self, tmp_project: Path):
        pm = tmp_project / ".pm"
        _at_version_one(pm)
        _tool_explain(tmp_project, question=QUESTION)
        revised = pm_update_decision(
            decision_id=ADR_ID,
            fact_core=_facts(weak={"kind": "weak", "text": "w2"}),
            expected_fact_core_version=1,
            project_path=str(tmp_project),
        )
        assert STALE in _codes(revised["warnings"])
        _tool_explain(tmp_project, 2)
        got = _tool_get(tmp_project)
        section = got["lineage"]["explanations"]
        assert section["explanations_total"] == 2
        new, old = section["items"]
        assert (new["id"], new["stale"], new["changed_ids"]) == (f"{ADR_ID}.EX-2", False, [])
        assert new["paragraphs"] == _covering(CORE_IDS) and new["accuracy"] == "unconfirmed"
        assert (old["id"], old["stale"], old["changed_ids"]) == (f"{ADR_ID}.EX-1", True, ["F-weak"])
        assert "paragraphs" not in old and old["question_kind"] == "judgment"
        assert NOTICE in got["notice"]
        assert "decision_get_truncated" not in _codes(got["warnings"])

    def test_rows_without_a_body_go_after_the_previous_values(self, tmp_project: Path):
        # S2 spec §3.6: previous values go first (oldest first), then the
        # oldest explanation rows without a body; the row with the body stays.
        pm = tmp_project / ".pm"
        _at_version_one(pm)
        _revise(pm, _facts(weak={"kind": "weak", "text": "w2"}), 1)
        doc = _load(pm)
        # Every numbered id changed since version 1, so each stale row lists
        # about 300 ids: twenty of them pass the budget on their own.
        doc["fact_core"]["previous_values"] = [
            {"id": f"F-{kind}.{n}", "last_version": 1, "text": "x", "source": "proposal"}
            for kind in ("alt", "conditions", "premise")
            for n in range(1, 100)
        ]
        doc["explanations"] = {
            "last_number": 21,
            "items": [_entry(n) for n in range(1, 21)] + [_entry(21, 2)],
        }
        _store(pm, doc)
        got = _tool_get(tmp_project)
        part = got["lineage"]
        assert _s2_chars(part) <= server._DECISION_GET_S2_MAX_CHARS
        [warning] = [w for w in got["warnings"] if w["code"] == "decision_get_truncated"]
        assert warning["level"] == "info"
        assert "30 previous value(s) of the fact core" in warning["message"]
        assert "explanation row(s) without a body" in warning["message"]
        assert part["fact_core"]["previous_values"] == []
        assert len(part["fact_core"]["items"]) == 5
        rows = part["explanations"]["items"]
        assert rows[0]["id"] == f"{ADR_ID}.EX-21" and rows[0]["paragraphs"]
        kept = [row["id"] for row in rows[1:]]
        assert 0 < len(kept) < 19
        assert kept == [f"{ADR_ID}.EX-{n}" for n in range(20, 20 - len(kept), -1)]
        assert part["explanations"]["explanations_total"] == 21
        assert f"{19 - len(kept)} explanation row(s) without a body" in warning["message"]

    def test_the_docstring_names_the_explanations(self):
        doc = " ".join((pm_decision_query.__doc__ or "").split())
        assert "the fact core and explanations (stale ones marked)" in doc
