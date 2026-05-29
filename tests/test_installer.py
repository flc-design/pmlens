"""Tests for Claude Code + Codex MCP installer."""

import subprocess
import textwrap
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

import pytest
import tomlkit

from pm_server.installer import (
    InstallResult,
    InstallStatus,
    InstallSummary,
    install,
    install_claude_code,
    install_codex,
    install_mcp,
    uninstall,
    uninstall_claude_code,
    uninstall_codex,
    uninstall_mcp,
)


def _make_result(returncode: int = 0, stderr: str = "") -> CompletedProcess:
    return CompletedProcess(args=[], returncode=returncode, stdout="", stderr=stderr)


@pytest.fixture
def fake_codex_config(tmp_path, monkeypatch):
    """Redirect installer._codex_config_path to a tmp_path location.

    Tests must explicitly create the config file if needed; otherwise
    install_codex / uninstall_codex see a missing config and return
    status="skipped". This isolates tests from the real ~/.codex.
    """
    config_path = tmp_path / ".codex" / "config.toml"
    config_path.parent.mkdir(exist_ok=True)
    monkeypatch.setattr("pm_server.installer._codex_config_path", lambda: config_path)
    return config_path


class TestInstallMcp:
    def test_pm_server_not_found(self):
        with patch("pm_server.installer.shutil.which", return_value=None):
            msg = install_mcp()
            assert "pm-server command not found" in msg

    def test_claude_not_found(self):
        def which(name):
            return "/usr/bin/pm-server" if name == "pm-server" else None

        with patch("pm_server.installer.shutil.which", side_effect=which):
            msg = install_mcp()
            assert "claude command not found" in msg

    def test_already_registered(self):
        def which(name):
            return f"/usr/bin/{name}"

        with (
            patch("pm_server.installer.shutil.which", side_effect=which),
            patch(
                "pm_server.installer.subprocess.run",
                return_value=_make_result(0),
            ),
        ):
            msg = install_mcp()
            assert "already registered" in msg.lower()

    def test_install_success(self):
        def which(name):
            return f"/usr/bin/{name}"

        call_count = 0

        def mock_run(cmd, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # mcp get -> not found
                return _make_result(1)
            # mcp add -> success
            return _make_result(0)

        with (
            patch("pm_server.installer.shutil.which", side_effect=which),
            patch("pm_server.installer.subprocess.run", side_effect=mock_run),
        ):
            msg = install_mcp()
            assert "registered" in msg.lower()
            assert "user scope" in msg.lower()

    def test_install_failure(self):
        def which(name):
            return f"/usr/bin/{name}"

        def mock_run(cmd, **kwargs):
            if "get" in cmd:
                return _make_result(1)
            return _make_result(1, stderr="some error")

        with (
            patch("pm_server.installer.shutil.which", side_effect=which),
            patch("pm_server.installer.subprocess.run", side_effect=mock_run),
        ):
            msg = install_mcp()
            assert "failed to register" in msg.lower()

    def test_install_mcp_emits_deprecation_warning(self):
        """PMSERV-055: install_mcp is a v0.4.x compat wrapper scheduled for
        removal in v1.0.0; calling it must emit a DeprecationWarning naming
        the replacement."""

        def which(name):
            return f"/usr/bin/{name}"

        def mock_run(cmd, **kwargs):
            return _make_result(1) if "get" in cmd else _make_result(0)

        with (
            patch("pm_server.installer.shutil.which", side_effect=which),
            patch("pm_server.installer.subprocess.run", side_effect=mock_run),
            pytest.warns(DeprecationWarning, match=r"install_mcp.*1\.0\.0"),
        ):
            install_mcp()


class TestUninstallMcp:
    def test_claude_not_found(self):
        with patch("pm_server.installer.shutil.which", return_value=None):
            msg = uninstall_mcp()
            assert "claude command not found" in msg

    def test_uninstall_success(self):
        with (
            patch("pm_server.installer.shutil.which", return_value="/usr/bin/claude"),
            patch(
                "pm_server.installer.subprocess.run",
                return_value=_make_result(0),
            ),
        ):
            msg = uninstall_mcp()
            assert "unregistered" in msg.lower()

    def test_uninstall_not_registered(self):
        with (
            patch("pm_server.installer.shutil.which", return_value="/usr/bin/claude"),
            patch(
                "pm_server.installer.subprocess.run",
                return_value=_make_result(1, stderr="not found"),
            ),
        ):
            msg = uninstall_mcp()
            assert "not registered" in msg.lower() or "removal failed" in msg.lower()

    def test_uninstall_mcp_emits_deprecation_warning(self):
        """PMSERV-055: uninstall_mcp deprecation mirrors install_mcp."""
        with (
            patch("pm_server.installer.shutil.which", return_value="/usr/bin/claude"),
            patch("pm_server.installer.subprocess.run", return_value=_make_result(0)),
            pytest.warns(DeprecationWarning, match=r"uninstall_mcp.*1\.0\.0"),
        ):
            uninstall_mcp()


class TestMigrateFromPmAgent:
    def test_migrate_from_pm_agent(self, tmp_path, monkeypatch, recwarn):
        """migrate コマンドが旧 pm-agent を解除して pm-server を登録する。"""
        calls = []

        def mock_run(cmd, **kwargs):
            calls.append(cmd)
            result = subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
            return result

        monkeypatch.setattr(subprocess, "run", mock_run)
        monkeypatch.setattr(
            "pm_server.installer.shutil.which",
            lambda name: "/usr/bin/claude" if name == "claude" else f"/usr/bin/{name}",
        )

        # registry を用意
        registry_dir = tmp_path / ".pm"
        registry_dir.mkdir()
        (registry_dir / "registry.yaml").write_text("projects: []")
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        from pm_server.installer import migrate_from_pm_agent

        migrate_from_pm_agent()

        # remove pm-agent が呼ばれたこと
        assert any("pm-agent" in str(c) for c in calls)
        # add pm-server が呼ばれたこと
        assert any("pm-server" in str(c) for c in calls)
        # PMSERV-055: migrate must call the non-deprecated per-host function
        # (install_claude_code), not the install_mcp() wrapper — so no
        # DeprecationWarning should escape from the migration path.
        assert not any("deprecated" in str(w.message).lower() for w in recwarn)


class TestInstallClaudeCode:
    """Structured install_claude_code (PMSERV-037)."""

    def test_pm_server_not_found_returns_failed(self):
        with patch("pm_server.installer.shutil.which", return_value=None):
            r = install_claude_code()
            assert r.target == "claude-code"
            assert r.status == "failed"
            assert "pm-server command not found" in r.message

    def test_claude_not_found_returns_skipped(self):
        def which(name):
            return "/usr/bin/pm-server" if name == "pm-server" else None

        with patch("pm_server.installer.shutil.which", side_effect=which):
            r = install_claude_code()
            assert r.status == "skipped"
            assert "claude command not found" in r.message

    def test_already_registered(self):
        def which(name):
            return f"/usr/bin/{name}"

        with (
            patch("pm_server.installer.shutil.which", side_effect=which),
            patch(
                "pm_server.installer.subprocess.run",
                return_value=_make_result(0),
            ),
        ):
            r = install_claude_code()
            assert r.status == "already_registered"

    def test_install_success(self):
        def which(name):
            return f"/usr/bin/{name}"

        call_count = 0

        def mock_run(cmd, **kwargs):
            nonlocal call_count
            call_count += 1
            return _make_result(1) if call_count == 1 else _make_result(0)

        with (
            patch("pm_server.installer.shutil.which", side_effect=which),
            patch("pm_server.installer.subprocess.run", side_effect=mock_run),
        ):
            r = install_claude_code()
            assert r.status == "installed"
            assert "user scope" in r.message.lower()

    def test_install_dry_run_when_not_registered_does_not_run_mcp_add(self):
        """dry-run propagates pre-check but never executes ``claude mcp add`` (PMSERV-039)."""

        def which(name):
            return f"/usr/bin/{name}"

        calls = []

        def mock_run(cmd, **kwargs):
            calls.append(list(cmd))
            return _make_result(1)  # mcp get -> not registered

        with (
            patch("pm_server.installer.shutil.which", side_effect=which),
            patch("pm_server.installer.subprocess.run", side_effect=mock_run),
        ):
            r = install_claude_code(dry_run=True)

        assert r.status == "installed"
        assert r.is_dry_run is True
        assert "would register" in r.message.lower()
        # Only the read-only `mcp get` call was made; no `mcp add`.
        assert len(calls) == 1
        assert calls[0][1:3] == ["mcp", "get"]

    def test_install_dry_run_already_registered_returns_already_registered(self):
        """dry-run reports already_registered without firing ``claude mcp add`` (PMSERV-039)."""

        def which(name):
            return f"/usr/bin/{name}"

        with (
            patch("pm_server.installer.shutil.which", side_effect=which),
            patch(
                "pm_server.installer.subprocess.run",
                return_value=_make_result(0),
            ),
        ):
            r = install_claude_code(dry_run=True)
        assert r.status == "already_registered"
        assert r.is_dry_run is True


class TestUninstallClaudeCode:
    """Structured uninstall_claude_code (PMSERV-039 W2-B: pre-check added)."""

    def test_uninstall_when_not_registered_returns_skipped(self):
        """Pre-check distinguishes "not registered" from genuine removal errors."""

        with (
            patch("pm_server.installer.shutil.which", return_value="/usr/bin/claude"),
            patch(
                "pm_server.installer.subprocess.run",
                return_value=_make_result(1, stderr="not found"),
            ),
        ):
            r = uninstall_claude_code()
        assert r.status == "skipped"
        assert "not registered" in r.message.lower()

    def test_uninstall_dry_run_when_registered_does_not_run_mcp_remove(self):
        """dry-run runs ``claude mcp get`` to detect registration but never removes."""

        calls = []

        def mock_run(cmd, **kwargs):
            calls.append(list(cmd))
            return _make_result(0)  # mcp get -> registered

        with (
            patch("pm_server.installer.shutil.which", return_value="/usr/bin/claude"),
            patch("pm_server.installer.subprocess.run", side_effect=mock_run),
        ):
            r = uninstall_claude_code(dry_run=True)

        assert r.status == "uninstalled"
        assert r.is_dry_run is True
        assert "would unregister" in r.message.lower()
        # Only the read-only `mcp get` call was made; no `mcp remove`.
        assert len(calls) == 1
        assert calls[0][1:3] == ["mcp", "get"]

    def test_uninstall_when_mcp_remove_fails_returns_failed(self):
        """Genuine ``claude mcp remove`` failure (post-pre-check) yields status=failed."""

        call_count = 0

        def mock_run(cmd, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # mcp get -> registered
                return _make_result(0)
            # mcp remove -> error
            return _make_result(1, stderr="permission denied")

        with (
            patch("pm_server.installer.shutil.which", return_value="/usr/bin/claude"),
            patch("pm_server.installer.subprocess.run", side_effect=mock_run),
        ):
            r = uninstall_claude_code()

        assert r.status == "failed"
        assert "removal failed" in r.message.lower()
        assert "permission denied" in r.message


class TestInstallOrchestrator:
    """install / uninstall orchestrators (PMSERV-037)."""

    def test_target_claude_code_returns_one_result(self):
        def which(name):
            return "/usr/bin/pm-server" if name == "pm-server" else None

        with patch("pm_server.installer.shutil.which", side_effect=which):
            summary = install(target="claude-code")
        assert len(summary.results) == 1
        assert summary.results[0].target == "claude-code"

    def test_target_codex_returns_skipped_when_config_not_found(self, fake_codex_config):
        # fake_codex_config does not create the file -> install_codex sees no config
        summary = install(target="codex")
        assert len(summary.results) == 1
        assert summary.results[0].target == "codex"
        assert summary.results[0].status == "skipped"
        assert "not found" in summary.results[0].message.lower()

    def test_target_auto_runs_both(self, fake_codex_config):
        def which(name):
            return "/usr/bin/pm-server" if name == "pm-server" else None

        with patch("pm_server.installer.shutil.which", side_effect=which):
            summary = install(target="auto")
        targets = [r.target for r in summary.results]
        assert "claude-code" in targets
        assert "codex" in targets

    def test_failure_in_one_host_does_not_abort_sibling(self, fake_codex_config):
        def which(name):
            return f"/usr/bin/{name}"

        def mock_run(cmd, **kwargs):
            raise RuntimeError("kaboom")

        with (
            patch("pm_server.installer.shutil.which", side_effect=which),
            patch("pm_server.installer.subprocess.run", side_effect=mock_run),
        ):
            summary = install(target="auto")

        cc = next(r for r in summary.results if r.target == "claude-code")
        codex = next(r for r in summary.results if r.target == "codex")
        assert cc.status == "failed"
        assert "kaboom" in cc.message
        assert codex.status == "skipped"

    def test_uninstall_target_codex_returns_skipped_when_config_not_found(self, fake_codex_config):
        summary = uninstall(target="codex")
        assert len(summary.results) == 1
        assert summary.results[0].target == "codex"
        assert summary.results[0].status == "skipped"

    def test_unknown_target_raises_value_error(self):
        with pytest.raises(ValueError, match="unknown target"):
            install(target="banana")

    def test_uninstall_target_claude_code_directly(self):
        """uninstall(target='claude-code') dispatches to uninstall_claude_code (PMSERV-040)."""
        with (
            patch("pm_server.installer.shutil.which", return_value="/usr/bin/claude"),
            patch(
                "pm_server.installer.subprocess.run",
                return_value=_make_result(0),
            ),
        ):
            summary = uninstall(target="claude-code")
        assert len(summary.results) == 1
        assert summary.results[0].target == "claude-code"
        assert summary.results[0].status == "uninstalled"

    def test_target_all_returns_all_known_hosts_in_order(self, fake_codex_config):
        """target='all' synonyms 'auto', preserving _KNOWN_HOSTS order (PMSERV-039)."""

        def which(name):
            return "/usr/bin/pm-server" if name == "pm-server" else None

        with patch("pm_server.installer.shutil.which", side_effect=which):
            summary = install(target="all")
        targets = [r.target for r in summary.results]
        assert targets == ["claude-code", "codex"]

    def test_unknown_target_error_lists_all_in_valid_choices(self):
        """ValueError message lists ``"all"`` alongside ``"auto"`` (PMSERV-039)."""
        with pytest.raises(ValueError, match=r"\bauto\b") as exc:
            install(target="banana")
        assert "all" in str(exc.value)

    def test_install_dry_run_propagates_to_all_hosts(self, fake_codex_config):
        """dry_run is passed to every per-host installer (PMSERV-039)."""

        def which(name):
            return f"/usr/bin/{name}"

        # Codex config exists but pm-server is not in [mcp_servers]; installer
        # would normally write — verify dry-run skips that.
        fake_codex_config.write_text("# stub\n", encoding="utf-8")

        with (
            patch("pm_server.installer.shutil.which", side_effect=which),
            patch(
                "pm_server.installer.subprocess.run",
                return_value=_make_result(1),  # claude mcp get -> not registered
            ),
            patch(
                "pm_server.installer._resolve_pm_server_path",
                return_value=Path("/usr/bin/pm-server"),
            ),
        ):
            summary = install(target="all", dry_run=True)

        assert all(r.is_dry_run is True for r in summary.results)
        assert all("would " in r.message.lower() for r in summary.results)
        # Codex config file must not have been mutated.
        assert fake_codex_config.read_text(encoding="utf-8") == "# stub\n"

    def test_uninstall_dry_run_propagates_to_all_hosts(self, fake_codex_config):
        """dry_run is propagated by the uninstall orchestrator (PMSERV-039)."""

        codex_content = textwrap.dedent("""
            [mcp_servers.pm-server]
            command = "/usr/bin/pm-server"
            args = ["serve"]
            startup_timeout_sec = 30
        """).lstrip()
        fake_codex_config.write_text(codex_content, encoding="utf-8")

        with (
            patch("pm_server.installer.shutil.which", return_value="/usr/bin/claude"),
            patch(
                "pm_server.installer.subprocess.run",
                return_value=_make_result(0),  # mcp get -> registered
            ),
        ):
            summary = uninstall(target="all", dry_run=True)

        assert all(r.is_dry_run is True for r in summary.results)
        # Codex config must not have been mutated.
        assert fake_codex_config.read_text(encoding="utf-8") == codex_content


class TestInstallSummary:
    """InstallSummary aggregation (PMSERV-037)."""

    def test_overall_status_failed_takes_priority(self):
        s = InstallSummary(
            results=[
                InstallResult("a", "failed", "x"),
                InstallResult("b", "installed", "y"),
            ]
        )
        assert s.overall_status == "failed"

    def test_overall_status_installed_when_no_failure(self):
        s = InstallSummary(
            results=[
                InstallResult("a", "installed", "x"),
                InstallResult("b", "skipped", "y"),
            ]
        )
        assert s.overall_status == "installed"

    def test_overall_status_skipped_when_all_skipped(self):
        s = InstallSummary(
            results=[
                InstallResult("a", "skipped", "x"),
                InstallResult("b", "skipped", "y"),
            ]
        )
        assert s.overall_status == "skipped"

    def test_message_aggregation(self):
        s = InstallSummary(
            results=[
                InstallResult("a", "installed", "hello"),
                InstallResult("b", "skipped", "world"),
            ]
        )
        assert "[a] hello" in s.message
        assert "[b] world" in s.message

    def test_message_when_empty(self):
        s = InstallSummary()
        assert s.message == "no targets processed"

    def test_overall_status_installed_when_skipped_mixed(self):
        """installed + skipped mixture resolves to 'installed' (PMSERV-040)."""
        s = InstallSummary(
            results=[
                InstallResult("claude-code", "installed", "ok"),
                InstallResult("codex", "skipped", "config not found"),
            ]
        )
        assert s.overall_status == "installed"

    def test_overall_status_empty_summary_is_skipped(self):
        """An empty summary resolves to 'skipped' — the only surviving role of
        the final return now that InstallStatus is a Literal (PMSERV-054)."""
        assert InstallSummary().overall_status == "skipped"

    def test_status_priority_covers_all_statuses(self):
        """PMSERV-054: every InstallStatus member must have a priority slot, so
        overall_status can never fall through to the empty-summary sentinel for
        a *non-empty* summary. Guards against the Literal and _STATUS_PRIORITY
        drifting apart."""
        from typing import get_args

        from pm_server.installer import _STATUS_PRIORITY

        assert set(_STATUS_PRIORITY) == set(get_args(InstallStatus))
        assert len(_STATUS_PRIORITY) == len(get_args(InstallStatus))  # no dupes

    def test_install_result_is_dry_run_default_false(self):
        """is_dry_run defaults to False; positional construction stays compatible (PMSERV-039)."""
        r = InstallResult("claude-code", "installed", "msg")
        assert r.is_dry_run is False
        assert r.backup_path is None
        # Explicit dry-run construction is also supported.
        r2 = InstallResult("codex", "installed", "msg", backup_path=None, is_dry_run=True)
        assert r2.is_dry_run is True

    def test_install_result_lens_mode_default_false(self):
        """lens_mode defaults to False (PMSERV-087 / WF-026 FINDING-F)."""
        r = InstallResult("claude-code", "installed", "msg")
        assert r.lens_mode is False
        r2 = InstallResult("codex", "installed", "msg", lens_mode=True)
        assert r2.lens_mode is True


class TestLensModeActive:
    """PMSERV-087: ``_lens_mode_active`` reads PM_LENS env."""

    @pytest.mark.parametrize("truthy", ["1", "true", "True", "yes", "ON"])
    def test_truthy_values(self, truthy, monkeypatch):
        from pm_server.installer import _lens_mode_active

        monkeypatch.setenv("PM_LENS", truthy)
        assert _lens_mode_active() is True

    @pytest.mark.parametrize("falsy", ["0", "false", "no", "", "off"])
    def test_falsy_values(self, falsy, monkeypatch):
        from pm_server.installer import _lens_mode_active

        monkeypatch.setenv("PM_LENS", falsy)
        assert _lens_mode_active() is False

    def test_unset_is_false(self, monkeypatch):
        from pm_server.installer import _lens_mode_active

        monkeypatch.delenv("PM_LENS", raising=False)
        assert _lens_mode_active() is False


class TestInstallClaudeCodeLensMode:
    """PMSERV-087: install_claude_code propagates PM_LENS to ``claude mcp add``."""

    def test_lens_mode_injects_env_flag(self, monkeypatch):
        monkeypatch.setenv("PM_LENS", "1")

        def which(name):
            return f"/usr/bin/{name}"

        call_count = 0
        captured_cmds: list[list[str]] = []

        def mock_run(cmd, **kwargs):
            nonlocal call_count
            call_count += 1
            captured_cmds.append(list(cmd))
            return _make_result(1) if call_count == 1 else _make_result(0)

        with (
            patch("pm_server.installer.shutil.which", side_effect=which),
            patch("pm_server.installer.subprocess.run", side_effect=mock_run),
        ):
            r = install_claude_code()

        assert r.status == "installed"
        assert r.lens_mode is True
        assert "Lens" in r.message
        # The second subprocess call is `mcp add ...`; check --env present.
        add_cmd = captured_cmds[1]
        assert "--env" in add_cmd
        env_idx = add_cmd.index("--env")
        assert add_cmd[env_idx + 1] == "PM_LENS=1"

    def test_non_lens_mode_does_not_inject_env(self, monkeypatch):
        monkeypatch.delenv("PM_LENS", raising=False)

        def which(name):
            return f"/usr/bin/{name}"

        call_count = 0
        captured_cmds: list[list[str]] = []

        def mock_run(cmd, **kwargs):
            nonlocal call_count
            call_count += 1
            captured_cmds.append(list(cmd))
            return _make_result(1) if call_count == 1 else _make_result(0)

        with (
            patch("pm_server.installer.shutil.which", side_effect=which),
            patch("pm_server.installer.subprocess.run", side_effect=mock_run),
        ):
            r = install_claude_code()

        assert r.status == "installed"
        assert r.lens_mode is False
        assert "--env" not in captured_cmds[1]

    def test_lens_mode_dry_run_message_mentions_lens(self, monkeypatch):
        monkeypatch.setenv("PM_LENS", "1")

        def which(name):
            return f"/usr/bin/{name}"

        with (
            patch("pm_server.installer.shutil.which", side_effect=which),
            patch("pm_server.installer.subprocess.run", return_value=_make_result(1)),
        ):
            r = install_claude_code(dry_run=True)

        assert r.is_dry_run is True
        assert r.lens_mode is True
        assert "Lens" in r.message


class TestInstallCodexLensMode:
    """PMSERV-087: install_codex writes PM_LENS env into the TOML section."""

    def test_lens_mode_writes_env_table_on_fresh_install(
        self, fake_codex_config, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("PM_LENS", "1")
        fake_codex_config.write_text("", encoding="utf-8")
        monkeypatch.setattr(
            "pm_server.installer._resolve_pm_server_path", lambda: tmp_path / "pm-server"
        )

        r = install_codex()
        assert r.status == "installed"
        assert r.lens_mode is True
        assert "Lens" in r.message

        doc = tomlkit.parse(fake_codex_config.read_text(encoding="utf-8"))
        section = doc["mcp_servers"]["pm-server"]
        assert "env" in section
        assert str(section["env"]["PM_LENS"]) == "1"

    def test_non_lens_install_does_not_write_env_table(
        self, fake_codex_config, tmp_path, monkeypatch
    ):
        monkeypatch.delenv("PM_LENS", raising=False)
        fake_codex_config.write_text("", encoding="utf-8")
        monkeypatch.setattr(
            "pm_server.installer._resolve_pm_server_path", lambda: tmp_path / "pm-server"
        )

        r = install_codex()
        assert r.status == "installed"
        assert r.lens_mode is False

        doc = tomlkit.parse(fake_codex_config.read_text(encoding="utf-8"))
        section = doc["mcp_servers"]["pm-server"]
        assert "env" not in section

    def test_already_registered_when_lens_env_matches(
        self, fake_codex_config, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("PM_LENS", "1")
        pm_server_path = tmp_path / "pm-server"
        monkeypatch.setattr("pm_server.installer._resolve_pm_server_path", lambda: pm_server_path)
        fake_codex_config.write_text(
            textwrap.dedent(
                f"""
                [mcp_servers.pm-server]
                command = "{pm_server_path}"
                args = ["serve"]
                startup_timeout_sec = 30
                env = {{ PM_LENS = "1" }}
                """
            ).strip(),
            encoding="utf-8",
        )

        r = install_codex()
        assert r.status == "already_registered"
        assert r.lens_mode is True

    def test_reinstall_flips_rw_to_lens_by_adding_env(
        self, fake_codex_config, tmp_path, monkeypatch
    ):
        # Start: existing RW registration (no env). Run install with PM_LENS=1.
        # Expect: status=installed (mutation) + env.PM_LENS now "1".
        monkeypatch.setenv("PM_LENS", "1")
        pm_server_path = tmp_path / "pm-server"
        monkeypatch.setattr("pm_server.installer._resolve_pm_server_path", lambda: pm_server_path)
        fake_codex_config.write_text(
            textwrap.dedent(
                f"""
                [mcp_servers.pm-server]
                command = "{pm_server_path}"
                args = ["serve"]
                startup_timeout_sec = 30
                """
            ).strip(),
            encoding="utf-8",
        )

        r = install_codex()
        assert r.status == "installed"
        assert r.lens_mode is True

        doc = tomlkit.parse(fake_codex_config.read_text(encoding="utf-8"))
        section = doc["mcp_servers"]["pm-server"]
        assert str(section["env"]["PM_LENS"]) == "1"

    def test_reinstall_flips_lens_to_rw_by_removing_env(
        self, fake_codex_config, tmp_path, monkeypatch
    ):
        # Start: Lens-registered (env.PM_LENS=1). Run install with PM_LENS unset.
        # Expect: env.PM_LENS removed; if env table becomes empty, removed too.
        monkeypatch.delenv("PM_LENS", raising=False)
        pm_server_path = tmp_path / "pm-server"
        monkeypatch.setattr("pm_server.installer._resolve_pm_server_path", lambda: pm_server_path)
        fake_codex_config.write_text(
            textwrap.dedent(
                f"""
                [mcp_servers.pm-server]
                command = "{pm_server_path}"
                args = ["serve"]
                startup_timeout_sec = 30
                env = {{ PM_LENS = "1" }}
                """
            ).strip(),
            encoding="utf-8",
        )

        r = install_codex()
        assert r.status == "installed"
        assert r.lens_mode is False

        doc = tomlkit.parse(fake_codex_config.read_text(encoding="utf-8"))
        section = doc["mcp_servers"]["pm-server"]
        # Empty env table is collapsed
        assert "env" not in section

    def test_dry_run_lens_mode_message_mentions_lens(
        self, fake_codex_config, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("PM_LENS", "1")
        fake_codex_config.write_text("", encoding="utf-8")
        monkeypatch.setattr(
            "pm_server.installer._resolve_pm_server_path", lambda: tmp_path / "pm-server"
        )

        r = install_codex(dry_run=True)
        assert r.is_dry_run is True
        assert r.lens_mode is True
        assert "Lens" in r.message
        # File untouched
        assert fake_codex_config.read_text(encoding="utf-8") == ""


class TestResolvePmServerPath:
    """Absolute path resolution for sandbox-safe Codex registration (PMSERV-038)."""

    def test_uses_sys_executable_neighbor(self, tmp_path, monkeypatch):
        fake_python = tmp_path / "python"
        fake_pm_server = tmp_path / "pm-server"
        fake_pm_server.write_text("")
        monkeypatch.setattr("pm_server.installer.sys.executable", str(fake_python))
        from pm_server.installer import _resolve_pm_server_path

        path = _resolve_pm_server_path()
        assert path == fake_pm_server.resolve()

    def test_falls_back_to_shutil_which(self, tmp_path, monkeypatch):
        fake_python = tmp_path / "python"
        monkeypatch.setattr("pm_server.installer.sys.executable", str(fake_python))
        fallback = tmp_path / "fallback-pm-server"
        fallback.write_text("")
        monkeypatch.setattr(
            "pm_server.installer.shutil.which",
            lambda name: str(fallback) if name == "pm-server" else None,
        )
        from pm_server.installer import _resolve_pm_server_path

        path = _resolve_pm_server_path()
        assert path == fallback.resolve()

    def test_raises_when_not_found(self, tmp_path, monkeypatch):
        fake_python = tmp_path / "python"
        monkeypatch.setattr("pm_server.installer.sys.executable", str(fake_python))
        monkeypatch.setattr("pm_server.installer.shutil.which", lambda name: None)
        from pm_server.installer import _resolve_pm_server_path

        with pytest.raises(FileNotFoundError, match="pm-server binary not found"):
            _resolve_pm_server_path()


class TestInstallCodex:
    """install_codex against a tmp_path Codex config (PMSERV-038)."""

    @staticmethod
    def _make_pm_server_resolvable(tmp_path, monkeypatch):
        pm = tmp_path / "pm-server"
        pm.write_text("")
        resolved = pm.resolve()
        monkeypatch.setattr("pm_server.installer._resolve_pm_server_path", lambda: resolved)
        return resolved

    def test_skipped_when_config_not_found(self, fake_codex_config):
        result = install_codex()
        assert result.target == "codex"
        assert result.status == "skipped"
        assert "not found" in result.message.lower()
        assert result.backup_path is None

    def test_installed_when_section_new(self, fake_codex_config, tmp_path, monkeypatch):
        pm_path = self._make_pm_server_resolvable(tmp_path, monkeypatch)
        fake_codex_config.write_text(
            textwrap.dedent(
                """\
                [mcp_servers.filesystem]
                command = "npx"
                args = ["-y", "@modelcontextprotocol/server-filesystem"]
                """
            )
        )

        result = install_codex()
        assert result.status == "installed"
        assert "Codex" in result.message
        assert result.backup_path is not None
        doc = tomlkit.parse(fake_codex_config.read_text())
        assert str(doc["mcp_servers"]["pm-server"]["command"]) == str(pm_path)
        assert list(doc["mcp_servers"]["pm-server"]["args"]) == ["serve"]
        assert doc["mcp_servers"]["pm-server"]["startup_timeout_sec"] == 30
        assert "filesystem" in doc["mcp_servers"]

    def test_already_registered_when_command_matches(
        self, fake_codex_config, tmp_path, monkeypatch
    ):
        pm_path = self._make_pm_server_resolvable(tmp_path, monkeypatch)
        fake_codex_config.write_text(
            textwrap.dedent(
                f"""\
                [mcp_servers.pm-server]
                command = "{pm_path}"
                args = ["serve"]
                startup_timeout_sec = 30
                """
            )
        )

        result = install_codex()
        assert result.status == "already_registered"
        assert "already registered" in result.message.lower()
        # No mutation -> no backup created
        assert result.backup_path is None
        backups = list(fake_codex_config.parent.glob("config.toml.bak.*"))
        assert backups == []

    def test_installed_when_command_differs(self, fake_codex_config, tmp_path, monkeypatch):
        pm_path = self._make_pm_server_resolvable(tmp_path, monkeypatch)
        fake_codex_config.write_text(
            textwrap.dedent(
                """\
                [mcp_servers.pm-server]
                command = "/old/path/to/pm-server"
                args = ["serve"]
                startup_timeout_sec = 30
                """
            )
        )

        result = install_codex()
        assert result.status == "installed"
        doc = tomlkit.parse(fake_codex_config.read_text())
        assert str(doc["mcp_servers"]["pm-server"]["command"]) == str(pm_path)

    def test_preserves_subtables(self, fake_codex_config, tmp_path, monkeypatch):
        self._make_pm_server_resolvable(tmp_path, monkeypatch)
        fake_codex_config.write_text(
            textwrap.dedent(
                """\
                [mcp_servers.pm-server]
                command = "/old/path/to/pm-server"
                args = ["serve"]
                startup_timeout_sec = 30

                [mcp_servers.pm-server.tools.pm_init]
                approval_mode = "approve"

                [mcp_servers.pm-server.tools.pm_status]
                approval_mode = "approve"
                """
            )
        )

        result = install_codex()
        assert result.status == "installed"
        doc = tomlkit.parse(fake_codex_config.read_text())
        assert doc["mcp_servers"]["pm-server"]["tools"]["pm_init"]["approval_mode"] == "approve"
        assert doc["mcp_servers"]["pm-server"]["tools"]["pm_status"]["approval_mode"] == "approve"

    def test_preserves_comments_and_other_sections(self, fake_codex_config, tmp_path, monkeypatch):
        self._make_pm_server_resolvable(tmp_path, monkeypatch)
        fake_codex_config.write_text(
            textwrap.dedent(
                """\
                # Top-of-file comment
                [mcp_servers.filesystem]
                command = "npx"
                args = ["-y", "@modelcontextprotocol/server-filesystem"]

                # PM Server section comment
                [mcp_servers.pm-server]
                command = "/old/pm-server"
                args = ["serve"]
                startup_timeout_sec = 30
                """
            )
        )

        install_codex()
        new_text = fake_codex_config.read_text()
        assert "# Top-of-file comment" in new_text
        assert "# PM Server section comment" in new_text
        assert "[mcp_servers.filesystem]" in new_text

    def test_creates_backup(self, fake_codex_config, tmp_path, monkeypatch):
        self._make_pm_server_resolvable(tmp_path, monkeypatch)
        fake_codex_config.write_text('[mcp_servers.filesystem]\ncommand = "npx"\n')
        result = install_codex()
        assert result.backup_path is not None
        backup = Path(result.backup_path)
        assert backup.exists()
        assert backup.name.startswith("config.toml.bak.")
        assert "filesystem" in backup.read_text()

    def test_atomic_write_no_leftover_tmp(self, fake_codex_config, tmp_path, monkeypatch):
        self._make_pm_server_resolvable(tmp_path, monkeypatch)
        fake_codex_config.write_text('[mcp_servers.filesystem]\ncommand = "npx"\n')
        install_codex()
        tmp_file = fake_codex_config.with_name(fake_codex_config.name + ".tmp")
        assert not tmp_file.exists()
        doc = tomlkit.parse(fake_codex_config.read_text())
        assert "pm-server" in doc["mcp_servers"]

    def test_install_codex_twice_second_returns_already_registered(
        self, fake_codex_config, tmp_path, monkeypatch
    ):
        """Second install_codex returns already_registered without extra backup (PMSERV-040)."""
        self._make_pm_server_resolvable(tmp_path, monkeypatch)
        fake_codex_config.write_text(
            textwrap.dedent(
                """\
                [mcp_servers.filesystem]
                command = "npx"
                """
            )
        )

        first = install_codex()
        assert first.status == "installed"
        assert first.backup_path is not None

        second = install_codex()
        assert second.status == "already_registered"
        assert second.backup_path is None

        backups = list(fake_codex_config.parent.glob("config.toml.bak.*"))
        assert len(backups) == 1

    def test_install_codex_preserves_inline_comment_on_other_keys(
        self, fake_codex_config, tmp_path, monkeypatch
    ):
        """tomlkit inline comments on other-server keys survive round-trip (PMSERV-040)."""
        self._make_pm_server_resolvable(tmp_path, monkeypatch)
        fake_codex_config.write_text(
            textwrap.dedent(
                """\
                [mcp_servers.filesystem]
                command = "npx"  # the npx runtime
                args = ["-y", "@modelcontextprotocol/server-filesystem"]
                """
            )
        )

        result = install_codex()
        assert result.status == "installed"
        new_text = fake_codex_config.read_text()
        assert "# the npx runtime" in new_text

    def test_install_codex_when_mcp_servers_section_absent(
        self, fake_codex_config, tmp_path, monkeypatch
    ):
        """install_codex creates [mcp_servers] when the section itself is absent (PMSERV-040)."""
        pm_path = self._make_pm_server_resolvable(tmp_path, monkeypatch)
        fake_codex_config.write_text(
            textwrap.dedent(
                """\
                # User config without any mcp_servers
                model = "gpt-4o"
                """
            )
        )

        result = install_codex()
        assert result.status == "installed"
        doc = tomlkit.parse(fake_codex_config.read_text())
        assert "mcp_servers" in doc
        assert "pm-server" in doc["mcp_servers"]
        assert str(doc["mcp_servers"]["pm-server"]["command"]) == str(pm_path)
        assert str(doc["model"]) == "gpt-4o"

    def test_install_codex_existing_section_without_startup_timeout(
        self, fake_codex_config, tmp_path, monkeypatch
    ):
        """install_codex backfills startup_timeout_sec when missing (PMSERV-040, line 308)."""
        self._make_pm_server_resolvable(tmp_path, monkeypatch)
        fake_codex_config.write_text(
            textwrap.dedent(
                """\
                [mcp_servers.pm-server]
                command = "/old/pm-server"
                args = ["serve"]
                """
            )
        )

        result = install_codex()
        assert result.status == "installed"
        doc = tomlkit.parse(fake_codex_config.read_text())
        assert doc["mcp_servers"]["pm-server"]["startup_timeout_sec"] == 30

    def test_dry_run_does_not_create_backup_or_write_file(
        self, tmp_path, monkeypatch, fake_codex_config
    ):
        """dry-run skips _backup_codex_config and _atomic_write_toml (PMSERV-039)."""
        self._make_pm_server_resolvable(tmp_path, monkeypatch)
        original_content = "# unrelated\n[other]\nfoo = 1\n"
        fake_codex_config.write_text(original_content, encoding="utf-8")

        result = install_codex(dry_run=True)

        assert result.status == "installed"
        assert result.is_dry_run is True
        assert result.backup_path is None
        assert "would register" in result.message.lower()
        # File contents must not change.
        assert fake_codex_config.read_text(encoding="utf-8") == original_content
        # No backup file created.
        backups = list(fake_codex_config.parent.glob("config.toml.bak.*"))
        assert backups == []

    def test_dry_run_already_registered_returns_already_registered(
        self, tmp_path, monkeypatch, fake_codex_config
    ):
        """dry-run still recognizes the idempotent already_registered case (PMSERV-039)."""
        self._make_pm_server_resolvable(tmp_path, monkeypatch)
        pm_server_path = tmp_path / "pm-server"
        fake_codex_config.write_text(
            textwrap.dedent(
                f"""\
                [mcp_servers.pm-server]
                command = "{pm_server_path}"
                args = ["serve"]
                startup_timeout_sec = 30
                """
            ),
            encoding="utf-8",
        )

        result = install_codex(dry_run=True)
        assert result.status == "already_registered"
        assert result.is_dry_run is True
        assert result.backup_path is None

    def test_dry_run_when_command_differs_reports_would_update(
        self, tmp_path, monkeypatch, fake_codex_config
    ):
        """dry-run hits the 'command differs' branch with a 'would update' message (PMSERV-039)."""
        self._make_pm_server_resolvable(tmp_path, monkeypatch)
        # Existing section with a stale command path forces the update branch.
        fake_codex_config.write_text(
            textwrap.dedent(
                """\
                [mcp_servers.pm-server]
                command = "/old/pm-server"
                args = ["serve"]
                startup_timeout_sec = 30
                """
            ),
            encoding="utf-8",
        )

        result = install_codex(dry_run=True)

        assert result.status == "installed"
        assert result.is_dry_run is True
        assert result.backup_path is None
        assert "would update" in result.message.lower()


class TestUninstallCodex:
    """uninstall_codex against a tmp_path Codex config (PMSERV-038)."""

    def test_skipped_when_config_not_found(self, fake_codex_config):
        result = uninstall_codex()
        assert result.status == "skipped"
        assert "not found" in result.message.lower()
        assert result.backup_path is None

    def test_skipped_when_pm_server_not_registered(self, fake_codex_config):
        fake_codex_config.write_text(
            textwrap.dedent(
                """\
                [mcp_servers.filesystem]
                command = "npx"
                """
            )
        )
        result = uninstall_codex()
        assert result.status == "skipped"
        assert "not registered" in result.message.lower()
        assert result.backup_path is None

    def test_full_removal_when_no_subtables(self, fake_codex_config):
        fake_codex_config.write_text(
            textwrap.dedent(
                """\
                [mcp_servers.pm-server]
                command = "/some/pm-server"
                args = ["serve"]
                startup_timeout_sec = 30
                """
            )
        )
        result = uninstall_codex()
        assert result.status == "uninstalled"
        assert result.backup_path is not None
        doc = tomlkit.parse(fake_codex_config.read_text())
        if "mcp_servers" in doc:
            assert "pm-server" not in doc["mcp_servers"]

    def test_preserves_subtables_with_warning(self, fake_codex_config):
        fake_codex_config.write_text(
            textwrap.dedent(
                """\
                [mcp_servers.pm-server]
                command = "/some/pm-server"
                args = ["serve"]
                startup_timeout_sec = 30

                [mcp_servers.pm-server.tools.pm_init]
                approval_mode = "approve"
                """
            )
        )
        result = uninstall_codex()
        assert result.status == "uninstalled"
        assert "preserved" in result.message.lower() or "manually" in result.message.lower()
        doc = tomlkit.parse(fake_codex_config.read_text())
        section = doc["mcp_servers"]["pm-server"]
        assert "command" not in section
        assert "args" not in section
        assert "startup_timeout_sec" not in section
        assert section["tools"]["pm_init"]["approval_mode"] == "approve"

    def test_creates_backup_when_mutating(self, fake_codex_config):
        fake_codex_config.write_text(
            textwrap.dedent(
                """\
                [mcp_servers.pm-server]
                command = "/some/pm-server"
                args = ["serve"]
                """
            )
        )
        result = uninstall_codex()
        assert result.backup_path is not None
        backup = Path(result.backup_path)
        assert backup.exists()
        assert "pm-server" in backup.read_text()

    def test_uninstall_codex_twice_second_returns_skipped(self, fake_codex_config):
        """Second uninstall_codex skips when pm-server not registered (PMSERV-040)."""
        fake_codex_config.write_text(
            textwrap.dedent(
                """\
                [mcp_servers.pm-server]
                command = "/some/pm-server"
                args = ["serve"]
                """
            )
        )

        first = uninstall_codex()
        assert first.status == "uninstalled"

        second = uninstall_codex()
        assert second.status == "skipped"
        assert "not registered" in second.message.lower()
        assert second.backup_path is None

    def test_uninstall_codex_preserves_top_of_file_comment(self, fake_codex_config):
        """uninstall_codex preserves a top-of-file comment after mutation (PMSERV-040)."""
        fake_codex_config.write_text(
            textwrap.dedent(
                """\
                # Top-of-file comment
                [mcp_servers.pm-server]
                command = "/some/pm-server"
                args = ["serve"]
                """
            )
        )

        uninstall_codex()
        new_text = fake_codex_config.read_text()
        assert "# Top-of-file comment" in new_text

    def test_uninstall_codex_preserves_other_section_comments(self, fake_codex_config):
        """uninstall_codex preserves comments tied to unrelated server sections (PMSERV-040)."""
        fake_codex_config.write_text(
            textwrap.dedent(
                """\
                # Filesystem MCP server
                [mcp_servers.filesystem]
                command = "npx"
                args = ["-y", "@modelcontextprotocol/server-filesystem"]

                [mcp_servers.pm-server]
                command = "/some/pm-server"
                args = ["serve"]
                """
            )
        )

        uninstall_codex()
        new_text = fake_codex_config.read_text()
        assert "# Filesystem MCP server" in new_text
        assert "[mcp_servers.filesystem]" in new_text

    def test_dry_run_does_not_create_backup_or_write_file(self, fake_codex_config):
        """dry-run uninstall skips _backup_codex_config and _atomic_write_toml (PMSERV-039)."""
        original_content = textwrap.dedent(
            """\
            [mcp_servers.pm-server]
            command = "/usr/bin/pm-server"
            args = ["serve"]
            startup_timeout_sec = 30
            """
        )
        fake_codex_config.write_text(original_content, encoding="utf-8")

        result = uninstall_codex(dry_run=True)

        assert result.status == "uninstalled"
        assert result.is_dry_run is True
        assert result.backup_path is None
        assert "would unregister" in result.message.lower()
        # File contents must not change.
        assert fake_codex_config.read_text(encoding="utf-8") == original_content
        # No backup file created.
        backups = list(fake_codex_config.parent.glob("config.toml.bak.*"))
        assert backups == []

    def test_dry_run_when_subtables_exist_preserves_them_in_message(self, fake_codex_config):
        """dry-run hits the sub-tables-preserved branch with the right message (PMSERV-039)."""
        original_content = textwrap.dedent(
            """\
            [mcp_servers.pm-server]
            command = "/usr/bin/pm-server"
            args = ["serve"]
            startup_timeout_sec = 30

            [mcp_servers.pm-server.tools.pm_init]
            approval_mode = "never"
            """
        )
        fake_codex_config.write_text(original_content, encoding="utf-8")

        result = uninstall_codex(dry_run=True)

        assert result.status == "uninstalled"
        assert result.is_dry_run is True
        assert result.backup_path is None
        assert "would remove pm server top-level fields" in result.message.lower()
        assert "sub-tables would be preserved" in result.message.lower()
        # Sub-tables and top-level fields untouched on disk.
        assert fake_codex_config.read_text(encoding="utf-8") == original_content


class TestCodexLifecycle:
    """install -> uninstall -> install roundtrip state-transition coverage (PMSERV-040)."""

    def test_install_uninstall_install_codex_roundtrip(
        self, fake_codex_config, tmp_path, monkeypatch
    ):
        pm_path = TestInstallCodex._make_pm_server_resolvable(tmp_path, monkeypatch)
        fake_codex_config.write_text(
            textwrap.dedent(
                """\
                [mcp_servers.filesystem]
                command = "npx"
                """
            )
        )

        r1 = install_codex()
        assert r1.status == "installed"
        doc = tomlkit.parse(fake_codex_config.read_text())
        assert "pm-server" in doc["mcp_servers"]

        r2 = uninstall_codex()
        assert r2.status == "uninstalled"
        doc = tomlkit.parse(fake_codex_config.read_text())
        assert "pm-server" not in doc.get("mcp_servers", {})
        assert "filesystem" in doc.get("mcp_servers", {})

        r3 = install_codex()
        assert r3.status == "installed"
        doc = tomlkit.parse(fake_codex_config.read_text())
        assert str(doc["mcp_servers"]["pm-server"]["command"]) == str(pm_path)
        assert "filesystem" in doc["mcp_servers"]

        backups = list(fake_codex_config.parent.glob("config.toml.bak.*"))
        assert len(backups) >= 1


class TestCliInstallation:
    """CLI ↔ orchestrator wiring (PMSERV-039).

    These tests use ``click.testing.CliRunner`` and patch
    ``pm_server.installer.install`` / ``...uninstall`` directly so the
    test exercises only the CLI surface (option parsing, exit code,
    output rendering) — the orchestrator's behavior is covered by the
    ``TestInstallOrchestrator`` class. The ``__main__`` module imports
    ``installer`` as a module attribute (``from . import installer``)
    so module-level patching reliably intercepts the call.
    """

    @staticmethod
    def _ok_summary_single_host(target: str = "claude-code") -> InstallSummary:
        return InstallSummary(results=[InstallResult(target, "installed", "PM Server registered")])

    @staticmethod
    def _ok_summary_all_hosts() -> InstallSummary:
        return InstallSummary(
            results=[
                InstallResult("claude-code", "installed", "registered"),
                InstallResult("codex", "skipped", "config not found"),
            ]
        )

    def test_install_no_flags_dispatches_with_claude_code_target(self, monkeypatch):
        from click.testing import CliRunner

        from pm_server.__main__ import cli

        captured = {}

        def fake_install(target: str = "claude-code", *, dry_run: bool = False):
            captured["target"] = target
            captured["dry_run"] = dry_run
            return self._ok_summary_single_host("claude-code")

        monkeypatch.setattr("pm_server.installer.install", fake_install)
        result = CliRunner().invoke(cli, ["install"])

        assert result.exit_code == 0
        assert captured == {"target": "claude-code", "dry_run": False}
        # Backward-compat: target prefix added but everything else preserved.
        assert "✓ claude-code: PM Server registered" in result.output

    def test_install_target_all_renders_hosts_in_known_order(self, monkeypatch):
        from click.testing import CliRunner

        from pm_server.__main__ import cli

        def fake_install(target: str = "claude-code", *, dry_run: bool = False):
            assert target == "all"
            return self._ok_summary_all_hosts()

        monkeypatch.setattr("pm_server.installer.install", fake_install)
        result = CliRunner().invoke(cli, ["install", "--target", "all"])

        assert result.exit_code == 0
        # Order: claude-code line precedes codex line.
        cc_idx = result.output.index("claude-code:")
        codex_idx = result.output.index("codex:")
        assert cc_idx < codex_idx

    def test_install_dry_run_flag_propagates_and_outputs_dry_run_tag(self, monkeypatch):
        from click.testing import CliRunner

        from pm_server.__main__ import cli

        captured = {}

        def fake_install(target: str = "claude-code", *, dry_run: bool = False):
            captured["dry_run"] = dry_run
            # Per B-1 contract: per-host messages do NOT embed the [dry-run]
            # tag. The CLI layer adds it exactly once via _print_install_summary
            # so the rendered line stays free of double-tagging.
            return InstallSummary(
                results=[
                    InstallResult(
                        "claude-code",
                        "installed",
                        "would register PM Server in Claude Code (user scope).",
                        is_dry_run=True,
                    )
                ]
            )

        monkeypatch.setattr("pm_server.installer.install", fake_install)
        result = CliRunner().invoke(cli, ["install", "--dry-run"])

        assert result.exit_code == 0
        assert captured["dry_run"] is True
        assert "[dry-run] claude-code:" in result.output
        # Double-tagging regression guard: the rendered line must contain
        # exactly one occurrence of "[dry-run]".
        assert result.output.count("[dry-run]") == 1

    def test_install_failed_result_exits_non_zero(self, monkeypatch):
        from click.testing import CliRunner

        from pm_server.__main__ import cli

        def fake_install(target: str = "claude-code", *, dry_run: bool = False):
            return InstallSummary(results=[InstallResult("claude-code", "failed", "boom")])

        monkeypatch.setattr("pm_server.installer.install", fake_install)
        result = CliRunner().invoke(cli, ["install"])

        assert result.exit_code == 1
        assert "✗ claude-code: boom" in result.output

    def test_install_target_all_both_hosts_failed_exits_non_zero(self, monkeypatch):
        """Both-host failure with --target all still surfaces exit code 1 (PMSERV-039 / S2)."""
        from click.testing import CliRunner

        from pm_server.__main__ import cli

        def fake_install(target: str = "claude-code", *, dry_run: bool = False):
            return InstallSummary(
                results=[
                    InstallResult("claude-code", "failed", "kaboom-cc"),
                    InstallResult("codex", "failed", "kaboom-codex"),
                ]
            )

        monkeypatch.setattr("pm_server.installer.install", fake_install)
        result = CliRunner().invoke(cli, ["install", "--target", "all"])

        assert result.exit_code == 1
        assert "✗ claude-code:" in result.output
        assert "✗ codex:" in result.output

    def test_uninstall_no_flags_dispatches_with_claude_code_target(self, monkeypatch):
        from click.testing import CliRunner

        from pm_server.__main__ import cli

        captured = {}

        def fake_uninstall(target: str = "claude-code", *, dry_run: bool = False):
            captured["target"] = target
            captured["dry_run"] = dry_run
            return InstallSummary(
                results=[InstallResult("claude-code", "uninstalled", "PM Server unregistered")]
            )

        monkeypatch.setattr("pm_server.installer.uninstall", fake_uninstall)
        result = CliRunner().invoke(cli, ["uninstall"])

        assert result.exit_code == 0
        assert captured == {"target": "claude-code", "dry_run": False}
        assert "✓ claude-code: PM Server unregistered" in result.output


# ─── PMSERV-100 / ADR-019 — PM_DESKTOP_WRITE propagation ───────────


class TestDesktopWriteModeActive:
    """PMSERV-100: _desktop_write_mode_active reads PM_DESKTOP_WRITE env."""

    @pytest.mark.parametrize("truthy", ["1", "true", "True", "yes", "ON"])
    def test_truthy_values(self, truthy, monkeypatch):
        from pm_server.installer import _desktop_write_mode_active

        monkeypatch.setenv("PM_DESKTOP_WRITE", truthy)
        assert _desktop_write_mode_active() is True

    @pytest.mark.parametrize("falsy", ["0", "false", "no", "", "off"])
    def test_falsy_values(self, falsy, monkeypatch):
        from pm_server.installer import _desktop_write_mode_active

        monkeypatch.setenv("PM_DESKTOP_WRITE", falsy)
        assert _desktop_write_mode_active() is False

    def test_unset_is_false(self, monkeypatch):
        from pm_server.installer import _desktop_write_mode_active

        monkeypatch.delenv("PM_DESKTOP_WRITE", raising=False)
        assert _desktop_write_mode_active() is False


class TestInstallClaudeCodeDesktopWritePropagation:
    """PMSERV-100: install_claude_code propagates PM_DESKTOP_WRITE to `claude mcp add`."""

    def test_desktop_write_alone_injects_env_flag(self, monkeypatch):
        monkeypatch.delenv("PM_LENS", raising=False)
        monkeypatch.setenv("PM_DESKTOP_WRITE", "1")

        def which(name):
            return f"/usr/bin/{name}"

        call_count = 0
        captured_cmds: list[list[str]] = []

        def mock_run(cmd, **kwargs):
            nonlocal call_count
            call_count += 1
            captured_cmds.append(list(cmd))
            return _make_result(1) if call_count == 1 else _make_result(0)

        with (
            patch("pm_server.installer.shutil.which", side_effect=which),
            patch("pm_server.installer.subprocess.run", side_effect=mock_run),
        ):
            r = install_claude_code()

        assert r.status == "installed"
        add_cmd = captured_cmds[1]
        # PM_DESKTOP_WRITE=1 must appear as a --env pair.
        env_pairs = [add_cmd[i + 1] for i, tok in enumerate(add_cmd) if tok == "--env"]
        assert "PM_DESKTOP_WRITE=1" in env_pairs

    def test_lens_plus_desktop_write_injects_both(self, monkeypatch):
        monkeypatch.setenv("PM_LENS", "1")
        monkeypatch.setenv("PM_DESKTOP_WRITE", "1")

        def which(name):
            return f"/usr/bin/{name}"

        call_count = 0
        captured_cmds: list[list[str]] = []

        def mock_run(cmd, **kwargs):
            nonlocal call_count
            call_count += 1
            captured_cmds.append(list(cmd))
            return _make_result(1) if call_count == 1 else _make_result(0)

        with (
            patch("pm_server.installer.shutil.which", side_effect=which),
            patch("pm_server.installer.subprocess.run", side_effect=mock_run),
        ):
            r = install_claude_code()

        assert r.status == "installed"
        assert r.lens_mode is True
        add_cmd = captured_cmds[1]
        env_pairs = [add_cmd[i + 1] for i, tok in enumerate(add_cmd) if tok == "--env"]
        assert "PM_LENS=1" in env_pairs
        assert "PM_DESKTOP_WRITE=1" in env_pairs

    def test_desktop_write_unset_omits_env(self, monkeypatch):
        monkeypatch.delenv("PM_LENS", raising=False)
        monkeypatch.delenv("PM_DESKTOP_WRITE", raising=False)

        def which(name):
            return f"/usr/bin/{name}"

        call_count = 0
        captured_cmds: list[list[str]] = []

        def mock_run(cmd, **kwargs):
            nonlocal call_count
            call_count += 1
            captured_cmds.append(list(cmd))
            return _make_result(1) if call_count == 1 else _make_result(0)

        with (
            patch("pm_server.installer.shutil.which", side_effect=which),
            patch("pm_server.installer.subprocess.run", side_effect=mock_run),
        ):
            install_claude_code()

        assert "--env" not in captured_cmds[1]


class TestInstallCodexDesktopWritePropagation:
    """PMSERV-100: install_codex writes PM_DESKTOP_WRITE into TOML env table."""

    def test_desktop_write_writes_env_table_on_fresh_install(
        self, fake_codex_config, tmp_path, monkeypatch
    ):
        monkeypatch.delenv("PM_LENS", raising=False)
        monkeypatch.setenv("PM_DESKTOP_WRITE", "1")
        fake_codex_config.write_text("", encoding="utf-8")
        monkeypatch.setattr(
            "pm_server.installer._resolve_pm_server_path", lambda: tmp_path / "pm-server"
        )

        r = install_codex()
        assert r.status == "installed"
        config_text = fake_codex_config.read_text(encoding="utf-8")
        assert "PM_DESKTOP_WRITE" in config_text
        assert '"1"' in config_text  # value is quoted as string

    def test_lens_plus_desktop_write_both_in_env_table(
        self, fake_codex_config, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("PM_LENS", "1")
        monkeypatch.setenv("PM_DESKTOP_WRITE", "1")
        fake_codex_config.write_text("", encoding="utf-8")
        monkeypatch.setattr(
            "pm_server.installer._resolve_pm_server_path", lambda: tmp_path / "pm-server"
        )

        r = install_codex()
        assert r.status == "installed"
        assert r.lens_mode is True
        config_text = fake_codex_config.read_text(encoding="utf-8")
        assert "PM_LENS" in config_text
        assert "PM_DESKTOP_WRITE" in config_text

    def test_reinstall_drops_stale_desktop_write_when_unset(
        self, fake_codex_config, tmp_path, monkeypatch
    ):
        """Reinstall without PM_DESKTOP_WRITE must strip a previously-written key
        so the spawned server starts in the requested mode (no stale env)."""
        # Seed an existing registration that already has PM_DESKTOP_WRITE=1.
        seed = (
            "[mcp_servers.pm-server]\n"
            f'command = "{tmp_path / "pm-server"}"\n'
            'args = ["serve"]\n'
            "startup_timeout_sec = 30\n"
            'env = { PM_DESKTOP_WRITE = "1" }\n'
        )
        fake_codex_config.write_text(seed, encoding="utf-8")
        monkeypatch.delenv("PM_LENS", raising=False)
        monkeypatch.delenv("PM_DESKTOP_WRITE", raising=False)
        monkeypatch.setattr(
            "pm_server.installer._resolve_pm_server_path", lambda: tmp_path / "pm-server"
        )

        r = install_codex()
        assert r.status == "installed"
        config_text = fake_codex_config.read_text(encoding="utf-8")
        assert "PM_DESKTOP_WRITE" not in config_text
