"""Tests for the pm-server -> pmlens migration (PMSERV-137 / ADR-034 step 3).

Drives ``installer.migrate_to_pmlens`` and its three per-surface functions
against the ``legacy_user_env`` fixture (a pre-rename user) and mocked Claude
subprocess. The legacy ``migrate`` command (pm-agent -> pm-server) is a separate
surface covered by ``test_installer.py::TestMigrateFromPmAgent`` and must stay
untouched — asserted here via the CLI.
"""

from __future__ import annotations

import json
import subprocess
import tomllib

from pmlens import installer


def _mock_claude(monkeypatch, *, pm_server_present: bool, claude: str | None = "/usr/bin/claude"):
    """Patch shutil.which + subprocess.run for migrate_claude_code.

    ``pm_server_present`` controls the ``claude mcp get pm-server`` probe return
    code; add/remove always succeed. Returns the recorded command list.
    """

    def which(name):
        # PMSERV-137 A4: pmlens is on PATH (mirrors a host after `pipx install
        # pmlens`) so the new registration resolves to the pmlens binary, with
        # pm-server kept as the mid-flight fallback.
        return {
            "claude": claude,
            "pmlens": "/usr/bin/pmlens",
            "pm-server": "/usr/bin/pm-server",
        }.get(name)

    monkeypatch.setattr(installer.shutil, "which", which)

    calls: list[list[str]] = []

    def run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[2:4] == ["get", "pm-server"]:
            rc = 0 if pm_server_present else 1
        else:
            rc = 0
        return subprocess.CompletedProcess(cmd, rc, stdout="", stderr="")

    monkeypatch.setattr(installer.subprocess, "run", run)
    return calls


# ─── Codex re-key (deep-copy) ──────────────────────────────────────────────


class TestMigrateCodex:
    def test_rekeys_and_preserves_subtables(self, legacy_user_env):
        env = legacy_user_env
        result = installer.migrate_codex()

        assert result.status == "installed"
        assert result.backup_path is not None

        doc = tomllib.loads(env.codex_config.read_text(encoding="utf-8"))
        servers = doc["mcp_servers"]
        assert "pm-server" not in servers, "old key must be deleted"
        assert "pmlens" in servers, "new key must be present"
        # User-authored tools.* sub-tables survive byte-for-byte under the new key.
        assert set(servers["pmlens"]["tools"]) == set(env.codex_tool_subtables)
        for tool in env.codex_tool_subtables:
            assert servers["pmlens"]["tools"][tool]["approval_mode"] == "approve"
        # Managed top-level fields preserved (pure re-key, no command rewrite).
        assert servers["pmlens"]["command"] == "/old/path/to/pm-server"
        assert list(servers["pmlens"]["args"]) == ["serve"]
        assert servers["pmlens"]["startup_timeout_sec"] == 30

    def test_idempotent_second_run_skipped(self, legacy_user_env):
        # After a successful re-key the old key is gone, so a re-run finds
        # nothing to migrate (the normal idempotent post-migration state).
        installer.migrate_codex()
        result = installer.migrate_codex()
        assert result.status == "skipped"

    def test_both_keys_present_is_already_registered(self, legacy_user_env):
        # Partial/manual state: both old and new keys exist -> conservative
        # no-op (don't clobber a possibly-customized pmlens table).
        import tomlkit

        doc = tomlkit.parse(legacy_user_env.codex_config.read_text(encoding="utf-8"))
        pmlens = tomlkit.table()
        pmlens["command"] = "/custom/pmlens"
        doc["mcp_servers"]["pmlens"] = pmlens
        legacy_user_env.codex_config.write_text(tomlkit.dumps(doc), encoding="utf-8")

        result = installer.migrate_codex()
        assert result.status == "already_registered"
        # Old key is left intact for the user to resolve; pmlens not clobbered.
        doc2 = tomllib.loads(legacy_user_env.codex_config.read_text(encoding="utf-8"))
        assert "pm-server" in doc2["mcp_servers"]
        assert doc2["mcp_servers"]["pmlens"]["command"] == "/custom/pmlens"

    def test_dry_run_does_not_write(self, legacy_user_env):
        before = legacy_user_env.codex_config.read_text(encoding="utf-8")
        result = installer.migrate_codex(dry_run=True)
        assert result.is_dry_run and result.status == "installed"
        assert result.backup_path is None
        assert legacy_user_env.codex_config.read_text(encoding="utf-8") == before

    def test_skipped_when_no_pm_server(self, legacy_user_env):
        legacy_user_env.codex_config.write_text(
            '[mcp_servers.other]\ncommand = "x"\n', encoding="utf-8"
        )
        result = installer.migrate_codex()
        assert result.status == "skipped"

    def test_skipped_when_no_config(self, legacy_user_env):
        legacy_user_env.codex_config.unlink()
        result = installer.migrate_codex()
        assert result.status == "skipped"


# ─── settings.json additive perm rewrite ───────────────────────────────────


class TestMigrateSettingsPerms:
    def test_additive_keeps_old_adds_new(self, legacy_user_env):
        env = legacy_user_env
        result = installer.migrate_settings_perms()

        assert result.status == "installed"
        assert result.backup_path is not None

        allow = json.loads(env.settings_json.read_text(encoding="utf-8"))["permissions"]["allow"]
        for old in env.perm_entries:
            assert old in allow, "legacy entry must be KEPT (additive)"
            twin = old.replace("mcp__pm-server__", "mcp__pmlens__")
            assert twin in allow, "pmlens twin must be added"
        assert len([a for a in allow if a.startswith("mcp__pm-server__")]) == 3
        assert len([a for a in allow if a.startswith("mcp__pmlens__")]) == 3

    def test_idempotent_second_run_skipped(self, legacy_user_env):
        installer.migrate_settings_perms()
        after_first = legacy_user_env.settings_json.read_text(encoding="utf-8")
        result = installer.migrate_settings_perms()
        assert result.status == "skipped", "twins already present — nothing to add"
        assert legacy_user_env.settings_json.read_text(encoding="utf-8") == after_first

    def test_dry_run_does_not_write(self, legacy_user_env):
        before = legacy_user_env.settings_json.read_text(encoding="utf-8")
        result = installer.migrate_settings_perms(dry_run=True)
        assert result.is_dry_run and result.status == "installed"
        assert legacy_user_env.settings_json.read_text(encoding="utf-8") == before

    def test_unrelated_perms_untouched(self, legacy_user_env):
        s = json.loads(legacy_user_env.settings_json.read_text(encoding="utf-8"))
        s["permissions"]["allow"].append("Bash(ls:*)")
        legacy_user_env.settings_json.write_text(json.dumps(s), encoding="utf-8")
        installer.migrate_settings_perms()
        allow = json.loads(legacy_user_env.settings_json.read_text(encoding="utf-8"))[
            "permissions"
        ]["allow"]
        assert "Bash(ls:*)" in allow

    def test_skipped_when_no_pm_server_perms(self, legacy_user_env):
        legacy_user_env.settings_json.write_text(
            json.dumps({"permissions": {"allow": ["Bash"]}}), encoding="utf-8"
        )
        result = installer.migrate_settings_perms()
        assert result.status == "skipped"


# ─── Claude Code re-registration (mocked subprocess) ───────────────────────


class TestMigrateClaudeCode:
    def test_migrates_when_pm_server_present(self, monkeypatch):
        calls = _mock_claude(monkeypatch, pm_server_present=True)
        result = installer.migrate_claude_code()
        assert result.status == "installed"
        assert any("add" in c and "pmlens" in c for c in calls), calls
        assert any("remove" in c and "pm-server" in c for c in calls), calls
        # PMSERV-137 A4: the new pmlens registration must invoke the pmlens
        # binary (resolved via shutil.which("pmlens")), not the legacy
        # pm-server binary. The binary is the token right after the "--".
        add_cmd = next(c for c in calls if "add" in c and "pmlens" in c)
        assert add_cmd[add_cmd.index("--") + 1] == "/usr/bin/pmlens", add_cmd

    def test_skipped_when_pm_server_absent(self, monkeypatch):
        _mock_claude(monkeypatch, pm_server_present=False)
        result = installer.migrate_claude_code()
        assert result.status == "skipped"

    def test_dry_run_probes_only(self, monkeypatch):
        calls = _mock_claude(monkeypatch, pm_server_present=True)
        result = installer.migrate_claude_code(dry_run=True)
        assert result.is_dry_run and result.status == "installed"
        assert all("add" not in c and "remove" not in c for c in calls), calls

    def test_skipped_when_claude_not_found(self, monkeypatch):
        _mock_claude(monkeypatch, pm_server_present=True, claude=None)
        result = installer.migrate_claude_code()
        assert result.status == "skipped"


# ─── Orchestrator ──────────────────────────────────────────────────────────


class TestMigrateToPmlens:
    def test_three_surfaces_migrate(self, legacy_user_env, monkeypatch):
        _mock_claude(monkeypatch, pm_server_present=True)
        summary = installer.migrate_to_pmlens()
        by_target = {r.target: r for r in summary.results}
        assert set(by_target) == {"claude-code", "codex", "claude-code-settings"}
        assert by_target["codex"].status == "installed"
        assert by_target["claude-code-settings"].status == "installed"
        assert by_target["claude-code"].status == "installed"

    def test_safe_call_isolates_failure(self, legacy_user_env, monkeypatch):
        def _boom(**kwargs):
            raise RuntimeError("boom")

        monkeypatch.setattr(installer, "migrate_codex", _boom)
        # No claude binary -> claude-code skipped; settings from fixture installs.
        monkeypatch.setattr(installer.shutil, "which", lambda n: None)

        summary = installer.migrate_to_pmlens()
        by_target = {r.target: r for r in summary.results}
        assert by_target["codex"].status == "failed"
        assert "boom" in by_target["codex"].message
        assert by_target["claude-code-settings"].status == "installed"

    def test_dry_run_writes_nothing(self, legacy_user_env, monkeypatch):
        _mock_claude(monkeypatch, pm_server_present=True)
        before_codex = legacy_user_env.codex_config.read_text(encoding="utf-8")
        before_settings = legacy_user_env.settings_json.read_text(encoding="utf-8")
        summary = installer.migrate_to_pmlens(dry_run=True)
        assert all(r.is_dry_run for r in summary.results)
        assert legacy_user_env.codex_config.read_text(encoding="utf-8") == before_codex
        assert legacy_user_env.settings_json.read_text(encoding="utf-8") == before_settings


# ─── Read-only cutover awareness probe ─────────────────────────────────────


class TestLegacyAwareness:
    def test_dormant_when_not_pmlens_identity(self, legacy_user_env):
        # Steps 3-5: the live FastMCP name is still "pm-server", so the probe is
        # dormant — no false "migrate now" prompt while config still matches.
        assert installer.legacy_pm_server_awareness(identity_is_pmlens=False) is None

    def test_fires_post_flip_with_legacy_config(self, legacy_user_env):
        banner = installer.legacy_pm_server_awareness(identity_is_pmlens=True)
        assert banner is not None
        assert banner["perm_entries"] == 3
        assert banner["codex_legacy"] is True
        assert "migrate-from-pm-server" in banner["message"]

    def test_none_when_no_legacy_config(self, legacy_user_env):
        legacy_user_env.settings_json.write_text("{}", encoding="utf-8")
        legacy_user_env.codex_config.unlink()
        assert installer.legacy_pm_server_awareness(identity_is_pmlens=True) is None

    def test_counts_only_pm_server_perms(self, legacy_user_env):
        # codex removed; only the 3 settings.json perms remain as the signal.
        legacy_user_env.codex_config.unlink()
        banner = installer.legacy_pm_server_awareness(identity_is_pmlens=True)
        assert banner is not None
        assert banner["perm_entries"] == 3
        assert banner["codex_legacy"] is False


# ─── CLI wiring ────────────────────────────────────────────────────────────


class TestMigrateCli:
    def test_migrate_from_pm_server_command_registered(self):
        from click.testing import CliRunner

        from pmlens.__main__ import cli

        result = CliRunner().invoke(cli, ["migrate-from-pm-server", "--help"])
        assert result.exit_code == 0
        assert "pmlens" in result.output.lower()

    def test_upgrade_alias_registered(self):
        from click.testing import CliRunner

        from pmlens.__main__ import cli

        result = CliRunner().invoke(cli, ["upgrade", "--help"])
        assert result.exit_code == 0

    def test_legacy_migrate_command_unchanged(self):
        # The pm-agent migrate command must remain a distinct, working command.
        from click.testing import CliRunner

        from pmlens.__main__ import cli

        result = CliRunner().invoke(cli, ["migrate", "--help"])
        assert result.exit_code == 0

    def test_dry_run_cli_writes_nothing(self, legacy_user_env, monkeypatch):
        from click.testing import CliRunner

        from pmlens.__main__ import cli

        # No claude binary -> claude-code skipped; codex+settings dry-run only.
        monkeypatch.setattr(installer.shutil, "which", lambda n: None)
        before = legacy_user_env.codex_config.read_text(encoding="utf-8")
        result = CliRunner().invoke(cli, ["migrate-from-pm-server", "--dry-run"])
        assert result.exit_code == 0
        assert legacy_user_env.codex_config.read_text(encoding="utf-8") == before
