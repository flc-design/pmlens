"""Decision Lineage S2: feedback, from the pure layer to the tool (PMSERV-231).

What this file pins, by the S2 specification's numbering
(docs/issues/DESIGN_decision-lineage-s2.md):

* The vocabularies (§2.9): the eight feedback kinds, the declared respondent
  scope, and display labels that never say a review took place.
* The argument checks of pm_add_feedback (§3.4), before any file is read.
* The writer (§2.4, §2.7, §3.4, §5.1): numbering ``ADR-n.FB-k`` with
  ``last_number``, the fact core always looked at (a broken one refuses even
  without explanation_id or fact_ids; none is version 0), the feedback and
  explanations sections, check_answer copying only a vocabulary question
  kind, fact ids of the current version, the fixed ``next`` templates (and
  the fixed-ADR one), the warnings table, copy-on-write, D11 for feedback,
  approve / reject never changing the lifecycle or decisions.yaml, no score
  field, no way to upgrade a reply, D7 with threads and processes.
* ``declared_respondent_scope`` (§2.4): counts and a label over the replies to
  the current version, ``mixed`` so one can_judge never hides a cannot_judge,
  undeclared and out-of-vocabulary values counted as unknown.
* The S2 view of the feedback (§2.6, §2.7): rows, caps, broken entries,
  hand-written values.
* The tool (§3.4): the response, the argument checks before the project is
  resolved, the docstring as the spec words it, full mode only; caused_by
  through pm_update_decision (§3.1); get showing the feedback and the
  declared respondent scope, the feedback rows dropped first when the
  budget is passed, and the notice (§3.6).
"""

from __future__ import annotations

import contextlib
import copy
import json
import multiprocessing as mp
import re
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest
import yaml

from pmlens import lineage, server, storage
from pmlens.lineage import (
    FEEDBACK_ID_RE,
    LineageChange,
    RawLineage,
    add_feedback_doc,
    anchor_for,
    declared_respondent_scope,
    derive_lifecycle,
    dump_lineage,
    feedback_call_error,
    lineage_view,
    read_lineage_raw,
)
from pmlens.models import Decision, DecisionStatus, FeedbackKind, RespondentScope
from pmlens.server import pm_add_feedback, pm_decision_query, pm_update_decision
from pmlens.storage import (
    _save_decisions,
    change_decision_lineage,
    record_lineage_explanation,
    record_lineage_feedback,
)

NOW = "2026-10-30T01:00:00Z"
SECRET = "AKIA" + "Z" * 16
ADR_ID = "ADR-900"
OTHER_ID = "ADR-901"
CORE_IDS = ["F-what", "F-change", "F-revert", "F-weak", "F-uncertain"]
SKIPPED = "decision_lineage_items_skipped"
UNREADABLE = "decision_lineage_unreadable"
LIFECYCLE_INFO = "decision_feedback_does_not_change_lifecycle"
NO_REVIEW = "decision_no_review_by_able_person"
DECLARED_CAN_JUDGE = "decision_respondent_declared_can_judge"
KINDS = [kind.value for kind in FeedbackKind]
SCOPES = [scope.value for scope in RespondentScope]
JUDGED = ("approve", "reject", "check_answer")
REVISING = ("premise_mismatch", "what_if", "unacceptable", "try_alternative")
QUESTION = {"question_kind": "premise_check", "fact_ids": ["F-premise.1"], "text": "Still 30?"}
# Words that would make a reply a grade of the person (D8 (e)).
SCORE_WORDS = re.compile(r"score|rating|grade|rank|correct")

BASE: list[dict] = [
    {"kind": "what", "text": "Send the booking mail without the queue"},
    {"kind": "change", "text": "Before: through the queue. After: inside the request"},
    {"kind": "revert", "text": "Revert the setting and deploy again", "reversibility": "unknown"},
    {"kind": "weak", "text": "A slow mail server slows every booking"},
    {"kind": "uncertain", "text": "Peak volume was not measured", "confidence": "medium"},
    {"kind": "premise", "text": "At most 30 users for now"},
]
ALL_IDS = [*CORE_IDS, "F-premise.1"]


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


def _codes(entries: list[dict]) -> list[str]:
    return [entry["code"] for entry in entries]


# ─── Pure-layer helpers ─────────────────────────────────────────────────


def _fact_core(version: int = 1) -> dict:
    """A stored fact core holding ALL_IDS (as the writer stores it)."""
    items = []
    for fact_id in ALL_IDS:
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
        "retired_ids": ["F-alt.1"],
        "previous_values": [],
    }


def _doc(adr: Decision, **overrides: object) -> dict:
    """A lineage as pm_add_decision writes it, with a fact core at version 1."""
    lifecycle = derive_lifecycle(adr.status)
    doc = {
        "schema": 1,
        "decision_id": adr.id,
        "anchor": anchor_for(adr),
        "recorded_at": NOW,
        "declared": {
            "origin": "ai_auto",
            "recorded_timing": "before_impl",
            "decision_kind": "premise_dependent",
        },
        "lifecycle": lifecycle,
        "links": {"supersedes": [], "superseded_by": [], "amends": []},
        "fact_core": _fact_core(),
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


def _explanation(number: int = 1, version: int = 1, question: object = QUESTION) -> dict:
    entry: dict = {
        "id": f"{ADR_ID}.EX-{number}",
        "at": NOW,
        "via": "pm_add_explanation",
        "fact_core_version": version,
        "fact_ids_present": True,
        "paragraphs": [{"fact_ids": [fact_id], "text": "t"} for fact_id in ALL_IDS],
    }
    if question is not None:
        entry["question"] = question
    return entry


def _entry(number: int, version: int = 1, **fields: object) -> dict:
    """A stored feedback entry, as the writer stores it."""
    entry: dict = {
        "id": f"{ADR_ID}.FB-{number}",
        "at": NOW,
        "via": "pm_add_feedback",
        "feedback_kind": "comment",
        "text": f"reply {number}",
        "fact_core_version": version,
        "respondent_scope": "unknown",
    }
    entry.update(fields)
    return entry


def _add(doc: dict, adr: Decision | None = None, **kwargs: object) -> lineage.AppendOutcome:
    kwargs.setdefault("feedback_kind", "comment")
    kwargs.setdefault("text", "The user said so")
    return add_feedback_doc(doc, adr or _adr(), NOW, **kwargs)


def _refused(doc: dict, code: str, **kwargs: object) -> dict:
    before = copy.deepcopy(doc)
    outcome = _add(doc, **kwargs)
    assert outcome.error is not None and outcome.error["code"] == code, outcome.error
    assert outcome.doc is None and doc == before
    return outcome.error


def _hand_view(**overrides: object) -> lineage.LineageView:
    adr = _adr()
    doc = _doc(adr, **overrides)
    return lineage_view(adr, RawLineage(ADR_ID, exists=True, data=doc), include_s2=True)


# ─── Storage helpers ────────────────────────────────────────────────────


def _seed(pm_path: Path) -> Decision:
    adr = _adr()
    _save_decisions(pm_path, [adr, _adr(DecisionStatus.ACCEPTED, OTHER_ID)])
    return adr


def _path(pm_path: Path, adr_id: str = ADR_ID) -> Path:
    return pm_path / "decision_lineage" / f"{adr_id}.yaml"


def _load(pm_path: Path, adr_id: str = ADR_ID) -> dict:
    return yaml.safe_load(_path(pm_path, adr_id).read_text(encoding="utf-8"))


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


def _revise(pm_path: Path, items: list[dict], expected: int) -> None:
    result = change_decision_lineage(
        pm_path, ADR_ID, LineageChange(fact_core=items, expected_fact_core_version=expected)
    )
    assert result.status == "updated", result.error


def _at_version_one(pm_path: Path) -> Decision:
    """ADR-900 with its fact core at version 1, a premise included."""
    adr = _seed(pm_path)
    _revise(pm_path, [dict(item) for item in BASE], 0)
    return adr


def _explained(pm_path: Path, question: object = QUESTION) -> str:
    result = record_lineage_explanation(
        pm_path,
        ADR_ID,
        fact_core_version=1,
        paragraphs=[{"fact_ids": [fact_id], "text": f"about {fact_id}"} for fact_id in ALL_IDS],
        question=question,
    )
    assert result.status == "recorded", result.error
    return result.fields["explanation_id"]


def _feedback(
    pm_path: Path, kind: str = "comment", text: str = "The user said so", **kwargs: object
) -> storage.LineageAppendResult:
    kwargs.setdefault("decision_id", ADR_ID)
    decision_id = kwargs.pop("decision_id")
    return record_lineage_feedback(pm_path, decision_id, feedback_kind=kind, text=text, **kwargs)


def _recorded(
    pm_path: Path, kind: str = "comment", **kwargs: object
) -> storage.LineageAppendResult:
    result = _feedback(pm_path, kind, **kwargs)
    assert result.status == "recorded", result.error
    return result


def _private_key(label: str) -> str:
    body = "\n".join(["Zm9v" * 16] * 5)
    return f"-----BEGIN {label}-----\n{body}\n-----END {label}-----"


def _view(pm_path: Path, adr: Decision) -> lineage.LineageView:
    return lineage_view(adr, read_lineage_raw(pm_path, ADR_ID), include_s2=True)


# ─── Vocabularies (S2 spec §2.9) ────────────────────────────────────────


class TestVocabulary:
    def test_the_feedback_kinds(self):
        assert KINDS == [
            "premise_mismatch",
            "what_if",
            "unacceptable",
            "try_alternative",
            "approve",
            "reject",
            "comment",
            "check_answer",
        ]
        assert lineage.FEEDBACK_KINDS == tuple(KINDS)

    def test_the_declared_scopes_and_their_labels(self):
        assert SCOPES == ["can_judge", "cannot_judge", "unknown"]
        assert lineage.RESPONDENT_SCOPES == tuple(SCOPES)
        assert lineage.review_label("can_judge") == "respondent_declared_can_judge"
        assert lineage.review_label("cannot_judge") == "no_review_by_able_person"
        assert lineage.review_label("unknown") == "no_review_by_able_person"
        # No label says a review took place (D8 (a), S2 spec §2.4).
        for label in (
            lineage.REVIEW_DECLARED_CAN_JUDGE,
            lineage.REVIEW_NONE,
            lineage.SCOPE_LABEL_NONE,
            lineage.SCOPE_LABEL_MIXED,
            *SCOPES,
        ):
            assert not re.search(r"confirm|verif|human|accura", label), label

    @pytest.mark.parametrize("value", ["ADR-900.FB-1", "ADR-1.FB-999999", "ADR-000123.FB-10"])
    def test_feedback_ids(self, value: str):
        assert FEEDBACK_ID_RE.fullmatch(value)

    @pytest.mark.parametrize(
        "value",
        ["ADR-900.FB-0", "ADR-900.FB-1000000", "ADR-900.FB-01", "ADR-900.EX-1", "ADR-900.FB-1\n"],
    )
    def test_not_feedback_ids(self, value: str):
        assert FEEDBACK_ID_RE.fullmatch(value) is None


# ─── The arguments (S2 spec §3.4) ───────────────────────────────────────


def _call(**kwargs: object) -> dict | None:
    kwargs.setdefault("decision_id", ADR_ID)
    kwargs.setdefault("feedback_kind", "comment")
    kwargs.setdefault("text", "The user said so")
    return feedback_call_error(**kwargs)


class TestArguments:
    @pytest.mark.parametrize("kind", KINDS)
    def test_every_kind_is_accepted(self, kind: str):
        extra = {"explanation_id": f"{ADR_ID}.EX-1"} if kind == "check_answer" else {}
        assert _call(feedback_kind=kind, **extra) is None

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"respondent_scope": "can_judge"},
            {"fact_ids": []},
            {"fact_ids": [f"F-alt.{n}" for n in range(1, 11)]},
            {"explanation_id": f"{ADR_ID}.EX-12"},
            {"text": "x" * lineage.MAX_TEXT_CHARS},
        ],
    )
    def test_accepted_values(self, kwargs: dict):
        assert _call(**kwargs) is None

    @pytest.mark.parametrize(
        ("kwargs", "code"),
        [
            ({"decision_id": f"ADR-{SECRET}"}, "invalid_decision_id"),
            ({"feedback_kind": SECRET}, "invalid_feedback_kind"),
            ({"feedback_kind": "Approve"}, "invalid_feedback_kind"),
            ({"feedback_kind": "score"}, "invalid_feedback_kind"),
            ({"feedback_kind": None}, "invalid_feedback_kind"),
            ({"respondent_scope": SECRET}, "invalid_respondent_scope"),
            ({"respondent_scope": "verified"}, "invalid_respondent_scope"),
            ({"respondent_scope": None}, "invalid_respondent_scope"),
            ({"text": ""}, "feedback_text_required"),
            ({"text": " \n "}, "feedback_text_required"),
            ({"text": None}, "feedback_text_required"),
            ({"text": b"bytes"}, "feedback_text_required"),
            ({"text": SECRET * 300}, "text_too_long"),
            ({"explanation_id": f"{OTHER_ID}.EX-1"}, "invalid_explanation_id"),
            ({"explanation_id": f"{ADR_ID}.FB-1"}, "invalid_explanation_id"),
            ({"explanation_id": f"{ADR_ID}.EX-{SECRET}"}, "invalid_explanation_id"),
            ({"explanation_id": 5}, "invalid_explanation_id"),
            ({"fact_ids": "F-what"}, "invalid_fact_ids"),
            ({"fact_ids": ["F-what", "F-what"]}, "invalid_fact_ids"),
            ({"fact_ids": [f"F-{SECRET}"]}, "invalid_fact_ids"),
            ({"fact_ids": [f"F-alt.{n}" for n in range(1, 12)]}, "invalid_fact_ids"),
            ({"feedback_kind": "check_answer"}, "check_answer_without_question"),
        ],
    )
    def test_refused_without_quoting_the_value(self, kwargs: dict, code: str):
        error = _call(**kwargs)
        assert error is not None and error["code"] == code, error
        assert SECRET not in json.dumps(error)

    def test_text_too_long_names_its_field(self):
        error = _call(text="x" * (lineage.MAX_TEXT_CHARS + 1))
        assert error["field"] == "text"

    def test_the_order_of_the_checks(self):
        # Vocabularies before the text, the text before the ids (S2 spec §3.4).
        assert _call(feedback_kind="x", text="")["code"] == "invalid_feedback_kind"
        assert _call(respondent_scope="x", text="")["code"] == "invalid_respondent_scope"
        assert _call(text="", explanation_id="x")["code"] == "feedback_text_required"
        assert _call(explanation_id="x", fact_ids="x")["code"] == "invalid_explanation_id"
        error = _call(feedback_kind="check_answer", fact_ids="x")
        assert error["code"] == "invalid_fact_ids"


# ─── The writer, pure (S2 spec §2.4, §2.7, §3.4) ────────────────────────


class TestPureWriter:
    def test_an_entry_is_appended(self):
        adr = _adr()
        doc = _doc(adr)
        before = copy.deepcopy(doc)
        outcome = _add(
            doc,
            feedback_kind="premise_mismatch",
            text="Next year it will be about 30",
            fact_ids=["F-premise.1"],
            respondent_scope="can_judge",
        )
        assert outcome.error is None
        assert doc == before  # nothing changed in place
        new = outcome.doc
        assert new["feedback"] == {
            "last_number": 1,
            "items": [
                {
                    "id": "ADR-900.FB-1",
                    "at": NOW,
                    "via": "pm_add_feedback",
                    "feedback_kind": "premise_mismatch",
                    "text": "Next year it will be about 30",
                    "fact_core_version": 1,
                    "fact_ids": ["F-premise.1"],
                    "respondent_scope": "can_judge",
                }
            ],
        }
        # The section goes before the events, and no event is added (S2 spec §2.5).
        assert list(new)[-2:] == ["feedback", "events"]
        assert new["events"] == before["events"]
        assert new["lifecycle"] == before["lifecycle"]
        assert new["fact_core"] == before["fact_core"]
        assert outcome.fields == {
            "feedback_id": "ADR-900.FB-1",
            "feedback_kind": "premise_mismatch",
            "fact_core_version": 1,
            "respondent_scope": "can_judge",
            "review": "respondent_declared_can_judge",
            "next": (
                "If the premise is wrong, revise the fact core with pm_update_decision "
                '(fact_core, expected_fact_core_version=1, caused_by=["ADR-900.FB-1"]) and '
                "explain the new version."
            ),
        }
        assert outcome.notices == []

    def test_the_stored_keys_carry_no_score(self):
        doc = _doc(_adr(), explanations={"items": [_explanation()]})
        outcome = _add(
            doc,
            feedback_kind="check_answer",
            explanation_id="ADR-900.EX-1",
            fact_ids=["F-what"],
            respondent_scope="cannot_judge",
        )
        entry = outcome.doc["feedback"]["items"][0]
        assert set(entry) == {
            "id",
            "at",
            "via",
            "feedback_kind",
            "text",
            "fact_core_version",
            "explanation_id",
            "fact_ids",
            "respondent_scope",
            "question_kind",
        }
        for name in [*entry, *outcome.fields, *outcome.doc["feedback"]]:
            assert not SCORE_WORDS.search(name), name
        # The answer is kept as said; nothing is derived from it.
        assert entry["text"] == "The user said so"

    def test_without_a_fact_core_the_version_is_zero(self):
        doc = _doc(_adr())
        del doc["fact_core"]
        outcome = _add(doc, feedback_kind="approve")
        assert outcome.doc["feedback"]["items"][0]["fact_core_version"] == 0
        assert outcome.fields["fact_core_version"] == 0
        assert "fact_core" not in outcome.doc

    @pytest.mark.parametrize(
        "fact_core",
        [
            {**_fact_core(), "version": "1"},
            {**_fact_core(), "version": 0},
            {**_fact_core(), "version": True},
            {**_fact_core(), "items": "not a list"},
            {**_fact_core(), "items": _fact_core()["items"][1:]},  # no F-what
            ["not", "a", "mapping"],
        ],
    )
    def test_a_broken_fact_core_refuses_every_call(self, fact_core: object):
        # Even without explanation_id or fact_ids: the version recorded with
        # the reply could not be told (S2 spec §2.4).
        doc = _doc(_adr(), fact_core=fact_core)
        error = _refused(doc, UNREADABLE, feedback_kind="approve")
        assert "fact_core" in error["message"]
        assert "feedback cannot be written" in error["message"]

    def test_numbers_follow_last_number_and_the_items(self):
        doc = _doc(_adr(), feedback={"last_number": 7, "items": [_entry(2)]})
        assert _add(doc).fields["feedback_id"] == "ADR-900.FB-8"
        doc = _doc(_adr(), feedback={"last_number": 1, "items": [_entry(4)]})
        outcome = _add(doc)
        assert outcome.fields["feedback_id"] == "ADR-900.FB-5"
        assert outcome.doc["feedback"]["last_number"] == 5
        # Items moved out by hand leave the number behind (never reused).
        doc = _doc(_adr(), feedback={"last_number": 3, "items": []})
        assert _add(doc).fields["feedback_id"] == "ADR-900.FB-4"

    @pytest.mark.parametrize("last", ["3", 3.0, True, -1, None])
    def test_a_broken_last_number_is_refused(self, last: object):
        doc = _doc(_adr(), feedback={"last_number": last, "items": []})
        error = _refused(doc, UNREADABLE)
        assert "feedback.last_number" in error["message"]

    def test_numbers_run_out(self):
        doc = _doc(_adr(), feedback={"last_number": 999_999, "items": []})
        error = _refused(doc, "feedback_ids_exhausted")
        assert "999999" in error["message"]
        doc = _doc(_adr(), feedback={"items": [_entry(999_999)]})
        _refused(doc, "feedback_ids_exhausted")

    @pytest.mark.parametrize(
        ("feedback", "where"),
        [
            ([SECRET], "feedback"),
            ({"items": SECRET}, "feedback.items"),
            ({"last_number": 1}, "feedback.items"),
            ({"items": [SECRET]}, "entry 1 of feedback.items"),
            ({"items": [_entry(1), {**_entry(2), "id": f"ADR-900.FB-{SECRET}"}]}, "entry 2"),
            ({"items": [{**_entry(1), "id": "ADR-900.EX-1"}]}, "entry 1"),
            ({"items": [{**_entry(1), "fact_core_version": "1"}]}, "entry 1"),
            ({"items": [{**_entry(1), "fact_core_version": -1}]}, "entry 1"),
            ({"items": [{**_entry(1), "fact_core_version": False}]}, "entry 1"),
        ],
    )
    def test_a_broken_feedback_section_is_refused(self, feedback: object, where: str):
        error = _refused(_doc(_adr(), feedback=feedback), UNREADABLE)
        assert where in error["message"]
        assert SECRET not in json.dumps(error)

    def test_the_explanation_must_be_on_record(self):
        doc = _doc(_adr(), explanations={"items": [_explanation(1)]})
        error = _refused(doc, "explanation_not_found", explanation_id="ADR-900.EX-2")
        assert "ADR-900.EX-2" in error["message"]
        _refused(_doc(_adr()), "explanation_not_found", explanation_id="ADR-900.EX-1")
        outcome = _add(doc, explanation_id="ADR-900.EX-1")
        assert outcome.doc["feedback"]["items"][0]["explanation_id"] == "ADR-900.EX-1"

    @pytest.mark.parametrize(
        "explanations",
        [[SECRET], {"items": SECRET}, {"items": [{**_explanation(), "id": SECRET}]}],
    )
    def test_the_explanations_are_looked_at_only_for_an_explanation_id(self, explanations: object):
        doc = _doc(_adr(), explanations=explanations)
        error = _refused(doc, UNREADABLE, explanation_id="ADR-900.EX-1")
        assert "explanations" in error["message"]
        assert SECRET not in json.dumps(error)
        assert _add(doc).error is None

    def test_the_explanations_last_number_is_not_the_feedback_writers_concern(self):
        doc = _doc(_adr(), explanations={"last_number": "x", "items": [_explanation()]})
        assert _add(doc, explanation_id="ADR-900.EX-1").error is None

    @pytest.mark.parametrize("kind", ["judgment", "premise_check", "tech_review"])
    def test_check_answer_copies_the_question_kind(self, kind: str):
        question = {**QUESTION, "question_kind": kind}
        doc = _doc(_adr(), explanations={"items": [_explanation(question=question)]})
        outcome = _add(doc, feedback_kind="check_answer", explanation_id="ADR-900.EX-1")
        entry = outcome.doc["feedback"]["items"][0]
        assert entry["question_kind"] == kind
        # The vocabulary's own string, not the one loaded from the file.
        assert entry["question_kind"] is next(k for k in lineage.QUESTION_KINDS if k == kind)
        assert outcome.fields["next"] is None

    @pytest.mark.parametrize(
        "question",
        [
            None,
            "not a mapping",
            {**QUESTION, "question_kind": "judgeable_kinds"},
            {**QUESTION, "question_kind": f"judgment {SECRET}"},
            {**QUESTION, "question_kind": b"judgment"},
            {**QUESTION, "question_kind": {"judgment": 1}},
            {key: value for key, value in QUESTION.items() if key != "question_kind"},
        ],
    )
    def test_check_answer_needs_a_readable_question(self, question: object):
        doc = _doc(_adr(), explanations={"items": [_explanation(question=question)]})
        error = _refused(
            doc,
            "check_answer_without_question",
            feedback_kind="check_answer",
            explanation_id="ADR-900.EX-1",
        )
        assert SECRET not in json.dumps(error) and "judgeable" not in json.dumps(error)
        # Another kind may still name that explanation; nothing is copied then.
        outcome = _add(doc, feedback_kind="comment", explanation_id="ADR-900.EX-1")
        assert "question_kind" not in outcome.doc["feedback"]["items"][0]

    def test_the_newest_explanation_with_that_id_is_answered(self):
        hand = [_explanation(question=None), _explanation(question=QUESTION)]
        doc = _doc(_adr(), explanations={"items": hand})
        outcome = _add(doc, feedback_kind="check_answer", explanation_id="ADR-900.EX-1")
        assert outcome.doc["feedback"]["items"][0]["question_kind"] == "premise_check"

    def test_fact_ids_must_be_current(self):
        doc = _doc(_adr())
        error = _refused(
            doc, "feedback_fact_ids_unknown", fact_ids=["F-what", "F-alt.1", "F-premise.2"]
        )
        # F-alt.1 is retired, F-premise.2 never existed.
        assert error["unknown_fact_ids"] == ["F-alt.1", "F-premise.2"]
        assert error["current_version"] == 1
        no_core = _doc(_adr())
        del no_core["fact_core"]
        error = _refused(no_core, "feedback_fact_ids_unknown", fact_ids=["F-what"])
        assert error["current_version"] == 0

    def test_the_order_of_the_refusals(self):
        # fact core -> feedback -> explanations -> question -> fact ids -> numbering.
        broken = {**_fact_core(), "version": "1"}
        everything = {
            "fact_core": broken,
            "feedback": {"last_number": "x", "items": []},
            "explanations": "broken",
        }
        doc = _doc(_adr(), **everything)
        error = _refused(doc, UNREADABLE, explanation_id="ADR-900.EX-1")
        assert "fact_core.version" in error["message"]
        doc = _doc(_adr(), **{**everything, "fact_core": _fact_core()})
        assert "feedback.last_number" in _refused(doc, UNREADABLE)["message"]
        doc = _doc(_adr(), explanations="broken", feedback={"last_number": 999_999, "items": []})
        error = _refused(doc, UNREADABLE, explanation_id="ADR-900.EX-1")
        assert "explanations" in error["message"]
        doc = _doc(
            _adr(),
            explanations={"items": [_explanation(question=None)]},
            feedback={"last_number": 999_999, "items": []},
        )
        _refused(
            doc,
            "check_answer_without_question",
            feedback_kind="check_answer",
            explanation_id="ADR-900.EX-1",
            fact_ids=["F-alt.9"],
        )
        _refused(doc, "feedback_fact_ids_unknown", fact_ids=["F-alt.9"])
        _refused(doc, "feedback_ids_exhausted")

    def test_an_aliased_section_keeps_its_value_elsewhere(self):
        section = {"last_number": 1, "items": [_entry(1)]}
        doc = _doc(_adr(), feedback=section, mirror=section, mirror_items=section["items"])
        outcome = _add(doc)
        new = outcome.doc
        assert [e["id"] for e in new["feedback"]["items"]] == ["ADR-900.FB-1", "ADR-900.FB-2"]
        assert new["mirror"] == {"last_number": 1, "items": [_entry(1)]}
        assert new["mirror_items"] == [_entry(1)]
        assert new["mirror"] is section and new["feedback"] is not section

    def test_a_missing_anchor_is_added_back(self):
        doc = _doc(_adr())
        del doc["anchor"]
        outcome = _add(doc)
        assert outcome.doc["anchor"] == anchor_for(_adr())
        assert _codes(outcome.notices) == ["decision_lineage_anchor_missing"]


# ─── The warnings and next (S2 spec §3.4) ───────────────────────────────


def _expected_codes(kind: str, scope: str) -> list[str]:
    codes = [LIFECYCLE_INFO] if kind in ("approve", "reject") else []
    if kind in JUDGED:
        codes.append(DECLARED_CAN_JUDGE if scope == "can_judge" else NO_REVIEW)
    return codes


class TestWarningsAndNext:
    @pytest.mark.parametrize("scope", SCOPES)
    @pytest.mark.parametrize("kind", KINDS)
    def test_the_warnings_of_each_kind_and_scope(self, kind: str, scope: str):
        doc = _doc(_adr(), explanations={"items": [_explanation()]})
        outcome = _add(
            doc, feedback_kind=kind, respondent_scope=scope, explanation_id="ADR-900.EX-1"
        )
        assert _codes(outcome.notices) == _expected_codes(kind, scope)
        assert {notice["level"] for notice in outcome.notices} <= {"info"}
        assert outcome.fields["review"] == (
            "respondent_declared_can_judge" if scope == "can_judge" else "no_review_by_able_person"
        )
        # The lifecycle and the events never change, approve and reject included.
        assert outcome.doc["lifecycle"] == "proposed"
        assert outcome.doc["events"] == doc["events"]

    def test_the_declared_can_judge_notice_comes_every_time(self):
        doc = _doc(_adr())
        for _ in range(3):
            outcome = _add(doc, feedback_kind="approve", respondent_scope="can_judge")
            assert DECLARED_CAN_JUDGE in _codes(outcome.notices)
            notice = next(n for n in outcome.notices if n["code"] == DECLARED_CAN_JUDGE)
            assert "has not checked" in notice["message"]
            doc = outcome.doc

    def test_the_lifecycle_notice_names_the_lifecycle_and_points_to_the_user(self):
        adr = _adr(DecisionStatus.ACCEPTED)
        outcome = _add(_doc(adr), adr, feedback_kind="reject")
        notice = next(n for n in outcome.notices if n["code"] == LIFECYCLE_INFO)
        assert "still adopted" in notice["message"]
        assert "pm_update_decision" in notice["remediation"]

    def test_the_next_templates(self):
        doc = _doc(
            _adr(),
            feedback={"last_number": 4, "items": []},
            explanations={"items": [_explanation()]},
        )
        calls = (
            "pm_update_decision (fact_core, expected_fact_core_version=1, "
            'caused_by=["ADR-900.FB-5"])'
        )
        texts = {}
        for kind in KINDS:
            extra = {"explanation_id": "ADR-900.EX-1"} if kind == "check_answer" else {}
            texts[kind] = _add(doc, feedback_kind=kind, **extra).fields["next"]
        for kind in REVISING:
            assert calls in texts[kind], kind
        assert texts["approve"].startswith("Approval recorded here does not adopt the ADR.")
        assert "lifecycle=adopted" in texts["approve"]
        assert texts["reject"].startswith("Rejection recorded here does not reject the ADR.")
        assert texts["comment"] is None and texts["check_answer"] is None

    def test_the_next_never_carries_the_text_or_file_values(self):
        doc = _doc(_adr(), declared={"decision_kind": SECRET}, fact_core=_fact_core(version=3))
        for kind in REVISING:
            outcome = _add(doc, feedback_kind=kind, text=f"words {SECRET}")
            assert SECRET not in outcome.fields["next"]
            assert "words" not in outcome.fields["next"]
            assert "expected_fact_core_version=3" in outcome.fields["next"]

    def test_version_zero_points_to_the_first_version(self):
        doc = _doc(_adr())
        del doc["fact_core"]
        outcome = _add(doc, feedback_kind="what_if")
        assert "expected_fact_core_version=0" in outcome.fields["next"]

    @pytest.mark.parametrize("lifecycle", ["adopted", "deprecated", "superseded", "reverted"])
    def test_a_fixed_fact_core_is_not_pointed_at(self, lifecycle: str):
        adr = _adr(DecisionStatus.ACCEPTED)
        doc = _doc(adr, lifecycle=lifecycle)
        doc["events"].append(
            {
                "at": NOW,
                "kind": "lifecycle",
                "from": "proposed",
                "to": "adopted",
                "fact_core_version": 1,
            }
        )
        for kind in REVISING:
            outcome = _add(doc, adr, feedback_kind=kind)
            assert outcome.error is None
            assert outcome.fields["next"] == lineage._FEEDBACK_NEXT_FIXED
            assert "fact_core," not in outcome.fields["next"]
            assert "only if the user asks" in outcome.fields["next"]
            assert "amends" in outcome.fields["next"]
        # Approval is not about a revision, so its next is unchanged.
        approve = _add(doc, adr, feedback_kind="approve").fields["next"]
        assert approve.startswith("Approval recorded here")

    def test_a_malformed_adopted_version_is_treated_as_fixed(self):
        adr = _adr(DecisionStatus.ACCEPTED)
        doc = _doc(adr, lifecycle="adopted")
        doc["events"].append(
            {"at": NOW, "kind": "lifecycle", "to": "adopted", "fact_core_version": "1"}
        )
        outcome = _add(doc, adr, feedback_kind="premise_mismatch")
        assert outcome.fields["next"] == lineage._FEEDBACK_NEXT_FIXED

    def test_moved_back_to_proposed_points_to_a_revision_again(self):
        doc = _doc(_adr(), lifecycle="proposed")
        doc["events"] += [
            {"at": NOW, "kind": "lifecycle", "to": "adopted", "fact_core_version": 1},
            {"at": NOW, "kind": "lifecycle", "from": "adopted", "to": "proposed"},
        ]
        outcome = _add(doc, feedback_kind="premise_mismatch")
        assert "caused_by=" in outcome.fields["next"]

    def test_adopted_without_a_recorded_version_points_to_a_revision(self):
        # Facts written after an adoption stay editable (S2 spec §2.2).
        adr = _adr(DecisionStatus.ACCEPTED)
        doc = _doc(adr)
        outcome = _add(doc, adr, feedback_kind="try_alternative")
        assert "caused_by=" in outcome.fields["next"]


# ─── declared_respondent_scope (S2 spec §2.4) ───────────────────────────


def _scope_of(*entries: dict, fact_core: object = None, **section: object) -> dict:
    doc = _doc(_adr(), feedback={"items": list(entries), **section})
    if fact_core is not None:
        doc["fact_core"] = fact_core
    return declared_respondent_scope(doc)


def _counts(can: int = 0, cannot: int = 0, unknown: int = 0) -> dict:
    return {"can_judge": can, "cannot_judge": cannot, "unknown": unknown}


class TestDeclaredRespondentScope:
    def test_none_without_replies(self):
        assert _scope_of() == {"label": "none", "counts": _counts()}
        doc = _doc(_adr())
        assert declared_respondent_scope(doc) == {"label": "none", "counts": _counts()}

    def test_every_reply_declared_able_to_judge(self):
        scope = _scope_of(
            _entry(1, feedback_kind="approve", respondent_scope="can_judge"),
            _entry(2, feedback_kind="check_answer", respondent_scope="can_judge"),
        )
        assert scope == {"label": "respondent_declared_can_judge", "counts": _counts(can=2)}

    @pytest.mark.parametrize("declared", ["cannot_judge", "unknown"])
    def test_no_reply_declared_able_to_judge(self, declared: str):
        scope = _scope_of(_entry(1, feedback_kind="approve", respondent_scope=declared))
        assert scope["label"] == "no_review_by_able_person"
        assert scope["label"] != "respondent_declared_can_judge"

    def test_a_later_can_judge_does_not_hide_a_cannot_judge(self):
        scope = _scope_of(
            _entry(1, feedback_kind="reject", respondent_scope="cannot_judge"),
            _entry(2, feedback_kind="approve", respondent_scope="can_judge"),
        )
        assert scope == {"label": "mixed", "counts": _counts(can=1, cannot=1)}

    @pytest.mark.parametrize("declared", ["expert", "CAN_JUDGE", b"can_judge", ["can_judge"], 1])
    def test_values_out_of_vocabulary_count_as_unknown(self, declared: object):
        alone = _scope_of(_entry(1, feedback_kind="approve", respondent_scope=declared))
        assert alone == {"label": "no_review_by_able_person", "counts": _counts(unknown=1)}
        beside = _scope_of(
            _entry(1, feedback_kind="approve", respondent_scope=declared),
            _entry(2, feedback_kind="approve", respondent_scope="can_judge"),
        )
        assert beside == {"label": "mixed", "counts": _counts(can=1, unknown=1)}

    def test_a_missing_declaration_counts_as_unknown(self):
        entry = _entry(1, feedback_kind="reject")
        del entry["respondent_scope"]
        scope = _scope_of(entry, _entry(2, feedback_kind="approve", respondent_scope="can_judge"))
        assert scope == {"label": "mixed", "counts": _counts(can=1, unknown=1)}

    def test_only_replies_that_answer_for_the_decision_count(self):
        entries = [
            _entry(n, feedback_kind=kind, respondent_scope="can_judge")
            for n, kind in enumerate(KINDS, 1)
        ]
        entries.append(_entry(99, feedback_kind=SECRET, respondent_scope="cannot_judge"))
        scope = _scope_of(*entries)
        assert scope == {"label": "respondent_declared_can_judge", "counts": _counts(can=3)}

    def test_replies_to_an_older_version_are_not_counted(self):
        scope = _scope_of(
            _entry(1, version=1, feedback_kind="reject", respondent_scope="cannot_judge"),
            _entry(2, version=2, feedback_kind="approve", respondent_scope="can_judge"),
            fact_core=_fact_core(version=2),
        )
        assert scope == {"label": "respondent_declared_can_judge", "counts": _counts(can=1)}

    def test_without_a_fact_core_version_zero_counts(self):
        doc = _doc(
            _adr(),
            feedback={
                "items": [
                    _entry(1, version=0, feedback_kind="approve"),
                    _entry(2, version=1, feedback_kind="approve", respondent_scope="can_judge"),
                ]
            },
        )
        del doc["fact_core"]
        assert declared_respondent_scope(doc)["counts"] == _counts(unknown=1)

    def test_nothing_is_counted_while_the_fact_core_cannot_be_read(self):
        scope = _scope_of(
            _entry(1, feedback_kind="approve", respondent_scope="can_judge"),
            fact_core={**_fact_core(), "version": "1"},
        )
        assert scope == {"label": "none", "counts": _counts()}

    def test_broken_entries_and_sections_are_not_counted(self):
        scope = _scope_of(
            SECRET,
            {**_entry(1, feedback_kind="approve"), "id": "nope"},
            _entry(2, feedback_kind="approve", respondent_scope="cannot_judge"),
        )
        assert scope["counts"] == _counts(cannot=1)
        doc = _doc(_adr(), feedback=[SECRET])
        assert declared_respondent_scope(doc)["label"] == "none"

    def test_the_scan_is_capped(self):
        approve = _entry(1, feedback_kind="approve", respondent_scope="can_judge")
        reject = _entry(2, feedback_kind="reject", respondent_scope="cannot_judge")
        scope = _scope_of(reject, *([approve] * lineage.SCAN_FEEDBACK))
        # The oldest reply is past the scan; the cap is said.
        assert scope == {
            "label": "respondent_declared_can_judge",
            "counts": _counts(can=lineage.SCAN_FEEDBACK),
            "counts_capped": True,
        }


# ─── Through the storage layer (S2 spec §3.4, §5.1) ─────────────────────


class TestThroughStorage:
    def test_a_reply_is_recorded(self, tmp_pm_path: Path):
        _at_version_one(tmp_pm_path)
        decisions = (tmp_pm_path / "decisions.yaml").read_bytes()
        before = _load(tmp_pm_path)
        result = _recorded(tmp_pm_path, "approve", respondent_scope="cannot_judge")
        assert result.decision_id == ADR_ID
        assert result.fields == {
            "feedback_id": "ADR-900.FB-1",
            "feedback_kind": "approve",
            "fact_core_version": 1,
            "respondent_scope": "cannot_judge",
            "review": "no_review_by_able_person",
            "next": lineage.feedback_next("approve", "ADR-900.FB-1", 1, fixed=False),
        }
        assert _codes(result.warnings) == [LIFECYCLE_INFO, NO_REVIEW]
        doc = _load(tmp_pm_path)
        assert doc["feedback"]["items"][0]["id"] == "ADR-900.FB-1"
        # The lifecycle, the events, the fact core and decisions.yaml are left alone.
        assert doc["lifecycle"] == before["lifecycle"] == "proposed"
        assert doc["events"] == before["events"]
        assert doc["fact_core"] == before["fact_core"]
        assert (tmp_pm_path / "decisions.yaml").read_bytes() == decisions
        assert _recorded(tmp_pm_path, "reject").fields["feedback_id"] == "ADR-900.FB-2"
        assert (tmp_pm_path / "decisions.yaml").read_bytes() == decisions
        assert _load(tmp_pm_path)["lifecycle"] == "proposed"

    def test_a_lineage_is_started_for_an_adr_without_one(self, tmp_pm_path: Path):
        _seed(tmp_pm_path)
        decisions = (tmp_pm_path / "decisions.yaml").read_bytes()
        result = _recorded(tmp_pm_path, "comment", decision_id=OTHER_ID)
        assert result.fields["feedback_id"] == "ADR-901.FB-1"
        assert result.fields["fact_core_version"] == 0
        assert _codes(result.warnings) == ["decision_lineage_started"]
        doc = _load(tmp_pm_path, OTHER_ID)
        assert doc["lifecycle"] == "adopted"
        assert [e["kind"] for e in doc["events"]] == ["lineage_started"]
        assert doc["events"][0]["via"] == "pm_add_feedback"
        assert doc["feedback"]["items"][0]["fact_core_version"] == 0
        assert (tmp_pm_path / "decisions.yaml").read_bytes() == decisions

    def test_a_lineage_cannot_be_started_from_an_unknown_status(self, tmp_pm_path: Path):
        (tmp_pm_path / "decisions.yaml").write_text(
            f"decisions:\n- id: {ADR_ID}\n  title: t\n  status: archived\n", encoding="utf-8"
        )
        before = _snapshot(tmp_pm_path)
        result = _feedback(tmp_pm_path)
        assert result.error["code"] == "decision_status_unknown"
        assert _snapshot(tmp_pm_path) == before

    @pytest.mark.parametrize(
        ("kwargs", "code"),
        [
            ({"kind": "score"}, "invalid_feedback_kind"),
            ({"respondent_scope": "verified"}, "invalid_respondent_scope"),
            ({"text": " "}, "feedback_text_required"),
            ({"text": "x" * 4_001}, "text_too_long"),
            ({"explanation_id": f"{OTHER_ID}.EX-1"}, "invalid_explanation_id"),
            ({"fact_ids": ["F-what"] * 2}, "invalid_fact_ids"),
            ({"kind": "check_answer"}, "check_answer_without_question"),
            ({"explanation_id": f"{ADR_ID}.EX-9"}, "explanation_not_found"),
            (
                {"kind": "check_answer", "explanation_id": f"{ADR_ID}.EX-2"},
                "check_answer_without_question",
            ),
            ({"fact_ids": ["F-alt.1"]}, "feedback_fact_ids_unknown"),
            ({"decision_id": "ADR-777"}, "decision_not_found"),
            ({"decision_id": "not an id"}, "invalid_decision_id"),
        ],
    )
    def test_errors_leave_the_files_alone(self, tmp_pm_path: Path, kwargs: dict, code: str):
        _at_version_one(tmp_pm_path)
        _explained(tmp_pm_path)
        _explained(tmp_pm_path, question=None)
        _recorded(tmp_pm_path)
        before = _snapshot(tmp_pm_path)
        result = _feedback(tmp_pm_path, **kwargs)
        assert result.status == "error" and result.error["code"] == code, result.error
        assert _snapshot(tmp_pm_path) == before

    def test_check_answer_through_storage(self, tmp_pm_path: Path):
        _at_version_one(tmp_pm_path)
        explanation = _explained(tmp_pm_path)
        result = _recorded(
            tmp_pm_path,
            "check_answer",
            text="It is still about 30",
            explanation_id=explanation,
            respondent_scope="can_judge",
        )
        assert _codes(result.warnings) == [DECLARED_CAN_JUDGE]
        assert result.fields["next"] is None
        entry = _load(tmp_pm_path)["feedback"]["items"][0]
        assert entry["question_kind"] == "premise_check"
        assert entry["explanation_id"] == "ADR-900.EX-1"

    def test_a_question_kind_written_by_hand_is_never_copied(self, tmp_pm_path: Path):
        _at_version_one(tmp_pm_path)
        _explained(tmp_pm_path)
        doc = _load(tmp_pm_path)
        doc["explanations"]["items"][0]["question"]["question_kind"] = f"judgment {SECRET}"
        _store(tmp_pm_path, doc)
        before = _snapshot(tmp_pm_path)
        result = _feedback(tmp_pm_path, "check_answer", explanation_id="ADR-900.EX-1")
        assert result.error["code"] == "check_answer_without_question"
        assert SECRET not in json.dumps(result.error)
        assert _snapshot(tmp_pm_path) == before

    def test_a_broken_fact_core_refuses_a_bare_reply(self, tmp_pm_path: Path):
        _at_version_one(tmp_pm_path)
        doc = _load(tmp_pm_path)
        doc["fact_core"]["version"] = "1"
        _store(tmp_pm_path, doc)
        before = _snapshot(tmp_pm_path)
        result = _feedback(tmp_pm_path, "approve")
        assert result.error["code"] == UNREADABLE
        assert "fact_core.version" in result.error["message"]
        assert _snapshot(tmp_pm_path) == before

    def test_an_adr_without_a_fact_core_records_version_zero(self, tmp_pm_path: Path):
        _seed(tmp_pm_path)
        change_decision_lineage(tmp_pm_path, ADR_ID, LineageChange(note="a note"))
        result = _recorded(tmp_pm_path, "premise_mismatch")
        assert result.fields["fact_core_version"] == 0
        assert "expected_fact_core_version=0" in result.fields["next"]
        assert _load(tmp_pm_path)["feedback"]["items"][0]["fact_core_version"] == 0

    def test_a_fixed_adr_gets_no_revision_in_next(self, tmp_pm_path: Path):
        _at_version_one(tmp_pm_path)
        adopted = change_decision_lineage(
            tmp_pm_path, ADR_ID, LineageChange(lifecycle="adopted", reason="the user adopted it")
        )
        assert adopted.status == "updated", adopted.error
        result = _recorded(tmp_pm_path, "premise_mismatch", text="That premise is wrong")
        assert result.fields["next"] == lineage._FEEDBACK_NEXT_FIXED
        assert "pm_update_decision (fact_core" not in result.fields["next"]
        # The fix itself is not touched by feedback.
        assert _load(tmp_pm_path)["lifecycle"] == "adopted"

    def test_a_reply_cannot_be_upgraded(self, tmp_pm_path: Path):
        # A cannot_judge reply stays as recorded; a later can_judge one is a new
        # entry, and the ADR's summary shows both (S2 spec §2.4).
        adr = _at_version_one(tmp_pm_path)
        _recorded(tmp_pm_path, "reject", respondent_scope="cannot_judge")
        first = _load(tmp_pm_path)["feedback"]["items"][0]
        _recorded(tmp_pm_path, "approve", respondent_scope="can_judge")
        items = _load(tmp_pm_path)["feedback"]["items"]
        assert items[0] == first and len(items) == 2
        assert _view(tmp_pm_path, adr).declared_respondent_scope == {
            "label": "mixed",
            "counts": _counts(can=1, cannot=1),
        }
        # Nothing in the storage or pure layer edits or removes a reply.
        names = [*vars(storage), *vars(lineage)]
        assert not [
            n for n in names if re.search(r"(edit|update|change|remove|delete)_feedback", n)
        ]

    def test_secrets_are_never_stored_or_returned(self, tmp_pm_path: Path):
        adr = _at_version_one(tmp_pm_path)
        result = _recorded(tmp_pm_path, "comment", text=f"key {SECRET} and {SECRET}")
        assert SECRET not in _path(tmp_pm_path).read_text(encoding="utf-8")
        assert SECRET not in json.dumps([result.fields, result.warnings])
        warning = next(
            w for w in result.warnings if w["code"] == "decision_lineage_secrets_redacted"
        )
        assert warning["level"] == "warning" and warning["message"].startswith("2 ")
        assert SECRET not in json.dumps(_view(tmp_pm_path, adr).as_dict())
        # A refusal never quotes the text either.
        refused = _feedback(tmp_pm_path, "comment", text=SECRET * 300)
        assert refused.error["code"] == "text_too_long"
        assert SECRET not in json.dumps(refused.error)

    @pytest.mark.parametrize("label", ["RSA PRIVATE KEY", "OPENSSH PRIVATE KEY"])
    def test_a_private_key_goes_with_its_body(self, tmp_pm_path: Path, label: str):
        # D11 names the private key beside the AWS key (S2 spec §6.1): the
        # body lines must go with the BEGIN line, not just the marker.
        adr = _at_version_one(tmp_pm_path)
        result = _recorded(tmp_pm_path, "comment", text=f"key:\n{_private_key(label)}\nend")
        assert _codes(result.warnings) == ["decision_lineage_secrets_redacted"]
        stored = _load(tmp_pm_path)["feedback"]["items"][0]["text"]
        assert stored == "key:\n<REDACTED:secret>\nend"
        shown = [
            _path(tmp_pm_path).read_text(encoding="utf-8"),
            json.dumps([result.fields, result.warnings]),
            json.dumps(_view(tmp_pm_path, adr).as_dict()),
        ]
        for text in shown:
            assert "Zm9v" not in text and "PRIVATE KEY" not in text

    def test_an_aliased_section_keeps_its_value_elsewhere(self, tmp_pm_path: Path):
        _at_version_one(tmp_pm_path)
        _recorded(tmp_pm_path)
        text = _path(tmp_pm_path).read_text(encoding="utf-8")
        text = text.replace("feedback:\n", "feedback: &fb\n", 1)
        text = text.replace("  items:\n  - id: ADR-900.FB-1", "  items: &it\n  - id: ADR-900.FB-1")
        _store(tmp_pm_path, text + "mirror: *fb\nmirror_items: *it\n")
        loaded = _load(tmp_pm_path)
        assert loaded["mirror"] is loaded["feedback"]
        _recorded(tmp_pm_path)
        doc = _load(tmp_pm_path)
        assert [e["id"] for e in doc["feedback"]["items"]] == ["ADR-900.FB-1", "ADR-900.FB-2"]
        assert doc["feedback"]["last_number"] == 2
        assert doc["mirror"] == {"last_number": 1, "items": doc["mirror_items"]}
        assert [e["id"] for e in doc["mirror_items"]] == ["ADR-900.FB-1"]

    def test_the_append_cap_counts_bytes(self, tmp_pm_path: Path):
        # 4,000 Japanese characters are 12,000 bytes: they fit in the room left
        # by characters, not in bytes (S2 spec §2.8).
        _at_version_one(tmp_pm_path)
        doc = _load(tmp_pm_path)
        doc["padding"] = ""
        _store(tmp_pm_path, doc)
        base = _path(tmp_pm_path).stat().st_size
        room = 6_000
        doc["padding"] = "あ" * ((lineage.MAX_APPEND_BYTES - room - base) // 3)
        _store(tmp_pm_path, doc)
        before = _snapshot(tmp_pm_path)
        result = _feedback(tmp_pm_path, "comment", text="返" * lineage.MAX_TEXT_CHARS)
        assert result.error["code"] == "decision_lineage_too_large"
        assert "feedback" in result.error["message"]
        assert "last_number" in result.error["remediation"]
        assert _snapshot(tmp_pm_path) == before
        _recorded(tmp_pm_path, "comment", text="短い返事")
        assert _path(tmp_pm_path).stat().st_size <= lineage.MAX_APPEND_BYTES

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
        _recorded(tmp_pm_path, "approve")
        assert taken == [f"decision_lineage-{ADR_ID}"]


# ─── D7: ids never repeat under concurrency (S2 spec §6.1) ──────────────


def _worker_record_feedback(pm_path_str: str, count: int) -> None:
    """Record ``count`` replies to ADR-900 (module level so spawn can pickle it)."""
    pm_path = Path(pm_path_str)
    for i in range(count):
        result = record_lineage_feedback(
            pm_path, ADR_ID, feedback_kind="comment", text=f"reply {i}"
        )
        if result.status != "recorded":
            raise SystemExit(f"not recorded: {result.error}")


class TestConcurrentIds:
    def test_threads(self, tmp_pm_path: Path, monkeypatch: pytest.MonkeyPatch):
        _at_version_one(tmp_pm_path)
        monkeypatch.setenv("PM_LOCK_TIMEOUT_S", "30")
        results: list[storage.LineageAppendResult] = []
        lock = threading.Lock()
        start = threading.Barrier(8)

        def record(i: int) -> None:
            start.wait(10)
            result = _feedback(tmp_pm_path, "comment", text=f"reply {i}")
            with lock:
                results.append(result)

        threads = [threading.Thread(target=record, args=(i,)) for i in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        assert all(result.status == "recorded" for result in results) and len(results) == 8
        ids = sorted(result.fields["feedback_id"] for result in results)
        expected = sorted(f"ADR-900.FB-{n}" for n in range(1, 9))
        assert ids == expected
        stored = [entry["id"] for entry in _load(tmp_pm_path)["feedback"]["items"]]
        assert sorted(stored) == expected

    def test_processes(self, tmp_pm_path: Path, monkeypatch: pytest.MonkeyPatch):
        _at_version_one(tmp_pm_path)
        monkeypatch.setenv("PM_LOCK_TIMEOUT_S", "30")
        ctx = mp.get_context("spawn")
        procs = [
            ctx.Process(target=_worker_record_feedback, args=(str(tmp_pm_path), 10))
            for _ in range(2)
        ]
        for proc in procs:
            proc.start()
        for proc in procs:
            proc.join(timeout=60)
        assert [proc.exitcode for proc in procs] == [0, 0]
        stored = [entry["id"] for entry in _load(tmp_pm_path)["feedback"]["items"]]
        assert stored == [f"ADR-900.FB-{n}" for n in range(1, 21)]
        assert _load(tmp_pm_path)["feedback"]["last_number"] == 20


# ─── The S2 view (S2 spec §2.6, §2.7) ───────────────────────────────────


class TestView:
    def test_a_row(self):
        entry = _entry(
            3,
            feedback_kind="check_answer",
            text="Still about 30",
            explanation_id="ADR-900.EX-1",
            fact_ids=["F-premise.1"],
            respondent_scope="can_judge",
            question_kind="premise_check",
        )
        view = _hand_view(feedback={"last_number": 3, "items": [entry]})
        assert view.feedback == {
            "items": [
                {
                    "id": "ADR-900.FB-3",
                    "at": NOW,
                    "feedback_kind": "check_answer",
                    "text": "Still about 30",
                    "text_truncated": False,
                    "fact_core_version": 1,
                    "explanation_id": "ADR-900.EX-1",
                    "fact_ids": ["F-premise.1"],
                    "respondent_scope": "can_judge",
                    "question_kind": "premise_check",
                    "review": "respondent_declared_can_judge",
                    "recorded_as": "assistant_recorded_unverified",
                }
            ],
            "feedback_total": 1,
        }
        assert view.declared_respondent_scope == {
            "label": "respondent_declared_can_judge",
            "counts": _counts(can=1),
        }
        shown = view.as_dict()
        assert list(shown)[-5:] == [
            "fact_core",
            "explanations",
            "feedback",
            "declared_respondent_scope",
            "adopted_fact_core_version",
        ]
        for name in shown["feedback"]["items"][0]:
            assert not SCORE_WORDS.search(name), name
        assert SKIPPED not in view.note_codes

    def test_left_out_fields(self):
        view = _hand_view(feedback={"items": [_entry(1)]})
        row = view.feedback["items"][0]
        assert row["explanation_id"] is None and row["fact_ids"] == []
        assert row["question_kind"] is None and row["respondent_scope"] == "unknown"
        assert row["review"] == "no_review_by_able_person"
        assert SKIPPED not in view.note_codes

    def test_newest_first_and_capped(self):
        entries = [_entry(n) for n in range(1, 26)]
        view = _hand_view(feedback={"items": entries})
        assert [row["id"] for row in view.feedback["items"]] == [
            f"ADR-900.FB-{n}" for n in range(25, 5, -1)
        ]
        assert view.feedback["feedback_total"] == 25
        assert "feedback_total_capped" not in view.feedback
        assert SKIPPED not in view.note_codes  # rows past the 20 shown are not skipped

    def test_the_scan_is_capped(self):
        entries = [_entry(1)] * (lineage.SCAN_FEEDBACK + 5)
        view = _hand_view(feedback={"items": entries})
        assert view.feedback["feedback_total"] == lineage.SCAN_FEEDBACK
        assert view.feedback["feedback_total_capped"] is True
        assert len(view.feedback["items"]) == lineage.VIEW_FEEDBACK
        assert {"code": SKIPPED, "count": 5} in view.notes

    def test_the_text_is_cut(self):
        view = _hand_view(feedback={"items": [_entry(1, text="x" * 600)]})
        row = view.feedback["items"][0]
        assert len(row["text"]) == lineage.VIEW_FEEDBACK_TEXT_CHARS
        assert row["text_truncated"] is True

    def test_fact_ids_are_capped(self):
        ids = [f"F-alt.{n}" for n in range(1, 16)]
        view = _hand_view(feedback={"items": [_entry(1, fact_ids=ids)]})
        assert view.feedback["items"][0]["fact_ids"] == ids[:10]
        assert {"code": SKIPPED, "count": 5} in view.notes

    @pytest.mark.parametrize("feedback", [[SECRET], {"items": SECRET}, {"last_number": 2}, 5])
    def test_a_broken_section_is_shown_empty(self, feedback: object):
        view = _hand_view(feedback=feedback)
        assert view.feedback == {"items": [], "feedback_total": 0}
        assert SKIPPED in view.note_codes
        assert SECRET not in json.dumps(view.as_dict())

    def test_broken_entries_are_skipped(self):
        entries = [
            SECRET,
            {**_entry(1), "id": f"ADR-900.FB-{SECRET}"},
            {**_entry(2), "fact_core_version": "1"},
            _entry(3),
        ]
        view = _hand_view(feedback={"items": entries})
        assert [row["id"] for row in view.feedback["items"]] == ["ADR-900.FB-3"]
        assert view.feedback["feedback_total"] == 4
        assert {"code": SKIPPED, "count": 3} in view.notes
        assert SECRET not in json.dumps(view.as_dict())

    def test_hand_written_values_are_never_shown(self):
        entry = _entry(
            1,
            feedback_kind=f"approve {SECRET}",
            text=f"key {SECRET}",
            explanation_id=SECRET,
            fact_ids=[SECRET, "F-what"],
            respondent_scope=f"can_judge {SECRET}",
            question_kind=SECRET,
            at=SECRET,
        )
        entry[SECRET] = SECRET
        view = _hand_view(feedback={"items": [entry], f"note-{SECRET}": 1})
        dumped = json.dumps(view.as_dict())
        assert SECRET not in dumped
        row = view.feedback["items"][0]
        assert row["feedback_kind"] is None and row["question_kind"] is None
        assert row["explanation_id"] is None and row["fact_ids"] == ["F-what"]
        assert row["respondent_scope"] == "unknown"
        assert row["review"] == "no_review_by_able_person"
        assert row["unknown_fields"] == ["<REDACTED:secret>"]
        assert view.feedback["unknown_fields"] == ["note-<REDACTED:secret>"]
        assert SKIPPED in view.note_codes

    @pytest.mark.parametrize("declared", [b"can_judge", {"can_judge": True}, "expert"])
    def test_an_undeclared_value_reads_as_unknown(self, declared: object):
        view = _hand_view(
            feedback={"items": [_entry(1, feedback_kind="approve", respondent_scope=declared)]}
        )
        row = view.feedback["items"][0]
        assert row["respondent_scope"] == "unknown"
        assert row["review"] == "no_review_by_able_person"
        assert view.declared_respondent_scope["counts"] == _counts(unknown=1)
        assert SKIPPED in view.note_codes

    def test_only_an_s2_view_shows_feedback(self):
        adr = _adr()
        raw = RawLineage(ADR_ID, exists=True, data=_doc(adr, feedback={"items": [_entry(1)]}))
        plain = lineage_view(adr, raw).as_dict()
        assert "feedback" not in plain and "declared_respondent_scope" not in plain
        assert "feedback" not in lineage_view(adr, raw).unknown_keys
        derived = lineage_view(adr, RawLineage(ADR_ID, exists=False), include_s2=True)
        assert derived.as_dict()["feedback"] == {"items": [], "feedback_total": 0}
        assert derived.as_dict()["declared_respondent_scope"] == {
            "label": "none",
            "counts": _counts(),
        }

    def test_recorded_feedback_through_storage(self, tmp_pm_path: Path):
        adr = _at_version_one(tmp_pm_path)
        _recorded(tmp_pm_path, "reject", respondent_scope="cannot_judge")
        _recorded(tmp_pm_path, "what_if", text="What if it doubles?", fact_ids=["F-weak"])
        view = _view(tmp_pm_path, adr)
        assert [row["feedback_kind"] for row in view.feedback["items"]] == ["what_if", "reject"]
        assert view.feedback["items"][0]["fact_ids"] == ["F-weak"]
        assert view.declared_respondent_scope == {
            "label": "no_review_by_able_person",
            "counts": _counts(cannot=1),
        }
        # A revision makes the replies to version 1 older: they are not counted.
        doc = _load(tmp_pm_path)
        items = change_decision_lineage(
            tmp_pm_path,
            ADR_ID,
            LineageChange(
                fact_core=[
                    {
                        "id": item["id"],
                        "text": item["text"] + " (2)",
                        **{k: item[k] for k in ("confidence", "reversibility") if k in item},
                    }
                    for item in doc["fact_core"]["items"]
                ],
                expected_fact_core_version=1,
            ),
        )
        assert items.status == "updated", items.error
        assert _view(tmp_pm_path, adr).declared_respondent_scope["label"] == "none"


# ─── The tool (S2 spec §3.4) ────────────────────────────────────────────

SPEC = Path(__file__).resolve().parents[1] / "docs" / "issues" / "DESIGN_decision-lineage-s2.md"
NOTICE = "Feedback is what an assistant recorded the user saying."
TRUNCATED = "decision_get_truncated"


def _tool(project: Path, kind: str = "comment", **kwargs: object) -> dict:
    kwargs.setdefault("decision_id", ADR_ID)
    kwargs.setdefault("text", "The user said so")
    return pm_add_feedback(feedback_kind=kind, project_path=str(project), **kwargs)


def _tool_get(project: Path, decision_id: str = ADR_ID) -> dict:
    return pm_decision_query(action="get", decision_id=decision_id, project_path=str(project))


def _unreachable(*args: object, **kwargs: object) -> None:
    raise AssertionError("reached past the argument checks")


class TestTool:
    def test_the_response_shape(self, tmp_project: Path):
        # The S2 spec §3.4 example, key for key and in its order.
        _at_version_one(tmp_project / ".pm")
        result = _tool(
            tmp_project,
            "premise_mismatch",
            text="More than 30 people will use it",
            fact_ids=["F-premise.1"],
            respondent_scope="can_judge",
        )
        assert result == {
            "status": "recorded",
            "decision_id": ADR_ID,
            "feedback_id": "ADR-900.FB-1",
            "feedback_kind": "premise_mismatch",
            "fact_core_version": 1,
            "respondent_scope": "can_judge",
            "review": "respondent_declared_can_judge",
            "warnings": [],
            "next": (
                "If the premise is wrong, revise the fact core with pm_update_decision "
                '(fact_core, expected_fact_core_version=1, caused_by=["ADR-900.FB-1"]) '
                "and explain the new version."
            ),
        }
        assert list(result) == [
            "status",
            "decision_id",
            "feedback_id",
            "feedback_kind",
            "fact_core_version",
            "respondent_scope",
            "review",
            "warnings",
            "next",
        ]
        for name in result:
            assert not SCORE_WORDS.search(name), name

    @pytest.mark.parametrize("kind", ["comment", "check_answer"])
    def test_no_next_step_for_a_comment_or_an_answer(self, tmp_project: Path, kind: str):
        pm = tmp_project / ".pm"
        _at_version_one(pm)
        explanation_id = _explained(pm)
        result = _tool(tmp_project, kind, explanation_id=explanation_id)
        assert result["status"] == "recorded" and result["next"] is None
        assert list(result)[-2:] == ["warnings", "next"]

    @pytest.mark.parametrize("scope", SCOPES)
    @pytest.mark.parametrize("kind", ["approve", "reject"])
    def test_approve_and_reject_change_nothing_but_the_feedback(
        self, tmp_project: Path, kind: str, scope: str
    ):
        pm = tmp_project / ".pm"
        _at_version_one(pm)
        decisions = (pm / "decisions.yaml").read_bytes()
        before = _load(pm)
        result = _tool(tmp_project, kind, respondent_scope=scope)
        assert result["status"] == "recorded"
        assert _codes(result["warnings"]) == _expected_codes(kind, scope)
        assert result["review"] == (
            "respondent_declared_can_judge" if scope == "can_judge" else "no_review_by_able_person"
        )
        assert "pm_update_decision" in result["next"]
        after = _load(pm)
        assert (pm / "decisions.yaml").read_bytes() == decisions
        assert {k: v for k, v in after.items() if k != "feedback"} == before
        got = _tool_get(tmp_project)
        assert (got["decision"]["status"], got["lineage"]["lifecycle"]) == ("proposed", "proposed")

    def test_the_declared_can_judge_notice_comes_every_time(self, tmp_project: Path):
        _at_version_one(tmp_project / ".pm")
        for _ in range(2):
            result = _tool(tmp_project, "approve", respondent_scope="can_judge")
            assert DECLARED_CAN_JUDGE in _codes(result["warnings"])

    def test_a_lineage_is_started_for_an_adr_without_one(self, tmp_project: Path):
        _seed(tmp_project / ".pm")
        result = _tool(tmp_project, "what_if", decision_id=OTHER_ID)
        assert (result["status"], result["feedback_id"]) == ("recorded", "ADR-901.FB-1")
        assert result["fact_core_version"] == 0
        assert _codes(result["warnings"]) == ["decision_lineage_started"]
        # Adopted before any fact was recorded, so nothing is fixed: next points
        # to the first version (S2 spec §2.2).
        assert "expected_fact_core_version=0" in result["next"]

    def test_a_fixed_adr_gets_no_revision_in_next(self, tmp_project: Path):
        pm = tmp_project / ".pm"
        _at_version_one(pm)
        _explained(pm)
        adopted = pm_update_decision(
            decision_id=ADR_ID,
            lifecycle="adopted",
            reason="the user adopted it",
            project_path=str(tmp_project),
        )
        assert adopted["status"] == "updated"
        result = _tool(tmp_project, "premise_mismatch")
        assert result["next"].startswith("The fact core of this ADR is fixed")
        assert "fact_core," not in result["next"]

    @pytest.mark.parametrize(
        ("kwargs", "code"),
        [
            ({"decision_id": SECRET}, "invalid_decision_id"),
            ({"kind": SECRET}, "invalid_feedback_kind"),
            ({"kind": "score"}, "invalid_feedback_kind"),
            ({"respondent_scope": SECRET}, "invalid_respondent_scope"),
            ({"respondent_scope": "verified"}, "invalid_respondent_scope"),
            ({"text": " "}, "feedback_text_required"),
            ({"text": SECRET + "x" * 4_001}, "text_too_long"),
            ({"explanation_id": f"{OTHER_ID}.EX-1"}, "invalid_explanation_id"),
            ({"explanation_id": SECRET}, "invalid_explanation_id"),
            ({"fact_ids": [SECRET]}, "invalid_fact_ids"),
            ({"fact_ids": [f"F-alt.{n}" for n in range(1, 12)]}, "invalid_fact_ids"),
            ({"kind": "check_answer"}, "check_answer_without_question"),
        ],
    )
    def test_argument_errors_come_before_the_project_is_resolved(
        self, monkeypatch: pytest.MonkeyPatch, kwargs: dict, code: str
    ):
        # S2 spec §3.4: nothing is resolved, read or locked for them.
        monkeypatch.setattr(server, "_get_pm_path", _unreachable)
        monkeypatch.setattr(server, "record_lineage_feedback", _unreachable)
        kwargs = dict(kwargs)
        kind = kwargs.pop("kind", "comment")
        kwargs.setdefault("decision_id", ADR_ID)
        kwargs.setdefault("text", "The user said so")
        result = pm_add_feedback(feedback_kind=kind, **kwargs)
        assert result["status"] == "error" and result["code"] == code
        assert SECRET not in json.dumps(result)

    @pytest.mark.parametrize(
        ("kwargs", "code"),
        [
            ({"decision_id": "ADR-950"}, "decision_not_found"),
            ({"explanation_id": f"{ADR_ID}.EX-9"}, "explanation_not_found"),
            ({"fact_ids": ["F-alt.1"]}, "feedback_fact_ids_unknown"),
        ],
    )
    def test_file_errors_come_back_as_they_are(self, tmp_project: Path, kwargs: dict, code: str):
        pm = tmp_project / ".pm"
        _at_version_one(pm)
        before = _snapshot(pm)
        result = _tool(tmp_project, **kwargs)
        assert result["status"] == "error" and result["code"] == code
        assert _snapshot(pm) == before

    def test_a_broken_fact_core_refuses_a_bare_reply(self, tmp_project: Path):
        pm = tmp_project / ".pm"
        _at_version_one(pm)
        doc = _load(pm)
        doc["fact_core"]["version"] = "1"
        _store(pm, doc)
        before = _snapshot(pm)
        result = _tool(tmp_project, "approve")
        assert (result["status"], result["code"]) == ("error", UNREADABLE)
        assert _snapshot(pm) == before

    def test_secrets_are_neither_stored_nor_shown(self, tmp_project: Path):
        pm = tmp_project / ".pm"
        _at_version_one(pm)
        result = _tool(tmp_project, "what_if", text=f"What if the key {SECRET} leaks?")
        assert result["status"] == "recorded"
        assert _codes(result["warnings"]) == ["decision_lineage_secrets_redacted"]
        assert SECRET not in json.dumps(result)
        assert SECRET not in _path(pm).read_text(encoding="utf-8")
        assert SECRET not in json.dumps(_tool_get(tmp_project), ensure_ascii=False)

    @pytest.mark.parametrize("label", ["RSA PRIVATE KEY", "OPENSSH PRIVATE KEY"])
    def test_a_private_key_is_neither_stored_nor_shown(self, tmp_project: Path, label: str):
        # D11 through the tool (S2 spec §6.1): file, response and get.
        pm = tmp_project / ".pm"
        _at_version_one(pm)
        result = _tool(tmp_project, "what_if", text=f"What if\n{_private_key(label)}\nleaks?")
        assert result["status"] == "recorded"
        assert _codes(result["warnings"]) == ["decision_lineage_secrets_redacted"]
        shown = [
            _path(pm).read_text(encoding="utf-8"),
            json.dumps(result),
            json.dumps(_tool_get(tmp_project), ensure_ascii=False),
        ]
        for text in shown:
            assert "Zm9v" not in text and "PRIVATE KEY" not in text

    def test_the_tool_is_full_mode_only(self):
        # Lens registers only RO_ALLOWLIST (S2 spec §6.3, D9); the wire check
        # is in test_launch_surfaces.
        assert "pm_add_feedback" not in server.RO_ALLOWLIST
        assert "pm_add_feedback" not in server.OUTBOX_READ_ALLOWLIST
        assert "pm_add_feedback" not in server.OUTBOX_WRITE_ALLOWLIST

    def test_the_docstring_is_the_one_the_spec_words(self):
        spec = SPEC.read_text(encoding="utf-8")
        start = spec.index("Record what the user said about an ADR: a premise")
        worded = " ".join(spec[start : spec.index("```", start)].split())
        doc = " ".join((pm_add_feedback.__doc__ or "").split())
        assert doc == worded
        # The declaration comes only from the reader's own answer (S2 spec §6.4).
        assert "pass only the can_judge or cannot_judge that pm_decision_brief returned" in doc
        assert "Never pass can_judge on a guess" in doc
        assert "pmlens does not check it" in doc
        assert "Replies are recorded, never scored." in doc


# ─── caused_by through the tool (S2 spec §3.1) ──────────────────────────


def _revision(pm_path: Path) -> list[dict]:
    """The stored items with every text changed, as a revision passes them."""
    return [
        {
            "id": item["id"],
            "text": item["text"] + " (2)",
            **{k: item[k] for k in ("confidence", "reversibility") if k in item},
        }
        for item in _load(pm_path)["fact_core"]["items"]
    ]


class TestCausedByThroughTheTool:
    def test_a_revision_and_an_evaluation_cite_the_feedback(self, tmp_project: Path):
        pm = tmp_project / ".pm"
        _at_version_one(pm)
        reply = _tool(tmp_project, "premise_mismatch", fact_ids=["F-premise.1"])
        revised = pm_update_decision(
            decision_id=ADR_ID,
            fact_core=_revision(pm),
            expected_fact_core_version=1,
            caused_by=[reply["feedback_id"], reply["feedback_id"]],
            project_path=str(tmp_project),
        )
        assert revised["status"] == "updated" and revised["fact_core"]["version"] == 2
        evaluated = pm_update_decision(
            decision_id=ADR_ID,
            evaluation="the load test passed with 40 users",
            evaluation_kind="test",
            caused_by=[reply["feedback_id"]],
            project_path=str(tmp_project),
        )
        assert evaluated["status"] == "updated"
        events = _load(pm)["events"]
        assert events[-2]["kind"] == "fact_core"
        assert events[-2]["caused_by"] == ["ADR-900.FB-1"]
        assert events[-1]["kind"] == "evaluation"
        assert events[-1]["caused_by"] == ["ADR-900.FB-1"]
        shown = _tool_get(tmp_project)["lineage"]["events"]
        assert [e.get("caused_by") for e in shown[-2:]] == [["ADR-900.FB-1"]] * 2

    @pytest.mark.parametrize(
        ("caused_by", "extra", "code"),
        [
            ([SECRET], {"evaluation": "e"}, "invalid_caused_by"),
            (["ADR-900.FB-1"], {}, "caused_by_without_target"),
            (["ADR-900.FB-1"], {"note": "n"}, "caused_by_without_target"),
            ([f"ADR-900.FB-{n}" for n in range(1, 12)], {"evaluation": "e"}, "too_many_caused_by"),
        ],
    )
    def test_argument_errors_come_before_the_project_is_resolved(
        self, monkeypatch: pytest.MonkeyPatch, caused_by: list[str], extra: dict, code: str
    ):
        monkeypatch.setattr(server, "_get_pm_path", _unreachable)
        monkeypatch.setattr(server, "change_decision_lineage", _unreachable)
        result = pm_update_decision(decision_id=ADR_ID, caused_by=caused_by, **extra)
        assert result["status"] == "error" and result["code"] == code
        assert SECRET not in json.dumps(result)

    @pytest.mark.parametrize(
        ("caused_by", "code"),
        [
            (["ADR-901.FB-1"], "invalid_caused_by"),
            (["ADR-900.FB-7"], "caused_by_not_found"),
        ],
    )
    def test_only_feedback_of_this_adr_on_record(
        self, tmp_project: Path, caused_by: list[str], code: str
    ):
        pm = tmp_project / ".pm"
        _at_version_one(pm)
        _tool(tmp_project, "what_if")
        _tool(tmp_project, "what_if", decision_id=OTHER_ID)
        before = _snapshot(pm)
        result = pm_update_decision(
            decision_id=ADR_ID,
            evaluation="e",
            caused_by=caused_by,
            project_path=str(tmp_project),
        )
        assert result["status"] == "error" and result["code"] == code
        assert _snapshot(pm) == before

    def test_the_ratchet_holds_with_caused_by(self, tmp_project: Path):
        pm = tmp_project / ".pm"
        _at_version_one(pm)
        reply = _tool(tmp_project, "unacceptable", respondent_scope="can_judge")
        items = _revision(pm)
        for item in items:
            if item["id"] == "F-revert":
                item["reversibility"] = "code_revertable"
        before = _snapshot(pm)
        result = pm_update_decision(
            decision_id=ADR_ID,
            fact_core=items,
            expected_fact_core_version=1,
            caused_by=[reply["feedback_id"]],
            project_path=str(tmp_project),
        )
        assert result["code"] == "fact_core_reversibility_lowering_refused"
        assert _snapshot(pm) == before


# ─── get (S2 spec §3.6) ─────────────────────────────────────────────────


def _s2_chars(part: dict) -> int:
    return len(
        json.dumps({key: part[key] for key in server._DECISION_GET_S2_KEYS}, ensure_ascii=False)
    )


class TestGet:
    def test_feedback_and_the_declared_scope_are_shown(self, tmp_project: Path):
        _at_version_one(tmp_project / ".pm")
        _tool(tmp_project, "reject", respondent_scope="cannot_judge")
        _tool(tmp_project, "approve", respondent_scope="can_judge", fact_ids=["F-what"])
        got = _tool_get(tmp_project)
        part = got["lineage"]
        assert list(part)[-5:] == [
            "fact_core",
            "explanations",
            "feedback",
            "declared_respondent_scope",
            "adopted_fact_core_version",
        ]
        assert [
            (row["id"], row["feedback_kind"], row["review"]) for row in part["feedback"]["items"]
        ] == [
            ("ADR-900.FB-2", "approve", "respondent_declared_can_judge"),
            ("ADR-900.FB-1", "reject", "no_review_by_able_person"),
        ]
        assert part["feedback"]["feedback_total"] == 2
        assert {row["recorded_as"] for row in part["feedback"]["items"]} == {
            "assistant_recorded_unverified"
        }
        # One can_judge never hides a cannot_judge (S2 spec §2.4).
        assert part["declared_respondent_scope"] == {
            "label": "mixed",
            "counts": _counts(can=1, cannot=1),
        }
        assert NOTICE in got["notice"]
        assert "their accuracy is unconfirmed" in got["notice"]
        assert TRUNCATED not in _codes(got["warnings"])

    def test_feedback_rows_go_first_when_the_budget_is_passed(self, tmp_project: Path):
        # S2 spec §3.6: the oldest feedback rows go before any previous value
        # or explanation row. A text of control characters is escaped to six
        # times its length, so 20 rows pass the budget on their own.
        pm = tmp_project / ".pm"
        _at_version_one(pm)
        _revise(pm, _revision(pm), 1)
        doc = _load(pm)
        doc["feedback"] = {
            "last_number": 25,
            "items": [
                _entry(
                    n, 2, feedback_kind="reject", text="\x01" * 600, respondent_scope="cannot_judge"
                )
                for n in range(1, 26)
            ],
        }
        _store(pm, doc)
        got = _tool_get(tmp_project)
        part = got["lineage"]
        assert _s2_chars(part) <= server._DECISION_GET_S2_MAX_CHARS
        [warning] = [w for w in got["warnings"] if w["code"] == TRUNCATED]
        assert warning["level"] == "info"
        assert "feedback row(s)" in warning["message"]
        assert "previous value(s)" not in warning["message"]
        assert "explanation row(s)" not in warning["message"]
        assert part["fact_core"]["previous_values"]
        rows = [row["id"] for row in part["feedback"]["items"]]
        assert 0 < len(rows) < lineage.VIEW_FEEDBACK
        # The newest stay, the oldest went.
        assert rows == [f"ADR-900.FB-{n}" for n in range(25, 25 - len(rows), -1)]
        assert f"{lineage.VIEW_FEEDBACK - len(rows)} feedback row(s)" in warning["message"]
        # The totals and the declared scope still count what was read.
        assert part["feedback"]["feedback_total"] == 25
        assert part["declared_respondent_scope"]["counts"] == _counts(cannot=25)

    def test_the_docstring_names_the_feedback(self):
        doc = " ".join((pm_decision_query.__doc__ or "").split())
        assert "the fact core, explanations (stale ones marked) and recorded feedback" in doc
