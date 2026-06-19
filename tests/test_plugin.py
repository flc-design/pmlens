"""Tests for the Claude Code plugin (``plugin/``) and root ``marketplace.json``.

The plugin is shell + JSON (no Python), so these tests:

1. Validate the marketplace/plugin JSON and their cross-file consistency
   (the marketplace entry must point at the real ``plugin/`` dir and agree
   with ``plugin.json``).
2. Exercise the PostToolUse shell hook via subprocess across its branches:
   git-commit -> directive, non-commit -> silent, manual settings.json hook
   present -> defer (double-fire guard), and the jq-less fallback path.

Pure stdlib. The hooks must work without ``jq`` (the bundled MCP user may not
have it), so we explicitly test that path with a restricted ``PATH``.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PLUGIN_DIR = REPO_ROOT / "plugin"
MARKETPLACE = REPO_ROOT / ".claude-plugin" / "marketplace.json"
PLUGIN_MANIFEST = PLUGIN_DIR / ".claude-plugin" / "plugin.json"
HOOKS_JSON = PLUGIN_DIR / "hooks" / "hooks.json"
POST_HOOK = PLUGIN_DIR / "hooks" / "post-tool-use.sh"
SESSION_HOOK = PLUGIN_DIR / "hooks" / "session-start.sh"
PLUGIN_MCP = PLUGIN_DIR / ".mcp.json"
PLUGIN_README = PLUGIN_DIR / "README.md"
PYPROJECT = REPO_ROOT / "pyproject.toml"

_COMMIT_INPUT = '{"tool_input":{"command":"git commit -m \\"msg\\""},"cwd":"/tmp"}'


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


# ─── JSON structure & cross-file consistency ──────────────────────────────────


def test_marketplace_json_well_formed():
    mp = _load(MARKETPLACE)
    assert mp["name"], "marketplace name is the @<name> install suffix — required"
    assert isinstance(mp["owner"], dict) and mp["owner"].get("name")
    assert isinstance(mp["plugins"], list) and mp["plugins"]


def test_marketplace_entry_points_at_plugin_dir_and_matches_manifest():
    mp = _load(MARKETPLACE)
    manifest = _load(PLUGIN_MANIFEST)
    entry = next(p for p in mp["plugins"] if p["name"] == manifest["name"])
    src = entry["source"]
    assert isinstance(src, str), "in-repo plugin source must be a relative path string"
    assert (REPO_ROOT / src).resolve() == PLUGIN_DIR.resolve()
    assert (REPO_ROOT / src / ".claude-plugin" / "plugin.json").is_file()


def test_marketplace_name_distinct_from_plugin_name():
    # `/plugin install <plugin>@<marketplace>` reads confusingly if identical.
    mp = _load(MARKETPLACE)
    manifest = _load(PLUGIN_MANIFEST)
    assert mp["name"] != manifest["name"]


def test_hooks_json_registers_both_events():
    hooks = _load(HOOKS_JSON)["hooks"]
    assert "SessionStart" in hooks
    assert "PostToolUse" in hooks
    ptu = hooks["PostToolUse"][0]
    assert ptu["matcher"] == "Bash"
    cmd = ptu["hooks"][0]["command"]
    assert cmd.endswith("post-tool-use.sh")
    assert "${CLAUDE_PLUGIN_ROOT}" in cmd


def test_hook_scripts_present_and_executable():
    for script in (POST_HOOK, SESSION_HOOK):
        assert script.is_file()
        assert os.access(script, os.X_OK), f"{script.name} must be executable"


# ─── PostToolUse shell behaviour ──────────────────────────────────────────────


def _run_post_hook(
    stdin: str, *, config_dir: Path, no_jq: bool = False
) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["CLAUDE_CONFIG_DIR"] = str(config_dir)
    if no_jq:
        # Restrict PATH to the coreutils the hook needs, excluding jq, so the
        # `command -v jq` probe fails and the flat-stdout fallback is exercised.
        bindir = config_dir / "_bin"
        bindir.mkdir(exist_ok=True)
        for tool in ("bash", "env", "cat", "grep"):
            real = shutil.which(tool)
            if real is None:
                pytest.skip(f"cannot build jq-less PATH: {tool} not found")
            link = bindir / tool
            if not link.exists():
                link.symlink_to(real)
        env["PATH"] = str(bindir)
    return subprocess.run(
        ["bash", str(POST_HOOK)],
        input=stdin,
        capture_output=True,
        text=True,
        env=env,
        timeout=20,
    )


@pytest.fixture
def empty_config(tmp_path: Path) -> Path:
    d = tmp_path / "empty"
    d.mkdir()
    (d / "settings.json").write_text('{"hooks":{}}', encoding="utf-8")
    return d


@pytest.fixture
def manual_config(tmp_path: Path) -> Path:
    """Config dir whose settings.json carries the manual pm-server hook."""
    d = tmp_path / "manual"
    d.mkdir()
    settings = {
        "hooks": {
            "PostToolUse": [
                {
                    "matcher": "Bash",
                    "hooks": [{"type": "command", "command": "/x/pm-server hook post-tool-use"}],
                }
            ]
        }
    }
    (d / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
    return d


def test_directive_emitted_on_git_commit(empty_config: Path):
    r = _run_post_hook(_COMMIT_INPUT, config_dir=empty_config)
    assert r.returncode == 0
    assert "pm_update_task" in r.stdout
    assert "pm_log" in r.stdout
    assert "pm_next" in r.stdout
    # When jq is available the hook MUST emit the structured envelope Claude Code
    # consumes — pin the exact contract so a wrong key or invalid JSON regresses
    # loudly. (The substring checks above pass even on flat or typo'd output.)
    if shutil.which("jq"):
        hook_out = json.loads(r.stdout)["hookSpecificOutput"]
        assert hook_out["hookEventName"] == "PostToolUse"
        assert "pm_update_task" in hook_out["additionalContext"]


def test_silent_on_non_commit(empty_config: Path):
    r = _run_post_hook('{"tool_input":{"command":"ls -la"}}', config_dir=empty_config)
    assert r.returncode == 0
    assert r.stdout.strip() == ""


def test_defers_when_manual_hook_present(manual_config: Path):
    """Double-fire guard: a manual settings.json hook -> emit nothing."""
    r = _run_post_hook(_COMMIT_INPUT, config_dir=manual_config)
    assert r.returncode == 0
    assert r.stdout.strip() == ""


def test_directive_emitted_without_jq(empty_config: Path):
    r = _run_post_hook(
        '{"tool_input":{"command":"git commit"}}', config_dir=empty_config, no_jq=True
    )
    assert r.returncode == 0
    assert "pm_update_task" in r.stdout
    # Positively prove the FLAT fallback ran (not the jq envelope): the directive
    # is emitted as a bare line with no structured wrapper.
    assert "hookSpecificOutput" not in r.stdout


# ─── Plugin version drift guard (PMSERV-133) ──────────────────────────────────
#
# The plugin pins pm-server's version across FOUR surfaces that must all move in
# lockstep with pyproject.toml on every release. The v0.10.0 release skew — a
# plugin pin lagging main by many commits — is what motivated this guard: a
# missed surface now fails loudly here instead of silently shipping a stale
# plugin. Mirrors the lockstep-assertion style of test_manifest.py.


def _pyproject_version() -> str:
    """Single source of truth: pyproject.toml [project].version."""
    with PYPROJECT.open("rb") as f:
        return tomllib.load(f)["project"]["version"]


class TestPluginVersionSync:
    """Every plugin version surface must equal the pyproject version."""

    def test_plugin_manifest_version_matches_pyproject(self):
        assert _load(PLUGIN_MANIFEST)["version"] == _pyproject_version(), (
            "plugin/.claude-plugin/plugin.json version drifted from pyproject.toml; "
            "bump in lockstep across all plugin version surfaces (PMSERV-133)"
        )

    def test_marketplace_metadata_version_matches_pyproject(self):
        # The version lives under metadata.version; plugins[0] has no version key.
        assert _load(MARKETPLACE)["metadata"]["version"] == _pyproject_version(), (
            ".claude-plugin/marketplace.json metadata.version drifted from "
            "pyproject.toml; bump in lockstep (PMSERV-133)"
        )

    def test_mcp_uvx_pin_matches_pyproject(self):
        ver = _pyproject_version()
        pin = _load(PLUGIN_MCP)["mcpServers"]["pm-server"]["args"][0]
        m = re.fullmatch(r"pm-server@(.+)", pin)
        assert m is not None, f"plugin/.mcp.json uvx pin malformed: {pin!r}"
        assert m.group(1) == ver, (
            f"plugin/.mcp.json pins {pin!r}; expected 'pm-server@{ver}' — the uvx "
            "pin drifted from pyproject.toml (PMSERV-133)"
        )

    def test_plugin_readme_pins_match_pyproject(self):
        ver = _pyproject_version()
        text = PLUGIN_README.read_text(encoding="utf-8")
        # The load-bearing committed release pin, e.g. `uvx pm-server@0.10.0`.
        assert f"pm-server@{ver}" in text, (
            f"plugin/README.md is missing the release pin 'pm-server@{ver}'; its "
            "documented uvx pin drifted from pyproject.toml (PMSERV-133)"
        )
        # Every CONCRETE version pin must match (also catches the `pm-server>=x.y.z`
        # floor example); the generic `pm-server@x.y` placeholder carries no
        # numeric version and is correctly ignored by the \d+\.\d+\.\d+ anchor.
        pinned = re.findall(r"pm-server[@>=]+(\d+\.\d+\.\d+)", text)
        assert pinned, "expected at least one concrete pm-server version pin in README"
        assert all(p == ver for p in pinned), (
            f"plugin/README.md has version pins {sorted(set(pinned))} disagreeing "
            f"with pyproject {ver}; bump all in lockstep (PMSERV-133)"
        )
