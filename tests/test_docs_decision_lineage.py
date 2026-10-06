"""The Decision Lineage documentation follows the code (S1 review: SEM-02, SEM-06, SEM-13).

The shipped files are read as data, as in ``test_docs_tool_list``:

* The model and enum counts that README.md, README.ja.md and docs/design.md
  state match ``pmlens.models``.
* The cheatsheets' enum reference lists each enum with exactly its values and
  includes the Decision Lineage vocabularies, and their ADR example records a
  proposed ADR and adopts it only after the user accepted its text (the
  default status is proposed).
* Every row that guarantees "no argument sets human confirmation" says the
  check is by argument name and names what it does not catch: an assistant
  passing ``status=accepted`` (or ``lifecycle=adopted``) is the caller's claim,
  and the guarantee must not read as more than that.
* Every ``decision_*`` / ``draft_source_decision_*`` code the source emits is
  in the code table of a Decision Lineage specification
  (docs/issues/DESIGN_decision-lineage-s*.md), which the S1 specification
  names as the one list of codes that tests match on.
* The S1 specification's pm_update_decision return example is what the tool
  returns for the call it shows (keys, events, warnings and ``next``), and its
  §8.3 says which prompt-pack check looks at which path.
* Every ``.pm/`` layout tree in the user-facing documents lists
  ``decision_lineage/``.
* The release that ships the current redaction catalog tells users, in its
  upgrade notes, what to do with drafts and memories redacted by an older one
  (a redacted draft is never redacted again).

Only the return-example check runs code (pm_update_decision, on a tmp_path
project); the rest read the shipped files as data.
"""

from __future__ import annotations

import ast
import datetime as dt
import inspect
import json
import re
from enum import StrEnum
from pathlib import Path

import pytest
from pydantic import BaseModel

from pmlens import models, storage
from pmlens.models import Decision, DecisionStatus
from pmlens.redaction import CATALOG_VERSION
from pmlens.server import pm_update_decision

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = REPO_ROOT / "src" / "pmlens"
SPEC_GLOB = "docs/issues/DESIGN_decision-lineage-s*.md"
S1_SPEC = "docs/issues/DESIGN_decision-lineage-s1.md"


def _model_classes(base: type) -> list[type]:
    """Classes defined in pmlens.models that derive from ``base``."""
    return [
        cls
        for _, cls in inspect.getmembers(models, inspect.isclass)
        if cls.__module__ == models.__name__ and issubclass(cls, base) and cls is not base
    ]


_ENUMS = {cls.__name__: [member.value for member in cls] for cls in _model_classes(StrEnum)}
_MODEL_COUNT = len(_model_classes(BaseModel))
_LINEAGE_ENUMS = (
    "DecisionLifecycle",
    "DecisionOrigin",
    "RecordedTiming",
    "DecisionKind",
    "EvaluationKind",
)


def _read(relpath: str) -> str:
    return (REPO_ROOT / relpath).read_text(encoding="utf-8")


def test_the_sources_are_populated() -> None:
    assert _MODEL_COUNT >= 15
    assert set(_LINEAGE_ENUMS) <= set(_ENUMS)
    assert "DecisionStatus" in _ENUMS
    assert list(REPO_ROOT.glob(SPEC_GLOB)), "no Decision Lineage specification found"


# ─── Model and enum counts ──────────────────────────────────────────

_COUNT_PATTERNS = (
    ("README.md", r"\((\d+) models, (\d+) enums\)"),
    ("README.ja.md", r"\((\d+) models, (\d+) enums\)"),
    ("docs/design.md", r"\((\d+)モデル, (\d+) Enum\)"),
    ("docs/design.md", r"(\d+) Pydantic モデル \+ (\d+) Enum"),
)


@pytest.mark.parametrize(
    ("relpath", "pattern"),
    _COUNT_PATTERNS,
    ids=["README.md", "README.ja.md", "design.md-tree", "design.md-summary"],
)
def test_the_stated_model_and_enum_counts_match(relpath: str, pattern: str) -> None:
    found = [(int(m), int(e)) for m, e in re.findall(pattern, _read(relpath))]
    assert found, f"{relpath} no longer states the counts as {pattern!r}"
    assert set(found) == {(_MODEL_COUNT, len(_ENUMS))}, (
        f"{relpath} states {found}; pmlens.models has {_MODEL_COUNT} models and {len(_ENUMS)} enums"
    )


# ─── Cheatsheets ───────────────────────────────────────────────────

_CHEATSHEETS = ("docs/cheatsheet.md", "docs/cheatsheet.ja.md")
_ENUM_ROW = re.compile(r"^\|\s*([A-Z][A-Za-z]+)\s*\|(.*)\|\s*$")


def _enum_reference(text: str) -> dict[str, list[str]]:
    """The cheatsheet's enum reference: type name -> backticked values, in order."""
    start = re.search(r"^## Enum (?:Reference|リファレンス)\s*$", text, re.MULTILINE)
    assert start, "the cheatsheet no longer has an enum reference"
    section = text[start.end() :]
    following = re.search(r"^## ", section, re.MULTILINE)
    if following:
        section = section[: following.start()]
    matches = [match for line in section.splitlines() if (match := _ENUM_ROW.match(line))]
    # The first row is the table's header ("| Type | Values |").
    return {match.group(1): re.findall(r"`([^`]+)`", match.group(2)) for match in matches[1:]}


def _code_blocks(text: str) -> list[str]:
    """The contents of the fenced code blocks, in order."""
    blocks: list[str] = []
    current: list[str] | None = None
    for line in text.splitlines():
        if line.startswith("```"):
            if current is None:
                current = []
            else:
                blocks.append("\n".join(current))
                current = None
        elif current is not None:
            current.append(line)
    return blocks


@pytest.mark.parametrize("relpath", _CHEATSHEETS)
def test_the_enum_reference_follows_models(relpath: str) -> None:
    rows = _enum_reference(_read(relpath))
    missing = [name for name in _LINEAGE_ENUMS if name not in rows]
    assert not missing, f"{relpath} does not list {missing}"
    for name, values in rows.items():
        assert name in _ENUMS, f"{relpath} lists {name}, which pmlens.models does not define"
        assert values == _ENUMS[name], f"{relpath}: {name} lists {values}, not {_ENUMS[name]}"


@pytest.mark.parametrize("relpath", _CHEATSHEETS)
def test_the_adr_example_records_proposed_and_adopts_after_acceptance(relpath: str) -> None:
    examples = [block for block in _code_blocks(_read(relpath)) if "pm_add_decision(" in block]
    assert len(examples) == 1, f"{relpath}: expected one pm_add_decision example"
    example = examples[0]
    recorded = example.index("pm_add_decision(")
    assert "status=" not in example[recorded:].split(")", 1)[0], (
        f"{relpath}: the example should rely on the proposed default"
    )
    assert "proposed" in example, f"{relpath}: the example does not say the ADR is proposed"
    adopted = example.find('lifecycle="adopted"')
    assert adopted > recorded and "pm_update_decision(" in example[recorded:], (
        f"{relpath}: the example does not adopt the ADR with pm_update_decision afterwards"
    )


# ─── Guarantee rows ────────────────────────────────────────────────

_GUARANTEE_ROWS = (
    ("README.md", "| Guaranteed |", "No tool has an argument", "checked by argument name"),
    ("README.ja.md", "| 保証 |", "人間による確認を設定する引数", "引数名による検査"),
    ("docs/design.md", "| 保証 |", "人間による確認を設定する引数", "引数名による検査"),
    (
        "docs/issues/DESIGN_decision-lineage-s1.md",
        "| 保証 |",
        "人間による確認を設定する引数",
        "引数名による検査",
    ),
)


@pytest.mark.parametrize(
    ("relpath", "prefix", "claim", "limit"),
    _GUARANTEE_ROWS,
    ids=[row[0] for row in _GUARANTEE_ROWS],
)
def test_the_no_confirmation_argument_guarantee_states_its_limit(
    relpath: str, prefix: str, claim: str, limit: str
) -> None:
    rows = [
        line for line in _read(relpath).splitlines() if line.startswith(prefix) and claim in line
    ]
    assert len(rows) == 1, f"{relpath}: expected one guarantee row containing {claim!r}"
    row = re.sub(r"[`\"]", "", rows[0])
    assert limit in row, f"{relpath}: the guarantee does not say it is a check by name"
    # What the name check misses, on both tools.
    for missed in ("lifecycle=adopted", "status=accepted"):
        assert missed in row, f"{relpath}: the guarantee row does not name {missed}"


# ─── Codes in the specification ─────────────────────────────────────

# Field and directory names that share the prefix but are not codes.
_NOT_CODES = frozenset({"decision_id", "decision_kind", "decision_lineage", "decision_status"})
_CODE_RE = re.compile(r"(?:decisions?|draft_source_decision)_[a-z_]+")


def _emitted_codes() -> set[str]:
    """Every string constant in the source shaped like a decision code."""
    codes: set[str] = set()
    for path in SRC_DIR.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and _CODE_RE.fullmatch(node.value)
            ):
                codes.add(node.value)
    return codes - _NOT_CODES


def _tabled_codes() -> set[str]:
    """Names in the first column of the specifications' tables."""
    names: set[str] = set()
    for path in REPO_ROOT.glob(SPEC_GLOB):
        for line in path.read_text(encoding="utf-8").splitlines():
            cells = line.split("|")
            if not line.startswith("|") or len(cells) < 4:
                continue
            for part in cells[1].split("/"):
                name = part.strip().strip("`").strip()
                if re.fullmatch(r"[a-z_]+", name):
                    names.add(name)
    return names


def test_the_emitted_codes_are_found() -> None:
    emitted = _emitted_codes()
    assert {"decision_lifecycle_changed", "draft_source_decision_not_adopted"} <= emitted
    assert len(emitted) >= 30


def test_every_emitted_decision_code_is_in_a_specification_table() -> None:
    missing = sorted(_emitted_codes() - _tabled_codes())
    assert not missing, f"codes missing from the code tables of {SPEC_GLOB}: {missing}"


# ─── The S1 specification against the code ─────────────────────────


def _spec_section(heading: str) -> str:
    """The S1 specification's text from ``heading`` to the next heading of any level."""
    text = _read(S1_SPEC)
    start = text.index(heading)
    following = re.search(r"^#{1,3} ", text[start + len(heading) :], re.MULTILINE)
    end = start + len(heading) + following.start() if following else len(text)
    return text[start:end]


def _update_decision_return_example() -> dict:
    section = _spec_section("### 4.3 pm_update_decision")
    returned = section[section.index("**戻り値**") :]
    block = re.search(r"```json\n(.*?)\n```", returned, re.DOTALL)
    assert block, "§4.3 no longer shows the return value as a JSON block"
    return json.loads(block.group(1))


def test_the_update_decision_return_example_is_what_the_tool_returns(
    tmp_project: Path,
) -> None:
    example = _update_decision_return_example()
    decision_id = example["decision_id"]
    added = {kind: ids for kind, ids in example["links"].items() if ids}
    # The ADR before the call, as the example's ``changes`` describe it, with no
    # lineage (the example starts one); every link target is an accepted ADR.
    before = DecisionStatus(example["changes"]["decision_status"]["from"])
    adrs = [(decision_id, before)] + [
        (target, DecisionStatus.ACCEPTED) for ids in added.values() for target in ids
    ]
    storage._save_decisions(
        tmp_project / ".pm",
        [
            Decision(
                id=adr_id,
                title=f"title {adr_id}",
                date=dt.date(2026, 10, 1),
                status=status,
                context="c",
                decision="d",
            )
            for adr_id, status in adrs
        ],
    )

    result = pm_update_decision(
        decision_id=decision_id,
        lifecycle=example["lifecycle"],
        reason="replaced",
        add_links=added,
        project_path=str(tmp_project),
    )

    assert set(result) == set(example), "§4.3's return example has other keys than the tool"
    for key in ("status", "decision_id", "lifecycle", "decision_status", "changes", "links"):
        assert result[key] == example[key], f"§4.3's example shows {key}={example[key]!r}"
    assert result["events_added"] == example["events_added"]
    shown = [(warning["level"], warning["code"]) for warning in example["warnings"]]
    assert [(warning["level"], warning["code"]) for warning in result["warnings"]] == shown
    assert result["next"].startswith(example["next"].split("…", 1)[0].rstrip())


def _reserved_directory_bullets() -> list[str]:
    """The sub-bullets of §8.3's prompt-pack item, each joined into one string."""
    section = _spec_section("### 8.3 その他の不変条件")
    item = section[section.index("- **予約ディレクトリ**") :]
    lines = item.splitlines()[1:]
    bullets: list[str] = []
    for line in lines:
        if line.startswith("  - "):
            bullets.append(line[4:])
        elif line.startswith("    ") and bullets:
            bullets[-1] += line.strip()
        else:
            break
    return bullets


def test_the_spec_says_which_prompt_pack_check_looks_at_which_path() -> None:
    bullets = _reserved_directory_bullets()
    directory = [bullet for bullet in bullets if "の直後が `decision_lineage`" in bullet]
    names = [bullet for bullet in bullets if "予約ファイル名" in bullet]
    assert len(directory) == 1 and len(names) == 1, bullets
    # _reserved_dir_in checks the path as written and the resolved path ...
    assert "書いたままのパス" in directory[0] and "resolve したパス" in directory[0]
    # ... while the file-name check sees only the basename as written.
    assert "basename" in names[0]
    assert "両方を見る" not in names[0]


# ─── .pm layout trees ──────────────────────────────────────────────

_LAYOUT_DOCS = (
    "README.md",
    "README.ja.md",
    "docs/cheatsheet.md",
    "docs/cheatsheet.ja.md",
    "docs/architecture.html",
)


def _layout_trees(relpath: str) -> list[str]:
    """The ``.pm/`` layout trees in a document (those that list decisions.yaml)."""
    text = _read(relpath)
    if relpath.endswith(".html"):
        blocks = re.findall(r'<div class="tree">(.*?)</div>', text, re.DOTALL)
    else:
        blocks = _code_blocks(text)
    return [block for block in blocks if "── decisions.yaml" in block]


@pytest.mark.parametrize("relpath", _LAYOUT_DOCS)
def test_the_pm_layout_lists_the_lineage_directory(relpath: str) -> None:
    trees = _layout_trees(relpath)
    assert len(trees) == 1, f"{relpath}: expected one .pm layout tree, found {len(trees)}"
    assert "── decision_lineage/" in trees[0], f"{relpath}: the .pm layout omits decision_lineage/"


# ─── Redaction catalog upgrade note ─────────────────────────────────


def test_the_release_with_the_current_catalog_says_what_to_do_with_older_redactions() -> None:
    text = _read("CHANGELOG.md")
    releases = re.split(r"^## ", text, flags=re.MULTILINE)[1:]
    shipping = [release for release in releases if f"catalog_version: {CATALOG_VERSION}" in release]
    assert shipping, f"no CHANGELOG release mentions catalog_version: {CATALOG_VERSION}"
    release = shipping[0]
    assert "**Upgrade notes.**" in release, "that release has no upgrade notes"
    notes = release[release.index("**Upgrade notes.**") :]
    notes = notes[: notes.index("\n### ")] if "\n### " in notes else notes
    # Drafts keep what the older catalog left (they are never redacted again),
    # and indexed memories keep it until they are ingested again.
    for needed in ("catalog_version", "pm_reject_draft", "pm_memory_ingest"):
        assert needed in notes, f"the upgrade notes do not mention {needed}"
