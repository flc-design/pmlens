"""Every registration surface must actually reach ``mcp.run()`` (PMSERV-177).

pmlens is registered with hosts through four different launch shapes:

===========================  ==========================================
surface                      launch argv
===========================  ==========================================
``plugin/.mcp.json``         ``uvx pm-server@<ver> serve``
``manifest.json`` (.mcpb)    ``uv run --directory <dir> python -m pmlens serve``
``installer`` claude-code    ``claude mcp add ... -- <path> serve``
``installer`` TOML/JSON      ``command=<path>, args=["serve"]``
===========================  ==========================================

They agree on exactly one thing that matters: the argv must terminate in
the ``serve`` subcommand. ``__main__.cli`` is a plain ``@click.group()``
with no ``invoke_without_command``, so a surface that omits ``serve``
prints usage and exits 2 — the MCP server never starts.

PMSERV-177: ``plugin/.mcp.json`` omitted ``serve`` from its first commit
(``pm-server@0.9.0``) through 0.14.0. Nothing caught it. ``test_plugin.py``
defined a ``PLUGIN_MCP`` constant and never asserted on it;
``test_version_lockstep.py`` only matched the ``pm-server@<version>``
string; ``test_installer.py`` covers the *programmatic* surfaces well (21
``serve`` occurrences) but cannot see a static JSON file. The gap was
precisely the two declarative config files, which no test read for
launch shape.

So this module tests the surfaces *as data* — parsing the shipped config
files rather than trusting the code that writes them — and then proves
over a real stdio pipe that ``serve`` does start an MCP server. The
static half catches a dropped subcommand; the wire half catches the
subcommand becoming wrong.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any, NamedTuple

import pytest

from pmlens import __version__

REPO_ROOT = Path(__file__).resolve().parent.parent
PLUGIN_MCP = REPO_ROOT / "plugin" / ".mcp.json"
MCPB_MANIFEST = REPO_ROOT / "manifest.json"

# The subcommand that reaches `mcp.run(transport="stdio")`.
SERVE = "serve"


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _plugin_launch() -> tuple[str, list[str]]:
    entry = _load(PLUGIN_MCP)["mcpServers"]["pmlens"]
    return entry["command"], list(entry["args"])


def _mcpb_launch() -> tuple[str, list[str]]:
    cfg = _load(MCPB_MANIFEST)["server"]["mcp_config"]
    return cfg["command"], list(cfg["args"])


# ─── Static surfaces: the shipped config files ────────────────────────────────


@pytest.mark.parametrize(
    "name, loader",
    [
        ("plugin/.mcp.json", _plugin_launch),
        ("manifest.json (.mcpb)", _mcpb_launch),
    ],
)
def test_declarative_surface_reaches_serve(name: str, loader):
    """The launch argv must end in the `serve` subcommand.

    Asserting on the LAST token (not merely membership) is deliberate:
    click consumes the first non-option token as the subcommand, so a
    `serve` buried mid-argv would be read as an argument to something
    else. Anchoring the position keeps the assertion honest.
    """
    command, args = loader()
    assert command, f"{name}: launch command must not be empty"
    assert args, f"{name}: launch args must not be empty — bare command cannot start the server"
    assert args[-1] == SERVE, (
        f"{name}: launch argv must end with {SERVE!r}, got {[command, *args]!r}. "
        f"Without it the CLI prints usage and exits 2 (PMSERV-177)."
    )


def test_every_declarative_surface_is_covered():
    """Guard against a new surface being added without a launch assertion.

    A future `.mcp.json`-style file that nobody parameterized above would
    silently reintroduce PMSERV-177. This test fails when the set of
    shipped MCP launch configs changes, forcing the parametrize list to
    be updated alongside it.
    """
    known = {PLUGIN_MCP, MCPB_MANIFEST}
    found = {p for p in REPO_ROOT.glob("**/.mcp.json") if ".git" not in p.parts}
    # manifest.json is the only non-".mcp.json" launch config we ship.
    unknown = sorted(str(p.relative_to(REPO_ROOT)) for p in found - known)
    assert not unknown, (
        f"unregistered MCP launch config(s): {unknown}. "
        f"Add them to test_declarative_surface_reaches_serve's parametrize list."
    )


def test_bare_cli_invocation_cannot_start_a_server():
    """The property that makes a missing `serve` fatal rather than cosmetic.

    `cli` is a plain click group. If someone later adds
    `invoke_without_command=True` (defaulting to serve), this test fails
    — which is the correct signal: the surfaces' hard `serve` requirement
    would have changed, and the assertions above should be revisited
    rather than silently kept.
    """
    result = subprocess.run(
        [sys.executable, "-m", "pmlens"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode != 0, (
        "bare `python -m pmlens` succeeded — the CLI group now runs without a "
        "subcommand. Revisit whether every launch surface still needs `serve`."
    )
    assert "Usage:" in (result.stdout + result.stderr)


# ─── Wire surface: `serve` really speaks MCP over stdio ───────────────────────


def _rpc(obj: dict[str, Any]) -> str:
    return json.dumps(obj) + "\n"


class Session(NamedTuple):
    """What one real handshake told us."""

    protocol_version: str
    server_info: dict[str, Any]
    tools: list[str]
    tool_definitions: dict[str, dict[str, Any]]
    responses: list[dict[str, Any]]


def _mcp_session(
    *,
    lens: bool,
    desktop_write: bool = False,
    calls: list[dict[str, Any]] | None = None,
    timeout: float = 90.0,
) -> Session:
    """Launch `python -m pmlens serve`, handshake, and report what came back.

    Speaks the MCP stdio framing directly (newline-delimited JSON-RPC)
    instead of going through a client library, because the point is to
    exercise the same path a host uses — process spawn, stdin/stdout
    pipes, and all.
    """
    env = os.environ.copy()
    # conftest's autouse isolated_home already redirected HOME; copying
    # os.environ propagates that to the child (ADR-048 / PMSERV-165 — a
    # subprocess that inherits the real HOME would write the developer's
    # own config).
    env["PM_LENS"] = "1" if lens else "0"
    env["PM_DESKTOP_WRITE"] = "1" if desktop_write else "0"
    env["PYTHONUNBUFFERED"] = "1"

    proc = subprocess.Popen(
        [sys.executable, "-m", "pmlens", SERVE],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )

    # readline() blocks, so a watchdog is the only way to keep a wedged
    # server from hanging the suite forever.
    watchdog = threading.Timer(timeout, proc.kill)
    watchdog.start()
    try:
        assert proc.stdin is not None and proc.stdout is not None
        proc.stdin.write(
            _rpc(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-11-25",
                        "capabilities": {},
                        "clientInfo": {"name": "pmlens-launch-surface-test", "version": "0"},
                    },
                }
            )
        )
        proc.stdin.flush()
        init = _read_response(proc, want_id=1)
        assert "result" in init, f"initialize failed: {init!r}"

        proc.stdin.write(_rpc({"jsonrpc": "2.0", "method": "notifications/initialized"}))
        proc.stdin.flush()

        proc.stdin.write(_rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}))
        proc.stdin.flush()
        listed = _read_response(proc, want_id=2)
        assert "result" in listed, f"tools/list failed: {listed!r}"
        responses = []
        for request_id, params in enumerate(calls or [], start=3):
            proc.stdin.write(
                _rpc({"jsonrpc": "2.0", "id": request_id, "method": "tools/call", "params": params})
            )
            proc.stdin.flush()
            response = _read_response(proc, want_id=request_id)
            assert "result" in response, f"tools/call failed: {response!r}"
            assert not response["result"].get("isError"), response
            responses.append(response["result"])
        return Session(
            protocol_version=init["result"].get("protocolVersion", ""),
            server_info=init["result"].get("serverInfo", {}),
            tools=[t["name"] for t in listed["result"]["tools"]],
            tool_definitions={t["name"]: t for t in listed["result"]["tools"]},
            responses=responses,
        )
    finally:
        watchdog.cancel()
        proc.kill()
        proc.wait(timeout=30)


def _read_response(proc: subprocess.Popen[str], *, want_id: int) -> dict[str, Any]:
    """Read stdout until the response with `want_id` arrives.

    Skips non-JSON lines and notifications rather than assuming the
    server's first line of output is the answer — a stray banner or a
    server-initiated notification would otherwise be misread as the
    response.
    """
    assert proc.stdout is not None
    while True:
        line = proc.stdout.readline()
        if not line:
            stderr = proc.stderr.read() if proc.stderr else ""
            raise AssertionError(
                f"server closed stdout before responding to id={want_id}. stderr={stderr!r}"
            )
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue  # non-protocol chatter on stdout
        if msg.get("id") == want_id:
            return msg


@pytest.mark.smoke
def test_serve_speaks_mcp_over_stdio():
    """`serve` completes a real handshake and lists tools.

    This is what the static assertions above cannot prove: that the
    subcommand they require actually brings up a working MCP server.
    """
    session = _mcp_session(lens=False)
    assert session.tools, "tools/list returned nothing over the wire"
    assert "pm_status" in session.tools


@pytest.mark.smoke
@pytest.mark.parametrize("lens", [False, True], ids=["full", "lens"])
def test_handshake_reports_pmlens_identity(lens: bool) -> None:
    """The wire identity must identify pmlens itself (PMSERV-179)."""
    session = _mcp_session(lens=lens)
    assert session.server_info.get("name") == "pmlens"
    assert session.server_info.get("version") == __version__, (
        f"serverInfo must report the pmlens version {__version__!r}, got {session.server_info!r}"
    )


@pytest.mark.smoke
def test_lens_mode_exposes_a_strict_subset_over_the_wire():
    """PM_LENS=1 narrows the wire surface, and only ever narrows it.

    Asserting subset + strictly-smaller rather than an exact count keeps
    this from breaking every time a tool is added, while still failing if
    the read-only gating stops applying or starts leaking a write tool.
    """
    full = set(_mcp_session(lens=False).tools)
    lens = set(_mcp_session(lens=True).tools)
    assert lens, "Lens mode exposed no tools at all"
    assert lens < full, (
        f"Lens mode must expose a strict subset of the default surface. "
        f"only-in-lens={sorted(lens - full)}, lens={len(lens)}, full={len(full)}"
    )


@pytest.mark.smoke
def test_content_aliases_share_schemas_and_state_over_stdio(tmp_project: Path) -> None:
    """Both MCP names work against one existing-format DB, with identical schemas."""
    (tmp_project / ".pm" / "project.yaml").write_text(
        "name: aliases\ndisplay_name: Aliases\nversion: 0.0.1\n"
        "status: development\nstarted: 2026-01-01\ndescription: alias test\nphases: []\n",
        encoding="utf-8",
    )
    args = {
        "signal_type": "lesson",
        "source_refs": ["memory:1"],
        "raw_content": "private concentrate",
        "hook": "a draft hook",
        "project_path": str(tmp_project),
    }
    listing = {"filter_status": "all", "limit": 1, "offset": 0, "project_path": str(tmp_project)}
    session = _mcp_session(
        lens=False,
        calls=[
            {"name": "pm_draft_content", "arguments": args},
            {"name": "pm_draft_x", "arguments": args},
            {"name": "pm_drafts_pending", "arguments": listing},
            {"name": "pm_x_drafts_pending", "arguments": listing},
        ],
    )
    for preferred, legacy in (
        ("pm_draft_content", "pm_draft_x"),
        ("pm_drafts_pending", "pm_x_drafts_pending"),
    ):
        new = session.tool_definitions[preferred]
        old = session.tool_definitions[legacy]
        assert old["inputSchema"] == new["inputSchema"]
        assert old.get("outputSchema") == new.get("outputSchema")
        assert preferred in old["description"]

    replies = [json.loads(response["content"][0]["text"]) for response in session.responses]
    saved, duplicate, preferred_page, legacy_page = replies
    assert saved["status"] == "saved"
    assert duplicate["status"] == "skipped"
    assert duplicate["warnings"][0]["existing_ids"] == [saved["draft_id"]]
    assert preferred_page == legacy_page
    assert preferred_page["total"] == 1
    assert preferred_page["items"][0]["id"] == saved["draft_id"]
    assert "private concentrate" not in json.dumps(preferred_page)
    assert "a draft hook" not in json.dumps(preferred_page)
    assert (tmp_project / ".pm" / "x_drafts.db").is_file()
    assert not (tmp_project / ".pm" / "drafts.db").exists()


@pytest.mark.smoke
@pytest.mark.parametrize("desktop_write", [False, True], ids=["lens", "desktop"])
def test_content_aliases_are_absent_from_lens_wire_surface(desktop_write: bool) -> None:
    session = _mcp_session(lens=True, desktop_write=desktop_write)
    assert not {"pm_draft_content", "pm_draft_x", "pm_drafts_pending", "pm_x_drafts_pending"} & set(
        session.tools
    )


@pytest.mark.smoke
def test_handshake_reports_a_protocol_version(record_property):
    """Record which MCP revision we negotiate — deliberately without pinning it.

    pmlens has no handshake code of its own; `__main__` hands the base
    protocol to the SDK, so the advertised `protocolVersion` is a
    third-party constant. Asserting a literal value here would go red the
    day the SDK legitimately moves forward (e.g. `mcp` 2.x, which speaks
    the 2026-07-28 revision) — a false failure about someone else's
    release, not a pmlens regression.

    What IS worth pinning is that a version is negotiated at all and that
    it is revision-shaped. The concrete value is attached to the test
    record instead, so `--junitxml` and a verbose run show which era this
    build actually speaks. That matters because the 2026-07-28 revision
    removes the `initialize` handshake entirely in favour of
    `server/discover`; when the SDK crosses that line, this test keeps
    passing and the recorded value is how you notice.
    """
    session = _mcp_session(lens=False)
    version = session.protocol_version
    assert version, "server negotiated no protocolVersion at all"
    parts = version.split("-")
    assert len(parts) == 3 and all(p.isdigit() for p in parts), (
        f"protocolVersion {version!r} is not revision-shaped (YYYY-MM-DD)"
    )
    record_property("mcp_protocol_version", version)
    record_property("mcp_server_info", json.dumps(session.server_info, sort_keys=True))
