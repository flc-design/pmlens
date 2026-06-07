"""Architecture fitness: the read-only (Lens / ``RO_ALLOWLIST``) tool surface is
statically *disjoint* from git branch detection and ``subprocess`` shell-out.

ADR-028 / PMSERV-125. Branch-aware recall rests on a hard invariant: branch
*detection* (:func:`pm_server.discovery.read_git_branch`, which reads
``.git/HEAD``) happens only on the WRITE path (``pm_session_summary``), and the
read-only surface never shells out (``subprocess``). The suite already has two
*simple* guards for this:

* ``test_server_does_not_import_subprocess`` — ``server.py`` source contains no
  ``import subprocess`` (a single string match on one module).
* ``test_recall_with_track_never_invokes_git_detection`` — ``pm_recall``, run
  once, does not touch a monkeypatched ``read_git_branch`` (one dynamic path).

This module strengthens both into a **complete static reachability proof**: it
builds a call graph over the *entire* ``pm_server`` package and asserts that the
forward closure from EVERY ``RO_ALLOWLIST`` tool is disjoint from (a) the
functions that call ``read_git_branch`` and (b) the functions that touch
``subprocess`` — across all branches/inputs of every reachable function, not
just the one path an example invocation happens to take.

Soundness (no false negatives). If any RO tool could transitively reach a sink,
the function that physically performs the sink call is itself reachable (hence
in the closure) AND is recorded as a sink caller — so the closure and the
sink-caller set intersect and the assertion fires. Two design choices keep this
sound:

* **Bare-name call edges.** Calls are matched on their unqualified name, which
  over-approximates the graph (only ever ADDS edges, never drops a real one) —
  the safe direction for a security invariant. ``read_git_branch`` and the
  ``subprocess``-using ``installer`` functions all have globally-unique names,
  so this yields no false positives here (verified: the module stays green).
* **Import-alias resolution.** Sink detection is resolved through each module's
  import aliases, so ``from .discovery import read_git_branch as rgb; rgb()``
  and ``import subprocess as sp; sp.run()`` are caught too — a bare-name match
  alone would miss the renamed form, which is the obvious way a refactor (or an
  adversary) could otherwise slip a sink onto the RO surface.
"""

from __future__ import annotations

import ast
import textwrap
from pathlib import Path

import pm_server
import pm_server.server as srv

_PKG_DIR = Path(pm_server.__file__).parent

# Sinks the read-only surface must never reach.
_GIT_DETECT_FN = "read_git_branch"  # reads .git/HEAD — write-path only
_WRITE_PATH_FN = "pm_session_summary"  # the sole legitimate read_git_branch caller
_SUBPROCESS = "subprocess"


def _callee_bare_name(call: ast.Call) -> str | None:
    """Return the unqualified callee name of an ``ast.Call`` node.

    ``foo(...)`` -> ``"foo"``; ``obj.method(...)`` -> ``"method"``;
    anything more exotic (``foo()(...)``, subscripts) -> ``None``. We match on
    the bare name because resolving the concrete receiver type of an attribute
    call from source alone is undecidable in general — bare-name matching
    over-approximates the graph, which is the sound direction (see module
    docstring).
    """
    func = call.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _sink_alias_map(tree: ast.Module) -> dict[str, str]:
    """Map import-bound local names to a canonical sink token.

    Only sink-relevant imports are tracked::

        import subprocess [as sp]              -> {sp|subprocess: "subprocess"}
        from subprocess import run [as r]      -> {r|run: "subprocess"}
        from ... import read_git_branch [as g] -> {g|read_git_branch: "read_git_branch"}

    Aliases are collected module-wide (function-level imports included) and
    applied module-wide — an over-approximation, which is the sound direction.
    Resolving through this map makes sink detection robust to ``as`` aliasing,
    which a pure bare-name match would otherwise miss.
    """
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name.split(".")[0] == _SUBPROCESS:
                    aliases[(a.asname or a.name).split(".")[0]] = _SUBPROCESS
        elif isinstance(node, ast.ImportFrom):
            mod_top = (node.module or "").split(".")[0]
            for a in node.names:
                local = a.asname or a.name
                if mod_top == _SUBPROCESS:
                    aliases[local] = _SUBPROCESS
                elif a.name == _GIT_DETECT_FN:
                    aliases[local] = _GIT_DETECT_FN
    return aliases


def _scan_function(fn: ast.AST, aliases: dict[str, str]) -> tuple[set[str], bool, bool]:
    """Analyse one function body. Returns ``(calls, calls_git, uses_subprocess)``.

    ``calls`` is the set of callee bare names, with import aliases resolved to
    their canonical sink token (so an aliased ``read_git_branch`` edge is still
    labelled ``read_git_branch``). ``uses_subprocess`` is True when the body
    imports or names ``subprocess`` under any alias.
    """
    calls: set[str] = set()
    uses_subprocess = False
    for node in ast.walk(fn):
        if isinstance(node, ast.Call):
            name = _callee_bare_name(node)
            if name is not None:
                calls.add(aliases.get(name, name))
        elif isinstance(node, ast.Name):
            if aliases.get(node.id) == _SUBPROCESS:
                uses_subprocess = True
        elif isinstance(node, ast.Import):
            if any(a.name.split(".")[0] == _SUBPROCESS for a in node.names):
                uses_subprocess = True
        elif isinstance(node, ast.ImportFrom):
            if (node.module or "").split(".")[0] == _SUBPROCESS:
                uses_subprocess = True
    return calls, (_GIT_DETECT_FN in calls), uses_subprocess


class _CallGraph:
    """Alias-resolved, bare-name call graph over the ``pm_server`` package source.

    Attributes:
        edges: ``name -> set(canonical callee names reachable inside any def of
            that name)``. Bodies of equally-named defs are merged (the
            over-approximation).
        subprocess_fns: bare names of defs that reference ``subprocess`` (the
            shell-out sink owners), alias-resolved.
        git_detect_callers: bare names of defs that call ``read_git_branch``,
            alias-resolved.
        defined: every defined function/method bare name (vacuity guards).
    """

    def __init__(self) -> None:
        self.edges: dict[str, set[str]] = {}
        self.subprocess_fns: set[str] = set()
        self.git_detect_callers: set[str] = set()
        self.defined: set[str] = set()

    def ingest(self, source: str, filename: str = "<src>") -> None:
        """Fold one module's source into the graph (alias-resolved)."""
        tree = ast.parse(source, filename=filename)
        aliases = _sink_alias_map(tree)
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            name = fn.name
            self.defined.add(name)
            calls, calls_git, uses_subprocess = _scan_function(fn, aliases)
            self.edges.setdefault(name, set()).update(calls)
            if calls_git:
                self.git_detect_callers.add(name)
            if uses_subprocess:
                self.subprocess_fns.add(name)

    @classmethod
    def build(cls, pkg_dir: Path) -> _CallGraph:
        graph = cls()
        for py in sorted(pkg_dir.rglob("*.py")):
            graph.ingest(py.read_text(encoding="utf-8"), str(py))
        return graph

    @classmethod
    def from_source(cls, source: str) -> _CallGraph:
        """Build a graph from a single source string (for unit tests)."""
        graph = cls()
        graph.ingest(textwrap.dedent(source), "<test>")
        return graph

    def reachable(self, seed: set[str]) -> set[str]:
        """Forward call-closure (canonical names) from ``seed``."""
        seen = set(seed)
        stack = list(seed)
        while stack:
            for callee in self.edges.get(stack.pop(), ()):
                if callee not in seen:
                    seen.add(callee)
                    stack.append(callee)
        return seen


# Built once — pure static analysis of on-disk source, no import side effects.
_GRAPH = _CallGraph.build(_PKG_DIR)
_RO_SEED = set(srv.RO_ALLOWLIST)
_RO_CLOSURE = _GRAPH.reachable(_RO_SEED)


class TestGraphSanity:
    """Vacuity / teeth guards: prove the analysis is wired to reality so the
    disjointness assertions below cannot pass for the wrong reason."""

    def test_ro_allowlist_entries_are_real_functions(self):
        """Every RO_ALLOWLIST name is an actually-defined function — guards
        against a typo'd allowlist entry silently seeding an empty closure."""
        missing = _RO_SEED - _GRAPH.defined
        assert not missing, f"RO_ALLOWLIST names with no matching def: {sorted(missing)}"

    def test_detector_sees_the_write_path_caller(self):
        """The git-detection detector must flag the one legitimate caller
        (``pm_session_summary``). If it can see that call, it would equally see
        an illicit one wired onto the RO surface — i.e. the check has teeth."""
        assert _WRITE_PATH_FN in _GRAPH.git_detect_callers

    def test_detector_sees_installer_subprocess(self):
        """The subprocess detector must flag the known shell-out owners in
        ``installer.py`` — otherwise an empty sink set would make the
        disjointness check vacuous."""
        assert "install_claude_code" in _GRAPH.subprocess_fns
        assert _GRAPH.subprocess_fns, "no subprocess users detected at all"

    def test_call_graph_actually_connects(self):
        """The RO closure must reach real downstream helpers — proof the graph
        is populated, not a degenerate seed-only set."""
        assert "_resolve_track" in _RO_CLOSURE
        assert _RO_CLOSURE > _RO_SEED  # strictly larger than the seed


class TestReadOnlySurfaceDisjoint:
    """The core ADR-028 invariant, proven statically over the whole package."""

    def test_ro_surface_never_calls_git_detection(self):
        """No reachable RO function calls ``read_git_branch`` (alias-resolved),
        and the sink name never appears in the closure."""
        leaked = _RO_CLOSURE & _GRAPH.git_detect_callers
        assert not leaked, (
            f"read-only tool(s) reach a read_git_branch caller: {sorted(leaked)} "
            "— branch detection leaked onto the RO/Lens surface (ADR-028 violated)"
        )
        assert _GIT_DETECT_FN not in _RO_CLOSURE

    def test_ro_surface_never_reaches_write_path(self):
        """The branch-recording write path itself stays off the RO closure."""
        assert _WRITE_PATH_FN not in _RO_CLOSURE

    def test_ro_surface_disjoint_from_subprocess(self):
        """The RO closure shares no function with any ``subprocess`` user."""
        leaked = _RO_CLOSURE & _GRAPH.subprocess_fns
        assert not leaked, (
            f"read-only tools can reach subprocess-using function(s): {sorted(leaked)} "
            "— the git config-exec / shell-out risk the design forbids"
        )


class TestCheckerHasTeeth:
    """Mutation guards: prove the assertions FAIL when the invariant breaks, so
    a green run is meaningful rather than accidentally trivial."""

    def test_injected_git_detection_edge_is_caught(self):
        """Splice a regression — ``pm_recall`` calling ``read_git_branch`` —
        into a copy of the real graph and confirm the closure now flags it."""
        graph = _CallGraph.build(_PKG_DIR)
        graph.edges.setdefault("pm_recall", set()).add(_GIT_DETECT_FN)
        assert _GIT_DETECT_FN in graph.reachable(set(srv.RO_ALLOWLIST))

    def test_injected_subprocess_edge_is_caught(self):
        """Splice ``pm_status`` -> an installer shell-out function and confirm
        the disjointness check would catch it."""
        graph = _CallGraph.build(_PKG_DIR)
        graph.edges.setdefault("pm_status", set()).add("install_claude_code")
        closure = graph.reachable(set(srv.RO_ALLOWLIST))
        assert closure & graph.subprocess_fns


class TestAliasRobustness:
    """Aliased imports must not let a sink slip past detection (the gap a pure
    bare-name match would have). Each case is the renamed form of a real sink."""

    def test_aliased_git_detection_is_detected(self):
        graph = _CallGraph.from_source("""
            from .discovery import read_git_branch as rgb

            def pm_recall():
                return rgb(somewhere)
        """)
        assert "pm_recall" in graph.git_detect_callers
        assert _GIT_DETECT_FN in graph.edges["pm_recall"]

    def test_aliased_subprocess_module_is_detected(self):
        graph = _CallGraph.from_source("""
            import subprocess as sp

            def pm_status():
                return sp.run(["echo"])
        """)
        assert "pm_status" in graph.subprocess_fns

    def test_from_subprocess_import_alias_is_detected(self):
        graph = _CallGraph.from_source("""
            from subprocess import run as r

            def pm_next():
                return r(["echo"])
        """)
        assert "pm_next" in graph.subprocess_fns
