"""Tests for project discovery and info detection."""

import json

from pm_server.discovery import detect_project_info, discover_projects


class TestDetectProjectInfo:
    def test_bare_directory(self, tmp_path):
        info = detect_project_info(tmp_path)
        assert info["name"] == tmp_path.name
        assert info["version"] == "0.1.0"

    def test_pyproject_toml(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "my-pkg"\nversion = "2.0.0"\ndescription = "A package"\n'
        )
        info = detect_project_info(tmp_path)
        assert info["name"] == "my-pkg"
        assert info["version"] == "2.0.0"
        assert info["description"] == "A package"

    def test_package_json(self, tmp_path):
        (tmp_path / "package.json").write_text(
            json.dumps({"name": "my-app", "version": "3.1.0", "description": "A JS app"})
        )
        info = detect_project_info(tmp_path)
        assert info["name"] == "my-app"
        assert info["version"] == "3.1.0"

    def test_cargo_toml(self, tmp_path):
        (tmp_path / "Cargo.toml").write_bytes(b'[package]\nname = "rvim"\nversion = "0.2.0"\n')
        info = detect_project_info(tmp_path)
        assert info["name"] == "rvim"
        assert info["version"] == "0.2.0"

    def test_readme_fallback(self, tmp_path):
        (tmp_path / "README.md").write_text(
            "# My Project\n\nThis is a longer description of the project.\n"
        )
        info = detect_project_info(tmp_path)
        assert "longer description" in info["description"]

    def test_display_name_generated(self, tmp_path):
        info = detect_project_info(tmp_path)
        # tmp directories have random names, just check it's set
        assert info["display_name"]

    def test_git_remote_origin_parsed(self, tmp_path):
        # Tab-indented keys (how git actually writes config) must be handled.
        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        (git_dir / "config").write_text(
            "[core]\n\trepositoryformatversion = 0\n"
            '[remote "origin"]\n\turl = https://github.com/acme/widget.git\n'
            "\tfetch = +refs/heads/*:refs/remotes/origin/*\n"
        )
        info = detect_project_info(tmp_path)
        assert info["repository"] == "https://github.com/acme/widget.git"

    def test_git_config_malicious_not_executed(self, tmp_path):
        # CVE-2026-45033 / git config-exec class: a hostile .git/config must
        # NOT lead to command execution. We never shell out to git, so the
        # sentinel file the gadget would create must be absent, while the
        # legitimate origin URL is still parsed.
        sentinel = tmp_path / "PWNED"
        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        (git_dir / "config").write_text(
            "[core]\n"
            f"\tfsmonitor = touch {sentinel}\n"
            f"\tsshCommand = touch {sentinel}\n"
            f"\tpager = touch {sentinel}\n"
            f"\thooksPath = {tmp_path}\n"
            '[remote "origin"]\n'
            "\turl = https://github.com/acme/widget.git\n"
        )
        info = detect_project_info(tmp_path)
        assert not sentinel.exists(), "git config gadget must never execute"
        assert info["repository"] == "https://github.com/acme/widget.git"

    def test_git_dir_as_file_returns_none(self, tmp_path):
        # Worktree/submodule: .git is a file (``gitdir: ...``). We must not
        # follow an attacker-influenced pointer; degrade safely to None.
        (tmp_path / ".git").write_text("gitdir: /tmp/evil/.git/worktrees/x\n")
        info = detect_project_info(tmp_path)
        assert info["repository"] is None

    def test_no_git_dir_no_repository(self, tmp_path):
        info = detect_project_info(tmp_path)
        assert info["repository"] is None


class TestDiscoverProjects:
    def test_finds_projects(self, tmp_path):
        # Create two projects
        for name in ["proj-a", "proj-b"]:
            p = tmp_path / name / ".pm"
            p.mkdir(parents=True)
            (p / "project.yaml").write_text("name: " + name)

        found = discover_projects(tmp_path)
        assert len(found) == 2
        names = {f["name"] for f in found}
        assert "proj-a" in names
        assert "proj-b" in names

    def test_skips_without_project_yaml(self, tmp_path):
        (tmp_path / "bad" / ".pm").mkdir(parents=True)
        # No project.yaml
        found = discover_projects(tmp_path)
        assert len(found) == 0

    def test_empty_directory(self, tmp_path):
        found = discover_projects(tmp_path)
        assert found == []

    def test_nonexistent_path(self, tmp_path):
        found = discover_projects(tmp_path / "nope")
        assert found == []
