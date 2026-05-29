"""Project rules auto-management for PM Server.

Manages a marker-delimited section in target rule files (``CLAUDE.md``
for Claude Code; ``AGENTS.md`` for Codex CLI). This module is the
general-purpose foundation for multi-host rule injection introduced by
ADR-008 and elaborated in PMSERV-044.

For backward compatibility, ``pm_server.claudemd`` re-exports every
v0.4.x-vintage symbol below; existing callers continue to work
unchanged.
"""

from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from .utils import _atomic_write_text, _codex_config_path, _timestamped_backup

# Mapping from host id (used by `target` arg) to the rule-file basename
# managed in the project root. Same marker scheme is reused across hosts
# (ADR-008 #6) — Codex parses Markdown verbatim so HTML comments are
# inert (validated by SynapticLedger's existing AGENTS.md marker block).
TARGET_FILES: dict[str, str] = {
    "claude-code": "CLAUDE.md",
    "codex": "AGENTS.md",
}

TEMPLATE_VERSION = 8
BEGIN_MARKER = "<!-- pm-server:begin v={version} -->"
END_MARKER = "<!-- pm-server:end -->"
BEGIN_PATTERN = re.compile(r"<!-- pm-server:begin v=(\d+) -->")
OTHER_SECTION_PATTERN = re.compile(r"<!-- ([\w-]+):begin")

CLAUDEMD_TEMPLATE = """\
<!-- pm-server:begin v={version} -->
## PM Server 自動行動ルール（必ず従うこと）

### セッション開始時（最初の応答の前に必ず実行）
1. pm_status を MCP ツールとして実行し、現在の進捗を表示する
2. pm_next で次に着手すべきタスクを3件表示する
3. pm_recall で前回セッションの文脈を取得する
4. ブロッカーや期限超過があれば警告する
5. pm_status の claudemd.other_rule_sections に他のルールセクションが報告された場合、
   この CLAUDE.md 内の該当セクションのルールも全て実行する

### タスクに着手する前
1. 該当タスクを pm_update_task で in_progress に変更する

### 作業中に重要な発見・判断があった時
1. pm_remember で記憶を保存する（関連タスクIDがあれば task_id で紐付け）

### コンテキスト保全（Compaction / Clear 対策）
Claude Code はセッションが長くなるとコンテキストを自動圧縮（compaction）する。
圧縮のタイミングは予測できないため、重要な情報は随時保存すること。
1. 重要な発見・技術的判断は発生時点で即座に pm_remember で保存する（セッション終了を待たない）
2. 複雑な議論や設計検討の後は、結論を pm_remember でまとめて保存する
3. 3往復以上のやり取りで未記録の知見があれば、チェックポイントとして pm_remember で保存する
4. ユーザーが /clear する前は必ず pm_session_summary を実行する
5. Compaction 後にコンテキストが失われていると感じたら pm_recall で復元する

### 記憶の二重化を避ける（pm_remember と Claude Code auto memory の役割分担）
Claude Code には pm-server とは独立した「auto memory」(~/.claude/projects/<repo>/memory/) がある。
両者は互いを参照しない別ストアのため、同じ知識を両方へ書くと内容が分岐（drift）し、
片方のクエリからもう一方が見えなくなる（split-brain）。役割を固定すること:
1. プロジェクト知識（タスク・判断・教訓・設計・進捗）は pm_remember を正（SSoT）として保存する
2. auto memory には harness/ツール固有のメモ（build コマンド、環境の癖等）のみ委ねる
3. 同一の知識を pm_remember と auto memory の両方へ二重書き込みしない
4. 過去の知見を想起する時は pm_recall を一次情報源とする（auto memory は補助）

### タスク完了時（コードが動作確認できたら）
1. pm_update_task で done に変更する
2. all_issues_resolved フラグが返された場合、親タスクの完了もユーザーに提案する
3. pm_log に完了内容を記録する
4. 次の推薦タスクを pm_next で表示する
5. アトミックコミットを作成する

### タスク完了確認中にイシュー（課題）が見つかった時
1. 「欠陥（defect）」か「将来改善（enhancement）」かを判断し、適切なツールを選択する:
   - **欠陥**: pm_add_issue(..., severity="defect") を使う（既定）
     - phase は親タスクから自動継承される
     - 親タスクが done だった場合、自動で review に戻される（warnings[] で通知される）
   - **将来改善・親に紐付く提案**: pm_add_issue(..., severity="enhancement") を使う
     - 親の status は変更されない
   - **独立したバックログ項目**: pm_add_task を使う（親子関係は不要）
2. 欠陥イシューを解消したら pm_update_task で done に変更する
3. 全イシューが解消されると all_issues_resolved フラグが返される
4. 親タスクの完了をユーザーに提案する

### MCP ツールのレスポンスに warnings[] が含まれる場合
pm-server のツールは副作用（例: 親タスクの自動 revert）を warnings[] で返す。
サイレントに進めると「バグっぽく見える」ため、必ずユーザーに明示的に伝えること。
1. warnings[] の各エントリを日本語で要約してユーザーに明示する（要約に埋めない）
2. warnings[].remediation があれば、その対応を次の選択肢としてユーザーに提示する
3. 警告を拾い損ねたまま次の話題へ進まない

### 設計上の意思決定が発生した時
1. ユーザーに「ADRとして記録しますか？」と確認する
2. 承認されたら pm_add_decision で保存する

### ワークフロー管理
ワークフローはpm-serverの構造化された開発プロセスである。
テンプレートベースのステートマシンで、ステップごとにガイダンスを提供する。

#### ワークフロー開始時
1. ユーザーが機能開発やリサーチを始める時、pm_workflow_templates で利用可能なテンプレートを確認する
2. pm_workflow_start でワークフローを開始する（discovery: 調査・ブレスト、development: 実装）
3. 最初のステップのガイダンス（tool_hint, skill_hint, agent_hint）に従って作業を進める

#### ワークフロー進行中
1. 各ステップの作業が完了したら pm_workflow_advance で次へ進む
2. artifacts（ADR ID、タスクID等）があれば artifacts パラメータで記録する
3. gate（user_approval）のあるステップでは、ユーザーの承認を待ってから進む
4. loop ステップでは proceed=false でループバック、proceed=true でループ終了
5. pm_workflow_status でいつでも進捗を確認できる

#### ワークフロー完了時
1. chain_to がある場合（例: discovery → development）、次のワークフロー開始を提案する
2. ワークフロー完了を pm_log に記録する

### コーディングセッション終了時
1. 進行中のタスクの状態を確認し、必要に応じて更新する
2. pm_log にセッションの成果を記録する
3. pm_session_summary で要約を保存する
4. 未コミットの変更があればコミットする
<!-- pm-server:end -->"""


def get_claudemd_status(project_root: Path) -> dict:
    """Return the PM Server section status in CLAUDE.md.

    Returns:
        dict with keys: exists, has_pm_section, version, up_to_date,
        other_rule_sections
    """
    claude_md = project_root / "CLAUDE.md"
    result: dict = {
        "exists": claude_md.exists(),
        "has_pm_section": False,
        "version": None,
        "up_to_date": False,
        "other_rule_sections": [],
    }
    if not claude_md.exists():
        return result

    content = claude_md.read_text(encoding="utf-8")
    match = BEGIN_PATTERN.search(content)
    if match:
        result["has_pm_section"] = True
        result["version"] = int(match.group(1))
        result["up_to_date"] = result["version"] >= TEMPLATE_VERSION

    # Detect other MCP rule sections (any <!-- name:begin --> marker except pm-server)
    all_sections = OTHER_SECTION_PATTERN.findall(content)
    result["other_rule_sections"] = [s for s in all_sections if s != "pm-server"]

    return result


def _render_template() -> str:
    """Render the template with the current version."""
    return CLAUDEMD_TEMPLATE.format(version=TEMPLATE_VERSION)


def _separator_for(content: str) -> str:
    """Choose the right separator to append content."""
    if not content:
        return ""
    if content.endswith("\n\n"):
        return ""
    if content.endswith("\n"):
        return "\n"
    return "\n\n"


def ensure_claudemd(project_root: Path) -> str:
    """Ensure CLAUDE.md has the PM Server rules section.

    Called from pm_init. Behavior:
    - No CLAUDE.md -> create with PM section
    - No markers -> append PM section
    - Same version -> skip
    - Old version -> replace PM section

    Returns:
        Status message describing what was done.
    """
    status = get_claudemd_status(project_root)
    claude_md = project_root / "CLAUDE.md"
    template = _render_template()

    if not status["exists"]:
        claude_md.write_text(template + "\n", encoding="utf-8")
        return "created CLAUDE.md with PM Server rules"

    content = claude_md.read_text(encoding="utf-8")

    if not status["has_pm_section"]:
        separator = _separator_for(content)
        claude_md.write_text(content + separator + template + "\n", encoding="utf-8")
        return "appended PM Server rules to CLAUDE.md"

    if status["up_to_date"]:
        return "CLAUDE.md already has up-to-date PM Server rules (skipped)"

    # Old version -> replace
    return _replace_pm_section(claude_md, content, template)


def update_claudemd(project_root: Path) -> str:
    """Update the PM Server rules section to the latest template.

    Called from the pm_update_claudemd MCP tool. Unlike ensure_claudemd,
    this always replaces regardless of version.

    Returns:
        Status message describing what was done.
    """
    status = get_claudemd_status(project_root)
    claude_md = project_root / "CLAUDE.md"
    template = _render_template()

    if not status["exists"]:
        claude_md.write_text(template + "\n", encoding="utf-8")
        return "created CLAUDE.md with PM Server rules"

    content = claude_md.read_text(encoding="utf-8")

    if not status["has_pm_section"]:
        separator = _separator_for(content)
        claude_md.write_text(content + separator + template + "\n", encoding="utf-8")
        return "appended PM Server rules to CLAUDE.md"

    return _replace_pm_section(claude_md, content, template)


def _replace_pm_section(claude_md: Path, content: str, template: str) -> str:
    """Replace the marker-delimited PM section with new template."""
    begin_match = BEGIN_PATTERN.search(content)
    end_idx = content.find(END_MARKER)

    if begin_match and end_idx != -1:
        # Normal: replace between begin and end markers
        before = content[: begin_match.start()]
        after = content[end_idx + len(END_MARKER) :]
        new_content = before + template + after
        claude_md.write_text(new_content, encoding="utf-8")
        old_version = int(begin_match.group(1))
        return f"updated PM Server rules in CLAUDE.md (v{old_version} → v{TEMPLATE_VERSION})"

    if begin_match and end_idx == -1:
        # Corrupted: begin marker exists but no end marker — remove begin and everything after it
        before = content[: begin_match.start()]
        new_content = before.rstrip() + "\n\n" + template + "\n"
        claude_md.write_text(new_content, encoding="utf-8")
        return "replaced corrupted PM Server section in CLAUDE.md"

    # No markers at all — fallback to append
    separator = _separator_for(content)
    claude_md.write_text(content + separator + template + "\n", encoding="utf-8")
    return "appended PM Server rules to CLAUDE.md (no markers found)"


# --- PMSERV-044: multi-host detection + injection layer -------------------


def _scan_rule_file(path: Path) -> dict:
    """Return marker-status dict for any rule file (same shape as
    ``get_claudemd_status``).

    Internal helper for ``get_rules_status``. Does NOT replace
    ``get_claudemd_status``: the legacy function is preserved verbatim
    as the v0.4.x compatibility surface (ADR-008 #9).
    """
    result: dict = {
        "exists": path.exists(),
        "has_pm_section": False,
        "version": None,
        "up_to_date": False,
        "other_rule_sections": [],
    }
    if not path.exists():
        return result
    try:
        content = path.read_text(encoding="utf-8")
    except OSError:  # pragma: no cover — defensive FS guard (PMSERV-059)
        return result
    match = BEGIN_PATTERN.search(content)
    if match:
        result["has_pm_section"] = True
        result["version"] = int(match.group(1))
        result["up_to_date"] = result["version"] >= TEMPLATE_VERSION
    all_sections = OTHER_SECTION_PATTERN.findall(content)
    result["other_rule_sections"] = [s for s in all_sections if s != "pm-server"]
    return result


def get_rules_status(project_root: Path) -> dict[str, dict]:
    """Return per-host rule-file status keyed by host id.

    Each value has the same shape as ``get_claudemd_status``: ``exists``,
    ``has_pm_section``, ``version``, ``up_to_date``,
    ``other_rule_sections``. Host ids use underscore form
    (``"claude_code"``, ``"codex"``) so the dict is ergonomic in JSON
    and Python attribute-like access.
    """
    return {
        "claude_code": _scan_rule_file(project_root / TARGET_FILES["claude-code"]),
        "codex": _scan_rule_file(project_root / TARGET_FILES["codex"]),
    }


def _has_pm_marker(path: Path) -> bool:
    """Return True iff ``path`` exists and contains a pm-server begin marker.

    Used as a "positive signal" by ``detect_hosts``: a file already
    managed by pm-server in this project is strong evidence that the
    associated host is in active use, even if external probes (PATH /
    config files / env vars) fail to detect it.
    """
    if not path.exists():
        return False
    try:
        content = path.read_text(encoding="utf-8")
    except OSError:  # pragma: no cover — defensive FS guard (PMSERV-059)
        return False
    return bool(BEGIN_PATTERN.search(content))


def detect_hosts(project_root: Path) -> tuple[list[str], str]:
    """Detect which MCP hosts are present, returning (hosts, source).

    Strategy (PMSERV-044 spec v1, validated by super-research):
    1. **Filesystem (primary, deterministic)**: ``shutil.which("claude")``
       for Claude Code, ``~/.codex/config.toml`` exists for Codex.
    2. **Marker (positive signal)**: a project file already containing the
       pm-server marker proves the host is in active use here.
    3. **CLAUDECODE env var (positive signal only, never negative
       judgment)**: documented Claude Code child-process inheritance.
       No reliable Codex env var exists (Codex strips inherited env per
       ``[shell_environment_policy]``).
    4. **Tertiary fallback**: ``["claude-code"]`` — caller MUST surface
       a warning so the user can opt into an explicit ``target``.

    Returns:
        A 2-tuple ``(hosts, source)`` where ``source`` is one of
        ``"filesystem+marker+env"`` (any positive signal fired) or
        ``"fallback"`` (no signal, defaulted to claude-code).
    """
    hosts: set[str] = set()

    # Filesystem (primary)
    if shutil.which("claude") is not None:
        hosts.add("claude-code")
    if _codex_config_path().exists():
        hosts.add("codex")

    # Marker (positive signal)
    if _has_pm_marker(project_root / TARGET_FILES["claude-code"]):
        hosts.add("claude-code")
    if _has_pm_marker(project_root / TARGET_FILES["codex"]):
        hosts.add("codex")

    # Env var (positive only)
    if os.environ.get("CLAUDECODE") == "1":
        hosts.add("claude-code")

    if hosts:
        return sorted(hosts), "filesystem+marker+env"

    return ["claude-code"], "fallback"


#: The statuses a single rule-file injection can yield. Annotating
#: ``InjectResult.status`` (and the aggregate ``InjectSummary.overall_status``)
#: with this Literal pushes validation to the type checker, mirroring
#: ``installer.InstallStatus`` (PMSERV-110 / PMSERV-054 follow-up). Note the
#: aggregate never surfaces ``"appended"`` — ``_aggregate_overall_status``
#: collapses it into ``"updated"`` — but a single shared Literal keeps the
#: vocabulary in one place; the aggregate value is a documented subset.
InjectStatus = Literal[
    "created",
    "appended",
    "updated",
    "skipped",
    "failed",
]


@dataclass(frozen=True)
class InjectResult:
    """Outcome of injecting PM Server rules into a single rule file.

    Backward-compat-sensitive substrings in ``message`` are kept for
    legacy ``pm_update_claudemd`` callers (the v0.4.x dict shape is
    preserved by ``server.pm_update_claudemd``).

    Attributes:
        target_file: ``"CLAUDE.md"`` or ``"AGENTS.md"``.
        host: ``"claude-code"`` or ``"codex"``.
        status: ``"created"``, ``"appended"``, ``"updated"``,
            ``"skipped"``, or ``"failed"``.
        message: Human-readable detail. Does NOT include backup path or
            ``[dry-run]`` tag — those are presentation concerns owned
            exclusively by ``__main__._print_inject_summary`` (PMSERV-044
            cross-check R6, applies PMSERV-039 L1 lesson).
        backup_path: Path to ``.bak.<timestamp>`` if created, else None.
            Both CLAUDE.md and AGENTS.md are backed up before an existing
            file is overwritten (PMSERV-058 / ADR-008 amendment A5
            symmetrised the former v0.5.0 CLAUDE.md no-backup behaviour).
            ``None`` for newly created files and dry runs.
        is_dry_run: True if no on-disk side effects occurred.
    """

    target_file: str
    host: str
    status: InjectStatus
    message: str
    backup_path: Path | None = None
    is_dry_run: bool = False


@dataclass(frozen=True)
class InjectSummary:
    """Aggregate outcome of an ``inject_pm_rules`` invocation.

    Attributes:
        results: One ``InjectResult`` per processed host.
        detected_hosts: Hosts identified by ``detect_hosts`` (or the
            explicit-target list if ``target != "auto"``). Surfaced for
            UX transparency (PMSERV-044 cross-check R7/E1).
        detection_source: ``"filesystem+marker+env"``, ``"explicit"``,
            or ``"fallback"``.
        created: Subset of result target files that were newly created.
        updated: Subset of result target files whose existing pm-server
            section was overwritten or appended.
        overall_status: Worst case across results, with priority order
            ``failed > skipped > updated > created`` (cross-check D1).
    """

    results: list[InjectResult] = field(default_factory=list)
    detected_hosts: list[str] = field(default_factory=list)
    detection_source: str = "explicit"
    created: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    overall_status: InjectStatus = "skipped"


def _inject_into_file(
    path: Path,
    host: str,
    *,
    dry_run: bool = False,
) -> InjectResult:
    """Inject the pm-server marker section into a single rule file.

    Generalises the v0.4.x ``update_claudemd`` "always-replace"
    semantics to any rule file. A timestamped ``.bak.<timestamp>`` is
    created before overwriting any *existing* rule file — both
    ``CLAUDE.md`` and ``AGENTS.md`` (PMSERV-058 / ADR-008 amendment A5,
    which retired the v0.5.0 CLAUDE.md no-backup asymmetry). Newly created
    files have nothing to back up.

    Returned status reflects on-disk transition:
        * ``"created"`` — file did not exist
        * ``"appended"`` — file existed, no pm-server marker found
        * ``"updated"`` — pm-server marker found and rewritten
                           (also for corrupted begin-without-end markers)
        * ``"failed"`` — read/write/backup raised an OSError
    """
    target_file = path.name
    template = _render_template()

    # Symlink-safe resolution (cross-check D3): operate on the underlying
    # file so the backup names the real target, not the symlink itself.
    if path.exists() or path.is_symlink():
        resolved = path.resolve(strict=False)
    else:
        resolved = path

    # --- Path 1: file does not exist — create it ---
    if not resolved.exists():
        if not dry_run:
            try:
                _atomic_write_text(resolved, template + "\n")
            except OSError as e:  # pragma: no cover — defensive FS guard (PMSERV-059)
                return InjectResult(
                    target_file=target_file,
                    host=host,
                    status="failed",
                    message=f"failed to create {target_file}: {e}",
                    is_dry_run=dry_run,
                )
        return InjectResult(
            target_file=target_file,
            host=host,
            status="created",
            message=f"created {target_file} with PM Server rules",
            is_dry_run=dry_run,
        )

    # --- File exists: read current content ---
    try:
        content = resolved.read_text(encoding="utf-8")
    except OSError as e:  # pragma: no cover — defensive FS guard (PMSERV-059)
        return InjectResult(
            target_file=target_file,
            host=host,
            status="failed",
            message=f"failed to read {target_file}: {e}",
            is_dry_run=dry_run,
        )

    begin_match = BEGIN_PATTERN.search(content)
    end_idx = content.find(END_MARKER)

    # Compute new content + status + message
    if begin_match and end_idx != -1:
        before = content[: begin_match.start()]
        after = content[end_idx + len(END_MARKER) :]
        new_content = before + template + after
        old_version = int(begin_match.group(1))
        if new_content == content:
            # No-op: the on-disk section already matches the current template
            # byte-for-byte (e.g. v=N → v=N). Report "skipped" so dry-run (and
            # real runs) can tell "nothing to do" apart from an actual rewrite,
            # and skip the backup + write below — otherwise PMSERV-058 would
            # create a spurious CLAUDE.md backup for a no change (PMSERV-062).
            status = "skipped"
            message = (
                f"PM Server rules in {target_file} already up to date "
                f"(v{TEMPLATE_VERSION}); no changes"
            )
        else:
            status = "updated"
            message = (
                f"updated PM Server rules in {target_file} (v{old_version} → v{TEMPLATE_VERSION})"
            )
    elif begin_match and end_idx == -1:
        # Corrupted: begin without end — treat as replace-from-corruption
        before = content[: begin_match.start()]
        new_content = before.rstrip() + "\n\n" + template + "\n"
        status = "updated"
        message = f"replaced corrupted PM Server section in {target_file}"
    else:
        # No markers — append
        separator = _separator_for(content)
        new_content = content + separator + template + "\n"
        status = "appended"
        message = f"appended PM Server rules to {target_file}"

    # Backup + write only when the content actually changes. A no-op (status
    # "skipped" above) must not create a spurious backup or rewrite the file
    # (PMSERV-062 / PMSERV-058 synergy). New files take the create path above
    # and need no backup. Backup applies to every existing rule file
    # (PMSERV-058 / ADR-008 amendment A5: symmetrise CLAUDE.md with AGENTS.md).
    changed = status != "skipped"

    backup_path: Path | None = None
    if not dry_run and changed:
        try:
            backup_path = _timestamped_backup(resolved)
        except OSError as e:  # pragma: no cover — defensive FS guard (PMSERV-059)
            return InjectResult(
                target_file=target_file,
                host=host,
                status="failed",
                message=f"failed to back up {target_file}: {e}",
                is_dry_run=dry_run,
            )

    # Write
    if not dry_run and changed:
        try:
            _atomic_write_text(resolved, new_content)
        except OSError as e:
            return InjectResult(
                target_file=target_file,
                host=host,
                status="failed",
                message=f"failed to write {target_file}: {e}",
                backup_path=backup_path,
                is_dry_run=dry_run,
            )

    return InjectResult(
        target_file=target_file,
        host=host,
        status=status,
        message=message,
        backup_path=backup_path,
        is_dry_run=dry_run,
    )


def _safe_inject(path: Path, host: str, *, dry_run: bool) -> InjectResult:
    """Run ``_inject_into_file`` with a top-level exception guard.

    Per-host failures must not abort sibling hosts (ADR-008 design
    principle inherited from ADR-007 case C; cross-check D1).
    """
    try:
        return _inject_into_file(path, host, dry_run=dry_run)
    except Exception as e:  # noqa: BLE001 - intentional broad guard
        return InjectResult(
            target_file=TARGET_FILES[host],
            host=host,
            status="failed",
            message=f"unexpected error in {host} injection: {e}",
            is_dry_run=dry_run,
        )


#: Aggregation priority for ``_aggregate_overall_status`` (worst → best).
#: Typed against :data:`InjectStatus` so a stray value is a type error;
#: ``test_inject_status_priority_covers_all_statuses`` guards the inverse
#: (a status added to the Literal without a priority slot) (PMSERV-110).
_INJECT_STATUS_PRIORITY: tuple[InjectStatus, ...] = (
    "failed",
    "skipped",
    "updated",
    "appended",
    "created",
)


def _aggregate_overall_status(results: list[InjectResult]) -> InjectStatus:
    """Compute ``InjectSummary.overall_status`` with priority order
    ``failed > skipped > updated > created`` (cross-check D1).

    ``"appended"`` collapses to ``"updated"`` in the aggregate since both
    represent on-disk modification of an existing file, so the returned
    value is always one of ``created/updated/skipped/failed`` — a documented
    subset of :data:`InjectStatus`.
    """
    statuses = {r.status for r in results}
    for level in _INJECT_STATUS_PRIORITY:
        if level in statuses:
            return "updated" if level == "appended" else level
    return "skipped"


def inject_pm_rules(
    project_root: Path,
    *,
    target: str = "auto",
    dry_run: bool = False,
) -> InjectSummary:
    """Inject PM Server rules into per-host rule files.

    Args:
        project_root: Project root directory holding the rule files.
        target: One of ``TARGET_CHOICES``:

            * ``"auto"`` (default) — detect installed hosts via
              ``detect_hosts`` (filesystem + marker + CLAUDECODE).
            * ``"all"`` — process every known host unconditionally.
            * ``"claude-code"`` / ``"codex"`` — single-host targeting.

        dry_run: When True, no files are written or backed up; results
            still describe what *would* happen and per-result
            ``is_dry_run`` is True.

    Returns:
        ``InjectSummary`` aggregating per-host results. Per-host
        failures are isolated so a write failure in one rule file does
        NOT abort the sibling host (best-effort; ADR-008 + cross-check
        D1). The aggregate ``overall_status`` follows the priority
        order ``failed > skipped > updated > created``.

    Raises:
        ValueError: If ``target`` is not in ``TARGET_CHOICES``.
    """
    from .utils import TARGET_CHOICES

    if target not in TARGET_CHOICES:
        raise ValueError(f"unknown target: {target!r}. Expected one of {TARGET_CHOICES}.")

    # Resolve target → list of hosts + detection source
    if target == "auto":
        hosts, source = detect_hosts(project_root)
    elif target == "all":
        hosts = list(TARGET_FILES.keys())
        source = "explicit"
    else:
        hosts = [target]
        source = "explicit"

    # Per-host injection (best-effort, isolated failures)
    results = [
        _safe_inject(project_root / TARGET_FILES[host], host, dry_run=dry_run) for host in hosts
    ]

    # Aggregate UX-surfaced lists
    created = [r.target_file for r in results if r.status == "created"]
    updated = [r.target_file for r in results if r.status in ("updated", "appended")]

    return InjectSummary(
        results=results,
        detected_hosts=list(hosts),
        detection_source=source,
        created=created,
        updated=updated,
        overall_status=_aggregate_overall_status(results),
    )
