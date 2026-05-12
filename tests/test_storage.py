"""Tests for YAML storage layer."""

import datetime as _dt

import pytest

from pm_server.models import (
    DailyLogEntry,
    LogCategory,
    Milestone,
    PmServerError,
    ProjectStatus,
    Registry,
    RegistryEntry,
    Risk,
    Task,
    TaskNotFoundError,
    TaskStatus,
)
from pm_server.storage import (
    _yaml_transaction,
    add_daily_log,
    add_decision,
    add_milestone,
    add_risk,
    add_task,
    init_pm_directory,
    load_daily_log,
    load_decisions,
    load_milestones,
    load_project,
    load_registry,
    load_risks,
    load_tasks,
    next_decision_number,
    next_risk_number,
    next_task_number,
    register_project,
    save_milestones,
    save_project,
    save_registry,
    save_risks,
    save_tasks,
    unregister_project,
    update_task,
)


class TestProjectStorage:
    def test_save_and_load(self, tmp_pm_path, sample_project):
        save_project(tmp_pm_path, sample_project)
        loaded = load_project(tmp_pm_path)
        assert loaded.name == "testproj"
        assert loaded.version == "1.0.0"
        assert len(loaded.phases) == 2

    def test_load_missing_returns_default(self, tmp_pm_path):
        project = load_project(tmp_pm_path)
        assert project.name == tmp_pm_path.parent.name
        assert project.status == ProjectStatus.DEVELOPMENT

    def test_yaml_has_header(self, tmp_pm_path, sample_project):
        save_project(tmp_pm_path, sample_project)
        content = (tmp_pm_path / "project.yaml").read_text()
        assert content.startswith("# PM Server - project.yaml")


class TestTaskStorage:
    def test_save_and_load(self, tmp_pm_path, sample_tasks):
        save_tasks(tmp_pm_path, sample_tasks)
        loaded = load_tasks(tmp_pm_path)
        assert len(loaded) == 4
        assert loaded[0].id == "TEST-001"

    def test_load_missing_returns_empty(self, tmp_pm_path):
        assert load_tasks(tmp_pm_path) == []

    def test_add_task(self, tmp_pm_path):
        task = Task(id="NEW-001", title="New task", phase="phase-1")
        add_task(tmp_pm_path, task)
        loaded = load_tasks(tmp_pm_path)
        assert len(loaded) == 1
        assert loaded[0].title == "New task"

    def test_update_task(self, tmp_pm_path, sample_tasks):
        save_tasks(tmp_pm_path, sample_tasks)
        updated = update_task(tmp_pm_path, "TEST-002", status=TaskStatus.IN_PROGRESS)
        assert updated.status == TaskStatus.IN_PROGRESS

        # Verify persistence
        loaded = load_tasks(tmp_pm_path)
        t = next(t for t in loaded if t.id == "TEST-002")
        assert t.status == TaskStatus.IN_PROGRESS

    def test_update_nonexistent_raises(self, tmp_pm_path, sample_tasks):
        save_tasks(tmp_pm_path, sample_tasks)
        with pytest.raises(TaskNotFoundError):
            update_task(tmp_pm_path, "NOPE-999", status=TaskStatus.DONE)

    def test_next_task_number(self, tmp_pm_path, sample_tasks):
        save_tasks(tmp_pm_path, sample_tasks)
        assert next_task_number(tmp_pm_path) == 5  # TEST-004 + 1

    def test_next_task_number_empty(self, tmp_pm_path):
        assert next_task_number(tmp_pm_path) == 1


class TestDecisionStorage:
    def test_save_and_load(self, tmp_pm_path, sample_decision):
        add_decision(tmp_pm_path, sample_decision)
        loaded = load_decisions(tmp_pm_path)
        assert len(loaded) == 1
        assert loaded[0].title == "Use YAML for storage"

    def test_load_missing_returns_empty(self, tmp_pm_path):
        assert load_decisions(tmp_pm_path) == []

    def test_next_decision_number(self, tmp_pm_path, sample_decision):
        add_decision(tmp_pm_path, sample_decision)
        assert next_decision_number(tmp_pm_path) == 2


class TestDailyLog:
    def test_add_and_load(self, tmp_pm_path):
        entry = DailyLogEntry(time="14:30", category=LogCategory.PROGRESS, entry="Did stuff")
        log = add_daily_log(tmp_pm_path, entry, log_date=_dt.date(2026, 4, 3))
        assert len(log.entries) == 1

        loaded = load_daily_log(tmp_pm_path, log_date=_dt.date(2026, 4, 3))
        assert len(loaded.entries) == 1
        assert loaded.entries[0].entry == "Did stuff"

    def test_append_to_existing_log(self, tmp_pm_path):
        d = _dt.date(2026, 4, 3)
        add_daily_log(tmp_pm_path, DailyLogEntry(time="10:00", entry="Morning"), log_date=d)
        add_daily_log(tmp_pm_path, DailyLogEntry(time="14:00", entry="Afternoon"), log_date=d)
        loaded = load_daily_log(tmp_pm_path, log_date=d)
        assert len(loaded.entries) == 2


class TestRegistry:
    def test_save_and_load(self, tmp_registry_dir):
        registry = Registry(projects=[RegistryEntry(path="/a/b", name="proj1")])
        save_registry(registry, tmp_registry_dir)
        loaded = load_registry(tmp_registry_dir)
        assert len(loaded.projects) == 1

    def test_load_missing_returns_empty(self, tmp_registry_dir):
        reg = load_registry(tmp_registry_dir)
        assert reg.projects == []

    def test_register_project(self, tmp_registry_dir, tmp_project):
        register_project(tmp_project, "myproj", tmp_registry_dir)
        reg = load_registry(tmp_registry_dir)
        assert len(reg.projects) == 1
        assert reg.projects[0].name == "myproj"

    def test_register_idempotent(self, tmp_registry_dir, tmp_project):
        register_project(tmp_project, "myproj", tmp_registry_dir)
        register_project(tmp_project, "myproj", tmp_registry_dir)
        reg = load_registry(tmp_registry_dir)
        assert len(reg.projects) == 1

    def test_unregister(self, tmp_registry_dir, tmp_project):
        register_project(tmp_project, "myproj", tmp_registry_dir)
        unregister_project(tmp_project, tmp_registry_dir)
        reg = load_registry(tmp_registry_dir)
        assert len(reg.projects) == 0


class TestInitPmDirectory:
    def test_creates_structure(self, tmp_path):
        pm_path = init_pm_directory(tmp_path)
        assert pm_path.is_dir()
        assert (pm_path / "daily").is_dir()

    def test_idempotent(self, tmp_path):
        init_pm_directory(tmp_path)
        init_pm_directory(tmp_path)
        assert (tmp_path / ".pm").is_dir()


class TestRisksAndMilestones:
    def test_risks_roundtrip(self, tmp_pm_path):
        risks = [Risk(id="RISK-001", title="Deadline risk")]
        save_risks(tmp_pm_path, risks)
        loaded = load_risks(tmp_pm_path)
        assert len(loaded) == 1
        assert loaded[0].title == "Deadline risk"

    def test_milestones_roundtrip(self, tmp_pm_path):
        milestones = [Milestone(id="MS-001", name="MVP")]
        save_milestones(tmp_pm_path, milestones)
        loaded = load_milestones(tmp_pm_path)
        assert len(loaded) == 1
        assert loaded[0].name == "MVP"

    def test_add_risk(self, tmp_pm_path):
        risk = Risk(id="RISK-001", title="Test risk")
        add_risk(tmp_pm_path, risk)
        loaded = load_risks(tmp_pm_path)
        assert len(loaded) == 1
        assert loaded[0].id == "RISK-001"

    def test_next_risk_number(self, tmp_pm_path):
        add_risk(tmp_pm_path, Risk(id="RISK-001", title="A"))
        add_risk(tmp_pm_path, Risk(id="RISK-002", title="B"))
        assert next_risk_number(tmp_pm_path) == 3

    def test_next_risk_number_empty(self, tmp_pm_path):
        assert next_risk_number(tmp_pm_path) == 1

    def test_add_milestone(self, tmp_pm_path):
        ms = Milestone(id="MS-001", name="Alpha")
        add_milestone(tmp_pm_path, ms)
        loaded = load_milestones(tmp_pm_path)
        assert len(loaded) == 1
        assert loaded[0].name == "Alpha"


class TestBrokenYaml:
    def test_broken_yaml_raises_pm_server_error(self, tmp_pm_path):
        broken = tmp_pm_path / "project.yaml"
        broken.write_text("name: [invalid\n  yaml: {broken", encoding="utf-8")
        with pytest.raises(PmServerError, match="Failed to parse"):
            load_project(tmp_pm_path)

    def test_broken_tasks_yaml(self, tmp_pm_path):
        broken = tmp_pm_path / "tasks.yaml"
        broken.write_text("tasks:\n  - id: [bad", encoding="utf-8")
        with pytest.raises(PmServerError, match="Failed to parse"):
            load_tasks(tmp_pm_path)


class TestYamlTransactionLocking:
    """Unit tests for the _yaml_transaction context manager (PMSERV-048)."""

    def test_lock_dir_created_lazily(self, tmp_pm_path):
        assert not (tmp_pm_path / ".locks").exists()
        with _yaml_transaction(tmp_pm_path, "tasks.yaml"):
            # Lock dir must exist while the lock is held
            assert (tmp_pm_path / ".locks").is_dir()
            assert (tmp_pm_path / ".locks" / "tasks.lock").exists()
        # Dir persists after release; the lock file may be removed by filelock
        assert (tmp_pm_path / ".locks").is_dir()

    def test_lock_dir_seeds_gitignore(self, tmp_pm_path):
        with _yaml_transaction(tmp_pm_path, "decisions.yaml"):
            pass
        gitignore = tmp_pm_path / ".locks" / ".gitignore"
        assert gitignore.exists()
        content = gitignore.read_text(encoding="utf-8")
        assert "*" in content
        assert "!.gitignore" in content

    def test_lock_label_strips_yaml_suffix(self, tmp_pm_path):
        # Verify both label forms acquire on the same lock path by checking
        # the lock file is present inside the with-block (filelock may delete
        # it on release in newer versions, so we observe during the hold).
        with _yaml_transaction(tmp_pm_path, "tasks.yaml"):
            assert (tmp_pm_path / ".locks" / "tasks.lock").exists()
            # No "tasks.yaml.lock" — suffix must have been stripped
            assert not (tmp_pm_path / ".locks" / "tasks.yaml.lock").exists()
        with _yaml_transaction(tmp_pm_path, "tasks"):
            # Same path for the bare label
            assert (tmp_pm_path / ".locks" / "tasks.lock").exists()

    def test_releases_on_exception(self, tmp_pm_path):
        with pytest.raises(RuntimeError, match="boom"):
            with _yaml_transaction(tmp_pm_path, "tasks.yaml"):
                raise RuntimeError("boom")
        # Lock must be released — second acquire must succeed quickly
        with _yaml_transaction(tmp_pm_path, "tasks.yaml", timeout=1.0):
            pass

    def test_timeout_raises_pm_server_error(self, tmp_pm_path):
        from filelock import FileLock

        # Hold the lock externally, then try to acquire via _yaml_transaction
        external = FileLock(str(tmp_pm_path / ".locks" / "tasks.lock"))
        (tmp_pm_path / ".locks").mkdir(exist_ok=True)
        external.acquire(timeout=1)
        try:
            with pytest.raises(PmServerError, match="timeout after 0.2s"):
                with _yaml_transaction(tmp_pm_path, "tasks.yaml", timeout=0.2):
                    pass
        finally:
            external.release()

    def test_atomic_write_no_partial_on_failure(self, tmp_pm_path, monkeypatch):
        """If atomic write fails mid-way, the original yaml stays intact."""
        # Seed initial state
        add_task(tmp_pm_path, Task(id="TST-001", title="orig", phase="phase-1"))
        original_path = tmp_pm_path / "tasks.yaml"
        original_bytes = original_path.read_bytes()

        # Force os.replace to fail (simulating crash during atomic rename)
        from pm_server import utils as _utils

        original_replace = _utils.os.replace

        def boom(src, dst):
            raise OSError("disk full")

        monkeypatch.setattr(_utils.os, "replace", boom)

        with pytest.raises(OSError, match="disk full"):
            add_task(tmp_pm_path, Task(id="TST-002", title="should-fail", phase="phase-1"))

        # Restore replace and verify the original is untouched (atomicity)
        monkeypatch.setattr(_utils.os, "replace", original_replace)
        assert original_path.read_bytes() == original_bytes


class TestBuiltinTemplatesDirStatus:
    """PMSERV-068 — diagnostic helper for the BUILTIN_TEMPLATES_DIR stale-cache
    pattern documented in the 2026-05-08 incident.
    """

    def test_returns_dict_with_expected_keys(self):
        from pm_server.storage import get_builtin_templates_dir_status

        status = get_builtin_templates_dir_status()
        assert set(status.keys()) == {"path", "exists", "template_count", "stale"}

    def test_healthy_install_reports_existing_dir(self):
        """In a normal install, the builtin templates dir exists with >= 1 yaml."""
        from pm_server.storage import get_builtin_templates_dir_status

        status = get_builtin_templates_dir_status()
        assert status["exists"] is True
        assert status["stale"] is False
        assert status["template_count"] >= 1, (
            "src/pm_server/templates/workflows/ must ship at least one builtin"
        )
        # Path should resolve under pm_server/templates/workflows
        assert status["path"].endswith("workflows")

    def test_stale_when_dir_missing(self, tmp_path, monkeypatch):
        """Simulate the 2026-05-08 incident: BUILTIN_TEMPLATES_DIR points at
        a path that no longer exists on disk (e.g. wheel uninstalled after
        import). Helper must surface ``stale=True`` instead of silently
        returning template_count=0 with no signal.
        """
        from pm_server import storage as _storage

        vanished = tmp_path / "uninstalled" / "templates" / "workflows"
        monkeypatch.setattr(_storage, "BUILTIN_TEMPLATES_DIR", vanished)

        status = _storage.get_builtin_templates_dir_status()
        assert status["exists"] is False
        assert status["stale"] is True
        assert status["template_count"] == 0
        assert str(vanished) in status["path"]
