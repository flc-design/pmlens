"""Forward compatibility of whole-file ledger rewrites (PMSERV-218 / ADR-056).

decisions.yaml and knowledge.yaml are rewritten in full on every mutation. Under
the pydantic default (extra="ignore") a rewrite silently dropped every key the
models did not declare — so an older pmlens erased fields a newer one had added,
and a single unknown ``status`` made the whole decisions.yaml unreadable. These
tests pin the fixed behaviour: unknown keys (per record, nested, and top-level)
survive a rewrite, and an unknown ADR status is kept rather than fatal.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from pmlens.models import Decision, DecisionStatus, KnowledgeCategory, KnowledgeRecord
from pmlens.storage import (
    add_decision,
    add_knowledge,
    load_decisions,
    load_knowledge,
    unknown_decision_statuses,
    update_knowledge,
)


def _write(path: Path, doc: dict) -> None:
    path.write_text(yaml.safe_dump(doc, allow_unicode=True, sort_keys=False), encoding="utf-8")


def _read(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _adr(adr_id: str, **extra) -> dict:
    base = {
        "id": adr_id,
        "title": f"title {adr_id}",
        "date": "2026-10-01",
        "status": "accepted",
        "context": "c",
        "decision": "d",
        "consequences": {"positive": [], "negative": [], "mitigations": []},
    }
    base.update(extra)
    return base


class TestDecisionsRoundTrip:
    def test_unknown_record_fields_survive_append(self, tmp_pm_path: Path):
        path = tmp_pm_path / "decisions.yaml"
        _write(path, {"decisions": [_adr("ADR-001", origin="ai_auto", fact_core={"version": 1})]})

        add_decision(tmp_pm_path, Decision(id="ADR-002", title="new"))

        first = _read(path)["decisions"][0]
        assert first["origin"] == "ai_auto"
        assert first["fact_core"] == {"version": 1}

    def test_unknown_nested_consequence_keys_survive(self, tmp_pm_path: Path):
        path = tmp_pm_path / "decisions.yaml"
        consequences = {"positive": ["p"], "negative": [], "mitigations": [], "risks": ["r"]}
        _write(path, {"decisions": [_adr("ADR-001", consequences=consequences)]})

        add_decision(tmp_pm_path, Decision(id="ADR-002", title="new"))

        assert _read(path)["decisions"][0]["consequences"]["risks"] == ["r"]

    def test_top_level_keys_survive_and_keep_their_order(self, tmp_pm_path: Path):
        path = tmp_pm_path / "decisions.yaml"
        _write(path, {"schema": 2, "decisions": [_adr("ADR-001")], "meta": {"k": "v"}})

        add_decision(tmp_pm_path, Decision(id="ADR-002", title="new"))

        doc = _read(path)
        assert list(doc) == ["schema", "decisions", "meta"]
        assert doc["schema"] == 2
        assert doc["meta"] == {"k": "v"}
        assert [d["id"] for d in doc["decisions"]] == ["ADR-001", "ADR-002"]

    def test_unknown_status_does_not_break_the_file(self, tmp_pm_path: Path):
        path = tmp_pm_path / "decisions.yaml"
        _write(path, {"decisions": [_adr("ADR-001"), _adr("ADR-002", status="adopted")]})

        loaded = load_decisions(tmp_pm_path)

        assert [d.id for d in loaded] == ["ADR-001", "ADR-002"]
        assert loaded[0].status is DecisionStatus.ACCEPTED
        assert loaded[1].status == "adopted"
        assert unknown_decision_statuses(loaded) == [{"id": "ADR-002", "status": "adopted"}]

    def test_unknown_status_is_written_back_unchanged(self, tmp_pm_path: Path):
        path = tmp_pm_path / "decisions.yaml"
        _write(path, {"decisions": [_adr("ADR-001", status="adopted")]})

        add_decision(tmp_pm_path, Decision(id="ADR-002", title="new"))

        statuses = [d["status"] for d in _read(path)["decisions"]]
        assert statuses == ["adopted", "accepted"]

    def test_known_statuses_still_parse_as_the_enum(self, tmp_pm_path: Path):
        path = tmp_pm_path / "decisions.yaml"
        _write(
            path,
            {
                "decisions": [
                    _adr(f"ADR-00{i}", status=s.value) for i, s in enumerate(DecisionStatus, 1)
                ]
            },
        )

        loaded = load_decisions(tmp_pm_path)

        assert [d.status for d in loaded] == list(DecisionStatus)
        assert all(isinstance(d.status, DecisionStatus) for d in loaded)
        assert unknown_decision_statuses(loaded) == []


class TestKnowledgeRoundTrip:
    def _kr(self, kr_id: str, **extra) -> dict:
        base = {"id": kr_id, "category": "constraint", "title": f"t {kr_id}"}
        base.update(extra)
        return base

    def test_unknown_record_fields_survive_append(self, tmp_pm_path: Path):
        path = tmp_pm_path / "knowledge.yaml"
        _write(path, {"knowledge": [self._kr("KR-001", provider="staff-a")]})

        add_knowledge(
            tmp_pm_path, KnowledgeRecord(id="KR-002", category=KnowledgeCategory.SPEC, title="n")
        )

        assert _read(path)["knowledge"][0]["provider"] == "staff-a"

    def test_unknown_fields_survive_update(self, tmp_pm_path: Path):
        path = tmp_pm_path / "knowledge.yaml"
        _write(path, {"knowledge": [self._kr("KR-001", provider="staff-a")]})

        update_knowledge(tmp_pm_path, "KR-001", conclusion="done")

        record = _read(path)["knowledge"][0]
        assert record["provider"] == "staff-a"
        assert record["conclusion"] == "done"

    def test_top_level_keys_survive(self, tmp_pm_path: Path):
        path = tmp_pm_path / "knowledge.yaml"
        _write(path, {"knowledge": [self._kr("KR-001")], "schema": 2})

        add_knowledge(
            tmp_pm_path, KnowledgeRecord(id="KR-002", category=KnowledgeCategory.SPEC, title="n")
        )

        doc = _read(path)
        assert doc["schema"] == 2
        assert [k["id"] for k in doc["knowledge"]] == ["KR-001", "KR-002"]
        assert [k.id for k in load_knowledge(tmp_pm_path)] == ["KR-001", "KR-002"]


def test_decision_status_value_set_is_frozen():
    """D1, first half (ADR-056): the stored ADR status vocabulary is fixed.

    Already-shipped readers (Desktop 0.15.0, pipx 0.16.0) validate ``status``
    as a strict enum, so writing any new value makes the WHOLE decisions.yaml
    unreadable for them. Lifecycle values beyond these four live in the
    per-ADR lineage file and are projected onto this set (ADR-056). Changing
    this set needs a new ADR and a staged rollout, not just an edit here.
    """
    assert [s.value for s in DecisionStatus] == ["proposed", "accepted", "deprecated", "superseded"]


class TestWriteSideAndSurfacing:
    """Review follow-ups for PMSERV-218 (SEC-04 / SEM-02 / SEM-03 / PP-5)."""

    def _project(self, tmp_path: Path, sample_project) -> Path:
        from pmlens.storage import _save_project, init_pm_directory

        pm_path = init_pm_directory(tmp_path)
        _save_project(pm_path, sample_project)
        return pm_path

    def test_a_new_adr_must_use_a_known_status(self, tmp_pm_path: Path):
        from pmlens.models import PmServerError
        from pmlens.storage import add_decision_with_next_id

        with pytest.raises(PmServerError, match="not one of"):
            add_decision(tmp_pm_path, Decision(id="ADR-001", title="t", status="adopted"))
        with pytest.raises(PmServerError, match="not one of"):
            add_decision_with_next_id(
                tmp_pm_path, lambda n: Decision(id=f"ADR-{n:03d}", title="t", status="rejected")
            )
        assert not (tmp_pm_path / "decisions.yaml").exists()

    def test_pm_status_reports_unknown_statuses(self, tmp_path: Path, sample_project):
        from pmlens.server import pm_status

        pm_path = self._project(tmp_path, sample_project)
        _write(
            pm_path / "decisions.yaml",
            {"decisions": [_adr("ADR-001"), _adr("ADR-002", status="adopted")]},
        )

        codes = {w["code"]: w for w in pm_status(project_path=str(tmp_path))["warnings"]}

        assert "ADR-002='adopted'" in codes["decision_status_unknown"]["message"]

    def test_pm_status_reports_an_unreadable_ledger_instead_of_failing(
        self, tmp_path: Path, sample_project
    ):
        from pmlens.server import pm_status

        pm_path = self._project(tmp_path, sample_project)
        (pm_path / "decisions.yaml").write_text("decisions:\n- id: ADR-001\n  status: ~\n")

        codes = {w["code"] for w in pm_status(project_path=str(tmp_path))["warnings"]}

        assert "decisions_yaml_unreadable" in codes

    # PMSERV-258: the warnings used to embed ``str(exc)`` and the raw status
    # values, which quote file content — a pasted credential among it. Lens's
    # pm_status reaches this code, so the text would also leave via Desktop.
    _SECRET = "AKIA" + "Q" * 16

    def _warnings_text(self, tmp_path: Path) -> tuple[str, dict]:
        import json

        from pmlens.server import pm_status

        warnings = pm_status(project_path=str(tmp_path))["warnings"]
        return json.dumps(warnings), {w["code"]: w for w in warnings}

    def test_unreadable_yaml_names_the_position_not_the_line(self, tmp_path: Path, sample_project):
        pm_path = self._project(tmp_path, sample_project)
        (pm_path / "decisions.yaml").write_text(
            "decisions:\n- id: ADR-001\n  title: t\n"
            f'  context: "{self._SECRET} never closed\n  status: accepted\n',
            encoding="utf-8",
        )

        text, codes = self._warnings_text(tmp_path)

        assert self._SECRET not in text
        message = codes["decisions_yaml_unreadable"]["message"]
        assert "Error at line " in message and ", column " in message

    def test_validation_error_does_not_echo_the_input(self, tmp_path: Path, sample_project):
        pm_path = self._project(tmp_path, sample_project)
        _write(pm_path / "decisions.yaml", {"decisions": [_adr("ADR-001", title=[self._SECRET])]})

        text, codes = self._warnings_text(tmp_path)

        assert self._SECRET not in text
        assert codes["decisions_yaml_unreadable"]["message"].endswith(": ValidationError")

    def test_unknown_status_is_cut_and_redacted(self, tmp_path: Path, sample_project):
        pm_path = self._project(tmp_path, sample_project)
        status = f"{self._SECRET} " + "x" * 500
        _write(pm_path / "decisions.yaml", {"decisions": [_adr("ADR-001", status=status)]})

        text, codes = self._warnings_text(tmp_path)

        assert self._SECRET not in text
        message = codes["decision_status_unknown"]["message"]
        assert "ADR-001=" in message and "<REDACTED:secret>" in message
        assert "x" * 101 not in message

    def test_a_long_list_of_unknown_statuses_is_capped(self, tmp_path: Path, sample_project):
        pm_path = self._project(tmp_path, sample_project)
        adrs = [_adr(f"ADR-{i:03d}", status="adopted") for i in range(1, 61)]
        _write(pm_path / "decisions.yaml", {"decisions": adrs})

        _, codes = self._warnings_text(tmp_path)

        message = codes["decision_status_unknown"]["message"]
        assert "ADR-050=" in message and "ADR-051=" not in message
        assert "and 10 more" in message

    def test_pm_status_is_quiet_for_a_healthy_ledger(self, tmp_path: Path, sample_project):
        from pmlens.server import pm_status

        pm_path = self._project(tmp_path, sample_project)
        _write(pm_path / "decisions.yaml", {"decisions": [_adr("ADR-001")]})

        codes = {w["code"] for w in pm_status(project_path=str(tmp_path))["warnings"]}

        assert not codes & {"decision_status_unknown", "decisions_yaml_unreadable"}

    def test_dashboard_renders_known_and_unknown_statuses(self, tmp_path: Path, sample_project):
        from pmlens.dashboard import render_project_dashboard

        pm_path = self._project(tmp_path, sample_project)
        _write(
            pm_path / "decisions.yaml",
            {"decisions": [_adr("ADR-001"), _adr("ADR-002", status="adopted")]},
        )

        html = render_project_dashboard(pm_path, format="html")

        assert "(accepted)" in html
        assert "(adopted)" in html


class TestUnknownValuesAreWrittenBackVerbatim:
    """Review follow-ups SEC-01 / SEC-02 / F6: extras are re-emitted as the
    objects ``safe_load`` produced, never re-serialised through JSON mode."""

    @staticmethod
    def _alias_bomb(depth: int = 7, width: int = 9) -> str:
        lines = ["a0: &a0 [" + ", ".join(["x"] * width) + "]"]
        for i in range(1, depth + 1):
            lines.append(f"a{i}: &a{i} [" + ", ".join([f"*a{i - 1}"] * width) + "]")
        return "\n".join(lines) + "\n"

    def test_alias_bomb_in_an_unknown_key_is_not_expanded(self, tmp_pm_path: Path):
        import time

        path = tmp_pm_path / "decisions.yaml"
        path.write_text(
            self._alias_bomb()
            + "decisions:\n- id: ADR-001\n  title: t\n  x: *a7\n"
            + "  consequences: {positive: [p], y: *a7}\n",
            encoding="utf-8",
        )

        started = time.monotonic()
        add_decision(tmp_pm_path, Decision(id="ADR-002", title="new"))

        # 9**8 leaves fully expanded would be ~117 MB and ~25 s (measured).
        assert time.monotonic() - started < 5
        assert path.stat().st_size < 20_000

    def test_yaml_typed_values_keep_their_type(self, tmp_pm_path: Path):
        import datetime as dt
        import math

        path = tmp_pm_path / "decisions.yaml"
        path.write_text(
            "decisions:\n- id: ADR-001\n  title: t\n"
            "  blob: !!binary /w==\n  when: 2026-10-01\n  ratio: .nan\n"
            "  table: {1: one}\n  loop: &l [*l]\n",
            encoding="utf-8",
        )

        add_decision(tmp_pm_path, Decision(id="ADR-002", title="new"))

        first = _read(path)["decisions"][0]
        assert first["blob"] == b"\xff"
        assert first["when"] == dt.date(2026, 10, 1)
        assert math.isnan(first["ratio"])
        assert first["table"] == {1: "one"}
        assert first["loop"][0] is first["loop"]
