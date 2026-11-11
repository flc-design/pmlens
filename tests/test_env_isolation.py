"""The suite cannot be aimed at a real project from the developer's shell.

pmlens picks its project and its mode from environment variables. With
``PM_PROJECT_PATH`` exported, every tool call that omits ``project_path`` used
to act on that project instead of the test's tmp_path, and the memory-cleanup
tests purged it. ``tests/conftest.py`` now clears those variables; this module
keeps that list complete and proves the clearing works with the variables
really exported.
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

import pmlens
import pmlens.server
from pmlens.utils import resolve_project_path
from tests.conftest import PMLENS_ENV_CLEARED, PMLENS_ENV_KEPT

_SRC = Path(pmlens.__file__).resolve().parent
_REPO_ROOT = Path(__file__).resolve().parent.parent


def _is_environ(node: ast.expr) -> bool:
    """``os.environ`` or a bare ``environ`` (``from os import environ``)."""
    if isinstance(node, ast.Attribute):
        return node.attr == "environ" and isinstance(node.value, ast.Name) and node.value.id == "os"
    return isinstance(node, ast.Name) and node.id == "environ"


def _is_getenv(node: ast.expr) -> bool:
    """``os.getenv`` or a bare ``getenv``."""
    if isinstance(node, ast.Attribute):
        return node.attr == "getenv" and isinstance(node.value, ast.Name) and node.value.id == "os"
    return isinstance(node, ast.Name) and node.id == "getenv"


def _env_reads(tree: ast.Module) -> list[tuple[int, ast.expr]]:
    """Every expression that names an environment variable being read."""
    found: list[tuple[int, ast.expr]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and node.args:
            func = node.func
            if _is_getenv(func):
                found.append((node.lineno, node.args[0]))
            elif (
                isinstance(func, ast.Attribute)
                and func.attr in {"get", "pop", "setdefault"}
                and _is_environ(func.value)
            ):
                found.append((node.lineno, node.args[0]))
        elif isinstance(node, ast.Subscript) and _is_environ(node.value):
            found.append((node.lineno, node.slice))
        elif isinstance(node, ast.Compare) and any(_is_environ(c) for c in node.comparators):
            found.append((node.lineno, node.left))
    return found


def _module_string_constants(tree: ast.Module) -> dict[str, str]:
    """Module-level ``NAME = "text"`` assignments (how storage.py names its variable)."""
    constants: dict[str, str] = {}
    for node in tree.body:
        targets: list[ast.expr] = []
        value: ast.expr | None = None
        if isinstance(node, ast.Assign):
            targets, value = list(node.targets), node.value
        elif isinstance(node, ast.AnnAssign):
            targets, value = [node.target], node.value
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            for target in targets:
                if isinstance(target, ast.Name):
                    constants[target.id] = value.value
    return constants


def _variables_pmlens_reads() -> tuple[set[str], list[str]]:
    """Names read from the environment under src/pmlens, plus reads no name resolves for."""
    names: set[str] = set()
    unresolved: list[str] = []
    for path in sorted(_SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        constants = _module_string_constants(tree)
        for lineno, arg in _env_reads(tree):
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                names.add(arg.value)
            elif isinstance(arg, ast.Name) and arg.id in constants:
                names.add(constants[arg.id])
            else:
                unresolved.append(f"{path.relative_to(_SRC)}:{lineno}")
    return names, unresolved


def test_every_variable_pmlens_reads_is_cleared_or_deliberately_kept() -> None:
    names, unresolved = _variables_pmlens_reads()
    assert not unresolved, (
        "these environment reads use a name this guard cannot resolve; pass a string "
        f"literal or a module-level constant: {unresolved}"
    )
    # The scan must really see the reads, or the assertion below is vacuous.
    assert {"PM_PROJECT_PATH", "PM_LENS", "PM_DESKTOP_WRITE"} <= names
    unlisted = sorted(names - set(PMLENS_ENV_CLEARED) - set(PMLENS_ENV_KEPT))
    assert not unlisted, (
        f"pmlens reads {unlisted} from the environment, but tests/conftest.py neither "
        "clears it (PMLENS_ENV_CLEARED) nor lists it as kept with a reason "
        "(PMLENS_ENV_KEPT). An exported value would steer the suite."
    )


def test_cleared_and_kept_lists_are_consistent() -> None:
    assert not set(PMLENS_ENV_CLEARED) & set(PMLENS_ENV_KEPT)
    assert all(reason.strip() for reason in PMLENS_ENV_KEPT.values())
    names, _ = _variables_pmlens_reads()
    stale = sorted(set(PMLENS_ENV_KEPT) - names)
    assert not stale, f"PMLENS_ENV_KEPT lists variables pmlens no longer reads: {stale}"


def test_selectors_are_absent_from_the_environment() -> None:
    present = [name for name in PMLENS_ENV_CLEARED if name in os.environ]
    assert not present, f"{present} reached a test; tests/conftest.py must clear them"


def test_mode_flags_are_not_frozen_from_the_shell() -> None:
    # server.py reads both at import time, so clearing them per test is too late.
    assert pmlens.server.PM_LENS_ENABLED is False
    assert pmlens.server.PM_DESKTOP_WRITE_ENABLED is False


def test_a_call_without_project_path_stays_in_the_tmp_project(
    tmp_project: Path, monkeypatch
) -> None:
    (tmp_project / ".pm" / "project.yaml").write_text("name: here\n", encoding="utf-8")
    monkeypatch.chdir(tmp_project)
    assert resolve_project_path(None) == tmp_project.resolve()


def test_exported_selectors_do_not_reach_the_suite(tmp_path: Path) -> None:
    """Run the three tests above with the variables exported, as a shell would.

    In CI none of the variables is set, so the tests above pass with or
    without the clearing. Only a run that starts with them exported shows that
    the clearing happens early enough (before pmlens is imported) and reaches
    the per-test environment. The nested run selects those tests by node id,
    so it cannot start another nested run.
    """
    decoy = tmp_path / "decoy"
    (decoy / ".pm").mkdir(parents=True)
    (decoy / ".pm" / "project.yaml").write_text("name: decoy\n", encoding="utf-8")
    env = {
        **os.environ,
        "PM_PROJECT_PATH": str(decoy),
        "PM_LENS": "1",
        "PM_DESKTOP_WRITE": "1",
        "PM_LOCK_TIMEOUT_S": "0.01",
        "PM_SERVER_RECALL_AMBIGUITY_WINDOW_MIN": "1",
    }
    this_file = Path(__file__).resolve()
    selected = [
        f"{this_file}::test_selectors_are_absent_from_the_environment",
        f"{this_file}::test_mode_flags_are_not_frozen_from_the_shell",
        f"{this_file}::test_a_call_without_project_path_stays_in_the_tmp_project",
    ]
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *selected],
        cwd=_REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert result.returncode == 0, result.stdout[-3000:] + result.stderr[-1000:]
    assert "3 passed" in result.stdout
    # Nothing was written into the project the shell pointed at.
    assert sorted(p.name for p in (decoy / ".pm").iterdir()) == ["project.yaml"]
