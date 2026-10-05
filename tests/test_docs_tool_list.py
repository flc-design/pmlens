"""The documented tool lists follow server.py (Decision Lineage S1, PMSERV-223).

A tool added to server.py without a line in the user-facing lists is invisible
to the people who read them, and the Desktop bundle's description of its
read-only allowlist is the one place a Lens user learns what it can call.
These checks read the shipped files as data:

* manifest.json's long_description names exactly the Lens allowlist
  (``RO_ALLOWLIST``), no more and no fewer.
* Every ``@_tool()`` name appears in README.md, README.ja.md and both
  cheatsheets, matched as a whole name (``pm_knowledge`` is not found inside
  ``pm_knowledge_query``).
* The tool counts those files state match the number of ``@_tool()``
  functions, less the two compatibility aliases.
* The counts stated without a tool list follow server.py as well: the
  documentation index (docs/README.md) and the content-tool migration guide,
  whose Full / Lens / Lens + Desktop outbox name counts follow the
  allowlists.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import pytest

from pmlens import server

REPO_ROOT = Path(__file__).resolve().parent.parent

# Legacy names kept as aliases of the content tools (docs/content-tool-migration.md).
_ALIASES = frozenset({"pm_draft_x", "pm_x_drafts_pending"})

_TOOL_LIST_DOCS = ("README.md", "README.ja.md", "docs/cheatsheet.md", "docs/cheatsheet.ja.md")

# How each file states the count, e.g. "45 MCP tools + 2 compatibility aliases".
_COUNT_PATTERNS = {
    "README.md": r"(\d+) (?:MCP )?tools \+ 2 compatibility aliases",
    "README.ja.md": r"(\d+) ?(?:の )?(?:MCP )?ツール \+ 互換名2個",
    "docs/cheatsheet.md": r"(\d+) (?:MCP )?tools \+ 2 compatibility aliases",
    "docs/cheatsheet.ja.md": r"(\d+) ?(?:の )?(?:MCP )?ツール \+ 互換名2個",
}


def _decorated_tools() -> set[str]:
    """Every ``@_tool()`` function in server.py, read from source (any PM_LENS)."""
    tree = ast.parse(Path(server.__file__).read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for deco in node.decorator_list:
            if (
                isinstance(deco, ast.Call)
                and isinstance(deco.func, ast.Name)
                and deco.func.id == "_tool"
            ):
                names.add(node.name)
    return names


_ALL_TOOLS = _decorated_tools()


def _mentions(text: str, name: str) -> bool:
    return re.search(rf"(?<![A-Za-z0-9_]){re.escape(name)}(?![A-Za-z0-9_])", text) is not None


def _manifest_allowlist() -> set[str]:
    manifest = json.loads((REPO_ROOT / "manifest.json").read_text(encoding="utf-8"))
    match = re.search(r"read-only tool allowlist \(([^)]*)\)", manifest["long_description"])
    assert match, "manifest long_description no longer lists the read-only allowlist"
    return {name.strip() for name in match.group(1).split(",")}


def test_the_sources_are_populated():
    assert len(_ALL_TOOLS) >= 40
    assert _ALIASES <= _ALL_TOOLS
    assert {"pm_add_decision", "pm_decision_query"} <= _ALL_TOOLS
    assert "pm_decision_query" in server.RO_ALLOWLIST


def test_the_manifest_names_exactly_the_lens_allowlist():
    assert _manifest_allowlist() == set(server.RO_ALLOWLIST)


@pytest.mark.parametrize("relpath", _TOOL_LIST_DOCS)
def test_every_tool_is_listed(relpath: str):
    text = (REPO_ROOT / relpath).read_text(encoding="utf-8")
    missing = sorted(name for name in _ALL_TOOLS if not _mentions(text, name))
    assert not missing, f"{relpath} does not list: {missing}"


@pytest.mark.parametrize("relpath", _TOOL_LIST_DOCS)
def test_the_stated_tool_count_matches(relpath: str):
    text = (REPO_ROOT / relpath).read_text(encoding="utf-8")
    counts = [int(found) for found in re.findall(_COUNT_PATTERNS[relpath], text)]
    assert counts, f"{relpath} states no tool count"
    expected = len(_ALL_TOOLS) - len(_ALIASES)
    assert set(counts) == {expected}, f"{relpath} states {counts}, server.py has {expected}"


def test_the_docs_index_count_matches():
    text = (REPO_ROOT / "docs/README.md").read_text(encoding="utf-8")
    counts = [int(found) for found in re.findall(r"(\d+) MCP tools, Lens mode", text)]
    assert counts == [len(_ALL_TOOLS) - len(_ALIASES)], f"docs/README.md states {counts}"


def test_the_migration_guide_counts_match():
    text = (REPO_ROOT / "docs/content-tool-migration.md").read_text(encoding="utf-8")
    full = re.search(r"Full mode exposes (\d+) MCP\s+tool names for (\d+) operations", text)
    lens = re.search(r"Lens mode\s+exposes (\d+) names, or (\d+) with Desktop outbox", text)
    assert full and lens, "docs/content-tool-migration.md no longer states the name counts"
    lens_names = (server.RO_ALLOWLIST | server.OUTBOX_READ_ALLOWLIST) & _ALL_TOOLS
    desktop_names = lens_names | (server.OUTBOX_WRITE_ALLOWLIST & _ALL_TOOLS)
    assert (int(full[1]), int(full[2])) == (len(_ALL_TOOLS), len(_ALL_TOOLS) - len(_ALIASES))
    assert (int(lens[1]), int(lens[2])) == (len(lens_names), len(desktop_names))


def test_the_whole_name_match_has_teeth():
    assert not _mentions("`pm_knowledge_query`", "pm_knowledge")
    assert _mentions("| `pm_knowledge` |", "pm_knowledge")
