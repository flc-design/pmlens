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
    Task,
    TaskStatus,
)
from pmlens.prompt_pack import (
    _condense,
    _fence,
    adr_refs_for_task,
    build_prompt_pack_md,
    read_verify_commands,
    render_task_card,
)
from pmlens.storage import _save_decisions, _save_tasks, init_pm_directory


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
        out = Path(res["out_path"])
        assert out.exists()
        assert out.parent == tmp_path / ".pm" / "exports"
        md = out.read_text(encoding="utf-8")
        assert "### P-1" in md and "### P-2" in md
        assert "P-3" not in md  # done excluded

    def test_task_ids_override_includes_done(self, tmp_path):
        tasks = [_task(id="P-9", title="done", tags=["x"], status=TaskStatus.DONE)]
        _seed(tmp_path, tasks)
        res = srv.pm_prompt_pack(task_ids=["P-9"], project_path=str(tmp_path))
        assert res["task_count"] == 1
        assert res["task_ids"] == ["P-9"]

    def test_format_html_is_v2_error(self, tmp_path):
        _seed(tmp_path, [_task()])
        res = srv.pm_prompt_pack(format="html", project_path=str(tmp_path))
        assert res["status"] == "error"
        assert "v2" in res["message"]

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
