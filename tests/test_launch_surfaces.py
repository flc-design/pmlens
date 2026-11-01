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
    assert (tmp_project / ".pm" / "drafts.db").is_file()
    assert not (tmp_project / ".pm" / "x_drafts.db").exists()


@pytest.mark.smoke
@pytest.mark.parametrize("desktop_write", [False, True], ids=["lens", "desktop"])
def test_content_aliases_are_absent_from_lens_wire_surface(desktop_write: bool) -> None:
    session = _mcp_session(lens=True, desktop_write=desktop_write)
    assert not {"pm_draft_content", "pm_draft_x", "pm_drafts_pending", "pm_x_drafts_pending"} & set(
        session.tools
    )


@pytest.mark.smoke
@pytest.mark.parametrize("desktop_write", [False, True], ids=["lens", "desktop"])
def test_lens_wire_surface_reads_decisions_but_cannot_write_them(desktop_write: bool) -> None:
    """Decision Lineage S1 (D9): the ADR reader is on the Lens wire, its writers are not.

    S2 adds the decision posture's change tool, the explanation writer and the
    feedback writer to the writers (S2 spec §6.1 D9); pm_status, which shows
    the posture, stays on the wire.
    """
    tools = set(_mcp_session(lens=True, desktop_write=desktop_write).tools)
    assert {"pm_decision_query", "pm_status"} <= tools
    writers = {
        "pm_add_decision",
        "pm_update_decision",
        "pm_update_decision_posture",
        "pm_add_explanation",
        "pm_add_feedback",
    }
    assert not writers & tools


@pytest.mark.smoke
def test_decisions_round_trip_over_stdio(tmp_project: Path) -> None:
    """pm_add_decision then pm_decision_query (list, filtered list, get) over the wire.

    Proves the tool's argument schema and that its responses serialize as JSON
    through a real host path, not just as Python dicts.
    """
    project = str(tmp_project)
    session = _mcp_session(
        lens=False,
        calls=[
            {
                "name": "pm_add_decision",
                "arguments": {
                    "title": "Use SQLite",
                    "context": "c",
                    "decision": "d",
                    "origin": "ai_auto",
                    "project_path": project,
                },
            },
            {"name": "pm_decision_query", "arguments": {"project_path": project}},
            {
                "name": "pm_decision_query",
                "arguments": {"action": "list", "lifecycle": "adopted", "project_path": project},
            },
            {
                "name": "pm_decision_query",
                "arguments": {"action": "get", "decision_id": "ADR-001", "project_path": project},
            },
        ],
    )
    schema = session.tool_definitions["pm_decision_query"]["inputSchema"]
    assert set(schema["properties"]) == {
        "action",
        "decision_id",
        "lifecycle",
        "limit",
        "offset",
        "project_path",
    }
    assert not schema.get("required")

    added, listed, adopted, got = (
        json.loads(response["content"][0]["text"]) for response in session.responses
    )
    assert added["status"] == "recorded" and added["decision_id"] == "ADR-001"
    assert [
        (row["id"], row["lifecycle"], row["declared_origin"]) for row in listed["decisions"]
    ] == [("ADR-001", "proposed", "ai_auto")]
    assert listed["has_more"] is False and listed["next_offset"] == 1
    assert adopted["count"] == 0 and adopted["total"] == 1
    assert got["decision"]["status"] == "proposed"
    assert got["lineage"]["derived"] is False
    assert got["lineage"]["declared"]["origin"] == "ai_auto"
    assert got["lineage"]["events"][0]["kind"] == "created"


def _object_of_string_lists(schema: dict[str, Any]) -> bool:
    """True when ``schema`` (or one of its anyOf branches) is a dict[str, list[str]]."""
    for branch in schema.get("anyOf", [schema]):
        values = branch.get("additionalProperties")
        if (
            branch.get("type") == "object"
            and isinstance(values, dict)
            and values.get("type") == "array"
            and values.get("items", {}).get("type") == "string"
        ):
            return True
    return False


@pytest.mark.smoke
def test_decision_update_round_trips_over_stdio(tmp_project: Path) -> None:
    """pm_add_decision → pm_update_decision → pm_decision_query over the wire (design §8.2).

    Proves pm_update_decision's argument schema (``dict[str, list[str]]`` for
    the links included) and that its response serializes as JSON through a
    real host path.
    """
    project = str(tmp_project)

    def add(title: str) -> dict[str, Any]:
        return {
            "name": "pm_add_decision",
            "arguments": {"title": title, "context": "c", "decision": "d", "project_path": project},
        }

    session = _mcp_session(
        lens=False,
        calls=[
            add("Use SQLite"),
            add("Use SQLite with WAL"),
            {
                "name": "pm_update_decision",
                "arguments": {
                    "decision_id": "ADR-002",
                    "lifecycle": "adopted",
                    "reason": "the user approved the design",
                    "add_links": {"amends": ["ADR-001"]},
                    "project_path": project,
                },
            },
            {
                "name": "pm_decision_query",
                "arguments": {"action": "list", "lifecycle": "adopted", "project_path": project},
            },
            {
                "name": "pm_decision_query",
                "arguments": {"action": "get", "decision_id": "ADR-001", "project_path": project},
            },
        ],
    )
    schema = session.tool_definitions["pm_update_decision"]["inputSchema"]
    assert set(schema["properties"]) == {
        "decision_id",
        "lifecycle",
        "reason",
        "add_links",
        "remove_links",
        "evaluation",
        "evaluation_kind",
        "note",
        "origin",
        "recorded_timing",
        "fact_core",
        "expected_fact_core_version",
        "caused_by",
        "project_path",
    }
    assert schema.get("required") == ["decision_id"]
    assert _object_of_string_lists(schema["properties"]["add_links"])
    assert _object_of_string_lists(schema["properties"]["remove_links"])
    # S2 spec §3.1: the fact core is a list of {id or kind, text, ...} objects.
    assert {"type": "array", "items": {"type": "object", "additionalProperties": True}} in (
        schema["properties"]["fact_core"]["anyOf"]
    )
    assert {"type": "integer"} in schema["properties"]["expected_fact_core_version"]["anyOf"]
    # S2 spec §3.1: caused_by is a list of feedback ids.
    assert {"type": "array", "items": {"type": "string"}} in (
        schema["properties"]["caused_by"]["anyOf"]
    )

    _first, _second, updated, adopted, got = (
        json.loads(response["content"][0]["text"]) for response in session.responses
    )
    assert updated["status"] == "updated"
    assert (updated["lifecycle"], updated["decision_status"]) == ("adopted", "accepted")
    assert updated["links"]["amends"] == ["ADR-001"]
    assert updated["events_added"] == ["lifecycle", "link"]  # design §2.4 order
    assert "decision_lifecycle_changed" in {w["code"] for w in updated["warnings"]}
    assert [(row["id"], row["status"]) for row in adopted["decisions"]] == [("ADR-002", "accepted")]
    assert got["lineage"]["linked_from"] == {"supersedes": [], "amends": ["ADR-002"]}


@pytest.mark.smoke
def test_explanation_round_trips_over_stdio(tmp_project: Path) -> None:
    """pm_update_decision (fact core) → pm_add_explanation → get over the wire (S2 spec §6.3).

    Proves pm_add_explanation's argument schema (``list[dict]`` paragraphs and a
    ``dict | None`` question) and that its response serializes as JSON
    through a real host path in full mode.
    """
    project = str(tmp_project)
    fact_core = [
        {"kind": "what", "text": "Send the booking mail without the queue"},
        {"kind": "change", "text": "Before: through the queue. After: inside the request"},
        {"kind": "revert", "text": "Revert the setting", "reversibility": "unknown"},
        {"kind": "weak", "text": "A slow mail server slows every booking"},
        {"kind": "uncertain", "text": "Peak volume was not measured", "confidence": "medium"},
    ]
    ids = ["F-what", "F-change", "F-revert", "F-weak", "F-uncertain"]
    session = _mcp_session(
        lens=False,
        calls=[
            {
                "name": "pm_add_decision",
                "arguments": {
                    "title": "Send mail inline",
                    "context": "c",
                    "decision": "d",
                    "project_path": project,
                },
            },
            {
                "name": "pm_update_decision",
                "arguments": {
                    "decision_id": "ADR-001",
                    "fact_core": fact_core,
                    "expected_fact_core_version": 0,
                    "project_path": project,
                },
            },
            {
                "name": "pm_add_explanation",
                "arguments": {
                    "decision_id": "ADR-001",
                    "fact_core_version": 1,
                    "paragraphs": [{"fact_ids": [i], "text": f"About {i}."} for i in ids],
                    "question": {
                        "question_kind": "judgment",
                        "fact_ids": ["F-weak"],
                        "text": "Is the slower booking acceptable?",
                    },
                    "project_path": project,
                },
            },
            {
                "name": "pm_decision_query",
                "arguments": {"action": "get", "decision_id": "ADR-001", "project_path": project},
            },
        ],
    )
    schema = session.tool_definitions["pm_add_explanation"]["inputSchema"]
    assert set(schema["properties"]) == {
        "decision_id",
        "fact_core_version",
        "paragraphs",
        "question",
        "project_path",
    }
    assert schema.get("required") == ["decision_id", "fact_core_version", "paragraphs"]
    assert schema["properties"]["paragraphs"]["type"] == "array"
    assert schema["properties"]["paragraphs"]["items"]["type"] == "object"
    assert {"type": "null"} in schema["properties"]["question"]["anyOf"]

    _added, revised, explained, got = (
        json.loads(response["content"][0]["text"]) for response in session.responses
    )
    assert revised["status"] == "updated"
    assert explained == {
        "status": "recorded",
        "decision_id": "ADR-001",
        "explanation_id": "ADR-001.EX-1",
        "fact_core_version": 1,
        "fact_ids_present": True,
        "accuracy": "unconfirmed",
        "paragraphs": 5,
        "warnings": [],
    }
    [row] = got["lineage"]["explanations"]["items"]
    assert (row["id"], row["stale"], row["accuracy"]) == ("ADR-001.EX-1", False, "unconfirmed")
    assert row["question"]["question_kind"] == "judgment"


@pytest.mark.smoke
def test_feedback_round_trips_over_stdio(tmp_project: Path) -> None:
    """pm_add_feedback → pm_update_decision (caused_by) → get over the wire (S2 spec §6.3).

    Proves pm_add_feedback's argument schema (``list[str] | None`` fact ids
    and a defaulted respondent_scope) and caused_by's, and that the responses
    serialize as JSON through a real host path in full mode.
    """
    project = str(tmp_project)
    fact_core = [
        {"kind": "what", "text": "Send the booking mail without the queue"},
        {"kind": "change", "text": "Before: through the queue. After: inside the request"},
        {"kind": "revert", "text": "Revert the setting", "reversibility": "unknown"},
        {"kind": "weak", "text": "A slow mail server slows every booking"},
        {"kind": "uncertain", "text": "Peak volume was not measured", "confidence": "medium"},
    ]
    revised = [
        {"id": item, "text": f"{fact['text']} (2)"}
        | {key: fact[key] for key in ("reversibility", "confidence") if key in fact}
        for item, fact in zip(
            ["F-what", "F-change", "F-revert", "F-weak", "F-uncertain"], fact_core, strict=True
        )
    ]
    session = _mcp_session(
        lens=False,
        calls=[
            {
                "name": "pm_add_decision",
                "arguments": {
                    "title": "Send mail inline",
                    "context": "c",
                    "decision": "d",
                    "project_path": project,
                },
            },
            {
                "name": "pm_update_decision",
                "arguments": {
                    "decision_id": "ADR-001",
                    "fact_core": fact_core,
                    "expected_fact_core_version": 0,
                    "project_path": project,
                },
            },
            {
                "name": "pm_add_feedback",
                "arguments": {
                    "decision_id": "ADR-001",
                    "feedback_kind": "unacceptable",
                    "text": "A slow booking is not acceptable",
                    "fact_ids": ["F-weak"],
                    "project_path": project,
                },
            },
            {
                "name": "pm_update_decision",
                "arguments": {
                    "decision_id": "ADR-001",
                    "fact_core": revised,
                    "expected_fact_core_version": 1,
                    "caused_by": ["ADR-001.FB-1"],
                    "project_path": project,
                },
            },
            {
                "name": "pm_decision_query",
                "arguments": {"action": "get", "decision_id": "ADR-001", "project_path": project},
            },
        ],
    )
    schema = session.tool_definitions["pm_add_feedback"]["inputSchema"]
    assert set(schema["properties"]) == {
        "decision_id",
        "feedback_kind",
        "text",
        "explanation_id",
        "fact_ids",
        "respondent_scope",
        "project_path",
    }
    assert schema.get("required") == ["decision_id", "feedback_kind", "text"]
    assert {"type": "array", "items": {"type": "string"}} in (
        schema["properties"]["fact_ids"]["anyOf"]
    )
    assert schema["properties"]["respondent_scope"]["default"] == "unknown"

    _added, first, replied, second, got = (
        json.loads(response["content"][0]["text"]) for response in session.responses
    )
    assert first["status"] == "updated"
    assert replied["status"] == "recorded"
    assert (replied["feedback_id"], replied["fact_core_version"]) == ("ADR-001.FB-1", 1)
    assert (replied["respondent_scope"], replied["review"]) == (
        "unknown",
        "no_review_by_able_person",
    )
    assert second["status"] == "updated" and second["fact_core"]["version"] == 2
    fact_core_events = [e for e in got["lineage"]["events"] if e["kind"] == "fact_core"]
    assert fact_core_events[-1]["caused_by"] == ["ADR-001.FB-1"]
    [row] = got["lineage"]["feedback"]["items"]
    assert (row["id"], row["feedback_kind"], row["fact_ids"]) == (
        "ADR-001.FB-1",
        "unacceptable",
        ["F-weak"],
    )
    assert got["lineage"]["declared_respondent_scope"]["label"] == "none"


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
