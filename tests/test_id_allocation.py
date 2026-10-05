"""In-lock id allocation and duplicate-id guards (PMSERV-219).

The numbering race: ``next_*_number`` read the ledger without the lock and the
append re-read it under the lock without checking ids, so two concurrent calls
saved two records with one id. These tests pin the two halves of the fix:
``add_*_with_next_id`` calls the builder while the ledger lock is held, and
every append refuses an id that is already present.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from pathlib import Path

import pytest

from pmlens.models import (
    Decision,
    KnowledgeCategory,
    KnowledgeRecord,
    PmServerError,
    Task,
    Workflow,
)
from pmlens.storage import (
    _yaml_transaction,
    add_decision,
    add_decision_with_next_id,
    add_knowledge,
    add_knowledge_with_next_id,
    add_task,
    add_task_with_next_id,
    add_workflow,
    add_workflow_with_next_id,
    load_decisions,
    load_knowledge,
    load_tasks,
    load_workflows,
)


def _task(record_id: str) -> Task:
    return Task(id=record_id, title="t", phase="phase-1")


def _decision(record_id: str) -> Decision:
    return Decision(id=record_id, title="d")


def _knowledge(record_id: str) -> KnowledgeRecord:
    return KnowledgeRecord(id=record_id, category=KnowledgeCategory.SPEC, title="k")


def _workflow(record_id: str) -> Workflow:
    return Workflow(id=record_id, name="n", feature="f", template="development")


# (lock filename, plain add, add-with-next-id, loader, record factory, id format)
LEDGERS = [
    ("tasks.yaml", add_task, add_task_with_next_id, load_tasks, _task, "T-{:03d}"),
    (
        "decisions.yaml",
        add_decision,
        add_decision_with_next_id,
        load_decisions,
        _decision,
        "ADR-{:03d}",
    ),
    (
        "knowledge.yaml",
        add_knowledge,
        add_knowledge_with_next_id,
        load_knowledge,
        _knowledge,
        "KR-{:03d}",
    ),
    (
        "workflows.yaml",
        add_workflow,
        add_workflow_with_next_id,
        load_workflows,
        _workflow,
        "WF-{:03d}",
    ),
]
LEDGER_IDS = [entry[0] for entry in LEDGERS]


@pytest.mark.parametrize(("lock_name", "add", "_", "load", "make", "fmt"), LEDGERS, ids=LEDGER_IDS)
def test_append_rejects_an_existing_id(
    tmp_pm_path: Path,
    lock_name: str,
    add: Callable,
    _: Callable,
    load: Callable,
    make: Callable,
    fmt: str,
) -> None:
    add(tmp_pm_path, make(fmt.format(1)))

    with pytest.raises(PmServerError, match="already exists"):
        add(tmp_pm_path, make(fmt.format(1)))

    assert [r.id for r in load(tmp_pm_path)] == [fmt.format(1)]


@pytest.mark.parametrize(
    ("lock_name", "_", "add_next", "load", "make", "fmt"), LEDGERS, ids=LEDGER_IDS
)
def test_next_id_is_allocated_sequentially(
    tmp_pm_path: Path,
    lock_name: str,
    _: Callable,
    add_next: Callable,
    load: Callable,
    make: Callable,
    fmt: str,
) -> None:
    for _i in range(3):
        add_next(tmp_pm_path, lambda n: make(fmt.format(n)))

    assert [r.id for r in load(tmp_pm_path)] == [fmt.format(n) for n in (1, 2, 3)]


@pytest.mark.parametrize(
    ("lock_name", "_", "add_next", "load", "make", "fmt"), LEDGERS, ids=LEDGER_IDS
)
def test_builder_runs_while_the_ledger_lock_is_held(
    tmp_pm_path: Path,
    lock_name: str,
    _: Callable,
    add_next: Callable,
    load: Callable,
    make: Callable,
    fmt: str,
) -> None:
    """A second writer trying the same lock during ``build`` must be shut out.

    This is what makes the allocated number safe: nobody can append between
    the moment the number is computed and the moment the record is saved.
    """
    probe: dict[str, bool] = {}

    def try_lock() -> None:
        try:
            with _yaml_transaction(tmp_pm_path, lock_name, timeout=0.2):
                probe["acquired"] = True
        except PmServerError:
            probe["acquired"] = False

    def build(number: int):
        other = threading.Thread(target=try_lock)
        other.start()
        other.join()
        return make(fmt.format(number))

    add_next(tmp_pm_path, build)

    assert probe == {"acquired": False}
    assert [r.id for r in load(tmp_pm_path)] == [fmt.format(1)]


def test_a_failing_builder_writes_nothing(tmp_pm_path: Path) -> None:
    add_decision(tmp_pm_path, _decision("ADR-001"))
    before = (tmp_pm_path / "decisions.yaml").read_bytes()

    def build(number: int) -> Decision:
        raise ValueError("bad input")

    with pytest.raises(ValueError):
        add_decision_with_next_id(tmp_pm_path, build)

    assert (tmp_pm_path / "decisions.yaml").read_bytes() == before


# ─── The MCP tool paths (review follow-up: PP-2 / SEM-04) ─────────────────
# The storage tests above pin add_*_with_next_id itself; these pin that every
# id-allocating TOOL actually goes through it. Each id generator is wrapped so
# that, at the moment the id is produced, another thread tries the ledger
# lock: it must be shut out, i.e. the id is allocated inside the lock.


@pytest.fixture
def tool_project(tmp_path: Path, sample_project, sample_tasks) -> Path:
    from pmlens.storage import _save_project, _save_tasks, init_pm_directory

    pm_path = init_pm_directory(tmp_path)
    _save_project(pm_path, sample_project)
    _save_tasks(pm_path, sample_tasks)
    return tmp_path


def _lock_probe(pm_path: Path, lock_name: str, seen: list[bool]) -> Callable:
    def wrap(real: Callable) -> Callable:
        def probed(*args, **kwargs):
            def try_lock() -> None:
                try:
                    with _yaml_transaction(pm_path, lock_name, timeout=0.2):
                        seen.append(True)
                except PmServerError:
                    seen.append(False)

            other = threading.Thread(target=try_lock)
            other.start()
            other.join()
            return real(*args, **kwargs)

        return probed

    return wrap


def _call_add_task(root: Path) -> None:
    from pmlens.server import pm_add_task

    pm_add_task(title="t", phase="phase-1", project_path=str(root))


def _call_add_issue(root: Path) -> None:
    from pmlens.server import pm_add_issue

    pm_add_issue(parent_id="TEST-002", title="i", project_path=str(root))


def _call_add_decision(root: Path) -> None:
    from pmlens.server import pm_add_decision

    pm_add_decision(title="d", context="c", decision="x", project_path=str(root))


def _call_record(root: Path) -> None:
    from pmlens.server import pm_record

    pm_record(category="spec", title="k", project_path=str(root))


def _call_workflow_start(root: Path) -> None:
    from pmlens.server import pm_workflow_start

    pm_workflow_start(feature="f", project_path=str(root))


@pytest.mark.parametrize(
    ("call", "module", "attr", "lock_name"),
    [
        (_call_add_task, "pmlens.server", "generate_task_id", "tasks.yaml"),
        (_call_add_issue, "pmlens.server", "generate_task_id", "tasks.yaml"),
        (_call_add_decision, "pmlens.server", "generate_decision_id", "decisions.yaml"),
        (_call_record, "pmlens.server", "KnowledgeRecord", "knowledge.yaml"),
        (_call_workflow_start, "pmlens.workflow", "_generate_workflow_id", "workflows.yaml"),
    ],
    ids=["pm_add_task", "pm_add_issue", "pm_add_decision", "pm_record", "pm_workflow_start"],
)
def test_tool_allocates_its_id_inside_the_ledger_lock(
    tool_project: Path,
    monkeypatch: pytest.MonkeyPatch,
    call: Callable,
    module: str,
    attr: str,
    lock_name: str,
) -> None:
    import importlib

    mod = importlib.import_module(module)
    seen: list[bool] = []
    probe = _lock_probe(tool_project / ".pm", lock_name, seen)
    monkeypatch.setattr(mod, attr, probe(getattr(mod, attr)))

    call(tool_project)

    assert seen, f"{attr} was never called — the probe is not on the id path"
    assert not any(seen), f"{attr} ran while {lock_name} was NOT locked"


def test_pm_add_decision_writes_the_lineage_while_the_decisions_lock_is_held(
    tool_project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Decision Lineage (ADR-056 S1): the lineage is written inside the ledger lock.

    pm_add_decision creates ADR-NNN's lineage file in the same transaction as
    the ADR: at the moment the lineage is saved, decisions.yaml's lock (and the
    ADR's own lineage lock) must shut another thread out, or a concurrent
    writer could number or rewrite between the two files.
    """
    import pmlens.storage as storage

    pm_path = tool_project / ".pm"
    seen: dict[str, list[bool]] = {"decisions.yaml": [], "decision_lineage-ADR-001": []}
    real_save = storage._save_yaml

    def probed_save(path: Path, data, header_name: str) -> None:
        if Path(path).parent.name == "decision_lineage":
            for lock_name, results in seen.items():
                _lock_probe(pm_path, lock_name, results)(lambda: None)()
        real_save(path, data, header_name)

    monkeypatch.setattr(storage, "_save_yaml", probed_save)

    _call_add_decision(tool_project)

    assert (pm_path / "decision_lineage" / "ADR-001.yaml").exists()
    for lock_name, results in seen.items():
        assert results, f"the lineage was never saved through _save_yaml ({lock_name})"
        assert not any(results), f"the lineage was written while {lock_name} was NOT locked"
