"""Workflow engine for managing development workflow state.

The engine manages workflow lifecycle: start → advance through steps → complete,
with abandon as a non-terminal escape hatch that preserves history.
Supports loops (brainstorming), gates (user approval), and chaining (discovery → development).

Architecture:
- pm-server (this engine): state machine — tracks which step is active
- Skills/Agents: execution — Claude invokes them based on step hints
- CLAUDE.md: orchestration — auto-behavior rules connecting the two
"""

from __future__ import annotations

import datetime as _dt
from pathlib import Path

from .models import (
    PmServerError,
    Workflow,
    WorkflowNotFoundError,
    WorkflowStatus,
    WorkflowStep,
    WorkflowStepStatus,
)
from .storage import (
    _yaml_transaction,
    add_workflow,
    load_knowledge,
    load_workflow_template,
    load_workflows,
    next_workflow_number,
    save_workflows,
)

# ─── Helpers ────────────────────────────────────────


def _generate_workflow_id(number: int) -> str:
    """Generate a workflow ID in WF-001 format."""
    return f"WF-{number:03d}"


def _step_guidance(step: WorkflowStep) -> dict:
    """Build guidance dict for a workflow step.

    Returns only non-empty fields to keep output concise for the LLM.
    """
    guidance: dict = {
        "id": step.id,
        "name": step.name,
        "status": step.status.value,
    }
    if step.description:
        guidance["description"] = step.description
    if step.tool_hint:
        guidance["tool_hint"] = step.tool_hint
    if step.skill_hint:
        guidance["skill_hint"] = step.skill_hint
    if step.agent_hint:
        guidance["agent_hint"] = step.agent_hint
    if step.gate:
        guidance["gate"] = step.gate
    if step.loop_group:
        guidance["loop"] = True
        guidance["loop_group"] = step.loop_group
        guidance["iteration"] = step.iteration
    if step.required_artifacts:
        guidance["required_artifacts"] = step.required_artifacts
    if step.produces:
        guidance["produces"] = step.produces
    if step.consumes:
        guidance["consumes"] = step.consumes
    if step.optional:
        guidance["optional"] = True
    if step.artifacts:
        guidance["artifacts"] = step.artifacts
    return guidance


def _progress(wf: Workflow) -> str:
    """Calculate progress string like '3/6'."""
    done = sum(
        1 for s in wf.steps if s.status in (WorkflowStepStatus.DONE, WorkflowStepStatus.SKIPPED)
    )
    return f"{done}/{len(wf.steps)}"


# ─── Core operations ────────────────────────────────


def start_workflow(
    pm_path: Path,
    feature: str,
    template_name: str = "development",
) -> dict:
    """Start a new workflow from a template.

    Creates a workflow instance, sets the first step to active,
    and returns guidance for the first step.
    """
    workflows = load_workflows(pm_path)
    active = [w for w in workflows if w.status == WorkflowStatus.ACTIVE]

    template = load_workflow_template(template_name, pm_path)

    number = next_workflow_number(pm_path)
    workflow_id = _generate_workflow_id(number)

    # Deep copy steps from template so the template isn't modified
    steps = [step.model_copy(deep=True) for step in template.steps]
    if steps:
        steps[0].status = WorkflowStepStatus.ACTIVE

    workflow = Workflow(
        id=workflow_id,
        name=template.name,
        feature=feature,
        template=template_name,
        steps=steps,
        current_step_index=0,
        status=WorkflowStatus.ACTIVE,
        chain_to=template.chain_to,
    )
    add_workflow(pm_path, workflow)

    result: dict = {
        "status": "started",
        "workflow_id": workflow_id,
        "feature": feature,
        "template": template_name,
        "total_steps": len(steps),
    }

    if active:
        result["warning"] = (
            f"There are {len(active)} active workflow(s): {', '.join(w.id for w in active)}"
        )

    if steps:
        result["current_step"] = _step_guidance(steps[0])

    if template.chain_to:
        result["chain_to"] = template.chain_to

    return result


def get_active_workflow(pm_path: Path) -> Workflow | None:
    """Get the most recently created active workflow."""
    workflows = load_workflows(pm_path)
    active = [w for w in workflows if w.status == WorkflowStatus.ACTIVE]
    return active[-1] if active else None


def get_workflow(pm_path: Path, workflow_id: str) -> Workflow:
    """Get a specific workflow by ID."""
    workflows = load_workflows(pm_path)
    for wf in workflows:
        if wf.id == workflow_id:
            return wf
    raise WorkflowNotFoundError(f"Workflow {workflow_id} not found")


def workflow_status(pm_path: Path, workflow_id: str | None = None) -> dict:
    """Get workflow status with step details and guidance.

    Auto-detects active workflow if workflow_id is not specified.
    """
    if workflow_id:
        wf = get_workflow(pm_path, workflow_id)
    else:
        wf = get_active_workflow(pm_path)
        if wf is None:
            return {
                "status": "no_active_workflow",
                "message": "No active workflow. Use pm_workflow_start to begin.",
            }

    result: dict = {
        "workflow_id": wf.id,
        "name": wf.name,
        "feature": wf.feature,
        "template": wf.template,
        "status": wf.status.value,
        "progress": _progress(wf),
        "steps": [_step_guidance(s) for s in wf.steps],
    }

    if wf.status == WorkflowStatus.ACTIVE and wf.current_step_index < len(wf.steps):
        result["current_step"] = _step_guidance(wf.steps[wf.current_step_index])

    if wf.chain_to:
        result["chain_to"] = wf.chain_to

    # Knowledge records linked to this workflow
    knowledge = load_knowledge(pm_path)
    linked = [k for k in knowledge if k.workflow_id == wf.id]
    if linked:
        by_cat: dict[str, int] = {}
        for k in linked:
            by_cat[k.category.value] = by_cat.get(k.category.value, 0) + 1
        result["knowledge"] = {
            "count": len(linked),
            "by_category": by_cat,
        }

    return result


def advance_step(
    pm_path: Path,
    workflow_id: str | None = None,
    proceed: bool = True,
    artifacts: list[str] | None = None,
    notes: str | None = None,
    skip: bool = False,
) -> dict:
    """Advance the current workflow step.

    Args:
        workflow_id: Specific workflow. Auto-detects if omitted.
        proceed: For loop steps — True exits the loop, False loops back.
        artifacts: Artifact IDs produced by this step (ADR, task, KR IDs).
        notes: Free-text notes for this step.
        skip: Skip the current step (marks as SKIPPED).

    Returns:
        Result dict with status, completed/next step, progress, chain info.
    """
    with _yaml_transaction(pm_path, "workflows.yaml"):
        workflows = load_workflows(pm_path)
        wf, wf_index = _resolve_workflow(workflows, workflow_id)

        # Validate state
        if wf.status != WorkflowStatus.ACTIVE:
            return {
                "status": "error",
                "message": f"Workflow {wf.id} is {wf.status.value}, not active",
            }

        if wf.current_step_index >= len(wf.steps):
            return {
                "status": "error",
                "message": f"Workflow {wf.id} has no more steps",
            }

        current = wf.steps[wf.current_step_index]

        if current.status != WorkflowStepStatus.ACTIVE:
            return {
                "status": "error",
                "message": f"Step '{current.id}' is {current.status.value}, not active",
            }

        # Record artifacts and notes on current step
        if artifacts:
            current.artifacts.extend(artifacts)
        if notes:
            current.notes = notes if not current.notes else f"{current.notes}\n{notes}"

        result: dict = {"workflow_id": wf.id}

        if skip:
            current.status = WorkflowStepStatus.SKIPPED
            result["status"] = "skipped"
            result["skipped_step"] = _step_guidance(current)
            _move_to_next(wf, result)

        elif current.loop_group and not proceed:
            # Loop back: reset all steps in loop_group, increment iteration
            _loop_back(wf, current.loop_group)
            next_step = wf.steps[wf.current_step_index]
            result["status"] = "looped"
            result["iteration"] = next_step.iteration
            result["loop_group"] = current.loop_group
            result["current_step"] = _step_guidance(next_step)
            result["progress"] = _progress(wf)

        else:
            # Normal advance: mark done, move to next
            current.status = WorkflowStepStatus.DONE
            result["status"] = "advanced"
            result["completed_step"] = _step_guidance(current)

            if current.required_artifacts and not current.artifacts:
                result["warning"] = (
                    f"Step '{current.id}' has required_artifacts "
                    f"{current.required_artifacts} but none were provided"
                )

            _move_to_next(wf, result)

        # Save
        wf.updated = _dt.date.today()
        workflows[wf_index] = wf
        save_workflows(pm_path, workflows)

    return result


def abandon_workflow(
    pm_path: Path,
    workflow_id: str | None = None,
    notes: str | None = None,
) -> dict:
    """Abandon an active or paused workflow.

    Transitions WorkflowStatus to ABANDONED while preserving step state
    (artifacts, notes, current_step_index). Subsequent advance_step calls
    will error since the workflow is no longer ACTIVE.

    Args:
        workflow_id: Specific workflow. Auto-detects the most recent ACTIVE
            workflow if omitted. To abandon a PAUSED workflow, pass its id.
        notes: Optional reason. Appended to the current step's notes if the
            workflow has a current step in range.

    Returns:
        Result dict. status="abandoned" on success, "already_abandoned" if
        the workflow was already ABANDONED (idempotent), or "error" if the
        workflow is COMPLETED.
    """
    with _yaml_transaction(pm_path, "workflows.yaml"):
        workflows = load_workflows(pm_path)
        wf, wf_index = _resolve_workflow(workflows, workflow_id)

        if wf.status == WorkflowStatus.ABANDONED:
            return {
                "status": "already_abandoned",
                "workflow_id": wf.id,
                "feature": wf.feature,
                "message": f"Workflow {wf.id} is already abandoned",
            }

        if wf.status == WorkflowStatus.COMPLETED:
            return {
                "status": "error",
                "workflow_id": wf.id,
                "message": f"Cannot abandon {wf.id}: workflow is completed",
            }

        previous_status = wf.status

        if notes and 0 <= wf.current_step_index < len(wf.steps):
            current = wf.steps[wf.current_step_index]
            current.notes = notes if not current.notes else f"{current.notes}\n{notes}"

        wf.status = WorkflowStatus.ABANDONED
        wf.updated = _dt.date.today()
        workflows[wf_index] = wf
        save_workflows(pm_path, workflows)

        result: dict = {
            "status": "abandoned",
            "workflow_id": wf.id,
            "feature": wf.feature,
            "template": wf.template,
            "previous_status": previous_status.value,
            "progress": _progress(wf),
        }

        if 0 <= wf.current_step_index < len(wf.steps):
            result["abandoned_at_step"] = _step_guidance(wf.steps[wf.current_step_index])

    return result


# ─── Internal state transitions ──────────────────────


def _resolve_workflow(workflows: list[Workflow], workflow_id: str | None) -> tuple[Workflow, int]:
    """Find workflow by ID or auto-detect the active one."""
    if workflow_id:
        for i, w in enumerate(workflows):
            if w.id == workflow_id:
                return w, i
        raise WorkflowNotFoundError(f"Workflow {workflow_id} not found")

    # Auto-detect: last active workflow
    for i in range(len(workflows) - 1, -1, -1):
        if workflows[i].status == WorkflowStatus.ACTIVE:
            return workflows[i], i

    raise PmServerError("No active workflow")


def _move_to_next(wf: Workflow, result: dict) -> None:
    """Move to the next step, or complete the workflow."""
    next_index = wf.current_step_index + 1
    result["progress"] = _progress(wf)

    if next_index < len(wf.steps):
        wf.current_step_index = next_index
        wf.steps[next_index].status = WorkflowStepStatus.ACTIVE
        result["current_step"] = _step_guidance(wf.steps[next_index])
    else:
        # All steps done
        wf.status = WorkflowStatus.COMPLETED
        wf.current_step_index = len(wf.steps)
        result["workflow_completed"] = True

        if wf.chain_to:
            result["chain_to"] = wf.chain_to
            result["message"] = f"Workflow completed. Start '{wf.chain_to}' workflow?"


def _loop_back(wf: Workflow, loop_group: str) -> None:
    """Reset all steps in the loop group and move to the first.

    Increments iteration counter on all group steps and sets the
    first step to ACTIVE. Artifacts and notes from previous iterations
    are preserved.
    """
    group_indices = [i for i, s in enumerate(wf.steps) if s.loop_group == loop_group]

    if not group_indices:
        raise PmServerError(f"Loop group '{loop_group}' not found")

    for idx in group_indices:
        wf.steps[idx].iteration += 1
        wf.steps[idx].status = WorkflowStepStatus.PENDING

    first_idx = group_indices[0]
    wf.steps[first_idx].status = WorkflowStepStatus.ACTIVE
    wf.current_step_index = first_idx
