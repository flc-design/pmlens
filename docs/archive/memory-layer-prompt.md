# Memory Layer 実装指示プロンプト

> このプロンプトを PM Lens プロジェクトの別セッションで使用してください。
> 設計書: `docs/memory-layer-design.md` を必ず先に読み込んでください。
>
> **Last updated**: 2026-04-13 (v2)

---

## 変更履歴

| Date | 変更内容 |
|------|---------|
| 2026-04-13 (v1) | 初版作成 |
| 2026-04-13 (v2) | Hook 戦略を CLAUDE.md ルールベースに統一（SessionStart Hook 非存在のため）。Phase 構造を設計書 5 Phase と整合。セッション ID 戦略追加。FTS5 日本語テキストノート追加。テスト件数更新（136 件）。モデルを Pydantic BaseModel に統一。`pm_recall` のデフォルト動作を明確化 |

---

## 指示プロンプト（コピー&ペースト用）

```
PM Lens に Memory Layer を追加実装してください。

## 背景

claude-mem (50K stars) の機能分析から、PM Lens の既存基盤（グローバル+per-project、CLAUDE.md 注入、レジストリ、CWD walk-up）を活かして、セッション記憶機能を追加します。
Agent SDK は使わず、Claude Code のコンテキスト内で完結する設計です。追加外部依存は不要（Python 標準 sqlite3 を使用）。

## 設計書

まず `docs/memory-layer-design.md` を読んでください。全体アーキテクチャ、SQLite スキーマ、ツール仕様、モジュール構成、実装フェーズが記載されています。

## 現在のコードベース状態

実装を始める前に、現在のコードベースを把握すること:

| 項目 | 現状 |
|------|------|
| バージョン | 0.3.0（`pyproject.toml`） |
| MCP ツール | 16 個（`server.py`） |
| テスト | 136 件全パス（`python -m pytest`） |
| モデル | Pydantic BaseModel × 12, StrEnum × 9（`models.py`） |
| ストレージ | YAML のみ（`storage.py`）— メモリ用 SQLite と混在させないこと |
| CLI | Click グループ — install, uninstall, serve, discover, status, migrate, update-claudemd |
| CLAUDE.md テンプレート | v1（`claudemd.py` の `TEMPLATE_VERSION`） |
| フォーマッター/リンター | ruff（`pyproject.toml` に設定） |

## セッション記憶のアーキテクチャ概要

### なぜ CLAUDE.md ルールベースか

Claude Code の Lifecycle Hooks には `SessionStart` イベントが **存在しない**。
利用可能な Hook イベントは `PreToolCall`, `PostToolCall`, `Notification`, `Stop`, `SubAgentStop` のみ。

したがって:
- セッション開始時の自動行動 → **CLAUDE.md ルール** でのみ実現可能
- セッション終了時の自動行動 → `Stop` Hook も使えるが、agent 停止後にツール呼び出しはできないため、**CLAUDE.md ルール** で Claude に事前に実行させる
- 作業中の記憶保存 → **CLAUDE.md ルール** で Claude に判断させる

```
セッション開始 → CLAUDE.md ルール → Claude が pm_recall + pm_status を呼ぶ
作業中        → CLAUDE.md ルール → Claude が pm_remember を呼ぶ（重要な発見時）
セッション終了 → CLAUDE.md ルール → Claude が pm_session_summary(save) を呼ぶ
```

> 設計書セクション 4 の Hook 設計は、将来 SessionStart Hook が追加された場合の
> アーキテクチャ。現時点では CLAUDE.md ルールが唯一の実現手段。
> `context.py` と `context-inject` CLI は将来の Hook 対応への備え（Phase 3）。

### セッション ID 戦略

MCP サーバープロセスの寿命 = Claude Code セッションの寿命。
サーバーモジュール読み込み時に `session_id` を一度だけ自動生成し、全メモリ操作で暗黙的に使用する:

```python
import uuid
from datetime import datetime

def generate_session_id() -> str:
    """Generate a session ID: sess-YYYYMMDD-HHMMSS-XXXXXX."""
    now = datetime.now().strftime("%Y%m%d-%H%M%S")
    suffix = uuid.uuid4().hex[:6]
    return f"sess-{now}-{suffix}"

# server.py モジュールレベルで一度だけ生成
_current_session_id: str = generate_session_id()
```

ユーザーにセッション ID を入力させない。`pm_remember` / `pm_session_summary` は内部で `_current_session_id` を使用する。

## 実装順序

### Phase 1: メモリ基盤 (P0)

1. `src/pmlens/models.py` に追加
   - `MemoryType` StrEnum（既存の StrEnum パターンに合わせる）:
     ```python
     class MemoryType(StrEnum):
         OBSERVATION = "observation"
         INSIGHT = "insight"
         LESSON = "lesson"
     ```
   - `Memory` Pydantic BaseModel:
     ```python
     class Memory(BaseModel):
         id: int | None = None
         session_id: str
         type: MemoryType = MemoryType.OBSERVATION
         content: str
         task_id: str | None = None
         decision_id: str | None = None
         tags: list[str] = Field(default_factory=list)
         created_at: str = ""
         project: str = ""
     ```
   - `SessionSummary` Pydantic BaseModel:
     ```python
     class SessionSummary(BaseModel):
         id: int | None = None
         session_id: str
         summary: str
         goals: str = ""
         tasks_done: list[str] = Field(default_factory=list)
         decisions: list[str] = Field(default_factory=list)
         pending: list[str] = Field(default_factory=list)
         created_at: str = ""
         project: str = ""
     ```

2. `src/pmlens/memory.py` を新規作成
   - `MemoryStore` クラス: SQLite 接続管理、スキーマ自動作成（FTS5 含む）
   - スキーマは `docs/memory-layer-design.md` セクション 2.3 に準拠
   - FTS5 仮想テーブル + INSERT/DELETE トリガーで自動同期
   - メソッド:
     - `save(memory: Memory) -> int` — 保存し自動採番 ID を返す
     - `search(query: str, type: str | None, limit: int) -> list[Memory]` — FTS5 全文検索
     - `get_by_task(task_id: str) -> list[Memory]` — タスク ID でフィルタ
     - `get_by_decision(decision_id: str) -> list[Memory]` — ADR ID でフィルタ
     - `get_recent(limit: int = 10) -> list[Memory]` — 新しい順に取得
     - `save_session_summary(summary: SessionSummary) -> int`
     - `get_latest_summary() -> SessionSummary | None`
     - `list_summaries(limit: int = 10) -> list[SessionSummary]`
   - DB パス: `.pm/memory.db`（per-project）
   - **tags 変換**: モデルでは `list[str]`、DB では `,` 区切り TEXT
     ```python
     def _tags_to_str(tags: list[str]) -> str:
         return ",".join(tags) if tags else ""

     def _str_to_tags(s: str) -> list[str]:
         return [t.strip() for t in s.split(",") if t.strip()] if s else []
     ```
   - **セキュリティ**: 全 SQL クエリでパラメータバインディング（`?` プレースホルダー）を使用

3. `src/pmlens/server.py` に 3 つの MCP ツールを追加

   **`pm_remember`**: 記憶の保存（task_id / decision_id 紐付け対応）
   ```python
   @mcp.tool()
   def pm_remember(
       content: str,                    # 記憶する内容
       type: str = "observation",       # observation | insight | lesson
       task_id: str | None = None,      # 紐付くタスク (e.g., MYAPP-003)
       decision_id: str | None = None,  # 紐付く ADR (e.g., ADR-002)
       tags: str | None = None,         # カンマ区切りタグ
       project_path: str | None = None,
   ) -> dict:
   ```
   - `_current_session_id` を内部で使用
   - `project` フィールドは `load_project()` から取得

   **`pm_recall`**: 記憶の検索・取得
   ```python
   @mcp.tool()
   def pm_recall(
       query: str | None = None,        # FTS5 検索クエリ
       task_id: str | None = None,      # タスクに紐づく記憶
       type: str | None = None,         # フィルタ: observation | insight | lesson
       limit: int = 5,                  # 最大件数
       cross_project: bool = False,     # 全プロジェクト横断検索
       project_path: str | None = None,
   ) -> list:
   ```
   - **引数なしの場合のデフォルト動作**: 最新セッション要約 + 直近の記憶を返す
     （セッション開始時に CLAUDE.md ルールで `pm_recall` が呼ばれる場面を想定）
   - `cross_project=True` は Phase 3 で実装。Phase 1 では未対応の旨を返す

   **`pm_session_summary`**: セッション要約の save/get/list
   ```python
   @mcp.tool()
   def pm_session_summary(
       action: str = "save",            # save | get | list
       summary: str | None = None,      # セッション要約テキスト
       goals: str | None = None,        # 達成した目標
       pending: str | None = None,      # 保留事項
       project_path: str | None = None,
   ) -> dict:
   ```
   - `save` 時: `_current_session_id` を使用。`summary` 必須
   - `get` 時: 直近の要約を 1 件返す
   - `list` 時: 要約一覧を返す

4. `tests/test_memory.py` を作成
   - `conftest.py` に `memory_store` フィクスチャ追加（`tmp_path` で一時 DB）
   - テストケース:
     - MemoryStore 初期化（スキーマ自動作成の確認）
     - save → search の往復テスト
     - FTS5 検索テスト（英語キーワード）
     - FTS5 検索テスト（日本語キーワード: 「認証」「リファクタ」等）
     - task_id / decision_id フィルタテスト
     - get_recent のソート順テスト
     - セッション要約 save/get/list テスト
     - 空 DB での graceful degradation テスト
     - tags の list ↔ str 変換テスト
   - **server ツール統合テスト**: pm_remember → pm_recall の往復

### Phase 2: セッション継続 (P0)

5. `src/pmlens/recall.py` を新規作成
   - `ContextBuilder` クラス:
     ```python
     class ContextBuilder:
         def __init__(self, memory_store: MemoryStore, pm_path: Path):
             ...

         def build_session_context(self, max_tokens: int = 2000) -> str:
             """Progressive Disclosure でコンテキストブロックを構築。"""
     ```
   - Progressive Disclosure（トークンバジェット配分）:
     - Layer 1: 最新セッション要約 (~200 tokens)
     - Layer 2: 進行中タスクの記憶 (~500 tokens)
     - Layer 3: 最近の判断 (~300 tokens)
     - Layer 4: 関連記憶 (~remaining tokens)
   - トークン推定: `len(text) // 2`（日本語 1 文字 ≈ 1.5-2 tokens で概算）
   - 各レイヤーが空なら次のレイヤーにバジェットを譲渡
   - 出力形式: Markdown セクション付きテキスト

6. `src/pmlens/claudemd.py` を更新
   - `TEMPLATE_VERSION` を `2` に更新
   - `CLAUDEMD_TEMPLATE` を拡張（Memory Layer ルール追加）:
     ```
     ### セッション開始時（最初の応答の前に必ず実行）
     1. pm_status を実行し、現在の進捗を表示する
     2. pm_next で推薦タスクを3件表示する
     3. pm_recall で前回セッションの文脈を取得する  ← NEW
     4. ブロッカーや期限超過があれば警告する

     ### 作業中に重要な発見・判断があった時  ← NEW セクション
     1. pm_remember で記憶を保存する（関連タスク ID があれば紐付け）

     ### コーディングセッション終了時
     1. 進行中のタスクの状態を確認し、必要に応じて更新する
     2. pm_log にセッションの成果を記録する
     3. pm_session_summary で要約を保存する  ← NEW
     4. 未コミットの変更があればコミットする
     ```
   - 既存の `ensure_claudemd()` / `update_claudemd()` ロジックは変更不要
     （`TEMPLATE_VERSION` と `CLAUDEMD_TEMPLATE` を更新するだけで自動置換される）

7. `tests/test_recall.py` を作成
   - ContextBuilder の各レイヤーテスト
   - トークンバジェット制限テスト（超過時の切り詰め）
   - 空データでの graceful degradation
   - `tests/test_claudemd.py` の既存テストが v2 テンプレートで全パスすることを確認

### Phase 3: 横断検索・自動化 (P1)

8. `src/pmlens/memory.py` に横断検索を追加
   - `sync_to_global(memory: Memory)` メソッド: per-project → `~/.pm/memory.db` への同期
   - グローバル DB スキーマ: `memory_index` + `memory_index_fts`（設計書セクション 2.3）
   - `search_global(query: str, ...) -> list[Memory]` メソッド
   - `save()` 呼び出し時に `sync_to_global()` を自動実行

9. `src/pmlens/server.py` に `pm_memory_search` ツールを追加
   - `pm_recall` より高度な検索: 日付範囲、複数タグ AND、ソート順
   - `cross_project=True` でグローバル DB を使用
   - `pm_recall` の `cross_project` パラメータも Phase 3 で有効化

10. `src/pmlens/context.py` を新規作成（将来の Hook 対応準備）
    - `inject_context(project_path) -> None`: stdout にコンテキストブロック出力
    - `ContextBuilder.build_session_context()` の結果を出力
    - 将来 `SessionStart` Hook が追加された場合に即座に接続可能

11. `src/pmlens/__main__.py` に CLI コマンド追加
    - `pmlens context-inject` コマンド（`context.py` を呼び出し）

12. `src/pmlens/installer.py` — CLAUDE.md ルール更新を install フローに統合
    - `install_mcp()` で CLAUDE.md 自動更新を案内
    - プログラマティック Hook は設定しない（ルールベースのため）

13. テスト追加: `tests/test_context.py`, server 統合テスト

### Phase 4: 運用ツール (P2, 後回し可)

14. `pm_memory_stats` — 記憶の統計情報（件数、タイプ別分布、期間別推移）
15. `pm_memory_cleanup` — 古い記憶の整理・アーカイブ（retention policy 付き）
16. ドキュメント更新: README.md, docs/design.md に Memory Layer セクション追加

## FTS5 と日本語テキストについて

SQLite FTS5 の `unicode61` トークナイザーは CJK 文字を 1 文字単位でトークン化する。
PM Lens のユースケース（作業ログ・セッション記憶の検索）では実用的に機能する:

- ✅ 「認証」で検索 → 「ユーザー認証 API 実装」がヒット（連続文字マッチ）
- ✅ 英語キーワードも正常に動作
- ⚠️ 形態素解析ではないため、動詞の活用形正規化はされない
- ⚠️ 同義語展開なし

将来的に `icu` トークナイザーへの移行も可能だが、現段階では不要。

## 重要な設計原則

- **既存コード変更最小限**: storage.py, velocity.py, dashboard.py, discovery.py, utils.py は変更しない
- **依存追加なし**: Python 標準 sqlite3 のみ使用。pyproject.toml の dependencies は変更しない
- **テストファースト**: 各新規モジュールに対応するテストを必ず作成
- **既存テスト保持**: 既存の 136 テストが全てパスすること（`python -m pytest` で確認）
- **YAML 層との分離**: 記憶は SQLite（memory.py）、タスク/ADR は YAML（storage.py）。混ぜない
- **段階的コミット**: Phase ごとにコミット。Phase 1 完了時点で動作確認
- **Pydantic 一貫性**: 新規モデルも BaseModel を使用。dataclass は使わない
- **セキュリティ**: SQL は必ずパラメータバインディング（? プレースホルダー）を使用
- **エラーハンドリング**: SQLite 操作エラーは PmServerError にラップして伝播
- **セッション ID 自動管理**: ユーザーにセッション ID を入力させない。サーバープロセスで自動生成

## コミットメッセージ規約

PM Lens のコミット規約に従い:
- Phase 1: `feat: Memory Layer 基盤 — SQLite ストア + pm_remember/pm_recall/pm_session_summary`
- Phase 2: `feat: セッション継続 — ContextBuilder + CLAUDE.md v2`
- Phase 3: `feat: 横断検索・自動化 — global sync + pm_memory_search + CLI`
- Phase 4: `feat: メモリ運用ツール — pm_memory_stats/pm_memory_cleanup`
```

---

## 補足: Synaptic Ledger との関係

PM Lens Memory Layer は **セッション記憶**（claude-mem 相当）を担当。
Synaptic Ledger は **知識品質管理 + LLM オーケストレーション**（独自領域）を担当。

両者は直交する問題を解くため、併用可能で相互に干渉しない:

```
PM Lens        = 「何をすべきか」+「何が起きたか」（プロジェクト管理 + セッション記憶）
Synaptic Ledger = 「何が正しいか」+「どう処理すべきか」（知識検証 + LLM 最適化）
```
