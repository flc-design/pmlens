"""Tests for recall.py — ContextBuilder and Progressive Disclosure."""

from __future__ import annotations

from pathlib import Path

import pytest

from pm_server.memory import MemoryStore
from pm_server.models import (
    Memory,
    MemoryType,
    Phase,
    PhaseStatus,
    Priority,
    Project,
    ProjectStatus,
    SessionSummary,
    Task,
    TaskStatus,
)
from pm_server.recall import ContextBuilder, _estimate_tokens, _truncate_to_tokens
from pm_server.storage import save_project, save_tasks

# ─── Token estimation helpers ──────────────────────────


class TestTokenEstimation:
    def test_empty_string(self):
        assert _estimate_tokens("") == 0

    def test_short_english(self):
        tokens = _estimate_tokens("hello world")
        assert tokens >= 1

    def test_japanese_text(self):
        tokens = _estimate_tokens("ユーザー認証APIの実装")
        assert tokens >= 5  # ~10 chars / 2

    def test_truncate_within_budget(self):
        text = "short text"
        assert _truncate_to_tokens(text, 100) == text

    def test_truncate_exceeds_budget(self):
        text = "a" * 1000
        result = _truncate_to_tokens(text, 10)
        assert result.endswith("...")
        assert len(result) < len(text)


# ─── ContextBuilder ────────────────────────────────────


@pytest.fixture
def context_project(tmp_path: Path) -> Path:
    """Create a project with tasks for ContextBuilder tests."""
    pm_path = tmp_path / ".pm"
    pm_path.mkdir(exist_ok=True)
    (pm_path / "daily").mkdir(exist_ok=True)

    project = Project(
        name="ctxproj",
        display_name="Context Test",
        status=ProjectStatus.DEVELOPMENT,
        phases=[Phase(id="phase-1", name="Core", status=PhaseStatus.ACTIVE)],
    )
    save_project(pm_path, project)

    tasks = [
        Task(
            id="CTX-001",
            title="Implement auth",
            phase="phase-1",
            status=TaskStatus.IN_PROGRESS,
            priority=Priority.P0,
        ),
        Task(
            id="CTX-002",
            title="Write tests",
            phase="phase-1",
            status=TaskStatus.TODO,
            priority=Priority.P1,
        ),
    ]
    save_tasks(pm_path, tasks)
    return tmp_path


@pytest.fixture
def ctx_store(tmp_path: Path) -> MemoryStore:
    """MemoryStore for context tests."""
    store = MemoryStore(tmp_path / ".pm" / "memory.db", global_db_path=None)
    yield store
    store.close()


@pytest.fixture
def builder(ctx_store: MemoryStore, context_project: Path) -> ContextBuilder:
    """ContextBuilder wired to the test project."""
    return ContextBuilder(ctx_store, context_project / ".pm")


class TestContextBuilderEmpty:
    """Tests with no data — should gracefully return empty."""

    def test_empty_context(self, builder: ContextBuilder):
        result = builder.build_session_context()
        assert result == ""

    def test_empty_with_custom_budget(self, builder: ContextBuilder):
        result = builder.build_session_context(max_tokens=100)
        assert result == ""


class TestContextBuilderLayer1:
    """Layer 1: Last session summary."""

    def test_summary_included(self, builder: ContextBuilder, ctx_store: MemoryStore):
        summary = SessionSummary(
            session_id="sess-prev",
            summary="Implemented JWT auth flow",
            goals="Auth module complete",
            pending=["Code review"],
            project="ctxproj",
        )
        ctx_store.save_session_summary(summary)

        result = builder.build_session_context()
        assert "前回セッションからの引き継ぎ" in result
        assert "Implemented JWT auth flow" in result
        assert "Auth module complete" in result
        assert "Code review" in result

    def test_summary_only_when_exists(self, builder: ContextBuilder):
        result = builder.build_session_context()
        assert "前回のセッション" not in result


class TestContextBuilderLayer2:
    """Layer 2: In-progress task memories."""

    def test_task_memories_included(self, builder: ContextBuilder, ctx_store: MemoryStore):
        mem = Memory(
            session_id="sess-prev",
            content="JWT needs RS256 algorithm",
            task_id="CTX-001",
            project="ctxproj",
        )
        ctx_store.save(mem)

        result = builder.build_session_context()
        assert "進行中タスク" in result
        assert "CTX-001" in result
        assert "JWT needs RS256 algorithm" in result

    def test_only_in_progress_tasks(self, builder: ContextBuilder, ctx_store: MemoryStore):
        """Memories for TODO tasks should not appear in Layer 2."""
        mem = Memory(
            session_id="sess-prev",
            content="Note for TODO task",
            task_id="CTX-002",  # TODO status
            project="ctxproj",
        )
        ctx_store.save(mem)

        result = builder.build_session_context()
        # CTX-002 is TODO, so its memories shouldn't appear in Layer 2
        assert "CTX-002" not in result or "進行中タスク" not in result


class TestContextBuilderLayer3:
    """Layer 3: Recent decisions."""

    def test_decision_memories_included(self, builder: ContextBuilder, ctx_store: MemoryStore):
        mem = Memory(
            session_id="sess-prev",
            content="Chose JWT over session cookies",
            decision_id="ADR-001",
            project="ctxproj",
        )
        ctx_store.save(mem)

        result = builder.build_session_context()
        assert "ADR-001" in result
        assert "Chose JWT over session cookies" in result

    def test_no_decisions(self, builder: ContextBuilder):
        result = builder.build_session_context()
        assert "最近の判断" not in result


class TestContextBuilderLayer4:
    """Layer 4: Recent memories."""

    def test_recent_memories_included(self, builder: ContextBuilder, ctx_store: MemoryStore):
        for i in range(3):
            mem = Memory(
                session_id="sess-prev",
                type=MemoryType.OBSERVATION,
                content=f"General observation {i}",
                project="ctxproj",
            )
            ctx_store.save(mem)

        result = builder.build_session_context()
        assert "最近の記憶" in result
        assert "General observation" in result


class TestContextBuilderBudget:
    """Token budget constraints."""

    def test_respects_max_tokens(self, builder: ContextBuilder, ctx_store: MemoryStore):
        # Fill with lots of data
        summary = SessionSummary(
            session_id="sess-big",
            summary="A" * 500,
            goals="B" * 500,
            project="ctxproj",
        )
        ctx_store.save_session_summary(summary)

        for i in range(20):
            mem = Memory(
                session_id="sess-big",
                content=f"Memory content block {i} " + "x" * 100,
                project="ctxproj",
            )
            ctx_store.save(mem)

        result = builder.build_session_context(max_tokens=200)
        tokens = _estimate_tokens(result)
        # Allow some overhead for headers, but should be roughly within budget
        assert tokens < 400  # generous upper bound (budget + headers)

    def test_empty_layers_donate_budget(self, builder: ContextBuilder, ctx_store: MemoryStore):
        """When early layers are empty, later layers should get more space."""
        # Only add recent memories (no summary, no task memories, no decisions)
        for i in range(10):
            mem = Memory(
                session_id="sess-prev",
                content=f"Recent note {i}: " + "data " * 20,
                project="ctxproj",
            )
            ctx_store.save(mem)

        result = builder.build_session_context(max_tokens=2000)
        assert "最近の記憶" in result
        # Should have multiple entries since layers 1-3 donated budget
        lines = [line for line in result.split("\n") if line.startswith("- ")]
        assert len(lines) >= 3


# ─── CLAUDE.md v2 template ─────────────────────────────


class TestClaudeMdV3:
    """Verify template evolution: Memory Layer + checkpoint + issue + warnings[] relay."""

    def test_template_version(self):
        from pm_server.claudemd import TEMPLATE_VERSION

        assert TEMPLATE_VERSION == 8

    def test_template_has_pm_recall(self):
        from pm_server.claudemd import CLAUDEMD_TEMPLATE

        assert "pm_recall" in CLAUDEMD_TEMPLATE

    def test_template_covers_severity_and_warnings(self):
        """v7 must guide Claude on severity selection and warnings[] relay."""
        from pm_server.claudemd import CLAUDEMD_TEMPLATE

        assert "severity=" in CLAUDEMD_TEMPLATE or 'severity="' in CLAUDEMD_TEMPLATE
        assert "warnings[]" in CLAUDEMD_TEMPLATE
        assert "remediation" in CLAUDEMD_TEMPLATE

    def test_template_has_pm_remember(self):
        from pm_server.claudemd import CLAUDEMD_TEMPLATE

        assert "pm_remember" in CLAUDEMD_TEMPLATE

    def test_template_has_checkpoint_section(self):
        from pm_server.claudemd import CLAUDEMD_TEMPLATE

        assert "コンテキスト保全" in CLAUDEMD_TEMPLATE
        assert "Compaction" in CLAUDEMD_TEMPLATE

    def test_template_has_pm_session_summary(self):
        from pm_server.claudemd import CLAUDEMD_TEMPLATE

        assert "pm_session_summary" in CLAUDEMD_TEMPLATE

    def test_template_has_memory_section(self):
        from pm_server.claudemd import CLAUDEMD_TEMPLATE

        assert "作業中に重要な発見・判断があった時" in CLAUDEMD_TEMPLATE

    def test_template_has_pm_add_issue(self):
        from pm_server.claudemd import CLAUDEMD_TEMPLATE

        assert "pm_add_issue" in CLAUDEMD_TEMPLATE

    def test_template_has_issue_workflow_section(self):
        from pm_server.claudemd import CLAUDEMD_TEMPLATE

        assert "タスク完了確認中にイシュー" in CLAUDEMD_TEMPLATE

    def test_template_has_other_rule_sections_instruction(self):
        from pm_server.claudemd import CLAUDEMD_TEMPLATE

        assert "other_rule_sections" in CLAUDEMD_TEMPLATE

    def test_template_has_memory_routing_section(self):
        # PMSERV-111 / ADR-023 (v8): pm_remember=SSoT vs Claude Code auto
        # memory role split, with an explicit no-dual-write rule.
        from pm_server.claudemd import CLAUDEMD_TEMPLATE

        assert "記憶の二重化を避ける" in CLAUDEMD_TEMPLATE
        assert "auto memory" in CLAUDEMD_TEMPLATE
        assert "二重書き込み" in CLAUDEMD_TEMPLATE
        assert "SSoT" in CLAUDEMD_TEMPLATE


# ─── PMSERV-049: ContextBuilder session-self-id marker ─────────────


class TestContextBuilderSessionMarker:
    """Layer 1 marks the summary as foreign when current_session_id differs."""

    def test_summary_marker_when_same_session(self, ctx_store: MemoryStore, context_project: Path):
        ctx_store.save_session_summary(
            SessionSummary(
                session_id="sess-self",
                summary="my own work",
                project="ctxproj",
            )
        )
        builder = ContextBuilder(
            ctx_store,
            context_project / ".pm",
            current_session_id="sess-self",
        )
        result = builder.build_session_context()
        assert "別セッション" not in result

    def test_summary_marker_when_different_session(
        self, ctx_store: MemoryStore, context_project: Path
    ):
        ctx_store.save_session_summary(
            SessionSummary(
                session_id="sess-other",
                summary="other session work",
                project="ctxproj",
            )
        )
        builder = ContextBuilder(
            ctx_store,
            context_project / ".pm",
            current_session_id="sess-self",
        )
        result = builder.build_session_context()
        assert "別セッション" in result

    def test_summary_marker_omitted_without_current_session_id(
        self, ctx_store: MemoryStore, context_project: Path
    ):
        # Backward compat: caller that doesn't pass current_session_id
        # gets the same output as v0.4.x.
        ctx_store.save_session_summary(
            SessionSummary(
                session_id="sess-anything",
                summary="some work",
                project="ctxproj",
            )
        )
        builder = ContextBuilder(ctx_store, context_project / ".pm")
        result = builder.build_session_context()
        assert "別セッション" not in result
