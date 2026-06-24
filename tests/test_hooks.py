"""Tests for hooks.py — PostToolUse handler and hook installation."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from pmlens.hooks import (
    _build_commit_reminder,
    get_hooks_status,
    handle_post_tool_use,
    install_hooks,
    uninstall_hooks,
)
from pmlens.models import Phase, PhaseStatus, Priority, Project, ProjectStatus, Task, TaskStatus
from pmlens.storage import _save_project, _save_tasks

# ─── Fixtures ─────────────────────────────────────


@pytest.fixture
def settings_dir(tmp_path: Path) -> Path:
    """Temporary directory for Claude Code settings."""
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    return claude_dir


@pytest.fixture
def pm_project(tmp_path: Path) -> Path:
    """Create a project with .pm/ and tasks."""
    pm_path = tmp_path / "project" / ".pm"
    pm_path.mkdir(parents=True)
    (pm_path / "daily").mkdir()

    project = Project(
        name="hooktest",
        display_name="Hook Test",
        status=ProjectStatus.DEVELOPMENT,
        phases=[Phase(id="phase-1", name="Core", status=PhaseStatus.ACTIVE)],
    )
    _save_project(pm_path, project)

    tasks = [
        Task(
            id="HK-001",
            title="Implement feature",
            phase="phase-1",
            status=TaskStatus.IN_PROGRESS,
            priority=Priority.P0,
        ),
        Task(
            id="HK-002",
            title="Write tests",
            phase="phase-1",
            status=TaskStatus.TODO,
            priority=Priority.P1,
        ),
    ]
    _save_tasks(pm_path, tasks)
    return tmp_path / "project"


# ─── Hook Installation ────────────────────────────


class TestGetHooksStatus:
    def test_no_settings_file(self, settings_dir: Path):
        with patch("pmlens.hooks._settings_path", return_value=settings_dir / "settings.json"):
            status = get_hooks_status()
            assert status["installed"] is False

    def test_empty_settings(self, settings_dir: Path):
        (settings_dir / "settings.json").write_text("{}")
        with patch("pmlens.hooks._settings_path", return_value=settings_dir / "settings.json"):
            status = get_hooks_status()
            assert status["installed"] is False

    def test_hooks_installed(self, settings_dir: Path):
        settings = {
            "hooks": {
                "PostToolUse": [
                    {
                        "matcher": "Bash",
                        "hooks": [{"type": "command", "command": "pm-server hook post-tool-use"}],
                    }
                ]
            }
        }
        (settings_dir / "settings.json").write_text(json.dumps(settings))
        with patch("pmlens.hooks._settings_path", return_value=settings_dir / "settings.json"):
            status = get_hooks_status()
            assert status["installed"] is True


class TestInstallHooks:
    def test_install_fresh(self, settings_dir: Path):
        with patch("pmlens.hooks._settings_path", return_value=settings_dir / "settings.json"):
            msg = install_hooks()
            assert "installed" in msg

            settings = json.loads((settings_dir / "settings.json").read_text())
            assert "PostToolUse" in settings["hooks"]
            hooks = settings["hooks"]["PostToolUse"]
            assert len(hooks) == 1
            assert hooks[0]["matcher"] == "Bash"

    def test_install_idempotent(self, settings_dir: Path):
        with patch("pmlens.hooks._settings_path", return_value=settings_dir / "settings.json"):
            install_hooks()
            msg = install_hooks()
            assert "skipped" in msg

            settings = json.loads((settings_dir / "settings.json").read_text())
            # Should still have only 1 hook group, not duplicated
            assert len(settings["hooks"]["PostToolUse"]) == 1

    def test_install_preserves_existing_hooks(self, settings_dir: Path):
        existing = {
            "hooks": {
                "PostToolUse": [
                    {
                        "matcher": "Write",
                        "hooks": [{"type": "command", "command": "my-custom-hook"}],
                    }
                ]
            },
            "other_setting": True,
        }
        (settings_dir / "settings.json").write_text(json.dumps(existing))

        with patch("pmlens.hooks._settings_path", return_value=settings_dir / "settings.json"):
            install_hooks()

            settings = json.loads((settings_dir / "settings.json").read_text())
            assert settings["other_setting"] is True
            hooks = settings["hooks"]["PostToolUse"]
            assert len(hooks) == 2  # existing + pm-server
            assert hooks[0]["matcher"] == "Write"  # existing preserved
            assert hooks[1]["matcher"] == "Bash"  # pm-server added

    def test_install_creates_settings_file(self, settings_dir: Path):
        settings_path = settings_dir / "new_dir" / "settings.json"
        with patch("pmlens.hooks._settings_path", return_value=settings_path):
            install_hooks()
            assert settings_path.exists()


class TestUninstallHooks:
    def test_uninstall_removes_pm_hooks(self, settings_dir: Path):
        with patch("pmlens.hooks._settings_path", return_value=settings_dir / "settings.json"):
            install_hooks()
            msg = uninstall_hooks()
            assert "removed" in msg

            settings = json.loads((settings_dir / "settings.json").read_text())
            # hooks key should be cleaned up
            assert "hooks" not in settings

    def test_uninstall_preserves_other_hooks(self, settings_dir: Path):
        settings = {
            "hooks": {
                "PostToolUse": [
                    {"matcher": "Write", "hooks": [{"type": "command", "command": "other"}]},
                    {
                        "matcher": "Bash",
                        "hooks": [{"type": "command", "command": "pm-server hook post-tool-use"}],
                    },
                ]
            }
        }
        (settings_dir / "settings.json").write_text(json.dumps(settings))

        with patch("pmlens.hooks._settings_path", return_value=settings_dir / "settings.json"):
            uninstall_hooks()

            result = json.loads((settings_dir / "settings.json").read_text())
            hooks = result["hooks"]["PostToolUse"]
            assert len(hooks) == 1
            assert hooks[0]["matcher"] == "Write"

    def test_uninstall_no_hooks(self, settings_dir: Path):
        (settings_dir / "settings.json").write_text("{}")
        with patch("pmlens.hooks._settings_path", return_value=settings_dir / "settings.json"):
            msg = uninstall_hooks()
            assert "skipped" in msg


# ─── Hook Handler ─────────────────────────────────


class TestBuildCommitReminder:
    def test_with_active_task(self, pm_project: Path, monkeypatch):
        # Pin the RW branch — a stray PM_LENS=1 in the shell would otherwise
        # switch the reminder to the read-only suggestion set (PMSERV-086).
        monkeypatch.delenv("PM_LENS", raising=False)
        reminder = _build_commit_reminder(pm_project / ".pm")
        assert "HK-001" in reminder
        assert "pm_update_task" in reminder
        assert "pm_log" in reminder
        assert "pm_next" in reminder

    def test_no_active_tasks(self, tmp_path: Path, monkeypatch):
        monkeypatch.delenv("PM_LENS", raising=False)
        pm_path = tmp_path / ".pm"
        pm_path.mkdir()
        project = Project(
            name="empty",
            display_name="Empty",
            status=ProjectStatus.DEVELOPMENT,
            phases=[],
        )
        _save_project(pm_path, project)
        _save_tasks(pm_path, [])

        reminder = _build_commit_reminder(pm_path)
        assert "pm_update_task" in reminder
        assert "pm_log" in reminder


class TestBuildCommitReminderLensMode:
    """PMSERV-086 / WF-026 FINDING-E: PM_LENS=1 must yield only RO suggestions."""

    def test_lens_mode_swaps_to_ro_tools(self, pm_project: Path, monkeypatch):
        monkeypatch.setenv("PM_LENS", "1")
        reminder = _build_commit_reminder(pm_project / ".pm")
        # Lens banner present
        assert "Read-only mode" in reminder
        # RO tools (in RO_ALLOWLIST) appear
        assert "pm_next" in reminder
        assert "pm_status" in reminder
        assert "pm_tasks" in reminder
        # RW tools must NOT appear — they would 404 in Lens process
        assert "pm_update_task" not in reminder
        assert "pm_log" not in reminder

    def test_lens_mode_keeps_active_task_id_in_pm_next_line(self, pm_project: Path, monkeypatch):
        monkeypatch.setenv("PM_LENS", "1")
        reminder = _build_commit_reminder(pm_project / ".pm")
        # Active task surface is still useful context for the reader
        assert "HK-001" in reminder

    @pytest.mark.parametrize("falsy", ["0", "false", "no", ""])
    def test_falsy_pm_lens_takes_rw_branch(self, pm_project: Path, monkeypatch, falsy):
        monkeypatch.setenv("PM_LENS", falsy)
        reminder = _build_commit_reminder(pm_project / ".pm")
        assert "pm_update_task" in reminder
        assert "pm_log" in reminder
        assert "Read-only mode" not in reminder


class TestHandlePostToolUse:
    def test_git_commit_triggers_reminder(self, pm_project: Path, capsys):
        stdin_data = json.dumps(
            {
                "tool_name": "Bash",
                "tool_input": {"command": 'git commit -m "fix bug"'},
                "cwd": str(pm_project),
            }
        )
        import io

        with patch("sys.stdin", io.StringIO(stdin_data)):
            with patch("sys.stdout", new_callable=io.StringIO) as mock_out:
                handle_post_tool_use()
                output = mock_out.getvalue()

        if output:
            result = json.loads(output)
            assert "additionalContext" in result
            assert "HK-001" in result["additionalContext"]

    def test_non_commit_skipped(self):
        stdin_data = json.dumps(
            {
                "tool_name": "Bash",
                "tool_input": {"command": "ls -la"},
                "cwd": "/tmp",
            }
        )
        import io

        with patch("sys.stdin", io.StringIO(stdin_data)):
            with patch("sys.stdout", new_callable=io.StringIO) as mock_out:
                handle_post_tool_use()
                assert mock_out.getvalue() == ""

    def test_no_pm_dir_skipped(self, tmp_path: Path):
        stdin_data = json.dumps(
            {
                "tool_name": "Bash",
                "tool_input": {"command": 'git commit -m "test"'},
                "cwd": str(tmp_path),
            }
        )
        import io

        with patch("sys.stdin", io.StringIO(stdin_data)):
            with patch("sys.stdout", new_callable=io.StringIO) as mock_out:
                handle_post_tool_use()
                assert mock_out.getvalue() == ""

    def test_invalid_stdin_handled(self):
        import io

        with patch("sys.stdin", io.StringIO("not json")):
            handle_post_tool_use()  # should not raise
