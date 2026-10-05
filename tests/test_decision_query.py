"""pm_decision_query: read ADRs and their lineage (ADR-056 S1, PMSERV-223 + PMSERV-253).

What this file pins, by the design document's numbering
(docs/issues/DESIGN_decision-lineage-s1.md, §4.2 and §9.1 row 3):

* D2: an ADR without a lineage reads as derived / unknown / not_recorded, and
  reading writes nothing (decisions.yaml bytes, no ``.locks/``, no
  ``decision_lineage/``).
* list and get, the lifecycle filter (on the effective lifecycle), the status
  mismatch warning, the three not_recorded cases, declared_later, linked_from
  (and its scan limit), one-sided links, duplicate ids, anchor mismatch.
* PMSERV-253: unknown keys come back by name only, nested lineage values only
  through the per-kind allow-list, every response string is redacted at the
  exit, and no error message quotes the file. A secret in a label redacted
  before the exit (an unknown status, a malformed id) is still counted in
  ``decision_text_secrets_redacted``, and the exit pass takes time linear in
  the text: a whitespace-free run longer than MAX_SCAN_RUN_CHARS is withheld
  unscanned.
* Hostile lineage files (FIFO, symlink, oversize, directory) do not block or
  raise.
* Every error code returns the error dict and leaves the files as they were.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import threading
import time
from pathlib import Path

import pytest
import yaml

from pmlens import lineage, storage
from pmlens.lineage import LineageChange, anchor_for, dump_lineage, new_lineage_doc
from pmlens.models import Decision, DecisionStatus
from pmlens.server import pm_add_decision, pm_decision_query

NOW = "2026-10-05T03:12:00Z"
SECRET = "AKIA" + "Q" * 16
ADR_DATE = dt.date(2026, 10, 1)
ALL_NOT_RECORDED = ["recorded_at", "origin", "recorded_timing", "decision_kind"]


@pytest.fixture(autouse=True)
def _fixed_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(lineage, "_utc_now", lambda: NOW)


# ─── Helpers ─────────────────────────────────────────


def _pm(project: Path) -> Path:
    return project / ".pm"


def _query(project: Path, **kwargs) -> dict:
    return pm_decision_query(project_path=str(project), **kwargs)


def _get(project: Path, decision_id: str) -> dict:
    return _query(project, action="get", decision_id=decision_id)


def _add(project: Path, title: str = "Use SQLite", **kwargs) -> dict:
    result = pm_add_decision(
        title=title, context="c", decision="d", project_path=str(project), **kwargs
    )
    assert result["status"] == "recorded", result
    return result


def _adr(adr_id: str, status: DecisionStatus | str = DecisionStatus.ACCEPTED, **extra) -> Decision:
    return Decision(
        id=adr_id,
        title=extra.pop("title", f"title {adr_id}"),
        date=ADR_DATE,
        status=status,
        context=extra.pop("context", "c"),
        decision="d",
        **extra,
    )


def _seed(project: Path, *adrs: Decision) -> None:
    storage._save_decisions(_pm(project), list(adrs))


def _lineage_path(project: Path, adr_id: str) -> Path:
    return _pm(project) / "decision_lineage" / f"{adr_id}.yaml"


def _put_lineage(project: Path, adr_id: str, content: dict | str) -> Path:
    path = _lineage_path(project, adr_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = content if isinstance(content, str) else dump_lineage(content, adr_id)
    path.write_text(text, encoding="utf-8")
    return path


def _doc_for(adr: Decision, **overrides: object) -> dict:
    """A lineage document as S1 writes it for ``adr`` (any status, even unknown)."""
    known = isinstance(adr.status, DecisionStatus)
    doc = new_lineage_doc(adr if known else adr.model_copy(update={"status": "accepted"}), {}, NOW)
    doc["anchor"] = anchor_for(adr)
    doc.update(overrides)
    return doc


def _change(project: Path, adr_id: str, **kwargs) -> None:
    result = storage.change_decision_lineage(_pm(project), adr_id, LineageChange(**kwargs))
    assert result.status == "updated", result


def _snapshot(root: Path) -> dict[str, object]:
    out: dict[str, object] = {}
    for path in sorted(root.rglob("*")):
        key = str(path.relative_to(root))
        if path.is_symlink():
            out[key] = ("link", os.readlink(path))
        elif path.is_fifo():
            out[key] = "fifo"
        elif path.is_dir():
            out[key] = "dir"
        else:
            out[key] = path.read_bytes()
    return out


def _codes(result: dict) -> list[str]:
    return [warning["code"] for warning in result.get("warnings", [])]


def _warning(result: dict, code: str) -> dict:
    [found] = [warning for warning in result.get("warnings", []) if warning["code"] == code]
    return found


def _note_codes(result: dict) -> set[str]:
    return {note["code"] for note in result["lineage"]["notes"]}


def _ids(result: dict) -> list[str]:
    return [row["id"] for row in result["decisions"]]


# ─── D2: no lineage, nothing written ─────────────────


class TestWithoutLineage:
    def test_derived_unknown_and_not_recorded_and_nothing_is_written(self, tmp_project: Path):
        _seed(tmp_project, _adr("ADR-001"))
        before = _snapshot(_pm(tmp_project))

        listed = _query(tmp_project)
        got = _get(tmp_project, "ADR-001")

        assert _snapshot(_pm(tmp_project)) == before
        assert not (_pm(tmp_project) / ".locks").exists()
        assert not (_pm(tmp_project) / "decision_lineage").exists()
        assert listed["decisions"] == [
            {
                "id": "ADR-001",
                "title": "title ADR-001",
                "date": "2026-10-01",
                "status": "accepted",
                "lifecycle": "adopted",
                "derived": True,
                "origin": "unknown",
            }
        ]
        view = got["lineage"]
        assert view["derived"] is True
        assert view["lifecycle"] == "adopted"
        assert view["recorded_at"] is None
        assert view["declared"] == dict.fromkeys(
            ("origin", "recorded_timing", "decision_kind"), "unknown"
        )
        assert view["not_recorded"] == ALL_NOT_RECORDED
        assert got["warnings"] == []

    @pytest.mark.parametrize(
        "status, lifecycle",
        [
            ("proposed", "proposed"),
            ("accepted", "adopted"),
            ("deprecated", "deprecated"),
            ("superseded", "superseded"),
        ],
    )
    def test_every_status_derives_its_lifecycle(self, tmp_project: Path, status, lifecycle):
        _seed(tmp_project, _adr("ADR-001", DecisionStatus(status)))
        got = _get(tmp_project, "ADR-001")
        assert got["lineage"]["lifecycle"] == lifecycle
        assert got["lineage"]["derived"] is True
        if status == "superseded":
            assert "decision_lineage_superseded_without_successor" in _note_codes(got)

    def test_an_unknown_status_has_no_lifecycle_and_is_warned(self, tmp_project: Path):
        _seed(tmp_project, _adr("ADR-001", "draft"))
        got = _get(tmp_project, "ADR-001")
        assert got["decision"]["status"] == "draft"
        assert got["lineage"]["lifecycle"] is None
        assert _codes(got) == ["decision_status_unknown"]
        listed = _query(tmp_project)
        assert listed["decisions"][0]["lifecycle"] is None
        assert _codes(listed) == ["decision_status_unknown"]
        assert _query(tmp_project, lifecycle="proposed")["decisions"] == []

    def test_a_project_without_decisions_lists_nothing(self, tmp_project: Path):
        listed = _query(tmp_project)
        assert listed == {
            "count": 0,
            "total": 0,
            "lifecycle_filter": None,
            "decisions": [],
            "warnings": [],
        }


# ─── list ────────────────────────────────────────────


class TestList:
    def test_rows_and_the_lifecycle_filter(self, tmp_project: Path):
        _add(tmp_project, "first", origin="ai_auto")
        _add(tmp_project, "second", status="accepted", origin="human")
        listed = _query(tmp_project)
        assert listed["count"] == listed["total"] == 2
        assert listed["lifecycle_filter"] is None
        fields = ("id", "status", "lifecycle", "derived", "origin")
        assert [tuple(row[name] for name in fields) for row in listed["decisions"]] == [
            ("ADR-001", "proposed", "proposed", False, "ai_auto"),
            ("ADR-002", "accepted", "adopted", False, "human"),
        ]

        proposed = _query(tmp_project, lifecycle="proposed")
        assert (proposed["count"], proposed["total"]) == (1, 2)
        assert proposed["lifecycle_filter"] == "proposed"
        assert _ids(proposed) == ["ADR-001"]
        assert _ids(_query(tmp_project, lifecycle="adopted")) == ["ADR-002"]
        assert _ids(_query(tmp_project, lifecycle="rejected")) == []

    def test_the_filter_uses_the_lineage_not_the_status(self, tmp_project: Path):
        adr = _adr("ADR-001", DecisionStatus.PROPOSED)
        _seed(tmp_project, adr)
        _put_lineage(tmp_project, "ADR-001", _doc_for(adr, lifecycle="adopted"))
        assert _ids(_query(tmp_project, lifecycle="proposed")) == []
        adopted = _query(tmp_project, lifecycle="adopted")
        assert _ids(adopted) == ["ADR-001"]
        assert adopted["decisions"][0]["status"] == "proposed"
        assert adopted["decisions"][0]["lifecycle"] == "adopted"
        assert _codes(adopted) == ["decision_status_mismatch"]
        assert "ADR-001" in _warning(adopted, "decision_status_mismatch")["message"]

    def test_warnings_are_one_per_code_and_list_ids(self, tmp_project: Path):
        adrs = [_adr(f"ADR-00{n}") for n in range(1, 5)]
        _seed(tmp_project, *adrs)
        _put_lineage(tmp_project, "ADR-001", "lifecycle: [unclosed\n")
        _put_lineage(tmp_project, "ADR-002", "decision_id: ADR-002\nlifecycle: : :\n")
        _put_lineage(tmp_project, "ADR-003", _doc_for(adrs[2], schema=7))
        listed = _query(tmp_project)
        assert _codes(listed) == [
            "decision_lineage_unreadable",
            "decision_lineage_schema_unsupported",
        ]
        message = _warning(listed, "decision_lineage_unreadable")["message"]
        assert "ADR-001" in message and "ADR-002" in message
        assert "ADR-003" not in message

    def test_more_than_fifty_ids_end_with_a_count(self, tmp_project: Path):
        _seed(tmp_project, *(_adr(f"ADR-{n:03d}", "draft") for n in range(1, 54)))
        listed = _query(tmp_project)
        message = _warning(listed, "decision_status_unknown")["message"]
        assert "ADR-050" in message
        assert "ADR-051" not in message
        assert message.endswith("and 3 more.")


# ─── get ─────────────────────────────────────────────


class TestGet:
    def test_the_response_shape(self, tmp_project: Path):
        _add(
            tmp_project,
            "first",
            origin="ai_auto",
            recorded_timing="before_impl",
            decision_kind="technical",
            consequences_positive=["fast"],
        )
        got = _get(tmp_project, "ADR-001")
        assert set(got) == {"decision", "lineage", "notice", "warnings"}
        assert "status" not in got  # the ADR's status lives under decision
        assert got["decision"] == {
            "id": "ADR-001",
            "title": "first",
            "date": dt.date.today().isoformat(),
            "status": "proposed",
            "context": "c",
            "decision": "d",
            "consequences": {
                "positive": ["fast"],
                "negative": [],
                "mitigations": [],
                "unknown_keys": [],
            },
            "unknown_keys": [],
        }
        assert got["lineage"] == {
            "derived": False,
            "schema": 1,
            "lifecycle": "proposed",
            "recorded_at": NOW,
            "declared": {
                "origin": "ai_auto",
                "recorded_timing": "before_impl",
                "decision_kind": "technical",
            },
            "declared_later": [],
            "not_recorded": [],
            "links": {"supersedes": [], "superseded_by": [], "amends": []},
            "linked_from": {"supersedes": [], "amends": []},
            "events": [
                {
                    "at": NOW,
                    "kind": "created",
                    "via": "pm_add_decision",
                    "lifecycle": "proposed",
                    "status": "proposed",
                }
            ],
            "events_total": 1,
            "unknown_keys": [],
            "notes": [],
        }
        assert "cannot tell whether a person approved them" in got["notice"]
        assert "not a human review" in got["notice"]
        assert got["warnings"] == []
        json.dumps(got)

    def test_not_recorded_when_the_declared_values_were_left_out(self, tmp_project: Path):
        _add(tmp_project)
        lineage_view = _get(tmp_project, "ADR-001")["lineage"]
        assert lineage_view["derived"] is False
        assert lineage_view["not_recorded"] == ["origin", "recorded_timing", "decision_kind"]

    def test_not_recorded_for_a_lineage_started_later(self, tmp_project: Path):
        _seed(tmp_project, _adr("ADR-001"))
        _change(tmp_project, "ADR-001", note="a small call made while implementing it")
        lineage_view = _get(tmp_project, "ADR-001")["lineage"]
        assert lineage_view["derived"] is False
        assert lineage_view["recorded_at"] is None
        assert lineage_view["not_recorded"] == ALL_NOT_RECORDED
        assert [event["kind"] for event in lineage_view["events"]] == ["lineage_started", "note"]

    def test_not_recorded_for_a_derived_adr(self, tmp_project: Path):
        _seed(tmp_project, _adr("ADR-001"))
        assert _get(tmp_project, "ADR-001")["lineage"]["not_recorded"] == ALL_NOT_RECORDED

    def test_declared_later_names_backfilled_fields(self, tmp_project: Path):
        _add(tmp_project)
        _change(tmp_project, "ADR-001", origin="ai_auto", reason="the session log says so")
        lineage_view = _get(tmp_project, "ADR-001")["lineage"]
        assert lineage_view["declared"]["origin"] == "ai_auto"
        assert lineage_view["declared_later"] == ["origin"]
        assert lineage_view["not_recorded"] == ["recorded_timing", "decision_kind"]

    def test_status_mismatch_is_warned_with_both_values(self, tmp_project: Path):
        adr = _adr("ADR-001", DecisionStatus.PROPOSED)
        _seed(tmp_project, adr)
        _put_lineage(tmp_project, "ADR-001", _doc_for(adr, lifecycle="adopted"))
        got = _get(tmp_project, "ADR-001")
        assert got["decision"]["status"] == "proposed"
        assert got["lineage"]["lifecycle"] == "adopted"
        warning = _warning(got, "decision_status_mismatch")
        assert warning["level"] == "warning"
        assert "status proposed" in warning["message"]
        assert "lifecycle adopted" in warning["message"]
        assert "status accepted" in warning["message"]
        assert "Ask the user" in warning["remediation"]

    def test_not_found(self, tmp_project: Path):
        _seed(tmp_project, _adr("ADR-001"))
        result = _get(tmp_project, "ADR-002")
        assert result["status"] == "error"
        assert result["code"] == "decision_not_found"

    def test_an_invalid_id_is_shown_without_reading_a_lineage(self, tmp_project: Path):
        _seed(tmp_project, _adr("adr-1"))
        got = _get(tmp_project, "adr-1")
        assert got["lineage"]["derived"] is True
        assert "decision_id_invalid" in _note_codes(got)
        assert _codes(got) == ["decision_id_invalid"]
        assert _codes(_query(tmp_project)) == ["decision_id_invalid"]


# ─── links ───────────────────────────────────────────


class TestLinks:
    def test_linked_from_collects_supersedes_and_amends(self, tmp_project: Path):
        for title in ("one", "two", "three"):
            _add(tmp_project, title)
        _change(tmp_project, "ADR-002", add_links={"amends": ["ADR-001"]})
        _change(tmp_project, "ADR-003", add_links={"supersedes": ["ADR-001"]})
        got = _get(tmp_project, "ADR-001")
        assert got["lineage"]["linked_from"] == {"supersedes": ["ADR-003"], "amends": ["ADR-002"]}
        # ADR-001 does not name ADR-003 as its successor yet: one side only.
        warning = _warning(got, "decision_lineage_link_asymmetric")
        assert "ADR-003 supersedes ADR-001" in warning["message"]
        assert (
            "ADR-003 supersedes ADR-001"
            in _warning(_query(tmp_project), "decision_lineage_link_asymmetric")["message"]
        )
        assert (
            "ADR-003 supersedes ADR-001"
            in _warning(_get(tmp_project, "ADR-003"), "decision_lineage_link_asymmetric")["message"]
        )

        _change(
            tmp_project,
            "ADR-001",
            lifecycle="superseded",
            reason="replaced",
            add_links={"superseded_by": ["ADR-003"]},
        )
        for result in (
            _get(tmp_project, "ADR-001"),
            _get(tmp_project, "ADR-003"),
            _query(tmp_project),
        ):
            assert "decision_lineage_link_asymmetric" not in _codes(result), result
        assert _get(tmp_project, "ADR-001")["lineage"]["links"]["superseded_by"] == ["ADR-003"]

    def test_a_successor_without_the_reverse_link_is_one_sided(self, tmp_project: Path):
        for title in ("one", "two"):
            _add(tmp_project, title)
        _change(
            tmp_project,
            "ADR-001",
            lifecycle="superseded",
            reason="replaced",
            add_links={"superseded_by": ["ADR-002"]},
        )
        for adr_id in ("ADR-001", "ADR-002"):
            message = _warning(_get(tmp_project, adr_id), "decision_lineage_link_asymmetric")[
                "message"
            ]
            assert "ADR-001 superseded_by ADR-002" in message

    def test_unattributed_lineages_link_nothing(self, tmp_project: Path):
        target = _adr("ADR-001")
        other = _adr("ADR-002")
        _seed(tmp_project, target, other)
        links = {"supersedes": ["ADR-001"], "superseded_by": [], "amends": ["ADR-001"]}
        # An orphan (no ADR-009 in decisions.yaml) and one written for another
        # ADR that had the number 2 before.
        orphan = _doc_for(_adr("ADR-009"), links=links)
        stale = _doc_for(_adr("ADR-002", title="an older ADR-002"), links=links)
        _put_lineage(tmp_project, "ADR-009", orphan)
        _put_lineage(tmp_project, "ADR-002", stale)
        got = _get(tmp_project, "ADR-001")
        assert got["lineage"]["linked_from"] == {"supersedes": [], "amends": []}
        assert "decision_lineage_link_asymmetric" not in _codes(got)

    def test_the_linked_from_scan_stops_at_its_limit(
        self, tmp_project: Path, monkeypatch: pytest.MonkeyPatch
    ):
        for n in range(1, 6):
            _add(tmp_project, f"adr {n}")
        for n in range(2, 6):
            _change(tmp_project, f"ADR-00{n}", add_links={"amends": ["ADR-001"]})
        assert _get(tmp_project, "ADR-001")["lineage"]["linked_from"]["amends"] == [
            "ADR-002",
            "ADR-003",
            "ADR-004",
            "ADR-005",
        ]
        monkeypatch.setattr(lineage, "LINKED_FROM_SCAN_LIMIT", 2)
        got = _get(tmp_project, "ADR-001")
        assert got["lineage"]["linked_from"]["amends"] == ["ADR-002", "ADR-003"]
        assert {"code": "decision_lineage_linked_from_truncated", "count": 2} in got["lineage"][
            "notes"
        ]

    def test_the_scan_reads_in_number_order(self, tmp_project: Path, monkeypatch):
        adrs = [_adr(adr_id) for adr_id in ("ADR-001", "ADR-010", "ADR-0100", "ADR-9")]
        _seed(tmp_project, *adrs)
        for adr in adrs[1:]:
            links = {"supersedes": [], "superseded_by": [], "amends": ["ADR-001"]}
            _put_lineage(tmp_project, adr.id, _doc_for(adr, links=links))
        monkeypatch.setattr(lineage, "LINKED_FROM_SCAN_LIMIT", 2)
        assert _get(tmp_project, "ADR-001")["lineage"]["linked_from"]["amends"] == [
            "ADR-9",
            "ADR-010",
        ]


# ─── duplicates and anchors ──────────────────────────


class TestAttribution:
    def test_duplicate_ids_are_both_derived(self, tmp_project: Path):
        first = _adr("ADR-001", DecisionStatus.PROPOSED, title="first copy")
        second = _adr("ADR-001", title="second copy")
        _seed(tmp_project, first, second)
        _put_lineage(tmp_project, "ADR-001", _doc_for(first, lifecycle="rejected"))

        listed = _query(tmp_project)
        assert [(r["title"], r["derived"], r["lifecycle"]) for r in listed["decisions"]] == [
            ("first copy", True, "proposed"),
            ("second copy", True, "adopted"),
        ]
        message = _warning(listed, "decision_id_duplicate")["message"]
        assert message.count("ADR-001") == 1

        got = _get(tmp_project, "ADR-001")
        assert got["decision"]["title"] == "first copy"
        assert got["lineage"]["derived"] is True
        assert got["lineage"]["lifecycle"] == "proposed"
        assert "decision_id_duplicate" in _note_codes(got)
        assert "2 records" in _warning(got, "decision_id_duplicate")["message"]

    def test_an_anchor_mismatch_hides_the_lineage(self, tmp_project: Path):
        adr = _adr("ADR-001", DecisionStatus.PROPOSED)
        _seed(tmp_project, adr)
        older = _adr("ADR-001", title="an ADR deleted by hand")
        _put_lineage(tmp_project, "ADR-001", _doc_for(older, lifecycle="rejected"))
        got = _get(tmp_project, "ADR-001")
        assert got["lineage"]["derived"] is True
        assert got["lineage"]["lifecycle"] == "proposed"
        assert got["lineage"]["events"] == []
        assert "decision_lineage_anchor_mismatch" in _note_codes(got)
        assert _codes(got) == ["decision_lineage_anchor_mismatch"]
        assert _codes(_query(tmp_project)) == ["decision_lineage_anchor_mismatch"]

    def test_a_missing_anchor_is_a_note_not_a_warning(self, tmp_project: Path):
        adr = _adr("ADR-001")
        _seed(tmp_project, adr)
        doc = _doc_for(adr)
        del doc["anchor"]
        _put_lineage(tmp_project, "ADR-001", doc)
        got = _get(tmp_project, "ADR-001")
        assert got["lineage"]["derived"] is False
        assert "decision_lineage_anchor_missing" in _note_codes(got)
        assert got["warnings"] == []


# ─── PMSERV-253: names only, allow-listed values, exit redaction ──────


def _alias_bomb(levels: int = 6, width: int = 10) -> str:
    lines = [f"lol0: &lol0 [{', '.join(['x'] * width)}]"]
    for level in range(1, levels):
        refs = ", ".join([f"*lol{level - 1}"] * width)
        lines.append(f"lol{level}: &lol{level} [{refs}]")
    return "\n".join("  " + line for line in lines)


def _hostile_decisions_yaml() -> str:
    return (
        "decisions:\n"
        "- id: ADR-001\n"
        "  title: hostile\n"
        "  date: 2026-10-01\n"
        "  status: accepted\n"
        "  context: c\n"
        "  decision: d\n"
        "  consequences:\n"
        "    positive: []\n"
        "    extra_blob: !!binary aGVsbG8gd29ybGQ=\n"
        f"    extra_secret: token {SECRET}\n"
        "  blob: !!binary aGVsbG8gd29ybGQ=\n"
        f"  note: the key is {SECRET}\n"
        f"  {SECRET}: secret in a key name\n"
        "  nested:\n"
        f"    deeper: [{SECRET}, 1, 2]\n"
        f"{_alias_bomb()}\n"
    )


class TestUnknownKeysAreNamesOnly:
    def test_values_never_leave_and_names_are_redacted(self, tmp_project: Path):
        (_pm(tmp_project) / "decisions.yaml").write_text(
            _hostile_decisions_yaml(), encoding="utf-8"
        )
        for result in (_get(tmp_project, "ADR-001"), _query(tmp_project)):
            text = json.dumps(result)
            assert len(text.encode("utf-8")) <= 64 * 1024
            assert SECRET not in text
            assert "aGVsbG8" not in text and "hello world" not in text
        got = _get(tmp_project, "ADR-001")
        names = got["decision"]["unknown_keys"]
        assert names[:3] == ["blob", "note", "<REDACTED:secret>"]
        assert "nested" in names and "lol5" in names
        assert got["decision"]["consequences"]["unknown_keys"] == ["extra_blob", "extra_secret"]
        assert _codes(got) == ["decision_text_secrets_redacted"]

    def test_unknown_key_names_are_cut_and_capped(self, tmp_project: Path):
        keys = {f"k{n:03d}_" + "x" * 200: n for n in range(60)}
        _seed(tmp_project, _adr("ADR-001", **keys))
        names = _get(tmp_project, "ADR-001")["decision"]["unknown_keys"]
        assert len(names) == lineage.MAX_UNKNOWN_KEYS
        assert all(len(name) <= lineage.MAX_LABEL_CHARS for name in names)

    def test_nested_lineage_values_go_through_the_allow_list(self, tmp_project: Path):
        adr = _adr("ADR-001")
        _seed(tmp_project, adr)
        doc = _doc_for(adr)
        doc["fact_core"] = {"claim": SECRET}
        doc["events"].append(
            {
                "at": dt.datetime(2026, 10, 5, 3, 12),
                "kind": "lifecycle",
                "from": "proposed",
                "to": "adopted",
                "status": "accepted",
                "reason": {"nested": SECRET},
                "caused_by": b"\x00binary",
                "via": "pm_update_decision",
            }
        )
        _put_lineage(tmp_project, "ADR-001", doc)
        text = _lineage_path(tmp_project, "ADR-001").read_text(encoding="utf-8")
        assert "!!binary" in text and "2026-10-05 03:12:00" in text

        got = _get(tmp_project, "ADR-001")
        dumped = json.dumps(got)
        assert SECRET not in dumped
        last = got["lineage"]["events"][-1]
        assert last["at"] == "2026-10-05T03:12:00"
        assert "reason" not in last
        assert last["unknown_fields"] == ["caused_by"]
        assert got["lineage"]["unknown_keys"] == ["fact_core"]
        assert "decision_lineage_items_skipped" in _note_codes(got)


class TestExitRedaction:
    def test_title_status_and_lifecycle_are_redacted(self, tmp_project: Path):
        adr = _adr("ADR-001", f"draft {SECRET} " + "y" * 300, title=f"about {SECRET}")
        _seed(tmp_project, adr)
        _put_lineage(tmp_project, "ADR-001", _doc_for(adr, lifecycle=f"odd {SECRET}"))
        for result in (_get(tmp_project, "ADR-001"), _query(tmp_project)):
            dumped = json.dumps(result)
            assert SECRET not in dumped
            assert "<REDACTED:secret>" in dumped
            assert _codes(result).count("decision_text_secrets_redacted") == 1
            warning = _warning(result, "decision_text_secrets_redacted")
            assert warning["level"] == "warning"
            assert "revoke" in warning["remediation"]
        got = _get(tmp_project, "ADR-001")
        assert got["decision"]["title"] == "about <REDACTED:secret>"
        assert len(got["decision"]["status"]) <= lineage.MAX_LABEL_CHARS + 1
        assert got["lineage"]["lifecycle"] == "odd <REDACTED:secret>"
        assert "decision_lineage_lifecycle_unknown" in _note_codes(got)
        row = _query(tmp_project)["decisions"][0]
        assert row["title"] == "about <REDACTED:secret>"
        assert row["lifecycle"] == "odd <REDACTED:secret>"

    def test_body_and_event_text_are_redacted(self, tmp_project: Path):
        _add(tmp_project, consequences_negative=[f"leaks {SECRET}"])
        _change(tmp_project, "ADR-001", note="fine")
        path = _lineage_path(tmp_project, "ADR-001")
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        doc["events"][-1]["text"] = f"pasted {SECRET}"  # a hand edit after the fact
        _put_lineage(tmp_project, "ADR-001", doc)
        got = _get(tmp_project, "ADR-001")
        assert SECRET not in json.dumps(got)
        assert got["decision"]["consequences"]["negative"] == ["leaks <REDACTED:secret>"]
        assert got["lineage"]["events"][-1]["text"] == "pasted <REDACTED:secret>"
        assert "2 secret-like" in _warning(got, "decision_text_secrets_redacted")["message"]

    def test_declared_values_and_lineage_key_names_are_redacted(self, tmp_project: Path):
        adr = _adr("ADR-001")
        _seed(tmp_project, adr)
        doc = _doc_for(adr)
        doc["declared"]["origin"] = f"pasted {SECRET}"
        doc[f"key {SECRET}"] = "value"
        doc["events"][0][f"field {SECRET}"] = "value"
        _put_lineage(tmp_project, "ADR-001", doc)
        got = _get(tmp_project, "ADR-001")
        listed = _query(tmp_project)
        for result in (got, listed):
            assert SECRET not in json.dumps(result)
            assert "decision_text_secrets_redacted" in _codes(result)
        assert got["lineage"]["declared"]["origin"] == "pasted <REDACTED:secret>"
        assert got["lineage"]["unknown_keys"] == ["key <REDACTED:secret>"]
        assert got["lineage"]["events"][0]["unknown_fields"] == ["field <REDACTED:secret>"]
        assert listed["decisions"][0]["origin"] == "pasted <REDACTED:secret>"

    def test_a_secret_only_in_an_unknown_status_is_counted(self, tmp_project: Path):
        # The status label is redacted before the exit pass, which then finds
        # nothing; the label's own count must still reach the warning.
        adr = _adr("ADR-001", f"draft {SECRET}")
        _seed(tmp_project, adr)
        _put_lineage(tmp_project, "ADR-001", _doc_for(adr))  # adopted: a mismatch
        got, listed = _get(tmp_project, "ADR-001"), _query(tmp_project)
        for result in (got, listed):
            assert SECRET not in json.dumps(result)
            assert _codes(result).count("decision_text_secrets_redacted") == 1
            warning = _warning(result, "decision_text_secrets_redacted")
            assert warning["message"].startswith("1 secret-like")
        assert got["decision"]["status"] == "draft <REDACTED:secret>"
        mismatch = _warning(got, "decision_status_mismatch")["message"]
        assert "status draft <REDACTED:secret>, lifecycle adopted" in mismatch
        assert listed["decisions"][0]["status"] == "draft <REDACTED:secret>"

    def test_a_secret_only_in_a_malformed_id_is_counted(self, tmp_project: Path):
        bad_id = f"ADR-{SECRET}"
        _seed(tmp_project, _adr(bad_id, title="clean"))
        # The last call filters the row out; decision_id_invalid still names it.
        results = (
            _get(tmp_project, bad_id),
            _query(tmp_project),
            _query(tmp_project, lifecycle="proposed"),
        )
        for result in results:
            dumped = json.dumps(result)
            assert SECRET not in dumped and "ADR-<REDACTED:secret>" in dumped
            assert _codes(result) == ["decision_id_invalid", "decision_text_secrets_redacted"]
            warning = _warning(result, "decision_text_secrets_redacted")
            assert warning["message"].startswith("1 secret-like")
        assert results[0]["decision"]["id"] == "ADR-<REDACTED:secret>"
        assert _ids(results[1]) == ["ADR-<REDACTED:secret>"] and _ids(results[2]) == []

    def test_a_long_run_is_withheld_unscanned_and_counted(self, tmp_project: Path):
        # Some redaction patterns take time quadratic in a run without
        # whitespace: unscanned-run withholding is what keeps these two calls
        # from taking about 15 s each on a 128 KiB run.
        run = "a" * (128 * 1024)
        token = "t" * (lineage.MAX_SCAN_RUN_CHARS + 1)
        context = f"see {run} and Bearer {token} then {SECRET}"
        _seed(tmp_project, _adr("ADR-001", title=run, context=context))
        started = time.perf_counter()
        got, listed = _get(tmp_project, "ADR-001"), _query(tmp_project)
        assert time.perf_counter() - started < 5
        hidden = lineage.UNSCANNED_PLACEHOLDER
        assert got["decision"]["title"] == hidden
        assert got["decision"]["context"] == (
            f"see {hidden} and Bearer {hidden} then <REDACTED:secret>"
        )
        assert listed["decisions"][0]["title"] == hidden
        for result, count in ((got, 4), (listed, 1)):
            message = _warning(result, "decision_text_secrets_redacted")["message"]
            assert message.startswith(f"{count} secret-like")
            assert f"shown as {hidden} without being scanned" in message
            assert len(json.dumps(result)) < 8 * 1024

    def test_the_run_limit_boundary_and_the_scanned_worst_case(self):
        limit = lineage.MAX_SCAN_RUN_CHARS
        at_limit = SECRET + "x" * (limit - len(SECRET))
        scrubbed, count = lineage.scrub_view(
            {"at": at_limit, "over": at_limit + "x", "long": f"{at_limit} tail"}
        )
        kept = "<REDACTED:secret>" + "x" * (limit - len(SECRET))
        assert scrubbed == {
            "at": kept,
            "over": lineage.UNSCANNED_PLACEHOLDER,
            "long": f"{kept} tail",
        }
        assert count == 3
        # The slowest text the pass still scans: runs of exactly the limit,
        # here 128 KiB of them (about 0.5 s; time grows with the limit).
        runs = ("a" * limit + " ") * (128 * 1024 // (limit + 1))
        started = time.perf_counter()
        assert lineage.scrub_view(runs) == (runs, 0)
        assert time.perf_counter() - started < 5

    def test_the_files_are_not_changed_by_redaction(self, tmp_project: Path):
        _seed(tmp_project, _adr("ADR-001", title=f"about {SECRET}"))
        before = _snapshot(_pm(tmp_project))
        _get(tmp_project, "ADR-001")
        _query(tmp_project)
        assert _snapshot(_pm(tmp_project)) == before


# ─── errors ──────────────────────────────────────────


class TestBrokenDecisionsYaml:
    @pytest.mark.parametrize(
        "text, detail",
        [
            (
                f'decisions:\n- id: ADR-001\n  title: "unterminated {SECRET}\n  status: x\n',
                "at line",
            ),
            (f"decisions:\n- id: ADR-001\n  title: [{SECRET}, 2]\n", "ValidationError"),
        ],
        ids=["yaml-syntax", "validation"],
    )
    def test_the_error_never_quotes_the_file(self, tmp_project: Path, text: str, detail: str):
        (_pm(tmp_project) / "decisions.yaml").write_text(text, encoding="utf-8")
        before = _snapshot(_pm(tmp_project))
        for result in (_query(tmp_project), _get(tmp_project, "ADR-001")):
            assert result["status"] == "error"
            assert result["code"] == "decisions_yaml_unreadable"
            assert detail in result["message"]
            dumped = json.dumps(result)
            assert SECRET not in dumped
            assert "unterminated" not in dumped
        assert _snapshot(_pm(tmp_project)) == before


def _write_unreadable(project: Path) -> None:
    (_pm(project) / "decisions.yaml").write_text("decisions: [\n", encoding="utf-8")


def _write_good(project: Path) -> None:
    adr = _adr("ADR-001")
    _seed(project, adr)
    _put_lineage(project, "ADR-001", _doc_for(adr))


@pytest.mark.parametrize(
    "setup, kwargs, code",
    [
        (_write_good, {"action": "update"}, "invalid_action"),
        (_write_good, {"action": "get"}, "decision_id_required"),
        (_write_good, {"action": "get", "decision_id": ""}, "decision_id_required"),
        (_write_unreadable, {}, "decisions_yaml_unreadable"),
        (
            _write_unreadable,
            {"action": "get", "decision_id": "ADR-001"},
            "decisions_yaml_unreadable",
        ),
        (_write_good, {"action": "get", "decision_id": "ADR-999"}, "decision_not_found"),
        (_write_good, {"lifecycle": "accepted"}, "invalid_lifecycle"),
        (_write_good, {"lifecycle": "nonsense"}, "invalid_lifecycle"),
    ],
)
def test_every_error_code_returns_an_error_dict_and_writes_nothing(
    tmp_project: Path, setup, kwargs: dict, code: str
):
    setup(tmp_project)
    before = _snapshot(_pm(tmp_project))
    result = _query(tmp_project, **kwargs)
    assert result["status"] == "error"
    assert result["code"] == code
    assert isinstance(result["message"], str) and result["message"]
    assert _snapshot(_pm(tmp_project)) == before


def test_an_invalid_action_is_refused_before_anything_is_read(tmp_path: Path):
    # No project at all: the action is checked first.
    result = pm_decision_query(action="delete", project_path=str(tmp_path / "missing"))
    assert result["code"] == "invalid_action"


# ─── hostile lineage files ───────────────────────────


def _make_fifo(path: Path, tmp_path: Path) -> None:
    if not hasattr(os, "mkfifo"):
        pytest.skip("no FIFOs on this platform")
    os.mkfifo(path)


def _make_symlink(path: Path, tmp_path: Path) -> None:
    target = tmp_path / "elsewhere.yaml"
    target.write_text("decision_id: ADR-001\nlifecycle: rejected\n", encoding="utf-8")
    path.symlink_to(target)


def _make_oversized(path: Path, tmp_path: Path) -> None:
    path.write_text("decision_id: ADR-001\n# " + "x" * lineage.MAX_LINEAGE_BYTES, encoding="utf-8")


def _make_directory(path: Path, tmp_path: Path) -> None:
    path.mkdir()


@pytest.mark.parametrize("make", [_make_fifo, _make_symlink, _make_oversized, _make_directory])
def test_hostile_lineage_files_neither_block_nor_raise(tmp_project: Path, tmp_path: Path, make):
    _seed(tmp_project, _adr("ADR-001"), _adr("ADR-002"))
    directory = _pm(tmp_project) / "decision_lineage"
    directory.mkdir()
    make(directory / "ADR-001.yaml", tmp_path)
    results: dict[str, object] = {}

    def run() -> None:
        try:
            results["get"] = _get(tmp_project, "ADR-001")
            results["other"] = _get(tmp_project, "ADR-002")  # scans the bad file
            results["list"] = _query(tmp_project)
        except Exception as exc:  # noqa: BLE001 - reported by the assertion below
            results["error"] = exc

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(timeout=2)
    assert not worker.is_alive(), "pm_decision_query blocked on a hostile lineage file"
    assert "error" not in results, results.get("error")
    got = results["get"]
    assert isinstance(got, dict)
    assert got["lineage"]["derived"] is True
    assert "decision_lineage_unreadable" in _note_codes(got)
    assert "decision_lineage_unreadable" in _codes(got)
    listed = results["list"]
    assert isinstance(listed, dict)
    assert "ADR-001" in _warning(listed, "decision_lineage_unreadable")["message"]
    other = results["other"]
    assert isinstance(other, dict) and other["lineage"]["linked_from"] == {
        "supersedes": [],
        "amends": [],
    }


def test_a_symlinked_lineage_directory_is_not_followed(tmp_project: Path, tmp_path: Path):
    adr = _adr("ADR-001")
    _seed(tmp_project, adr, _adr("ADR-002"))
    real = tmp_path / "real_lineage"
    real.mkdir()
    links = {"supersedes": [], "superseded_by": [], "amends": ["ADR-002"]}
    (real / "ADR-001.yaml").write_text(
        dump_lineage(_doc_for(adr, links=links), "ADR-001"), encoding="utf-8"
    )
    (_pm(tmp_project) / "decision_lineage").symlink_to(real)
    got = _get(tmp_project, "ADR-001")
    assert got["lineage"]["derived"] is True
    assert "decision_lineage_unreadable" in _note_codes(got)
    assert _get(tmp_project, "ADR-002")["lineage"]["linked_from"]["amends"] == []
