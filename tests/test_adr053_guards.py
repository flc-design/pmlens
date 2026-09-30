"""Tests for the ADR-053 guards (PMSERV-193).

Three behaviours introduced by the 2026-09-17 Claude Code configuration audit:

* pmlens refuses to write PM rules into ``$HOME`` (an ancestor of every
  project, so Claude Code would load them into every session);
* ``pm_status`` warns when an ANCESTOR ``CLAUDE.md`` already carries the PM
  section (``pm_rules_in_ancestor_claudemd``);
* ``get_hooks_status`` reports stale (binary gone) and duplicate PM Lens hook
  entries, ``pm_status`` warns (``stale_pm_hook_command``) and
  ``install_hooks`` repairs them without touching anyone else's hooks.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from pmlens.hooks import (
    _hook_binary_exists,
    _is_pm_hook_command,
    get_hooks_status,
    install_hooks,
    stale_pm_hook_warning,
    uninstall_hooks,
)
from pmlens.models import PmServerError
from pmlens.rules import (
    BEGIN_MARKER,
    END_MARKER,
    TEMPLATE_VERSION,
    ancestor_rules_warning,
    ensure_claudemd,
    guard_not_home_root,
    inject_pm_rules,
    update_claudemd,
)

# ─── Helpers ──────────────────────────────────────


def _pm_section(version: int) -> str:
    return f"{BEGIN_MARKER.format(version=version)}\n## PM rules\n{END_MARKER}\n"


def _settings_with(commands: list[str]) -> dict:
    return {
        "hooks": {
            "PostToolUse": [
                {
                    "matcher": "Bash",
                    "hooks": [{"type": "command", "command": c} for c in commands],
                }
            ]
        }
    }


@pytest.fixture
def settings_file(tmp_path: Path) -> Path:
    claude_dir = tmp_path / "settings-home" / ".claude"
    claude_dir.mkdir(parents=True)
    return claude_dir / "settings.json"


@pytest.fixture
def live_binary(tmp_path: Path) -> Path:
    exe = tmp_path / "bin" / "pmlens"
    exe.parent.mkdir()
    exe.write_text("#!/bin/sh\n", encoding="utf-8")
    return exe


# ─── $HOME guard ──────────────────────────────────


class TestHomeRootGuard:
    def test_guard_raises_for_home(self, isolated_home):
        with pytest.raises(PmServerError, match="home directory"):
            guard_not_home_root(isolated_home)

    def test_guard_passes_for_project_dir(self, isolated_home):
        proj = isolated_home / "proj"
        proj.mkdir()
        guard_not_home_root(proj)  # must not raise

    def test_guard_raises_for_filesystem_root(self):
        root = Path(Path.cwd().anchor)
        with pytest.raises(PmServerError, match="filesystem root"):
            guard_not_home_root(root)

    def test_ensure_and_update_refuse_home(self, isolated_home):
        with pytest.raises(PmServerError, match="home directory"):
            ensure_claudemd(isolated_home)
        with pytest.raises(PmServerError, match="home directory"):
            update_claudemd(isolated_home)
        assert not (isolated_home / "CLAUDE.md").exists()

    def test_inject_pm_rules_reports_failed_instead_of_raising(self, isolated_home):
        summary = inject_pm_rules(isolated_home, target="claude-code")
        assert summary.overall_status == "failed"
        assert len(summary.results) == 1
        assert summary.results[0].status == "failed"
        assert "home directory" in summary.results[0].message
        assert not (isolated_home / "CLAUDE.md").exists()

    def test_inject_still_works_one_level_below_home(self, isolated_home):
        proj = isolated_home / "proj"
        proj.mkdir()
        summary = inject_pm_rules(proj, target="claude-code")
        assert summary.overall_status == "created"
        assert (proj / "CLAUDE.md").exists()

    def test_pm_init_refuses_home_via_cwd(self, isolated_home, monkeypatch):
        from pmlens.server import pm_init

        monkeypatch.chdir(isolated_home)
        with pytest.raises(PmServerError, match="home directory"):
            pm_init()
        assert not (isolated_home / ".pm" / "project.yaml").exists()
        assert not (isolated_home / "CLAUDE.md").exists()

    def test_pm_init_refuses_home_via_explicit_path(self, isolated_home):
        from pmlens.server import pm_init

        with pytest.raises(PmServerError, match="home directory"):
            pm_init(project_path=str(isolated_home))
        assert not (isolated_home / "CLAUDE.md").exists()


# ─── Ancestor CLAUDE.md duplication ───────────────


class TestAncestorRulesWarning:
    def test_none_without_ancestor_section(self, isolated_home):
        proj = isolated_home / "work" / "proj"
        proj.mkdir(parents=True)
        (proj / "CLAUDE.md").write_text(_pm_section(TEMPLATE_VERSION), encoding="utf-8")
        assert ancestor_rules_warning(proj) is None

    def test_ancestor_without_marker_is_ignored(self, isolated_home):
        proj = isolated_home / "work" / "proj"
        proj.mkdir(parents=True)
        (isolated_home / "CLAUDE.md").write_text("# plain personal notes\n", encoding="utf-8")
        assert ancestor_rules_warning(proj) is None

    def test_warns_with_version_and_path(self, isolated_home):
        proj = isolated_home / "work" / "proj"
        proj.mkdir(parents=True)
        (isolated_home / "CLAUDE.md").write_text(_pm_section(11), encoding="utf-8")
        (proj / "CLAUDE.md").write_text(_pm_section(9), encoding="utf-8")

        warning = ancestor_rules_warning(proj)

        assert warning is not None
        assert warning["code"] == "pm_rules_in_ancestor_claudemd"
        assert "v11" in warning["message"]
        # The project's own section is named by ITS version, not the server's
        # template version (ADR-055): the two are what Claude actually sees.
        assert "own section (v9)" in warning["message"]
        assert warning["files"] == [str((isolated_home / "CLAUDE.md").resolve())]
        assert "claudeMdExcludes" in warning["remediation"]

    def test_reports_every_offending_ancestor(self, isolated_home):
        proj = isolated_home / "a" / "b" / "proj"
        proj.mkdir(parents=True)
        (isolated_home / "a" / "CLAUDE.md").write_text(_pm_section(9), encoding="utf-8")
        (isolated_home / "a" / "b" / "CLAUDE.md").write_text(_pm_section(13), encoding="utf-8")

        warning = ancestor_rules_warning(proj)

        assert warning is not None
        assert len(warning["files"]) == 2
        assert "v9" in warning["message"] and "v13" in warning["message"]

    def test_detects_marker_in_logical_ancestor_through_symlink(self, isolated_home):
        # Claude Code walks the path as typed. `work/proj` is a symlink into
        # `vol/proj`; the offending CLAUDE.md sits in the LOGICAL parent `work/`
        # and is invisible from the resolved ancestry.
        real = isolated_home / "vol" / "proj"
        real.mkdir(parents=True)
        (isolated_home / "work").mkdir()
        logical = isolated_home / "work" / "proj"
        logical.symlink_to(real, target_is_directory=True)
        (isolated_home / "work" / "CLAUDE.md").write_text(_pm_section(7), encoding="utf-8")

        warning = ancestor_rules_warning(logical)

        assert warning is not None
        assert warning["files"] == [str((isolated_home / "work" / "CLAUDE.md").resolve())]
        assert "v7" in warning["message"]

    def test_claude_local_md_in_ancestor_counts(self, isolated_home):
        proj = isolated_home / "work" / "proj"
        proj.mkdir(parents=True)
        (isolated_home / "work" / "CLAUDE.local.md").write_text(_pm_section(5), encoding="utf-8")
        warning = ancestor_rules_warning(proj)
        assert warning is not None
        assert warning["files"] == [str((isolated_home / "work" / "CLAUDE.local.md").resolve())]

    def test_pm_status_surfaces_ancestor_warning(self, isolated_home):
        from pmlens.server import pm_init, pm_status

        proj = isolated_home / "work" / "proj"
        proj.mkdir(parents=True)
        (isolated_home / "work" / "CLAUDE.md").write_text(_pm_section(11), encoding="utf-8")
        pm_init(project_path=str(proj), project_name="anc")

        warnings = pm_status(project_path=str(proj))["warnings"]

        hits = [w for w in warnings if w["code"] == "pm_rules_in_ancestor_claudemd"]
        assert len(hits) == 1
        assert str((isolated_home / "work" / "CLAUDE.md").resolve()) in hits[0]["files"]


# ─── Hook health ──────────────────────────────────


class TestHookBinaryExists:
    def test_empty_command(self):
        assert _hook_binary_exists("") is False

    def test_absolute_path_missing(self, tmp_path):
        assert _hook_binary_exists(f"{tmp_path}/nope/pmlens hook post-tool-use") is False

    def test_absolute_path_present(self, live_binary):
        assert _hook_binary_exists(f"{live_binary} hook post-tool-use") is True

    def test_bare_name_resolves_on_path(self):
        assert _hook_binary_exists("sh -c true") is True
        assert _hook_binary_exists("definitely-not-a-real-binary-xyz hook") is False

    def test_quoted_path_with_spaces(self, tmp_path):
        exe_dir = tmp_path / "Claude Extensions"
        exe_dir.mkdir()
        exe = exe_dir / "pm-server"
        exe.write_text("", encoding="utf-8")
        assert _hook_binary_exists(f'"{exe}" hook post-tool-use') is True
        assert _hook_binary_exists(f'"{exe_dir}/missing" hook post-tool-use') is False

    def test_unquoted_path_with_spaces_is_stale_like_the_shell_would_see_it(self, tmp_path):
        # The shell splits on the space too, so this entry can never run.
        exe_dir = tmp_path / "Claude Extensions"
        exe_dir.mkdir()
        (exe_dir / "pm-server").write_text("", encoding="utf-8")
        assert _hook_binary_exists(f"{exe_dir}/pm-server hook post-tool-use") is False

    def test_env_assignment_prefix_is_skipped(self):
        assert _hook_binary_exists("PM_LENS=1 sh -c true") is True
        assert _hook_binary_exists("PM_LENS=1 definitely-not-a-real-binary-xyz hook") is False

    def test_expandable_variable_is_resolved(self, live_binary, monkeypatch):
        monkeypatch.setenv("PMLENS_TEST_BIN", str(live_binary.parent))
        assert _hook_binary_exists("$PMLENS_TEST_BIN/pmlens hook post-tool-use") is True
        assert _hook_binary_exists("$PMLENS_TEST_BIN/missing hook post-tool-use") is False

    def test_unexpandable_variable_is_assumed_live(self, monkeypatch):
        monkeypatch.delenv("PMLENS_UNSET_VAR_XYZ", raising=False)
        # Fail-safe: we cannot know what the shell will resolve, so never
        # report stale (a wrong verdict would let install_hooks rewrite it).
        assert _hook_binary_exists("$PMLENS_UNSET_VAR_XYZ/pmlens hook post-tool-use") is True


class TestIsPmHookCommand:
    @pytest.mark.parametrize(
        "cmd",
        [
            "pmlens hook post-tool-use",
            "pm-server hook post-tool-use",
            "/Users/me/.local/bin/pmlens hook post-tool-use",
            '"/Users/me/Library/Claude Extensions/x/.venv/bin/pm-server" hook post-tool-use',
            "/Users/me/Library/Application Support/Claude/x/.venv/bin/pm-server hook post-tool-use",
            "PM_LENS=1 pmlens hook post-tool-use",
        ],
    )
    def test_matches_our_own_invocations(self, cmd):
        assert _is_pm_hook_command(cmd) is True

    @pytest.mark.parametrize(
        "cmd",
        [
            "/repo/pm-server/scripts/hooks/notify.sh",
            "/Users/me/bin/pmlens-notify hook post-tool-use",
            "echo pmlens hooked",
            "/Users/me/.local/bin/pmlens serve",
            "/Users/me/.local/bin/ct mark running",
            "",
        ],
    )
    def test_ignores_unrelated_commands(self, cmd):
        assert _is_pm_hook_command(cmd) is False


class TestHooksHealthStatus:
    def test_flags_stale_and_duplicates(self, settings_file, live_binary, tmp_path):
        live_cmd = f"{live_binary} hook post-tool-use"
        dead_cmd = f"{tmp_path}/gone/pm-server hook post-tool-use"
        settings_file.write_text(json.dumps(_settings_with([live_cmd, live_cmd, dead_cmd])))

        with patch("pmlens.hooks._settings_path", return_value=settings_file):
            status = get_hooks_status()

        assert status["installed"] is True
        assert status["healthy"] is False
        assert status["commands"] == [live_cmd, live_cmd, dead_cmd]
        assert status["stale"] == [dead_cmd]
        assert status["duplicates"] == [live_cmd]

    def test_healthy_single_entry(self, settings_file, live_binary):
        settings_file.write_text(json.dumps(_settings_with([f"{live_binary} hook post-tool-use"])))
        with patch("pmlens.hooks._settings_path", return_value=settings_file):
            status = get_hooks_status()
        assert status["healthy"] is True
        assert status["stale"] == [] and status["duplicates"] == []

    def test_not_installed_is_not_healthy(self, settings_file):
        settings_file.write_text("{}")
        with patch("pmlens.hooks._settings_path", return_value=settings_file):
            status = get_hooks_status()
        assert status["installed"] is False
        assert status["healthy"] is False
        assert status["commands"] == []

    def test_warning_none_when_healthy(self):
        assert stale_pm_hook_warning({"stale": [], "duplicates": [], "path": "x"}) is None

    def test_warning_shape(self):
        warning = stale_pm_hook_warning(
            {"stale": ["/gone/pm-server hook post-tool-use"], "duplicates": [], "path": "/s.json"}
        )
        assert warning is not None
        assert warning["code"] == "stale_pm_hook_command"
        assert "/gone/pm-server" in warning["message"]
        assert "pmlens install-hooks" in warning["remediation"]
        assert warning["stale"] == ["/gone/pm-server hook post-tool-use"]


class TestInstallHooksRepair:
    def test_repairs_stale_entry_and_keeps_user_hooks(self, settings_file, live_binary, tmp_path):
        settings = {
            "model": "opus",
            "hooks": {
                "PostToolUse": [
                    {"matcher": "*", "hooks": [{"type": "command", "command": "/usr/bin/true"}]},
                    {
                        "matcher": "Bash",
                        "hooks": [
                            {
                                "type": "command",
                                "command": f"{tmp_path}/gone/pm-server hook post-tool-use",
                            },
                            {"type": "command", "command": "/usr/bin/false"},
                        ],
                    },
                ]
            },
        }
        settings_file.write_text(json.dumps(settings))

        with (
            patch("pmlens.hooks._settings_path", return_value=settings_file),
            patch("pmlens.hooks._pm_server_command", return_value=str(live_binary)),
        ):
            message = install_hooks()
            status = get_hooks_status()

        assert "repaired" in message
        assert status["healthy"] is True
        assert status["commands"] == [f"{live_binary} hook post-tool-use"]

        saved = json.loads(settings_file.read_text())
        all_commands = [h["command"] for g in saved["hooks"]["PostToolUse"] for h in g["hooks"]]
        assert "/usr/bin/true" in all_commands
        assert "/usr/bin/false" in all_commands, "user hook sharing the group must survive"
        assert saved["model"] == "opus", "unrelated keys must be preserved"

    def test_repair_never_removes_a_lookalike_user_hook(self, settings_file, live_binary, tmp_path):
        # A user's own hook whose PATH happens to contain "pm-server" and "hook"
        # (and whose script is gone) must survive the repair untouched.
        lookalike = f"{tmp_path}/repo/pm-server/scripts/hooks/notify.sh"
        settings = _settings_with([f"{tmp_path}/gone/pm-server hook post-tool-use", lookalike])
        settings_file.write_text(json.dumps(settings))

        with (
            patch("pmlens.hooks._settings_path", return_value=settings_file),
            patch("pmlens.hooks._pm_server_command", return_value=str(live_binary)),
        ):
            install_hooks()

        saved = json.loads(settings_file.read_text())
        all_commands = [h["command"] for g in saved["hooks"]["PostToolUse"] for h in g["hooks"]]
        assert lookalike in all_commands
        assert f"{tmp_path}/gone/pm-server hook post-tool-use" not in all_commands

    def test_dedupes_duplicate_entries(self, settings_file, live_binary):
        cmd = f"{live_binary} hook post-tool-use"
        settings_file.write_text(json.dumps(_settings_with([cmd, cmd])))

        with (
            patch("pmlens.hooks._settings_path", return_value=settings_file),
            patch("pmlens.hooks._pm_server_command", return_value=str(live_binary)),
        ):
            message = install_hooks()
            status = get_hooks_status()

        assert "repaired" in message
        assert status["commands"] == [cmd]
        assert status["healthy"] is True

    def test_skips_when_healthy(self, settings_file, live_binary):
        settings_file.write_text(json.dumps(_settings_with([f"{live_binary} hook post-tool-use"])))
        before = settings_file.read_text()

        with (
            patch("pmlens.hooks._settings_path", return_value=settings_file),
            patch("pmlens.hooks._pm_server_command", return_value=str(live_binary)),
        ):
            message = install_hooks()

        assert "already installed" in message
        assert settings_file.read_text() == before

    def test_uninstall_keeps_user_hook_in_shared_group(self, settings_file, live_binary):
        settings = {
            "hooks": {
                "PostToolUse": [
                    {
                        "matcher": "Bash",
                        "hooks": [
                            {"type": "command", "command": f"{live_binary} hook post-tool-use"},
                            {"type": "command", "command": "/usr/bin/false"},
                        ],
                    }
                ]
            }
        }
        settings_file.write_text(json.dumps(settings))

        with patch("pmlens.hooks._settings_path", return_value=settings_file):
            message = uninstall_hooks()
            status = get_hooks_status()

        assert "removed" in message
        assert status["installed"] is False
        saved = json.loads(settings_file.read_text())
        all_commands = [h["command"] for g in saved["hooks"]["PostToolUse"] for h in g["hooks"]]
        assert all_commands == ["/usr/bin/false"]

    def test_pm_status_reports_but_does_not_repair(self, isolated_home, tmp_path):
        from pmlens.server import pm_init, pm_status

        proj = isolated_home / "proj"
        proj.mkdir()
        settings_path = isolated_home / ".claude" / "settings.json"
        settings_path.parent.mkdir(parents=True)
        original = _settings_with([f"{tmp_path}/gone/pmlens hook post-tool-use"])
        settings_path.write_text(json.dumps(original))
        pm_init(project_path=str(proj), project_name="hk")

        result = pm_status(project_path=str(proj))

        codes = [w["code"] for w in result["warnings"]]
        assert "stale_pm_hook_command" in codes
        assert result["hooks"]["installed"] is True
        assert result["hooks"]["healthy"] is False
        assert json.loads(settings_path.read_text()) == original, "pm_status must be report-only"
