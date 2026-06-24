"""Tests for velocity calculation and risk detection."""

import datetime as _dt

from pmlens.models import Phase, PhaseStatus, Project, Task, TaskStatus
from pmlens.storage import _save_project, _save_tasks
from pmlens.velocity import calculate_velocity, detect_risks


class TestCalculateVelocity:
    def test_empty_project(self, tmp_pm_path):
        result = calculate_velocity(tmp_pm_path)
        assert result["average"] == 0
        assert result["total_done"] == 0
        assert result["trend"] == "stable"
        assert len(result["weeks"]) == 4

    def test_with_completed_tasks(self, tmp_pm_path):
        today = _dt.date.today()
        tasks = [
            Task(
                id="T-001",
                title="Done recently",
                phase="p1",
                status=TaskStatus.DONE,
                updated=today,
            ),
            Task(
                id="T-002",
                title="Done recently too",
                phase="p1",
                status=TaskStatus.DONE,
                updated=today - _dt.timedelta(days=1),
            ),
            Task(
                id="T-003",
                title="Still todo",
                phase="p1",
                status=TaskStatus.TODO,
            ),
        ]
        _save_tasks(tmp_pm_path, tasks)
        result = calculate_velocity(tmp_pm_path)
        assert result["total_done"] == 2
        assert result["average"] > 0

    def test_single_week(self, tmp_pm_path):
        result = calculate_velocity(tmp_pm_path, weeks=1)
        assert len(result["weeks"]) == 1

    def test_trend_improving(self, tmp_pm_path):
        today = _dt.date.today()
        # Old tasks completed weeks ago, many tasks completed this week
        tasks = [
            Task(
                id=f"T-{i:03d}",
                title=f"Recent {i}",
                phase="p1",
                status=TaskStatus.DONE,
                updated=today - _dt.timedelta(days=1),
            )
            for i in range(1, 6)
        ]
        _save_tasks(tmp_pm_path, tasks)
        result = calculate_velocity(tmp_pm_path, weeks=4)
        # All completions in the most recent week → trend should be improving or stable
        assert result["trend"] in ("improving", "stable")

    def test_trend_declining(self, tmp_pm_path):
        today = _dt.date.today()
        # Tasks completed only in the oldest week
        tasks = [
            Task(
                id=f"T-{i:03d}",
                title=f"Old {i}",
                phase="p1",
                status=TaskStatus.DONE,
                updated=today - _dt.timedelta(weeks=3),
            )
            for i in range(1, 6)
        ]
        _save_tasks(tmp_pm_path, tasks)
        result = calculate_velocity(tmp_pm_path, weeks=4)
        assert result["trend"] in ("declining", "stable")

    def test_weeks_in_chronological_order(self, tmp_pm_path):
        result = calculate_velocity(tmp_pm_path, weeks=4)
        dates = [w["week_start"] for w in result["weeks"]]
        assert dates == sorted(dates)


class TestDetectRisks:
    def test_no_risks(self, tmp_pm_path):
        tasks = [
            Task(id="T-001", title="Normal", phase="p1", status=TaskStatus.TODO),
        ]
        _save_tasks(tmp_pm_path, tasks)
        risks = detect_risks(tmp_pm_path)
        assert risks == []

    def test_blocked_task_detected(self, tmp_pm_path):
        tasks = [
            Task(
                id="T-001",
                title="Blocked task",
                phase="p1",
                status=TaskStatus.BLOCKED,
                blocked_by=["T-000"],
                updated=_dt.date.today() - _dt.timedelta(days=3),
            ),
        ]
        _save_tasks(tmp_pm_path, tasks)
        risks = detect_risks(tmp_pm_path)
        assert len(risks) == 1
        assert risks[0]["type"] == "blocked_task"
        assert risks[0]["severity"] == "medium"

    def test_long_blocked_is_high_severity(self, tmp_pm_path):
        tasks = [
            Task(
                id="T-001",
                title="Long blocked",
                phase="p1",
                status=TaskStatus.BLOCKED,
                blocked_by=["T-000"],
                updated=_dt.date.today() - _dt.timedelta(days=10),
            ),
        ]
        _save_tasks(tmp_pm_path, tasks)
        risks = detect_risks(tmp_pm_path)
        assert risks[0]["severity"] == "high"

    def test_stale_in_progress_detected(self, tmp_pm_path):
        tasks = [
            Task(
                id="T-001",
                title="Stale WIP",
                phase="p1",
                status=TaskStatus.IN_PROGRESS,
                updated=_dt.date.today() - _dt.timedelta(days=10),
            ),
        ]
        _save_tasks(tmp_pm_path, tasks)
        risks = detect_risks(tmp_pm_path)
        assert len(risks) == 1
        assert risks[0]["type"] == "stale_task"

    def test_fresh_in_progress_not_flagged(self, tmp_pm_path):
        tasks = [
            Task(
                id="T-001",
                title="Fresh WIP",
                phase="p1",
                status=TaskStatus.IN_PROGRESS,
                updated=_dt.date.today(),
            ),
        ]
        _save_tasks(tmp_pm_path, tasks)
        risks = detect_risks(tmp_pm_path)
        assert risks == []

    def test_estimate_overrun_detected(self, tmp_pm_path):
        tasks = [
            Task(
                id="T-001",
                title="Overrun",
                phase="p1",
                status=TaskStatus.IN_PROGRESS,
                estimate_hours=4.0,
                actual_hours=8.0,
                updated=_dt.date.today(),
            ),
        ]
        _save_tasks(tmp_pm_path, tasks)
        risks = detect_risks(tmp_pm_path)
        overrun = [r for r in risks if r["type"] == "overrun"]
        assert len(overrun) == 1

    def test_overrun_not_flagged_for_done(self, tmp_pm_path):
        tasks = [
            Task(
                id="T-001",
                title="Done overrun",
                phase="p1",
                status=TaskStatus.DONE,
                estimate_hours=4.0,
                actual_hours=8.0,
            ),
        ]
        _save_tasks(tmp_pm_path, tasks)
        risks = detect_risks(tmp_pm_path)
        overrun = [r for r in risks if r["type"] == "overrun"]
        assert overrun == []

    def test_empty_project(self, tmp_pm_path):
        risks = detect_risks(tmp_pm_path)
        assert risks == []

    def test_phase_overdue_detected(self, tmp_pm_path):
        project = Project(
            name="test",
            phases=[
                Phase(
                    id="p1",
                    name="Design",
                    status=PhaseStatus.ACTIVE,
                    target_date=_dt.date.today() - _dt.timedelta(days=5),
                ),
            ],
        )
        _save_project(tmp_pm_path, project)
        risks = detect_risks(tmp_pm_path)
        overdue = [r for r in risks if r["type"] == "phase_overdue"]
        assert len(overdue) == 1
        assert overdue[0]["severity"] == "medium"

    def test_phase_overdue_high_severity(self, tmp_pm_path):
        project = Project(
            name="test",
            phases=[
                Phase(
                    id="p1",
                    name="Core",
                    status=PhaseStatus.ACTIVE,
                    target_date=_dt.date.today() - _dt.timedelta(days=20),
                ),
            ],
        )
        _save_project(tmp_pm_path, project)
        risks = detect_risks(tmp_pm_path)
        overdue = [r for r in risks if r["type"] == "phase_overdue"]
        assert overdue[0]["severity"] == "high"

    def test_completed_phase_not_flagged(self, tmp_pm_path):
        project = Project(
            name="test",
            phases=[
                Phase(
                    id="p1",
                    name="Done Phase",
                    status=PhaseStatus.COMPLETED,
                    target_date=_dt.date.today() - _dt.timedelta(days=30),
                ),
            ],
        )
        _save_project(tmp_pm_path, project)
        risks = detect_risks(tmp_pm_path)
        overdue = [r for r in risks if r["type"] == "phase_overdue"]
        assert overdue == []
