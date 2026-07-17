# PM Lens Memory Layer 設計書

**Version**: 0.4.0
**Date**: 2026-04-13
**Author**: Shinichi Nakazato / FLC design Co., Ltd.
**Status**: Implemented (実装完了)
**Based on**: claude-mem (50K stars) の機能分析 + PM Lens 既存基盤の活用

> **注**: 本書は v0.4.0 実装時点の設計スナップショット。`session_summaries` はその後
> `updated_at` / `branch` 列・ミリ秒精度タイムスタンプ・recency 式 index が追加されている
> (PMSERV-158〜162)。現行スキーマと recency 意味論（「最新 = 最後に作業した行」）は
> `docs/design.md` の ADR-028/042/043 節と CHANGELOG を参照。

---

## 変更履歴

| Version | Date | 変更内容 |
|---|---|---|
| 0.4.0 | 2026-04-13 | Memory Layer 設計初版。claude-mem 比較分析に基づく |
| 0.4.0 | 2026-04-13 | 全Phase実装完了。22 MCP tools, 227 tests |

---

## 1. 背景と動機

### 1.1 claude-mem の分析

[claude-mem](https://github.com/thedotmack/claude-mem) (50K+ stars) は Claude Code の自動セッション記憶ツール:

- **7つの Lifecycle Hooks** で全ツール使用を自動キャプチャ
- **Claude Agent SDK** で観測データをAI圧縮
- **SessionStart時** に関連コンテキストを自動注入
- **SQLite + ChromaDB** でセマンティック検索
- **Web UI** でセッション閲覧

#### claude-mem の弱点 (209 open issues より)

| 問題 | Issue |
|------|-------|
| メモリリーク | #532 |
| 観測データ重複（重複検出なし） | #1534 |
| APIコスト暴走（Agent SDK圧縮） | #1693 |
| 孤立セッション | #514 |
| Bun依存でNode.js環境クラッシュ | #1645 |
| Windows互換性問題 | 複数 |
| AGPL-3.0 ライセンス（商用制約） | - |

### 1.2 PM Lens が既に持つ基盤

claude-mem がゼロから構築した以下のインフラを PM Lens は既に持っている:

```
✅ グローバル + per-project パターン（~/.pm/ + .pm/）
✅ CLAUDE.md マーカーインジェクション（バージョン付き）
✅ プロジェクト自動検出（Cargo.toml, package.json, pyproject.toml, git）
✅ CWD walk-up パス解決
✅ グローバルレジストリ（registry.yaml）
✅ セッション開始/終了の自動行動ルール（CLAUDE.md）
✅ 日次ログ（daily/YYYY-MM-DD.yaml）
✅ MCP stdio transport (FastMCP 2.0)
✅ ゼロコンフィグインストール（pip install + pmlens install）
✅ MIT ライセンス
```

### 1.3 統合の優位性

| 優位性 | 説明 |
|--------|------|
| **構造 × 記憶** | タスク/ADRに紐づいた記憶。「どのタスク作業中の記憶か」が分かる |
| **コスト0** | Agent SDK 不要。Claude Code のコンテキスト内で要約生成 |
| **シンプル** | Bun不要、Worker不要、ChromaDB不要。Python + SQLite |
| **MIT** | AGPL-3.0 の claude-mem と違い商用利用自由 |
| **基盤済み** | 新規インフラ構築なし。機能追加のみ |

---

## 2. アーキテクチャ

### 2.1 全体構成

```
┌──────────────────────────────────────────────────────────┐
│                    PM Lens v0.4.0                        │
│                                                            │
│  ┌──────────────────────┐  ┌───────────────────────────┐  │
│  │  Project Layer        │  │  Memory Layer (NEW)        │  │
│  │  (既存・変更なし)      │  │                            │  │
│  │  ├─ Tasks (YAML)      │  │  ├─ Observations (SQLite) │  │
│  │  ├─ ADR (YAML)        │  │  ├─ Session Summaries     │  │
│  │  ├─ Daily Log (YAML)  │  │  ├─ FTS5 Search           │  │
│  │  ├─ Velocity          │  │  ├─ Context Injection     │  │
│  │  ├─ Risks             │  │  └─ Cross-project Recall  │  │
│  │  └─ Dashboard         │  │                            │  │
│  └──────────────────────┘  └───────────────────────────┘  │
│                                                            │
│  Storage:                                                  │
│    .pm/project.yaml, tasks.yaml, ...  (既存 YAML)          │
│    .pm/memory.db                      (新規 SQLite)        │
│    ~/.pm/memory.db                    (横断検索用)          │
└──────────────────────────────────────────────────────────┘
```

### 2.2 ストレージ戦略

**ハイブリッドアプローチ:**

| データ | 形式 | 理由 |
|--------|------|------|
| タスク/ADR/ログ | YAML（既存） | Git-friendly、手編集可能、変更不要 |
| セッション記憶 | SQLite | FTS5検索、大量データ対応、軽量 |
| 横断検索インデックス | SQLite (global) | 全プロジェクトの記憶を一元検索 |

### 2.3 SQLite スキーマ

```sql
-- .pm/memory.db (per-project)

CREATE TABLE memories (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT NOT NULL,           -- セッション識別子
    type        TEXT NOT NULL,           -- observation | summary | insight
    content     TEXT NOT NULL,           -- 記憶内容
    task_id     TEXT,                    -- 紐付くタスクID (e.g., MYAPP-003)
    decision_id TEXT,                    -- 紐付くADR ID (e.g., ADR-002)
    tags        TEXT,                    -- カンマ区切りタグ
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    project     TEXT NOT NULL            -- プロジェクト名
);

CREATE TABLE session_summaries (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT NOT NULL UNIQUE,
    summary     TEXT NOT NULL,           -- セッション要約
    goals       TEXT,                    -- 達成した目標
    tasks_done  TEXT,                    -- 完了タスクIDs (JSON array)
    decisions   TEXT,                    -- 下した判断 (JSON array)
    pending     TEXT,                    -- 保留事項 (JSON array)
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    project     TEXT NOT NULL
);

-- FTS5 全文検索インデックス
CREATE VIRTUAL TABLE memories_fts USING fts5(
    content,
    tags,
    content=memories,
    content_rowid=id,
    tokenize='unicode61'
);

-- トリガーでFTS自動同期
CREATE TRIGGER memories_ai AFTER INSERT ON memories BEGIN
    INSERT INTO memories_fts(rowid, content, tags)
    VALUES (new.id, new.content, new.tags);
END;

CREATE TRIGGER memories_ad AFTER DELETE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, content, tags)
    VALUES ('delete', old.id, old.content, old.tags);
END;
```

```sql
-- ~/.pm/memory.db (global cross-project index)

CREATE TABLE memory_index (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    project     TEXT NOT NULL,           -- プロジェクト名
    project_path TEXT NOT NULL,          -- プロジェクトパス
    memory_id   INTEGER NOT NULL,        -- per-project memory.db の ID
    type        TEXT NOT NULL,
    content     TEXT NOT NULL,
    tags        TEXT,
    task_id     TEXT,
    created_at  TEXT NOT NULL
);

CREATE VIRTUAL TABLE memory_index_fts USING fts5(
    content,
    tags,
    project,
    content=memory_index,
    content_rowid=id,
    tokenize='unicode61'
);
```

---

## 3. 新規 MCP ツール

### 3.1 ツール一覧

| ツール | 説明 | 優先度 |
|--------|------|--------|
| `pm_remember` | セッション記憶を保存（タスク/ADR紐付け可） | P0 |
| `pm_recall` | セマンティック検索で記憶を取得 | P0 |
| `pm_session_summary` | セッション要約の保存/取得 | P0 |
| `pm_memory_search` | 全文検索 + フィルタで記憶を検索 | P1 |
| `pm_memory_stats` | 記憶の統計情報 | P2 |
| `pm_memory_cleanup` | 古い記憶の整理/アーカイブ | P2 |

### 3.2 ツール詳細

#### `pm_remember`

```python
@mcp.tool()
async def pm_remember(
    content: str,                    # 記憶する内容
    type: str = "observation",       # observation | insight | lesson
    task_id: str | None = None,      # 紐付くタスク (e.g., MYAPP-003)
    decision_id: str | None = None,  # 紐付くADR (e.g., ADR-002)
    tags: str | None = None,         # カンマ区切りタグ
    project_path: str | None = None,
) -> str:
    """Save a memory tied to the current session context.

    Memories are searchable and persist across sessions.
    Link to task_id or decision_id for structured context.
    """
```

#### `pm_recall`

```python
@mcp.tool()
async def pm_recall(
    query: str | None = None,        # 検索クエリ（FTS5）
    task_id: str | None = None,      # タスクに紐づく記憶
    type: str | None = None,         # フィルタ: observation | insight | lesson
    limit: int = 5,                  # 最大件数
    cross_project: bool = False,     # 全プロジェクト横断検索
    project_path: str | None = None,
) -> str:
    """Recall memories relevant to the current context.

    Searches by full-text query, task association, or type.
    Use cross_project=true to search across all projects.
    """
```

#### `pm_session_summary`

```python
@mcp.tool()
async def pm_session_summary(
    action: str = "save",            # save | get | list
    summary: str | None = None,      # セッション要約テキスト
    goals: str | None = None,        # 達成した目標
    pending: str | None = None,      # 保留事項
    project_path: str | None = None,
) -> str:
    """Manage session summaries for cross-session continuity.

    - save: Store a summary for the current session
    - get: Retrieve the most recent session summary
    - list: Show all session summaries
    """
```

---

## 4. Lifecycle Hooks 統合

### 4.1 Hook 設計

Claude Code の Lifecycle Hooks を活用してセッション記憶を自動化:

```json
{
  "hooks": {
    "SessionStart": [
      {
        "type": "command",
        "command": "pmlens context-inject",
        "description": "Inject relevant memories into session context"
      }
    ],
    "Stop": [
      {
        "type": "command",
        "command": "pmlens session-end-hook",
        "description": "Trigger session summary generation"
      }
    ]
  }
}
```

### 4.2 コンテキスト注入フロー

```
SessionStart
    │
    ▼
pmlens context-inject
    │
    ├─ 最新セッション要約を取得
    ├─ 進行中タスクに紐づく記憶を取得
    ├─ 関連する ADR/判断を取得
    ├─ トークンバジェット内で Progressive Disclosure
    │
    ▼
stdout → Claude Code に注入
    (前回の文脈 + 関連記憶 + 保留事項)
```

### 4.3 セッション終了フロー

```
Stop Hook
    │
    ▼
Claude Code が自動的に pm_session_summary(action="save") を呼び出し
    （CLAUDE.md の自動行動ルールで指示）
    │
    ├─ セッション中の作業を要約
    ├─ 完了タスク一覧
    ├─ 保留事項
    ├─ 次セッションへの申し送り
    │
    ▼
.pm/memory.db に保存
```

**重要**: Agent SDK を使わない。Claude Code のコンテキスト内で Claude 自身が要約を生成し、`pm_session_summary` で保存する。追加APIコスト = 0。

---

## 5. CLAUDE.md ルール拡張

既存の自動行動ルールに Memory Layer 用ルールを追加:

```markdown
### セッション開始時（最初の応答の前に必ず実行）
1. pm_status を実行し、現在の進捗を表示する
2. pm_next で推薦タスクを3件表示する
3. **pm_recall で前回セッションの文脈を取得する** ← NEW
4. ブロッカーや期限超過があれば警告する

### 作業中に重要な発見・判断があった時 ← NEW
1. pm_remember で記憶を保存する（関連タスクIDがあれば紐付け）

### コーディングセッション終了時
1. 進行中のタスクの状態を確認し、必要に応じて更新する
2. pm_log にセッションの成果を記録する
3. **pm_session_summary で要約を保存する** ← NEW
4. 未コミットの変更があればコミットする
```

---

## 6. 新規モジュール構成

```
src/pmlens/
├── __main__.py          # CLI に context-inject, session-end-hook 追加
├── server.py            # 新規ツール 6個追加
├── models.py            # Memory, SessionSummary モデル追加
├── storage.py           # 変更なし（YAML層）
├── memory.py            # ★ NEW: SQLite記憶ストレージ
├── recall.py            # ★ NEW: FTS5検索 + コンテキスト構築
├── context.py           # ★ NEW: セッション注入ロジック
├── discovery.py         # 変更なし
├── installer.py         # Hook 設定の自動登録を追加
├── claudemd.py          # TEMPLATE_VERSION 2 に更新、Memory ルール追加
├── velocity.py          # 変更なし
├── dashboard.py         # 変更なし
└── utils.py             # 変更なし
```

### 6.1 memory.py (新規)

```python
"""SQLite-based memory storage for PM Lens.

Per-project memory in .pm/memory.db.
Global cross-project index in ~/.pm/memory.db.
FTS5 full-text search support.
"""

import sqlite3
from pathlib import Path
from dataclasses import dataclass

@dataclass
class Memory:
    id: int | None
    session_id: str
    type: str           # observation | insight | lesson
    content: str
    task_id: str | None
    decision_id: str | None
    tags: str | None
    created_at: str
    project: str

class MemoryStore:
    """SQLite memory store with FTS5 search."""

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._ensure_schema()

    def _ensure_schema(self): ...
    def save(self, memory: Memory) -> int: ...
    def search(self, query: str, ...) -> list[Memory]: ...
    def get_by_task(self, task_id: str) -> list[Memory]: ...
    def get_session_summary(self, session_id: str) -> ...: ...
    def save_session_summary(self, ...) -> int: ...
    def sync_to_global(self, memory: Memory): ...
```

### 6.2 recall.py (新規)

```python
"""Memory recall and context building.

Builds contextual memory blocks for session injection.
Supports progressive disclosure for token efficiency.
"""

class ContextBuilder:
    """Build context injection from memories + project state."""

    def build_session_context(
        self,
        project_path: Path,
        max_tokens: int = 2000,
    ) -> str:
        """Generate context block for session start.

        Layers (progressive disclosure):
        1. Last session summary (~200 tokens)
        2. In-progress task memories (~500 tokens)
        3. Recent decisions (~300 tokens)
        4. Related memories by recency (~remaining tokens)
        """
```

### 6.3 context.py (新規)

```python
"""Session context injection via CLI hook.

Called by SessionStart hook to inject context into Claude Code.
"""

def inject_context(project_path: Path | None = None) -> None:
    """Print context block to stdout for Claude Code injection."""
```

---

## 7. 依存関係の変更

```toml
# pyproject.toml - 追加依存なし!
# Python 3.11+ の標準ライブラリ sqlite3 を使用
# 追加パッケージ不要
```

**重要**: SQLite は Python 標準ライブラリに含まれるため、新しい依存関係は一切不要。

---

## 8. 実装フェーズ

### Phase 1: 基盤 (P0)

1. `memory.py` — SQLite ストア + FTS5 スキーマ
2. `pm_remember` ツール — 記憶の保存
3. `pm_recall` ツール — FTS5 検索
4. テスト — MemoryStore の単体テスト

### Phase 2: セッション継続 (P0)

5. `pm_session_summary` ツール — 要約の保存/取得
6. `recall.py` — ContextBuilder
7. `claudemd.py` — TEMPLATE_VERSION 2、Memory ルール追加
8. テスト — セッション要約 + コンテキスト構築

### Phase 3: 自動化 (P1)

9. `context.py` — CLI hook 用コンテキスト注入
10. `installer.py` — Hook 設定の自動登録
11. `__main__.py` — CLI コマンド追加
12. テスト — E2E フロー

### Phase 4: 横断検索 (P1)

13. グローバル memory.db — 横断インデックス
14. `pm_memory_search` ツール — 高度な検索
15. `sync_to_global()` — 保存時の自動同期
16. テスト — 横断検索

### Phase 5: 運用 (P2)

17. `pm_memory_stats` ツール
18. `pm_memory_cleanup` ツール
19. ドキュメント更新

---

## 9. claude-mem との比較（実装後）

| 機能 | claude-mem | PM Lens v0.4 |
|------|-----------|----------------|
| 自動キャプチャ | Agent SDK (有料) | CLAUDE.md ルール (無料) |
| セッション要約 | AI圧縮 | Claude 自身が生成 |
| 検索 | FTS5 + ChromaDB | FTS5 |
| コンテキスト注入 | SessionStart Hook | SessionStart Hook |
| タスク紐付け | なし | **あり (task_id)** |
| ADR紐付け | なし | **あり (decision_id)** |
| 横断検索 | 単一プロジェクト | **全プロジェクト** |
| 重複検出 | なし (Issue #1534) | **FTS5で類似検出** |
| Web UI | あり | なし (Dashboard に統合予定) |
| 依存 | Bun + Agent SDK + ChromaDB | **Python標準ライブラリのみ** |
| ライセンス | AGPL-3.0 | **MIT** |
| コスト | Agent SDK API費用 | **0** |

---

## 10. Synaptic Ledger との棲み分け

```
PM Lens v0.4.0       = プロジェクト管理 + セッション記憶
                        「何をすべきか」+「何が起きたか」

Synaptic Ledger      = 知識品質管理 + LLMオーケストレーション
                        「何が正しいか」+「どう処理すべきか」
```

両プロジェクトは直交する問題を解く。併用可能で相互に干渉しない。
