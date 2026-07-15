"""Prompt Pack generation — backlog tasks → self-contained session prompts.

PMSERV-154 (v1). Turns pm-server backlog tasks into ready-to-paste
implementation-session prompts, so the output of a review/planning session
becomes the input of an implementation session (the "1 task = 1 session" flow
described in docs/proposals/pmlens-prompt-pack-proposal.md).

This module is **pure and read-only**: it only reads already-loaded
tasks/memory/decisions/project and returns a markdown string. The single write
(the export file under ``.pm/exports/``) and all git-avoidance live in the
caller (``server.pm_prompt_pack``), which keeps this module trivially testable
and re-usable, and keeps the read/write boundary explicit (RO invariant, the
same principle as ADR-028 and PMSERV-144).

v2 scope (PMSERV-155 / ADR-041): adds a self-contained HTML format (progress
diagram grouped into lanes + per-card copy buttons, no CDN — CSP-safe), the
``suggested_model`` / ``after_recommended`` / ``track`` task fields and the
``discipline`` / ``verify_commands`` project fields, and a two-layer template
override (``.pm/prompt-templates/`` wins over the built-in). The paste-ready
prompt body is generated once (``build_prompt_body``) and shared by both the
markdown fence and the HTML ``<pre>`` so the two formats never drift.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml
from jinja2 import ChoiceLoader, Environment, FileSystemLoader, select_autoescape

from .models import Decision, Memory, MemoryType, Project, SuggestedModel, Task

# Built-in template dir (shared with dashboard). The two-layer override adds the
# project's ``.pm/prompt-templates/`` in front of this at render time.
_TEMPLATES_DIR = Path(__file__).parent / "templates"
_PROMPT_PACK_TEMPLATE = "prompt_pack.html"

# ADR references written into a task description (e.g. "ADR-039 の不変条件").
# Decision has no task foreign key, so this text scan — plus linked memories'
# ``decision_id`` — is the cheap v1 way to surface "linked ADRs".
_ADR_REF_RE = re.compile(r"ADR-\d+")

# Memory types worth surfacing as 注意 (caution) in a prompt card. Lessons and
# insights carry the "past accident / design judgement" signal the proposal
# wants; routine observations would be noise in a session prompt.
_CAUTION_MEMORY_TYPES = frozenset({MemoryType.LESSON, MemoryType.INSIGHT})

# Cap a caution line so a long memory body does not swamp the card.
_CAUTION_MAX_CHARS = 240


def read_verify_commands(pm_path: Path) -> list[str]:
    """Read optional ``verify_commands`` from ``project.yaml`` (tolerant).

    v1 adds no model field (the proposal's ``verify_commands`` is a v2 data-model
    extension), so this reads the raw YAML and returns the list only when it is
    present and well-formed — the "use if present" contract from proposal §5.
    Returns ``[]`` when the key is absent, malformed, or the file is unreadable,
    so a project without the field still generates a valid pack.
    """
    project_yaml = pm_path / "project.yaml"
    if not project_yaml.exists():
        return []
    try:
        data = yaml.safe_load(project_yaml.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return []
    # A project.yaml whose root is valid YAML but not a mapping (a list, bare
    # scalar, or int) safe_loads to a non-dict; guard before .get() so the
    # documented "returns [] when malformed" tolerance actually holds — the
    # same isinstance guard load_tracks uses (would otherwise AttributeError).
    if not isinstance(data, dict):
        return []
    raw = data.get("verify_commands")
    if isinstance(raw, str):
        return [raw]
    if isinstance(raw, list):
        return [str(c) for c in raw if str(c).strip()]
    return []


def adr_refs_for_task(task: Task, memories: list[Memory]) -> list[str]:
    """ADR ids linked to a task, de-duplicated with stable order.

    Two cheap v1 sources (Decision has no task FK): the ``ADR-\\d+`` references
    in the task description (first, in text order) and the ``decision_id`` of
    any memory linked to the task.
    """
    refs: list[str] = []
    seen: set[str] = set()
    for ref in _ADR_REF_RE.findall(task.description):
        if ref not in seen:
            seen.add(ref)
            refs.append(ref)
    for mem in memories:
        did = mem.decision_id
        if did and did not in seen:
            seen.add(did)
            refs.append(did)
    return refs


def _condense(text: str, limit: int = _CAUTION_MAX_CHARS) -> str:
    """Collapse whitespace and truncate for a one-line card entry."""
    flattened = " ".join(text.split())
    if len(flattened) <= limit:
        return flattened
    return flattened[: limit - 1].rstrip() + "…"


def _fence(body: str) -> str:
    """Return a backtick fence longer than any run of backticks in ``body``.

    Keeps the paste-ready block intact even if a task description itself
    contains fenced code (```): the outer fence is always one backtick longer
    than the longest inner run, minimum three.
    """
    longest = max((len(m) for m in re.findall(r"`+", body)), default=0)
    return "`" * max(3, longest + 1)


def build_prompt_body(
    task: Task,
    *,
    memories: list[Memory],
    decisions_by_id: dict[str, Decision],
    verify_commands: list[str],
) -> str:
    """The paste-ready prompt text for one task (準備/内容/注意/完了条件).

    Shared verbatim by the markdown fence and the HTML ``<pre>`` so the two
    output formats can never drift (ADR-041). Contains no title, chrome, or
    fence — just the body a caller pastes into a fresh implementation session.
    """
    adr_refs = adr_refs_for_task(task, memories)
    cautions = [m for m in memories if m.type in _CAUTION_MEMORY_TYPES]

    body: list[str] = []
    body.append(f"{task.id} を実装してください（着手前に pm_update_task で in_progress に）。")
    body.append("")
    body.append("## 準備")
    body.append(f"- pm タスク {task.id} の description / acceptance_criteria を読む")
    if task.blocked_by:
        body.append(f"- 依存（先に完了が必要）: {', '.join(task.blocked_by)}")
    if task.after_recommended:
        body.append(f"- 推奨順序（この後に実施）: {', '.join(task.after_recommended)}")
    for ref in adr_refs:
        dec = decisions_by_id.get(ref)
        body.append(f"- 関連 ADR を確認: {ref}" + (f" — {dec.title}" if dec else ""))
    body.append("")
    body.append("## 内容")
    body.append(task.description.strip() or "（description 未記載 — pm タスクを直接確認）")
    body.append("")
    if cautions:
        body.append("## 注意（過去の教訓・設計判断）")
        for m in cautions:
            body.append(f"- [{m.type.value}] {_condense(m.content)}")
        body.append("")
    body.append("## 完了条件")
    for ac in task.acceptance_criteria:
        body.append(f"- {ac}")
    for cmd in verify_commands:
        body.append(f"- 検証コマンド: `{cmd}`")
    body.append("- 動作確認 → pm_update_task done → pm_log → アトミックコミット")

    return "\n".join(body)


def render_task_card(
    task: Task,
    *,
    memories: list[Memory],
    decisions_by_id: dict[str, Decision],
    verify_commands: list[str],
) -> str:
    """Render one task as a markdown prompt card with a paste-ready block."""
    body_text = build_prompt_body(
        task,
        memories=memories,
        decisions_by_id=decisions_by_id,
        verify_commands=verify_commands,
    )
    fence = _fence(body_text)

    priority = task.priority.value if hasattr(task.priority, "value") else str(task.priority)
    lines = [
        f"### {task.id} — {task.title}",
        "",
        f"*phase: {task.phase} / priority: {priority} / status: {task.status.value}*",
        "",
        f"{fence}text",
        body_text,
        fence,
        "",
    ]
    return "\n".join(lines)


def build_prompt_pack_md(
    tasks: list[Task],
    *,
    project: Project,
    memories_by_task: dict[str, list[Memory]],
    decisions_by_id: dict[str, Decision],
    verify_commands: list[str],
    filter_label: str,
    discipline: str = "",
) -> str:
    """Build the full markdown prompt pack for ``tasks``.

    ``memories_by_task`` maps task id → its linked memories (caller fetches
    them so this stays pure). ``decisions_by_id`` maps ADR id → Decision for
    titling linked ADRs. ``filter_label`` is a human description of the
    selection shown in the header. ``discipline`` is the project-wide discipline
    text appended to the common rules (PMSERV-155).
    """
    project_name = project.display_name or project.name
    header = [
        f"# 実装セッション プロンプトパック — {project_name}",
        "",
        f"- 対象: {filter_label}",
        f"- タスク数: {len(tasks)}",
        "- 使い方: ```text ブロックを新規セッションに貼り付ける（1タスク=1セッション）。",
        "",
        "## 共通運用ルール",
        "",
        "- 着手前: 該当タスクを pm_update_task で in_progress にする",
        "- 作業中に重要な発見・判断があれば pm_remember で記録（task_id で紐付け）",
        "- 完了時: 動作確認 → pm_update_task done → pm_log → アトミックコミット",
        "- 課題が見つかったら pm_add_issue（defect / enhancement を選ぶ）",
        "",
    ]
    if discipline.strip():
        header.append("### プロジェクト規律")
        header.append("")
        header.append(discipline.strip())
        header.append("")
    header.append("---")
    header.append("")
    cards = [
        render_task_card(
            task,
            memories=memories_by_task.get(task.id, []),
            decisions_by_id=decisions_by_id,
            verify_commands=verify_commands,
        )
        for task in tasks
    ]
    return "\n".join(header) + "\n".join(cards)


# ─── HTML output (v2, PMSERV-155 / ADR-041) ──────────────────────────────────

_VALID_GROUP_BY = frozenset({"none", "phase", "track"})


def _group_label(task: Task, group_by: str) -> str:
    """Lane label for one task under the given grouping (never raises)."""
    if group_by == "phase":
        return task.phase or "(no phase)"
    if group_by == "track":
        return task.track or "(no track)"
    return ""


def group_tasks(tasks: list[Task], group_by: str) -> list[tuple[str, list[Task]]]:
    """Group tasks into ordered lanes preserving task order within each lane.

    ``group_by`` is one of ``none`` (a single unlabeled lane), ``phase`` or
    ``track``. An unknown value degrades to ``none`` (the caller validates and
    errors first; this is defense-in-depth). Lane order follows first
    appearance so the diagram is stable and deterministic.
    """
    if group_by not in _VALID_GROUP_BY or group_by == "none":
        return [("", list(tasks))]
    order: list[str] = []
    buckets: dict[str, list[Task]] = {}
    for t in tasks:
        label = _group_label(t, group_by)
        if label not in buckets:
            buckets[label] = []
            order.append(label)
        buckets[label].append(t)
    return [(label, buckets[label]) for label in order]


def task_node(task: Task) -> dict:
    """Diagram-node view of a task (plain data for the template; autoescaped)."""
    model = (
        task.suggested_model.value
        if isinstance(task.suggested_model, SuggestedModel)
        else str(task.suggested_model)
    )
    return {
        "id": task.id,
        "title": task.title,
        "phase": task.phase,
        "priority": task.priority.value if hasattr(task.priority, "value") else str(task.priority),
        "status": task.status.value if hasattr(task.status, "value") else str(task.status),
        # None (not "any") so the template can simply test truthiness for a chip.
        "suggested_model": None if model == SuggestedModel.ANY.value else model,
        "blocked_by": list(task.blocked_by),
        "after_recommended": list(task.after_recommended),
    }


def _prompt_pack_env(pm_path: Path | None) -> Environment:
    """Jinja2 env with the two-layer loader: project override then built-in.

    ``.pm/prompt-templates/`` (when present) takes precedence over the bundled
    template, mirroring the workflow-templates override convention (ADR-041).
    autoescape is on for html so all task-derived text is XSS-safe.
    """
    search: list[FileSystemLoader] = []
    if pm_path is not None:
        override = pm_path / "prompt-templates"
        if override.is_dir():
            search.append(FileSystemLoader(str(override)))
    search.append(FileSystemLoader(str(_TEMPLATES_DIR)))
    return Environment(
        loader=ChoiceLoader(search),
        autoescape=select_autoescape(["html"]),
    )


def build_prompt_pack_html(
    tasks: list[Task],
    *,
    project: Project,
    memories_by_task: dict[str, list[Memory]],
    decisions_by_id: dict[str, Decision],
    verify_commands: list[str],
    filter_label: str,
    group_by: str = "none",
    discipline: str = "",
    pm_path: Path | None = None,
) -> str:
    """Build a self-contained (no-CDN) HTML prompt pack.

    Renders a lane diagram (blocked_by=hard / after_recommended=soft as notes,
    priority + suggested_model chips) plus per-task cards each with a paste-ready
    body and a copy button. All task text flows through Jinja2 autoescape, and
    the copy button reads the ``<pre>`` ``textContent`` (never a JS string), so
    the output is XSS-safe by construction.
    """
    lanes = [
        {
            "label": label,
            "nodes": [task_node(t) for t in lane_tasks],
        }
        for label, lane_tasks in group_tasks(tasks, group_by)
    ]
    cards = [
        {
            "id": task.id,
            "title": task.title,
            "phase": task.phase,
            "priority": task.priority.value
            if hasattr(task.priority, "value")
            else str(task.priority),
            "status": task.status.value if hasattr(task.status, "value") else str(task.status),
            "body": build_prompt_body(
                task,
                memories=memories_by_task.get(task.id, []),
                decisions_by_id=decisions_by_id,
                verify_commands=verify_commands,
            ),
        }
        for task in tasks
    ]
    context = {
        "project_name": project.display_name or project.name,
        "filter_label": filter_label,
        "task_count": len(tasks),
        "group_by": group_by,
        "grouped": group_by in _VALID_GROUP_BY and group_by != "none",
        "lanes": lanes,
        "cards": cards,
        "discipline": discipline.strip(),
    }
    env = _prompt_pack_env(pm_path)
    template = env.get_template(_PROMPT_PACK_TEMPLATE)
    return template.render(**context)
