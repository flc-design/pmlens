# PM Server チートシート

> Claude Code + Codex CLI 用プロジェクト管理 MCP Server — **42 ツール**
> Version 0.3.3+ | Python 3.11+ | PyPI: `pm-server`

---

## クイックスタート

```bash
pip install pm-server
pm-server install          # Claude Code に MCP サーバーを登録
```

Claude Code セッション:
```
> PM初期化して              → pm_init
> 現在の進捗は？            → pm_status
> 次にやるべきことは？      → pm_next
```

---

## ツールリファレンス

### セットアップ・プロジェクト管理

| ツール | 説明 | 主要パラメータ |
|--------|------|----------------|
| `pm_init` | .pm/ ディレクトリ初期化、プロジェクト情報を自動検出 | `project_path?`, `project_name?` |
| `pm_status` | プロジェクト状況: フェーズ進捗、タスク数、ブロッカー | `project_path?` |
| `pm_list` | 登録済み全プロジェクト一覧 | _(なし)_ |
| `pm_discover` | プロジェクトをスキャンして自動登録 | `scan_path="."` |
| `pm_cleanup` | レジストリの健全性チェック、無効エントリ削除 | _(なし)_ |
| `pm_update_rules` | CLAUDE.md / AGENTS.md のルールを更新（multi-host、ADR-008） | `project_path?, target?, dry_run?` |
| `pm_update_claudemd` | レガシー alias: `pm_update_rules(target="claude-code")` (v0.6.0 以降 deprecated) | `project_path?` |
| `pm_dashboard` | HTML/テキスト ダッシュボード生成 | `format="html"` |

### タスク管理

| ツール | 説明 | 主要パラメータ |
|--------|------|----------------|
| `pm_add_task` | タスク作成（ID自動採番） | `title`, `phase`, `priority="P1"` |
| `pm_update_task` | タスクのステータス・フィールド更新 | `task_id`, `status?`, `priority?`, `notes?` |
| `pm_tasks` | タスク一覧（フィルタ可能） | `status?`, `phase?`, `priority?`, `tag?`, `parent_id?` |
| `pm_next` | 次に着手すべきタスクを推薦 | `count=3` |
| `pm_blockers` | ブロックされているタスク一覧 | `project_path?` |
| `pm_add_issue` | タスクに子イシュー（課題）を追加 | `parent_id`, `title`, `priority="P1"` |

### メモリ（セッション記憶）

| ツール | 説明 | 主要パラメータ |
|--------|------|----------------|
| `pm_remember` | 記憶を保存（作業中タスクに自動紐付け） | `content`, `type="observation"`, `tags?` |
| `pm_recall` | 記憶を想起 / 前回セッション取得 | `query?`, `task_id?`, `limit=5` |
| `pm_memory_search` | 詳細検索（複数フィルタ対応） | `query`, `type?`, `tags?`, `cross_project?` |
| `pm_memory_stats` | メモリDB統計情報 | `project_path?` |
| `pm_memory_cleanup` | 古い記憶の削除 | `older_than_days?`, `keep_latest?`, `dry_run=True` |
| `pm_session_summary` | セッション要約の保存/取得/一覧 | `action="save"`, `summary?` |

### 記録・分析

| ツール | 説明 | 主要パラメータ |
|--------|------|----------------|
| `pm_log` | デイリーログに記録（作業中タスクに自動紐付け） | `entry`, `category="progress"` |
| `pm_add_decision` | ADR（設計判断記録）を保存（ID自動採番） | `title`, `context`, `decision` |
| `pm_velocity` | ベロシティとトレンド分析 | `weeks=4` |
| `pm_risks` | 自動検出 + 手動登録リスク一覧 | `project_path?` |

### 知識レコード

| ツール | 説明 | 主要パラメータ |
|--------|------|----------------|
| `pm_record` | 構造化された知識を記録 | `category`, `title`, `findings?`, `confidence="medium"` |
| `pm_knowledge` | 知識レコードの検索・更新 | `action="list"`, `category?`, `status?`, `tag?` |

### ワークフロー

| ツール | 説明 | 主要パラメータ |
|--------|------|----------------|
| `pm_workflow_start` | テンプレートからワークフローを開始 | `feature`, `template="development"` |
| `pm_workflow_status` | ワークフロー進捗とガイダンスを取得 | `workflow_id?`（自動検出） |
| `pm_workflow_advance` | ステップを進める/ループ/スキップ | `proceed=True`, `artifacts?`, `skip?` |
| `pm_workflow_abandon` | ワークフローを放棄（履歴は保持） | `workflow_id?`, `notes?` |
| `pm_workflow_list` | ワークフローインスタンス一覧 | `status?` |
| `pm_workflow_templates` | 利用可能なテンプレート一覧 | `project_path?` |

---

## Claude Code での使い方

ユーザーは自然言語で指示するだけで、Claude が適切なツールを呼び出します。
複合的な指示にも対応し、複数ツールを自動で組み合わせて実行します。

### プロジェクト管理の基本

```
ユーザー: PM初期化して
Claude:   → pm_init → pm_status → pm_next を実行
          .pm/ ディレクトリ作成、CLAUDE.md にルール追加、現在の状態を表示

ユーザー: 現在の進捗を教えて
Claude:   → pm_status を実行
          フェーズ進捗、タスク数、ブロッカーを表示

ユーザー: 次は何をすべき？
Claude:   → pm_next を実行
          優先度・依存関係を考慮して推薦タスクを3件表示
```

### タスク管理

```
ユーザー: 「認証機能の追加」をタスク登録して。phase-1、P0で
Claude:   → pm_add_task(title="認証機能の追加", phase="phase-1", priority="P0")
          PROJ-001 を作成

ユーザー: PROJ-001 に着手します
Claude:   → pm_update_task(task_id="PROJ-001", status="in_progress")
          ステータスを in_progress に変更

ユーザー: 認証機能を実装完了。テストもパスした
Claude:   → pm_update_task(task_id="PROJ-001", status="done")
          → pm_log(entry="認証機能を実装完了、テストパス")
          → pm_next()
          タスク完了、ログ記録、次のタスクを推薦（複合実行）
```

### イシュー（課題）の発見

```
ユーザー: あ、JWTのトークン期限切れが未処理だった。イシューにして
Claude:   → pm_add_issue(parent_id="PROJ-001", title="JWTトークンの期限切れが未処理")
          PROJ-005 を作成、PROJ-001 は自動で review に戻る

ユーザー: PROJ-005 修正完了
Claude:   → pm_update_task(task_id="PROJ-005", status="done")
          全イシュー解消 → 「PROJ-001 を done にしますか？」と提案
```

### 3層ナレッジ管理

用途に応じて3つの記録レベルを使い分けます:

```
ユーザー: FastMCP v2 は Python 3.11+ が必須らしい。メモしておいて
Claude:   → pm_remember(content="FastMCP v2 は Python 3.11+ が必須", type="observation")
          【Layer 1: Memory】カジュアルな気づきメモ

ユーザー: JWT と セッション認証を比較調査した結果を記録して
Claude:   → pm_record(category="tradeoff", title="JWT vs セッション認証",
              findings="JWT: ステートレスだがペイロードが大きい...",
              conclusion="API は JWT、Web はセッション",
              confidence="high")
          【Layer 2: Knowledge Record】構造化された調査結果

ユーザー: API認証にJWTを採用する方針で。ADRとして記録しますか？
Claude:   → pm_add_decision(title="API認証に JWT を採用",
              context="マイクロサービスにステートレス認証が必要",
              decision="JWT + RS256、有効期限15分")
          【Layer 3: ADR】フォーマルな設計判断記録
```

### 知識レコードのカテゴリ

| カテゴリ | 用途 | 使い方例 |
|----------|------|----------|
| `research` | 一般的な調査結果 | 「○○について調べた結果をまとめて」 |
| `market` | 市場分析、競合調査 | 「競合の認証方式を調査した結果を記録」 |
| `spike` | 技術スパイク / プロトタイプ結果 | 「FastMCPのスパイク結果を記録して」 |
| `requirement` | 要件定義 | 「認証機能の要件をまとめて」 |
| `constraint` | 技術的・ビジネス上の制約 | 「Python 3.11以上の制約を記録」 |
| `tradeoff` | トレードオフ分析（A vs B） | 「SQLとNoSQLの比較結果を記録」 |
| `risk_analysis` | リスク評価結果 | 「JWTの脆弱性リスクを記録」 |
| `spec` | 機能仕様 | 「認証APIの仕様を記録して」 |
| `api_design` | API 設計ドキュメント | 「エンドポイント設計を記録」 |

### 知識レコードの更新

```
ユーザー: KR-001 の調査を検証したので validated にして。信頼度は high に
Claude:   → pm_knowledge(action="update", record_id="KR-001",
              new_status="validated", confidence="high")

ユーザー: KR-001 は新しい調査で上書きされた。superseded にして
Claude:   → pm_knowledge(action="update", record_id="KR-001",
              new_status="superseded")

ユーザー: リサーチ系のナレッジを全部見せて
Claude:   → pm_knowledge(action="list", category="research")
          research カテゴリの全レコードを表示

ユーザー: ナレッジの概要を教えて
Claude:   → pm_knowledge(action="summary")
          カテゴリ別・ステータス別のサマリーを表示
```

---

## ワークフロー

### 組み込みテンプレート

#### Discovery（5ステップ、完了後 Development に連鎖）

```
research ──→ fact_check ──→ proposal ──→ cross_check ──→ confirm
   ↑              ↑             ↑
   └──── brainstorm loop ───────┘
         (proceed=false でループ)
```

- **research**: トピックを調査（ループ対象）
- **fact_check**: 調査結果を検証（ループ対象）
- **proposal**: ユーザーに提案（ループ対象、ゲート: user_approval）
- **cross_check**: 独立した妥当性検証
- **confirm**: 方向性を確定、ADR 記録（ゲート: user_approval）

#### Development（9ステップ）

```
decision → tasks → spec → plan → check → implement → test → quality → issues
                                   ↑                          ↑
                              ゲート: user_approval      ゲート: user_approval
```

- **decision**: ADR を記録
- **tasks**: タスクに分解
- **spec**: 仕様書を作成
- **plan**: 実装プランを設計
- **check**: クロスチェック（ゲート: user_approval）
- **implement**: コードを実装
- **test**: テストの作成・実行
- **quality**: 最終品質レビュー（ゲート: user_approval）
- **issues**: 残課題の登録（オプション）

### ワークフローの使い方

```
ユーザー: ユーザー認証機能について調査から始めたい
Claude:   → pm_workflow_start(feature="ユーザー認証", template="discovery")
          WF-001 開始。最初のステップ「research」のガイダンスを表示

ユーザー: 調査完了。次のステップへ
Claude:   → pm_workflow_advance(artifacts=["KR-001"])
          research 完了、次: fact_check

ユーザー: まだ調べ足りない。もう一周したい
Claude:   → pm_workflow_advance(proceed=False)
          brainstorm ループの先頭に戻る（イテレーション 2）

ユーザー: OK、今回の調査で十分。次へ進めて
Claude:   → pm_workflow_advance() を繰り返し実行
          research → fact_check → proposal（ここでユーザー承認を求める）

ユーザー: その方針でOK
Claude:   → pm_workflow_advance()  # proposal のゲート通過
          → cross_check → confirm まで進行
          「Discovery 完了。Development ワークフローを開始しますか？」

ユーザー: はい、お願いします
Claude:   → pm_workflow_start(feature="ユーザー認証", template="development")
          WF-002 開始（Discovery → Development の連鎖）

ユーザー: 今のワークフローの進捗は？
Claude:   → pm_workflow_status()
          進捗: 3/9、現在のステップ: plan、知識レコード: 5件

ユーザー: このステップはスキップして
Claude:   → pm_workflow_advance(skip=True)
          ステップをスキップ、次のステップへ

ユーザー: どんなワークフローテンプレートがある？
Claude:   → pm_workflow_templates()
          brainstorming (8 steps, builtin), discovery (5 steps, builtin),
          development (9 steps, builtin), super-research (6 steps, builtin)
```

### カスタムテンプレート

`.pm/workflow_templates/` に YAML ファイルを配置すると、
組み込みテンプレートの上書きや独自テンプレートの追加が可能:

```yaml
# .pm/workflow_templates/my-workflow.yaml
name: 独自ワークフロー
description: チーム専用のワークフロー
chain_to: development  # 省略可

steps:
  - id: research
    name: 調査
    tool_hint: pm_record
    loop: true
    loop_group: investigate

  - id: review
    name: レビュー
    gate: user_approval

  - id: implement
    name: 実装
    skill_hint: プランモードを使用
    optional: false
```

---

## セッションのライフサイクル

### セッション開始（CLAUDE.md ルールにより自動実行）

```
Claude:   → pm_status()       現在の状態を確認
          → pm_next()         推薦タスクを表示
          → pm_recall()       前回セッションの文脈を復元
          → ブロッカーがあれば警告
```

### 作業中

```
ユーザー: PROJ-003 に取り掛かります
Claude:   → pm_update_task(task_id="PROJ-003", status="in_progress")

ユーザー: （コードを書いて実装中...）
Claude:   重要な発見があれば → pm_remember() で自動保存
          知識を記録すべき時 → pm_record() で自動記録
          ワークフロー進行中 → pm_workflow_advance() でステップ更新
```

### セッション終了

```
ユーザー: 今日はここまでにしよう
Claude:   → pm_update_task() で進行中タスクの状態確認
          → pm_log(entry="セッションの成果をまとめ")
          → pm_session_summary(action="save", summary="...")
          → 未コミットの変更があればコミット提案
```

### コンテキスト圧縮後の復元

```
（Claude Code がコンテキストを自動圧縮した後）
Claude:   → pm_recall()       最新の記憶とセッション要約を復元
          → pm_workflow_status()  ワークフローの現在地を確認
          作業を継続
```

### ソース編集後の再起動 (editable install のみ)

`pip install -e .` で pm-server を入れた状態でセッション中にソースを
編集した場合、MCP ホスト (Claude Code または Codex CLI) を再起動して
パッケージを再読込みすること。Python は長期プロセス内でモジュールを
キャッシュするため、**遅延 import** されるモジュール (例: `pm_server.rules`)
は初回 import 時に古いキャッシュを参照し、他モジュールがディスク上で
新しくても整合性が崩れる。

`pm_status()` は fingerprint を返すので状況が一目でわかる:

```python
pm_status()["diagnostics"]["utils_fingerprint"]
# → {
#     "loaded":  "a1b2c3d4",     # 実行中プロセスがロードしたバイト
#     "current": "a1b2c3d4",     # 現在ディスク上のバイト
#     "stale":   False,           # True なら MCP ホスト再起動が必要
#     "path":    "/.../utils.py",
#   }
```

`stale: true` のときはディスクとメモリが乖離しているサイン。サーバを
再起動する。Wheel install (`pip install pm-server` from PyPI) では
ソースが immutable なので発生しない (次回 `pip install -U` まで)。
原因の詳細は PMSERV-060 参照。

---

## CLI コマンド

```bash
pm-server install              # MCP サーバー登録
pm-server uninstall            # MCP サーバー削除
pm-server serve                # MCP サーバー起動（stdio）
pm-server status               # プロジェクト状況表示
pm-server discover [path]      # プロジェクト検出・登録
pm-server update-rules         # CLAUDE.md / AGENTS.md ルール更新（multi-host）
pm-server update-rules -t auto --dry-run  # 検知された host をプレビュー
pm-server update-rules --all   # 登録された全プロジェクトに適用
pm-server update-claudemd      # レガシー: update-rules -t claude-code と等価
pm-server hook post-tool-use   # PostToolUse フックハンドラ
```

---

## データ保存先

```
.pm/                            # プロジェクトごと
├── project.yaml                # プロジェクトメタデータ
├── tasks.yaml                  # 全タスク
├── decisions.yaml              # ADR（設計判断記録）
├── knowledge.yaml              # 知識レコード
├── workflows.yaml              # ワークフローインスタンス
├── risks.yaml                  # 手動リスク
├── milestones.yaml             # マイルストーン
├── memory.db                   # SQLite + FTS5 メモリ
├── daily/                      # デイリーログ
│   └── 2026-04-16.yaml
└── workflow_templates/         # カスタムテンプレート（任意）
    └── my-workflow.yaml

~/.pm/                          # グローバル
├── registry.yaml               # プロジェクトレジストリ
└── memory.db                   # 横断メモリインデックス
```

---

## Enum リファレンス

| 型 | 値 |
|----|----|
| TaskStatus | `todo`, `in_progress`, `review`, `done`, `blocked` |
| Priority | `P0`（最重要）, `P1`（重要）, `P2`（あれば良い）, `P3`（いつか） |
| DecisionStatus | `proposed`, `accepted`, `deprecated`, `superseded` |
| LogCategory | `progress`, `decision`, `blocker`, `note`, `milestone` |
| MemoryType | `observation`, `insight`, `lesson` |
| KnowledgeCategory | `research`, `market`, `spike`, `requirement`, `constraint`, `tradeoff`, `risk_analysis`, `spec`, `api_design` |
| KnowledgeStatus | `draft`（下書き）, `validated`（検証済）, `superseded`（置換済） |
| ConfidenceLevel | `high`（高）, `medium`（中）, `low`（低） |
| WorkflowStepStatus | `pending`, `active`, `done`, `skipped` |
| WorkflowStatus | `active`, `completed`, `paused`, `abandoned` |
