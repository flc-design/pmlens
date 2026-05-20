"""Tests for the .mcpb manifest + bundle builder (PMSERV-083, WF-025).

Pin the Desktop/Cowork distribution contract so that:
  - manifest.json never drifts from pyproject.toml on version bumps
    (catches the same class of dogfooding drift memory:135 flagged for
    .pm/project.yaml).
  - server.type stays ``"uv"`` and the Lens-enforcing ``PM_LENS=1`` env
    cannot be silently removed.
  - The build script produces a real .mcpb zip with the expected files.
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


def _load_manifest() -> dict:
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def _load_pyproject_version() -> str:
    with open(PYPROJECT_PATH, "rb") as f:
        return tomllib.load(f)["project"]["version"]


class TestManifestShape:
    def test_manifest_is_valid_json(self):
        _load_manifest()  # would raise on parse error

    def test_manifest_version_is_v04(self):
        assert _load_manifest()["manifest_version"] == "0.4"

    def test_version_matches_pyproject(self):
        manifest = _load_manifest()
        assert manifest["version"] == _load_pyproject_version(), (
            "manifest.json version must move in lockstep with pyproject.toml"
        )

    def test_server_type_is_uv(self):
        server = _load_manifest()["server"]
        assert server["type"] == "uv"
        assert server["uv"]["package"] == "pm-server"
        assert server["uv"]["command"] == "pm-server"
        assert server["uv"]["args"] == ["serve"]

    def test_pm_lens_env_set(self):
        # ADR-017 + ADR-018: the manifest is policy, not a hard security
        # boundary, but the env value must be set so installs default to Lens.
        # The env lives under server.mcp_config.env per the MCPB spec.
        env = _load_manifest()["server"]["mcp_config"]["env"]
        assert env["PM_LENS"] == "1"

    def test_no_top_level_environment_or_permissions(self):
        # PMSERV-084 / WF-026 FINDING-D: top-level `environment` and
        # `permissions` are not documented MCPB fields. Env vars belong under
        # `server.mcp_config.env`; the read-only filesystem boundary is
        # enforced structurally by RO_ALLOWLIST and SQLite mode=ro, not by a
        # declarative manifest field.
        manifest = _load_manifest()
        assert "environment" not in manifest
        assert "permissions" not in manifest


class TestBuildScriptValidation:
    """The build_mcpb.py validator must reject manifests that violate the
    invariants we care about. Run validate() with manipulated dicts."""

    @pytest.fixture
    def validate(self):
        # Import lazily so the test does not require sys.path tweaks at module
        # import; the script lives outside the package root.
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
                "uv": {"package": "pm-server", "command": "pm-server", "args": ["serve"]},
                "mcp_config": {"env": {"PM_LENS": "1"}},
            },
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

    def test_missing_pm_lens_rejected(self, validate):
        bad = self._good_manifest()
        bad["server"]["mcp_config"] = {"env": {}}
        with pytest.raises(SystemExit, match="PM_LENS"):
            validate(bad, "9.9.9")

    def test_wrong_pm_lens_value_rejected(self, validate):
        bad = self._good_manifest()
        bad["server"]["mcp_config"]["env"]["PM_LENS"] = "0"
        with pytest.raises(SystemExit, match="PM_LENS"):
            validate(bad, "9.9.9")

    def test_wrong_package_name_rejected(self, validate):
        bad = self._good_manifest()
        bad["server"]["uv"]["package"] = "pm-server-evil"
        with pytest.raises(SystemExit, match="server.uv.package"):
            validate(bad, "9.9.9")


class TestBundleBuild:
    def test_build_creates_zip_with_manifest(self, tmp_path, monkeypatch):
        sys.path.insert(0, str(REPO_ROOT / "scripts"))
        try:
            import build_mcpb
        finally:
            sys.path.pop(0)

        # Redirect DIST_DIR so the test does not pollute the real dist/
        monkeypatch.setattr(build_mcpb, "DIST_DIR", tmp_path / "out")

        out_path = build_mcpb.build_bundle("9.9.9")
        assert out_path.exists()
        assert out_path.suffix == ".mcpb"
        assert "9.9.9" in out_path.name

        with zipfile.ZipFile(out_path) as zf:
            names = set(zf.namelist())
        # The bundle ships the manifest plus user-facing docs only.
        assert "manifest.json" in names
        assert "README.md" in names
        assert "LICENSE" in names
        # No source code: server.type=uv has the host fetch the package.
        assert not any(name.endswith(".py") for name in names)
