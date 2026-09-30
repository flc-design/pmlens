"""MCP server instructions (PMSERV-201, ADR-055).

The instructions ship with the server build, so they are the one place where
tool names can never drift from the running server — but only if every name
they mention is a tool registered in that mode. A model that follows
instructions literally will try to call whatever is named, so a dangling name
is a real defect (the template's ``pm_drafts_pending`` was exactly that on
pmlens 0.15.1).
"""

from __future__ import annotations

import re

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
