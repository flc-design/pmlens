"""The brief for explaining one ADR to a reader (S2 spec §3.5).

pm_decision_brief returns what an assistant needs to explain a decision to
the user in front of it: the current facts (``fact_core``), the rules an
explanation must follow, the floor that no reader and no posture can weaken,
and the one question to ask afterwards. This module builds that response from
values already read; it never reads a file, takes a lock or writes, and
tests/test_lineage.py holds it to lineage.py's import and no-write rules (it
may import lineage and policy, neither of which imports it).

What changes with what (S2 spec §3.5, the invariants its tests pin):

* The reader mode is ``(judgeable_kinds, reader_hint)``: which of spec_policy,
  premise_dependent and technical the reader can judge themselves (``None``
  until they have said) and how much detail they asked for (``None``,
  more_detail or simpler): 9 x 3 = 27 modes. A mode changes only
  ``language_rules``, ``judgment_request`` and ``question``.
* The project's posture (bold, cautious, unset) changes only ``posture``, and
  only the warnings about decision_policy.yaml come with it.
* The facts, ``floor``, ``fact_core_rules``, ``explanation_rules`` and
  ``validation`` are the same for every reader and every posture. The floor
  is read from the stored F-revert alone: anything but code_revertable needs
  a person able to judge before an irreversible effect, and that comes before
  ``judgment_request`` for every decision kind, technical included.
* What the replies to an ADR declared about who answered (``respondent_scope``)
  changes only ``feedback`` and ``declared_respondent_scope``.

Templates are server text with placeholders (``{premise}``); a fact they
quote is returned apart, in ``question.quoted_fact``, so project content never
sits inside a sentence the host would put to the user as pmlens's own. Every
string of the response is redacted once at the exit
(``decision_text_secrets_redacted`` carries the count only).

"S2 spec §N" below refers to docs/issues/DESIGN_decision-lineage-s2.md.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from .lineage import (
    ACCURACY_UNCONFIRMED,
    CAN_JUDGE,
    CANNOT_JUDGE,
    CODE_REVERTABLE,
    DECISION_FACT_CORE_MISSING,
    LINEAGE_ITEMS_SKIPPED,
    MAX_EXPLANATION_CHARS,
    MAX_FACT_NUMBER,
    MAX_PARAGRAPH_CHARS,
    MAX_PARAGRAPH_FACT_IDS,
    MAX_PARAGRAPHS,
    MAX_QUESTION_CHARS,
    QUESTION_KINDS,
    VIEW_FEEDBACK,
    LineageView,
    ScrubCache,
    _error,
    fact_core_notices,
    fact_kind,
    is_decision_id,
    notice,
    scrub_label_counted,
    scrub_view,
)
from .models import Decision, DecisionKind, DecisionStatus, RespondentScope
from .policy import (
    BOLD,
    CAUTIOUS,
    POLICY_SCHEMA_UNSUPPORTED,
    POLICY_UNREADABLE,
    UNSET,
    PolicyView,
)

# ─── Vocabularies (S2 spec §2.9) ─────────────────────

SPEC_POLICY = DecisionKind.SPEC_POLICY.value
PREMISE_DEPENDENT = DecisionKind.PREMISE_DEPENDENT.value
TECHNICAL = DecisionKind.TECHNICAL.value
# The kinds a reader may say they can judge; unknown is not a kind of decision.
JUDGEABLE_KINDS: tuple[str, ...] = (SPEC_POLICY, PREMISE_DEPENDENT, TECHNICAL)
MORE_DETAIL = "more_detail"
SIMPLER = "simpler"
READER_HINTS: tuple[str, ...] = (MORE_DETAIL, SIMPLER)
# The brief's first question: which kinds the reader can judge. It is not a
# question of an explanation (lineage.QUESTION_KINDS) and is never saved.
JUDGEABLE_KINDS_QUESTION = "judgeable_kinds_question"
UNKNOWN_SCOPE = RespondentScope.UNKNOWN.value

DETAIL_STANDARD = "standard"
DETAIL_MORE = "more"
DETAIL_LESS = "less"

# Error codes of the arguments (S2 spec §4).
INVALID_DECISION_ID = "invalid_decision_id"
INVALID_JUDGEABLE_KINDS = "invalid_judgeable_kinds"
INVALID_READER_HINT = "invalid_reader_hint"
TEXT_SECRETS_REDACTED = "decision_text_secrets_redacted"

# The ADR title is shown cut; decisions.yaml puts no cap on it.
MAX_TITLE_CHARS = 500

# The keys of the response, in order; the set never changes (S2 spec §3.5).
BRIEF_KEYS: tuple[str, ...] = (
    "decision",
    "fact_core",
    "fact_core_rules",
    "explanation_rules",
    "validation",
    "floor",
    "explanations",
    "feedback",
    "declared_respondent_scope",
    "language_rules",
    "judgment_request",
    "question",
    "posture",
    "notice",
    "warnings",
)
# The only keys a reader mode changes (ADR-058 4; S2 spec §3.5, 5.2).
MODE_KEYS: tuple[str, ...] = ("language_rules", "judgment_request", "question")

# ─── Fixed text (English; the host shows it in the user's language) ──

NOTICE = (
    "pmlens changes only language_rules, judgment_request and question with the reader, and "
    "only posture (with warnings about the posture file) with the project's posture; the "
    "facts, the floor and the validation rules are the same for every reader. Whether the "
    "explanation and the choices are good is up to the assistant and is not tested."
)

# What each kind of fact means (S2 spec §2.2, Q9).
FACT_KIND_MEANINGS: dict[str, str] = {
    "F-what": "What was decided, in one sentence.",
    "F-change": "The state before and after (for something new, say there was nothing before).",
    "F-revert": (
        "How to undo it and whether it can be undone; reversibility is code_revertable, "
        "external_irreversible or unknown."
    ),
    "F-weak": "Its weaknesses and disadvantages (or none, with the reason).",
    "F-uncertain": (
        "What is uncertain (or nothing, with the reason); confidence is high, medium or low."
    ),
    "F-alt.N": "An alternative that was considered, and why it was not taken.",
    "F-conditions.N": (
        "A condition, technical or operational, the decision needs in order to hold."
    ),
    "F-premise.N": (
        "A business premise the decision rests on (the number of users, a business rule …)."
    ),
}
FACT_ID_GRAMMAR = (
    "F-what, F-change, F-revert, F-weak and F-uncertain appear once each. F-alt.N, "
    f"F-conditions.N and F-premise.N are numbered 1 to {MAX_FACT_NUMBER} by pmlens: pass only "
    "the kind for a new one, and a number once used is never used again."
)
CONDITIONS_RULE = (
    "Record each condition the proposal or the user stated as its own F-conditions.N; never "
    "invent one; put a condition nobody has checked in F-uncertain."
)

EXPLANATION_RULES: dict[str, object] = {
    "may_change": ["order", "wording", "analogies", "level of detail"],
    "must_keep": ["facts", "important conditions", "disadvantages", "uncertainties"],
    "analogy": "When you use an analogy, add one line on where it does not fit.",
    "before_after": "Describe the state before and after in prose; a mermaid diagram is optional.",
    "format": (
        "paragraphs of {fact_ids, text}; fact_ids lists 1-2 ids, the first being what the "
        "paragraph is mainly about."
    ),
    "not_omission": "Easier to understand never means leaving facts out.",
}

# The checks every saved explanation passes, whoever it was written for (S2
# spec §2.3 checks 1-7, D4); lineage.explanation_refusal applies them.
VALIDATION_RULES: tuple[str, ...] = (
    f"The paragraphs are a list of 1 to {MAX_PARAGRAPHS} mappings, each holding only fact_ids "
    "and text.",
    f"Each paragraph's fact_ids lists 1 to {MAX_PARAGRAPH_FACT_IDS} different fact ids (F-what, "
    "F-alt.1 …).",
    f"Each paragraph's text is not blank and at most {MAX_PARAGRAPH_CHARS:,} characters long; "
    f"all of them together are at most {MAX_EXPLANATION_CHARS:,}.",
    "The explanation is checked against the current version of the fact core "
    "(fact_core_version must be that version), and every id it uses must be a fact of that "
    "version; an unknown or retired id is refused.",
    "Every fact of the current version is the first id (what the paragraph is mainly about) "
    "of some paragraph that is not only a mermaid diagram.",
    "The question after the explanation, if any, is of one of the kinds "
    f"{', '.join(QUESTION_KINDS)}, with 1 to {MAX_PARAGRAPH_FACT_IDS} fact ids of the current "
    "version (an F-premise id for "
    f"premise_check) and a text of at most {MAX_QUESTION_CHARS} characters; the question "
    "about which kinds the reader can judge is not part of an explanation.",
    "When an id is missing or unknown, nothing is saved and both lists come back "
    "(missing_fact_ids and unknown_fact_ids).",
)

# The floor (S2 spec §3.5, Q11). The requirement both floor.rule and
# judgment_request.if_cannot_judge state when the floor applies.
FLOOR_REQUIREMENT = "a person able to judge before its irreversible effect is carried out"
FLOOR_VERIFICATION = "Never weakened by reader mode or posture; strengthening is allowed."
FLOOR_RULE = (
    "An external_irreversible decision (or one whose reversibility is unknown) needs "
    f"{FLOOR_REQUIREMENT}, in either posture and for every decision kind; a later review is "
    "not enough. This takes precedence over judgment_request. When no person able to judge "
    "can be found, record that there was no review by a person able to judge and do not "
    "carry out the irreversible effect."
)
IF_CANNOT_JUDGE = (
    "Record replies with respondent_scope=cannot_judge; they are shown as no review by a "
    "person able to judge. Lean as the posture says."
)
IF_CANNOT_JUDGE_FLOOR = (
    f"Do not carry out the irreversible effect: this decision needs {FLOOR_REQUIREMENT}. Find "
    "such a person and check with them. Record replies with respondent_scope=cannot_judge; "
    "they are shown as no review by a person able to judge. The posture does not loosen this."
)

# language_rules (S2 spec §3.5): detail by reader_hint, terms by whether the
# reader can judge technical decisions.
DETAIL_RULES: dict[str, str] = {
    DETAIL_STANDARD: "Explain every fact at the usual level of detail.",
    DETAIL_MORE: "Add to each fact its grounds and the scope of its effect.",
    DETAIL_LESS: "Use short sentences and one fact per paragraph; leave no fact out.",
}
TERMS_PLAIN = (
    "Explain technical terms in everyday words, with a one-line explanation where each first "
    "appears."
)
TERMS_TECHNICAL = "Technical terms may be used as they are."

# Question templates. Only premise_check quotes a fact, through {premise}.
TEMPLATE_JUDGEABLE_KINDS = (
    "Which of these kinds of decision can you judge yourself? (a) a choice of specification "
    "or policy (spec_policy); (b) a business premise, such as the number of users or a "
    "business rule (premise_dependent); (c) a technical choice (technical). Your role "
    "(engineer, sales, operations and so on) can help you answer."
)
TEMPLATE_JUDGMENT = (
    "Compared with the alternatives that were considered, and knowing its main disadvantage, "
    "do you agree with this decision?"
)
TEMPLATE_PREMISE = "This decision assumes: {premise}. Is that right?"
TEMPLATE_UNDERSTANDING_SPEC = (
    "In your own words, what does this decision do, and what is its main disadvantage?"
)
TEMPLATE_UNDERSTANDING_TECH = (
    "In your own words, what changes with this decision, and how would it be undone?"
)
TEMPLATE_TECH_REVIEW = (
    "As a review after the fact: is this change sound, and does the way to undo it work?"
)
TEMPLATE_TECH_CHECK_BEFORE = (
    "Before its irreversible effect is carried out: is this change sound, and is it safe to "
    "carry out, given how far it can be undone?"
)
JUDGMENT_FACT_IDS: tuple[str, ...] = ("F-what", "F-weak")
TECHNICAL_FACT_IDS: tuple[str, ...] = ("F-change", "F-revert")
DEFAULT_PREMISE_ID = "F-premise.1"

# posture (S2 spec §3.5, ADR-058 3): how to lean when the reader cannot judge.
CAUTIOUS_LEANING: tuple[str, ...] = (
    "When the reader cannot judge a kind of decision, choose the conservative option that is "
    "easiest to revert.",
    "Where it matters, suggest checking with an outside expert.",
)
BOLD_LEANING: tuple[str, ...] = (
    "When the reader cannot judge a kind of decision, you may choose the more ambitious "
    "option, as far as it can be reverted in code.",
    "Record such a decision as proposed with origin=ai_auto, so the user can review it.",
)
BOLD_NOT_REVERTABLE = (
    "The posture is bold, but this decision is not known to be revertable in code, so lean "
    "as cautious."
)
LEANING_NEVER_LOOSENS = (
    "Leaning never weakens the validation or the floor: an irreversible effect still waits "
    "for a person able to judge."
)
POSTURE_QUESTION = (
    "Is this project closer to new development or challenging work (bold), or to "
    "maintenance, modification and fixes (cautious)? Until the user decides, it is treated "
    "as cautious."
)


# ─── Arguments ───────────────────────────────────────


def brief_call_error(
    decision_id: object, judgeable_kinds: object, reader_hint: object
) -> dict | None:
    """Check a pm_decision_brief call by its arguments alone (S2 spec §3.5).

    In this order: the id's shape (``invalid_decision_id``); judgeable_kinds,
    ``None`` or a list of distinct names of JUDGEABLE_KINDS, possibly empty
    (``invalid_judgeable_kinds``); reader_hint, ``None`` or one of
    READER_HINTS (``invalid_reader_hint``). No message quotes a value passed.

    Returns:
        An error dict, or ``None`` when the arguments are fine.
    """
    if not is_decision_id(decision_id):
        return _error(INVALID_DECISION_ID, "decision_id must look like ADR-NNN")
    if judgeable_kinds is not None and not (
        isinstance(judgeable_kinds, list)
        and all(isinstance(kind, str) and kind in JUDGEABLE_KINDS for kind in judgeable_kinds)
        and len(set(judgeable_kinds)) == len(judgeable_kinds)
    ):
        return _error(
            INVALID_JUDGEABLE_KINDS,
            "judgeable_kinds must be a list of different values among "
            f"{', '.join(JUDGEABLE_KINDS)} (or left out until the reader has said)",
        )
    if reader_hint is not None and not (
        isinstance(reader_hint, str) and reader_hint in READER_HINTS
    ):
        return _error(
            INVALID_READER_HINT,
            f"reader_hint must be one of {', '.join(READER_HINTS)} (or left out)",
        )
    return None


# ─── The rules and tables (pure) ─────────────────────


def kind_treated_as(decision_kind: object) -> str:
    """The decision kind the brief's table uses (S2 spec §3.5, Q8).

    unknown, and any value outside the vocabulary, is treated as spec_policy:
    a decision whose need for judgment is not known asks a person to judge.
    """
    if isinstance(decision_kind, str) and decision_kind in JUDGEABLE_KINDS:
        return decision_kind
    return SPEC_POLICY


def language_rules(judgeable: frozenset[str] | None, reader_hint: str | None) -> dict:
    """``language_rules``: detail by reader_hint, terms by technical in judgeable_kinds.

    Args:
        judgeable: The kinds the reader can judge, ``None`` when not asked yet.
        reader_hint: more_detail, simpler or ``None`` (standard).
    """
    detail = {MORE_DETAIL: DETAIL_MORE, SIMPLER: DETAIL_LESS}.get(
        reader_hint or "", DETAIL_STANDARD
    )
    technical = judgeable is not None and TECHNICAL in judgeable
    return {
        "detail": detail,
        "detail_rule": DETAIL_RULES[detail],
        "terms": TERMS_TECHNICAL if technical else TERMS_PLAIN,
    }


def floor_section(reversibility: str) -> dict:
    """``floor``, from the stored F-revert's reversibility alone (S2 spec §3.5).

    Anything but code_revertable (external_irreversible, unknown, a value
    outside the vocabulary, no F-revert, no fact core) needs a person able to
    judge before an irreversible effect.
    """
    return {
        "verification": FLOOR_VERIFICATION,
        "reversibility": reversibility,
        "needs_person_able_to_judge": reversibility != CODE_REVERTABLE,
        "rule": FLOOR_RULE,
    }


def _request_rule(kind: str, can_judge: bool | None, floor: bool) -> str:
    """The sentence of ``judgment_request`` saying what to ask, for one cell.

    ``can_judge`` is ``None`` before the reader has said which kinds they can
    judge. With the floor on, nothing here offers a review after the fact.
    """
    parts: list[str] = []
    if can_judge is None:
        parts.append(
            "The reader has not said which kinds of decision they can judge: ask the brief's "
            "question first, then call the brief again with judgeable_kinds from their own "
            "answer. Do not ask them to judge this decision until then."
        )
    elif can_judge and kind == TECHNICAL and not floor:
        parts.append(
            "Do not ask for a judgment before acting on a technical decision. Ask the brief's "
            "question as a review after the fact, and record tests, another AI's review and "
            "the results in the ADR's evaluation; record the reader's reply with "
            "respondent_scope=can_judge."
        )
    elif can_judge and kind == TECHNICAL:
        parts.append(
            "Ask the reader to check this technical decision with the brief's question, "
            "before its irreversible effect is carried out. Record tests, another AI's review "
            "and the results in the ADR's evaluation, and the reader's reply with "
            "respondent_scope=can_judge."
        )
    elif can_judge:
        when = (
            "after the explanation and before its irreversible effect is carried out"
            if floor
            else "after the explanation"
        )
        parts.append(
            f"Ask the reader to judge this decision with the brief's question, {when}; "
            "record their reply with respondent_scope=can_judge."
        )
    elif kind == TECHNICAL:
        lead = (
            "Do not ask this reader to judge it."
            if floor
            else "Do not ask for a judgment before acting on a technical decision, and do not "
            "ask this reader to review it."
        )
        parts.append(
            f"{lead} Ask the brief's question to check what they understood. Record tests, "
            "another AI's review and the results in the ADR's evaluation, and the reader's "
            "reply with respondent_scope=cannot_judge."
        )
    elif kind == PREMISE_DEPENDENT:
        parts.append(
            "Do not ask the reader to judge this decision, but ask whether its premise holds "
            "with the brief's question: they may know the business fact. Record their "
            "reply with respondent_scope=cannot_judge."
        )
    else:
        parts.append(
            "Do not ask the reader to judge this decision. Ask the brief's question to "
            "check what they understood, and record their reply with "
            "respondent_scope=cannot_judge."
        )
    if floor and not can_judge:
        parts.append(f"This decision needs {FLOOR_REQUIREMENT}; see if_cannot_judge.")
    return " ".join(parts)


def judgment_request(kind: str, judgeable: frozenset[str] | None, floor: bool) -> dict:
    """``judgment_request`` for one cell of f(decision_kind, mode) (S2 spec §3.5).

    Before the reader has said what they can judge, nothing is asked to be
    judged. A reader who can judge the kind is asked to judge it, except a
    technical decision, which is reviewed after the fact (ADR-056 5.1)
    unless the floor is on: then every kind, technical included, asks a reader
    who can judge before the irreversible effect, and anyone else is told not
    to carry it out (Q11). ``before_irreversible_effect`` copies the floor.

    Args:
        kind: :func:`kind_treated_as` of the declared kind.
        judgeable: The kinds the reader can judge, ``None`` when not asked yet.
        floor: ``floor.needs_person_able_to_judge``.
    """
    can_judge = None if judgeable is None else kind in judgeable
    if can_judge is None:
        scope = UNKNOWN_SCOPE
    else:
        scope = CAN_JUDGE if can_judge else CANNOT_JUDGE
    return {
        "ask_to_judge": bool(can_judge) and (kind != TECHNICAL or floor),
        "kind_treated_as": kind,
        "respondent_scope": scope,
        "before_irreversible_effect": floor,
        "rule": _request_rule(kind, can_judge, floor),
        "if_cannot_judge": IF_CANNOT_JUDGE_FLOOR if floor else IF_CANNOT_JUDGE,
    }


def question_section(
    kind: str,
    judgeable: frozenset[str] | None,
    floor: bool,
    premise: Mapping[str, object] | None,
) -> dict:
    """``question``: the one question after the explanation (S2 spec §3.5).

    Every question of an explanation passes lineage.explanation_refusal as
    returned: a kind of QUESTION_KINDS and at most two fact ids. Before the
    reader has said what they can judge, the question is which kinds they can
    judge (never saved with an explanation). Only premise_check quotes a fact,
    and the quote is returned apart from the template.

    Args:
        kind: :func:`kind_treated_as` of the declared kind.
        judgeable: The kinds the reader can judge, ``None`` when not asked yet.
        floor: ``floor.needs_person_able_to_judge``.
        premise: The first F-premise item of the fact core (``id`` and
            ``text``), or ``None`` when it has none; premise_check then names
            F-premise.1 and quotes nothing (no fact core: nothing to save yet).
    """
    if judgeable is None:
        return {
            "question_kind": JUDGEABLE_KINDS_QUESTION,
            "fact_ids": [],
            "template": TEMPLATE_JUDGEABLE_KINDS,
            "quoted_fact": None,
        }
    can_judge = kind in judgeable
    quoted: dict | None = None
    if kind == PREMISE_DEPENDENT:
        question_kind = "premise_check"
        premise_id = DEFAULT_PREMISE_ID
        if premise is not None:
            premise_id = str(premise["id"])
            quoted = {"id": premise_id, "text": premise["text"]}
        fact_ids = [premise_id]
        template = TEMPLATE_PREMISE
    elif kind == TECHNICAL:
        fact_ids = list(TECHNICAL_FACT_IDS)
        if can_judge:
            question_kind = "tech_review"
            template = TEMPLATE_TECH_CHECK_BEFORE if floor else TEMPLATE_TECH_REVIEW
        else:
            question_kind = "understanding_check"
            template = TEMPLATE_UNDERSTANDING_TECH
    else:
        fact_ids = list(JUDGMENT_FACT_IDS)
        if can_judge:
            question_kind = "judgment"
            template = TEMPLATE_JUDGMENT
        else:
            question_kind = "understanding_check"
            template = TEMPLATE_UNDERSTANDING_SPEC
    return {
        "question_kind": question_kind,
        "fact_ids": fact_ids,
        "template": template,
        "quoted_fact": quoted,
    }


def posture_section(policy: PolicyView, reversibility: str) -> dict:
    """``posture``: how to lean when the reader cannot judge (S2 spec §3.5, §2.10).

    Only a stored bold is bold; unset, an unreadable file and an unknown or
    non-string value act as cautious. Bold leans ambitiously only when the
    floor reads code_revertable; otherwise it leans as cautious. A plain unset
    returns the question that asks for a posture; an unreadable file, a
    schema this version cannot write and an unknown value return none (their
    warnings say what to do).

    Args:
        policy: :func:`policy.read_decision_policy` of the project.
        reversibility: ``floor.reversibility``.
    """
    effective = BOLD if policy.effective == BOLD and policy.value == BOLD else CAUTIOUS
    if effective == BOLD and reversibility == CODE_REVERTABLE:
        leaning = [*BOLD_LEANING]
    elif effective == BOLD:
        leaning = [BOLD_NOT_REVERTABLE, *CAUTIOUS_LEANING]
    else:
        leaning = [*CAUTIOUS_LEANING]
    leaning.append(LEANING_NEVER_LOOSENS)
    file_codes = {entry.get("code") for entry in policy.warnings}
    ask = policy.value == UNSET and not file_codes & {POLICY_UNREADABLE, POLICY_SCHEMA_UNSUPPORTED}
    return {
        "decision_posture": policy.value,
        "effective": effective,
        "leaning_rules": leaning,
        "posture_question": POSTURE_QUESTION if ask else None,
    }


def fact_core_rules(decision_kind: str) -> dict:
    """``fact_core_rules``: what each fact means and which are required (S2 spec §2.2).

    F-premise is required (at least one) for a premise_dependent decision.
    """
    required = [f"F-{kind}" for kind in ("what", "change", "revert", "weak", "uncertain")]
    if decision_kind == PREMISE_DEPENDENT:
        required.append("F-premise.N")
    return {
        "kinds": dict(FACT_KIND_MEANINGS),
        "required": required,
        "id_grammar": FACT_ID_GRAMMAR,
        "conditions": CONDITIONS_RULE,
    }


def validation_section() -> dict:
    """``validation``: the checks of S2 spec §2.3, the same for every reader."""
    return {
        "rules": list(VALIDATION_RULES),
        "accuracy": ACCURACY_UNCONFIRMED,
        "same_for_every_reader": True,
    }


# ─── Building the brief ──────────────────────────────


def _first_premise(fact_core: Mapping | None) -> dict | None:
    """The first F-premise item of a viewed fact core (``id`` and ``text``), or ``None``."""
    if fact_core is None:
        return None
    for item in fact_core.get("items", []):
        if fact_kind(item["id"]) == "premise":
            return {"id": item["id"], "text": item["text"]}
    return None


def _decision_section(decision: Decision, view: LineageView, cache: ScrubCache) -> tuple[dict, int]:
    """``decision``: the ADR's identity, lifecycle and declared kind; and the redactions.

    The id is shown only when well formed, the status as its value or a
    redacted label, the title redacted and cut to MAX_TITLE_CHARS.
    """
    hits = 0
    if is_decision_id(decision.id):
        decision_id = decision.id
    else:
        decision_id, count = scrub_label_counted(decision.id, cache=cache)
        hits += count
    if isinstance(decision.status, DecisionStatus):
        status = decision.status.value
    else:
        status, count = scrub_label_counted(decision.status, cache=cache)
        hits += count
    title, count = scrub_label_counted(decision.title, MAX_TITLE_CHARS, cache)
    hits += count
    return {
        "id": decision_id,
        "title": title,
        "lifecycle": view.lifecycle,
        "status": status,
        "decision_kind": view.declared.get("decision_kind"),
        "derived": view.derived,
        "adopted_fact_core_version": view.adopted_fact_core_version,
    }, hits


def _feedback_rows(view: LineageView, version: int) -> list[dict]:
    """The replies to the current version, newest first, at most VIEW_FEEDBACK.

    The view holds the newest replies of every version; replies are appended
    as versions rise, so the current version's are the newest ones. Bodies are
    left to get.
    """
    rows: list[dict] = []
    for row in view.feedback.get("items", []):
        if row.get("fact_core_version") != version:
            continue
        rows.append(
            {
                "id": row["id"],
                "feedback_kind": row.get("feedback_kind"),
                "fact_core_version": row["fact_core_version"],
                "respondent_scope": row.get("respondent_scope"),
            }
        )
    return rows[:VIEW_FEEDBACK]


def _plain(value: object) -> object:
    """A deep copy of plain dicts and lists (``copy`` is a banned name here)."""
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def _view_warnings(view: LineageView, fact_core: Mapping | None) -> list[dict]:
    """The notices the brief adds from the lineage view (S2 spec §3.5, §4).

    The fact core's infos (written after the adoption, changed since it,
    reversibility changed outside the tools), ``decision_fact_core_missing``,
    and the entries the view skipped or capped, by count only.
    """
    adr = view.decision_id or "this ADR"
    out = fact_core_notices(view)
    if fact_core is None:
        out.append(
            notice(
                "info",
                DECISION_FACT_CORE_MISSING,
                f"{adr} has no fact core to explain from (none was written, or it cannot be "
                "read). The rules and the question are returned; the floor reads its "
                "reversibility as unknown.",
                "Write the fact core with pm_update_decision (fact_core) from a full-mode "
                "pmlens host before explaining the decision.",
            )
        )
    skipped = sum(
        entry.get("count", 0) for entry in view.notes if entry.get("code") == LINEAGE_ITEMS_SKIPPED
    )
    if skipped:
        out.append(
            notice(
                "info",
                LINEAGE_ITEMS_SKIPPED,
                f"{skipped} entr{'y' if skipped == 1 else 'ies'} of {adr}'s lineage could not be "
                "read or were over a list limit, and are not shown.",
                "The lineage file under .pm holds them; fix a broken entry by hand.",
            )
        )
    return out


def build_decision_brief(
    decision: Decision,
    view: LineageView,
    policy: PolicyView,
    judgeable_kinds: Sequence[str] | None,
    reader_hint: str | None,
    *,
    lineage_warnings: Sequence[Mapping] = (),
) -> dict:
    """The pm_decision_brief response for one ADR; pure (S2 spec §3.5).

    Reads nothing and writes nothing: every input was read before. The key
    set never changes; a reader mode changes only MODE_KEYS, the posture only
    ``posture`` and the warnings about its file. Every string is redacted at
    the exit, and the redactions made while reading (the view's, the shown
    posture value's) and here are reported together as one count.

    Args:
        decision: The ADR from decisions.yaml (unique by id).
        view: lineage.lineage_view of the ADR with ``include_s2=True``.
        policy: policy.read_decision_policy of the project.
        judgeable_kinds: The kinds the reader can judge (checked by
            :func:`brief_call_error`), ``None`` until they have said.
        reader_hint: more_detail, simpler or ``None``.
        lineage_warnings: Warnings the caller built about the ADR and its
            lineage (attribution, status), put first; they go through the
            exit redaction with the rest.

    Returns:
        The response, keys in BRIEF_KEYS order.
    """
    cache = ScrubCache()
    decision_part, hits = _decision_section(decision, view, cache)
    fact_core = view.fact_core
    version = fact_core["version"] if fact_core is not None else 0
    judgeable = None if judgeable_kinds is None else frozenset(judgeable_kinds)
    kind = kind_treated_as(view.declared.get("decision_kind"))
    floor = floor_section(view.reversibility_floor)
    floor_on = bool(floor["needs_person_able_to_judge"])
    explanation_rows = view.explanations.get("items", [])

    warnings: list[dict] = [dict(entry) for entry in lineage_warnings]
    warnings.extend(_view_warnings(view, fact_core))
    warnings.extend(dict(entry) for entry in policy.warnings)

    response = {
        "decision": decision_part,
        "fact_core": (
            None
            if fact_core is None
            else {"version": version, "items": _plain(fact_core.get("items", []))}
        ),
        "fact_core_rules": fact_core_rules(kind),
        "explanation_rules": _plain(EXPLANATION_RULES),
        "validation": validation_section(),
        "floor": floor,
        "explanations": {
            "current": [row["id"] for row in explanation_rows if not row.get("stale")],
            "stale": [row["id"] for row in explanation_rows if row.get("stale")],
        },
        "feedback": _feedback_rows(view, version),
        "declared_respondent_scope": _plain(view.declared_respondent_scope),
        "language_rules": language_rules(judgeable, reader_hint),
        "judgment_request": judgment_request(kind, judgeable, floor_on),
        "question": question_section(kind, judgeable, floor_on, _first_premise(fact_core)),
        "posture": posture_section(policy, view.reversibility_floor),
        "notice": NOTICE,
        "warnings": warnings,
    }
    scrubbed, count = scrub_view(response)
    result = scrubbed if isinstance(scrubbed, dict) else {}
    # Only the posture's value is shown; the history's last change is not, so
    # its redactions are not this response's (S2 spec §3.5).
    total = view.redactions + policy.value_redactions + hits + count
    if total:
        result.setdefault("warnings", []).append(
            notice(
                "warning",
                TEXT_SECRETS_REDACTED,
                f"{total} secret-like string(s) were found while reading this ADR, its lineage "
                "and the decision posture; none of them is shown in this response, and the "
                "files were not changed.",
                "If a real credential is in .pm, revoke it and remove it from the file by hand.",
            )
        )
    return result
