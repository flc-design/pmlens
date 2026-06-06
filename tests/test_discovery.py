"""Tests for project discovery and info detection."""

import json
from pathlib import Path

from pm_server.discovery import (
    detect_project_info,
    discover_projects,
    read_git_branch,
    scan_projects,
)


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


class TestReadGitBranch:
    """PMSERV-124 / ADR-028: branch detection by text-parsing .git/HEAD.

    Mirrors the no-shell-out policy of _read_git_remote_origin_url — never runs
    ``git``, so a hostile .git/config can never execute code.
    """

    @staticmethod
    def _mk_repo(tmp_path: Path, head: str) -> Path:
        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        (git_dir / "HEAD").write_text(head)
        return tmp_path

    def test_normal_branch(self, tmp_path):
        self._mk_repo(tmp_path, "ref: refs/heads/main\n")
        assert read_git_branch(tmp_path) == "main"

    def test_branch_with_slashes(self, tmp_path):
        self._mk_repo(tmp_path, "ref: refs/heads/feature/paper\n")
        assert read_git_branch(tmp_path) == "feature/paper"

    def test_detached_head_returns_none(self, tmp_path):
        # Detached HEAD holds a raw SHA, not a ``ref:`` line.
        self._mk_repo(tmp_path, "9f1c2b3a4d5e6f7080910a1b2c3d4e5f60718293\n")
        assert read_git_branch(tmp_path) is None

    def test_no_git_dir_returns_none(self, tmp_path):
        assert read_git_branch(tmp_path) is None

    def test_git_dir_as_file_returns_none(self, tmp_path):
        # Worktree / submodule: .git is a FILE (``gitdir: ...``). We do not
        # follow an attacker-influenced pointer; worktree continuity relies on
        # per-directory .pm isolation instead.
        (tmp_path / ".git").write_text("gitdir: /tmp/evil/.git/worktrees/x\n")
        assert read_git_branch(tmp_path) is None

    def test_oversized_head_returns_none(self, tmp_path):
        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        (git_dir / "HEAD").write_text("ref: refs/heads/" + ("a" * 5000) + "\n")
        assert read_git_branch(tmp_path) is None

    def test_empty_head_returns_none(self, tmp_path):
        self._mk_repo(tmp_path, "")
        assert read_git_branch(tmp_path) is None

    def test_malicious_git_config_not_executed(self, tmp_path):
        # CVE-2026-45033 / git config-exec class: detecting the branch must not
        # execute a gadget planted in .git/config, because we read HEAD as text
        # and never invoke ``git``.
        sentinel = tmp_path / "PWNED"
        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        (git_dir / "HEAD").write_text("ref: refs/heads/paper\n")
        (git_dir / "config").write_text(
            "[core]\n"
            f"\tfsmonitor = touch {sentinel}\n"
            f"\tsshCommand = touch {sentinel}\n"
            f"\tpager = touch {sentinel}\n"
        )
        assert read_git_branch(tmp_path) == "paper"
        assert not sentinel.exists(), "git config gadget must never execute"


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


class TestDiscoverProjectsBounded:
    """PMSERV-081 (WF-025 R2, ADR-016): bounded walk hardening."""

    def _make_project(self, path):
        pm = path / ".pm"
        pm.mkdir(parents=True)
        (pm / "project.yaml").write_text("name: " + path.name)

    def test_depth_cap_excludes_deep_projects(self, tmp_path):
        # depth-5 project is included; depth-6 is excluded by the default cap.
        # depth here counts directory levels below scan_path until the
        # project's parent directory (the dir containing .pm).
        shallow = tmp_path / "a" / "b" / "c" / "d" / "shallow"
        deep = tmp_path / "a" / "b" / "c" / "d" / "e" / "f" / "deep"
        self._make_project(shallow)
        self._make_project(deep)

        found = discover_projects(tmp_path)
        names = {f["name"] for f in found}
        assert "shallow" in names
        assert "deep" not in names

    def test_custom_max_depth(self, tmp_path):
        nested = tmp_path / "a" / "b" / "c" / "d" / "e" / "f" / "g" / "p"
        self._make_project(nested)
        # default cap 5 excludes it
        assert discover_projects(tmp_path) == []
        # explicit larger cap includes it
        found = discover_projects(tmp_path, max_depth=10)
        assert any(f["name"] == "p" for f in found)

    def test_excluded_dirs_pruned(self, tmp_path):
        # Projects nested under common cache/dependency directories must not
        # be discovered — typical noise: a vendored copy of pm-server inside
        # node_modules, or a stray .pm inside .git from a botched merge.
        for excluded in ("node_modules", ".git", "__pycache__", ".venv", "target"):
            self._make_project(tmp_path / excluded / "vendored")

        # One legitimate project to confirm the walk still works overall
        self._make_project(tmp_path / "real-project")

        found = discover_projects(tmp_path)
        names = {f["name"] for f in found}
        assert names == {"real-project"}

    def test_global_pm_directory_skipped(self, tmp_path, monkeypatch):
        # ADR-016: even if scan_path lands at $HOME and a global ~/.pm
        # happens to contain project.yaml, it must not be enumerated.
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        global_pm = fake_home / ".pm"
        global_pm.mkdir()
        (global_pm / "project.yaml").write_text("name: global-pm")
        # Also a legitimate project sibling
        self._make_project(fake_home / "real")

        monkeypatch.setattr(Path, "home", lambda: fake_home)

        found = discover_projects(fake_home)
        names = {f["name"] for f in found}
        assert "real" in names
        assert "home" not in names  # the global ~/.pm parent is $HOME itself

    def test_global_pm_not_descended_for_subprojects(self, tmp_path, monkeypatch):
        # A nested project living *inside* ~/.pm/desktop (Desktop store
        # isolation) must not be reachable via discover. ADR-016 negative.
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        global_pm = fake_home / ".pm"
        nested = global_pm / "desktop" / "trapped"
        self._make_project(nested)
        # Legitimate project at $HOME level
        self._make_project(fake_home / "real")

        monkeypatch.setattr(Path, "home", lambda: fake_home)

        found = discover_projects(fake_home)
        names = {f["name"] for f in found}
        assert "real" in names
        assert "trapped" not in names

    def test_symlink_loops_do_not_hang(self, tmp_path):
        # A symlink pointing back to scan_path used to be an infinite-loop
        # hazard with naive rglob. With followlinks=False we must finish.
        self._make_project(tmp_path / "real")
        loop = tmp_path / "loop"
        try:
            loop.symlink_to(tmp_path)
        except OSError:
            # Filesystem may forbid symlinks; treat as skip.
            return

        found = discover_projects(tmp_path)
        names = {f["name"] for f in found}
        assert "real" in names

    def test_does_not_descend_into_pm(self, tmp_path):
        # A stray project.yaml deep inside another project's .pm/ must not
        # be misidentified — discovery records the .pm's parent then stops.
        self._make_project(tmp_path / "outer")
        nested_marker = tmp_path / "outer" / ".pm" / "ghost" / ".pm"
        nested_marker.mkdir(parents=True)
        (nested_marker / "project.yaml").write_text("name: ghost")

        found = discover_projects(tmp_path)
        names = {f["name"] for f in found}
        assert "outer" in names
        assert "ghost" not in names


class TestScanProjectsDepthCap:
    """PMSERV-089 (WF-026 FINDING-H): surface depth-cap exclusions.

    ``scan_projects`` returns a ``DiscoveryResult`` whose ``depth_capped``
    field lists directories skipped *purely* because of the depth cap, so
    ``pm_discover`` can warn instead of silently finding nothing.
    """

    def _make_project(self, path):
        pm = path / ".pm"
        pm.mkdir(parents=True)
        (pm / "project.yaml").write_text("name: " + path.name)

    def test_no_capped_dirs_for_shallow_tree(self, tmp_path):
        # A project well within the cap leaves depth_capped empty.
        self._make_project(tmp_path / "a" / "b" / "shallow")
        result = scan_projects(tmp_path)
        assert {p["name"] for p in result.projects} == {"shallow"}
        assert result.depth_capped == []
        assert result.max_depth == 5

    def test_records_boundary_dir_for_deep_project(self, tmp_path):
        # deep's parent sits at depth 6: the walk refuses to descend into the
        # depth-6 dir "f" from "e" (depth 5), so "f" is recorded and the
        # project itself is never found.
        self._make_project(tmp_path / "a" / "b" / "c" / "d" / "e" / "f" / "deep")
        result = scan_projects(tmp_path)
        assert result.projects == []
        capped_names = {Path(c).name for c in result.depth_capped}
        assert "f" in capped_names

    def test_excluded_dirs_not_counted_as_capped(self, tmp_path):
        # At depth 5 (dir "e"), a normal child is depth-capped but an
        # excluded-name child (node_modules) must be filtered *before* the
        # depth check and therefore never appear in depth_capped.
        base = tmp_path / "a" / "b" / "c" / "d" / "e"
        (base / "normal").mkdir(parents=True)
        (base / "node_modules").mkdir(parents=True)
        result = scan_projects(tmp_path)
        capped_names = {Path(c).name for c in result.depth_capped}
        assert "normal" in capped_names
        assert "node_modules" not in capped_names

    def test_custom_max_depth_clears_capped(self, tmp_path):
        # A larger cap that reaches the project leaves nothing capped and
        # echoes the cap that was applied.
        self._make_project(tmp_path / "a" / "b" / "c" / "d" / "e" / "f" / "g" / "p")
        result = scan_projects(tmp_path, max_depth=10)
        assert any(p["name"] == "p" for p in result.projects)
        assert result.depth_capped == []
        assert result.max_depth == 10

    def test_wrapper_matches_scan_projects(self, tmp_path):
        # discover_projects must stay a thin wrapper over scan_projects.
        self._make_project(tmp_path / "a" / "proj")
        assert discover_projects(tmp_path) == scan_projects(tmp_path).projects
