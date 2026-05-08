"""Tests for utils.py: project-path resolution + shared host/file helpers.

The host-target / file-mutation helpers (added in PMSERV-044) are consumed
by both ``installer.py`` and ``rules.py``. Their tests pin the contract so
that either consumer's refactor cannot silently break the other.
"""

import os

import pytest

from pm_server.models import ProjectNotFoundError
from pm_server.utils import (
    _KNOWN_HOSTS,
    TARGET_CHOICES,
    _atomic_write_text,
    _codex_config_path,
    _is_project_pm_dir,
    _resolve_targets,
    _timestamped_backup,
    get_utils_fingerprint,
    resolve_project_path,
)


class TestIsProjectPmDir:
    """Tests for _is_project_pm_dir helper."""

    def test_valid_project_pm_dir(self, tmp_path):
        pm = tmp_path / ".pm"
        pm.mkdir()
        (pm / "project.yaml").write_text("name: test\n")
        assert _is_project_pm_dir(pm) is True

    def test_global_pm_dir_without_project_yaml(self, tmp_path):
        """A .pm/ with registry.yaml but no project.yaml is NOT a project."""
        pm = tmp_path / ".pm"
        pm.mkdir()
        (pm / "registry.yaml").write_text("projects: []\n")
        assert _is_project_pm_dir(pm) is False

    def test_nonexistent_dir(self, tmp_path):
        pm = tmp_path / ".pm"
        assert _is_project_pm_dir(pm) is False

    def test_file_not_dir(self, tmp_path):
        pm = tmp_path / ".pm"
        pm.write_text("not a directory")
        assert _is_project_pm_dir(pm) is False


class TestResolveProjectPathExplicit:
    """Tests for explicit project_path argument (priority 1)."""

    def test_explicit_path_with_pm_dir(self, tmp_path):
        (tmp_path / ".pm").mkdir()
        result = resolve_project_path(str(tmp_path))
        assert result == tmp_path.resolve()

    def test_explicit_path_without_pm_dir(self, tmp_path):
        with pytest.raises(ProjectNotFoundError, match="No .pm/ directory found at"):
            resolve_project_path(str(tmp_path))


class TestResolveProjectPathEnvVar:
    """Tests for PM_PROJECT_PATH env var (priority 2)."""

    def test_env_var_with_pm_dir(self, tmp_path, monkeypatch):
        (tmp_path / ".pm").mkdir()
        monkeypatch.setenv("PM_PROJECT_PATH", str(tmp_path))
        result = resolve_project_path()
        assert result == tmp_path.resolve()

    def test_env_var_without_pm_dir_falls_through(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PM_PROJECT_PATH", str(tmp_path))
        monkeypatch.chdir(tmp_path)  # ensure cwd walk-up also finds nothing
        with pytest.raises(ProjectNotFoundError):
            resolve_project_path()


class TestResolveProjectPathCwdWalkUp:
    """Tests for cwd walk-up (priority 3) — the bug fix target."""

    def test_finds_project_in_cwd(self, tmp_path, monkeypatch):
        pm = tmp_path / ".pm"
        pm.mkdir()
        (pm / "project.yaml").write_text("name: test\n")
        monkeypatch.chdir(tmp_path)

        result = resolve_project_path()
        assert result == tmp_path.resolve()

    def test_finds_project_in_parent(self, tmp_path, monkeypatch):
        pm = tmp_path / ".pm"
        pm.mkdir()
        (pm / "project.yaml").write_text("name: test\n")
        child = tmp_path / "src" / "module"
        child.mkdir(parents=True)
        monkeypatch.chdir(child)

        result = resolve_project_path()
        assert result == tmp_path.resolve()

    def test_skips_global_pm_dir(self, tmp_path, monkeypatch):
        """Core bug fix: .pm/ with registry.yaml but no project.yaml is skipped."""
        home = tmp_path / "fakehome"
        home.mkdir()
        global_pm = home / ".pm"
        global_pm.mkdir()
        (global_pm / "registry.yaml").write_text("projects: []\n")
        (global_pm / "memory.db").write_bytes(b"")

        workdir = home / "projects" / "myproject"
        workdir.mkdir(parents=True)
        monkeypatch.chdir(workdir)

        with pytest.raises(ProjectNotFoundError):
            resolve_project_path()

    def test_skips_global_but_finds_project_below(self, tmp_path, monkeypatch):
        """Walks up past global-only .pm/, finds real project .pm/ lower in tree."""
        home = tmp_path / "fakehome"
        home.mkdir()
        global_pm = home / ".pm"
        global_pm.mkdir()
        (global_pm / "registry.yaml").write_text("projects: []\n")

        project_root = home / "projects" / "myproj"
        project_root.mkdir(parents=True)
        project_pm = project_root / ".pm"
        project_pm.mkdir()
        (project_pm / "project.yaml").write_text("name: myproj\n")

        src = project_root / "src"
        src.mkdir()
        monkeypatch.chdir(src)

        result = resolve_project_path()
        assert result == project_root.resolve()

    def test_home_with_intentional_pm_init(self, tmp_path, monkeypatch):
        """If user ran pm_init at home (project.yaml exists), it works."""
        home = tmp_path / "fakehome"
        home.mkdir()
        pm = home / ".pm"
        pm.mkdir()
        (pm / "project.yaml").write_text("name: home-project\n")
        (pm / "registry.yaml").write_text("projects: []\n")
        monkeypatch.chdir(home)

        result = resolve_project_path()
        assert result == home.resolve()

    def test_no_pm_dir_anywhere(self, tmp_path, monkeypatch):
        workdir = tmp_path / "empty" / "project"
        workdir.mkdir(parents=True)
        monkeypatch.chdir(workdir)

        with pytest.raises(ProjectNotFoundError, match="No .pm/ directory found"):
            resolve_project_path()


class TestKnownHosts:
    def test_order_is_significant(self):
        """``claude-code`` must come first — orchestrator dispatch order."""
        assert _KNOWN_HOSTS == ("claude-code", "codex")

    def test_target_choices_is_auto_all_then_known_hosts(self):
        assert TARGET_CHOICES == ("auto", "all", "claude-code", "codex")


class TestResolveTargets:
    def test_auto_expands_to_all_known_hosts(self):
        assert _resolve_targets("auto") == ["claude-code", "codex"]

    def test_all_is_synonym_of_auto(self):
        assert _resolve_targets("all") == _resolve_targets("auto")

    def test_specific_host_returns_singleton(self):
        assert _resolve_targets("claude-code") == ["claude-code"]
        assert _resolve_targets("codex") == ["codex"]

    def test_unknown_target_raises(self):
        with pytest.raises(ValueError, match="unknown target"):
            _resolve_targets("invalid")


class TestCodexConfigPath:
    def test_returns_codex_config_under_home(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HOME", str(tmp_path))
        assert _codex_config_path() == tmp_path / ".codex" / "config.toml"


class TestTimestampedBackup:
    def test_creates_backup_next_to_original(self, tmp_path):
        original = tmp_path / "config.toml"
        original.write_text('hello = "world"\n', encoding="utf-8")

        backup = _timestamped_backup(original)

        assert backup.parent == original.parent
        assert backup.name.startswith("config.toml.bak.")

    def test_preserves_content(self, tmp_path):
        original = tmp_path / "data.txt"
        original.write_text("line1\nline2\n", encoding="utf-8")

        backup = _timestamped_backup(original)

        assert backup.read_text(encoding="utf-8") == "line1\nline2\n"

    def test_preserves_mtime(self, tmp_path):
        """``shutil.copy2`` semantics: mtime is preserved."""
        original = tmp_path / "data.txt"
        original.write_text("x", encoding="utf-8")
        os.utime(original, (1_700_000_000, 1_700_000_000))

        backup = _timestamped_backup(original)

        assert backup.stat().st_mtime == original.stat().st_mtime


class TestAtomicWriteText:
    def test_writes_content_correctly(self, tmp_path):
        target = tmp_path / "out.txt"

        _atomic_write_text(target, "hello\n")

        assert target.read_text(encoding="utf-8") == "hello\n"

    def test_replaces_existing_file(self, tmp_path):
        target = tmp_path / "out.txt"
        target.write_text("old\n", encoding="utf-8")

        _atomic_write_text(target, "new\n")

        assert target.read_text(encoding="utf-8") == "new\n"

    def test_does_not_collide_with_fixed_tmp_suffix(self, tmp_path):
        """PMSERV-044 cross-check R8: ``mkstemp`` uses a randomised name so
        a pre-existing fixed-suffix ``.tmp`` file beside the target is left
        untouched (no concurrent-writer collision)."""
        target = tmp_path / "out.toml"
        target.write_text("initial", encoding="utf-8")

        # Squat on the legacy fixed-suffix path that the previous
        # implementation would have used.
        legacy_fixed_tmp = tmp_path / "out.toml.tmp"
        legacy_fixed_tmp.write_text("foreign-content", encoding="utf-8")

        _atomic_write_text(target, "new content\n")

        assert legacy_fixed_tmp.read_text(encoding="utf-8") == "foreign-content"
        assert target.read_text(encoding="utf-8") == "new content\n"

    def test_cleans_up_temp_on_exception(self, tmp_path, monkeypatch):
        """A failed rename must not leave a ``*.tmp`` orphan in the dir."""
        target = tmp_path / "out.txt"

        def failing_replace(src, dst):
            raise OSError("simulated replace failure")

        monkeypatch.setattr(os, "replace", failing_replace)

        with pytest.raises(OSError, match="simulated replace failure"):
            _atomic_write_text(target, "data")

        leftover = list(tmp_path.glob("*.tmp*"))
        assert leftover == []

    def test_supports_custom_encoding(self, tmp_path):
        target = tmp_path / "out.txt"

        _atomic_write_text(target, "héllo", encoding="utf-8")

        assert target.read_text(encoding="utf-8") == "héllo"

    def test_new_file_uses_umask_aware_permissions_not_mkstemp_default(self, tmp_path):
        """PMSERV-044 real-environment smoke regression: ``mkstemp``
        defaults to mode 0600 (security default). New rule files must
        follow ``open(...,"w")`` semantics (``0o666 & ~umask``) so they
        are not accidentally owner-only."""
        target = tmp_path / "AGENTS.md"

        _atomic_write_text(target, "rules\n")

        # Recompute the expected mode the same way the implementation does
        current_umask = os.umask(0)
        os.umask(current_umask)
        expected_mode = 0o666 & ~current_umask

        actual_mode = target.stat().st_mode & 0o777
        assert actual_mode == expected_mode, (
            f"new file should have umask-aware mode 0o{expected_mode:o}, "
            f"got 0o{actual_mode:o} (mkstemp default 0o600 leaked)"
        )

    def test_existing_file_inherits_its_own_permissions(self, tmp_path):
        """When the target already exists, atomic-write must preserve the
        existing mode rather than imposing a default — so a user who
        ``chmod 600`` an existing file keeps that lock."""
        target = tmp_path / "secret.toml"
        target.write_text("[s]\nk='v'\n", encoding="utf-8")
        os.chmod(target, 0o600)

        _atomic_write_text(target, "[s]\nk='w'\n")

        assert target.stat().st_mode & 0o777 == 0o600


class TestGetUtilsFingerprint:
    """Stale-module-cache detection helper (PMSERV-060)."""

    def test_returns_dict_with_expected_keys(self):
        fp = get_utils_fingerprint()
        assert set(fp.keys()) == {"loaded", "current", "stale", "path"}

    def test_loaded_and_current_match_when_unchanged(self):
        fp = get_utils_fingerprint()
        # Process just imported utils.py; disk hasn't been touched.
        assert fp["loaded"] == fp["current"]
        assert fp["stale"] is False

    def test_fingerprint_is_8_char_lowercase_hex(self):
        fp = get_utils_fingerprint()
        for key in ("loaded", "current"):
            value = fp[key]
            assert len(value) == 8, f"{key} should be 8 chars, got {len(value)}"
            assert all(c in "0123456789abcdef" for c in value), f"{key} should be lowercase hex"

    def test_path_resolves_to_utils_module(self):
        fp = get_utils_fingerprint()
        assert fp["path"].endswith("utils.py")

    def test_stale_detected_when_disk_diverges(self, tmp_path, monkeypatch):
        """Simulate a source edit by pointing the helper at a modified file."""
        from pm_server import utils as _utils

        # Write a file that the helper will read instead of the real utils.py
        fake_path = tmp_path / "utils.py"
        fake_path.write_text("# diverged source\n", encoding="utf-8")

        # Override the module __file__ so the disk-side recompute reads our
        # fake file. The cached _UTILS_FINGERPRINT (computed at real import
        # time) stays untouched, so loaded vs current must differ.
        monkeypatch.setattr(_utils, "__file__", str(fake_path))

        fp = get_utils_fingerprint()
        assert fp["loaded"] != fp["current"]
        assert fp["stale"] is True

    def test_unreadable_disk_returns_unreadable_marker(self, monkeypatch):
        """If the source file vanishes (unlikely but defensive), surface it."""
        from pm_server import utils as _utils

        monkeypatch.setattr(_utils, "__file__", "/nonexistent/path/utils.py")
        fp = get_utils_fingerprint()
        assert fp["current"] == "unreadable"
        # ``stale`` must be False when we cannot read — avoids false alarms
        assert fp["stale"] is False
