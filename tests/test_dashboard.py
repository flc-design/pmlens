"""Tests for dashboard generation."""

from unittest.mock import patch

import pytest

from pmlens.dashboard import (
    render_portfolio_dashboard,
    render_project_dashboard,
)
from pmlens.models import (
    KnowledgeCategory,
    KnowledgeRecord,
    Registry,
    RegistryEntry,
    Workflow,
    WorkflowStatus,
    WorkflowStep,
    WorkflowStepStatus,
)
from pmlens.storage import (
    _save_knowledge,
    _save_project,
    _save_tasks,
    _save_workflows,
    init_pm_directory,
)


@pytest.fixture
def dashboard_project(tmp_path, sample_project, sample_tasks):
    """Create a project with enough data for dashboard rendering."""
    pm_path = init_pm_directory(tmp_path)
    _save_project(pm_path, sample_project)
    _save_tasks(pm_path, sample_tasks)
    return pm_path


class TestProjectDashboard:
    def test_html_output(self, dashboard_project):
        html = render_project_dashboard(dashboard_project, format="html")
        assert "<!DOCTYPE html>" in html
        assert "Test Project" in html
        assert "Chart" in html

    def test_text_output(self, dashboard_project):
        text = render_project_dashboard(dashboard_project, format="text")
        assert "Test Project" in text
        assert "TODO:" in text or "Todo:" in text or "todo" in text.lower()

    def test_text_shows_blockers(self, dashboard_project):
        text = render_project_dashboard(dashboard_project, format="text")
        assert "TEST-004" in text or "Blocker" in text or "blocked" in text.lower()

    def test_text_shows_velocity(self, dashboard_project):
        text = render_project_dashboard(dashboard_project, format="text")
        assert "Velocity" in text or "velocity" in text


class TestProjectDashboardWorkflow:
    """Tests for workflow progress and knowledge map in project dashboard."""

    @pytest.fixture
    def dashboard_with_workflow(self, tmp_path, sample_project, sample_tasks):
        pm_path = init_pm_directory(tmp_path)
        _save_project(pm_path, sample_project)
        _save_tasks(pm_path, sample_tasks)
        wf = Workflow(
            id="WF-001",
            name="Development",
            feature="Add auth",
            template="development",
            steps=[
                WorkflowStep(
                    id="decision",
                    name="Architecture Decision",
                    status=WorkflowStepStatus.DONE,
                ),
                WorkflowStep(
                    id="tasks",
                    name="Task Breakdown",
                    status=WorkflowStepStatus.DONE,
                ),
                WorkflowStep(
                    id="implement",
                    name="Implementation",
                    status=WorkflowStepStatus.ACTIVE,
                ),
                WorkflowStep(
                    id="test",
                    name="Testing",
                    status=WorkflowStepStatus.PENDING,
                ),
            ],
            current_step_index=2,
            status=WorkflowStatus.ACTIVE,
        )
        _save_workflows(pm_path, [wf])
        return pm_path

    @pytest.fixture
    def dashboard_with_knowledge(self, tmp_path, sample_project, sample_tasks):
        pm_path = init_pm_directory(tmp_path)
        _save_project(pm_path, sample_project)
        _save_tasks(pm_path, sample_tasks)
        records = [
            KnowledgeRecord(
                id="KR-001",
                category=KnowledgeCategory.RESEARCH,
                title="Auth research",
            ),
            KnowledgeRecord(
                id="KR-002",
                category=KnowledgeCategory.TRADEOFF,
                title="JWT vs Session",
            ),
            KnowledgeRecord(
                id="KR-003",
                category=KnowledgeCategory.RESEARCH,
                title="OAuth providers",
            ),
        ]
        _save_knowledge(pm_path, records)
        return pm_path

    def test_html_shows_workflow(self, dashboard_with_workflow):
        html = render_project_dashboard(dashboard_with_workflow, format="html")
        assert "WF-001" in html
        assert "Add auth" in html
        assert "Architecture Decision" in html
        assert "Implementation" in html

    def test_text_shows_workflow(self, dashboard_with_workflow):
        text = render_project_dashboard(dashboard_with_workflow, format="text")
        assert "Workflows:" in text
        assert "WF-001" in text
        assert "Add auth" in text
        assert "2/4" in text

    def test_html_shows_knowledge(self, dashboard_with_knowledge):
        html = render_project_dashboard(dashboard_with_knowledge, format="html")
        assert "Knowledge Map" in html
        assert "research" in html
        assert "tradeoff" in html
        assert "knowledgeChart" in html

    def test_text_shows_knowledge(self, dashboard_with_knowledge):
        text = render_project_dashboard(dashboard_with_knowledge, format="text")
        assert "Knowledge Records: 3" in text
        assert "research: 2" in text
        assert "tradeoff: 1" in text

    def test_html_no_workflow_section_when_empty(self, dashboard_project):
        """No workflow section when there are no workflows."""
        html = render_project_dashboard(dashboard_project, format="html")
        assert "Workflows" not in html or "wf-timeline" not in html

    def test_html_no_knowledge_section_when_empty(self, dashboard_project):
        """No knowledge section when there are no knowledge records."""
        html = render_project_dashboard(dashboard_project, format="html")
        assert "Knowledge Map" not in html


class TestPortfolioDashboard:
    def test_html_empty(self):
        with patch("pmlens.dashboard.load_registry") as mock_reg:
            mock_reg.return_value = Registry()
            html = render_portfolio_dashboard(format="html")
            assert "<!DOCTYPE html>" in html
            assert "pm_init" in html

    def test_text_empty(self):
        with patch("pmlens.dashboard.load_registry") as mock_reg:
            mock_reg.return_value = Registry()
            text = render_portfolio_dashboard(format="text")
            assert "Portfolio" in text
            assert "0 projects" in text

    def test_text_with_projects(self, tmp_path, sample_project, sample_tasks):
        pm_path = init_pm_directory(tmp_path)
        _save_project(pm_path, sample_project)
        _save_tasks(pm_path, sample_tasks)

        with patch("pmlens.dashboard.load_registry") as mock_reg:
            mock_reg.return_value = Registry(
                projects=[RegistryEntry(path=str(tmp_path), name="testproj")]
            )
            text = render_portfolio_dashboard(format="text")
            assert "Test Project" in text

    def test_text_shows_wf_kr_columns(self, tmp_path, sample_project, sample_tasks):
        """Portfolio text output includes WF and KR columns."""
        pm_path = init_pm_directory(tmp_path)
        _save_project(pm_path, sample_project)
        _save_tasks(pm_path, sample_tasks)
        wf = Workflow(
            id="WF-001",
            name="Dev",
            feature="test",
            template="development",
            steps=[
                WorkflowStep(id="s1", name="S1", status=WorkflowStepStatus.ACTIVE),
            ],
            current_step_index=0,
            status=WorkflowStatus.ACTIVE,
        )
        _save_workflows(pm_path, [wf])
        records = [
            KnowledgeRecord(id="KR-001", category=KnowledgeCategory.RESEARCH, title="R1"),
            KnowledgeRecord(id="KR-002", category=KnowledgeCategory.SPEC, title="S1"),
        ]
        _save_knowledge(pm_path, records)

        with patch("pmlens.dashboard.load_registry") as mock_reg:
            mock_reg.return_value = Registry(
                projects=[RegistryEntry(path=str(tmp_path), name="testproj")]
            )
            text = render_portfolio_dashboard(format="text")
            assert "WF" in text
            assert "KR" in text

    def test_html_shows_wf_kr_columns(self, tmp_path, sample_project, sample_tasks):
        """Portfolio HTML output includes Workflows and Knowledge columns."""
        pm_path = init_pm_directory(tmp_path)
        _save_project(pm_path, sample_project)
        _save_tasks(pm_path, sample_tasks)
        wf = Workflow(
            id="WF-001",
            name="Dev",
            feature="test",
            template="development",
            steps=[
                WorkflowStep(id="s1", name="S1", status=WorkflowStepStatus.ACTIVE),
            ],
            current_step_index=0,
            status=WorkflowStatus.ACTIVE,
        )
        _save_workflows(pm_path, [wf])

        with patch("pmlens.dashboard.load_registry") as mock_reg:
            mock_reg.return_value = Registry(
                projects=[RegistryEntry(path=str(tmp_path), name="testproj")]
            )
            html = render_portfolio_dashboard(format="html")
            assert "Workflows" in html
            assert "Knowledge" in html
            assert "1 active" in html
