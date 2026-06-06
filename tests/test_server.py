"""Tests for MCP server tools."""

from unittest.mock import patch

import pytest

from pm_server.server import (
    pm_add_decision,
    pm_add_issue,
    pm_add_task,
    pm_blockers,
    pm_cleanup,
    pm_dashboard,
    pm_discover,
    pm_init,
    pm_list,
    pm_log,
    pm_next,
    pm_remember,
    pm_risks,
    pm_status,
    pm_tasks,
    pm_update_claudemd,
    pm_update_rules,
    pm_update_task,
    pm_velocity,
    pm_workflow_templates,
)
from pm_server.storage import (
    _save_project,
    _save_tasks,
    init_pm_directory,
)


@pytest.fixture
def initialized_project(tmp_path, sample_project, sample_tasks):
    """Create a fully initialized project with data."""
    pm_path = init_pm_directory(tmp_path)
    _save_project(pm_path, sample_project)
    _save_tasks(pm_path, sample_tasks)
    return tmp_path


class TestPmInit:
    def test_init_creates_pm_dir(self, tmp_path):
        result = pm_init(project_path=str(tmp_path))
        assert result["status"] == "initialized"
        assert (tmp_path / ".pm").is_dir()
        assert (tmp_path / ".pm" / "project.yaml").exists()

    def test_init_with_custom_name(self, tmp_path):
        result = pm_init(project_path=str(tmp_path), project_name="custom")
        assert result["project"]["name"] == "custom"


class TestPmStatus:
    def test_returns_status(self, initialized_project):
        result = pm_status(project_path=str(initialized_project))
        assert result["project"]["name"] == "testproj"
        assert result["tasks"]["total"] == 4
        assert result["tasks"]["done"] == 1
        assert result["tasks"]["blocked"] == 1

    def test_phase_progress(self, initialized_project):
        result = pm_status(project_path=str(initialized_project))
        phases = result["phases"]
        phase0 = next(p for p in phases if p["id"] == "phase-0")
        assert phase0["progress"] == "1/1"
        assert phase0["progress_pct"] == 100


class TestPmTasks:
    def test_all_tasks(self, initialized_project):
        result = pm_tasks(project_path=str(initialized_project))
        assert len(result) == 4

    def test_filter_by_status(self, initialized_project):
        result = pm_tasks(project_path=str(initialized_project), status="todo")
        assert len(result) == 2

    def test_filter_by_priority(self, initialized_project):
        result = pm_tasks(project_path=str(initialized_project), priority="P0")
        assert len(result) == 2

    def test_filter_by_tag(self, initialized_project):
        result = pm_tasks(project_path=str(initialized_project), tag="core")
        assert len(result) == 1
        assert result[0]["id"] == "TEST-002"


class TestPmAddTask:
    def test_add_task(self, initialized_project):
        result = pm_add_task(
            title="New feature",
            phase="phase-1",
            priority="P0",
            project_path=str(initialized_project),
            tags=["feature"],
        )
        assert result["status"] == "created"
        assert result["task"]["priority"] == "P0"

        # Verify persisted
        tasks = pm_tasks(project_path=str(initialized_project))
        assert len(tasks) == 5


class TestPmUpdateTask:
    def test_update_status(self, initialized_project):
        result = pm_update_task(
            task_id="TEST-002",
            status="in_progress",
            project_path=str(initialized_project),
        )
        assert result["status"] == "updated"
        assert result["task"]["status"] == "in_progress"

    def test_update_nonexistent(self, initialized_project):
        with pytest.raises(Exception):
            pm_update_task(
                task_id="NOPE-999",
                status="done",
                project_path=str(initialized_project),
            )


class TestPmNext:
    def test_recommends_actionable(self, initialized_project):
        result = pm_next(project_path=str(initialized_project))
        # TEST-002 is todo with no deps — should be first
        # TEST-003 depends on TEST-002 (not done) — should NOT appear
        ids = [t["id"] for t in result]
        assert "TEST-002" in ids
        assert "TEST-003" not in ids

    def test_respects_count(self, initialized_project):
        result = pm_next(project_path=str(initialized_project), count=1)
        assert len(result) <= 1


class TestPmBlockers:
    def test_lists_blocked(self, initialized_project):
        result = pm_blockers(project_path=str(initialized_project))
        assert len(result) == 1
        assert result[0]["id"] == "TEST-004"
        assert "days_blocked" in result[0]


class TestPmLog:
    def test_add_log(self, initialized_project):
        result = pm_log(
            entry="Completed setup",
            category="progress",
            project_path=str(initialized_project),
        )
        assert result["status"] == "logged"
        assert result["entries_today"] == 1


class TestPmAddDecision:
    def test_add_decision(self, initialized_project):
        result = pm_add_decision(
            title="Use SQLite",
            context="Need faster queries",
            decision="Switch to SQLite",
            consequences_positive=["Faster"],
            consequences_negative=["Binary format"],
            project_path=str(initialized_project),
        )
        assert result["status"] == "recorded"
        assert result["decision_id"] == "ADR-001"


class TestPmList:
    def test_list_with_registered(self, initialized_project, tmp_path):
        from pm_server.storage import register_project

        # Create a temp registry
        registry_dir = tmp_path / "reg"
        registry_dir.mkdir()
        register_project(initialized_project, "testproj", registry_dir)

        # pm_list uses the global registry; we patch it
        with patch("pm_server.server.load_registry") as mock_reg:
            from pm_server.models import Registry, RegistryEntry

            mock_reg.return_value = Registry(
                projects=[
                    RegistryEntry(
                        path=str(initialized_project),
                        name="testproj",
                    )
                ]
            )
            result = pm_list()
            assert len(result) == 1
            assert result[0]["name"] == "testproj"
            assert result[0]["tasks_total"] == 4


class TestPmInitIdempotent:
    def test_init_does_not_overwrite_existing(self, tmp_path):
        # First init
        pm_init(project_path=str(tmp_path), project_name="original")
        # Second init should not overwrite
        result = pm_init(project_path=str(tmp_path), project_name="overwritten")
        assert result["project"]["name"] == "original"


class TestPmDiscover:
    def test_discover_finds_projects(self, tmp_path):
        # Create two projects with .pm/
        for name in ["proj-a", "proj-b"]:
            pm = tmp_path / name / ".pm"
            pm.mkdir(parents=True)
            (pm / "project.yaml").write_text(f"name: {name}\n")
        result = pm_discover(scan_path=str(tmp_path))
        assert result["found"] == 2

    def test_discover_empty(self, tmp_path):
        result = pm_discover(scan_path=str(tmp_path))
        assert result["found"] == 0
        # PMSERV-089: warnings key is always present, empty when nothing skipped.
        assert result["warnings"] == []

    def test_discover_emits_depth_cap_warning(self, tmp_path):
        """PMSERV-089 (WF-026 FINDING-H): a project deeper than the depth cap
        must not be dropped silently — pm_discover emits a structured warning
        even though it finds nothing to register.
        """
        pm = tmp_path / "a" / "b" / "c" / "d" / "e" / "f" / "deep" / ".pm"
        pm.mkdir(parents=True)
        (pm / "project.yaml").write_text("name: deep\n")

        result = pm_discover(scan_path=str(tmp_path))
        assert result["found"] == 0
        codes = {w["code"] for w in result["warnings"]}
        assert "discover_depth_capped" in codes
        warning = next(w for w in result["warnings"] if w["code"] == "discover_depth_capped")
        assert warning["level"] == "info"
        assert "remediation" in warning

    def test_discover_no_warning_when_within_cap(self, tmp_path):
        pm = tmp_path / "shallow" / ".pm"
        pm.mkdir(parents=True)
        (pm / "project.yaml").write_text("name: shallow\n")

        result = pm_discover(scan_path=str(tmp_path))
        assert result["found"] == 1
        assert result["warnings"] == []

    def test_discover_batches_save_into_one_call(self, tmp_path, monkeypatch):
        """PMSERV-066: ``pm_discover`` must commit N new entries with a
        single ``_save_registry`` call, not N per-entry saves.

        Before the fix the implementation called ``register_project`` in a
        loop, each acquiring its own ``_yaml_transaction(GLOBAL_PM_DIR,
        "registry")`` and writing ``registry.yaml`` once per project (N
        filelock acquires + N atomic writes). After the fix the entire diff
        is batched into one transaction with exactly one write.
        """
        from pm_server import server as _server_mod

        for name in ["p1", "p2", "p3"]:
            pm = tmp_path / name / ".pm"
            pm.mkdir(parents=True)
            (pm / "project.yaml").write_text(f"name: {name}\n")

        call_count = {"n": 0}
        real_save = _server_mod._save_registry

        def counting(*args, **kwargs):
            call_count["n"] += 1
            return real_save(*args, **kwargs)

        monkeypatch.setattr(_server_mod, "_save_registry", counting)

        result = pm_discover(scan_path=str(tmp_path))
        assert result["found"] == 3
        assert result["newly_registered"] == 3
        assert call_count["n"] == 1, (
            f"Expected 1 batched _save_registry call, got {call_count['n']}"
        )

    def test_discover_loads_registry_inside_lock(self, tmp_path, monkeypatch):
        """PMSERV-066: ``load_registry`` must occur inside the registry
        ``_yaml_transaction``, not in a lock-free pre-snapshot.

        The pre-fix implementation called ``load_registry()`` lock-free,
        used the snapshot to pick "new" entries, then took N separate locks
        in the loop. Another process could register a project between the
        snapshot and any of those per-call locks, producing a stale "is
        this new?" decision. Locking around the load closes that window.
        """
        from contextlib import contextmanager

        from pm_server import server as _server_mod

        pm = tmp_path / "p1" / ".pm"
        pm.mkdir(parents=True)
        (pm / "project.yaml").write_text("name: p1\n")

        events: list[str] = []
        real_load = _server_mod.load_registry
        real_tx = _server_mod._yaml_transaction

        def tracking_load(*args, **kwargs):
            events.append("load")
            return real_load(*args, **kwargs)

        @contextmanager
        def tracking_tx(*args, **kwargs):
            events.append("lock_enter")
            with real_tx(*args, **kwargs):
                yield
            events.append("lock_exit")

        monkeypatch.setattr(_server_mod, "load_registry", tracking_load)
        monkeypatch.setattr(_server_mod, "_yaml_transaction", tracking_tx)

        pm_discover(scan_path=str(tmp_path))

        assert events.count("load") == 1, events
        assert "lock_enter" in events, events
        load_idx = events.index("load")
        lock_idx = events.index("lock_enter")
        assert lock_idx < load_idx, f"load_registry must happen inside the lock; got {events}"

    def test_discover_skips_save_when_all_known(self, tmp_path, monkeypatch):
        """PMSERV-066: a discover pass where every found project is already
        registered must not write ``registry.yaml`` at all.

        With the batched implementation, the lock is still acquired (so the
        load is consistent), but ``_save_registry`` is only called when
        ``newly_registered`` is non-empty. This guards against churning the
        atomic-write path on idempotent re-discoveries.
        """
        from pm_server import server as _server_mod

        # First pass: register one project
        pm = tmp_path / "p1" / ".pm"
        pm.mkdir(parents=True)
        (pm / "project.yaml").write_text("name: p1\n")
        first = pm_discover(scan_path=str(tmp_path))
        assert first["newly_registered"] == 1

        # Second pass: same scan, count _save_registry calls — must be 0
        call_count = {"n": 0}
        real_save = _server_mod._save_registry

        def counting(*args, **kwargs):
            call_count["n"] += 1
            return real_save(*args, **kwargs)

        monkeypatch.setattr(_server_mod, "_save_registry", counting)

        second = pm_discover(scan_path=str(tmp_path))
        assert second["found"] == 1
        assert second["newly_registered"] == 0
        assert call_count["n"] == 0, (
            f"Idempotent re-discover should not _save_registry, got {call_count['n']} call(s)"
        )


class TestPmCleanup:
    def test_cleanup_removes_invalid(self, tmp_path, initialized_project):
        from pm_server.models import Registry, RegistryEntry

        with (
            patch("pm_server.server.load_registry") as mock_reg,
            patch("pm_server.server._save_registry"),
        ):
            mock_reg.return_value = Registry(
                projects=[
                    RegistryEntry(path=str(initialized_project), name="valid"),
                    RegistryEntry(path="/nonexistent/path", name="invalid"),
                ]
            )
            result = pm_cleanup()
            assert result["valid"] == 1
            assert result["removed"] == 1

    def test_cleanup_detects_orphan_files(self, initialized_project):
        """pm_cleanup reports orphan project files in global ~/.pm/."""
        import pm_server.storage

        global_pm = pm_server.storage.GLOBAL_PM_DIR
        (global_pm / "tasks.yaml").write_text("tasks: []\n")
        (global_pm / "decisions.yaml").write_text("decisions: []\n")

        from pm_server.models import Registry, RegistryEntry

        with (
            patch("pm_server.server.load_registry") as mock_reg,
            patch("pm_server.server._save_registry"),
        ):
            mock_reg.return_value = Registry(
                projects=[
                    RegistryEntry(path=str(initialized_project), name="valid"),
                ]
            )
            result = pm_cleanup()
            assert "tasks.yaml" in result["orphan_files_in_global"]
            assert "decisions.yaml" in result["orphan_files_in_global"]

    def test_cleanup_no_orphan_files(self, initialized_project):
        """pm_cleanup reports empty list when no orphan files exist."""
        from pm_server.models import Registry, RegistryEntry

        with (
            patch("pm_server.server.load_registry") as mock_reg,
            patch("pm_server.server._save_registry"),
        ):
            mock_reg.return_value = Registry(
                projects=[
                    RegistryEntry(path=str(initialized_project), name="valid"),
                ]
            )
            result = pm_cleanup()
            assert result["orphan_files_in_global"] == []

    def test_cleanup_loads_registry_inside_lock(self, monkeypatch, initialized_project):
        """PMSERV-069: pm_cleanup must load the registry INSIDE the registry
        ``_yaml_transaction`` so a concurrently-registered project is not lost
        by overwriting with a stale valid-snapshot (the TOCTOU class PMSERV-066
        closed for pm_discover)."""
        from contextlib import contextmanager

        from pm_server import server as _server_mod
        from pm_server.models import Registry, RegistryEntry

        events: list[str] = []
        real_tx = _server_mod._yaml_transaction

        def tracking_load(*args, **kwargs):
            events.append("load")
            return Registry(
                projects=[
                    RegistryEntry(path=str(initialized_project), name="valid"),
                    RegistryEntry(path="/nonexistent/path", name="invalid"),
                ]
            )

        @contextmanager
        def tracking_tx(*args, **kwargs):
            events.append("lock_enter")
            with real_tx(*args, **kwargs):
                yield
            events.append("lock_exit")

        monkeypatch.setattr(_server_mod, "load_registry", tracking_load)
        monkeypatch.setattr(_server_mod, "_yaml_transaction", tracking_tx)
        monkeypatch.setattr(_server_mod, "_save_registry", lambda *a, **k: None)

        result = pm_cleanup()

        assert result["removed"] == 1
        assert events.count("load") == 1, events
        assert "lock_enter" in events, events
        # load (and the save it gates) must sit inside the registry lock.
        assert events.index("lock_enter") < events.index("load"), (
            f"load_registry must happen inside the lock; got {events}"
        )
        assert events.index("lock_exit") > events.index("load"), events


class TestPmRisks:
    def test_risks_returns_list(self, initialized_project):
        result = pm_risks(project_path=str(initialized_project))
        assert isinstance(result, list)
        # sample_tasks has a blocked task → should detect it
        blocked_risks = [r for r in result if r.get("type") == "blocked_task"]
        assert len(blocked_risks) >= 1


class TestPmVelocity:
    def test_velocity_returns_dict(self, initialized_project):
        result = pm_velocity(project_path=str(initialized_project))
        assert "average" in result
        assert "trend" in result
        assert "weeks" in result


class TestPmUpdateClaudemd:
    def test_pm_update_claudemd_creates_new(self, initialized_project):
        """pm_update_claudemd creates CLAUDE.md when it doesn't exist."""
        # Remove CLAUDE.md if pm_init created it
        claude_md = initialized_project / "CLAUDE.md"
        if claude_md.exists():
            claude_md.unlink()
        result = pm_update_claudemd(project_path=str(initialized_project))
        assert result["status"] == "updated"
        assert "created" in result["message"]
        assert claude_md.exists()

    def test_pm_update_claudemd_updates_existing(self, initialized_project):
        """pm_update_claudemd updates existing CLAUDE.md."""
        # 初回
        pm_update_claudemd(project_path=str(initialized_project))
        # 2回目
        result = pm_update_claudemd(project_path=str(initialized_project))
        assert result["status"] == "updated"
        assert result["after"]["up_to_date"] is True

    def test_pm_update_claudemd_returns_legacy_dict_shape(self, initialized_project):
        """pm_update_claudemd returns the v0.4.x legacy dict shape.

        Regression guard for PMSERV-044: when this MCP tool is refactored
        to delegate to ``inject_pm_rules(target='claude-code')``, the
        response dict shape MUST be preserved exactly. Locks every
        documented top-level field and the nested ``before``/``after``
        keys (cross-check R3).
        """
        result = pm_update_claudemd(project_path=str(initialized_project))

        # Top-level keys (5 fields, exact set)
        assert set(result.keys()) == {
            "status",
            "message",
            "template_version",
            "before",
            "after",
        }
        assert result["status"] == "updated"
        assert isinstance(result["message"], str)
        assert isinstance(result["template_version"], int)

        # Nested before/after dict keys (5 each, exact set)
        for snapshot_key in ("before", "after"):
            snapshot = result[snapshot_key]
            assert set(snapshot.keys()) == {
                "exists",
                "has_pm_section",
                "version",
                "up_to_date",
                "other_rule_sections",
            }
            assert isinstance(snapshot["exists"], bool)
            assert isinstance(snapshot["has_pm_section"], bool)
            assert isinstance(snapshot["other_rule_sections"], list)


class TestPmUpdateRules:
    """pm_update_rules MCP tool (PMSERV-044)."""

    def test_default_target_auto_creates_claude_md(
        self, initialized_project, monkeypatch, tmp_path
    ):
        # Force fallback path: no claude binary, no codex config, no env
        monkeypatch.setattr("pm_server.rules.shutil.which", lambda _name: None)
        monkeypatch.setenv("HOME", str(tmp_path / "fake_home"))
        (tmp_path / "fake_home").mkdir(exist_ok=True)
        monkeypatch.delenv("CLAUDECODE", raising=False)

        result = pm_update_rules(project_path=str(initialized_project))

        assert result["detection_source"] == "fallback"
        assert result["detected_hosts"] == ["claude-code"]
        # Fallback MUST surface a warning (cross-check A3)
        assert len(result["warnings"]) == 1
        assert result["warnings"][0]["code"] == "host_detection_fallback"
        assert "remediation" in result["warnings"][0]

    def test_target_codex_creates_agents_md(self, initialized_project):
        result = pm_update_rules(project_path=str(initialized_project), target="codex")

        assert (initialized_project / "AGENTS.md").exists()
        assert "AGENTS.md" in result["created"] or "AGENTS.md" in result["updated"]
        assert result["detection_source"] == "explicit"
        # No warnings for explicit target
        assert result["warnings"] == []

    def test_target_all_processes_both_hosts(self, initialized_project):
        result = pm_update_rules(project_path=str(initialized_project), target="all")

        assert (initialized_project / "CLAUDE.md").exists()
        assert (initialized_project / "AGENTS.md").exists()
        assert {r["host"] for r in result["results"]} == {"claude-code", "codex"}

    def test_dry_run_does_not_write(self, initialized_project):
        # Remove any auto-created CLAUDE.md from pm_init
        claude_md = initialized_project / "CLAUDE.md"
        if claude_md.exists():
            claude_md.unlink()

        result = pm_update_rules(project_path=str(initialized_project), target="all", dry_run=True)

        assert result["is_dry_run"] is True
        assert all(r["is_dry_run"] for r in result["results"])
        assert not claude_md.exists()
        assert not (initialized_project / "AGENTS.md").exists()

    def test_response_dict_has_required_top_level_keys(self, initialized_project):
        result = pm_update_rules(project_path=str(initialized_project), target="claude-code")

        required = {
            "overall_status",
            "detected_hosts",
            "detection_source",
            "created",
            "updated",
            "is_dry_run",
            "results",
            "warnings",
        }
        assert set(result.keys()) == required

    def test_per_result_has_required_fields(self, initialized_project):
        result = pm_update_rules(project_path=str(initialized_project), target="claude-code")

        per_result_keys = {
            "target_file",
            "host",
            "status",
            "message",
            "backup_path",
            "is_dry_run",
        }
        assert set(result["results"][0].keys()) == per_result_keys

    def test_unknown_target_raises_value_error(self, initialized_project):
        with pytest.raises(ValueError, match="unknown target"):
            pm_update_rules(project_path=str(initialized_project), target="bogus")


class TestPmStatusRulesKey:
    """pm_status.rules key (PMSERV-044, additive — does not break v0.4.x)."""

    def test_pm_status_includes_rules_key(self, initialized_project):
        status = pm_status(project_path=str(initialized_project))

        assert "rules" in status
        assert "claude_code" in status["rules"]
        assert "codex" in status["rules"]

    def test_pm_status_claudemd_key_unchanged(self, initialized_project):
        """Legacy claudemd key MUST remain (v0.4.x compat regression guard)."""
        status = pm_status(project_path=str(initialized_project))

        assert "claudemd" in status
        # Same shape as v0.4.x get_claudemd_status output
        assert set(status["claudemd"].keys()) == {
            "exists",
            "has_pm_section",
            "version",
            "up_to_date",
            "other_rule_sections",
        }

    def test_per_host_rules_status_has_get_claudemd_status_shape(self, initialized_project):
        status = pm_status(project_path=str(initialized_project))

        for host_status in status["rules"].values():
            assert set(host_status.keys()) == {
                "exists",
                "has_pm_section",
                "version",
                "up_to_date",
                "other_rule_sections",
            }


class TestPmDashboard:
    def test_html_dashboard(self, initialized_project):
        html = pm_dashboard(project_path=str(initialized_project), format="html")
        assert "<!DOCTYPE html>" in html

    def test_text_dashboard(self, initialized_project):
        text = pm_dashboard(project_path=str(initialized_project), format="text")
        assert "testproj" in text.lower() or "Test Project" in text


class TestPmAddIssue:
    def test_add_issue_basic(self, initialized_project):
        """pm_add_issue creates a child task linked to the parent."""
        result = pm_add_issue(
            parent_id="TEST-002",
            title="Fix validation bug",
            project_path=str(initialized_project),
        )
        assert result["status"] == "created"
        assert result["task"]["parent_id"] == "TEST-002"
        assert result["task"]["phase"] == "phase-1"  # inherited from parent
        assert result["warnings"] == []

    def test_add_issue_inherits_phase(self, initialized_project):
        """Child task inherits phase from parent automatically."""
        result = pm_add_issue(
            parent_id="TEST-001",
            title="Phase-0 issue",
            project_path=str(initialized_project),
        )
        assert result["task"]["phase"] == "phase-0"

    def test_add_issue_defect_reverts_done_parent_to_review(self, initialized_project):
        """severity=defect (default) on a done parent moves it to 'review' and warns."""
        result = pm_add_issue(
            parent_id="TEST-001",  # status: done
            title="Found a problem",
            project_path=str(initialized_project),
        )
        # Structured warnings
        assert len(result["warnings"]) == 1
        warning = result["warnings"][0]
        assert warning["code"] == "parent_reverted"
        assert warning["level"] == "info"
        assert "TEST-001" in warning["message"]
        assert "remediation" in warning

        # Legacy fields still populated (deprecated, additive)
        assert result["parent_reverted"] is True
        assert "review" in result["message"]

        # Parent status actually changed
        tasks = pm_tasks(project_path=str(initialized_project))
        parent = next(t for t in tasks if t["id"] == "TEST-001")
        assert parent["status"] == "review"

    def test_add_issue_enhancement_keeps_parent_done(self, initialized_project):
        """severity=enhancement on a done parent must NOT revert status."""
        result = pm_add_issue(
            parent_id="TEST-001",  # status: done
            title="Future improvement",
            severity="enhancement",
            project_path=str(initialized_project),
        )
        assert result["warnings"] == []
        assert "parent_reverted" not in result
        assert "message" not in result

        tasks = pm_tasks(project_path=str(initialized_project))
        parent = next(t for t in tasks if t["id"] == "TEST-001")
        assert parent["status"] == "done"

    def test_add_issue_severity_persisted_on_task(self, initialized_project):
        """severity is stored on the child Task so later queries can distinguish."""
        result = pm_add_issue(
            parent_id="TEST-002",
            title="Enhancement idea",
            severity="enhancement",
            project_path=str(initialized_project),
        )
        child_id = result["task"]["id"]
        tasks = pm_tasks(project_path=str(initialized_project))
        child = next(t for t in tasks if t["id"] == child_id)
        assert child.get("severity") == "enhancement"

    def test_add_issue_invalid_severity_raises(self, initialized_project):
        """Unknown severity values are rejected with a helpful message."""
        with pytest.raises(Exception) as excinfo:
            pm_add_issue(
                parent_id="TEST-002",
                title="Bad severity",
                severity="critical",  # not a valid IssueSeverity
                project_path=str(initialized_project),
            )
        assert "severity" in str(excinfo.value).lower()

    def test_add_issue_no_revert_when_parent_not_done(self, initialized_project):
        """When parent is not 'done', no automatic status change."""
        result = pm_add_issue(
            parent_id="TEST-002",  # status: todo
            title="New issue",
            project_path=str(initialized_project),
        )
        assert result["warnings"] == []
        assert "parent_reverted" not in result

    def test_add_issue_nonexistent_parent(self, initialized_project):
        """Adding an issue to a nonexistent parent raises an error."""
        with pytest.raises(Exception):
            pm_add_issue(
                parent_id="NOPE-999",
                title="Orphan issue",
                project_path=str(initialized_project),
            )

    def test_add_issue_with_priority_and_tags(self, initialized_project):
        """pm_add_issue accepts priority and tags."""
        result = pm_add_issue(
            parent_id="TEST-002",
            title="Critical fix",
            priority="P0",
            tags=["bugfix", "urgent"],
            project_path=str(initialized_project),
        )
        assert result["task"]["priority"] == "P0"
        assert "bugfix" in result["task"]["tags"]

    def test_add_issue_defect_writes_tasks_atomically(self, initialized_project, monkeypatch):
        """PMSERV-065 / ADR-012: defect issue creation on a done parent must
        perform exactly one ``_save_tasks`` write under a single
        ``_yaml_transaction``. Pre-fix the compound op used ``add_task`` +
        ``update_task`` which wrote twice, leaving a TOCTOU window between
        them. A regression to that shape would surface here as 2 writes.
        """
        from pm_server import server as _server_mod

        call_count = {"n": 0}
        real_save = _server_mod._save_tasks

        def counting_save(pm_path, tasks):
            call_count["n"] += 1
            real_save(pm_path, tasks)

        monkeypatch.setattr(_server_mod, "_save_tasks", counting_save)

        result = pm_add_issue(
            parent_id="TEST-001",  # status: done — triggers compound parent revert
            title="Atomic compound write regression",
            project_path=str(initialized_project),
        )
        assert result["parent_reverted"] is True, "fixture invariant: TEST-001 is done"
        assert call_count["n"] == 1, (
            f"compound op must be one atomic write (ADR-012), got {call_count['n']}"
        )

    def test_add_issue_next_number_from_in_lock_list(self, initialized_project, monkeypatch):
        """PMSERV-065: pm_add_issue must compute the next task number from the
        in-lock fresh task list, not via a separate ``next_task_number(pm_path)``
        call that re-loads outside the lock (race window R3 in the spec).
        """
        from pm_server import server as _server_mod

        call_count = {"n": 0}
        real = _server_mod.next_task_number

        def counting(pm_path):
            call_count["n"] += 1
            return real(pm_path)

        monkeypatch.setattr(_server_mod, "next_task_number", counting)

        pm_add_issue(
            parent_id="TEST-002",
            title="Number from in-lock list",
            project_path=str(initialized_project),
        )
        assert call_count["n"] == 0, (
            f"next_task_number(pm_path) re-load was expected to be zero "
            f"(ADR-012 — compute from in-lock list), got {call_count['n']}"
        )


class TestPmTasksParentFilter:
    def test_filter_by_parent_id(self, initialized_project):
        """pm_tasks(parent_id=...) returns only child issues."""
        # Add two issues to TEST-002
        pm_add_issue(
            parent_id="TEST-002",
            title="Issue A",
            project_path=str(initialized_project),
        )
        pm_add_issue(
            parent_id="TEST-002",
            title="Issue B",
            project_path=str(initialized_project),
        )
        # Add one issue to TEST-001
        pm_add_issue(
            parent_id="TEST-001",
            title="Issue C",
            project_path=str(initialized_project),
        )

        children = pm_tasks(
            project_path=str(initialized_project),
            parent_id="TEST-002",
        )
        assert len(children) == 2
        assert all(c["parent_id"] == "TEST-002" for c in children)

    def test_filter_parent_id_no_children(self, initialized_project):
        """pm_tasks with parent_id returns empty list when no children."""
        children = pm_tasks(
            project_path=str(initialized_project),
            parent_id="TEST-003",
        )
        assert children == []


class TestPmUpdateTaskIssueCompletion:
    def test_all_issues_resolved_notification(self, initialized_project):
        """When all child issues are done, result includes completion hint."""
        # Add two issues
        pm_add_issue(
            parent_id="TEST-002",
            title="Issue 1",
            project_path=str(initialized_project),
        )
        pm_add_issue(
            parent_id="TEST-002",
            title="Issue 2",
            project_path=str(initialized_project),
        )

        # Complete first child
        children = pm_tasks(
            project_path=str(initialized_project),
            parent_id="TEST-002",
        )
        pm_update_task(
            task_id=children[0]["id"],
            status="done",
            project_path=str(initialized_project),
        )

        # Complete second child → should trigger notification
        result = pm_update_task(
            task_id=children[1]["id"],
            status="done",
            project_path=str(initialized_project),
        )
        assert result["all_issues_resolved"] is True
        assert result["parent_id"] == "TEST-002"

    def test_no_notification_when_issues_remain(self, initialized_project):
        """No notification when some child issues are still open."""
        pm_add_issue(
            parent_id="TEST-002",
            title="Issue 1",
            project_path=str(initialized_project),
        )
        pm_add_issue(
            parent_id="TEST-002",
            title="Issue 2",
            project_path=str(initialized_project),
        )

        children = pm_tasks(
            project_path=str(initialized_project),
            parent_id="TEST-002",
        )
        # Complete only one
        result = pm_update_task(
            task_id=children[0]["id"],
            status="done",
            project_path=str(initialized_project),
        )
        assert "all_issues_resolved" not in result

    def test_no_notification_for_top_level_task(self, initialized_project):
        """No notification when completing a task with no parent."""
        result = pm_update_task(
            task_id="TEST-002",
            status="done",
            project_path=str(initialized_project),
        )
        assert "all_issues_resolved" not in result


class TestPmStatusExtended:
    """Tests for active_tasks, hooks, and next_pm_actions in pm_status."""

    def test_active_tasks_included(self, initialized_project):
        # Set a task to in_progress first
        pm_update_task(
            task_id="TEST-002",
            status="in_progress",
            project_path=str(initialized_project),
        )
        result = pm_status(project_path=str(initialized_project))
        assert "active_tasks" in result
        assert any(t["id"] == "TEST-002" for t in result["active_tasks"])

    def test_active_tasks_empty(self, initialized_project):
        result = pm_status(project_path=str(initialized_project))
        assert result["active_tasks"] == []

    def test_hooks_status_included(self, initialized_project):
        result = pm_status(project_path=str(initialized_project))
        assert "hooks" in result
        assert "installed" in result["hooks"]

    def test_next_pm_actions_with_active(self, initialized_project):
        pm_update_task(
            task_id="TEST-002",
            status="in_progress",
            project_path=str(initialized_project),
        )
        result = pm_status(project_path=str(initialized_project))
        actions = result["next_pm_actions"]
        assert any("pm_update_task" in a for a in actions)
        assert any("pm_remember" in a for a in actions)

    def test_next_pm_actions_without_active(self, initialized_project):
        result = pm_status(project_path=str(initialized_project))
        actions = result["next_pm_actions"]
        assert any("pm_log" in a for a in actions)
        assert any("pm_session_summary" in a for a in actions)


class TestPmLogAutoLink:
    """Tests for pm_log task_id auto-inference."""

    def test_auto_links_single_active_task(self, initialized_project):
        pm_update_task(
            task_id="TEST-002",
            status="in_progress",
            project_path=str(initialized_project),
        )
        result = pm_log(
            entry="Completed feature",
            project_path=str(initialized_project),
        )
        assert result["auto_linked_task"] == "TEST-002"

    def test_no_auto_link_without_active(self, initialized_project):
        result = pm_log(
            entry="General note",
            project_path=str(initialized_project),
        )
        assert "auto_linked_task" not in result

    def test_explicit_task_id_used(self, initialized_project):
        pm_update_task(
            task_id="TEST-002",
            status="in_progress",
            project_path=str(initialized_project),
        )
        result = pm_log(
            entry="Specific task log",
            task_id="TEST-001",
            project_path=str(initialized_project),
        )
        # explicit task_id should override auto-link
        assert "auto_linked_task" not in result

    def test_no_auto_link_multiple_active(self, initialized_project):
        """No auto-link when multiple tasks are in_progress."""
        pm_update_task(
            task_id="TEST-002",
            status="in_progress",
            project_path=str(initialized_project),
        )
        pm_update_task(
            task_id="TEST-003",
            status="in_progress",
            project_path=str(initialized_project),
        )
        result = pm_log(
            entry="Ambiguous",
            project_path=str(initialized_project),
        )
        assert "auto_linked_task" not in result


class TestPmRememberAutoLink:
    """Tests for pm_remember task_id auto-inference."""

    def test_auto_links_single_active_task(self, initialized_project):
        pm_update_task(
            task_id="TEST-002",
            status="in_progress",
            project_path=str(initialized_project),
        )
        result = pm_remember(
            content="Important finding",
            project_path=str(initialized_project),
        )
        assert result["auto_linked_task"] == "TEST-002"

    def test_no_auto_link_when_task_id_provided(self, initialized_project):
        pm_update_task(
            task_id="TEST-002",
            status="in_progress",
            project_path=str(initialized_project),
        )
        result = pm_remember(
            content="Linked to specific task",
            task_id="TEST-001",
            project_path=str(initialized_project),
        )
        assert "auto_linked_task" not in result

    def test_no_auto_link_when_decision_id_provided(self, initialized_project):
        pm_update_task(
            task_id="TEST-002",
            status="in_progress",
            project_path=str(initialized_project),
        )
        result = pm_remember(
            content="Decision context",
            decision_id="ADR-001",
            project_path=str(initialized_project),
        )
        assert "auto_linked_task" not in result

    def test_no_auto_link_multiple_active(self, initialized_project):
        pm_update_task(
            task_id="TEST-002",
            status="in_progress",
            project_path=str(initialized_project),
        )
        pm_update_task(
            task_id="TEST-003",
            status="in_progress",
            project_path=str(initialized_project),
        )
        result = pm_remember(
            content="Ambiguous context",
            project_path=str(initialized_project),
        )
        assert "auto_linked_task" not in result


# ─── PMSERV-049: pm_recall multi-session disambiguation ─────────────


class TestPmRecall:
    """pm_recall returns current_session_id, ambiguity_detected, candidates."""

    @pytest.fixture(autouse=True)
    def _setup_project(self, tmp_project, monkeypatch):
        from pm_server.models import Project
        from pm_server.storage import _save_project as _save

        pm_path = tmp_project / ".pm"
        _save(pm_path, Project(name="testproj", display_name="Test"))
        monkeypatch.chdir(tmp_project)

        import pm_server.server

        pm_server.server._memory_stores.clear()
        monkeypatch.delenv("PM_SERVER_RECALL_AMBIGUITY_WINDOW_MIN", raising=False)

    @staticmethod
    def _force_session(monkeypatch, session_id: str):
        import pm_server.server

        monkeypatch.setattr(pm_server.server, "_current_session_id", session_id)

    def test_recall_default_returns_current_session_id(self, monkeypatch):
        self._force_session(monkeypatch, "sess-test-current")
        from pm_server.server import pm_recall

        result = pm_recall()
        assert result["current_session_id"] == "sess-test-current"

    def test_recall_default_with_single_session_no_ambiguity(self, monkeypatch):
        self._force_session(monkeypatch, "sess-A")
        from pm_server.server import pm_recall, pm_session_summary

        pm_session_summary(action="save", summary="Self session work")
        result = pm_recall()
        assert result["ambiguity_detected"] is False
        assert "last_session_candidates" not in result

    def test_recall_default_with_multiple_sessions_recent(self, monkeypatch):
        from pm_server.models import SessionSummary
        from pm_server.server import _get_memory_store, pm_recall

        self._force_session(monkeypatch, "sess-A")
        store = _get_memory_store(None)
        store.save_session_summary(
            SessionSummary(session_id="sess-A", summary="A's work", project="testproj")
        )
        store.save_session_summary(
            SessionSummary(session_id="sess-B", summary="B's work", project="testproj")
        )

        result = pm_recall()
        assert result["ambiguity_detected"] is True
        assert "last_session_candidates" in result
        candidate_ids = {c["session_id"] for c in result["last_session_candidates"]}
        assert {"sess-A", "sess-B"}.issubset(candidate_ids)
        for c in result["last_session_candidates"]:
            if c["session_id"] == "sess-A":
                assert c["is_current_session"] is True
            else:
                assert c["is_current_session"] is False

    def test_recall_default_with_old_other_session_outside_window(self, monkeypatch):
        from pm_server.models import SessionSummary
        from pm_server.server import _get_memory_store, pm_recall

        self._force_session(monkeypatch, "sess-A")
        store = _get_memory_store(None)
        store._conn.execute(
            """INSERT INTO session_summaries
               (session_id, summary, goals, tasks_done, decisions, pending,
                project, created_at, updated_at)
               VALUES (?, ?, '', '[]', '[]', '[]', ?,
                       datetime('now', '-2 hours'),
                       datetime('now', '-2 hours'))""",
            ("sess-old", "old work", "testproj"),
        )
        store._conn.commit()
        store.save_session_summary(
            SessionSummary(session_id="sess-A", summary="recent", project="testproj")
        )

        result = pm_recall()
        # Other session is outside window → ambiguity should not fire
        assert result["ambiguity_detected"] is False

    def test_recall_default_backward_compat_last_session_shape(self, monkeypatch):
        self._force_session(monkeypatch, "sess-A")
        from pm_server.server import pm_recall, pm_session_summary

        pm_session_summary(action="save", summary="content", goals="g", pending="p1,p2")
        result = pm_recall()
        # Legacy 5 keys + updated_at = 6 keys exactly
        assert set(result["last_session"].keys()) == {
            "session_id",
            "summary",
            "goals",
            "pending",
            "created_at",
            "updated_at",
        }

    # ─── PMSERV-124 / ADR-028: branch-aware recall (track=) ─────────

    def test_recall_no_track_omits_track_keys(self, monkeypatch):
        """Default (no track) response stays byte-identical to pre-ADR-028."""
        self._force_session(monkeypatch, "sess-A")
        from pm_server.server import pm_recall, pm_session_summary

        pm_session_summary(action="save", summary="work")
        result = pm_recall()
        assert "track" not in result
        assert "track_matched" not in result

    def test_recall_with_track_returns_that_lines_latest(self, monkeypatch):
        from pm_server.models import SessionSummary
        from pm_server.server import _get_memory_store, pm_recall

        self._force_session(monkeypatch, "sess-paper")
        store = _get_memory_store(None)
        store.save_session_summary(
            SessionSummary(
                session_id="sess-main", summary="main work", project="testproj", branch="main"
            )
        )
        store.save_session_summary(
            SessionSummary(
                session_id="sess-paper", summary="paper work", project="testproj", branch="paper"
            )
        )

        result = pm_recall(track="paper")
        assert result["track"] == "paper"
        assert result["track_matched"] is True
        assert result["last_session"]["session_id"] == "sess-paper"
        # track is a top-level key, never inside last_session (canary stays 6).
        assert "track" not in result["last_session"]
        assert set(result["last_session"].keys()) == {
            "session_id",
            "summary",
            "goals",
            "pending",
            "created_at",
            "updated_at",
        }
        # Branch/track scopes away cross-session ambiguity but keeps the key.
        assert result["ambiguity_detected"] is False

    def test_recall_with_unmatched_track_falls_back(self, monkeypatch):
        from pm_server.models import SessionSummary
        from pm_server.server import _get_memory_store, pm_recall

        self._force_session(monkeypatch, "sess-main")
        store = _get_memory_store(None)
        store.save_session_summary(
            SessionSummary(
                session_id="sess-main", summary="main work", project="testproj", branch="main"
            )
        )

        result = pm_recall(track="nonexistent")
        assert result["track"] == "nonexistent"
        assert result["track_matched"] is False
        # Graceful fallback to overall-latest so day-one users still get context.
        assert result["last_session"]["session_id"] == "sess-main"

    def test_recall_with_track_never_invokes_git_detection(self, monkeypatch):
        """RO invariant (ADR-028): pm_recall must not reach branch detection.

        If pm_recall ever called read_git_branch, this monkeypatched explosion
        would surface — proving the read path stays git-free.
        """
        import pm_server.server

        self._force_session(monkeypatch, "sess-A")

        def _boom(*_a, **_k):
            raise AssertionError("pm_recall must never detect git branch")

        monkeypatch.setattr(pm_server.server, "read_git_branch", _boom)
        # Must not raise.
        result = pm_server.server.pm_recall(track="main")
        assert result["track"] == "main"

    def test_save_records_git_branch(self, monkeypatch, tmp_project):
        """pm_session_summary save records the branch from .git/HEAD (text)."""
        from pm_server.server import _get_memory_store, pm_session_summary

        git_dir = tmp_project / ".git"
        git_dir.mkdir()
        (git_dir / "HEAD").write_text("ref: refs/heads/feature/paper\n")

        self._force_session(monkeypatch, "sess-A")
        result = pm_session_summary(action="save", summary="branch work")
        assert result["branch"] == "feature/paper"

        store = _get_memory_store(None)
        summary, matched = store.get_latest_summary_by_branch("feature/paper")
        assert matched is True
        assert summary.session_id == "sess-A"

    # ─── PMSERV-125 / ADR-035: logical track labels (.pm/tracks.yaml) ───

    @staticmethod
    def _write_tracks(tmp_project, body: str):
        (tmp_project / ".pm" / "tracks.yaml").write_text(body, encoding="utf-8")

    @staticmethod
    def _save_on_branch(store, session_id, branch):
        from pm_server.models import SessionSummary

        store.save_session_summary(
            SessionSummary(
                session_id=session_id, summary=f"{branch} work", project="testproj", branch=branch
            )
        )

    def test_track_logical_label_resolves_glob_to_latest(self, monkeypatch, tmp_project):
        from pm_server.server import _get_memory_store, pm_recall

        self._write_tracks(tmp_project, "tracks:\n  論文: [feat/p3-*, research/*]\n")
        self._force_session(monkeypatch, "s-b")
        store = _get_memory_store(None)
        self._save_on_branch(store, "s-main", "main")
        self._save_on_branch(store, "s-a", "feat/p3-a")
        self._save_on_branch(store, "s-b", "research/wave-1")
        # Make feat/p3-a the most recently worked of the 論文 line.
        store._conn.execute(
            "UPDATE session_summaries SET updated_at = datetime('now', '+1 hour')"
            " WHERE session_id = ?",
            ("s-a",),
        )
        store._conn.commit()

        result = pm_recall(track="論文")
        assert result["track"] == "論文"
        assert result["track_matched"] is True
        assert result["track_branch"] == "feat/p3-a"
        assert result["last_session"]["session_id"] == "s-a"
        # main is NOT part of the 論文 line.
        assert result["last_session"]["session_id"] != "s-main"

    def test_track_label_priority_then_raw_branch(self, monkeypatch, tmp_project):
        from pm_server.server import _get_memory_store, pm_recall

        self._write_tracks(tmp_project, "tracks:\n  教材: [edu/*]\n")
        self._force_session(monkeypatch, "s-edu")
        store = _get_memory_store(None)
        self._save_on_branch(store, "s-edu", "edu/intro")
        self._save_on_branch(store, "s-feat", "feat/x")

        # Logical label resolves via glob.
        by_label = pm_recall(track="教材")
        assert by_label["track_matched"] is True
        assert by_label["track_branch"] == "edu/intro"

        # A non-label value is matched as a raw branch (v1 behavior).
        by_raw = pm_recall(track="feat/x")
        assert by_raw["track_matched"] is True
        assert by_raw["last_session"]["session_id"] == "s-feat"

    def test_track_backward_compat_no_mapping_file(self, monkeypatch, tmp_project):
        from pm_server.server import _get_memory_store, pm_recall

        # No tracks.yaml at all → track is a raw branch.
        self._force_session(monkeypatch, "s-main")
        store = _get_memory_store(None)
        self._save_on_branch(store, "s-main", "main")
        result = pm_recall(track="main")
        assert result["track_matched"] is True
        assert result["last_session"]["session_id"] == "s-main"

    def test_track_rename_resistance_across_line(self, monkeypatch, tmp_project):
        """A line spanning an old + renamed branch still returns its latest."""
        from pm_server.server import _get_memory_store, pm_recall

        self._write_tracks(tmp_project, "tracks:\n  論文: [research/*, feat/p3-*]\n")
        self._force_session(monkeypatch, "s-new")
        store = _get_memory_store(None)
        self._save_on_branch(store, "s-old", "research/wave-old")  # earlier branch name
        self._save_on_branch(store, "s-new", "feat/p3-renamed")  # later, renamed
        store._conn.execute(
            "UPDATE session_summaries SET updated_at = datetime('now', '+1 hour')"
            " WHERE session_id = ?",
            ("s-new",),
        )
        store._conn.commit()
        result = pm_recall(track="論文")
        assert result["track_matched"] is True
        assert result["last_session"]["session_id"] == "s-new"

    def test_track_malformed_tracks_yaml_warns_and_falls_back(self, monkeypatch, tmp_project):
        from pm_server.server import _get_memory_store, pm_recall

        self._write_tracks(tmp_project, "tracks: [broken\n  x: {")
        self._force_session(monkeypatch, "s-main")
        store = _get_memory_store(None)
        self._save_on_branch(store, "s-main", "main")
        result = pm_recall(track="main")
        # Degrades to raw-branch matching, and surfaces a structured warning.
        assert result["track_matched"] is True
        assert result["last_session"]["session_id"] == "s-main"
        assert "warnings" in result
        assert result["warnings"][0]["code"] == "tracks_config_invalid"

    def test_recall_with_query_no_ambiguity_field(self, monkeypatch):
        self._force_session(monkeypatch, "sess-A")
        from pm_server.server import pm_recall, pm_remember

        pm_remember(content="searchable content")
        result = pm_recall(query="searchable")
        # Ambiguity is a default-branch concept only
        assert "ambiguity_detected" not in result
        assert "last_session_candidates" not in result

    def test_recall_default_no_ambiguity_omits_candidates_field(self, monkeypatch):
        self._force_session(monkeypatch, "sess-A")
        from pm_server.server import pm_recall, pm_session_summary

        pm_session_summary(action="save", summary="lone session")
        result = pm_recall()
        assert result["ambiguity_detected"] is False
        # Optional field must be absent (not just falsy)
        assert "last_session_candidates" not in result

    def test_recall_cross_project_includes_current_session_id(self, monkeypatch):
        self._force_session(monkeypatch, "sess-cross")
        from pm_server.server import pm_recall, pm_remember

        pm_remember(content="cross test data")
        result = pm_recall(query="cross test", cross_project=True)
        assert result["current_session_id"] == "sess-cross"
        assert result["cross_project"] is True


class TestBranchAwareLensSafety:
    """ADR-028: branch detection must stay off the read-only / Lens surface."""

    def test_server_does_not_import_subprocess(self):
        """We detect the branch by reading .git/HEAD as text — server.py must
        never import subprocess (the git config-exec risk the design avoids)."""
        import pathlib

        import pm_server.server as srv

        source = pathlib.Path(srv.__file__).read_text(encoding="utf-8")
        assert "import subprocess" not in source

    def test_pm_recall_is_read_only_allowlisted(self):
        import pm_server.server as srv

        assert "pm_recall" in srv.RO_ALLOWLIST
        # The branch-detecting mutator stays OUT of the read-only surface.
        assert "pm_session_summary" not in srv.RO_ALLOWLIST


class TestBuiltinTemplatesDirDiagnostics:
    """PMSERV-068 — surface BUILTIN_TEMPLATES_DIR sanity state through MCP tools.

    These tests pair with the inner-helper tests in test_storage.py
    (``TestBuiltinTemplatesDirStatus``). Here we verify the diagnostic
    reaches the two surfaces a Claude session sees: ``pm_status`` and
    ``pm_workflow_templates``.
    """

    def test_pm_status_diagnostics_includes_builtin_templates_dir(self, initialized_project):
        result = pm_status(project_path=str(initialized_project))
        diagnostics = result["diagnostics"]
        assert "builtin_templates_dir" in diagnostics
        btd = diagnostics["builtin_templates_dir"]
        assert set(btd.keys()) == {"path", "exists", "template_count", "stale"}
        # Healthy install in the test runner: dir exists, not stale
        assert btd["exists"] is True
        assert btd["stale"] is False

    def test_pm_workflow_templates_has_warnings_field(self, initialized_project):
        """Healthy state: warnings is present and empty."""
        result = pm_workflow_templates(project_path=str(initialized_project))
        assert "warnings" in result
        assert result["warnings"] == []
        # Pre-existing fields unchanged (backwards compatibility)
        assert "count" in result
        assert "templates" in result

    def test_pm_workflow_templates_warns_on_stale_builtin_dir(
        self, initialized_project, tmp_path, monkeypatch
    ):
        """If BUILTIN_TEMPLATES_DIR no longer resolves on disk (the 2026-05-08
        incident shape), ``pm_workflow_templates`` must emit a structured
        ``builtin_templates_dir_missing`` warning rather than silently
        returning fewer templates.
        """
        from pm_server import storage as _storage

        vanished = tmp_path / "uninstalled" / "templates" / "workflows"
        monkeypatch.setattr(_storage, "BUILTIN_TEMPLATES_DIR", vanished)

        result = pm_workflow_templates(project_path=str(initialized_project))
        warnings = result["warnings"]
        assert len(warnings) == 1
        w = warnings[0]
        assert w["code"] == "builtin_templates_dir_missing"
        assert w["level"] == "warn"
        assert str(vanished) in w["message"]
        assert "remediation" in w
        assert "再起動" in w["remediation"]
