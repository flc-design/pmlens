"""Comprehensive tests for knowledge records.

Covers:
- Knowledge models and enums
- Storage CRUD (load, save, add, update, next_number)
- MCP tools (pm_record, pm_knowledge)
- Workflow × knowledge integration
- Edge cases and error handling
"""

from __future__ import annotations

import datetime as _dt

import pytest

from pm_server.models import (
    ConfidenceLevel,
    KnowledgeCategory,
    KnowledgeNotFoundError,
    KnowledgeRecord,
    KnowledgeStatus,
)
from pm_server.storage import (
    add_knowledge,
    load_knowledge,
    next_knowledge_number,
    save_knowledge,
    update_knowledge,
)
from pm_server.workflow import start_workflow, workflow_status

# ─── Model Tests ────────────────────────────────────


class TestKnowledgeModels:
    """Test Pydantic models for knowledge objects."""

    def test_knowledge_category_values(self):
        assert len(KnowledgeCategory) == 9
        expected = {
            "research",
            "market",
            "spike",
            "requirement",
            "constraint",
            "tradeoff",
            "risk_analysis",
            "spec",
            "api_design",
        }
        assert {c.value for c in KnowledgeCategory} == expected

    def test_knowledge_status_values(self):
        assert KnowledgeStatus.DRAFT.value == "draft"
        assert KnowledgeStatus.VALIDATED.value == "validated"
        assert KnowledgeStatus.SUPERSEDED.value == "superseded"

    def test_confidence_level_values(self):
        assert ConfidenceLevel.HIGH.value == "high"
        assert ConfidenceLevel.MEDIUM.value == "medium"
        assert ConfidenceLevel.LOW.value == "low"

    def test_record_defaults(self):
        kr = KnowledgeRecord(id="KR-001", category="research", title="Test")
        assert kr.status == KnowledgeStatus.DRAFT
        assert kr.confidence == ConfidenceLevel.MEDIUM
        assert kr.findings == ""
        assert kr.conclusion == ""
        assert kr.sources == []
        assert kr.tags == []
        assert kr.task_id is None
        assert kr.workflow_id is None
        assert kr.created == _dt.date.today()

    def test_record_all_fields(self):
        kr = KnowledgeRecord(
            id="KR-001",
            category=KnowledgeCategory.TRADEOFF,
            title="SQL vs NoSQL",
            status=KnowledgeStatus.VALIDATED,
            confidence=ConfidenceLevel.HIGH,
            findings="SQL better for relational data",
            conclusion="Use PostgreSQL",
            sources=["paper1.pdf", "benchmark.md"],
            tags=["database", "architecture"],
            task_id="PROJ-005",
            workflow_id="WF-001",
        )
        assert kr.category == KnowledgeCategory.TRADEOFF
        assert kr.confidence == ConfidenceLevel.HIGH
        assert len(kr.sources) == 2
        assert kr.workflow_id == "WF-001"


# ─── Storage Tests ──────────────────────────────────


class TestKnowledgeStorage:
    """Test YAML CRUD for knowledge records."""

    def test_load_empty(self, tmp_pm_path):
        records = load_knowledge(tmp_pm_path)
        assert records == []

    def test_save_and_load(self, tmp_pm_path):
        kr = KnowledgeRecord(id="KR-001", category="research", title="Test Research")
        save_knowledge(tmp_pm_path, [kr])
        loaded = load_knowledge(tmp_pm_path)
        assert len(loaded) == 1
        assert loaded[0].id == "KR-001"
        assert loaded[0].category == KnowledgeCategory.RESEARCH

    def test_add_knowledge(self, tmp_pm_path):
        kr = KnowledgeRecord(id="KR-001", category="spike", title="Spike: FastMCP v3")
        add_knowledge(tmp_pm_path, kr)
        loaded = load_knowledge(tmp_pm_path)
        assert len(loaded) == 1

    def test_add_multiple(self, tmp_pm_path):
        add_knowledge(
            tmp_pm_path,
            KnowledgeRecord(id="KR-001", category="research", title="A"),
        )
        add_knowledge(
            tmp_pm_path,
            KnowledgeRecord(id="KR-002", category="market", title="B"),
        )
        loaded = load_knowledge(tmp_pm_path)
        assert len(loaded) == 2

    def test_update_knowledge(self, tmp_pm_path):
        add_knowledge(
            tmp_pm_path,
            KnowledgeRecord(id="KR-001", category="research", title="Draft"),
        )
        updated = update_knowledge(
            tmp_pm_path,
            "KR-001",
            status=KnowledgeStatus.VALIDATED,
            confidence=ConfidenceLevel.HIGH,
            conclusion="Confirmed findings",
        )
        assert updated.status == KnowledgeStatus.VALIDATED
        assert updated.confidence == ConfidenceLevel.HIGH
        assert updated.conclusion == "Confirmed findings"
        assert updated.updated == _dt.date.today()

    def test_update_nonexistent(self, tmp_pm_path):
        with pytest.raises(KnowledgeNotFoundError):
            update_knowledge(tmp_pm_path, "KR-999")

    def test_next_knowledge_number_empty(self, tmp_pm_path):
        assert next_knowledge_number(tmp_pm_path) == 1

    def test_next_knowledge_number_sequential(self, tmp_pm_path):
        add_knowledge(
            tmp_pm_path,
            KnowledgeRecord(id="KR-001", category="research", title="A"),
        )
        add_knowledge(
            tmp_pm_path,
            KnowledgeRecord(id="KR-003", category="market", title="B"),
        )
        assert next_knowledge_number(tmp_pm_path) == 4

    def test_yaml_roundtrip_preserves_fields(self, tmp_pm_path):
        kr = KnowledgeRecord(
            id="KR-001",
            category="tradeoff",
            title="SQL vs NoSQL",
            status=KnowledgeStatus.VALIDATED,
            confidence=ConfidenceLevel.HIGH,
            findings="Detailed findings",
            conclusion="Use PostgreSQL",
            sources=["paper.pdf"],
            tags=["db", "arch"],
            task_id="PROJ-005",
            workflow_id="WF-001",
        )
        save_knowledge(tmp_pm_path, [kr])
        loaded = load_knowledge(tmp_pm_path)[0]

        assert loaded.category == KnowledgeCategory.TRADEOFF
        assert loaded.status == KnowledgeStatus.VALIDATED
        assert loaded.confidence == ConfidenceLevel.HIGH
        assert loaded.sources == ["paper.pdf"]
        assert loaded.workflow_id == "WF-001"


# ─── Workflow Integration Tests ─────────────────────


class TestKnowledgeWorkflowIntegration:
    """Test knowledge records linked to workflows."""

    def test_workflow_status_no_knowledge(self, tmp_pm_path):
        start_workflow(tmp_pm_path, "test", "development")
        status = workflow_status(tmp_pm_path)
        assert "knowledge" not in status

    def test_workflow_status_with_knowledge(self, tmp_pm_path):
        start_workflow(tmp_pm_path, "test", "development")
        add_knowledge(
            tmp_pm_path,
            KnowledgeRecord(
                id="KR-001",
                category="research",
                title="Auth research",
                workflow_id="WF-001",
            ),
        )
        add_knowledge(
            tmp_pm_path,
            KnowledgeRecord(
                id="KR-002",
                category="tradeoff",
                title="JWT vs Session",
                workflow_id="WF-001",
            ),
        )
        status = workflow_status(tmp_pm_path)
        assert status["knowledge"]["count"] == 2
        assert status["knowledge"]["by_category"]["research"] == 1
        assert status["knowledge"]["by_category"]["tradeoff"] == 1

    def test_workflow_status_ignores_other_workflow(self, tmp_pm_path):
        start_workflow(tmp_pm_path, "first", "development")
        add_knowledge(
            tmp_pm_path,
            KnowledgeRecord(
                id="KR-001",
                category="research",
                title="Unlinked",
                workflow_id="WF-999",
            ),
        )
        status = workflow_status(tmp_pm_path)
        assert "knowledge" not in status

    def test_knowledge_filter_by_workflow_id(self, tmp_pm_path):
        add_knowledge(
            tmp_pm_path,
            KnowledgeRecord(id="KR-001", category="research", title="A", workflow_id="WF-001"),
        )
        add_knowledge(
            tmp_pm_path,
            KnowledgeRecord(id="KR-002", category="research", title="B", workflow_id="WF-002"),
        )
        all_records = load_knowledge(tmp_pm_path)
        filtered = [r for r in all_records if r.workflow_id == "WF-001"]
        assert len(filtered) == 1
        assert filtered[0].id == "KR-001"


# ─── Filter Tests ───────────────────────────────────


class TestKnowledgeFilters:
    """Test filtering knowledge records."""

    def _seed_records(self, tmp_pm_path):
        records = [
            KnowledgeRecord(
                id="KR-001",
                category="research",
                title="Auth Research",
                tags=["auth", "security"],
                task_id="PROJ-001",
                status=KnowledgeStatus.VALIDATED,
            ),
            KnowledgeRecord(
                id="KR-002",
                category="market",
                title="Market Analysis",
                tags=["market"],
                task_id="PROJ-002",
            ),
            KnowledgeRecord(
                id="KR-003",
                category="tradeoff",
                title="JWT vs Session",
                tags=["auth"],
                task_id="PROJ-001",
            ),
            KnowledgeRecord(
                id="KR-004",
                category="research",
                title="DB Research",
                tags=["database"],
                status=KnowledgeStatus.SUPERSEDED,
            ),
        ]
        for r in records:
            add_knowledge(tmp_pm_path, r)

    def test_filter_by_category(self, tmp_pm_path):
        self._seed_records(tmp_pm_path)
        records = load_knowledge(tmp_pm_path)
        research = [r for r in records if r.category == KnowledgeCategory.RESEARCH]
        assert len(research) == 2

    def test_filter_by_status(self, tmp_pm_path):
        self._seed_records(tmp_pm_path)
        records = load_knowledge(tmp_pm_path)
        validated = [r for r in records if r.status == KnowledgeStatus.VALIDATED]
        assert len(validated) == 1
        assert validated[0].id == "KR-001"

    def test_filter_by_tag(self, tmp_pm_path):
        self._seed_records(tmp_pm_path)
        records = load_knowledge(tmp_pm_path)
        auth_records = [r for r in records if "auth" in r.tags]
        assert len(auth_records) == 2

    def test_filter_by_task_id(self, tmp_pm_path):
        self._seed_records(tmp_pm_path)
        records = load_knowledge(tmp_pm_path)
        proj001 = [r for r in records if r.task_id == "PROJ-001"]
        assert len(proj001) == 2

    def test_combined_filters(self, tmp_pm_path):
        self._seed_records(tmp_pm_path)
        records = load_knowledge(tmp_pm_path)
        # research + auth tag
        filtered = [
            r for r in records if r.category == KnowledgeCategory.RESEARCH and "auth" in r.tags
        ]
        assert len(filtered) == 1
        assert filtered[0].id == "KR-001"


# ─── Lifecycle Tests ────────────────────────────────


class TestKnowledgeLifecycle:
    """Test draft → validated → superseded lifecycle."""

    def test_draft_to_validated(self, tmp_pm_path):
        add_knowledge(
            tmp_pm_path,
            KnowledgeRecord(id="KR-001", category="research", title="Draft"),
        )
        updated = update_knowledge(
            tmp_pm_path,
            "KR-001",
            status=KnowledgeStatus.VALIDATED,
            confidence=ConfidenceLevel.HIGH,
        )
        assert updated.status == KnowledgeStatus.VALIDATED
        assert updated.confidence == ConfidenceLevel.HIGH

    def test_validated_to_superseded(self, tmp_pm_path):
        add_knowledge(
            tmp_pm_path,
            KnowledgeRecord(
                id="KR-001",
                category="research",
                title="Old",
                status=KnowledgeStatus.VALIDATED,
            ),
        )
        updated = update_knowledge(
            tmp_pm_path,
            "KR-001",
            status=KnowledgeStatus.SUPERSEDED,
        )
        assert updated.status == KnowledgeStatus.SUPERSEDED

    def test_update_conclusion(self, tmp_pm_path):
        add_knowledge(
            tmp_pm_path,
            KnowledgeRecord(
                id="KR-001",
                category="tradeoff",
                title="Decision Pending",
            ),
        )
        updated = update_knowledge(
            tmp_pm_path,
            "KR-001",
            conclusion="Choose option A",
        )
        assert updated.conclusion == "Choose option A"


# ─── MCP Tool Import Tests ─────────────────────────


class TestKnowledgeMcpTools:
    """Test that MCP tools are importable and callable."""

    def test_tool_imports(self):
        from pm_server.server import pm_knowledge, pm_record

        assert callable(pm_record)
        assert callable(pm_knowledge)

    def test_record_id_format(self, tmp_pm_path):
        """Verify KR-xxx ID format via storage."""
        add_knowledge(
            tmp_pm_path,
            KnowledgeRecord(id="KR-001", category="research", title="A"),
        )
        assert next_knowledge_number(tmp_pm_path) == 2
