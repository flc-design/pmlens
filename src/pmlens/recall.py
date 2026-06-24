"""Memory recall and context building.

Builds contextual memory blocks for session injection.
Supports Progressive Disclosure for token efficiency.
"""

from __future__ import annotations

from pathlib import Path

from .memory import MemoryStore
from .models import TaskStatus
from .storage import load_tasks


def _estimate_tokens(text: str) -> int:
    """Estimate token count for mixed Japanese/English text.

    Japanese characters are roughly 1.5-2 tokens each.
    ASCII words are roughly 1 token per 4 chars.
    We use len(text) // 2 as a practical approximation.
    """
    if not text:
        return 0
    return max(1, len(text) // 2)


def _truncate_to_tokens(text: str, max_tokens: int) -> str:
    """Truncate text to fit within a token budget."""
    if _estimate_tokens(text) <= max_tokens:
        return text
    # Approximate character limit from token budget
    char_limit = max_tokens * 2
    return text[:char_limit] + "..."


class ContextBuilder:
    """Build context injection from memories + project state.

    Uses Progressive Disclosure to fit context within a token budget:
      Layer 1: Last session summary (~200 tokens)
      Layer 2: In-progress task memories (~500 tokens)
      Layer 3: Recent decisions (~300 tokens)
      Layer 4: Related memories (~remaining tokens)

    Empty layers donate their budget to subsequent layers.
    """

    # Default budget allocation per layer
    LAYER_BUDGETS = (200, 500, 300, 0)  # Layer 4 gets remainder

    def __init__(
        self,
        memory_store: MemoryStore,
        pm_path: Path,
        current_session_id: str | None = None,
    ) -> None:
        self.store = memory_store
        self.pm_path = pm_path
        self.current_session_id = current_session_id

    def build_session_context(self, max_tokens: int = 2000) -> str:
        """Generate context block for session start.

        Returns a Markdown-formatted string within the token budget.
        """
        sections: list[str] = []
        remaining = max_tokens

        # Layer 1: Last session summary
        budget_1 = self.LAYER_BUDGETS[0]
        layer_1 = self._build_layer_summary(budget_1)
        tokens_1 = _estimate_tokens(layer_1)
        if layer_1:
            sections.append(layer_1)
            remaining -= tokens_1
        else:
            remaining += 0  # nothing to donate from empty layer

        # Layer 2: In-progress task memories
        budget_2 = self.LAYER_BUDGETS[1] + (budget_1 - tokens_1 if layer_1 else budget_1)
        layer_2 = self._build_layer_task_memories(budget_2)
        tokens_2 = _estimate_tokens(layer_2)
        if layer_2:
            sections.append(layer_2)
            remaining -= tokens_2

        # Layer 3: Recent decisions
        budget_3 = self.LAYER_BUDGETS[2] + (budget_2 - tokens_2 if layer_2 else budget_2)
        budget_3 = min(budget_3, remaining)
        layer_3 = self._build_layer_decisions(budget_3)
        tokens_3 = _estimate_tokens(layer_3)
        if layer_3:
            sections.append(layer_3)
            remaining -= tokens_3

        # Layer 4: Related memories (gets all remaining budget)
        budget_4 = remaining
        if budget_4 > 0:
            layer_4 = self._build_layer_recent(budget_4)
            if layer_4:
                sections.append(layer_4)

        if not sections:
            return ""

        header = "## 前回セッションからの引き継ぎ\n"
        return header + "\n".join(sections)

    def _build_layer_summary(self, budget: int) -> str:
        """Layer 1: Last session summary."""
        summary = self.store.get_latest_summary()
        if summary is None:
            return ""

        marker = ""
        if self.current_session_id is not None and summary.session_id != self.current_session_id:
            marker = " ⚠️ 別セッション"

        lines = [f"### 前回のセッション ({summary.session_id}){marker}"]
        lines.append(f"**要約**: {summary.summary}")
        if summary.goals:
            lines.append(f"**目標**: {summary.goals}")
        if summary.pending:
            lines.append("**保留事項**: " + ", ".join(summary.pending))

        text = "\n".join(lines)
        return _truncate_to_tokens(text, budget)

    def _build_layer_task_memories(self, budget: int) -> str:
        """Layer 2: Memories linked to in-progress tasks."""
        try:
            tasks = load_tasks(self.pm_path)
        except Exception:
            return ""

        in_progress = [t for t in tasks if t.status == TaskStatus.IN_PROGRESS]
        if not in_progress:
            return ""

        lines = ["### 進行中タスクの記憶"]
        used = _estimate_tokens(lines[0])

        for task in in_progress:
            memories = self.store.get_by_task(task.id)
            if not memories:
                continue
            task_header = f"**{task.id}**: {task.title}"
            used += _estimate_tokens(task_header)
            if used > budget:
                break
            lines.append(task_header)
            for mem in memories[:3]:  # max 3 memories per task
                entry = f"  - [{mem.type.value}] {mem.content}"
                entry_tokens = _estimate_tokens(entry)
                if used + entry_tokens > budget:
                    break
                lines.append(entry)
                used += entry_tokens

        return "\n".join(lines) if len(lines) > 1 else ""

    def _build_layer_decisions(self, budget: int) -> str:
        """Layer 3: Recent decision-linked memories."""
        memories = [m for m in self.store.get_recent(limit=20) if m.decision_id]
        if not memories:
            return ""

        lines = ["### 最近の判断"]
        used = _estimate_tokens(lines[0])

        for mem in memories[:5]:
            entry = f"- **{mem.decision_id}**: {mem.content}"
            entry_tokens = _estimate_tokens(entry)
            if used + entry_tokens > budget:
                break
            lines.append(entry)
            used += entry_tokens

        return "\n".join(lines) if len(lines) > 1 else ""

    def _build_layer_recent(self, budget: int) -> str:
        """Layer 4: Recent memories not already covered."""
        memories = self.store.get_recent(limit=10)
        if not memories:
            return ""

        lines = ["### 最近の記憶"]
        used = _estimate_tokens(lines[0])

        for mem in memories:
            prefix = f"[{mem.type.value}]"
            task_ref = f" ({mem.task_id})" if mem.task_id else ""
            entry = f"- {prefix}{task_ref} {mem.content}"
            entry_tokens = _estimate_tokens(entry)
            if used + entry_tokens > budget:
                break
            lines.append(entry)
            used += entry_tokens

        return "\n".join(lines) if len(lines) > 1 else ""
