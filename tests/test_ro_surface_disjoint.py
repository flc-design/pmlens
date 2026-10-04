"""Architecture fitness: the read-only (Lens / ``RO_ALLOWLIST``) tool surface is
statically *disjoint* from git branch detection and ``subprocess`` shell-out.

ADR-028 / PMSERV-125. Branch-aware recall rests on a hard invariant: branch
*detection* (:func:`pmlens.discovery.read_git_branch`, which reads
``.git/HEAD``) happens only on the WRITE path (``pm_session_summary``), and the
read-only surface never shells out (``subprocess``). The suite already has two
*simple* guards for this:

* ``test_server_does_not_import_subprocess`` — ``server.py`` source contains no
  ``import subprocess`` (a single string match on one module).
* ``test_recall_with_track_never_invokes_git_detection`` — ``pm_recall``, run
  once, does not touch a monkeypatched ``read_git_branch`` (one dynamic path).

This module strengthens both into a **static reachability check**: it
builds a call graph over the *entire* ``pm_server`` package and asserts that the
forward closure from EVERY ``RO_ALLOWLIST`` tool is disjoint from (a) the
functions that call ``read_git_branch`` and (b) the functions that touch
``subprocess`` — across all branches/inputs of every reachable function, not
just the one path an example invocation happens to take.

Soundness, within stated limits. If any RO tool reaches a sink through ordinary
calls, the function that physically performs the sink call is itself reachable
(hence in the closure) AND is recorded as a sink caller — so the closure and
the sink-caller set intersect and the assertion fires. Two design choices keep
this sound for direct calls:

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

PMSERV-216 (ADR-056 S0) widens the proof in three ways:

* **More sinks.** Process execution through ``os`` (``os.system`` / ``os.popen``
  / ``os.exec*`` / ``os.spawn*`` / ``os.posix_spawn*``) and ``pty.spawn`` count
  as shell-out alongside ``subprocess``; and pmlens's own ledger-write helpers
  (``_save_yaml`` / ``_atomic_write_text`` / ``_yaml_transaction`` /
  ``_ensure_locks_dir``) are a write sink the RO surface must not reach.
* **More seeds, each with its own sinks.** ``OUTBOX_WRITE_ALLOWLIST`` registers
  under PM_LENS=1 + PM_DESKTOP_WRITE=1. It may write the Desktop outbox
  (SQLite), so the YAML-ledger sink does not apply to it, but shell-out and git
  detection still do.
* **The full-mode surface.** PM Lens never shells out to git on any path
  (ADR-028, README), so the closure of EVERY ``@_tool()`` function — read and
  write alike — must stay clear of shell-out.

Limits — what this check does NOT see (so a green run is not a proof):

* **Indirect calls.** A function passed as a value and called elsewhere
  (callbacks, ``executor.submit(fn)``, ``getattr(mod, name)()``,
  ``importlib`` / ``__import__``) has no edge. Nested defs are folded into
  their enclosing function, which covers the ``build`` callbacks handed to
  ``storage.add_*_with_next_id`` but not callbacks defined elsewhere.
* **Sinks outside the list.** Only ``subprocess``, the ``os`` / ``pty`` /
  ``asyncio`` process starters and the four ledger-write helpers count. Plain
  ``Path.write_text`` / ``open(..., "w")`` writes are not sinks here: they are
  too common to separate statically from legitimate reads of other kinds.
* **Runtime gates.** ``pm_status`` reaches ``install_hooks`` (which writes
  ``~/.claude/settings.json``) — that edge IS in the RO closure, but the call
  only runs when ``PM_LENS`` is off (PMSERV-144), which a static graph cannot
  tell. Writes like this are covered by the dynamic HOME-snapshot sweep in
  ``test_lens_invariant.py`` instead.
"""

from __future__ import annotations

import ast
import textwrap
from pathlib import Path

import pmlens
import pmlens.server as srv

_PKG_DIR = Path(pmlens.__file__).parent

# Sinks the read-only surface must never reach.
_GIT_DETECT_FN = "read_git_branch"  # reads .git/HEAD — write-path only
_WRITE_PATH_FN = "pm_session_summary"  # the sole legitimate read_git_branch caller
_SUBPROCESS = "subprocess"
# Process execution outside the subprocess module (PMSERV-216).
_OS_EXEC = "os-exec"
_OS_EXEC_ATTRS = frozenset(
    {
        "system",
        "popen",
        "execl",
        "execle",
        "execlp",
        "execlpe",
        "execv",
        "execve",
        "execvp",
        "execvpe",
        "spawnl",
        "spawnle",
        "spawnlp",
        "spawnlpe",
        "spawnv",
        "spawnve",
        "spawnvp",
        "spawnvpe",
        "posix_spawn",
        "posix_spawnp",
        "startfile",
    }
)
_ASYNCIO_EXEC_ATTRS = frozenset({"create_subprocess_exec", "create_subprocess_shell"})
# pmlens's own ledger-write helpers (storage / utils). Reaching one of these
# means writing (or locking) a .pm/ or ~/.pm YAML ledger.
_LEDGER_WRITE_HELPERS = frozenset(
    {"_save_yaml", "_atomic_write_text", "_yaml_transaction", "_ensure_locks_dir"}
)


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
                top = a.name.split(".")[0]
                local = (a.asname or a.name).split(".")[0]
                if top == _SUBPROCESS:
                    aliases[local] = _SUBPROCESS
                elif top in ("os", "pty", "asyncio"):
                    aliases[local] = f"module:{top}"
        elif isinstance(node, ast.ImportFrom):
            mod_top = (node.module or "").split(".")[0]
            for a in node.names:
                local = a.asname or a.name
                if mod_top == _SUBPROCESS:
                    aliases[local] = _SUBPROCESS
                elif mod_top == "os" and a.name in _OS_EXEC_ATTRS:
                    aliases[local] = _OS_EXEC
                elif mod_top == "pty" and a.name == "spawn":
                    aliases[local] = _OS_EXEC
                elif mod_top == "asyncio" and a.name in _ASYNCIO_EXEC_ATTRS:
                    aliases[local] = _OS_EXEC
                elif a.name == _GIT_DETECT_FN:
                    aliases[local] = _GIT_DETECT_FN
    return aliases


def _is_exec_call(call: ast.Call, aliases: dict[str, str]) -> bool:
    """True for ``os.system(...)``-style calls and their aliased forms."""
    func = call.func
    if isinstance(func, ast.Name):
        return aliases.get(func.id) == _OS_EXEC
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
        module = aliases.get(func.value.id)
        if module == "module:os":
            return func.attr in _OS_EXEC_ATTRS
        if module == "module:pty":
            return func.attr == "spawn"
        if module == "module:asyncio":
            return func.attr in _ASYNCIO_EXEC_ATTRS
    return False


def _scan_function(fn: ast.AST, aliases: dict[str, str]) -> tuple[set[str], bool, bool, bool]:
    """Analyse one function body.

    Returns ``(calls, calls_git, uses_subprocess, uses_exec)``. ``calls`` is the
    set of callee bare names, with import aliases resolved to their canonical
    sink token (so an aliased ``read_git_branch`` edge is still labelled
    ``read_git_branch``). ``uses_subprocess`` is True when the body imports or
    names ``subprocess`` under any alias; ``uses_exec`` when it calls an ``os``
    / ``pty`` process-execution function under any alias.
    """
    calls: set[str] = set()
    uses_subprocess = False
    uses_exec = False
    for node in ast.walk(fn):
        if isinstance(node, ast.Call):
            name = _callee_bare_name(node)
            if name is not None:
                calls.add(aliases.get(name, name))
            if _is_exec_call(node, aliases):
                uses_exec = True
        elif isinstance(node, ast.Name):
            if aliases.get(node.id) == _SUBPROCESS:
                uses_subprocess = True
        elif isinstance(node, ast.Import):
            if any(a.name.split(".")[0] == _SUBPROCESS for a in node.names):
                uses_subprocess = True
        elif isinstance(node, ast.ImportFrom):
            if (node.module or "").split(".")[0] == _SUBPROCESS:
                uses_subprocess = True
    return calls, (_GIT_DETECT_FN in calls), uses_subprocess, uses_exec


class _CallGraph:
    """Alias-resolved, bare-name call graph over the ``pm_server`` package source.

    Attributes:
        edges: ``name -> set(canonical callee names reachable inside any def of
            that name)``. Bodies of equally-named defs are merged (the
            over-approximation).
        subprocess_fns: bare names of defs that reference ``subprocess`` (the
            shell-out sink owners), alias-resolved.
        exec_fns: bare names of defs that call an ``os`` / ``pty``
            process-execution function, alias-resolved (PMSERV-216).
        git_detect_callers: bare names of defs that call ``read_git_branch``,
            alias-resolved.
        ledger_writers: the ledger-write helpers plus every def that calls one.
        defined: every defined function/method bare name (vacuity guards).
    """

    def __init__(self) -> None:
        self.edges: dict[str, set[str]] = {}
        self.subprocess_fns: set[str] = set()
        self.exec_fns: set[str] = set()
        self.git_detect_callers: set[str] = set()
        self.ledger_writers: set[str] = set()
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
            calls, calls_git, uses_subprocess, uses_exec = _scan_function(fn, aliases)
            self.edges.setdefault(name, set()).update(calls)
            if calls_git:
                self.git_detect_callers.add(name)
            if uses_subprocess:
                self.subprocess_fns.add(name)
            if uses_exec:
                self.exec_fns.add(name)
            if name in _LEDGER_WRITE_HELPERS or calls & _LEDGER_WRITE_HELPERS:
                self.ledger_writers.add(name)

    @property
    def shell_out_fns(self) -> set[str]:
        """Every def that can start a process (subprocess or os/pty exec)."""
        return self.subprocess_fns | self.exec_fns

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
# PMSERV-145 (ADR-039 T2): OUTBOX_READ_ALLOWLIST (pm_outbox_pending) is also
# a read-only Lens surface (registers under PM_LENS=1 independent of
# PM_DESKTOP_WRITE) — fold it into the seed so the same static-reachability
# proof covers it.
_RO_SEED = set(srv.RO_ALLOWLIST) | set(srv.OUTBOX_READ_ALLOWLIST)
_RO_CLOSURE = _GRAPH.reachable(_RO_SEED)
# PMSERV-216: Desktop outbox writers register under PM_LENS=1 too.
_OUTBOX_WRITE_SEED = set(srv.OUTBOX_WRITE_ALLOWLIST)
_OUTBOX_WRITE_CLOSURE = _GRAPH.reachable(_OUTBOX_WRITE_SEED)


def _decorated_tools(source: str) -> set[str]:
    """Names of every ``@_tool()`` function in server.py, read from source.

    Taken from the source rather than ``REGISTERED_TOOLS`` so the full-mode
    check does not depend on the PM_LENS value the test process imported with.
    """
    names: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for deco in node.decorator_list:
            if isinstance(deco, ast.Call) and isinstance(deco.func, ast.Name):
                if deco.func.id == "_tool":
                    names.add(node.name)
    return names


_ALL_TOOLS = _decorated_tools((_PKG_DIR / "server.py").read_text(encoding="utf-8"))
_FULL_CLOSURE = _GRAPH.reachable(_ALL_TOOLS)


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


class TestWidenedSurfaces:
    """PMSERV-216: ledger-write and os/pty-exec sinks, the outbox-write seed and
    the full-mode surface (see the module docstring)."""

    def test_new_detectors_are_populated(self):
        """Vacuity guards for the new sinks and seeds."""
        assert {"_save_yaml", "add_task", "add_decision_with_next_id"} <= _GRAPH.ledger_writers
        assert _OUTBOX_WRITE_SEED <= _GRAPH.defined
        assert _ALL_TOOLS > _RO_SEED | _OUTBOX_WRITE_SEED
        assert {"pm_add_task", "pm_add_decision", "pm_update_rules"} <= _ALL_TOOLS

    def test_ro_surface_never_writes_a_ledger(self):
        leaked = _RO_CLOSURE & _GRAPH.ledger_writers
        assert not leaked, (
            f"read-only tool(s) reach a ledger write/lock helper: {sorted(leaked)} "
            "— a Lens read must not write .pm/ or ~/.pm"
        )

    def test_ro_surface_never_execs(self):
        leaked = _RO_CLOSURE & _GRAPH.shell_out_fns
        assert not leaked, f"read-only tool(s) can start a process: {sorted(leaked)}"

    def test_outbox_writers_stay_off_ledgers_git_and_shell_out(self):
        """The outbox writers may touch desktop.db only."""
        for label, sinks in (
            ("ledger write", _GRAPH.ledger_writers),
            ("shell-out", _GRAPH.shell_out_fns),
            ("git detection", _GRAPH.git_detect_callers),
        ):
            leaked = _OUTBOX_WRITE_CLOSURE & sinks
            assert not leaked, f"outbox writer reaches a {label} sink: {sorted(leaked)}"

    def test_full_mode_never_shells_out(self):
        """No tool on ANY path starts a process — PM Lens never runs git (ADR-028)."""
        leaked = _FULL_CLOSURE & _GRAPH.shell_out_fns
        assert not leaked, (
            f"MCP tool(s) can start a process: {sorted(leaked)} — the README promises "
            "PM Lens never shells out to git, on read and write paths alike"
        )


class TestWidenedCheckerHasTeeth:
    """Mutation guards for the PMSERV-216 checks."""

    def test_mutator_added_to_the_ro_allowlist_is_caught(self):
        """Putting a write tool on the Lens surface must trip the ledger sink."""
        closure = _GRAPH.reachable(_RO_SEED | {"pm_add_task"})
        assert closure & _GRAPH.ledger_writers

    def test_write_tool_reaching_shell_out_is_caught(self):
        graph = _CallGraph.build(_PKG_DIR)
        graph.edges.setdefault("pm_add_task", set()).add("install_claude_code")
        assert graph.reachable(_ALL_TOOLS) & graph.shell_out_fns

    def test_os_exec_forms_are_detected(self):
        cases = {
            "import os\n\ndef pm_status():\n    os.system('x')\n": "pm_status",
            "import os as o\n\ndef pm_next():\n    o.popen('x')\n": "pm_next",
            "from os import execvp as ex\n\ndef pm_tasks():\n    ex('x', [])\n": "pm_tasks",
            "import pty\n\ndef pm_risks():\n    pty.spawn('x')\n": "pm_risks",
            (
                "import asyncio\n\nasync def pm_list():\n"
                "    await asyncio.create_subprocess_exec('x')\n"
            ): "pm_list",
        }
        for source, fn in cases.items():
            assert fn in _CallGraph.from_source(source).exec_fns, source

    def test_unrelated_os_calls_are_not_exec(self):
        graph = _CallGraph.from_source(
            "import os\n\ndef pm_status():\n    return os.path.join('a', 'b'), os.getcwd()\n"
        )
        assert "pm_status" not in graph.exec_fns

    def test_os_exec_wired_onto_the_ro_surface_is_caught(self):
        graph = _CallGraph.build(_PKG_DIR)
        graph.ingest("import os\n\ndef _evil_helper():\n    os.system('git status')\n")
        graph.edges.setdefault("pm_status", set()).add("_evil_helper")
        assert graph.reachable(_RO_SEED) & graph.shell_out_fns
