"""Comprehensive tests for workflow engine.

Covers:
- Workflow models and enums
- Storage CRUD (load, save, add, update, next_number)
- Template loading (builtin, custom, resolution order)
- Template listing
- Workflow engine (start, status, advance, loop, skip, gate, chain)
- MCP tool wrappers
- Edge cases and error handling
"""

from __future__ import annotations

import datetime as _dt

import pytest
import yaml

from pm_server.models import (
    PmServerError,
    Workflow,
    WorkflowNotFoundError,
    WorkflowStatus,
    WorkflowStep,
    WorkflowStepStatus,
    WorkflowTemplate,
)
from pm_server.storage import (
    add_workflow,
    list_workflow_templates,
    load_workflow_template,
    load_workflows,
    next_workflow_number,
    save_workflows,
    update_workflow,
)
from pm_server.workflow import (
    advance_step,
    get_active_workflow,
    get_workflow,
    start_workflow,
    workflow_status,
)

# ─── Model Tests ────────────────────────────────────


class TestWorkflowModels:
    """Test Pydantic models for workflow objects."""

    def test_workflow_step_defaults(self):
        step = WorkflowStep(id="s1", name="Step 1")
        assert step.status == WorkflowStepStatus.PENDING
        assert step.loop is False
        assert step.loop_group is None
        assert step.gate is None
        assert step.optional is False
        assert step.tool_hint is None
        assert step.artifacts == []
        assert step.iteration == 0
        assert step.notes == ""

    def test_workflow_step_all_fields(self):
        step = WorkflowStep(
            id="research",
            name="Research",
            description="Do research",
            status=WorkflowStepStatus.ACTIVE,
            loop=True,
            loop_group="brainstorm",
            gate="user_approval",
            optional=True,
            tool_hint="pm_remember",
            skill_hint="research-skill",
            agent_hint="Use a research agent",
            required_artifacts=["ADR"],
            produces=["findings"],
            consumes=["requirements"],
            artifacts=["ADR-001"],
            iteration=2,
            notes="Some notes",
        )
        assert step.loop_group == "brainstorm"
        assert step.gate == "user_approval"
        assert step.produces == ["findings"]
        assert step.iteration == 2

    def test_workflow_template_defaults(self):
        tmpl = WorkflowTemplate(name="test")
        assert tmpl.description == ""
        assert tmpl.chain_to is None
        assert tmpl.steps == []

    def test_workflow_defaults(self):
        wf = Workflow(id="WF-001", name="Test", feature="test", template="test")
        assert wf.status == WorkflowStatus.ACTIVE
        assert wf.current_step_index == 0
        assert wf.steps == []
        assert wf.chain_to is None
        assert wf.created == _dt.date.today()

    def test_workflow_step_status_enum(self):
        assert WorkflowStepStatus.PENDING.value == "pending"
        assert WorkflowStepStatus.ACTIVE.value == "active"
        assert WorkflowStepStatus.DONE.value == "done"
        assert WorkflowStepStatus.SKIPPED.value == "skipped"

    def test_workflow_status_enum(self):
        assert WorkflowStatus.ACTIVE.value == "active"
        assert WorkflowStatus.COMPLETED.value == "completed"
        assert WorkflowStatus.PAUSED.value == "paused"
        assert WorkflowStatus.ABANDONED.value == "abandoned"


# ─── Storage Tests ──────────────────────────────────


class TestWorkflowStorage:
    """Test YAML CRUD for workflows."""

    def test_load_empty(self, tmp_pm_path):
        workflows = load_workflows(tmp_pm_path)
        assert workflows == []

    def test_save_and_load(self, tmp_pm_path):
        wf = Workflow(
            id="WF-001",
            name="Dev",
            feature="auth",
            template="development",
            steps=[WorkflowStep(id="s1", name="Step 1")],
        )
        save_workflows(tmp_pm_path, [wf])

        loaded = load_workflows(tmp_pm_path)
        assert len(loaded) == 1
        assert loaded[0].id == "WF-001"
        assert loaded[0].feature == "auth"
        assert len(loaded[0].steps) == 1

    def test_add_workflow(self, tmp_pm_path):
        wf = Workflow(id="WF-001", name="Dev", feature="auth", template="dev")
        add_workflow(tmp_pm_path, wf)

        loaded = load_workflows(tmp_pm_path)
        assert len(loaded) == 1
        assert loaded[0].id == "WF-001"

    def test_add_multiple(self, tmp_pm_path):
        add_workflow(
            tmp_pm_path,
            Workflow(id="WF-001", name="A", feature="a", template="dev"),
        )
        add_workflow(
            tmp_pm_path,
            Workflow(id="WF-002", name="B", feature="b", template="dev"),
        )

        loaded = load_workflows(tmp_pm_path)
        assert len(loaded) == 2
        assert loaded[0].id == "WF-001"
        assert loaded[1].id == "WF-002"

    def test_update_workflow(self, tmp_pm_path):
        add_workflow(
            tmp_pm_path,
            Workflow(
                id="WF-001",
                name="Dev",
                feature="auth",
                template="dev",
                status=WorkflowStatus.ACTIVE,
            ),
        )
        updated = update_workflow(tmp_pm_path, "WF-001", status=WorkflowStatus.COMPLETED)
        assert updated.status == WorkflowStatus.COMPLETED
        assert updated.updated == _dt.date.today()

    def test_update_nonexistent(self, tmp_pm_path):
        with pytest.raises(WorkflowNotFoundError):
            update_workflow(tmp_pm_path, "WF-999")

    def test_next_workflow_number_empty(self, tmp_pm_path):
        assert next_workflow_number(tmp_pm_path) == 1

    def test_next_workflow_number_sequential(self, tmp_pm_path):
        add_workflow(
            tmp_pm_path,
            Workflow(id="WF-001", name="A", feature="a", template="dev"),
        )
        add_workflow(
            tmp_pm_path,
            Workflow(id="WF-003", name="B", feature="b", template="dev"),
        )
        assert next_workflow_number(tmp_pm_path) == 4

    def test_yaml_roundtrip_preserves_fields(self, tmp_pm_path):
        """Verify all workflow fields survive YAML serialization."""
        step = WorkflowStep(
            id="s1",
            name="Research",
            loop=True,
            loop_group="brainstorm",
            gate="user_approval",
            artifacts=["ADR-001"],
            iteration=3,
            notes="test notes",
        )
        wf = Workflow(
            id="WF-001",
            name="Dev",
            feature="auth",
            template="development",
            steps=[step],
            chain_to="next-wf",
        )
        save_workflows(tmp_pm_path, [wf])
        loaded = load_workflows(tmp_pm_path)[0]

        assert loaded.chain_to == "next-wf"
        assert loaded.steps[0].loop_group == "brainstorm"
        assert loaded.steps[0].gate == "user_approval"
        assert loaded.steps[0].artifacts == ["ADR-001"]
        assert loaded.steps[0].iteration == 3


# ─── Template Tests ─────────────────────────────────


class TestWorkflowTemplates:
    """Test template loading and resolution."""

    def test_load_builtin_development(self):
        tmpl = load_workflow_template("development")
        assert tmpl.name == "Development"
        assert len(tmpl.steps) == 9
        assert tmpl.steps[0].id == "decision"
        assert tmpl.steps[-1].id == "issues"
        assert tmpl.steps[-1].optional is True

    def test_load_builtin_discovery(self):
        tmpl = load_workflow_template("discovery")
        assert tmpl.name == "Discovery"
        assert len(tmpl.steps) == 5
        assert tmpl.chain_to == "development"
        # Check brainstorm loop group
        loop_steps = [s for s in tmpl.steps if s.loop_group == "brainstorm"]
        assert len(loop_steps) == 3

    def test_load_builtin_super_research(self):
        tmpl = load_workflow_template("super-research")
        assert tmpl.name == "Super Research"
        assert len(tmpl.steps) == 6
        assert tmpl.steps[0].id == "scope"
        assert tmpl.steps[-1].id == "synthesis"
        assert tmpl.steps[-1].gate == "user_approval"
        # Check research loop group
        loop_steps = [s for s in tmpl.steps if s.loop_group == "research"]
        assert len(loop_steps) == 2
        # Skill hint on parallel_research
        research_step = next(s for s in tmpl.steps if s.id == "parallel_research")
        assert research_step.skill_hint is not None
        assert "super-research" in research_step.skill_hint

    def test_load_nonexistent_template(self):
        with pytest.raises(PmServerError, match="not found"):
            load_workflow_template("nonexistent")

    def test_custom_overrides_builtin(self, tmp_pm_path):
        """Custom template with the same name should override built-in."""
        custom_dir = tmp_pm_path / "workflow_templates"
        custom_dir.mkdir()
        custom_data = {
            "name": "Custom Development",
            "description": "My custom workflow",
            "steps": [
                {"id": "only-step", "name": "The Only Step"},
            ],
        }
        (custom_dir / "development.yaml").write_text(yaml.safe_dump(custom_data), encoding="utf-8")

        tmpl = load_workflow_template("development", tmp_pm_path)
        assert tmpl.name == "Custom Development"
        assert len(tmpl.steps) == 1

    def test_list_templates_builtin(self):
        templates = list_workflow_templates()
        names = [t["name"] for t in templates]
        assert "development" in names
        assert "discovery" in names
        assert "super-research" in names
        for t in templates:
            assert t["source"] == "builtin"
            assert t["steps"] > 0

    def test_list_templates_custom_overrides(self, tmp_pm_path):
        custom_dir = tmp_pm_path / "workflow_templates"
        custom_dir.mkdir()
        custom_data = {
            "name": "Custom Dev",
            "description": "Override",
            "steps": [{"id": "s1", "name": "S1"}],
        }
        (custom_dir / "development.yaml").write_text(yaml.safe_dump(custom_data), encoding="utf-8")

        templates = list_workflow_templates(tmp_pm_path)
        dev_templates = [t for t in templates if t["name"] == "development"]
        assert len(dev_templates) == 1
        assert dev_templates[0]["source"] == "custom"

    def test_discovery_gates(self):
        """Discovery template should have user_approval gates."""
        tmpl = load_workflow_template("discovery")
        gated = [s for s in tmpl.steps if s.gate == "user_approval"]
        assert len(gated) >= 2  # proposal and confirm

    def test_development_produces_consumes(self):
        """Development template should have produces/consumes chains."""
        tmpl = load_workflow_template("development")
        decision_step = next(s for s in tmpl.steps if s.id == "decision")
        tasks_step = next(s for s in tmpl.steps if s.id == "tasks")
        assert "ADR" in decision_step.produces
        assert "ADR" in tasks_step.consumes


# ─── Engine Tests ───────────────────────────────────


class TestWorkflowEngine:
    """Test workflow lifecycle: start, advance, loop, skip, complete."""

    def test_start_workflow(self, tmp_pm_path):
        result = start_workflow(tmp_pm_path, "user auth", "development")
        assert result["status"] == "started"
        assert result["workflow_id"] == "WF-001"
        assert result["feature"] == "user auth"
        assert result["template"] == "development"
        assert result["total_steps"] == 9
        assert result["current_step"]["id"] == "decision"
        assert result["current_step"]["status"] == "active"

    def test_start_workflow_with_chain(self, tmp_pm_path):
        result = start_workflow(tmp_pm_path, "research", "discovery")
        assert result["chain_to"] == "development"

    def test_start_multiple_warns(self, tmp_pm_path):
        start_workflow(tmp_pm_path, "first", "development")
        result = start_workflow(tmp_pm_path, "second", "development")
        assert "warning" in result
        assert "WF-001" in result["warning"]

    def test_get_active_workflow(self, tmp_pm_path):
        assert get_active_workflow(tmp_pm_path) is None
        start_workflow(tmp_pm_path, "test", "development")
        wf = get_active_workflow(tmp_pm_path)
        assert wf is not None
        assert wf.id == "WF-001"

    def test_get_workflow_by_id(self, tmp_pm_path):
        start_workflow(tmp_pm_path, "test", "development")
        wf = get_workflow(tmp_pm_path, "WF-001")
        assert wf.feature == "test"

    def test_get_workflow_not_found(self, tmp_pm_path):
        with pytest.raises(WorkflowNotFoundError):
            get_workflow(tmp_pm_path, "WF-999")

    def test_workflow_status_active(self, tmp_pm_path):
        start_workflow(tmp_pm_path, "auth", "development")
        status = workflow_status(tmp_pm_path)
        assert status["workflow_id"] == "WF-001"
        assert status["status"] == "active"
        assert status["progress"] == "0/9"
        assert status["current_step"]["id"] == "decision"

    def test_workflow_status_no_active(self, tmp_pm_path):
        status = workflow_status(tmp_pm_path)
        assert status["status"] == "no_active_workflow"

    def test_workflow_status_by_id(self, tmp_pm_path):
        start_workflow(tmp_pm_path, "auth", "development")
        status = workflow_status(tmp_pm_path, "WF-001")
        assert status["workflow_id"] == "WF-001"

    def test_advance_step(self, tmp_pm_path):
        start_workflow(tmp_pm_path, "auth", "development")
        result = advance_step(tmp_pm_path)
        assert result["status"] == "advanced"
        assert result["completed_step"]["id"] == "decision"
        assert result["current_step"]["id"] == "tasks"
        assert result["progress"] == "1/9"

    def test_advance_with_artifacts(self, tmp_pm_path):
        start_workflow(tmp_pm_path, "auth", "development")
        result = advance_step(tmp_pm_path, artifacts=["ADR-005"])
        assert result["status"] == "advanced"
        # Verify artifact was stored
        wf = get_workflow(tmp_pm_path, "WF-001")
        assert "ADR-005" in wf.steps[0].artifacts

    def test_advance_with_notes(self, tmp_pm_path):
        start_workflow(tmp_pm_path, "auth", "development")
        advance_step(tmp_pm_path, notes="Decision recorded")
        wf = get_workflow(tmp_pm_path, "WF-001")
        assert "Decision recorded" in wf.steps[0].notes

    def test_advance_warns_missing_artifacts(self, tmp_pm_path):
        start_workflow(tmp_pm_path, "auth", "development")
        result = advance_step(tmp_pm_path)
        # decision step requires ADR artifact but none provided
        assert "warning" in result
        assert "required_artifacts" in result["warning"]

    def test_advance_no_warning_with_artifacts(self, tmp_pm_path):
        start_workflow(tmp_pm_path, "auth", "development")
        result = advance_step(tmp_pm_path, artifacts=["ADR-001"])
        assert "warning" not in result

    def test_skip_step(self, tmp_pm_path):
        start_workflow(tmp_pm_path, "auth", "development")
        result = advance_step(tmp_pm_path, skip=True)
        assert result["status"] == "skipped"
        assert result["skipped_step"]["id"] == "decision"
        assert result["current_step"]["id"] == "tasks"

    def test_complete_workflow(self, tmp_pm_path):
        start_workflow(tmp_pm_path, "auth", "development")
        # Advance through all 9 steps
        for _ in range(9):
            result = advance_step(tmp_pm_path, skip=True)

        assert result["workflow_completed"] is True
        wf = get_workflow(tmp_pm_path, "WF-001")
        assert wf.status == WorkflowStatus.COMPLETED

    def test_complete_with_chain(self, tmp_pm_path):
        start_workflow(tmp_pm_path, "research", "discovery")
        # Advance through all 5 steps
        for _ in range(5):
            result = advance_step(tmp_pm_path, skip=True)

        assert result["workflow_completed"] is True
        assert result["chain_to"] == "development"
        assert "message" in result

    def test_advance_completed_workflow(self, tmp_pm_path):
        start_workflow(tmp_pm_path, "auth", "development")
        for _ in range(9):
            advance_step(tmp_pm_path, skip=True)

        result = advance_step(tmp_pm_path, workflow_id="WF-001")
        assert result["status"] == "error"
        assert "not active" in result["message"]

    def test_advance_no_active_workflow(self, tmp_pm_path):
        with pytest.raises(PmServerError, match="No active workflow"):
            advance_step(tmp_pm_path)

    def test_auto_detect_active_workflow(self, tmp_pm_path):
        """advance_step without workflow_id should find the active one."""
        start_workflow(tmp_pm_path, "first", "development")
        start_workflow(tmp_pm_path, "second", "development")
        # Should advance WF-002 (latest active)
        result = advance_step(tmp_pm_path)
        assert result["workflow_id"] == "WF-002"


# ─── Loop Tests ─────────────────────────────────────


class TestWorkflowLoops:
    """Test brainstorming loop behavior."""

    def test_loop_back(self, tmp_pm_path):
        start_workflow(tmp_pm_path, "research", "discovery")
        # First step is research (in brainstorm group)
        result = advance_step(tmp_pm_path, proceed=False)
        assert result["status"] == "looped"
        assert result["iteration"] == 1
        assert result["loop_group"] == "brainstorm"
        assert result["current_step"]["id"] == "research"

    def test_loop_preserves_artifacts(self, tmp_pm_path):
        start_workflow(tmp_pm_path, "research", "discovery")
        # Record artifacts on first iteration
        advance_step(tmp_pm_path, artifacts=["finding-1"])
        advance_step(tmp_pm_path)  # fact_check
        # Loop back from proposal
        advance_step(tmp_pm_path, proceed=False)

        wf = get_workflow(tmp_pm_path, "WF-001")
        # Artifacts from first iteration should be preserved
        research = wf.steps[0]
        assert "finding-1" in research.artifacts

    def test_loop_increments_iteration(self, tmp_pm_path):
        start_workflow(tmp_pm_path, "research", "discovery")
        # Loop 3 times
        for i in range(3):
            advance_step(tmp_pm_path, proceed=False)

        wf = get_workflow(tmp_pm_path, "WF-001")
        assert wf.steps[0].iteration == 3

    def test_loop_then_proceed(self, tmp_pm_path):
        start_workflow(tmp_pm_path, "research", "discovery")
        # Loop once
        advance_step(tmp_pm_path, proceed=False)
        # Then proceed through the loop steps
        advance_step(tmp_pm_path)  # research done
        advance_step(tmp_pm_path)  # fact_check done
        result = advance_step(tmp_pm_path)  # proposal done, should go to cross_check
        assert result["current_step"]["id"] == "cross_check"

    def test_loop_resets_all_group_steps(self, tmp_pm_path):
        start_workflow(tmp_pm_path, "research", "discovery")
        # Advance research and fact_check
        advance_step(tmp_pm_path)  # research → fact_check
        advance_step(tmp_pm_path)  # fact_check → proposal
        # Loop back from proposal
        advance_step(tmp_pm_path, proceed=False)

        wf = get_workflow(tmp_pm_path, "WF-001")
        # All brainstorm steps should be reset
        brainstorm = [s for s in wf.steps if s.loop_group == "brainstorm"]
        assert brainstorm[0].status == WorkflowStepStatus.ACTIVE  # research
        assert brainstorm[1].status == WorkflowStepStatus.PENDING  # fact_check
        assert brainstorm[2].status == WorkflowStepStatus.PENDING  # proposal


# ─── Guidance Tests ─────────────────────────────────


class TestStepGuidance:
    """Test that step guidance includes appropriate hints."""

    def test_guidance_includes_tool_hint(self, tmp_pm_path):
        start_workflow(tmp_pm_path, "auth", "development")
        status = workflow_status(tmp_pm_path)
        # decision step has tool_hint: pm_add_decision
        step = status["current_step"]
        assert step["tool_hint"] == "pm_add_decision"

    def test_guidance_includes_gate(self, tmp_pm_path):
        start_workflow(tmp_pm_path, "auth", "development")
        # Advance to the check step (5th step, index 4)
        for _ in range(4):
            advance_step(tmp_pm_path, skip=True)

        status = workflow_status(tmp_pm_path)
        assert status["current_step"]["gate"] == "user_approval"

    def test_guidance_includes_loop_info(self, tmp_pm_path):
        start_workflow(tmp_pm_path, "research", "discovery")
        status = workflow_status(tmp_pm_path)
        step = status["current_step"]
        assert step["loop"] is True
        assert step["loop_group"] == "brainstorm"
        assert step["iteration"] == 0

    def test_guidance_includes_optional(self, tmp_pm_path):
        start_workflow(tmp_pm_path, "auth", "development")
        # Advance to the issues step (last, index 8)
        for _ in range(8):
            advance_step(tmp_pm_path, skip=True)

        status = workflow_status(tmp_pm_path)
        assert status["current_step"]["optional"] is True

    def test_guidance_omits_empty_fields(self, tmp_pm_path):
        start_workflow(tmp_pm_path, "auth", "development")
        status = workflow_status(tmp_pm_path)
        # The implement step (index 5) has no gate, no loop, no optional
        all_steps = status["steps"]
        implement_step = next(s for s in all_steps if s["id"] == "implement")
        assert "gate" not in implement_step
        assert "loop" not in implement_step
        assert "optional" not in implement_step

    def test_guidance_produces_consumes(self, tmp_pm_path):
        start_workflow(tmp_pm_path, "auth", "development")
        status = workflow_status(tmp_pm_path)
        decision = status["current_step"]
        assert "ADR" in decision["produces"]
        assert decision.get("required_artifacts") == ["ADR"]


# ─── Progress Tests ─────────────────────────────────


class TestWorkflowProgress:
    """Test progress calculation."""

    def test_progress_initial(self, tmp_pm_path):
        start_workflow(tmp_pm_path, "auth", "development")
        status = workflow_status(tmp_pm_path)
        assert status["progress"] == "0/9"

    def test_progress_after_advance(self, tmp_pm_path):
        start_workflow(tmp_pm_path, "auth", "development")
        advance_step(tmp_pm_path, skip=True)
        advance_step(tmp_pm_path, skip=True)
        status = workflow_status(tmp_pm_path)
        assert status["progress"] == "2/9"

    def test_progress_counts_skipped(self, tmp_pm_path):
        start_workflow(tmp_pm_path, "auth", "development")
        advance_step(tmp_pm_path, skip=True)  # skipped
        advance_step(tmp_pm_path)  # done
        status = workflow_status(tmp_pm_path)
        assert status["progress"] == "2/9"

    def test_progress_completed(self, tmp_pm_path):
        start_workflow(tmp_pm_path, "auth", "development")
        for _ in range(9):
            advance_step(tmp_pm_path, skip=True)
        status = workflow_status(tmp_pm_path, "WF-001")
        assert status["progress"] == "9/9"
        assert status["status"] == "completed"


# ─── Edge Cases ─────────────────────────────────────


class TestWorkflowEdgeCases:
    """Test edge cases and error paths."""

    def test_advance_specific_workflow(self, tmp_pm_path):
        start_workflow(tmp_pm_path, "first", "development")
        start_workflow(tmp_pm_path, "second", "development")
        result = advance_step(tmp_pm_path, workflow_id="WF-001")
        assert result["workflow_id"] == "WF-001"

    def test_advance_nonexistent_workflow(self, tmp_pm_path):
        with pytest.raises(WorkflowNotFoundError):
            advance_step(tmp_pm_path, workflow_id="WF-999")

    def test_notes_append(self, tmp_pm_path):
        start_workflow(tmp_pm_path, "auth", "development")
        advance_step(tmp_pm_path, notes="First note", skip=True)
        # Can't test append directly since we moved on,
        # but we can test the note was stored
        wf = get_workflow(tmp_pm_path, "WF-001")
        assert wf.steps[0].notes == "First note"

    def test_multiple_artifacts(self, tmp_pm_path):
        start_workflow(tmp_pm_path, "auth", "development")
        advance_step(
            tmp_pm_path,
            artifacts=["ADR-001", "ADR-002"],
        )
        wf = get_workflow(tmp_pm_path, "WF-001")
        assert wf.steps[0].artifacts == ["ADR-001", "ADR-002"]

    def test_workflow_id_format(self, tmp_pm_path):
        start_workflow(tmp_pm_path, "a", "development")
        wf = get_workflow(tmp_pm_path, "WF-001")
        assert wf.id == "WF-001"

        start_workflow(tmp_pm_path, "b", "development")
        wf = get_workflow(tmp_pm_path, "WF-002")
        assert wf.id == "WF-002"

    def test_updated_timestamp(self, tmp_pm_path):
        start_workflow(tmp_pm_path, "auth", "development")
        advance_step(tmp_pm_path)
        wf = get_workflow(tmp_pm_path, "WF-001")
        assert wf.updated == _dt.date.today()


# ─── MCP Tool Tests ─────────────────────────────────


class TestWorkflowMcpTools:
    """Test server.py MCP tool wrappers (import-level verification)."""

    def test_tool_imports(self):
        from pm_server.server import (
            pm_workflow_advance,
            pm_workflow_list,
            pm_workflow_start,
            pm_workflow_status,
            pm_workflow_templates,
        )

        assert callable(pm_workflow_start)
        assert callable(pm_workflow_status)
        assert callable(pm_workflow_advance)
        assert callable(pm_workflow_list)
        assert callable(pm_workflow_templates)

    def test_workflow_list_tool(self, tmp_pm_path):
        """Test pm_workflow_list logic directly via storage."""
        add_workflow(
            tmp_pm_path,
            Workflow(id="WF-001", name="A", feature="a", template="dev"),
        )
        add_workflow(
            tmp_pm_path,
            Workflow(
                id="WF-002",
                name="B",
                feature="b",
                template="dev",
                status=WorkflowStatus.COMPLETED,
            ),
        )

        # All workflows
        workflows = load_workflows(tmp_pm_path)
        assert len(workflows) == 2

        # Filter by status
        active = [w for w in workflows if w.status == WorkflowStatus.ACTIVE]
        assert len(active) == 1
        assert active[0].id == "WF-001"

    def test_workflow_templates_tool(self):
        """Test pm_workflow_templates logic directly."""
        templates = list_workflow_templates()
        assert len(templates) >= 2
        names = {t["name"] for t in templates}
        assert "development" in names
        assert "discovery" in names
