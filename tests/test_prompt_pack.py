"""Tests for prompt_pack.py + pm_prompt_pack (PMSERV-154 v1).

Covers the pure generator (build_prompt_pack_md / render_task_card / ADR
linkage / verify-command reading / fence escalation) and the pm_prompt_pack
tool (filtering, done-exclusion, task_ids override, format guard, read-path
purity over the SSoT, and Lens (PM_LENS=1) hiding — the tool writes, so it must
never register on a read-only host per PMSERV-144).
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

import pmlens.server as srv
from pmlens.memory import MemoryStore
from pmlens.models import (
    Decision,
    Memory,
    MemoryType,
    Priority,
    Project,
    SuggestedModel,
    Task,
    TaskStatus,
)
from pmlens.prompt_pack import (
    _condense,
    _fence,
    adr_refs_for_task,
    build_prompt_body,
    build_prompt_pack_html,
    build_prompt_pack_md,
    group_tasks,
    read_verify_commands,
    render_task_card,
    run_prompt_pack,
    task_node,
)
from pmlens.storage import (
    _save_decisions,
    _save_project,
    _save_tasks,
    init_pm_directory,
    load_project,
    load_tasks,
)


def _task(**kw) -> Task:
    base = {
        "id": "P-1",
        "title": "サンプルタスク",
        "phase": "phase-1",
        "description": "",
        "acceptance_criteria": [],
        "tags": [],
        "blocked_by": [],
    }
    base.update(kw)
    return Task(**base)


# ─── Pure generator ─────────────────────────────────────────────


class TestAdrRefsForTask:
    def test_from_description_and_memory_deduped_ordered(self):
        task = _task(description="ADR-039 の不変条件と ADR-039 再掲、さらに ADR-012 も参照")
        mems = [
            Memory(
                session_id="s",
                type=MemoryType.LESSON,
                content="x",
                project="p",
                decision_id="ADR-099",
            ),
            Memory(
                session_id="s",
                type=MemoryType.LESSON,
                content="y",
                project="p",
                decision_id="ADR-012",
            ),  # dup of description ref
        ]
        # Description refs first (text order, deduped), then new memory refs.
        assert adr_refs_for_task(task, mems) == ["ADR-039", "ADR-012", "ADR-099"]

    def test_no_refs(self):
        assert adr_refs_for_task(_task(description="no adr here"), []) == []


class TestRenderTaskCard:
    def test_sections_and_linkage(self):
        task = _task(
            id="P-7",
            title="機能実装",
            description="ADR-001 に従い実装する",
            blocked_by=["P-6"],
            acceptance_criteria=["X が動く", "テストが緑"],
            priority=Priority.P1,
        )
        mems = [
            Memory(
                session_id="s",
                type=MemoryType.LESSON,
                content="過去に settings.json を壊した",
                project="p",
            ),
            Memory(
                session_id="s", type=MemoryType.OBSERVATION, content="これはノイズ", project="p"
            ),
        ]
        decisions = {"ADR-001": Decision(id="ADR-001", title="基本方針")}
        card = render_task_card(
            task, memories=mems, decisions_by_id=decisions, verify_commands=["pytest -q"]
        )

        assert "### P-7 — 機能実装" in card
        assert "## 準備" in card
        assert "依存（先に完了が必要）: P-6" in card
        assert "関連 ADR を確認: ADR-001 — 基本方針" in card
        assert "## 内容" in card
        assert "ADR-001 に従い実装する" in card
        # Caution: lesson surfaced, observation filtered out.
        assert "## 注意" in card
        assert "settings.json を壊した" in card
        assert "これはノイズ" not in card
        # Completion: acceptance criteria + verify command + pm flow.
        assert "- X が動く" in card
        assert "検証コマンド: `pytest -q`" in card
        assert "pm_update_task done" in card
        # Paste-ready fenced block.
        assert "```text" in card

    def test_no_caution_section_when_only_observations(self):
        task = _task(description="d", acceptance_criteria=["a"])
        mems = [Memory(session_id="s", type=MemoryType.OBSERVATION, content="obs", project="p")]
        card = render_task_card(task, memories=mems, decisions_by_id={}, verify_commands=[])
        assert "## 注意" not in card

    def test_empty_description_placeholder(self):
        card = render_task_card(
            _task(description="   "), memories=[], decisions_by_id={}, verify_commands=[]
        )
        assert "description 未記載" in card


class TestFence:
    def test_default_three_backticks(self):
        assert _fence("plain body no backticks") == "```"

    def test_escalates_past_inner_code_fence(self):
        # A description containing a ``` fence must be wrapped in a longer one.
        body = "here is code:\n```\ncode\n```\n"
        fence = _fence(body)
        assert fence == "````"
        assert len(fence) > 3

    def test_card_with_code_fence_in_description_stays_wrapped(self):
        task = _task(description="```\nsome code\n```")
        card = render_task_card(task, memories=[], decisions_by_id={}, verify_commands=[])
        assert "````text" in card


class TestCondense:
    def test_collapses_whitespace(self):
        assert _condense("a\n  b\t c") == "a b c"

    def test_truncates(self):
        out = _condense("あ" * 500, limit=10)
        assert len(out) == 10
        assert out.endswith("…")


class TestBuildPromptPackMd:
    def test_header_count_and_cards(self):
        project = Project(name="proj", display_name="My Proj")
        tasks = [_task(id="P-1", title="one"), _task(id="P-2", title="two")]
        md = build_prompt_pack_md(
            tasks,
            project=project,
            memories_by_task={},
            decisions_by_id={},
            verify_commands=[],
            filter_label="tag=x",
        )
        assert "# 実装セッション プロンプトパック — My Proj" in md
        assert "対象: tag=x" in md
        assert "タスク数: 2" in md
        assert "### P-1 — one" in md
        assert "### P-2 — two" in md
        assert "## 共通運用ルール" in md


class TestReadVerifyCommands:
    def _write_project(self, pm_path: Path, body: str) -> None:
        (pm_path / "project.yaml").write_text(body, encoding="utf-8")

    def test_list(self, tmp_path):
        pm = tmp_path / ".pm"
        pm.mkdir()
        self._write_project(pm, "name: p\nverify_commands:\n  - pytest -q\n  - ruff check\n")
        assert read_verify_commands(pm) == ["pytest -q", "ruff check"]

    def test_scalar_string(self, tmp_path):
        pm = tmp_path / ".pm"
        pm.mkdir()
        self._write_project(pm, "name: p\nverify_commands: make test\n")
        assert read_verify_commands(pm) == ["make test"]

    def test_absent(self, tmp_path):
        pm = tmp_path / ".pm"
        pm.mkdir()
        self._write_project(pm, "name: p\n")
        assert read_verify_commands(pm) == []

    def test_malformed_ignored(self, tmp_path):
        pm = tmp_path / ".pm"
        pm.mkdir()
        self._write_project(pm, "name: p\nverify_commands: 123\n")
        assert read_verify_commands(pm) == []

    def test_missing_file(self, tmp_path):
        pm = tmp_path / ".pm"
        pm.mkdir()
        assert read_verify_commands(pm) == []

    @pytest.mark.parametrize("body", ["- a\n- b\n", "just a bare string\n", "42\n"])
    def test_non_mapping_root_ignored(self, tmp_path, body):
        # valid YAML whose root is a list / scalar / int must not AttributeError
        # (the "returns [] when malformed" tolerance contract). Regression for
        # the PMSERV-154 adversarial-verify finding.
        pm = tmp_path / ".pm"
        pm.mkdir()
        self._write_project(pm, body)
        assert read_verify_commands(pm) == []


# ─── Tool: pm_prompt_pack ───────────────────────────────────────


def _seed(tmp_path: Path, tasks, decisions=None, memories=None) -> Path:
    pm_path = init_pm_directory(tmp_path)
    _save_tasks(pm_path, tasks)
    if decisions:
        _save_decisions(pm_path, decisions)
    if memories:
        store = MemoryStore(pm_path / "memory.db", global_db_path=None)
        for m in memories:
            store.save(m)
        store.close()
    return pm_path


class TestPmPromptPackTool:
    def test_generates_pack_and_excludes_done(self, tmp_path):
        tasks = [
            _task(id="P-1", title="todo one", tags=["batch"], status=TaskStatus.TODO),
            _task(id="P-2", title="todo two", tags=["batch"], status=TaskStatus.TODO),
            _task(id="P-3", title="done one", tags=["batch"], status=TaskStatus.DONE),
        ]
        _seed(tmp_path, tasks)
        res = srv.pm_prompt_pack(filter_tag="batch", project_path=str(tmp_path))

        assert res["status"] == "ok"
        assert res["task_count"] == 2
        assert set(res["task_ids"]) == {"P-1", "P-2"}
        assert res["format"] == "md"
        out = Path(res["out_path"])
        assert out.exists()
        assert out.parent == tmp_path / ".pm" / "exports"
        assert out.suffix == ".md"  # default extension tracks the format
        md = out.read_text(encoding="utf-8")
        assert "### P-1" in md and "### P-2" in md
        assert "P-3" not in md  # done excluded

    def test_task_ids_override_includes_done(self, tmp_path):
        tasks = [_task(id="P-9", title="done", tags=["x"], status=TaskStatus.DONE)]
        _seed(tmp_path, tasks)
        res = srv.pm_prompt_pack(task_ids=["P-9"], project_path=str(tmp_path))
        assert res["task_count"] == 1
        assert res["task_ids"] == ["P-9"]

    def test_format_html_generates_self_contained_file(self, tmp_path):
        _seed(tmp_path, [_task(id="P-1", title="html task", tags=["x"])])
        res = srv.pm_prompt_pack(format="html", filter_tag="x", project_path=str(tmp_path))
        assert res["status"] == "ok"
        assert res["format"] == "html"
        out = Path(res["out_path"])
        assert out.suffix == ".html" and out.exists()
        html = out.read_text(encoding="utf-8")
        assert "<!doctype html>" in html.lower()
        assert "copy-btn" in html and "P-1" in html
        # self-contained: no external http(s) references
        import re as _re

        assert not _re.findall(r'https?://[^\s"\')]+', html)

    def test_format_unknown_is_error(self, tmp_path):
        _seed(tmp_path, [_task(tags=["x"])])
        res = srv.pm_prompt_pack(format="pdf", filter_tag="x", project_path=str(tmp_path))
        assert res["status"] == "error"
        assert "md" in res["message"] and "html" in res["message"]

    def test_group_by_unknown_is_error(self, tmp_path):
        _seed(tmp_path, [_task(tags=["x"])])
        res = srv.pm_prompt_pack(
            format="html", group_by="sprint", filter_tag="x", project_path=str(tmp_path)
        )
        assert res["status"] == "error"
        assert "group_by" in res["message"]

    def test_no_match_warns(self, tmp_path):
        _seed(tmp_path, [_task(tags=["a"])])
        res = srv.pm_prompt_pack(filter_tag="nope", project_path=str(tmp_path))
        assert res["status"] == "ok"
        assert res["task_count"] == 0
        assert res["out_path"] is None
        assert res["warnings"]

    def test_out_path_override(self, tmp_path):
        _seed(tmp_path, [_task(id="P-1", tags=["x"])])
        dest = tmp_path / "custom" / "pack.md"
        res = srv.pm_prompt_pack(filter_tag="x", out_path=str(dest), project_path=str(tmp_path))
        assert Path(res["out_path"]) == dest
        assert dest.exists()

    def test_out_path_rejects_reserved_ssot_name(self, tmp_path):
        # Defense-in-depth: an out_path aimed at an SSoT file must be refused so
        # the single write can never clobber the source of truth it reads.
        pm_path = _seed(tmp_path, [_task(id="P-1", tags=["x"])])
        tasks_before = (pm_path / "tasks.yaml").read_bytes()
        res = srv.pm_prompt_pack(
            filter_tag="x",
            out_path=str(pm_path / "tasks.yaml"),
            project_path=str(tmp_path),
        )
        assert res["status"] == "error"
        assert "reserved" in res["message"]
        # tasks.yaml untouched.
        assert (pm_path / "tasks.yaml").read_bytes() == tasks_before

    def test_missing_task_ids_warns(self, tmp_path):
        _seed(tmp_path, [_task(id="P-1", tags=["x"])])
        res = srv.pm_prompt_pack(task_ids=["P-1", "P-404"], project_path=str(tmp_path))
        assert res["task_count"] == 1
        assert res["task_ids"] == ["P-1"]
        assert any("P-404" in w for w in res["warnings"])

    def test_task_ids_preserve_requested_order(self, tmp_path):
        # tasks.yaml stores P-1 then P-2; requesting the reverse must honor it.
        _seed(tmp_path, [_task(id="P-1", tags=["x"]), _task(id="P-2", tags=["x"])])
        res = srv.pm_prompt_pack(task_ids=["P-2", "P-1"], project_path=str(tmp_path))
        assert res["task_ids"] == ["P-2", "P-1"]

    def test_read_path_only_does_not_mutate_ssot(self, tmp_path):
        # PMSERV-144 lineage: generating a pack must not mutate any of the three
        # SSoT stores the invariant names — tasks.yaml, decisions.yaml, AND
        # memory.db. memory.db is guarded by row count (not bytes): the tool
        # opens it RW under PM_LENS=0, so the file mtime/WAL may move even on a
        # pure SELECT — but the row count must not, so a stray save() is caught.
        tasks = [_task(id="P-1", description="see ADR-001", tags=["x"])]
        decisions = [Decision(id="ADR-001", title="方針")]
        memories = [
            Memory(
                session_id="s", type=MemoryType.LESSON, content="教訓", project="p", task_id="P-1"
            )
        ]
        pm_path = _seed(tmp_path, tasks, decisions=decisions, memories=memories)

        def _mem_row_count() -> int:
            conn = sqlite3.connect(str(pm_path / "memory.db"))
            try:
                return conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
            finally:
                conn.close()

        tasks_before = (pm_path / "tasks.yaml").read_bytes()
        decisions_before = (pm_path / "decisions.yaml").read_bytes()
        mem_rows_before = _mem_row_count()

        srv.pm_prompt_pack(filter_tag="x", project_path=str(tmp_path))

        assert (pm_path / "tasks.yaml").read_bytes() == tasks_before
        assert (pm_path / "decisions.yaml").read_bytes() == decisions_before
        assert _mem_row_count() == mem_rows_before

    def test_linked_memory_and_adr_rendered(self, tmp_path):
        tasks = [_task(id="P-1", description="ADR-001 に従う", tags=["x"])]
        decisions = [Decision(id="ADR-001", title="設計方針")]
        memories = [
            Memory(
                session_id="s",
                type=MemoryType.LESSON,
                content="過去の教訓A",
                project="p",
                task_id="P-1",
            )
        ]
        _seed(tmp_path, tasks, decisions=decisions, memories=memories)
        res = srv.pm_prompt_pack(filter_tag="x", project_path=str(tmp_path))
        md = Path(res["out_path"]).read_text(encoding="utf-8")
        assert "ADR-001 — 設計方針" in md
        assert "過去の教訓A" in md


# ─── Lens (PM_LENS=1) hiding — the tool writes, so it must not register ──────


def test_pm_prompt_pack_registered_in_claude_code_mode():
    """Sanity (this process is PM_LENS=0): the tool IS registered and is NOT
    in RO_ALLOWLIST (it writes, so it is a Claude-Code-only mutator surface)."""
    assert srv.PM_LENS_ENABLED is False
    assert "pm_prompt_pack" in srv.REGISTERED_TOOLS
    assert "pm_prompt_pack" not in srv.RO_ALLOWLIST


def test_pm_prompt_pack_hidden_under_lens(tmp_path):
    """Under PM_LENS=1 the tool must not register with MCP — it writes the
    export file, so a Lens read-only host may never invoke it (PMSERV-144)."""
    script = textwrap.dedent("""
        import pmlens.server as srv
        assert srv.PM_LENS_ENABLED is True, "PM_LENS not picked up in subprocess"
        # The registry is actually populated (RO tools DO register under Lens),
        # so the non-membership assertion below is meaningful, not vacuously
        # true against an empty set.
        assert "pm_status" in srv.REGISTERED_TOOLS, "RO tools should register under Lens"
        assert "pm_prompt_pack" not in srv.REGISTERED_TOOLS, (
            "pm_prompt_pack leaked into Lens registration"
        )
        assert callable(srv.pm_prompt_pack)  # bare fn still exists, just unregistered
        print("ok")
    """)
    env = {**os.environ, "HOME": str(tmp_path / "fake_home"), "PM_LENS": "1"}
    env.pop("VIRTUAL_ENV", None)
    env.pop("PM_DESKTOP_WRITE", None)
    (tmp_path / "fake_home").mkdir()

    proc = subprocess.run(
        [sys.executable, "-c", script],
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
        cwd=str(tmp_path),
    )
    assert proc.returncode == 0, f"stderr={proc.stderr!r}, stdout={proc.stdout!r}"
    assert proc.stdout.strip().splitlines()[-1] == "ok"


# ─── v2 (PMSERV-155 / ADR-041): data model, HTML, grouping, templates ────────


class TestV2DataModel:
    def test_task_new_fields_default_backward_compatible(self):
        t = _task()  # constructed WITHOUT the new fields
        assert t.suggested_model == SuggestedModel.ANY
        assert t.after_recommended == []
        assert t.track == ""

    def test_project_new_fields_default(self):
        p = Project(name="p")
        assert p.discipline == "" and p.verify_commands == []

    def test_task_fields_roundtrip_yaml(self, tmp_path):
        pm_path = init_pm_directory(tmp_path)
        _save_tasks(
            pm_path,
            [
                _task(
                    id="P-1",
                    suggested_model=SuggestedModel.OPUS,
                    after_recommended=["P-2"],
                    track="Sprint A",
                )
            ],
        )
        (t,) = load_tasks(pm_path)
        assert t.suggested_model == SuggestedModel.OPUS
        assert t.after_recommended == ["P-2"]
        assert t.track == "Sprint A"

    def test_project_fields_roundtrip_yaml(self, tmp_path):
        pm_path = init_pm_directory(tmp_path)
        proj = load_project(pm_path)
        proj.discipline = "常に typecheck を通す"
        proj.verify_commands = ["pnpm typecheck", "pnpm test"]
        _save_project(pm_path, proj)
        reloaded = load_project(pm_path)
        assert reloaded.discipline == "常に typecheck を通す"
        assert reloaded.verify_commands == ["pnpm typecheck", "pnpm test"]

    def test_legacy_project_yaml_without_new_fields_loads(self, tmp_path):
        # A project.yaml written before v2 (no discipline / verify_commands) must
        # still load with safe defaults (backward compat).
        pm_path = tmp_path / ".pm"
        pm_path.mkdir()
        (pm_path / "project.yaml").write_text(
            "name: legacy\ndisplay_name: Legacy\nversion: 0.1.0\nstatus: development\n",
            encoding="utf-8",
        )
        p = load_project(pm_path)
        assert p.discipline == "" and p.verify_commands == []


class TestGroupTasks:
    def test_none_single_lane(self):
        tasks = [_task(id="A"), _task(id="B")]
        lanes = group_tasks(tasks, "none")
        assert len(lanes) == 1 and lanes[0][0] == ""
        assert [t.id for t in lanes[0][1]] == ["A", "B"]

    def test_phase_lanes_order_preserved(self):
        tasks = [
            _task(id="A", phase="p1"),
            _task(id="B", phase="p2"),
            _task(id="C", phase="p1"),
        ]
        lanes = group_tasks(tasks, "phase")
        assert [lbl for lbl, _ in lanes] == ["p1", "p2"]  # first-appearance order
        assert [t.id for t in lanes[0][1]] == ["A", "C"]

    def test_track_lanes_with_missing_track(self):
        tasks = [_task(id="A", track="X"), _task(id="B")]  # B has no track
        lanes = dict((lbl, [t.id for t in ts]) for lbl, ts in group_tasks(tasks, "track"))
        assert lanes["X"] == ["A"]
        assert lanes["(no track)"] == ["B"]

    def test_unknown_group_by_degrades_to_single_lane(self):
        lanes = group_tasks([_task(id="A")], "bogus")
        assert len(lanes) == 1 and lanes[0][0] == ""


class TestTaskNode:
    def test_model_any_yields_no_chip(self):
        node = task_node(_task(id="A"))  # default suggested_model = any
        assert node["suggested_model"] is None

    def test_model_and_deps_surfaced(self):
        node = task_node(
            _task(
                id="A",
                priority=Priority.P0,
                suggested_model=SuggestedModel.OPUS,
                blocked_by=["B"],
                after_recommended=["C"],
            )
        )
        assert node["priority"] == "P0"
        assert node["suggested_model"] == "opus"
        assert node["blocked_by"] == ["B"] and node["after_recommended"] == ["C"]


class TestBuildHtml:
    def _html(self, tasks, **kw):
        proj = kw.pop("project", Project(name="p", display_name="P"))
        return build_prompt_pack_html(
            tasks,
            project=proj,
            memories_by_task=kw.pop("memories_by_task", {}),
            decisions_by_id=kw.pop("decisions_by_id", {}),
            verify_commands=kw.pop("verify_commands", []),
            filter_label=kw.pop("filter_label", "sel"),
            **kw,
        )

    def test_self_contained_no_external_refs(self):
        import re as _re

        html = self._html([_task(id="A", title="t")])
        assert not _re.findall(r'https?://[^\s"\')]+', html)
        assert "<script" in html and "</script>" in html  # inline JS only

    def test_autoescape_prevents_xss(self):
        task = _task(id="A", title="<script>alert(1)</script>", description="<b>x</b>")
        html = self._html([task])
        assert "<script>alert(1)</script>" not in html  # not live
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html  # escaped

    def test_autoescape_covers_every_task_derived_sink(self):
        # Each distinct {{ }} expression in the template that renders task-derived
        # text is its own escaping sink (ADR-041 contract). Feed a unique <img
        # onerror> payload into EACH and assert every one is escaped — so a
        # targeted `| safe` on any single field (not just a global autoescape
        # disable) is caught.
        def payload(marker: str) -> str:
            return f"<img src=x onerror=alert('{marker}')>"

        task = _task(
            id="A",
            title=payload("TITLE"),
            description=payload("DESC"),
            track=payload("TRACK"),
            blocked_by=[payload("BLK")],
            after_recommended=[payload("AFT")],
        )
        html = build_prompt_pack_html(
            [task],
            project=Project(name="p"),
            memories_by_task={},
            decisions_by_id={},
            verify_commands=[payload("VERIFY")],
            filter_label=payload("FILT"),
            group_by="track",  # so the track value renders as a lane label
            discipline=payload("DISC"),
        )
        for marker in ("TITLE", "DESC", "TRACK", "BLK", "AFT", "VERIFY", "FILT", "DISC"):
            live = f"<img src=x onerror=alert('{marker}')>"
            escaped = f"&lt;img src=x onerror=alert(&#39;{marker}&#39;)&gt;"
            assert live not in html, f"{marker} sink not escaped (XSS)"
            assert escaped in html, f"{marker} sink missing escaped form"

    def test_chips_and_dependency_notes(self):
        html = self._html(
            [
                _task(
                    id="A",
                    priority=Priority.P0,
                    suggested_model=SuggestedModel.OPUS,
                    blocked_by=["B"],
                    after_recommended=["C"],
                )
            ]
        )
        assert "chip prio-P0" in html
        assert "chip model-opus" in html
        assert "⛔" in html and "B" in html  # hard dep note
        assert "↝" in html and "C" in html  # soft dep note

    def test_copy_button_reads_textcontent(self):
        html = self._html([_task(id="A")])
        assert "copy-btn" in html
        assert "textContent" in html  # copies original text, not a JS string literal

    def test_discipline_and_verify_commands_rendered(self):
        html = self._html(
            [_task(id="A", acceptance_criteria=["works"])],
            discipline="規律テキスト",
            verify_commands=["pytest -q"],
        )
        assert "規律テキスト" in html
        assert "pytest -q" in html  # inside the card body via build_prompt_body

    def test_track_lanes_rendered(self):
        html = self._html(
            [_task(id="A", track="Sprint A"), _task(id="B", track="DX")],
            group_by="track",
        )
        assert "Sprint A" in html and "DX" in html

    def test_body_shared_with_md(self):
        # The paste-ready body must be byte-identical between md and html
        # (build_prompt_body is the single source — ADR-041).
        task = _task(id="A", description="do it", acceptance_criteria=["ok"])
        body = build_prompt_body(task, memories=[], decisions_by_id={}, verify_commands=["v"])
        html = self._html([task], verify_commands=["v"])
        # every non-empty body line appears in the escaped <pre>
        for line in body.splitlines():
            if line.strip():
                assert line.replace("<", "&lt;").replace(">", "&gt;") in html or line in html


class TestTemplateOverride:
    def test_project_override_wins(self, tmp_path):
        pm_path = init_pm_directory(tmp_path)
        override_dir = pm_path / "prompt-templates"
        override_dir.mkdir()
        (override_dir / "prompt_pack.html").write_text(
            "OVERRIDE-MARKER {{ task_count }} tasks", encoding="utf-8"
        )
        html = build_prompt_pack_html(
            [_task(id="A")],
            project=load_project(pm_path),
            memories_by_task={},
            decisions_by_id={},
            verify_commands=[],
            filter_label="x",
            pm_path=pm_path,
        )
        assert html == "OVERRIDE-MARKER 1 tasks"

    def test_builtin_used_when_no_override(self, tmp_path):
        pm_path = init_pm_directory(tmp_path)
        html = build_prompt_pack_html(
            [_task(id="A")],
            project=load_project(pm_path),
            memories_by_task={},
            decisions_by_id={},
            verify_commands=[],
            filter_label="x",
            pm_path=pm_path,
        )
        assert "<!doctype html>" in html.lower()  # bundled template


def test_html_read_path_only_does_not_mutate_ssot(tmp_path):
    # RO invariant for the HTML path (parallel to the md test): generating an
    # HTML pack must not mutate tasks.yaml / decisions.yaml / memory.db rows.
    tasks = [_task(id="P-1", description="see ADR-001", tags=["x"], track="A")]
    decisions = [Decision(id="ADR-001", title="方針")]
    memories = [
        Memory(session_id="s", type=MemoryType.LESSON, content="教訓", project="p", task_id="P-1")
    ]
    pm_path = _seed(tmp_path, tasks, decisions=decisions, memories=memories)

    def _mem_row_count() -> int:
        conn = sqlite3.connect(str(pm_path / "memory.db"))
        try:
            return conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
        finally:
            conn.close()

    tasks_before = (pm_path / "tasks.yaml").read_bytes()
    decisions_before = (pm_path / "decisions.yaml").read_bytes()
    rows_before = _mem_row_count()

    srv.pm_prompt_pack(format="html", group_by="track", filter_tag="x", project_path=str(tmp_path))

    assert (pm_path / "tasks.yaml").read_bytes() == tasks_before
    assert (pm_path / "decisions.yaml").read_bytes() == decisions_before
    assert _mem_row_count() == rows_before


def test_legacy_yaml_not_upgraded_with_new_fields(tmp_path):
    # ADR-041 negative-consequence guard: generating a pack over a PRE-v2
    # tasks.yaml / project.yaml (no suggested_model/track/discipline/... keys)
    # must not rewrite them — the read path never persists the new default
    # fields, so a legacy file stays byte-for-byte legacy (no surprise diff).
    pm_path = tmp_path / ".pm"
    (pm_path / "exports").mkdir(parents=True)
    (pm_path / "project.yaml").write_text(
        "name: legacy\ndisplay_name: Legacy\nversion: 0.1.0\nstatus: development\n",
        encoding="utf-8",
    )
    (pm_path / "tasks.yaml").write_text(
        "tasks:\n"
        "- id: L-1\n"
        "  title: legacy task\n"
        "  phase: phase-1\n"
        "  status: todo\n"
        "  priority: P1\n"
        "  tags: [x]\n",
        encoding="utf-8",
    )
    project_before = (pm_path / "project.yaml").read_bytes()
    tasks_before = (pm_path / "tasks.yaml").read_bytes()

    for fmt in ("md", "html"):
        res = srv.pm_prompt_pack(format=fmt, filter_tag="x", project_path=str(tmp_path))
        assert res["status"] == "ok" and res["task_count"] == 1

    # Neither SSoT file gained the new default fields.
    assert (pm_path / "project.yaml").read_bytes() == project_before
    assert (pm_path / "tasks.yaml").read_bytes() == tasks_before
    assert b"suggested_model" not in tasks_before  # sanity: file really is legacy


# ─── v3 (PMSERV-157): shared orchestration, CLI, workflow integration ────────


def test_run_prompt_pack_does_not_create_memory_db(tmp_path):
    # RO improvement: generating a pack for a project with no memory.db must not
    # create it (constructing a MemoryStore would write schema). run_prompt_pack
    # reads memories only when memory.db already exists.
    pm_path = init_pm_directory(tmp_path)
    _save_tasks(pm_path, [_task(id="P-1", tags=["x"])])
    assert not (pm_path / "memory.db").exists()
    res = run_prompt_pack(pm_path, filter_tag="x")
    assert res["status"] == "ok" and res["task_count"] == 1
    assert not (pm_path / "memory.db").exists()  # still absent — no side effect


def test_tool_delegates_to_run_prompt_pack_parity(tmp_path):
    # pm_prompt_pack is a thin wrapper over run_prompt_pack; the same selection
    # must yield the same result shape (minus the out_path, which is per-call).
    pm_path = _seed(tmp_path, [_task(id="P-1", tags=["x"]), _task(id="P-2", tags=["x"])])
    tool_res = srv.pm_prompt_pack(filter_tag="x", project_path=str(tmp_path))
    direct = run_prompt_pack(pm_path, filter_tag="x", out_path=str(tmp_path / "d.md"))
    assert tool_res["task_ids"] == direct["task_ids"]
    assert tool_res["task_count"] == direct["task_count"] == 2
    assert tool_res["format"] == direct["format"] == "md"


class TestPromptPackCLI:
    def _runner(self):
        from click.testing import CliRunner

        return CliRunner()

    def _cli(self):
        from pmlens.__main__ import cli

        return cli

    def test_md_generation(self, tmp_path):
        pm_path = init_pm_directory(tmp_path)
        _save_tasks(pm_path, [_task(id="T-1", tags=["b"]), _task(id="T-2", tags=["b"])])
        res = self._runner().invoke(
            self._cli(), ["prompt-pack", "--tag", "b", "--project", str(tmp_path)]
        )
        assert res.exit_code == 0, res.output
        assert "2 task(s)" in res.output and ".md" in res.output

    def test_html_group_by_track(self, tmp_path):
        pm_path = init_pm_directory(tmp_path)
        _save_tasks(
            pm_path,
            [
                _task(id="T-1", tags=["b"], track="A", suggested_model=SuggestedModel.OPUS),
                _task(id="T-2", tags=["b"], track="B"),
            ],
        )
        res = self._runner().invoke(
            self._cli(),
            [
                "prompt-pack",
                "--tag",
                "b",
                "--format",
                "html",
                "--group-by",
                "track",
                "--project",
                str(tmp_path),
            ],
        )
        assert res.exit_code == 0, res.output
        out = Path(res.output.strip().split("→")[1].split("(")[0].strip())
        html = out.read_text(encoding="utf-8")
        assert out.suffix == ".html"
        assert "chip model-opus" in html and ">A<" in html and ">B<" in html

    def test_task_id_repeatable_override(self, tmp_path):
        pm_path = init_pm_directory(tmp_path)
        _save_tasks(pm_path, [_task(id="T-1", tags=["b"]), _task(id="T-2", tags=["b"])])
        res = self._runner().invoke(
            self._cli(),
            ["prompt-pack", "--task-id", "T-2", "--task-id", "T-1", "--project", str(tmp_path)],
        )
        assert res.exit_code == 0, res.output
        assert "2 task(s)" in res.output

    def test_unregistered_project_exits_1(self, tmp_path):
        res = self._runner().invoke(
            self._cli(), ["prompt-pack", "--project", str(tmp_path / "nope")]
        )
        assert res.exit_code == 1

    def test_no_match_exits_0_with_message(self, tmp_path):
        pm_path = init_pm_directory(tmp_path)
        _save_tasks(pm_path, [_task(id="T-1", tags=["b"])])
        res = self._runner().invoke(
            self._cli(), ["prompt-pack", "--tag", "none", "--project", str(tmp_path)]
        )
        assert res.exit_code == 0
        assert "No tasks matched" in res.output

    def test_reserved_out_path_exits_1(self, tmp_path):
        pm_path = init_pm_directory(tmp_path)
        _save_tasks(pm_path, [_task(id="T-1", tags=["b"])])
        res = self._runner().invoke(
            self._cli(),
            [
                "prompt-pack",
                "--tag",
                "b",
                "--out",
                str(pm_path / "tasks.yaml"),
                "--project",
                str(tmp_path),
            ],
        )
        assert res.exit_code == 1
        assert "reserved" in res.output.lower()

    def test_invalid_format_rejected_by_choice(self, tmp_path):
        init_pm_directory(tmp_path)
        res = self._runner().invoke(
            self._cli(), ["prompt-pack", "--format", "pdf", "--project", str(tmp_path)]
        )
        assert res.exit_code == 2  # click.Choice rejects before run_prompt_pack

    def test_invalid_group_by_rejected_by_choice(self, tmp_path):
        init_pm_directory(tmp_path)
        res = self._runner().invoke(
            self._cli(), ["prompt-pack", "--group-by", "sprint", "--project", str(tmp_path)]
        )
        assert res.exit_code == 2

    def test_phase_and_priority_options(self, tmp_path):
        pm_path = init_pm_directory(tmp_path)
        _save_tasks(
            pm_path,
            [
                _task(id="A", phase="p1", priority=Priority.P0),
                _task(id="B", phase="p2", priority=Priority.P1),
            ],
        )
        res = self._runner().invoke(
            self._cli(),
            ["prompt-pack", "--phase", "p1", "--priority", "P0", "--project", str(tmp_path)],
        )
        assert res.exit_code == 0, res.output
        assert "1 task(s)" in res.output  # only task A matches both

    def test_missing_task_id_warning_surfaces(self, tmp_path):
        pm_path = init_pm_directory(tmp_path)
        _save_tasks(pm_path, [_task(id="T-1")])
        res = self._runner().invoke(
            self._cli(),
            ["prompt-pack", "--task-id", "T-1", "--task-id", "T-404", "--project", str(tmp_path)],
        )
        assert res.exit_code == 0, res.output
        assert "T-404" in res.output  # the "not found" warning is surfaced


def test_development_workflow_has_prompt_pack_hint_without_changing_shape():
    # Workflow integration (PMSERV-157): the Task Breakdown step gains a
    # prompt-pack skill_hint, but the step count (9) and chain target must be
    # unchanged so existing workflow behavior/tests are preserved.
    from pmlens.workflow import load_workflow_template

    tmpl = load_workflow_template("development")
    assert len(tmpl.steps) == 9  # unchanged
    tasks_step = next(s for s in tmpl.steps if s.id == "tasks")
    assert tasks_step.tool_hint == "pm_add_task"  # existing hint preserved
    assert tasks_step.skill_hint and "prompt_pack" in tasks_step.skill_hint.lower()


def test_run_prompt_pack_reads_memory_db_read_only(tmp_path):
    # Adversarial-review fix: linked memories are read through a mode=ro&immutable=1
    # connection, so a generate must not touch memory.db or its sidecars at all
    # (the read-WRITE open would run PRAGMA journal_mode=WAL + schema migrations).
    # Snapshot every memory.db* file (whatever the RW seed store left) and assert
    # run_prompt_pack changes none of them.
    import hashlib

    pm_path = init_pm_directory(tmp_path)
    _save_tasks(pm_path, [_task(id="P-1", tags=["x"])])
    store = MemoryStore(pm_path / "memory.db", global_db_path=None)
    store.save(
        Memory(
            session_id="s",
            type=MemoryType.LESSON,
            content="CAUTION-MARKER-XYZ",
            project="p",
            task_id="P-1",
        )
    )
    store.close()

    def _snap() -> dict[str, str | None]:
        out: dict[str, str | None] = {}
        for name in ("memory.db", "memory.db-wal", "memory.db-shm"):
            p = pm_path / name
            out[name] = hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else None
        return out

    before = _snap()
    res = run_prompt_pack(pm_path, filter_tag="x")

    assert res["status"] == "ok"
    assert _snap() == before  # run_prompt_pack wrote nothing to memory.db*
    # the memory WAS read (rendered into the caution section) — not a silent skip
    assert "CAUTION-MARKER-XYZ" in Path(res["out_path"]).read_text(encoding="utf-8")


def test_tool_validates_before_resolving_project(tmp_path):
    # Adversarial-review fix (parity): a bad format on an UNRESOLVABLE project
    # returns the format-error dict (validate-then-resolve), not a raised
    # ProjectNotFoundError.
    res = srv.pm_prompt_pack(format="xml", project_path=str(tmp_path / "no-such-project"))
    assert res["status"] == "error"
    assert "xml" in res["message"] and "format" in res["message"]


def test_run_prompt_pack_filter_phase_and_priority(tmp_path):
    # Adversarial-review gap: filter_phase / filter_priority had no coverage.
    pm_path = init_pm_directory(tmp_path)
    _save_tasks(
        pm_path,
        [
            _task(id="A", phase="p1", priority=Priority.P0),
            _task(id="B", phase="p2", priority=Priority.P0),
            _task(id="C", phase="p1", priority=Priority.P2),
        ],
    )
    assert set(run_prompt_pack(pm_path, filter_phase="p1")["task_ids"]) == {"A", "C"}
    assert set(run_prompt_pack(pm_path, filter_priority="P0")["task_ids"]) == {"A", "B"}
    assert run_prompt_pack(pm_path, filter_phase="p1", filter_priority="P0")["task_ids"] == ["A"]
