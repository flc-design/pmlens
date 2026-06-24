"""Tests for Pydantic data models."""

import datetime as _dt

from pmlens.models import (
    DailyLog,
    DailyLogEntry,
    Decision,
    DecisionStatus,
    LogCategory,
    PhaseStatus,
    Priority,
    Project,
    ProjectStatus,
    Registry,
    RegistryEntry,
    Risk,
    RiskSeverity,
    RiskStatus,
    Task,
    TaskStatus,
)


class TestEnums:
    def test_project_status_values(self):
        assert ProjectStatus.DESIGN.value == "design"
        assert ProjectStatus.ARCHIVED.value == "archived"

    def test_task_status_values(self):
        assert TaskStatus.TODO.value == "todo"
        assert TaskStatus.IN_PROGRESS.value == "in_progress"
        assert TaskStatus.BLOCKED.value == "blocked"

    def test_priority_values(self):
        assert Priority.P0.value == "P0"
        assert Priority.P3.value == "P3"

    def test_enum_from_string(self):
        assert TaskStatus("todo") == TaskStatus.TODO
        assert Priority("P1") == Priority.P1


class TestTask:
    def test_defaults(self):
        task = Task(id="T-001", title="Test", phase="phase-1")
        assert task.status == TaskStatus.TODO
        assert task.priority == Priority.P1
        assert task.assignee == "claude-code"
        assert task.depends_on == []
        assert task.tags == []

    def test_full_task(self):
        task = Task(
            id="PROJ-001",
            title="Implement feature",
            phase="phase-1",
            status=TaskStatus.IN_PROGRESS,
            priority=Priority.P0,
            estimate_hours=4.0,
            tags=["core", "feature"],
            depends_on=["PROJ-000"],
        )
        assert task.priority == Priority.P0
        assert "core" in task.tags
        assert task.estimate_hours == 4.0

    def test_serialization_roundtrip(self):
        task = Task(id="T-001", title="Test", phase="p1", tags=["a"])
        data = task.model_dump(mode="json")
        restored = Task(**data)
        assert restored.id == task.id
        assert restored.tags == ["a"]

    def test_parent_id_default_none(self):
        task = Task(id="T-001", title="Test", phase="p1")
        assert task.parent_id is None

    def test_parent_id_set(self):
        task = Task(id="T-002", title="Sub issue", phase="p1", parent_id="T-001")
        assert task.parent_id == "T-001"

    def test_parent_id_backward_compatible(self):
        """Existing YAML data without parent_id should load correctly."""
        data = {
            "id": "T-001",
            "title": "Old task",
            "phase": "p1",
            "status": "todo",
            "priority": "P1",
        }
        task = Task(**data)
        assert task.parent_id is None


class TestDecision:
    def test_defaults(self):
        d = Decision(id="ADR-001", title="Use YAML")
        assert d.status == DecisionStatus.ACCEPTED
        assert d.consequences.positive == []

    def test_with_consequences(self, sample_decision):
        assert sample_decision.consequences.positive == ["Git-friendly diffs"]
        assert len(sample_decision.consequences.negative) == 1


class TestProject:
    def test_defaults(self):
        p = Project(name="test")
        assert p.status == ProjectStatus.DEVELOPMENT
        assert p.version == "0.1.0"
        assert p.phases == []
        assert p.health.velocity is None

    def test_with_phases(self, sample_project):
        assert len(sample_project.phases) == 2
        assert sample_project.phases[0].status == PhaseStatus.COMPLETED

    def test_serialization(self, sample_project):
        data = sample_project.model_dump(mode="json")
        restored = Project(**data)
        assert restored.name == "testproj"
        assert len(restored.phases) == 2

    def test_pm_schema_default(self):
        p = Project(name="test")
        assert p.pm_schema == 1

    def test_pm_schema_backward_compat_old_yaml(self):
        # Old YAML payloads without `pm_schema` must load with the default.
        p = Project(**{"name": "legacy", "version": "0.5.1"})
        assert p.pm_schema == 1

    def test_pm_schema_serialized(self, sample_project):
        data = sample_project.model_dump(mode="json")
        assert data["pm_schema"] == 1


class TestRegistry:
    def test_empty(self):
        r = Registry()
        assert r.projects == []

    def test_with_entries(self):
        r = Registry(
            projects=[
                RegistryEntry(path="/a/b", name="proj1"),
                RegistryEntry(path="/c/d", name="proj2"),
            ]
        )
        assert len(r.projects) == 2


class TestDailyLog:
    def test_log_entry(self):
        entry = DailyLogEntry(time="14:30", category=LogCategory.PROGRESS, entry="Done task X")
        assert entry.category == LogCategory.PROGRESS

    def test_daily_log(self):
        log = DailyLog(
            date=_dt.date(2026, 4, 3),
            entries=[DailyLogEntry(time="10:00", entry="Started")],
        )
        assert len(log.entries) == 1


class TestRisk:
    def test_defaults(self):
        r = Risk(id="RISK-001", title="Deadline risk")
        assert r.severity == RiskSeverity.MEDIUM
        assert r.status == RiskStatus.OPEN

    def test_custom(self):
        r = Risk(
            id="RISK-002",
            title="Critical",
            severity=RiskSeverity.CRITICAL,
            related_tasks=["T-001"],
        )
        assert r.severity == RiskSeverity.CRITICAL
        assert "T-001" in r.related_tasks
