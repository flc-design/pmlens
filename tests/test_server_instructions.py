"""MCP server instructions (PMSERV-201, ADR-055).

The instructions ship with the server build, so they are the one place where
tool names can never drift from the running server — but only if every name
they mention is a tool registered in that mode. A model that follows
instructions literally will try to call whatever is named, so a dangling name
is a real defect (the template's ``pm_drafts_pending`` was exactly that on
pmlens 0.15.1).
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from pmlens import server
from pmlens.server import (
    OUTBOX_READ_ALLOWLIST,
    OUTBOX_WRITE_ALLOWLIST,
    RO_ALLOWLIST,
    build_server_instructions,
)

_TOOL_NAME = re.compile(r"\bpm_[a-z_]+\b")

MODES = {
    "full": {"lens": False, "desktop_write": False},
    "lens": {"lens": True, "desktop_write": False},
    "lens+outbox": {"lens": True, "desktop_write": True},
}


def _registered_in(mode: str) -> set[str]:
    if mode == "full":
        # Every tool the module defines; the default test process runs full
        # mode, where the decorator registers them all.
        return set(server.REGISTERED_TOOLS)
    allowed = set(RO_ALLOWLIST) | set(OUTBOX_READ_ALLOWLIST)
    if mode == "lens+outbox":
        allowed |= set(OUTBOX_WRITE_ALLOWLIST)
    return allowed


@pytest.mark.parametrize("mode", MODES)
def test_every_named_tool_is_registered_in_that_mode(mode):
    text = build_server_instructions(**MODES[mode])
    named = set(_TOOL_NAME.findall(text))
    assert named, "instructions should point at the tools they describe"
    assert named <= _registered_in(mode), named - _registered_in(mode)


@pytest.mark.parametrize("mode", MODES)
def test_fits_the_host_budget(mode):
    # Claude Code truncates long server instructions; stay well inside.
    assert len(build_server_instructions(**MODES[mode])) <= 2048


def test_full_mode_leads_with_the_start_routine():
    text = build_server_instructions(**MODES["full"])
    head = text[:512]
    assert "pm_status" in head
    assert "warnings[]" in head


def test_full_mode_keeps_the_safety_constraints():
    text = build_server_instructions(**MODES["full"])
    assert "pm_redact_draft" in text
    assert "raw_content" in text
    assert "never post or send" in text
    assert "user_approval" in text
    # Codex cross-check: the draft rules are scoped to pmlens's own drafts.
    assert "content pipeline" in text


def test_full_mode_says_new_adrs_start_proposed():
    # ADR-056 S1 / ADR-059: pm_add_decision records an ADR as proposed by
    # default; agreeing to record it is not accepting its content.
    text = build_server_instructions(**MODES["full"])
    assert "pm_add_decision" in text
    assert "proposed" in text


def test_full_mode_says_how_a_proposed_adr_becomes_adopted():
    # Design §4.1 【Q1=b】: "...; it is saved as proposed, and becomes adopted
    # with pm_update_decision once the user accepts its content." The tool is
    # registered in full mode (PMSERV-224), so the sentence can name it.
    text = build_server_instructions(**MODES["full"])
    assert "pm_update_decision" in server.REGISTERED_TOOLS
    assert "becomes adopted with pm_update_decision once the user accepts its content" in text


# Tool names that texts in server.py (tool docstrings, warning messages and
# remediations) may mention before the tool is registered, with the work that
# registers it. An entry must go when its tool lands: the next test fails
# until it does, and the instructions test above then asks for the full text.
_NAMED_BEFORE_REGISTERED = {
    "pm_outbox_merge_artifact": "outbox phase 2.2",
}


def _tool_names_in_server_strings() -> dict[str, set[int]]:
    """pm_* names in every string literal of server.py, with their line numbers.

    Docstrings and the literal parts of f-strings are string constants too, so
    this covers tool descriptions, warning messages and remediations.
    """
    tree = ast.parse(Path(server.__file__).read_text(encoding="utf-8"))
    found: dict[str, set[int]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            for name in _TOOL_NAME.findall(node.value):
                found.setdefault(name, set()).add(node.lineno)
    return found


def test_server_texts_name_only_registered_or_pending_tools():
    named = _tool_names_in_server_strings()
    allowed = set(server.REGISTERED_TOOLS) | set(_NAMED_BEFORE_REGISTERED)
    unknown = {name: sorted(lines) for name, lines in named.items() if name not in allowed}
    assert not unknown, unknown


def test_pending_tool_names_are_not_registered_yet():
    landed = set(_NAMED_BEFORE_REGISTERED) & set(server.REGISTERED_TOOLS)
    assert not landed, (
        f"{sorted(landed)} is registered now: drop it from _NAMED_BEFORE_REGISTERED "
        "and finish the texts that waited for it (the full-mode instructions included)"
    )


@pytest.mark.parametrize("mode", ["lens", "lens+outbox"])
def test_lens_modes_say_they_show_decisions(mode):
    # Both Lens texts list what they show; decisions are part of it.
    text = build_server_instructions(**MODES[mode])
    assert "decisions" in text


@pytest.mark.parametrize("mode", ["lens", "lens+outbox"])
def test_lens_modes_point_to_a_full_mode_host(mode):
    text = build_server_instructions(**MODES[mode])
    assert "full-mode pmlens host" in text
    assert "pm_update_task" not in text


def test_the_running_server_carries_its_mode_instructions():
    expected = build_server_instructions(
        lens=server.PM_LENS_ENABLED, desktop_write=server.PM_DESKTOP_WRITE_ENABLED
    )
    assert server.mcp.instructions == expected
