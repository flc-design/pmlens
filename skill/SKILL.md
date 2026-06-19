---
name: pm-server
description: >
  プロジェクト管理 MCP Server。タスク追跡、進捗可視化、ブロッカー検知、
  ADR記録、全プロジェクト横断ダッシュボードを提供する。
  「進捗」「タスク」「ブロッカー」「次にやること」「ダッシュボード」
  「ADR」「意思決定」「PM」「プロジェクト状態」「作業開始」「作業完了」
  「リスク」「ベロシティ」のキーワードで必ずトリガーすること。
  作業セッション開始時と終了時にも自動的に状態を確認・更新すること。
---

# PM Lens Skill

## 概要

PM Lens は Claude Code のプロジェクト管理を自動化する MCP Server。
各プロジェクトの `.pm/` ディレクトリにYAMLでタスク・ADR・ログを管理し、
`~/.pm/registry.yaml` で全プロジェクトを横断的に俯瞰できる。

## 利用可能な MCP ツール

### プロジェクト管理
- `pm_init` — PM初期化（.pm/作成 + レジストリ自動登録 + 情報推定）
- `pm_status` — プロジェクト状態サマリ
- `pm_tasks` — タスク一覧（フィルタ付き）
- `pm_add_task` — タスク追加（ID自動採番）
- `pm_update_task` — タスク更新
- `pm_next` — 次にやるべきタスクの推薦

### 記録
- `pm_log` — 日次ログ記録
- `pm_add_decision` — ADR（Architecture Decision Record）追加

### 分析
- `pm_velocity` — ベロシティ計算（週次トレンド付き）
- `pm_risks` — リスク・ブロッカー一覧
- `pm_blockers` — blocked タスク一覧

### 可視化
- `pm_dashboard` — HTMLダッシュボード生成（単体/横断）

### ディスカバリー
- `pm_discover` — 既存プロジェクトの一括検出・登録
- `pm_cleanup` — 無効なレジストリエントリの除去
- `pm_list` — 登録プロジェクト一覧

## 自動行動ルール

### セッション開始時（最初の発話の前に）

1. `pm_status` でカレントプロジェクトの状態を確認
2. `pm_next` で推薦タスクを3件提示
3. ブロッカーがあれば警告表示
4. 期限超過タスクがあれば注意喚起

### タスクに取り掛かる前

1. 該当タスクを `pm_update_task` で `in_progress` に変更
2. `depends_on` の完了状態を確認

### タスク完了時

1. `pm_update_task` で `done` に変更
2. `pm_log` に完了内容を記録
3. `acceptance_criteria` があれば充足確認を提案
4. 次の推薦タスクを提示

### 設計上の意思決定が発生した時

1. `pm_add_decision` で ADR を記録するか確認
2. 承認されたら context / decision / consequences を構造化して保存

### ブロッカー発見時

1. タスクを `blocked` に変更
2. `blocked_by` に原因タスクを記録
3. `pm_risks` に追加

## カレントプロジェクト検出

`project_path` を省略した場合、MCP Server が以下の順で自動検出：

1. 環境変数 `PM_PROJECT_PATH`
2. カレントディレクトリから上方向に `.pm/` を探索
3. 見つからない場合 → 「`pm_init` で初期化してください」と案内

## データ構造

各プロジェクトの `.pm/` ディレクトリ：

```
.pm/
├── project.yaml    # プロジェクトメタ情報
├── tasks.yaml      # タスク一覧・ステータス
├── decisions.yaml  # ADR
├── milestones.yaml # マイルストーン
├── risks.yaml      # リスク・課題
└── daily/
    └── YYYY-MM-DD.yaml  # 日次ログ
```

## タスクの優先度

- `P0` — 必須（ブロッカー級）
- `P1` — 重要（今のフェーズで必要）
- `P2` — 改善（あると良い）
- `P3` — 後回し（いつかやる）

## タスクのステータス

- `todo` → `in_progress` → `review` → `done`
- `todo` → `blocked`（ブロッカー発生時）
- `blocked` → `in_progress`（ブロッカー解消時）
