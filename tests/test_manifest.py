"""Tests for the .mcpb manifest + bundle builder.

Pins the Desktop/Cowork distribution contract against the public MCPB v0.4
spec (https://github.com/anthropics/mcpb). The historical ``server.uv =
{package, command, args}`` shape was an unreleased preview — Claude Desktop's
Zod validator rejects it with ``Unrecognized key(s) in object: 'uv'``. The
PMSERV-106 defect rewrote the manifest to use the documented v0.4 shape:
``server.entry_point`` + ``server.mcp_config.command = "uv"`` invoking
``uv run --directory ${__dirname} <entry>``, with source code bundled
in-package.
"""

from __future__ import annotations

import json
import sys
import tomllib
import zipfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
MANIFEST_PATH = REPO_ROOT / "manifest.json"
PYPROJECT_PATH = REPO_ROOT / "pyproject.toml"
MCPBIGNORE_PATH = REPO_ROOT / ".mcpbignore"


def _load_manifest() -> dict:
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def _load_pyproject_version() -> str:
    with open(PYPROJECT_PATH, "rb") as f:
        return tomllib.load(f)["project"]["version"]


class TestManifestShape:
    def test_manifest_is_valid_json(self):
        _load_manifest()

    def test_manifest_version_is_v04(self):
        assert _load_manifest()["manifest_version"] == "0.4"

    def test_version_matches_pyproject(self):
        manifest = _load_manifest()
        assert manifest["version"] == _load_pyproject_version(), (
            "manifest.json version must move in lockstep with pyproject.toml"
        )

    def test_init_version_matches_pyproject(self):
        # PMSERV-106 follow-up: src/pm_server/__init__.py:__version__ is what
        # `pm-server --version` prints to users — silently drifting from
        # pyproject (as it did between 0.6.2 and 0.8.0) is a release-quality
        # bug, even if functionally cosmetic.
        import re

        init_text = (REPO_ROOT / "src" / "pm_server" / "__init__.py").read_text(encoding="utf-8")
        match = re.search(r'__version__\s*=\s*"([^"]+)"', init_text)
        assert match is not None, "src/pm_server/__init__.py is missing __version__"
        assert match.group(1) == _load_pyproject_version(), (
            f"__init__.py __version__ ({match.group(1)!r}) drifted from "
            f"pyproject.toml ({_load_pyproject_version()!r}); bump in lockstep"
        )

    def test_server_type_is_uv(self):
        assert _load_manifest()["server"]["type"] == "uv"

    def test_server_has_entry_point_pointing_at_real_file(self):
        # PMSERV-106: server.entry_point is REQUIRED for v0.4 uv type.
        entry_point = _load_manifest()["server"]["entry_point"]
        assert isinstance(entry_point, str) and entry_point
        assert (REPO_ROOT / entry_point).is_file(), (
            f"manifest entry_point {entry_point!r} must exist in the repo so "
            "the bundled copy resolves at install time"
        )

    def test_server_does_not_have_legacy_uv_nested_block(self):
        # PMSERV-106: Claude Desktop's Zod validator rejects the historical
        # `server.uv = {package, command, args}` block as an unknown key.
        server = _load_manifest()["server"]
        assert "uv" not in server, (
            "legacy server.uv = {...} preview shape — Claude Desktop rejects "
            "with `Unrecognized key(s) in object: 'uv'`. Move runtime invocation "
            "into server.mcp_config.command + args."
        )

    def test_mcp_config_invokes_uv_run_with_dirname(self):
        # The v0.4 uv type expects host to call `uv run --directory ${__dirname}
        # <command>`; we pin the exact shape so a regression is loud.
        mcp_config = _load_manifest()["server"]["mcp_config"]
        assert mcp_config["command"] == "uv"
        args = mcp_config["args"]
        assert "run" in args
        assert "${__dirname}" in args, (
            "${__dirname} substitution is required so the entry script resolves "
            "relative to the installed bundle directory"
        )

    def test_mcp_config_uses_module_invocation_not_script_path(self):
        # PMSERV-106 follow-up: a direct script invocation (`python
        # src/pm_server/__main__.py`) loses the package context — Python sets
        # __package__ to None so `from . import __version__` dies with
        # `attempted relative import with no known parent package`. The correct
        # form is `python -m pm_server` which establishes pm_server as the
        # importing package.
        args = _load_manifest()["server"]["mcp_config"]["args"]
        assert "-m" in args, "must invoke as a module (`python -m pm_server`)"
        assert "pm_server" in args
        assert "serve" in args, "the `serve` subcommand starts the MCP stdio server"
        # Negative guard: a raw .py path in args means we've regressed to the
        # broken script-execution form.
        for arg in args:
            assert not arg.endswith("__main__.py"), (
                f"direct script path {arg!r} in args — use `-m pm_server` instead"
            )

    def test_pm_lens_env_set(self):
        # ADR-017 + ADR-018: the manifest is policy (not a hard security
        # boundary) but the env value must be set so installs default to Lens.
        env = _load_manifest()["server"]["mcp_config"]["env"]
        assert env["PM_LENS"] == "1"

    def test_pm_desktop_write_env_set(self):
        # PMSERV-100 (Phase 2 / ADR-019): the bundle propagates
        # PM_DESKTOP_WRITE=1 so Desktop hosts get the outbox bridge without
        # manual config edits.
        env = _load_manifest()["server"]["mcp_config"]["env"]
        assert env["PM_DESKTOP_WRITE"] == "1"

    def test_compatibility_platforms_use_nodejs_enum(self):
        # PMSERV-106: Claude Desktop's Zod schema validates against the
        # Node.js `process.platform` values (`darwin`/`win32`/`linux`); the
        # human-friendly `macos`/`windows` strings are rejected.
        platforms = _load_manifest()["compatibility"]["platforms"]
        assert set(platforms).issubset({"darwin", "linux", "win32"}), (
            f"platforms {platforms!r} contain values outside the v0.4 enum"
        )
        assert "macos" not in platforms
        assert "windows" not in platforms

    def test_no_top_level_environment_or_permissions(self):
        # WF-026 FINDING-D (memory:143): top-level `environment` and
        # `permissions` are not in the documented MCPB schema. Env vars belong
        # under server.mcp_config.env; the read-only boundary is enforced by
        # RO_ALLOWLIST + SQLite mode=ro, not by a declarative manifest field.
        manifest = _load_manifest()
        assert "environment" not in manifest
        assert "permissions" not in manifest


class TestBuildScriptValidation:
    """The build_mcpb.py validator must reject manifests that violate the
    v0.4 contract."""

    @pytest.fixture
    def validate(self):
        sys.path.insert(0, str(REPO_ROOT / "scripts"))
        try:
            from build_mcpb import validate as _validate
        finally:
            sys.path.pop(0)
        return _validate

    def _good_manifest(self):
        return {
            "manifest_version": "0.4",
            "version": "9.9.9",
            "server": {
                "type": "uv",
                "entry_point": "src/pm_server/__main__.py",
                "mcp_config": {
                    "command": "uv",
                    "args": [
                        "run",
                        "--directory",
                        "${__dirname}",
                        "python",
                        "-m",
                        "pm_server",
                        "serve",
                    ],
                    "env": {"PM_LENS": "1", "PM_DESKTOP_WRITE": "1"},
                },
            },
            "compatibility": {"platforms": ["darwin", "linux", "win32"]},
        }

    def test_good_manifest_passes(self, validate):
        validate(self._good_manifest(), "9.9.9")

    def test_version_mismatch_rejected(self, validate):
        with pytest.raises(SystemExit, match="manifest.json version"):
            validate(self._good_manifest(), "1.0.0")

    def test_non_uv_server_type_rejected(self, validate):
        bad = self._good_manifest()
        bad["server"]["type"] = "python"
        with pytest.raises(SystemExit, match="server.type"):
            validate(bad, "9.9.9")

    def test_legacy_server_uv_nested_block_rejected(self, validate):
        # PMSERV-106 regression guard.
        bad = self._good_manifest()
        bad["server"]["uv"] = {"package": "pm-server", "command": "pm-server"}
        with pytest.raises(SystemExit, match="server.uv"):
            validate(bad, "9.9.9")

    def test_missing_entry_point_rejected(self, validate):
        bad = self._good_manifest()
        del bad["server"]["entry_point"]
        with pytest.raises(SystemExit, match="entry_point"):
            validate(bad, "9.9.9")

    def test_entry_point_pointing_at_missing_file_rejected(self, validate):
        bad = self._good_manifest()
        bad["server"]["entry_point"] = "src/pm_server/does_not_exist.py"
        with pytest.raises(SystemExit, match="does not exist"):
            validate(bad, "9.9.9")

    def test_mcp_config_command_not_uv_rejected(self, validate):
        bad = self._good_manifest()
        bad["server"]["mcp_config"]["command"] = "python"
        with pytest.raises(SystemExit, match="mcp_config.command"):
            validate(bad, "9.9.9")

    def test_mcp_config_args_missing_dirname_rejected(self, validate):
        bad = self._good_manifest()
        bad["server"]["mcp_config"]["args"] = ["run", "python", "-m", "pm_server", "serve"]
        with pytest.raises(SystemExit, match=r"\$\{__dirname\}"):
            validate(bad, "9.9.9")

    def test_mcp_config_args_missing_module_invocation_rejected(self, validate):
        # PMSERV-106 follow-up regression guard: a raw script-path invocation
        # would dies at runtime with `attempted relative import…`.
        bad = self._good_manifest()
        bad["server"]["mcp_config"]["args"] = [
            "run",
            "--directory",
            "${__dirname}",
            "src/pm_server/__main__.py",
            "serve",
        ]
        with pytest.raises(SystemExit, match="python -m pm_server"):
            validate(bad, "9.9.9")

    def test_mcp_config_args_missing_serve_subcommand_rejected(self, validate):
        bad = self._good_manifest()
        bad["server"]["mcp_config"]["args"] = [
            "run",
            "--directory",
            "${__dirname}",
            "python",
            "-m",
            "pm_server",
        ]
        with pytest.raises(SystemExit, match="serve"):
            validate(bad, "9.9.9")

    def test_missing_pm_lens_rejected(self, validate):
        bad = self._good_manifest()
        bad["server"]["mcp_config"]["env"] = {}
        with pytest.raises(SystemExit, match="PM_LENS"):
            validate(bad, "9.9.9")

    def test_wrong_pm_lens_value_rejected(self, validate):
        bad = self._good_manifest()
        bad["server"]["mcp_config"]["env"]["PM_LENS"] = "0"
        with pytest.raises(SystemExit, match="PM_LENS"):
            validate(bad, "9.9.9")

    def test_invalid_platform_string_rejected(self, validate):
        # PMSERV-106: 'macos' and 'windows' are NOT in the v0.4 platform enum.
        bad = self._good_manifest()
        bad["compatibility"]["platforms"] = ["macos", "linux", "windows"]
        with pytest.raises(SystemExit, match="platforms"):
            validate(bad, "9.9.9")

    def test_top_level_environment_legacy_rejected(self, validate):
        # WF-026 FINDING-D regression guard.
        bad = self._good_manifest()
        bad["environment"] = {"PM_LENS": "1"}
        with pytest.raises(SystemExit, match="environment"):
            validate(bad, "9.9.9")


class TestBundleBuild:
    def test_build_creates_zip_with_required_payload(self, tmp_path, monkeypatch):
        sys.path.insert(0, str(REPO_ROOT / "scripts"))
        try:
            import build_mcpb
        finally:
            sys.path.pop(0)

        monkeypatch.setattr(build_mcpb, "DIST_DIR", tmp_path / "out")

        out_path = build_mcpb.build_bundle("9.9.9")
        assert out_path.exists()
        assert out_path.suffix == ".mcpb"
        assert "9.9.9" in out_path.name

        with zipfile.ZipFile(out_path) as zf:
            names = set(zf.namelist())

        # Top-level files required by MCPB v0.4 uv type.
        assert "manifest.json" in names
        assert "README.md" in names
        assert "LICENSE" in names
        assert "pyproject.toml" in names, (
            "uv server type needs pyproject.toml in the bundle so the host "
            "can install deps via `uv run`"
        )
        assert ".mcpbignore" in names

        # Python source must be bundled (the v0.4 uv contract — the host runs
        # `uv run --directory ${__dirname} <entry>` against in-bundle files).
        assert "src/pm_server/__main__.py" in names
        assert "src/pm_server/server.py" in names
        assert "src/pm_server/__init__.py" in names
        assert "src/pm_server/outbox.py" in names  # Phase 2 / ADR-019

    def test_bundle_excludes_caches_and_bytecode(self, tmp_path, monkeypatch):
        sys.path.insert(0, str(REPO_ROOT / "scripts"))
        try:
            import build_mcpb
        finally:
            sys.path.pop(0)
        monkeypatch.setattr(build_mcpb, "DIST_DIR", tmp_path / "out")

        out_path = build_mcpb.build_bundle("9.9.9")
        with zipfile.ZipFile(out_path) as zf:
            names = zf.namelist()

        assert not any("__pycache__" in n for n in names), (
            "bytecode caches must not leak into the distributable bundle"
        )
        assert not any(n.endswith((".pyc", ".pyo")) for n in names)

    def test_bundle_entry_point_resolves_inside_zip(self, tmp_path, monkeypatch):
        # Cross-check: whatever path the manifest declares as entry_point must
        # actually be present in the built zip, otherwise install succeeds but
        # the host fails at first launch.
        sys.path.insert(0, str(REPO_ROOT / "scripts"))
        try:
            import build_mcpb
        finally:
            sys.path.pop(0)
        monkeypatch.setattr(build_mcpb, "DIST_DIR", tmp_path / "out")

        out_path = build_mcpb.build_bundle("9.9.9")
        entry_point = _load_manifest()["server"]["entry_point"]

        with zipfile.ZipFile(out_path) as zf:
            assert entry_point in zf.namelist()


class TestMcpbignore:
    def test_mcpbignore_exists(self):
        assert MCPBIGNORE_PATH.is_file(), (
            ".mcpbignore is required by MCPB v0.4 uv type so the host knows "
            "which files to skip when copying the bundle into the install dir"
        )

    def test_mcpbignore_blocks_critical_exclusions(self):
        body = MCPBIGNORE_PATH.read_text(encoding="utf-8")
        # The spec specifically warns against shipping `.venv` or `server/lib`
        # in uv-type bundles; bytecode caches must also be skipped so the
        # bundle stays deterministic.
        for pattern in (".venv/", "__pycache__/", "*.pyc"):
            assert pattern in body, f"{pattern!r} missing from .mcpbignore"
