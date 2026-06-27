"""Phase-3 identity-flip POSITIVE guards (PMSERV-137 / ADR-034, step 6).

The companion ``test_phase3_rename_guards.py`` deliberately holds only the
INVARIANTS the rename must not break — it omits the positive
``FastMCP("pmlens")`` / manifest-name assertions because a guard written in the
same commit as the value it guards cannot protect THAT commit. Those positive
assertions land HERE, in step 6, alongside the values they assert, as
REGRESSION guards (they catch a future accidental revert) and are backstopped by
the INDEPENDENT mechanical grep gate in ``.github/workflows/ci.yml``
(``grep -c 'FastMCP("pm-server")' src`` == 0).

Scope note: this guards the user-facing identity flipped in step 6 — the FastMCP
server name, the .mcpb manifest top-level name, and the plugin ``.mcp.json``
registration key. The ``pm-server`` PyPI wrapper, the legacy binary name, and
the CLAUDE.md marker slug deliberately stay ``pm-server`` (their invariants live
in the companion file).
"""

from __future__ import annotations

import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
MANIFEST = REPO_ROOT / "manifest.json"
SERVER_PY = REPO_ROOT / "src" / "pmlens" / "server.py"
PLUGIN_MCP = REPO_ROOT / "plugin" / ".mcp.json"


class TestFastMcpIdentity:
    """The live MCP server now reports the ``pmlens`` identity. This is what
    activates the dormant ``legacy_pm_server_awareness`` probe (server.py gates
    it on ``mcp.name == "pmlens"``)."""

    def test_fastmcp_name_is_pmlens(self):
        from pmlens.server import mcp

        assert mcp.name == "pmlens"

    def test_server_source_has_no_legacy_fastmcp_name(self):
        # Mirrors the CI grep gate: the legacy FastMCP("pm-server") must be gone
        # and the new name present.
        src = SERVER_PY.read_text(encoding="utf-8")
        assert 'FastMCP("pm-server")' not in src
        assert 'FastMCP("pmlens")' in src


class TestManifestIdentity:
    """The .mcpb (Claude Desktop/Cowork) bundle's top-level name is the install
    identity; the human ``display_name`` stays the PM Lens label."""

    def test_mcpb_manifest_name_is_pmlens(self):
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        assert manifest["name"] == "pmlens"
        assert manifest["display_name"] == "PM Lens (Read-only)"

    def test_mcpb_manifest_has_no_pm_server_import_name(self):
        # The bundle invokes ``-m pmlens``; the legacy ``pm_server`` import name
        # must not leak into the entry_point/args.
        assert "pm_server" not in MANIFEST.read_text(encoding="utf-8")


class TestPluginMcpKey:
    """Claude Code plugin users get the bundled MCP under the ``pmlens`` key, so
    its tools namespace as ``mcp__pmlens__*`` (matching the Desktop identity)."""

    def test_plugin_mcp_registers_under_pmlens_key(self):
        mcp_cfg = json.loads(PLUGIN_MCP.read_text(encoding="utf-8"))
        assert "pmlens" in mcp_cfg["mcpServers"]
        assert "pm-server" not in mcp_cfg["mcpServers"]
