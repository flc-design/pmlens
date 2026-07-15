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

v1 scope (proposal §5): markdown only; tag/phase/priority/task_ids filters; one
built-in template; no data-model extensions. ``suggested_model`` /
``after_recommended`` / ``track`` and HTML output + progress diagram are v2 —
so this module reads only fields that already exist on the models today.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from .models import Decision, Memory, MemoryType, Project, Task

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


def render_task_card(
    task: Task,
    *,
    memories: list[Memory],
    decisions_by_id: dict[str, Decision],
    verify_commands: list[str],
) -> str:
    """Render one task as a markdown prompt card with a paste-ready block."""
    adr_refs = adr_refs_for_task(task, memories)
    cautions = [m for m in memories if m.type in _CAUTION_MEMORY_TYPES]

    body: list[str] = []
    body.append(f"{task.id} を実装してください（着手前に pm_update_task で in_progress に）。")
    body.append("")
    body.append("## 準備")
    body.append(f"- pm タスク {task.id} の description / acceptance_criteria を読む")
    if task.blocked_by:
        body.append(f"- 依存（先に完了が必要）: {', '.join(task.blocked_by)}")
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

    body_text = "\n".join(body)
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
) -> str:
    """Build the full markdown prompt pack for ``tasks``.

    ``memories_by_task`` maps task id → its linked memories (caller fetches
    them so this stays pure). ``decisions_by_id`` maps ADR id → Decision for
    titling linked ADRs. ``filter_label`` is a human description of the
    selection shown in the header.
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
        "---",
        "",
    ]
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
