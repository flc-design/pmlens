---
name: pm
description: >
  プロジェクト管理 MCP Server (pm-server)。タスク追跡、進捗可視化、ブロッカー検知、
  ADR記録、全プロジェクト横断ダッシュボードを提供する。
  「進捗」「タスク」「ブロッカー」「次にやること」「ダッシュボード」
  「ADR」「意思決定」「PM」「プロジェクト状態」「作業開始」「作業完了」
  「リスク」「ベロシティ」のキーワードで必ずトリガーすること。
  作業セッション開始時と終了時にも自動的に状態を確認・更新すること。
---

# PM Server Skill (plugin-homed)

> This skill is the **plugin-homed** copy of pm-server's behavioural rules.
> The manually-registered MCP setup drives these rules via `CLAUDE.md`, but
> plugins cannot ship a `CLAUDE.md`. The session-start ritual is additionally
> injected deterministically by the plugin's `SessionStart` hook; this skill
> re-states the full rule set so the model can act on it mid-session.

## 概要

PM Server は Claude Code のプロジェクト管理を自動化する MCP Server。
各プロジェクトの `.pm/` ディレクトリに YAML でタスク・ADR・ログを管理し、
`~/.pm/registry.yaml` で全プロジェクトを横断的に俯瞰できる。

## 利用可能な MCP ツール（抜粋）

### プロジェクト管理
- `pm_init` — PM初期化（.pm/作成 + レジストリ自動登録 + 情報推定）
- `pm_status` — プロジェクト状態サマリ
- `pm_tasks` / `pm_add_task` / `pm_update_task` — タスク一覧・追加（ID自動採番）・更新
- `pm_next` — 次にやるべきタスクの推薦

### 記録・想起
- `pm_log` — 日次ログ記録
- `pm_add_decision` — ADR（Architecture Decision Record）追加
- `pm_remember` / `pm_recall` — セッションを跨ぐ記憶の保存・想起
- `pm_session_summary` — セッション要約（/clear 前に実行）

### 分析・可視化
- `pm_velocity` / `pm_risks` / `pm_blockers` — ベロシティ・リスク・ブロッカー
- `pm_dashboard` — HTMLダッシュボード生成（単体/横断）

### ワークフロー
- `pm_workflow_templates` / `pm_workflow_start` / `pm_workflow_advance` / `pm_workflow_status`

## 自動行動ルール

### セッション開始時（最初の発話の前に）
1. `pm_status` でカレントプロジェクトの状態を確認
2. `pm_next` で推薦タスクを3件提示
3. `pm_recall` で前回セッションの文脈を取得
4. ブロッカー・期限超過があれば警告

### タスクに取り掛かる前
1. 該当タスクを `pm_update_task` で `in_progress` に変更

### 作業中に重要な発見・判断があった時
1. `pm_remember` で記憶を保存（関連タスクIDがあれば `task_id` で紐付け）

### タスク完了時
1. `pm_update_task` で `done` に変更
2. `pm_log` に完了内容を記録
3. 次の推薦タスクを `pm_next` で提示
4. アトミックコミットを作成

### 設計上の意思決定が発生した時
1. `pm_add_decision` で ADR を記録するか確認
2. 承認されたら context / decision / consequences を構造化して保存

### MCP ツールの応答に warnings[] が含まれる場合
副作用（親タスク自動 revert 等）を意味するため、各 warning を要約して
ユーザーに明示し、`remediation` があれば次の選択肢として提示する。

## カレントプロジェクト検出

`project_path` を省略した場合、MCP Server が以下の順で自動検出：
1. 環境変数 `PM_PROJECT_PATH`
2. カレントディレクトリから上方向に `.pm/` を探索
3. 見つからない場合 → 「`pm_init` で初期化してください」と案内

## タスクの優先度 / ステータス
- 優先度: `P0`（ブロッカー級）→ `P1` → `P2` → `P3`
- ステータス: `todo` → `in_progress` → `review` → `done`（`blocked` は別線）
