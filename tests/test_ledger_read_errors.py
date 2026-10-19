"""Ledger read errors quote nothing from the file (PMSERV-266).

A pydantic ValidationError quotes the offending value (``input_value``) and a
YAML error quotes the offending line. FastMCP sends an exception's text to the
client and logs its traceback with every chained exception, and Lens read-only
tools reach the same loaders, so a secret in a broken record used to surface
in the response (fastmcp 3.x) and on stderr. These tests plant a secret in each
kind of broken record and check the exception text, its printed traceback, the
MCP response and FastMCP's log.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import traceback
from contextlib import closing
from pathlib import Path

import pytest
import yaml
from fastmcp import Client
from pydantic import ValidationError

import pmlens.server as srv
from pmlens.discovery import detect_project_info
from pmlens.ledger_errors import (
    LedgerReadError,
    build_record,
    build_records,
    fields_summary,
    safe_validation_message,
)
from pmlens.lineage import error_summary
from pmlens.memory import MemoryStore, _json_to_list
from pmlens.models import Decision, PmServerError, Project, Task
from pmlens.outbox import default_outbox_db_path
from pmlens.storage import (
    list_workflow_templates,
    load_daily_log,
    load_decisions,
    load_knowledge,
    load_milestones,
    load_project,
    load_registry,
    load_risks,
    load_tasks,
    load_workflow_template,
    load_workflows,
)

SECRET = "sk-proj-LEAKMARKER0123456789abcdef"


def _write(path: Path, doc: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(doc, allow_unicode=True, sort_keys=False), encoding="utf-8")


def _printed(exc: BaseException) -> str:
    """The exception as a traceback prints it, chained exceptions included."""
    return "".join(traceback.format_exception(exc))


def _assert_content_free(exc: BaseException, file: str) -> None:
    assert isinstance(exc, LedgerReadError)
    assert file in str(exc)
    assert SECRET not in str(exc)
    assert SECRET not in exc.summary
    assert SECRET not in _printed(exc)
    # Raised from None inside an except block, or outside one with no context.
    assert exc.__cause__ is None
    assert exc.__suppress_context__ or exc.__context__ is None


# ─── the message builder ─────────────────────────────


class TestBuildRecord:
    def test_a_wrong_value_is_named_by_record_field_and_type(self):
        with pytest.raises(LedgerReadError) as caught:
            build_record(
                Task, {"id": "PMSERV-012", "title": [SECRET], "phase": "p"}, "tasks.yaml", 3
            )
        assert str(caught.value) == (
            "tasks.yaml: ValidationError in record 3 (PMSERV-012): title (string_type)"
        )
        assert (
            caught.value.summary == "ValidationError in record 3 (PMSERV-012): title (string_type)"
        )
        _assert_content_free(caught.value, "tasks.yaml")

    def test_an_id_without_the_usual_shape_is_not_shown(self):
        with pytest.raises(LedgerReadError) as caught:
            build_record(Task, {"id": SECRET, "title": [1], "phase": "p"}, "tasks.yaml", 1)
        assert str(caught.value) == "tasks.yaml: ValidationError in record 1: title (string_type)"

    def test_nested_paths_use_field_names_and_positions(self):
        raw = {"name": "p", "phases": [{"id": "phase-1", "name": "n", "target_date": [SECRET]}]}
        with pytest.raises(LedgerReadError) as caught:
            build_record(Project, raw, "project.yaml")
        message = str(caught.value)
        assert message.startswith(
            "project.yaml: ValidationError in the document: phases[0].target_date ("
        )
        _assert_content_free(caught.value, "project.yaml")

    def test_a_union_member_tag_ends_the_path(self):
        # Decision.status is DecisionStatus | str; pydantic reports one error
        # per union member and adds the member's tag to loc, which is schema
        # text, not a field.
        with pytest.raises(LedgerReadError) as caught:
            build_record(Decision, {"id": "ADR-001", "title": "t", "status": [SECRET]}, "d.yaml", 1)
        assert str(caught.value) == (
            "d.yaml: ValidationError in record 1 (ADR-001): status (enum); status (string_type)"
        )

    def test_many_fields_are_cut_after_five(self):
        raw = {
            "id": "PMSERV-001",
            "title": [SECRET],
            "phase": [SECRET],
            "description": [SECRET],
            "notes": [SECRET],
            "tags": SECRET,
            "blocked_by": SECRET,
            "acceptance_criteria": SECRET,
        }
        with pytest.raises(LedgerReadError) as caught:
            build_record(Task, raw, "tasks.yaml", 1)
        assert "; and " in str(caught.value) and str(caught.value).endswith(" more")
        _assert_content_free(caught.value, "tasks.yaml")

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            (SECRET, "tasks.yaml: record 2 is not a mapping (str)"),
            ([SECRET], "tasks.yaml: record 2 is not a mapping (list)"),
            ({1: SECRET}, "tasks.yaml: record 2 has a key that is not text"),
        ],
    )
    def test_records_that_are_not_mappings_of_text(self, raw, expected):
        with pytest.raises(LedgerReadError) as caught:
            build_records(
                Task, [{"id": "T-1", "title": "t", "phase": "p"}, raw], "tasks.yaml", "tasks"
            )
        assert str(caught.value) == expected
        _assert_content_free(caught.value, "tasks.yaml")

    def test_a_list_that_is_not_a_list(self):
        with pytest.raises(LedgerReadError) as caught:
            build_records(Task, {SECRET: 1}, "tasks.yaml", "tasks")
        assert str(caught.value) == "tasks.yaml: 'tasks' is not a list (dict)"

    def test_fields_summary_reads_no_input(self):
        with pytest.raises(ValidationError) as caught:
            Task(id="T-1", title=[SECRET], phase="p")
        assert fields_summary(Task, caught.value) == "title (string_type)"
        assert SECRET not in safe_validation_message(caught.value)

    def test_error_summary_returns_the_summary(self):
        with pytest.raises(LedgerReadError) as caught:
            build_record(Task, {"id": "T-1", "title": [SECRET], "phase": "p"}, "tasks.yaml", 1)
        assert error_summary(caught.value) == caught.value.summary


# ─── every loader ────────────────────────────────────

_TASK = {"id": "PMSERV-001", "title": [SECRET], "phase": "phase-1"}

_LIST_LEDGERS = [
    ("tasks.yaml", "tasks", _TASK, load_tasks),
    ("decisions.yaml", "decisions", {"id": "ADR-001", "title": [SECRET]}, load_decisions),
    ("milestones.yaml", "milestones", {"id": "M-001", "name": [SECRET]}, load_milestones),
    ("risks.yaml", "risks", {"id": "R-001", "title": [SECRET]}, load_risks),
    (
        "knowledge.yaml",
        "knowledge",
        {"id": "KR-001", "category": "spec", "title": [SECRET]},
        load_knowledge,
    ),
    (
        "workflows.yaml",
        "workflows",
        {"id": "WF-001", "name": "n", "feature": [SECRET], "template": "development"},
        load_workflows,
    ),
]


@pytest.mark.parametrize(("file", "key", "record", "loader"), _LIST_LEDGERS)
def test_list_ledgers_quote_nothing(tmp_pm_path: Path, file, key, record, loader):
    _write(tmp_pm_path / file, {key: [record]})
    with pytest.raises(LedgerReadError) as caught:
        loader(tmp_pm_path)
    _assert_content_free(caught.value, file)
    assert "record 1" in str(caught.value)


def test_project_yaml_quotes_nothing(tmp_pm_path: Path):
    _write(tmp_pm_path / "project.yaml", {"name": [SECRET]})
    with pytest.raises(LedgerReadError) as caught:
        load_project(tmp_pm_path)
    _assert_content_free(caught.value, "project.yaml")
    assert "name (string_type)" in str(caught.value)


def test_registry_quotes_nothing(tmp_registry_dir: Path):
    _write(tmp_registry_dir / "registry.yaml", {"projects": [{"path": [SECRET], "name": "x"}]})
    with pytest.raises(LedgerReadError) as caught:
        load_registry(tmp_registry_dir)
    _assert_content_free(caught.value, "registry.yaml")
    assert "projects[0].path (string_type)" in str(caught.value)


def test_daily_log_quotes_nothing(tmp_pm_path: Path):
    import datetime as dt

    day = dt.date(2026, 10, 19)
    _write(
        tmp_pm_path / "daily" / "2026-10-19.yaml",
        {"date": "2026-10-19", "entries": [{"time": [SECRET]}]},
    )
    with pytest.raises(LedgerReadError) as caught:
        load_daily_log(tmp_pm_path, day)
    _assert_content_free(caught.value, "daily/2026-10-19.yaml")
    assert "entries[0].time (string_type)" in str(caught.value)


def test_custom_workflow_templates_quote_nothing(tmp_pm_path: Path):
    _write(
        tmp_pm_path / "workflow_templates" / "custom.yaml",
        {"name": "custom", "steps": [{"id": "s1", "name": [SECRET]}]},
    )
    for read in (
        lambda: load_workflow_template("custom", tmp_pm_path),
        lambda: list_workflow_templates(tmp_pm_path),
    ):
        with pytest.raises(LedgerReadError) as caught:
            read()
        _assert_content_free(caught.value, "workflow_templates/custom.yaml")
        assert "record 1" in str(caught.value) and "name (string_type)" in str(caught.value)


def test_a_yaml_error_keeps_its_position_and_drops_the_line(tmp_pm_path: Path):
    (tmp_pm_path / "tasks.yaml").write_text(
        f'tasks:\n- id: T-1\n  title: "{SECRET} never closed\n  phase: p\n', encoding="utf-8"
    )
    with pytest.raises(LedgerReadError) as caught:
        load_tasks(tmp_pm_path)
    _assert_content_free(caught.value, "tasks.yaml")
    assert str(caught.value).startswith("Failed to parse tasks.yaml: ScannerError at line ")
    assert error_summary(caught.value).startswith("ScannerError at line ")


def test_alias_expansion_refusal_has_a_summary(tmp_pm_path: Path):
    bomb = "a0: &a0 [x, x, x, x, x, x, x, x, x]\n" + "".join(
        f"a{i}: &a{i} [" + ", ".join([f"*a{i - 1}"] * 9) + "]\n" for i in range(1, 8)
    )
    (tmp_pm_path / "tasks.yaml").write_text(bomb + "tasks: []\n", encoding="utf-8")
    with pytest.raises(LedgerReadError) as caught:
        load_tasks(tmp_pm_path)
    assert error_summary(caught.value).startswith("grows past ")


# ─── stored memories and session summaries ───────────


def _memory_db(project: Path) -> Path:
    db = project / ".pm" / "memory.db"
    MemoryStore(db, global_db_path=project / "global" / "memory.db").close()
    return db


def test_a_stored_memory_with_an_unknown_type_quotes_nothing(tmp_project: Path):
    db = _memory_db(tmp_project)
    with closing(sqlite3.connect(db)) as conn, conn:
        conn.execute(
            "INSERT INTO memories (session_id, type, content, project) VALUES (?, ?, ?, ?)",
            ("s", SECRET, "c", "p"),
        )
    store = MemoryStore(db, global_db_path=tmp_project / "global" / "memory.db")
    try:
        with pytest.raises(LedgerReadError) as caught:
            store.get_recent(limit=5)
    finally:
        store.close()
    assert str(caught.value) == "memory.db: memory 1 has an unknown type"
    _assert_content_free(caught.value, "memory.db")


def test_json_lists_that_are_not_lists_read_as_empty():
    assert _json_to_list(json.dumps({"a": SECRET})) == []
    assert _json_to_list(json.dumps(SECRET)) == []
    assert _json_to_list(json.dumps(["x"])) == ["x"]


# ─── the MCP boundary: response and log ──────────────


class _LogCapture(logging.Handler):
    """Every record FastMCP or the MCP SDK logs, formatted with its traceback."""

    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.text = ""

    def emit(self, record: logging.LogRecord) -> None:
        self.text += logging.Formatter("%(levelname)s %(name)s %(message)s").format(record) + "\n"


@pytest.fixture
def captured_log():
    handler = _LogCapture()
    loggers = [logging.getLogger(name) for name in ("fastmcp", "mcp")]
    for logger in loggers:
        logger.addHandler(handler)
    yield handler
    for logger in loggers:
        logger.removeHandler(handler)


def _call(tool: str, arguments: dict) -> tuple[bool | None, str]:
    """Call a tool through FastMCP in process: (is_error, text), or (None, error)."""

    async def run() -> tuple[bool | None, str]:
        async with Client(srv.mcp) as client:
            try:
                result = await client.call_tool(tool, arguments, raise_on_error=False)
            except Exception as exc:  # a JSON-RPC error such as -32602
                return None, f"{type(exc).__name__}: {exc}"
            return result.is_error, " ".join(getattr(c, "text", "") for c in result.content)

    return asyncio.run(run())


_LENS_READS = [
    ("pm_tasks", {}),
    ("pm_next", {}),
    ("pm_status", {}),
    ("pm_dashboard", {"format": "text"}),
]


@pytest.mark.parametrize(("tool", "extra"), _LENS_READS)
def test_tools_report_a_broken_record_without_its_value(tmp_project, captured_log, tool, extra):
    _write(tmp_project / ".pm" / "project.yaml", {"name": "p"})
    _write(tmp_project / ".pm" / "tasks.yaml", {"tasks": [_TASK]})

    is_error, text = _call(tool, {"project_path": str(tmp_project), **extra})

    assert is_error is True, text  # an error result, not a JSON-RPC -32602
    assert "tasks.yaml: ValidationError in record 1 (PMSERV-001): title (string_type)" in text
    assert SECRET not in text
    assert SECRET not in captured_log.text


@pytest.mark.parametrize(("tool", "extra"), _LENS_READS)
def test_tools_report_broken_yaml_without_the_line(tmp_project, captured_log, tool, extra):
    _write(tmp_project / ".pm" / "project.yaml", {"name": "p"})
    (tmp_project / ".pm" / "tasks.yaml").write_text(
        f'tasks:\n- id: T-1\n  title: "{SECRET} never closed\n', encoding="utf-8"
    )

    is_error, text = _call(tool, {"project_path": str(tmp_project), **extra})

    assert is_error is True, text
    assert "Failed to parse tasks.yaml: ScannerError at line " in text
    assert SECRET not in text
    assert SECRET not in captured_log.text


def test_pm_recall_reports_a_bad_memory_without_its_value(tmp_project, captured_log):
    _write(tmp_project / ".pm" / "project.yaml", {"name": "p"})
    db = _memory_db(tmp_project)
    with closing(sqlite3.connect(db)) as conn, conn:
        conn.execute(
            "INSERT INTO memories (session_id, type, content, project) VALUES (?, ?, ?, ?)",
            ("s", SECRET, "c", "p"),
        )

    is_error, text = _call("pm_recall", {"project_path": str(tmp_project)})

    assert is_error is True, text
    assert "memory.db: memory 1 has an unknown type" in text
    assert SECRET not in text
    assert SECRET not in captured_log.text


def _raw_validation_error() -> ValidationError:
    try:
        Task(id="T-1", title=[SECRET], phase="p")
    except ValidationError as exc:
        return exc
    raise AssertionError("expected a ValidationError")


def test_the_safety_net_catches_a_raw_validation_error(tmp_project, captured_log, monkeypatch):
    # A path that still lets pydantic's error out: fastmcp 4 turned it into
    # -32602 "Invalid request parameters", fastmcp 3 sent input_value.
    def broken(_pm_path):
        raise _raw_validation_error()

    monkeypatch.setattr(srv, "load_tasks", broken)

    is_error, text = _call("pm_tasks", {"project_path": str(tmp_project)})

    assert is_error is True, text
    assert "Validation failed (1 error(s): string_type)" in text
    assert SECRET not in text
    assert SECRET not in captured_log.text


def test_the_safety_net_drops_a_chained_raw_error(tmp_project, captured_log, monkeypatch):
    def broken(_pm_path):
        try:
            yaml.safe_load(f'x: "{SECRET}')
        except yaml.YAMLError:
            raise PmServerError("tasks.yaml is broken")  # noqa: B904 - the implicit chain is the point

    monkeypatch.setattr(srv, "load_tasks", broken)

    is_error, text = _call("pm_tasks", {"project_path": str(tmp_project)})

    assert is_error is True, text
    assert "tasks.yaml is broken" in text
    assert SECRET not in text
    assert SECRET not in captured_log.text


def test_ordinary_errors_pass_through_unchanged(tmp_project):
    _write(tmp_project / ".pm" / "project.yaml", {"name": "p"})
    is_error, text = _call(
        "pm_update_task",
        {"task_id": "PMSERV-999", "status": "done", "project_path": str(tmp_project)},
    )
    assert is_error is True
    assert "PMSERV-999" in text


def test_direct_calls_still_raise_the_loader_error(tmp_project):
    _write(tmp_project / ".pm" / "project.yaml", {"name": "p"})
    _write(tmp_project / ".pm" / "tasks.yaml", {"tasks": [_TASK]})
    with pytest.raises(LedgerReadError):
        srv.pm_tasks(project_path=str(tmp_project))


def test_wrapping_keeps_every_tool_and_its_arguments():
    async def listed() -> dict:
        async with Client(srv.mcp) as client:
            # input_schema in MCP SDK 2 (fastmcp 4), inputSchema before.
            return {
                t.name: getattr(t, "input_schema", None) or t.inputSchema
                for t in await client.list_tools()
            }

    tools = asyncio.run(listed())
    assert set(tools) == srv.REGISTERED_TOOLS
    assert set(tools["pm_tasks"]["properties"]) >= {"project_path", "status", "phase"}


# ─── outbox entries and project detection ────────────


def test_pm_outbox_merge_reports_an_invalid_entry_without_its_value(tmp_path: Path):
    project = tmp_path / "proj"
    _write(project / ".pm" / "project.yaml", {"name": "proj"})
    (project / ".pm" / "daily").mkdir(parents=True)
    srv.pm_outbox_remember(
        content="ok", type="memory", source_project=str(project)
    )  # creates the db
    # Content that is not text (bytes that are not UTF-8) fails Memory validation.
    with closing(sqlite3.connect(default_outbox_db_path())) as conn, conn:
        outbox_id = conn.execute(
            "INSERT INTO desktop_outbox (host_id, source_session, type, content, source_project)"
            " VALUES ('claude-desktop', 's', 'memory', ?, ?)",
            (b"\xff" + SECRET.encode(), str(project)),
        ).lastrowid

    result = srv.pm_outbox_merge(ids=[outbox_id], target_project=str(project))

    assert result["merged"] == []
    (warning,) = result["warnings"]
    assert warning["reason"] == "outbox_entry_invalid"
    assert warning["detail"].startswith(
        f"outbox: ValidationError in outbox entry {outbox_id}: content ("
    )
    assert SECRET not in json.dumps(result)


def test_project_detection_keeps_only_text_values(tmp_path: Path):
    root = tmp_path / "my-app"
    root.mkdir()
    (root / "package.json").write_text(
        json.dumps({"name": [SECRET], "version": 1, "description": {"x": SECRET}}), encoding="utf-8"
    )
    info = detect_project_info(root)
    assert (info["name"], info["version"], info["description"]) == ("my-app", "0.1.0", "")

    result = srv.pm_init(project_path=str(root))
    assert result["status"] == "initialized"
    assert SECRET not in json.dumps(result)
