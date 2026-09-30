"""Project rules auto-management for PM Lens.

Manages a marker-delimited section in target rule files (``CLAUDE.md``
for Claude Code; ``AGENTS.md`` for Codex CLI, Cursor and Grok Build).
This module is the general-purpose foundation for multi-host rule
injection introduced by ADR-008 and elaborated in PMSERV-044, with the
host facts themselves moved into :mod:`pmlens.hosts` by PMSERV-165.

Because three hosts share ``AGENTS.md``, injection is keyed on the rule
FILE rather than on the host — see :func:`inject_pm_rules`.

For backward compatibility, ``pmlens.claudemd`` re-exports every
v0.4.x-vintage symbol below; existing callers continue to work
unchanged.
"""

from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Literal

from .hosts import HOSTS, hosts_reading_both_rule_files, rule_files
from .models import PmServerError
from .utils import TARGET_CHOICES, _atomic_write_text, _timestamped_backup

# Mapping from host id (used by `target` arg) to the rule-file basename
# managed in the project root. Same marker scheme is reused across hosts
# (ADR-008 #6) — Codex parses Markdown verbatim so HTML comments are
# inert (validated by SynapticLedger's existing AGENTS.md marker block).
#
# Derived from the host registry (PMSERV-165), never restated: a host present
# in _KNOWN_HOSTS but absent here used to raise a bare KeyError out of an MCP
# tool, because the lookup happens in inject_pm_rules's comprehension — OUTSIDE
# _safe_inject's guard. Several hosts map to the same file; see
# inject_pm_rules for the deduplication that implies.
TARGET_FILES: dict[str, str] = rule_files()
assert set(TARGET_FILES) == set(HOSTS), "TARGET_FILES drifted from the host registry"

#: Targets accepted by :func:`inject_pm_rules`: the shared install targets plus
#: ``"existing"``, which only applies to rule injection (installing an MCP
#: server has no notion of "files that already carry the section").
RULE_TARGET_CHOICES: tuple[str, ...] = (*TARGET_CHOICES, "existing")

# v12 (PMSERV-165): the template is injected into AGENTS.md for Codex, Cursor
# and Grok Build as well as into CLAUDE.md, so its self-references had to stop
# naming CLAUDE.md; and the ADR-028 branch clause was factually wrong — it keyed
# on "hosts without hooks", but Cursor and Grok Build both HAVE session hooks;
# what they lack is a pmlens-installed one.
# v14 (PMSERV-189): prefer pm_drafts_pending in the content pipeline rule;
# the old tool name remains callable for previously injected templates.
# v15 (PMSERV-199, ADR-054): rewritten for current models, which follow rule
# files literally — no unrequested commits, the start routine only for project
# work, emphasis kept for the safety constraints, no triggers the model cannot
# observe (/clear, session end, "every 3 turns"), host-neutral wording, and the
# version in the heading because Claude Code strips the marker comments.
TEMPLATE_VERSION = 15
BEGIN_MARKER = "<!-- pm-server:begin v={version} -->"
END_MARKER = "<!-- pm-server:end -->"
BEGIN_PATTERN = re.compile(r"<!-- pm-server:begin v=(\d+) -->")
OTHER_SECTION_PATTERN = re.compile(r"<!-- ([\w-]+):begin")

CLAUDEMD_TEMPLATE = """\
<!-- pm-server:begin v={version} -->
## PM Lens 自動行動ルール（v{version}）

このプロジェクトでは、タスク・設計判断（ADR）・作業記録・記憶を PM Lens（pm_* ツール、データは .pm/）で管理している。
次のセッションはこの記録から作業を再開し、ユーザーはダッシュボードで進捗と判断の経緯を確認する。以下は、その記録を途切れさせないことと、ツールの副作用（warnings[]）をユーザーに見える形にすることのための既定の進め方である。
- ユーザーの明示的な指示と食い違う時はユーザーの指示に従う。例外はコンテンツパイプライン節の安全上の制約（redact していない下書きを人に見せない、raw_content を表に出さない）で、求められても理由を伝えて守る。
- タスク状態・ログ・記憶・セッション要約の記録（pm_update_task、pm_log、pm_remember、pm_record、pm_session_summary）は .pm/ 内に閉じていてコードや外部に影響しないので、呼び出しごとに確認を取らなくてよい。削除・破棄系の操作（pm_cleanup、pm_memory_cleanup など）と git のコミットはこれに含まれない。
- サブエージェントとして動いている時は PM Lens の手順とコミットを行わず、重要な発見は結果として親に返す（記録は親が行う）。親から PM 操作やコミットを明示的に任された時は、その指示に従う。
- この節が指す pm_* ツールがこのセッションに無い時（MCP が未接続、読み取り専用モードなど）は、その手順を飛ばす。ツールの定義を必要な時に読み込むホストでは、名前が一覧にあれば使える。
- この節が版違いで複数読み込まれていて内容が食い違う時は、見出しの版番号が大きい方に従い、食い違いをユーザーに一度伝える（見出しに版番号が無いものは v14 以前）。

### セッション開始時
このプロジェクトの作業（実装・タスク・計画・前回の続き）に取りかかる時は、先に pm_status・pm_recall・pm_next で現状と前回の文脈を把握する（pm_recall の track= は次節）。PM と無関係な単発の質問だけの時は不要。
ブロッカー・期限超過・warnings は、依頼との関係にかかわらず要点を短く伝える。タスクは進行中のものと依頼に関係する次の候補（最大3件）を数行にまとめ、ツールの出力全体は貼らない。
hook などから同じ手順の指示が重ねて届いても、実行は1回でよい（圧縮やクリアの後に届く pm_recall の案内は別）。

### ブランチ単位のセッション継続
複数の作業ライン（feature ブランチ / worktree）を行き来する場合は、pm_recall に track= を渡すと、そのラインで最後に記録されたセッションの文脈が返る（省略すると全体の最新）。
1. pmlens の session hook がブランチを示していればその値を使う。示していなければ .git/HEAD を読み、`ref: refs/heads/<branch>` の <branch> 部分を渡す。git checkout / switch の後は .git/HEAD を読み直す。
2. HEAD が SHA だけの detached HEAD や、.git がファイルの worktree ではブランチ無しとして track を省く。保存側（pm_session_summary）も同じ規則で記録するので値が一致する。pm-server は読み取り経路で git を実行しないので、ブランチの取得はこちらで行う。
3. .pm/tracks.yaml に `tracks: {{ラベル: [glob, ...]}}`（例: 本流→main、論文→feat/p3-*）があれば、ブランチ名の代わりにラベルを渡せる（glob に合うブランチを束ねた最新が返る）。
4. 応答の track_matched=false は、そのラインにまだ記録が無く全体の最新にフォールバックしたことを示す。track_branch で由来を確かめる。

### タスクに着手する前
該当タスクを pm_update_task で in_progress にする。

### 作業中に重要な発見・判断があった時（コンテキスト保全）
会話の履歴はホストによって圧縮（Compaction）やリセットをされることがあり、途中で得た結論は後から参照できなくなる。後の作業で必要になる発見・判断・結論は、確定した時点で理由とともに pm_remember に1件ずつ保存する（関連タスクがあれば task_id、type は lesson / insight / observation）。
- 1件に1つの知見を書き、1行目に要約、続けて理由を書く。うまくいった方針の確認やユーザーからの訂正も記録する。
- コミット・ADR・タスク・pm_log・pm_record・コードから分かることや、既に保存した内容は保存しない。誤りの訂正は、どの記録を訂正したかを書いて新しく保存する。
- 圧縮やリセットの後で以前の判断や作業状態が必要になったら、pm_recall（該当すれば track= 付き）で取り直す。

### 記憶の二重化を避ける（pm_remember とホスト自身の記憶機能の分担）
Claude Code の auto memory（~/.claude/projects/<repo>/memory/）のように、ホスト自身が記憶機能を持つ場合がある。両者は互いを参照しないので、同じ事実を両方に書くと内容が食い違っていく。二重書き込みはしない。
- プロジェクトの事実は pmlens 側を正（SSoT）とする。タスクは pm_add_task / pm_update_task、判断は pm_add_decision、調査結果は pm_record、それ以外の知見は pm_remember に書き、過去の知見は pm_recall を先に引く。
- ユーザー本人の好み、進め方へのフィードバック、ビルドコマンドや環境の癖は、ホスト側の記憶機能に保存してよい。

### タスク完了時（動作確認ができたら）
1. pm_update_task で done にする。all_issues_resolved が返ったら、親タスクの完了をユーザーに提案する。
2. pm_log に完了内容を記録する。このセッションのツール結果で裏付けられることだけを完了として書き、裏付けの無い項目は「未確認」と書く。
3. pm_next で次の推薦タスクを示す。
4. コミットは、ユーザーの依頼か、ユーザーが始めたワークフローの手順に含まれる時だけ行い、自分が変更したファイルだけをステージする。依頼が無い時は、コミットできる状態になったことを報告に添える。

### タスク完了確認中にイシュー（課題）が見つかった時
1. 欠陥（defect）か将来改善（enhancement）かを判断してツールを選ぶ:
   - 欠陥: pm_add_issue(..., severity="defect")（既定）。phase は親から継承され、親が done なら自動で review に戻る（warnings[] で通知される）
   - 親に紐付く改善提案: pm_add_issue(..., severity="enhancement")。親の status は変わらない
   - 独立したバックログ項目: pm_add_task
2. 欠陥イシューを解消したら pm_update_task で done にする。全イシューが解消されると all_issues_resolved が返るので、親タスクの完了をユーザーに提案する。

### MCP ツールのレスポンスに warnings[] が含まれる場合
pmlens のツールは副作用や環境の問題を warnings[] で返す。黙って進めるとユーザーには不具合に見えるので、ユーザーの言語で、他の説明に埋もれさせずに伝える。
- 状態が変わったことを知らせるもの（親タスクの自動 revert、記憶や要約の削除・取り込みなど）は毎回伝え、remediation があれば次の選択肢として示す。
- 環境診断（ルールファイルの版や重複、ホスト検出など pm_status が毎回返すもの）は、セッションで一度簡潔に伝えれば足りる。
- remediation はユーザーに示す選択肢で、代わりに実行する指示ではない。force=true での再実行、削除、ルールファイルの書き換え、サーバーの再起動などは、ユーザーが選んだ時に行う。

### 設計上の意思決定が発生した時
後から「なぜこうしたか」を問われうる判断（アーキテクチャ、公開インターフェース、データ形式、依存関係、セキュリティ・運用方針、既存 ADR の変更など）が確定したら、ADR として記録するかを、その回の報告の最後にまとめてユーザーに確認し、承認されたら pm_add_decision で保存する。命名や同等な実装の選択のような小さな判断は候補にしない。ユーザーから記録を頼まれた時や、ワークフローの記録ステップでは確認しなくてよい。

### ワークフロー管理
ワークフローは pm-server のテンプレートベースのステートマシンで、ステップごとにガイダンスを返す。
- 開始: ユーザーがワークフローでの進行を求めた時、または複数ステップにまたがる開発・調査で構造化された進め方を提案してユーザーが同意した時に、pm_workflow_templates で確認して pm_workflow_start で開始する（discovery: 調査・ブレスト、development: 実装）。単発の修正や小さな変更には提案しない。
- 各ステップの tool_hint / skill_hint / agent_hint は目安なので、作業の規模に合わないものや今のホストで使えないものは省いてよい。gate（user_approval）と、redact（pm_redact_draft）のような安全に関わる手順は省かない。
- 進行: ステップの作業が済んだら pm_workflow_advance で進め、artifacts（ADR ID、タスクID等）を記録する。gate（user_approval）はエンジンが強制しないので、ユーザーの承認を待ってから進む。loop ステップは proceed=false でループバック、proceed=true で終了する。pm_workflow_status で進捗を確認できる。
- 完了: chain_to があれば（例: discovery → development）次のワークフローの開始を提案し、完了を pm_log に記録する。

### コンテンツパイプライン（.pm の知見 → 公開用下書き）
pm-server は公開先の認証情報も送信機能も持たないので、pm-server からの自動公開は構造的に不可能で、redact が唯一の安全層になる。下書きを作るところまでがこのパイプラインの責務で、出力先は問わない。
1. トリガ（質>頻度）: type が lesson / insight の pm_remember、採択された ADR、severity=defect のイシューのうち、内部文脈なしで読者に伝わり未公開の計画に結びつかないものを記録した時だけ、下書きの作成を一度提案してよい（propose-don't-force: 強制しない）。ユーザーが乗ったら pm_workflow_start で content-pipeline ワークフローを開始し、その手順に従う。
2. 下書きは人に見せる前に必ず pm_redact_draft(draft_id) で redact する（1回の呼び出しで全セグメントが処理される）。redact 済みの本文は pm_drafts_pending（このセッションに無ければ pm_x_drafts_pending）で取り出し、シークレットや個人情報が残っていないか確かめる。/secret-scan・/privacy-check のようなスキャン用コマンドがあれば使い、無ければ追加のスキャンをしていないことをユーザーに伝える。
3. 公開の判断と操作はユーザーが行う。ユーザーの明示の依頼が無い限り、モデルから下書きを投稿・送信しない。依頼された場合も、redact 済みで人間が確認した下書きだけを扱う（redact は機械的な一次フィルタで、公開の可否は人間が判断するため）。
4. raw_content（redact 前の原文）はシークレットや個人情報を含みうるので、下書き作成ツールへの入力以外では表示せず、他のツールにも渡さない。

### 作業の区切りとセッションの終了
/clear の直前にモデルのターンは来ないので、終了の合図を待たずに、タスク完了や議論の結論などの区切りと、ユーザーが作業の終了を告げた時に次を行う。
1. 進行中タスクの状態を必要なら更新する。
2. タスク完了以外の区切り（調査の結論、ブロッカーの発見、設計判断）は、pm_log に category=milestone / blocker / decision で1件記録する（タスク完了時に記録済みなら不要）。
3. pm_session_summary に要約（summary と pending）を保存する。保存のたびにこのセッションの前回の要約は置き換わるので、毎回セッション全体の要約と未完了の項目を書く。完了として書くのは、ツール結果で裏付けられることだけ。
4. 未コミットの変更があれば、有無と概略をユーザーに伝える（コミットするかはユーザーが決める）。
<!-- pm-server:end -->"""


def get_claudemd_status(project_root: Path) -> dict:
    """Return the PM Lens section status in CLAUDE.md.

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
    """Ensure CLAUDE.md has the PM Lens rules section.

    Called from pm_init. Behavior:
    - No CLAUDE.md -> create with PM section
    - No markers -> append PM section
    - Same version -> skip
    - Old version -> replace PM section

    Returns:
        Status message describing what was done.
    """
    guard_not_home_root(project_root)
    status = get_claudemd_status(project_root)
    claude_md = project_root / "CLAUDE.md"
    template = _render_template()

    if not status["exists"]:
        claude_md.write_text(template + "\n", encoding="utf-8")
        return "created CLAUDE.md with PM Lens rules"

    content = claude_md.read_text(encoding="utf-8")

    if not status["has_pm_section"]:
        separator = _separator_for(content)
        claude_md.write_text(content + separator + template + "\n", encoding="utf-8")
        return "appended PM Lens rules to CLAUDE.md"

    if status["up_to_date"]:
        return "CLAUDE.md already has up-to-date PM Lens rules (skipped)"

    # Old version -> replace
    return _replace_pm_section(claude_md, content, template)


def update_claudemd(project_root: Path, *, force: bool = False) -> str:
    """Update the PM Lens rules section to the latest template.

    Called by the deprecated ``update-claudemd`` CLI. Unlike ensure_claudemd,
    this rewrites a section of the same or an older version, but — like
    :func:`inject_pm_rules` — it leaves a section NEWER than this build's
    template alone unless ``force`` is set (ADR-055).

    Returns:
        Status message describing what was done.
    """
    guard_not_home_root(project_root)
    status = get_claudemd_status(project_root)
    claude_md = project_root / "CLAUDE.md"
    template = _render_template()

    if not status["exists"]:
        claude_md.write_text(template + "\n", encoding="utf-8")
        return "created CLAUDE.md with PM Lens rules"

    content = claude_md.read_text(encoding="utf-8")

    if not status["has_pm_section"]:
        separator = _separator_for(content)
        claude_md.write_text(content + separator + template + "\n", encoding="utf-8")
        return "appended PM Lens rules to CLAUDE.md"

    if status["version"] > TEMPLATE_VERSION and not force:
        return _downgrade_refusal_message("CLAUDE.md", status["version"]) + " (skipped)"

    return _replace_pm_section(claude_md, content, template)


def _downgrade_refusal_message(target_file: str, version: int) -> str:
    """Explain why a section newer than this build's template was left alone."""
    return (
        f"PM Lens rules in {target_file} are v{version}, newer than this pmlens "
        f"(template v{TEMPLATE_VERSION}); left unchanged. Upgrade pmlens, or pass "
        f"force to rewrite them with v{TEMPLATE_VERSION}"
    )


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
        return f"updated PM Lens rules in CLAUDE.md (v{old_version} → v{TEMPLATE_VERSION})"

    if begin_match and end_idx == -1:
        # Corrupted: begin marker exists but no end marker — remove begin and everything after it
        before = content[: begin_match.start()]
        new_content = before.rstrip() + "\n\n" + template + "\n"
        claude_md.write_text(new_content, encoding="utf-8")
        return "replaced corrupted PM Lens section in CLAUDE.md"

    # No markers at all — fallback to append
    separator = _separator_for(content)
    claude_md.write_text(content + separator + template + "\n", encoding="utf-8")
    return "appended PM Lens rules to CLAUDE.md (no markers found)"


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
    and Python attribute-like access — hence ``HostSpec.status_key``
    rather than deriving the key from ``host_id`` and silently renaming
    ``claude_code`` to ``claude-code``.

    Hosts sharing a rule file (``codex``, ``cursor`` and ``grok`` all read
    ``AGENTS.md``) therefore report **identical** status. That is accurate —
    each of them really does see that marker — not a bug.
    """
    return {
        spec.status_key: _scan_rule_file(project_root / spec.rule_file) for spec in HOSTS.values()
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

    Strategy (PMSERV-044 spec v1, validated by super-research; extended to
    four hosts by PMSERV-165):
    1. **Filesystem (primary, deterministic)**: the host's install marker
       exists (``~/.codex/config.toml``, ``~/.cursor/``, ``~/.grok/config.toml``)
       or one of its ``probe_binaries`` is on PATH.
    2. **Marker (positive signal)**: a project file already containing the
       pm-server marker proves the host is in active use here.
    3. **CLAUDECODE env var (positive signal only, never negative
       judgment)**: documented Claude Code child-process inheritance.
       No reliable Codex env var exists (Codex strips inherited env per
       ``[shell_environment_policy]``).
    4. **Tertiary fallback**: ``["claude-code"]`` — caller MUST surface
       a warning so the user can opt into an explicit ``target``.

    **RO invariant (ADR-028).** This function is reachable from ``pm_status``
    via ``get_rules_status``, a read-only MCP tool, so every probe is
    ``shutil.which`` / ``Path.exists`` / ``os.environ`` only. Never run a
    host's own CLI (``grok inspect``, ``cursor-agent --version``) from here —
    that would put a subprocess on a read path.

    Note that ``codex``, ``cursor`` and ``grok`` share ``AGENTS.md``, so the
    marker signal cannot tell them apart: a project with a pm-managed
    AGENTS.md reports all three. That is deliberate — being wrong about
    *which* AGENTS.md host is present costs nothing (the file is written once
    either way), whereas missing a host means it silently gets no rules.

    Returns:
        A 2-tuple ``(hosts, source)`` where ``source`` is one of
        ``"filesystem+marker+env"`` (any positive signal fired) or
        ``"fallback"`` (no signal, defaulted to claude-code).
    """
    hosts: set[str] = set()

    for host_id, spec in HOSTS.items():
        # Filesystem (primary)
        if spec.install_marker is not None and spec.install_marker().exists():
            hosts.add(host_id)
        elif any(shutil.which(binary) is not None for binary in spec.probe_binaries):
            hosts.add(host_id)
        # Marker (positive signal)
        if _has_pm_marker(project_root / spec.rule_file):
            hosts.add(host_id)

    # Env var (positive only)
    if os.environ.get("CLAUDECODE") == "1":
        hosts.add("claude-code")

    if hosts:
        return sorted(hosts), "filesystem+marker+env"

    return ["claude-code"], "fallback"


def duplicate_rule_file_warning(project_root: Path) -> dict | None:
    """Warn when a detected host would read the PM rules twice (PMSERV-165).

    Grok Build scans ``AGENTS.md`` **and** ``CLAUDE.md`` in the same directory
    and loads every match, so a project managed for both Claude Code and Codex
    feeds it the same rule block twice. Neither file can be removed — Claude
    Code and Codex each need their own — so this is reported, not fixed.

    Only fires when the duplication is real: the host must actually be present
    on this machine, and both files must actually carry the pm-server marker.

    Returns:
        A ``warnings[]``-shaped dict, or ``None`` when nothing duplicates.
    """
    doubling = [
        HOSTS[h]
        for h in hosts_reading_both_rule_files()
        if HOSTS[h].install_marker is not None and HOSTS[h].install_marker().exists()
    ]
    if not doubling:
        return None

    claude_version = _scan_rule_file(project_root / "CLAUDE.md")["version"]
    affected = [
        spec
        for spec in doubling
        if spec.rule_file != "CLAUDE.md"
        and claude_version is not None
        # Different versions are not a harmless duplicate but a conflict;
        # rules_version_warnings reports that as rule_file_version_mismatch.
        and _scan_rule_file(project_root / spec.rule_file)["version"] == claude_version
    ]
    if not affected:
        return None

    names = ", ".join(spec.display_name for spec in affected)
    files = ", ".join(sorted({spec.rule_file for spec in affected} | {"CLAUDE.md"}))
    return {
        "code": "duplicate_rule_file_for_host",
        "message": (
            f"{names} reads every recognised rule file in a directory, and this "
            f"project has the PM Lens section in both {files}. Those hosts "
            "therefore receive the same rules twice, consuming context without "
            "adding information. Claude Code and Codex each require their own "
            "file, so removing one is not the fix."
        ),
        "remediation": (
            "Harmless but wasteful, since both copies are the same version. To drop "
            f"the duplicate in a repo used only with {names}, remove the PM Lens "
            "section from CLAUDE.md by hand; pm_update_rules(target='codex') keeps "
            "AGENTS.md current."
        ),
    }


#: The first template version that stops telling the model to commit on its
#: own (ADR-054). Named in the rules_outdated warning so the user can see why
#: an old section is worth replacing.
_FIRST_VERSION_WITHOUT_AUTO_COMMIT = 15


def pm_section_versions(project_root: Path) -> dict[str, int]:
    """Map each rule file that carries a PM Lens section to its version.

    Ordered by the host registry (CLAUDE.md first), one entry per file.
    """
    versions: dict[str, int] = {}
    for rule_file in dict.fromkeys(TARGET_FILES.values()):
        status = _scan_rule_file(project_root / rule_file)
        if status["has_pm_section"]:
            versions[rule_file] = status["version"]
    return versions


def rules_newer_than_server_warning(files: dict[str, int]) -> dict:
    """Build the warning for sections newer than this build's template.

    Shared by ``pm_status`` (detection) and ``pm_update_rules`` (a refused
    downgrade) so both say the same thing.
    """
    listing = ", ".join(f"{name} (v{version})" for name, version in files.items())
    return {
        "code": "rules_newer_than_server",
        "message": (
            f"The PM Lens section in {listing} is newer than the template this "
            f"pmlens ships (v{TEMPLATE_VERSION}), so this MCP server is older than "
            "the pmlens that last wrote the file. The section is left as it is: "
            "pm_update_rules skips it instead of downgrading it."
        ),
        "remediation": (
            "Upgrade the pmlens this host runs (for example `pipx upgrade pmlens`, "
            "or raise the plugin's pinned version) so the server and the rule "
            "files agree."
        ),
    }


def rules_version_warnings(project_root: Path) -> list[dict]:
    """Report PM Lens sections that disagree with this build or each other.

    Three conditions, each a ``warnings[]``-shaped dict (ADR-055):

    * ``rules_outdated`` — a section older than :data:`TEMPLATE_VERSION`.
      Deployed sections are only rewritten on request, so without this the
      older guidance stays in the project indefinitely.
    * ``rules_newer_than_server`` — a section newer than this build: another,
      newer pmlens wrote it, and this server would downgrade it if asked to
      rewrite it (it now refuses to).
    * ``rule_file_version_mismatch`` — CLAUDE.md and AGENTS.md carry
      different versions, so hosts that read both get two rule sets.

    Read-only (``_scan_rule_file`` per file), so it is legal on the
    ``pm_status`` read path (ADR-028).
    """
    versions = pm_section_versions(project_root)
    warnings: list[dict] = []

    outdated = {name: v for name, v in versions.items() if v < TEMPLATE_VERSION}
    if outdated:
        listing = ", ".join(f"{name} (v{v})" for name, v in outdated.items())
        reason = ""
        if (
            min(outdated.values()) < _FIRST_VERSION_WITHOUT_AUTO_COMMIT
            and TEMPLATE_VERSION >= _FIRST_VERSION_WITHOUT_AUTO_COMMIT
        ):
            reason = (
                f" Sections before v{_FIRST_VERSION_WITHOUT_AUTO_COMMIT} tell the "
                "model to commit without being asked."
            )
        warnings.append(
            {
                "code": "rules_outdated",
                "message": (
                    f"The PM Lens section in {listing} is older than the template "
                    f"this pmlens ships (v{TEMPLATE_VERSION}).{reason}"
                ),
                "remediation": (
                    "With the user's agreement, run pm_update_rules(target='existing') "
                    "from a full-mode pmlens host to rewrite only the files that "
                    "already carry the section (a timestamped backup is kept). The "
                    "files may be committed and shared, so the user should review "
                    "the diff."
                ),
            }
        )

    newer = {name: v for name, v in versions.items() if v > TEMPLATE_VERSION}
    if newer:
        warnings.append(rules_newer_than_server_warning(newer))

    if len(set(versions.values())) > 1:
        listing = " and ".join(f"{name} v{v}" for name, v in versions.items())
        readers = ", ".join(HOSTS[h].display_name for h in hosts_reading_both_rule_files())
        warnings.append(
            {
                "code": "rule_file_version_mismatch",
                "message": (
                    f"This project carries PM Lens rules {listing}. Hosts that read "
                    f"both files ({readers}) receive two different rule sets and may "
                    "follow either one."
                ),
                "remediation": (
                    "Run pm_update_rules(target='existing') from the newest pmlens on "
                    "this machine so both files carry the same version."
                ),
            }
        )

    return warnings


def _forbidden_root_kind(project_root: Path) -> str | None:
    """Classify ``project_root`` as ``"the filesystem root"``, ``"the home
    directory"`` or ``None`` (an ordinary directory).

    Both are ancestors of every project on the machine, so a rule file there
    is loaded into every Claude Code session. Home resolution failures
    (``RuntimeError`` from ``Path.home()`` when ``$HOME`` is unset and the
    passwd lookup fails) fall through to ``None`` — the guard cannot decide,
    and blocking every project write in such an environment would be a worse
    regression than the one it prevents.
    """
    try:
        resolved = project_root.resolve()
    except (OSError, RuntimeError):  # pragma: no cover — defensive FS guard
        return None
    if resolved == Path(resolved.anchor):
        return "the filesystem root"
    try:
        if resolved == Path.home().resolve():
            return "the home directory"
    except (OSError, RuntimeError):  # pragma: no cover — no resolvable home
        return None
    return None


def _is_home_root(project_root: Path) -> bool:
    """Return True iff ``project_root`` resolves to the user's home directory."""
    return _forbidden_root_kind(project_root) == "the home directory"


def guard_not_home_root(project_root: Path) -> None:
    """Refuse to manage PM rule files directly under ``$HOME`` or ``/`` (ADR-053).

    Claude Code concatenates every ``CLAUDE.md`` between the filesystem root
    and the working directory at launch, so a ``$HOME/CLAUDE.md`` is injected
    into EVERY session on the machine — the opposite of pmlens's per-project
    model, and a guaranteed duplicate (often at a stale template version) for
    any project that already carries the section. ``pm_init`` reaches the
    writers with ``Path.cwd()`` when an MCP host runs with ``cwd=$HOME``
    (Claude Desktop does), which is exactly how the 2026-09-17 audit found a
    v11 section sitting in the user's home directory. A host running with
    ``cwd=/`` would do the same one level higher, so the filesystem root is
    refused too.

    Raises:
        PmServerError: when ``project_root`` is the home directory or ``/``.
    """
    kind = _forbidden_root_kind(project_root)
    if kind is not None:
        raise PmServerError(
            f"refusing to write PM Lens rules into {project_root / 'CLAUDE.md'}: "
            f"{kind} is an ancestor of every project, so Claude Code "
            "would load these rules into every session on this machine. Run "
            "from a project directory or pass project_path explicitly (ADR-053)."
        )


def ancestor_rules_warning(project_root: Path) -> dict | None:
    """Warn when an ANCESTOR directory's ``CLAUDE.md`` also carries the PM
    section (ADR-053).

    Claude Code loads ``CLAUDE.md`` from every directory above the working
    directory, so the project's own section and the ancestor's both land in
    context — usually at different template versions — and the official docs
    state Claude picks arbitrarily between conflicting instructions. pmlens
    never manages ancestor files, so this is reported, not fixed.

    Claude Code walks the *logical* working directory (the path as typed,
    symlinks intact), so both the logical ancestry and the resolved ancestry
    are scanned and de-duplicated by real file; ``CLAUDE.local.md`` is
    included because Claude Code loads it beside ``CLAUDE.md``.

    Read-only (``Path.is_file`` + ``read_text`` per ancestor, no subprocess),
    which keeps it legal on the ``pm_status`` read path (ADR-028).

    Returns:
        A ``warnings[]``-shaped dict (with an extra ``files`` list of the
        offending paths, resolved), or ``None`` when no ancestor carries the
        marker.
    """
    bases: list[Path] = [project_root if project_root.is_absolute() else Path.cwd() / project_root]
    try:
        resolved_root = project_root.resolve()
    except (OSError, RuntimeError):  # pragma: no cover — defensive FS guard
        resolved_root = None
    if resolved_root is not None and resolved_root != bases[0]:
        bases.append(resolved_root)

    seen: set[Path] = set()
    hits: list[tuple[Path, int]] = []
    for base in bases:
        for parent in base.parents:
            for name in ("CLAUDE.md", "CLAUDE.local.md"):
                candidate = parent / name
                if not candidate.is_file():
                    continue
                try:
                    real = candidate.resolve()
                except (OSError, RuntimeError):  # pragma: no cover — defensive FS guard
                    real = candidate
                if real in seen:
                    continue
                seen.add(real)
                try:
                    content = real.read_text(encoding="utf-8")
                except OSError:  # pragma: no cover — defensive FS guard
                    continue
                match = BEGIN_PATTERN.search(content)
                if match:
                    hits.append((real, int(match.group(1))))

    if not hits:
        return None
    hits.sort(key=lambda item: str(item[0]))

    listing = ", ".join(f"{path} (v{version})" for path, version in hits)
    own_version = _scan_rule_file(project_root / "CLAUDE.md")["version"]
    own = f"this project's own section (v{own_version})" if own_version else "this project's rules"
    return {
        "code": "pm_rules_in_ancestor_claudemd",
        "message": (
            "Claude Code loads every CLAUDE.md between the filesystem root and "
            "the working directory, and an ancestor of this project already "
            f"carries the PM Lens section: {listing}. Those rules are injected "
            f"alongside {own}, so Claude sees the rules twice and, when the "
            "versions differ, may follow either copy."
        ),
        "remediation": (
            "Remove the PM Lens section from the ancestor file (pmlens does not "
            "manage files outside the project root), or exclude it for this "
            "machine with the Claude Code `claudeMdExcludes` setting. Never run "
            "pm_init / pm_update_rules from $HOME."
        ),
        "files": [str(path) for path, _ in hits],
    }


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
    """Outcome of injecting PM Lens rules into a single rule file.

    Backward-compat-sensitive substrings in ``message`` are kept for
    legacy ``pm_update_claudemd`` callers (the v0.4.x dict shape is
    preserved by ``server.pm_update_claudemd``).

    Attributes:
        target_file: ``"CLAUDE.md"`` or ``"AGENTS.md"``.
        host: The host that OWNS this file — the first, in registry order,
            among those that read it. Several hosts share ``AGENTS.md``; this
            field stays single-valued for backward compatibility.
        hosts: Every host that reads ``target_file``, in registry order.
            ``("codex", "cursor", "grok")`` for a shared ``AGENTS.md``. Empty
            when the result was constructed directly rather than by
            ``inject_pm_rules`` (per-host failure paths).
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
        refused_downgrade: True when the file's section is newer than this
            build's template and was left alone (status ``"skipped"``;
            ADR-055). Callers surface ``rules_newer_than_server``.
    """

    target_file: str
    host: str
    status: InjectStatus
    message: str
    backup_path: Path | None = None
    is_dry_run: bool = False
    hosts: tuple[str, ...] = ()
    refused_downgrade: bool = False


@dataclass(frozen=True)
class InjectSummary:
    """Aggregate outcome of an ``inject_pm_rules`` invocation.

    Attributes:
        results: One ``InjectResult`` per processed host.
        detected_hosts: Hosts identified by ``detect_hosts`` (or the
            explicit-target list if ``target != "auto"``). Surfaced for
            UX transparency (PMSERV-044 cross-check R7/E1).
        detection_source: ``"filesystem+marker+env"``, ``"explicit"``,
            ``"existing"`` (``target="existing"``), or ``"fallback"``.
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
    force: bool = False,
) -> InjectResult:
    """Inject the pm-server marker section into a single rule file.

    Rewrites a section of the same or an older version. A section NEWER than
    this build's template is left alone unless ``force`` is set: several
    pmlens builds can share a machine (a pipx release and a dev checkout, a
    pinned plugin), and an older one must not silently downgrade what a newer
    one wrote (ADR-055). A timestamped ``.bak.<timestamp>`` is
    created before overwriting any *existing* rule file — both
    ``CLAUDE.md`` and ``AGENTS.md`` (PMSERV-058 / ADR-008 amendment A5,
    which retired the v0.5.0 CLAUDE.md no-backup asymmetry). Newly created
    files have nothing to back up.

    Returned status reflects on-disk transition:
        * ``"created"`` — file did not exist
        * ``"appended"`` — file existed, no pm-server marker found
        * ``"updated"`` — pm-server marker found and rewritten
                           (also for corrupted begin-without-end markers)
        * ``"skipped"`` — already current, or newer than this build
                           (``refused_downgrade``)
        * ``"failed"`` — read/write/backup raised an OSError
    """
    target_file = path.name
    # ADR-053: a rule file in $HOME is an ancestor of every project and would
    # be loaded into every Claude Code session on the machine. _safe_inject
    # turns the PmServerError into a "failed" result with this message.
    guard_not_home_root(path.parent)
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
            message=f"created {target_file} with PM Lens rules",
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

    if begin_match and int(begin_match.group(1)) > TEMPLATE_VERSION and not force:
        return InjectResult(
            target_file=target_file,
            host=host,
            status="skipped",
            message=_downgrade_refusal_message(target_file, int(begin_match.group(1))),
            is_dry_run=dry_run,
            refused_downgrade=True,
        )

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
                f"PM Lens rules in {target_file} already up to date "
                f"(v{TEMPLATE_VERSION}); no changes"
            )
        else:
            status = "updated"
            message = (
                f"updated PM Lens rules in {target_file} (v{old_version} → v{TEMPLATE_VERSION})"
            )
    elif begin_match and end_idx == -1:
        # Corrupted: begin without end — treat as replace-from-corruption
        before = content[: begin_match.start()]
        new_content = before.rstrip() + "\n\n" + template + "\n"
        status = "updated"
        message = f"replaced corrupted PM Lens section in {target_file}"
    else:
        # No markers — append
        separator = _separator_for(content)
        new_content = content + separator + template + "\n"
        status = "appended"
        message = f"appended PM Lens rules to {target_file}"

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


def _safe_inject(path: Path, host: str, *, dry_run: bool, force: bool = False) -> InjectResult:
    """Run ``_inject_into_file`` with a top-level exception guard.

    Per-host failures must not abort sibling hosts (ADR-008 design
    principle inherited from ADR-007 case C; cross-check D1).
    """
    try:
        return _inject_into_file(path, host, dry_run=dry_run, force=force)
    except PmServerError as e:
        # Deliberate refusal (e.g. the ADR-053 $HOME guard): surface the
        # reason verbatim instead of the generic "unexpected error" wording.
        return InjectResult(
            target_file=TARGET_FILES[host],
            host=host,
            status="failed",
            message=str(e),
            is_dry_run=dry_run,
        )
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
    force: bool = False,
) -> InjectSummary:
    """Inject PM Lens rules into per-host rule files.

    Args:
        project_root: Project root directory holding the rule files.
        target: One of :data:`RULE_TARGET_CHOICES`:

            * ``"auto"`` (default) — detect installed hosts via
              ``detect_hosts`` (filesystem + marker + CLAUDECODE).
            * ``"all"`` — process every known host unconditionally.
            * ``"existing"`` — only the rule files that already carry the
              pm-server marker. Never creates or appends to a file, which
              makes it the safe choice for bulk migration (ADR-055).
            * a single host id from :data:`pmlens.hosts.HOSTS` —
              ``"claude-code"`` writes ``CLAUDE.md``; ``"codex"``,
              ``"cursor"`` and ``"grok"`` each write ``AGENTS.md``, which
              all three read.

        dry_run: When True, no files are written or backed up; results
            still describe what *would* happen and per-result
            ``is_dry_run`` is True.
        force: Rewrite a section even when it is newer than this build's
            template (a deliberate downgrade). Off by default (ADR-055).

    Returns:
        ``InjectSummary`` with one result per rule FILE, not per host —
        hosts sharing a file are deduplicated and named in
        ``InjectResult.hosts`` (PMSERV-165). Failures are isolated so a
        write failure in one rule file does NOT abort the others
        (best-effort; ADR-008 + cross-check D1). The aggregate
        ``overall_status`` follows the priority order
        ``failed > skipped > updated > created``.

    Raises:
        ValueError: If ``target`` is not in :data:`RULE_TARGET_CHOICES`.
    """
    if target not in RULE_TARGET_CHOICES:
        raise ValueError(f"unknown target: {target!r}. Expected one of {RULE_TARGET_CHOICES}.")

    # Resolve target → list of hosts + detection source
    if target == "auto":
        hosts, source = detect_hosts(project_root)
    elif target == "all":
        hosts = list(TARGET_FILES.keys())
        source = "explicit"
    elif target == "existing":
        hosts = [
            h for h, rule_file in TARGET_FILES.items() if _has_pm_marker(project_root / rule_file)
        ]
        source = "existing"
    else:
        hosts = [target]
        source = "explicit"

    # Deduplicate by rule FILE, not by host (PMSERV-165). Three hosts read
    # AGENTS.md; iterating hosts would back up and rewrite that one file three
    # times and return three InjectResults each claiming a different host wrote
    # it — the second and third would also report "skipped, already current"
    # because the first had just written it. The first host (in `hosts` order,
    # which follows the HOSTS registry) owns the result; `InjectResult.hosts`
    # names every host that reads it.
    file_owners: dict[str, list[str]] = {}
    for host in hosts:
        file_owners.setdefault(TARGET_FILES[host], []).append(host)

    # Per-file injection (best-effort, isolated failures)
    results = [
        replace(
            _safe_inject(project_root / rule_file, owners[0], dry_run=dry_run, force=force),
            hosts=tuple(owners),
        )
        for rule_file, owners in file_owners.items()
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
