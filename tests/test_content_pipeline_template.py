"""Tests for the content-pipeline workflow template (PMSERV-117).

Validates the new builtin template loads, its tool_hints reference REAL tools
(no dead reference to the removed pm_approve_draft), and the human-review step
does not imply an engine-enforced gate (must-fix #5).
"""

from __future__ import annotations

from pathlib import Path

import yaml

import pm_server
import pm_server.server as srv

_TEMPLATE = Path(pm_server.__file__).parent / "templates" / "workflows" / "content-pipeline.yaml"


def _make_project(tmp_path: Path, name: str = "tplproj") -> Path:
    proj = tmp_path / name
    (proj / ".pm" / "daily").mkdir(parents=True)
    (proj / ".pm" / "project.yaml").write_text(
        f"name: {name}\ndisplay_name: {name}\nversion: 0.0.1\n"
        "status: development\nstarted: 2026-01-01\ndescription: tpl test\nphases: []\n",
        encoding="utf-8",
    )
    (proj / ".pm" / "tasks.yaml").write_text("[]\n", encoding="utf-8")
    return proj


def test_content_pipeline_template_listed(tmp_path: Path) -> None:
    # Inject a tmp project: pm_workflow_templates resolves a project root to
    # discover custom templates, so calling it with no project_path leans on
    # ambient cwd walk-up — which only "passes" inside this dogfooded repo and
    # raises ProjectNotFoundError under a clean CI checkout.
    proj = _make_project(tmp_path)
    res = srv.pm_workflow_templates(project_path=str(proj))
    names = {t["name"] for t in res["templates"]}
    assert "content-pipeline" in names


def test_content_pipeline_steps_and_hints_reference_real_tools() -> None:
    data = yaml.safe_load(_TEMPLATE.read_text(encoding="utf-8"))
    step_ids = [s["id"] for s in data["steps"]]
    assert step_ids == ["extract", "draft", "redact", "review"]
    for step in data["steps"]:
        hint = step.get("tool_hint")
        if hint:
            assert hint in srv.REGISTERED_TOOLS, f"tool_hint {hint!r} is not a registered tool"


def test_content_pipeline_no_dead_tool_and_no_enforced_gate() -> None:
    raw = _TEMPLATE.read_text(encoding="utf-8")
    # The removed approval tool must not be referenced anywhere (must-fix #5).
    assert "pm_approve_draft" not in raw
    data = yaml.safe_load(raw)
    review = next(s for s in data["steps"] if s["id"] == "review")
    # No 'gate' key on the human-review step — gates are advisory in the engine
    # and a gate here would re-imply the false-safety trap the design avoids.
    assert "gate" not in review


def test_content_pipeline_starts(tmp_path: Path) -> None:
    proj = _make_project(tmp_path)
    res = srv.pm_workflow_start(
        feature="x pipeline template smoke test",
        template="content-pipeline",
        project_path=str(proj),
    )
    assert res["status"] == "started"
    assert res["current_step"]["id"] == "extract"
    assert res["current_step"]["tool_hint"] == "pm_recall"
