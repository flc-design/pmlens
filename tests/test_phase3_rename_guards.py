"""Phase-3 rename-migration guard tests (PMSERV-137 / ADR-034 / ADR-032).

These guards lay down the safety net BEFORE the mechanical ``pm_server`` →
``pmlens`` identity rename (step 2 of the ADR-034 execution plan). They assert,
against the CURRENT tree, the load-bearing INVARIANTS the rename must not
violate and the wrapper/plugin surfaces it must flip deliberately rather than
via an accidental global ``sed``. Every test here is GREEN against the
pre-rename code.

Deliberately NOT here (they land in step 6, the breaking identity flip): the
positive ``FastMCP("pmlens")`` and manifest-name guards. A guard edited in the
same commit as the value it guards gives zero protection against that commit, so
those are paired with INDEPENDENT CI grep gates instead (``grep -c
'FastMCP("pm-server")' src`` == 0). Pre-pinning them here would just be red the
moment step 6 starts; pinning the *invariants* (below) is what is load-bearing
during the step-2 sweep.

Companion scaffolding: the ``legacy_user_env`` fixture in ``conftest.py`` builds
the pre-rename user environment the ``migrate-from-pm-server`` tests (steps 3-6)
drive against; ``TestLegacyUserEnvFixture`` proves it is well-formed now.
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
WRAPPER_DIR = REPO_ROOT / "packaging" / "pm-server-wrapper"
PLUGIN_HOOKS = REPO_ROOT / "plugin" / "hooks"


class TestMarkerSlugInvariant:
    """The CLAUDE.md/AGENTS.md marker slug ``pm-server:begin``/``pm-server:end``
    is an opaque on-disk key (ADR-032 / ADR-034 invariant). The rename must NOT
    touch it, or ``BEGIN_PATTERN.search()`` misses existing user blocks and
    ``pm_update_rules`` appends a duplicate instead of upgrading in place.
    ``test_rules.py::test_markers_are_strings`` already asserts the slug
    positively; these assert it NEGATIVELY (no ``pmlens`` leak) and pin the
    source-file grep count that is the step-6 CI gate baseline."""

    def test_markers_use_pm_server_slug_not_pmlens(self):
        from pmlens.rules import BEGIN_MARKER, BEGIN_PATTERN, END_MARKER

        assert "pm-server:begin" in BEGIN_MARKER
        assert "pmlens:" not in BEGIN_MARKER
        assert "pm-server:end" in END_MARKER
        assert "pmlens:" not in END_MARKER
        assert "pm-server:begin" in BEGIN_PATTERN.pattern
        assert "pmlens" not in BEGIN_PATTERN.pattern

    def test_rules_source_keeps_at_least_three_pm_server_begin_lines(self):
        # CI grep-gate baseline: ``grep -c 'pm-server:begin' rules.py`` >= 3
        # (BEGIN_MARKER, BEGIN_PATTERN, CLAUDEMD_TEMPLATE). A global sed that
        # renamed the slug would drop this below 3 and turn red here.
        import pmlens.rules as rules_mod

        source = Path(rules_mod.__file__).read_text(encoding="utf-8")
        hits = sum(1 for line in source.splitlines() if "pm-server:begin" in line)
        assert hits >= 3, f"expected >=3 'pm-server:begin' lines in rules.py, found {hits}"


class TestWrapperMetapackageInvariant:
    """The ``pm-server`` PyPI wrapper must stay a zero-module metapackage that
    only depends on ``pmlens`` (ADR-031 / ADR-032). If it ever shipped its own
    ``pm_server`` top-level or a ``pm-server`` console_scripts entry it would
    collide with pmlens on ``pip install``. Phase-3 keeps pmlens the SOLE owner
    of both the ``pmlens`` and legacy ``pm_server`` import names."""

    @staticmethod
    def _wrapper_pyproject() -> dict:
        return tomllib.loads((WRAPPER_DIR / "pyproject.toml").read_text(encoding="utf-8"))

    def test_distribution_name_stays_pm_server(self):
        assert self._wrapper_pyproject()["project"]["name"] == "pm-server"

    def test_depends_only_on_pmlens(self):
        deps = self._wrapper_pyproject()["project"]["dependencies"]
        assert any(d.replace(" ", "").startswith("pmlens") for d in deps), deps

    def test_ships_no_python_packages(self):
        # Metapackage: pmlens provides every module.
        assert self._wrapper_pyproject()["tool"]["setuptools"]["packages"] == []

    def test_declares_no_console_scripts(self):
        # pmlens owns the ``pm-server`` console script; a wrapper script collides.
        assert "scripts" not in self._wrapper_pyproject()["project"]

    def test_top_level_txt_is_empty_when_present(self):
        top_level = WRAPPER_DIR / "pm_server.egg-info" / "top_level.txt"
        if top_level.exists():
            assert top_level.read_text(encoding="utf-8").strip() == "", (
                "wrapper top_level.txt is non-empty — it would ship a module that "
                "collides with pmlens's pm_server import name"
            )


class TestPluginGuardBaseline:
    """Baseline (single-recognition) plugin guard state, GREEN today. Step 6
    widens these to DUAL-recognize the new ``pmlens hook`` command and the
    ``pmlens`` MCP key; pinning the current strings makes that change a visible,
    intentional diff instead of a silent drift."""

    def test_post_tool_use_double_fire_guard_matches_pm_server_hook(self):
        script = (PLUGIN_HOOKS / "post-tool-use.sh").read_text(encoding="utf-8")
        # Today the double-fire guard only recognizes the legacy manual hook.
        assert "pm-server hook" in script

    def test_session_start_probes_pm_server_mcp_key(self):
        script = (PLUGIN_HOOKS / "session-start.sh").read_text(encoding="utf-8")
        # Today the duplicate-MCP probe only checks the legacy registration key.
        assert "claude mcp get pm-server" in script


class TestLegacyUserEnvFixture:
    """The ``legacy_user_env`` fixture (conftest.py) is the scaffolding the
    ``migrate-from-pm-server`` tests in steps 3-6 drive against. Assert it is
    well-formed now so those later tests build on an independently-verified
    contract."""

    def test_legacy_user_fixture_is_well_formed(self, legacy_user_env):
        env = legacy_user_env

        # CLAUDE.md carries the invariant marker block (migrate upgrades in place).
        claude_text = env.claude_md.read_text(encoding="utf-8")
        assert "<!-- pm-server:begin" in claude_text
        assert "<!-- pm-server:end -->" in claude_text

        # settings.json: the manual post-commit hook + exactly the 3 auto-approve
        # perms a key flip would silently revert to prompting.
        settings = json.loads(env.settings_json.read_text(encoding="utf-8"))
        allow = settings["permissions"]["allow"]
        pm_perms = [p for p in allow if p.startswith("mcp__pm-server__")]
        assert len(pm_perms) == 3
        assert set(pm_perms) == set(env.perm_entries)
        hook_command = settings["hooks"]["PostToolUse"][0]["hooks"][0]["command"]
        assert hook_command == env.hook_command
        assert "pm-server hook" in hook_command

        # Codex config: user-authored tools.* sub-tables the re-key must preserve.
        codex = tomllib.loads(env.codex_config.read_text(encoding="utf-8"))
        pm = codex["mcp_servers"]["pm-server"]
        assert set(pm["tools"]) == set(env.codex_tool_subtables)
        for tool in env.codex_tool_subtables:
            assert pm["tools"][tool]["approval_mode"] == "approve"
