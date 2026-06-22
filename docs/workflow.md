# Claude Desktop × Claude Code 開発ワークフロー

**Version**: 2.1.0
**Date**: 2026-04-08
**Author**: Shinichi Nakazato / FLC design Co., Ltd.

---

## 1. 概要

Claude Code を中心に、PM Lens で自律的な開発を構造化するワークフロー。
Claude Desktop（claude.ai）は企画・ブレストなどコードに縛られない思考が必要な場面で使う。

### 役割の変遷

```
v1.0 (2025): Desktop = 設計者、Code = 実装者（固定分離）
v2.0 (2026): Code = 設計 + 実装 + 管理（自律開発）
             Desktop = 企画・ブレスト（コードに縛られない思考）
             PM Lens = 構造化レイヤー（どちらで設計しても一元管理）
```

### 2026年4月時点の Claude Code の能力

- **Plan mode**: コードベースを探索し、変更前に設計判断ができる
- **code-architect サブエージェント**: コンポーネント設計・データフロー・実装マップを生成
- **並列エージェント**: フロントエンド/バックエンドを同時構築
- **Dispatch**: 大きなタスクを分解して複数エージェントに委任
- **MCP**: Google Drive, Jira, Slack 等からデータ取得可能
- **Web検索**: 技術調査もCode内で完結

---

## 2. 状況に応じたツール選択

| フェーズ | 推奨ツール | 理由 |
|---|---|---|
| ゼロからの企画・ブレスト | claude.ai / Desktop | コードに縛られない自由な思考 |
| 市場調査・技術比較 | どちらでも可 | 両方 Web 検索できる |
| アーキテクチャ設計 | Code (Plan mode) | コードベースを見ながら設計 |
| タスク分解・実装計画 | Code (PM Lens) | pm_add_task で構造化 |
| 実装・テスト | Code | 当然 |
| レビュー・修正 | Code (/review) | コード内で完結 |
| リリース判定 | 人間 + pm_dashboard | 俯瞰判断は人間の仕事 |

### Desktop を使うべき場面

- 「何を作るか」がまだ決まっていない企画段階
- 複数の選択肢を比較するリサーチ
- プロジェクトに紐づかない抽象的な議論
- Claude Code の結果を人間がレビューして方針決定する時

### Code で完結する場面（2026年以降）

- アーキテクチャ設計（Plan mode + code-architect）
- フェーズ分割と実装計画（PM Lens でタスク登録）
- 実装・テスト・コミット
- コードレビュー（/review コマンド）
- 複数タスクの並列実行（Dispatch + 並列エージェント）

---

## 3. ワークフローの5ステップ

### Step 1: 企画・構想

**場所**: claude.ai / Desktop（推奨）or Claude Code

- 市場調査・技術比較
- 実現可能性とポジショニングを決定
- Go/No-Go 判断

**成果物**: 調査結果のまとめ、プロジェクトの方向性

### Step 2: プロジェクト環境構築

**場所**: Claude Code or Desktop（Filesystem MCP 経由）

必要なファイルを配置：

```
project/
├── CLAUDE.md              ← Claude Code の "脳"（最重要）
├── .claude/
│   └── commands/
│       ├── review.md      ← /review（再利用パターン）
│       ├── test.md        ← /test
│       └── lint.md        ← /lint
├── docs/
│   └── architecture.md    ← 設計書
└── .gitignore
```

**CLAUDE.md に必ず含めるもの:**
- プロジェクト概要・技術スタック
- PM Lens 自動行動ルール（`pm_init` で自動追記される）
- Git 規約（ブランチ戦略 + Conventional Commits）
- コーディング規約
- 成功指標

### Step 3: PM 初期化 + タスク登録

**場所**: Claude Code

```
> PM初期化して
> docs/architecture.md を読んで、全フェーズのタスクを登録して
```

`PM初期化して` で `.pm/` ディレクトリの作成と CLAUDE.md への自動行動ルール追記が行われる。
続けて設計書を読ませてタスクを一括登録させると、PM Lens がタスクを構造化。
以降は `pm_next` で自律的に進む。

### Step 4: 自律開発サイクル

**場所**: Claude Code（PM Lens が進行管理）

```
pm_status → pm_next → 実装 → テスト → pm_update_task → git commit → pm_next → ...
```

Claude Code は CLAUDE.md の自動行動ルールに従い：
1. セッション開始時に `pm_status` + `pm_next` を自動実行
2. タスクを `in_progress` に変更して着手
3. 完了後に `done` + `pm_log` + アトミックコミット
4. 次のタスクへ自動遷移

**人間の役割**: pm_dashboard で俯瞰し、方向修正が必要な時だけ介入。

### Step 5: レビュー・リリース

**場所**: Claude Code + 人間

```
> /review          ← コードレビュー
> /final-review    ← リリース前検証
> /git-organize    ← コミット整理
```

人間が結果を確認し、GO / NO-GO 判定。

---

## 4. 指示の渡し方（3パターン）

### パターン A: スラッシュコマンド（再利用パターン）

```
.claude/commands/review.md → /review
```

**いつ使う**: 何度も繰り返すパターン（review, test, lint, git-organize）
**制約**: 新規作成後は Claude Code の再起動が必要

### パターン B: PM Lens タスク経由（一回きりの指示）

```
Claude Desktop or Code で pm_add_task に詳細な仕様を登録
  ↓
Claude Code が pm_next → タスク description を読んで実装
```

**いつ使う**: フェーズ実装、機能追加、バグ修正など一回きりのタスク
**利点**: 再起動不要。進捗も自動追跡。

### パターン C: プロンプトファイル（アドホック指示）

```
docs/prompts/next-task.md にタスク仕様を書く
  ↓
Claude Code に「docs/prompts/next-task.md を読んで実行して」と言う
```

**いつ使う**: PM Lens に登録するほどでもない小さな修正
**利点**: 再起動不要。

---

## 5. PM Lens 統合

### CLAUDE.md の自動管理

`pm_init` を実行すると、CLAUDE.md に PM Lens 自動行動ルールが自動追記される。
マーカー（`<!-- pm-server:begin -->` / `<!-- pm-server:end -->`）で囲まれるため、
他のセクションに影響しない。

PM Lens のバージョンアップ後は `pm-server update-claudemd --all` で全プロジェクトのルールを一括更新できる。

### PM Lens チートシート

```
進捗は？              → pm_status
次にやること           → pm_next
タスク追加            → pm_add_task
MYAPP-001 完了        → pm_update_task
ダッシュボード         → pm_dashboard
全プロジェクト状態     → pm_dashboard(project_path=None)
ブロッカー確認         → pm_blockers
設計決定を記録         → pm_add_decision
ベロシティ確認         → pm_velocity
リスク検知            → pm_risks
CLAUDE.md ルール更新   → pm_update_claudemd
プロジェクト一覧       → pm_list
```

---

## 6. Git ブランチ戦略

### 基本フロー

```
main ────────────────────────────── 常にリリース可能
  │
  ├── feature/xxx ── 機能追加 → squash merge → git branch -D
  ├── fix/xxx ────── バグ修正 → squash merge → git branch -D
  ├── refactor/xxx ─ リファクタ → squash merge → git branch -D
  └── release/vX.Y.Z リリース準備 → merge + tag
```

### ルール
- main は常にテストが通る状態
- 新機能・修正・リファクタは必ずブランチを切る
- ブランチ名: `{type}/{短い説明}`
- マージは squash merge（きれいな履歴）
- マージ後は `git branch -D`（squash merge 後は -d では拒否される）
- PM Lens のタスクIDをコミットメッセージに含める

### コミットメッセージ（Conventional Commits）

```
feat: add user authentication endpoint (MYAPP-014)
fix: rate limiter not resetting on new window (MYAPP-023)
refactor: extract validation logic to separate module
docs: add API documentation
test: add integration tests for auth flow
chore: update dependencies
```

---

## 7. カスタムスラッシュコマンドの設計パターン

### 再利用パターン専用

スラッシュコマンドは「何度も使う定型作業」だけに使う。
一回きりのタスク指示は PM Lens タスクかプロンプトファイルで。

### 推奨コマンドセット

| コマンド | 用途 |
|---|---|
| `/review` | コードレビュー（チェックリスト付き） |
| `/test` | テスト実行 |
| `/lint` | リント + フォーマット |
| `/git-organize` | コミット整理 |
| `/fix-{issue}` | 特定バグの修正（都度作成） |

### コマンドの基本構造（例: Python プロジェクト）

```markdown
# .claude/commands/review.md

まず pm_status で現在の状態を確認すること。

## チェック項目

1. ruff check src/ — エラーなし
2. ruff format --check src/ — フォーマット済み
3. pytest -v — 全テストパス

## レビュー観点

- アーキテクチャ原則への準拠
- エラーハンドリング
- テストカバレッジ
- セキュリティ

## 完了後

結果をサマリとして表示。
問題があれば修正方針を提案。
```

---

## 8. 新規プロジェクト開始チェックリスト

```
□ 企画・構想（Desktop or Code）
□ 設計書作成 (docs/architecture.md)
□ CLAUDE.md 作成（Git 規約 + コーディング規約）
□ .claude/commands/ に再利用コマンド（review, test, lint）
□ .gitignore
□ git init + 初回コミット
□ Claude Code で PM 初期化: 「PM初期化して」
  → .pm/ 作成 + CLAUDE.md に PM ルール自動追記
□ タスク一括登録: 「設計書を読んで全タスクを登録して」
□ 開発開始: pm_next → 実装サイクル
```
