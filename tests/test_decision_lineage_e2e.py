"""Decision Lineage S2: the minimal path end to end, and redaction on its writers (PMSERV-232).

What this file pins, by the S2 specification's numbering
(docs/issues/DESIGN_decision-lineage-s2.md):

* D10 (§6.1): one ADR through the whole minimal path with the tools a host
  calls: recorded as proposed → fact core version 1 → the brief → explanation
  EX-1 with the brief's question → a premise_mismatch reply FB-1 → version 2
  caused by FB-1 → EX-1 shown stale beside FB-1 → EX-2 → the user's approval
  FB-2, declared with the scope the brief returned → adopted (status accepted,
  version 2 on the lifecycle event) → a revision after the adoption refused
  with ``fact_core_after_adoption`` and ``.pm`` byte for byte as it was. Each
  step's response is checked, and the last get's fact core, explanations,
  feedback, declared respondent scope, ``adopted_fact_core_version`` and
  events. The same steps and checks run in process and over the real stdio
  host path in full mode; a PM_LENS=1 server then reads the same brief and
  get, lists no writer, and writes nothing (§6.3, D9).
* D11 (§6.2): one parametrize over the three writing paths (the fact core
  through pm_update_decision, an explanation, a reply). A fake AWS key and a
  private key placed in the text reach neither the lineage file nor the
  writer's response, get or the brief, and the writer's warnings carry only
  their count.

The per-tool cases (argument and file errors, caps, hand-written values) are
in test_fact_core, test_explanation, test_feedback and test_decision_brief.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from pmlens import server
from tests.test_launch_surfaces import _mcp_session

ADR_ID = "ADR-001"
EX_1 = f"{ADR_ID}.EX-1"
EX_2 = f"{ADR_ID}.EX-2"
FB_1 = f"{ADR_ID}.FB-1"
FB_2 = f"{ADR_ID}.FB-2"
NOTHING_DECLARED = {"label": "none", "counts": {"can_judge": 0, "cannot_judge": 0, "unknown": 0}}
IDS = ["F-what", "F-change", "F-revert", "F-weak", "F-uncertain", "F-premise.1"]
REDACTED = "decision_lineage_secrets_redacted"
# The tools Lens must not register (S2 spec §6.3, §6.4 the first row).
WRITERS = {
    "pm_add_decision",
    "pm_update_decision",
    "pm_add_explanation",
    "pm_add_feedback",
    "pm_update_decision_posture",
}

FACTS_V1: list[dict[str, str]] = [
    {"kind": "what", "text": "Keep one shared booking calendar"},
    {"kind": "change", "text": "Before: one calendar per room. After: one for all rooms"},
    {"kind": "revert", "text": "Split the calendar again", "reversibility": "code_revertable"},
    {"kind": "weak", "text": "A busy week is harder to read"},
    {"kind": "uncertain", "text": "Peak use was not measured", "confidence": "medium"},
    {"kind": "premise", "text": "At most 30 people book rooms"},
]
# Version 2 after the user said the premise is wrong: the premise and the
# weakness that follows from it change, the other facts are handed back as
# they were.
FACTS_V2: list[dict[str, str]] = [
    {"id": fact_id, "text": fact["text"]}
    | {key: fact[key] for key in ("reversibility", "confidence") if key in fact}
    for fact_id, fact in zip(IDS, FACTS_V1, strict=True)
]
FACTS_V2[3]["text"] = "A busy week with 80 people is hard to read"
FACTS_V2[5]["text"] = "About 80 people book rooms"
CHANGED_BY_V2 = ["F-weak", "F-premise.1"]
QUESTION = {
    "question_kind": "premise_check",
    "fact_ids": ["F-premise.1"],
    "text": "Do at most 30 people book rooms?",
}


def _call(name: str, **arguments: Any) -> dict[str, Any]:
    return {"name": name, "arguments": arguments}


def _paragraphs(suffix: str) -> list[dict[str, Any]]:
    return [{"fact_ids": [fact_id], "text": f"About {fact_id}{suffix}."} for fact_id in IDS]


def _get(project: str) -> dict[str, Any]:
    return _call("pm_decision_query", action="get", decision_id=ADR_ID, project_path=project)


def _brief(project: str) -> dict[str, Any]:
    return _call(
        "pm_decision_brief",
        decision_id=ADR_ID,
        judgeable_kinds=["premise_dependent"],
        project_path=project,
    )


def _minimal_path(project: str) -> list[dict[str, Any]]:
    """The D10 calls up to the adoption, in the order a host sends them (S2 spec §6.1)."""
    return [
        _call(
            "pm_add_decision",
            title="One booking calendar",
            context="c",
            decision="d",
            decision_kind="premise_dependent",
            project_path=project,
        ),
        _call(
            "pm_update_decision",
            decision_id=ADR_ID,
            fact_core=FACTS_V1,
            expected_fact_core_version=0,
            project_path=project,
        ),
        _brief(project),
        _call(
            "pm_add_explanation",
            decision_id=ADR_ID,
            fact_core_version=1,
            paragraphs=_paragraphs(""),
            question=QUESTION,
            project_path=project,
        ),
        _call(
            "pm_add_feedback",
            decision_id=ADR_ID,
            feedback_kind="premise_mismatch",
            text="About 80 people book rooms now",
            explanation_id=EX_1,
            fact_ids=["F-premise.1"],
            project_path=project,
        ),
        _get(project),
        _call(
            "pm_update_decision",
            decision_id=ADR_ID,
            fact_core=FACTS_V2,
            expected_fact_core_version=1,
            caused_by=[FB_1],
            project_path=project,
        ),
        _get(project),
        _brief(project),
        _call(
            "pm_add_explanation",
            decision_id=ADR_ID,
            fact_core_version=2,
            paragraphs=_paragraphs(" again"),
            project_path=project,
        ),
        # The approval carries the scope the brief returned for this reader
        # (S2 spec §6.4: the declaration comes only from the brief).
        _call(
            "pm_add_feedback",
            decision_id=ADR_ID,
            feedback_kind="approve",
            text="Yes, go with one calendar",
            explanation_id=EX_2,
            respondent_scope="can_judge",
            project_path=project,
        ),
        _call(
            "pm_update_decision",
            decision_id=ADR_ID,
            lifecycle="adopted",
            reason="the user adopted it",
            project_path=project,
        ),
    ]


def _after_adoption(project: str) -> list[dict[str, Any]]:
    """A revision of the adopted ADR, then get and the brief (S2 spec §6.1 D10)."""
    revised = [dict(fact) for fact in FACTS_V2]
    revised[3]["text"] = "A busy week is fine"
    return [
        _call(
            "pm_update_decision",
            decision_id=ADR_ID,
            fact_core=revised,
            expected_fact_core_version=2,
            project_path=project,
        ),
        _get(project),
        _brief(project),
    ]


def _in_process(calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Call the tool functions directly, each response as a host would decode it."""
    return [json.loads(json.dumps(getattr(server, c["name"])(**c["arguments"]))) for c in calls]


def _over_stdio(calls: list[dict[str, Any]], *, lens: bool = False) -> tuple[list[str], list[dict]]:
    """Send the calls to a real `pmlens serve` over stdio; the tools listed and the responses."""
    session = _mcp_session(lens=lens, calls=calls)
    return session.tools, [json.loads(r["content"][0]["text"]) for r in session.responses]


def _snapshot(project: Path) -> dict[str, bytes]:
    """Every file under ``.pm`` but the lock files (taking a lock creates them)."""
    root = project / ".pm"
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file() and ".locks" not in path.relative_to(root).parts
    }


def _codes(entries: list[dict[str, Any]]) -> list[str]:
    return [entry["code"] for entry in entries]


def _explanations(got: dict[str, Any]) -> dict[str, tuple[int, bool, list[str]]]:
    return {
        row["id"]: (row["fact_core_version"], row["stale"], sorted(row["changed_ids"]))
        for row in got["lineage"]["explanations"]["items"]
    }


def _check_minimal_path(responses: list[dict[str, Any]]) -> None:
    """Each step's response of :func:`_minimal_path` (S2 spec §6.1 D10)."""
    (
        added,
        first,
        brief,
        explained,
        replied,
        got,
        second,
        stale,
        rebrief,
        again,
        approved,
        adopted,
    ) = responses
    assert (added["status"], added["decision_id"]) == ("recorded", ADR_ID)
    assert (added["lifecycle"], added["decision_status"]) == ("proposed", "proposed")

    assert first["status"] == "updated", first
    assert first["changes"]["fact_core"]["to_version"] == 1
    assert first["fact_core"] == {"version": 1, "ids": IDS}
    assert first["events_added"] == ["fact_core"]

    assert brief["decision"]["adopted_fact_core_version"] is None
    assert brief["fact_core"]["version"] == 1
    assert [item["id"] for item in brief["fact_core"]["items"]] == IDS
    assert brief["explanations"] == {"current": [], "stale": []}
    # EX-1 asks the question the brief proposed for this reader.
    assert {key: brief["question"][key] for key in ("question_kind", "fact_ids")} == {
        key: QUESTION[key] for key in ("question_kind", "fact_ids")
    }

    assert explained == {
        "status": "recorded",
        "decision_id": ADR_ID,
        "explanation_id": EX_1,
        "fact_core_version": 1,
        "fact_ids_present": True,
        "accuracy": "unconfirmed",
        "paragraphs": len(IDS),
        "warnings": [],
    }

    assert replied["status"] == "recorded", replied
    assert (replied["feedback_id"], replied["feedback_kind"]) == (FB_1, "premise_mismatch")
    assert replied["fact_core_version"] == 1
    assert (replied["respondent_scope"], replied["review"]) == (
        "unknown",
        "no_review_by_able_person",
    )
    assert f'caused_by=["{FB_1}"]' in replied["next"]

    # Before the revision: EX-1 is current, FB-1 is shown, and a premise
    # reply declares nothing about who judged (only approve, reject and
    # check_answer are counted).
    assert _explanations(got) == {EX_1: (1, False, [])}
    assert [row["id"] for row in got["lineage"]["feedback"]["items"]] == [FB_1]
    assert got["lineage"]["declared_respondent_scope"] == NOTHING_DECLARED

    assert second["status"] == "updated", second
    assert (second["changes"]["fact_core"]["from_version"], second["fact_core"]["version"]) == (
        1,
        2,
    )
    assert sorted(second["changes"]["fact_core"]["changed_ids"]) == sorted(CHANGED_BY_V2)
    assert "decision_explanations_stale" in _codes(second["warnings"])

    # After it (D6): EX-1 is stale with the facts that changed, FB-1 is still
    # shown, and the declared scope counts only replies to version 2.
    lineage = stale["lineage"]
    assert _explanations(stale) == {EX_1: (1, True, sorted(CHANGED_BY_V2))}
    [row] = lineage["feedback"]["items"]
    assert (row["id"], row["feedback_kind"], row["fact_core_version"]) == (
        FB_1,
        "premise_mismatch",
        1,
    )
    assert (row["explanation_id"], row["fact_ids"]) == (EX_1, ["F-premise.1"])
    assert row["review"] == "no_review_by_able_person"
    assert lineage["declared_respondent_scope"] == NOTHING_DECLARED
    assert lineage["events"][-1].get("caused_by") == [FB_1]
    assert rebrief["explanations"] == {"current": [], "stale": [EX_1]}
    assert rebrief["judgment_request"]["respondent_scope"] == "can_judge"

    assert (again["status"], again["explanation_id"], again["fact_core_version"]) == (
        "recorded",
        EX_2,
        2,
    )

    assert approved["status"] == "recorded", approved
    assert (approved["feedback_id"], approved["fact_core_version"]) == (FB_2, 2)
    assert (approved["respondent_scope"], approved["review"]) == (
        "can_judge",
        "respondent_declared_can_judge",
    )
    # A reply never adopts the ADR, and a can_judge declaration is told each time.
    assert {
        "decision_feedback_does_not_change_lifecycle",
        "decision_respondent_declared_can_judge",
    } <= set(_codes(approved["warnings"]))

    assert adopted["status"] == "updated", adopted
    assert (adopted["lifecycle"], adopted["decision_status"]) == ("adopted", "accepted")
    # EX-2 explains the adopted version, so nothing warns about a missing one.
    assert _codes(adopted["warnings"]) == ["decision_lifecycle_changed"]


def _check_after_adoption(responses: list[dict[str, Any]]) -> None:
    """The refused revision, the last get and the last brief (S2 spec §6.1 D10)."""
    refused, got, brief = responses
    assert refused["status"] == "error", refused
    assert refused["code"] == "fact_core_after_adoption"
    assert (refused["adopted_fact_core_version"], refused["current_version"]) == (2, 2)

    assert got["decision"]["status"] == "accepted"
    lineage = got["lineage"]
    assert lineage["adopted_fact_core_version"] == 2
    assert [event["kind"] for event in lineage["events"]] == [
        "created",
        "fact_core",
        "fact_core",
        "lifecycle",
    ]
    created, first, second, adoption = lineage["events"]
    assert created["lifecycle"] == "proposed"
    assert (first["version"], second["version"], second.get("caused_by")) == (1, 2, [FB_1])
    assert (adoption["to"], adoption["status"], adoption["fact_core_version"]) == (
        "adopted",
        "accepted",
        2,
    )
    fact_core = lineage["fact_core"]
    assert fact_core["version"] == 2
    assert [(item["id"], item["text"]) for item in fact_core["items"]] == [
        (fact["id"], fact["text"]) for fact in FACTS_V2
    ]
    assert {item["id"]: item["since"] for item in fact_core["items"]} == {
        fact_id: 2 if fact_id in CHANGED_BY_V2 else 1 for fact_id in IDS
    }
    assert {(p["id"], p["last_version"], p["text"]) for p in fact_core["previous_values"]} == {
        ("F-weak", 1, FACTS_V1[3]["text"]),
        ("F-premise.1", 1, FACTS_V1[5]["text"]),
    }
    assert _explanations(got) == {
        EX_2: (2, False, []),
        EX_1: (1, True, sorted(CHANGED_BY_V2)),
    }
    assert sorted(row["id"] for row in lineage["feedback"]["items"]) == [FB_1, FB_2]
    # The label names a declaration, never a review (S2 spec §6.4).
    assert lineage["declared_respondent_scope"] == {
        "label": "respondent_declared_can_judge",
        "counts": {"can_judge": 1, "cannot_judge": 0, "unknown": 0},
    }

    assert brief["decision"]["adopted_fact_core_version"] == 2
    assert brief["fact_core"]["version"] == 2
    assert brief["explanations"] == {"current": [EX_2], "stale": [EX_1]}


# ─── D10 ────────────────────────────────────────────────────────────────


def test_the_minimal_path_in_process(tmp_project: Path) -> None:
    project = str(tmp_project)
    _check_minimal_path(_in_process(_minimal_path(project)))
    before = _snapshot(tmp_project)
    _check_after_adoption(_in_process(_after_adoption(project)))
    assert _snapshot(tmp_project) == before


@pytest.mark.smoke
def test_the_minimal_path_over_stdio_and_lens_reads_it(tmp_project: Path) -> None:
    """D10 over the real host path in full mode, then a PM_LENS=1 server (S2 spec §6.3, §7.2).

    Two full-mode sessions, so that ``.pm`` can be compared around the refused
    revision; then a Lens session lists the brief and get but no writer,
    returns what full mode returned, and leaves ``.pm`` as it was.
    """
    project = str(tmp_project)
    _tools, responses = _over_stdio(_minimal_path(project))
    _check_minimal_path(responses)
    before = _snapshot(tmp_project)
    _tools, last = _over_stdio(_after_adoption(project))
    _check_after_adoption(last)
    assert _snapshot(tmp_project) == before

    tools, read = _over_stdio([_get(project), _brief(project)], lens=True)
    assert {"pm_decision_query", "pm_decision_brief"} <= set(tools)
    assert not WRITERS & set(tools)
    assert read == last[1:]
    assert _snapshot(tmp_project) == before


# ─── D11 ────────────────────────────────────────────────────────────────

AWS_KEY = "AKIA" + "Q" * 16
KEY_BODY = "Y2xl" * 16
PRIVATE_KEY = (
    "-----BEGIN RSA PRIVATE KEY-----\n"
    + "\n".join([KEY_BODY] * 5)
    + "\n-----END RSA PRIVATE KEY-----"
)
PASTED = f"The deploy key {AWS_KEY} and\n{PRIVATE_KEY}\nwere pasted here"
# What may never be seen again once PASTED has been written.
LEAKS = (AWS_KEY, KEY_BODY, "PRIVATE KEY")


def _with_secrets_in_the_facts(project: str) -> dict[str, Any]:
    facts = [dict(fact) for fact in FACTS_V2]
    facts[3]["text"] = PASTED
    return _call(
        "pm_update_decision",
        decision_id=ADR_ID,
        fact_core=facts,
        expected_fact_core_version=1,
        project_path=project,
    )


def _with_secrets_in_an_explanation(project: str) -> dict[str, Any]:
    paragraphs = _paragraphs("")
    paragraphs[3]["text"] = PASTED
    return _call(
        "pm_add_explanation",
        decision_id=ADR_ID,
        fact_core_version=1,
        paragraphs=paragraphs,
        question={"question_kind": "judgment", "fact_ids": ["F-weak"], "text": PASTED},
        project_path=project,
    )


def _with_secrets_in_a_reply(project: str) -> dict[str, Any]:
    return _call(
        "pm_add_feedback",
        decision_id=ADR_ID,
        feedback_kind="what_if",
        text=PASTED,
        fact_ids=["F-weak"],
        project_path=project,
    )


@pytest.mark.parametrize(
    ("write", "status", "count"),
    [
        (_with_secrets_in_the_facts, "updated", 2),
        # A paragraph and the question each hold both secrets.
        (_with_secrets_in_an_explanation, "recorded", 4),
        (_with_secrets_in_a_reply, "recorded", 2),
    ],
    ids=["fact_core", "explanation", "feedback"],
)
def test_secrets_reach_neither_the_file_nor_any_response(
    tmp_project: Path, write: Any, status: str, count: int
) -> None:
    project = str(tmp_project)
    setup = _in_process(_minimal_path(project)[:2])
    assert [response["status"] for response in setup] == ["recorded", "updated"]

    [written] = _in_process([write(project)])
    assert written["status"] == status, written
    lineage_file = tmp_project / ".pm" / "decision_lineage" / f"{ADR_ID}.yaml"
    stored = lineage_file.read_text(encoding="utf-8")
    got, brief = _in_process([_get(project), _brief(project)])
    for shown in (stored, json.dumps(written), json.dumps(got), json.dumps(brief)):
        for leak in LEAKS:
            assert leak not in shown
    # The text was saved, with the secrets replaced, not dropped.
    assert stored.count("<REDACTED:secret>") == count
    assert "were pasted here" in json.dumps(yaml.safe_load(stored))
    # Only the count is told: one warning, no other trace of the secrets.
    [warning] = [w for w in written["warnings"] if w["code"] == REDACTED]
    assert warning["level"] == "warning"
    assert warning["message"].startswith(f"{count} secret-like string(s) were removed")
