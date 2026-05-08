# pm-server

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/)
[![Multi-Host](https://img.shields.io/badge/multi--host-Claude%20Code%20%2B%20Codex%20CLI-success)](#マルチホスト対応-claude-code--codex-cli)

**[English README](README.md)**

**PM Server for Claude Code + Codex CLI** — 複数 AI コーディングアシスタントに跨る プロジェクト管理 MCP Server

タスク管理・進捗可視化・意思決定記録を、Claude Code または Codex CLI セッション内の自然言語で。1 つの PM 基盤を複数ホストで共有。

```
> 進捗は？
✓ Phase 1 "Backend API": 60% 完了 (12/20 タスク)
  - 3件 作業中、1件 ブロック中
  - ベロシティ: 8 タスク/週 (↑ 上昇傾向)

> 次にやること
1. [P0] MYAPP-014: ユーザー認証エンドポイントの追加
2. [P1] MYAPP-015: レートリミット実装
3. [P1] MYAPP-018: インテグレーションテスト作成

> MYAPP-014 に着手
✓ MYAPP-014 → in_progress
```

---

## 特徴

- **🔌 マルチホストファースト** — `pm-server install --target=auto` 一発で **Claude Code と Codex CLI の両方に登録**。プロジェクトのルールも `CLAUDE.md` と `AGENTS.md` の両方に自動同期 (ADR-008)。プロジェクト途中でホストを切り替えてもコンテキストを失わない — 同じ `.pm/` データ、同じワークフロー
- **31 の MCP ツール** — タスク CRUD、子イシュー、ステータス、ブロッカー、ベロシティ、ダッシュボード、ADR、セッションメモリ、ワークフロー、ナレッジレコード、マルチホストルール注入 等
- **ワークフローエンジン** — テンプレートベースの開発ワークフロー（ループ、ユーザーゲート、チェイン対応：Discovery → Development）
- **ナレッジレコード** — カジュアルなメモリとフォーマルな ADR の中間に位置する構造化された知見記録（research、tradeoff、spec 等）
- **Super Research スキル** — 3 並列エージェント（Domain Expert、Critical Analyst、Lateral Thinker）+ Depth Check（6 次元）+ Fact Check + Cross-Check
- **セッションメモリ** — SQLite + FTS5 全文検索。記憶はセッションを跨いで永続化し、タスク・決定に紐付け可能
- **横断検索** — グローバルインデックスを使って全プロジェクトの記憶を横断検索
- **自然言語で操作** — 「進捗は？」「次にやること」と言うだけ
- **ゼロ設定** — `pip install` + `pm-server install` で完了。あとは「PM初期化して」と言うだけ
- **マルチプロジェクト** — グローバルレジストリで全プロジェクトを横断管理
- **Git フレンドリー** — `.pm/` ディレクトリにプレーン YAML で保存、`git diff` で追跡可能
- **非侵入的** — プロジェクトに `.pm/` を追加するだけ。`rm -rf .pm/` で完全除去

---

## クイックスタート

### インストール（初回のみ）

```bash
pip install pm-server
pm-server install       # Claude Code に MCP サーバーを登録
# Claude Code を再起動
```

### アップデート

```bash
pip install --upgrade pm-server
# Claude Code を再起動
```

> **注意:** `pip install pm-server`（`--upgrade` なし）では既存バージョンは更新されません。最新版にするには必ず `--upgrade`（または `-U`）を付けてください。

アップグレード後、各プロジェクトの CLAUDE.md 自動行動ルールは自動的に更新されます:

1. 次のセッション開始時に `pm_status` がテンプレートバージョンの不一致を検出
2. Claude Code が `pm_update_rules` を実行してルールセクションを更新（CLAUDE.md / AGENTS.md 両対応）
3. 新機能（子イシューワークフロー等）が即座に有効化

手動で更新することもできます:
```
> CLAUDE.md を更新して    # または: pm_update_rules
```

> レガシーの `pm_update_claudemd` ツールは後方互換 alias として引き続き利用可能（CLAUDE.md 限定）。
> v0.6.0 で `DeprecationWarning`、v1.0.0 で削除予定（PMSERV-055）。

### プロジェクト初期化

```
# Claude Code で対象プロジェクトに cd して
> PM初期化して
✓ .pm/ 作成
✓ グローバルレジストリに登録
✓ 検出: name=my-app, version=1.2.0 (package.json から)
```

`package.json`、`pyproject.toml`、`Cargo.toml`、`.git/config`、`README.md` からプロジェクト情報を自動検出します。

### 使い方

| 発話例 | 実行される処理 |
|---|---|
| `進捗は？` | プロジェクトの進捗サマリを表示 |
| `次にやること` | 優先度・依存関係から推薦タスクを表示 |
| `タスク追加：○○を実装` | 新規タスク追加（ID自動採番） |
| `MYAPP-003 完了` | タスクを done に更新 |
| `MYAPP-003 に課題がある` | タスクに子イシューを追加（phase 自動継承） |
| `ブロッカーある？` | ブロック中のタスクを一覧表示 |
| `ダッシュボード見せて` | HTML ダッシュボード生成（Chart.js、ダークテーマ） |
| `この設計にした理由を記録` | ADR（Architecture Decision Record）を追加 |
| `全プロジェクトの状態` | プロジェクト横断ポートフォリオビュー |

---

## マルチホスト対応 (Claude Code + Codex CLI)

`pm-server` v0.5.0 は **Claude Code** (`~/.claude/`) と **Codex CLI**
(`~/.codex/config.toml`) の 2 つの MCP **ホスト** に登録ターゲットとして対応します。
両ホストは MCP 設定ストアが完全に分離されているため、必要なら 1 度のインストールで
両方に届かせる必要があります。

### `--target` フラグ

`pm-server install` と `pm-server uninstall` は `--target` (alias `-t`) フラグを
受け付けます。デフォルトは **意図的に保守的**: `pm-server install` のままの既存
スクリプトやドキュメントは v0.4.x と完全に同じ「Claude Code のみ登録」の挙動を
維持します。

| `--target`      | 挙動                                                                          |
| --------------- | ----------------------------------------------------------------------------- |
| `claude-code`   | (default) Claude Code のみ登録。`~/.codex/config.toml` には一切触らない。     |
| `codex`         | Codex CLI のみ登録。`~/.claude/` には一切触らない。                           |
| `auto`          | filesystem 検知（`~/.codex/config.toml` の有無）— 検知された host のみ登録。  |
| `all`           | 全ての既知 host を強制登録。`~/.codex/config.toml` 不在でも作成。             |

姉妹コマンド `pm-server update-rules` (v0.5.0 で本機能と同時に追加) は
`--target auto` がデフォルトです。理由は v0.4.x の baseline がない新規コマンドで、
保守的に振る舞う必要がないため。

### 安全性

- **冪等性**: `install` を 2 回実行しても 2 回目は no-op。
- **バックアップ**: 各書き込み前に `~/.codex/config.toml` をタイムスタンプ付き
  バックアップにコピー。Claude Code 側は `claude mcp add` 経由のため、
  そちらの内部処理に従う。
- **コメント保持**: `config.toml` への編集は `tomlkit` を経由するため、
  ユーザー手書きのコメント、キー順序、空行がそのまま保持される。
- **ドライラン**: `--dry-run` は host 毎の予定アクションを表示し、書き込みは行わない。
  各行は `[dry-run]` プレフィックス付き。
- **ホスト独立性**: 1 つの host での失敗（例: `--target=all` 指定時に Codex CLI が
  未インストール）は他の host を中断させない。結果は host 毎に
  `installed` / `already_registered` / `skipped` / `failed` で報告される。

### クイック例

```bash
# デフォルト（後方互換）— Claude Code のみ
pm-server install

# このマシンで検知された host にだけ pm-server を追加
pm-server install --target auto

# 両 host に強制登録（必要なら ~/.codex/config.toml を作成）
pm-server install --target all

# プレビューのみ（ファイル書き込みなし）
pm-server install --target auto --dry-run

# 対称的なアンインストール（同じ --target セマンティクス）
pm-server uninstall --target auto
```

詳細は [`docs/design.md` §5.2](docs/design.md) と ADR-007 を参照
（detect-then-patch、backup、dry-run、絶対パス埋め込みの設計根拠）。

### プロジェクトルール注入 (CLAUDE.md / AGENTS.md)

`pm_init` と `pm_update_rules` は PM Server の自動行動ルールを host 毎の
適切な指示ファイルに同期します:

| Host          | 指示ファイル     |
| ------------- | ---------------- |
| Claude Code   | `CLAUDE.md`      |
| Codex CLI     | `AGENTS.md`      |

ルールセクションは `<!-- pm-server:begin v=N -->` / `<!-- pm-server:end -->`
マーカーで囲まれ、**マーカー内のみ in-place で更新** されます — マーカー外の
ユーザー記述には一切触れません。

`pm_update_rules` (および CLI 版 `pm-server update-rules`) のデフォルトは
`--target auto`: このマシンに存在する host を検知し、該当する指示ファイルのみ
更新します。検知は 4 種の signal（filesystem、marker、`CLAUDECODE` env、fallback）
で行われます — 詳細は ADR-008 amendment A3 と
[`docs/design.md` §6.4](docs/design.md) を参照。

| アクション                       | ツール                                                |
| -------------------------------- | ----------------------------------------------------- |
| MCP（セッション中）              | `pm_update_rules(target="auto", dry_run=False)`       |
| CLI（このプロジェクトに適用）    | `pm-server update-rules --target auto`                |
| CLI（登録された全プロジェクト）  | `pm-server update-rules --target auto --all`          |
| レガシー CLAUDE.md 限定          | `pm_update_claudemd` / `pm-server update-claudemd`    |

`AGENTS.md` は各書き込み前に `AGENTS.md.bak.<timestamp>` にバックアップされます。
`CLAUDE.md` の対称的バックアップは v0.6.0 で対応予定（PMSERV-058）。

詳細は [`docs/design.md` §6](docs/design.md) と ADR-008 を参照（claudemd → rules
モジュール rename、マーカー規約、データクラス、アトミック書き込みヘルパー）。

---

## ⚠ 並行セッション注意 (Phase-9 進行中)

`pm-server` v0.5.x は `pm_recall` に **多セッション disambiguation** を導入しました
（PMSERV-049、ADR-009）: 複数の Claude Code セッションが同一プロジェクトで並行
動作する場合、`pm_recall` は `current_session_id` と、曖昧な場合は
`last_session_candidates` 配列および `ambiguity_detected: true` フラグを返すため、
各セッションが自身に紐付くコンテキストを選択できます。

**PMSERV-048**（YAML アトミック書き込み + ファイルロック）が完了するまでは、
基底のストレージレイヤーは並行書き込みに対して安全ではありません。推奨事項:

- 並行セッションからの同時タスク更新は避ける（lost-update リスク）。
- `pm_recall` が `ambiguity_detected: true` を返した場合は、
  `last_session_candidates` を確認し `is_current_session: true` のエントリを採用。
- メモリレイヤー（SQLite）は PMSERV-047（WAL）が着地するまで rollback-journal
  モードで動作。

このセクションは PMSERV-048 着地時に削除されます。

---

## MCP ツール一覧（31ツール）

### プロジェクト管理

| ツール | 説明 |
|---|---|
| `pm_init` | `.pm/` 作成 + レジストリ登録 + プロジェクト情報推定 |
| `pm_status` | フェーズ進捗、タスク集計、ブロッカー、ベロシティ、アクティブタスク、hook 自動設定 |
| `pm_tasks` | タスク一覧（status / phase / priority / tag でフィルタ） |
| `pm_add_task` | タスク追加（ID自動採番: `MYAPP-001` 形式） |
| `pm_update_task` | ステータス・優先度・ノート・blocked_by を更新 |
| `pm_next` | 推薦タスク（blocked / 依存未完了を除外） |
| `pm_blockers` | ブロック中のタスクを全プロジェクトから一覧 |
| `pm_add_issue` | タスクに子イシューを追加（phase 自動継承、親タスクは自動で review に戻る） |

### 記録

| ツール | 説明 |
|---|---|
| `pm_log` | 日次ログ記録 + タスク自動紐付け（progress / decision / blocker / note / milestone） |
| `pm_add_decision` | ADR 追加（context、decision、consequences を構造化） |

### 分析

| ツール | 説明 |
|---|---|
| `pm_velocity` | 週次ベロシティ + トレンド判定（上昇 / 下降 / 横ばい） |
| `pm_risks` | リスク自動検知：期限超過、長期未更新、長期ブロック |

### 可視化

| ツール | 説明 |
|---|---|
| `pm_dashboard` | HTML ダッシュボード（単体プロジェクト or ポートフォリオ） |

### ディスカバリー

| ツール | 説明 |
|---|---|
| `pm_discover` | ディレクトリ配下の `.pm/` プロジェクトをスキャン・自動登録 |
| `pm_cleanup` | レジストリの無効パスを除去 |
| `pm_list` | 登録プロジェクト一覧 |

### メモリ（セッション継続）

| ツール | 説明 |
|---|---|
| `pm_remember` | 記憶を保存 + タスク自動紐付け（observation / insight / lesson） |
| `pm_recall` | 記憶を呼び出し — FTS5 検索、タスク別、横断検索に対応 |
| `pm_session_summary` | セッション要約の保存・取得・一覧 |
| `pm_memory_search` | type・tag・task_id フィルター付き高度な検索 |
| `pm_memory_stats` | メモリ DB の統計情報（件数・種別・DB サイズ） |
| `pm_memory_cleanup` | 古い記憶のクリーンアップ（dry-run 対応） |

### ナレッジレコード

| ツール | 説明 |
|---|---|
| `pm_record` | 構造化された知識を記録（research / market / spike / tradeoff / spec / api_design 等） |
| `pm_knowledge` | 知識レコードの検索・フィルタ・更新・サマリ |

### ワークフローエンジン

| ツール | 説明 |
|---|---|
| `pm_workflow_start` | テンプレートからワークフローを開始（development / discovery / super-research） |
| `pm_workflow_status` | 現在のステップ、進捗、次に取るべきアクションのガイダンスを表示 |
| `pm_workflow_advance` | 次のステップへ進める（ループ・スキップ対応、artifacts と notes を引き継げる） |
| `pm_workflow_list` | ステータスフィルタ付きで全ワークフローインスタンスを一覧 |
| `pm_workflow_templates` | 利用可能なテンプレート一覧（組み込み + カスタム） |

### メンテナンス

| ツール | 説明 |
|---|---|
| `pm_update_rules` | CLAUDE.md / AGENTS.md の PM Server ルールセクションを最新版に更新（マルチホスト対応、ADR-008）。デフォルト `target=auto` でインストール済 host を自動検出 |
| `pm_update_claudemd` | レガシー alias of `pm_update_rules(target="claude-code")` — v0.6.0 で deprecation 予定 |

---

## データ構造

タスクデータはプレーン YAML、記憶は SQLite で保存:

```
your-project/
└── .pm/
    ├── project.yaml        # プロジェクトメタ情報
    ├── tasks.yaml          # タスク（ステータス・優先度・依存関係）
    ├── decisions.yaml      # ADR (Architecture Decision Records)
    ├── milestones.yaml     # マイルストーン定義
    ├── risks.yaml          # リスク・ブロッカー
    ├── memory.db           # セッション記憶（SQLite + FTS5）
    └── daily/
        └── 2026-04-08.yaml # 日次ログ（自動生成）

~/.pm/
├── registry.yaml           # グローバルプロジェクトインデックス
└── memory.db               # 横断検索用メモリインデックス
```

YAML ファイルは人間が読め、手動編集しても壊れません。メモリ DB はセッションデータの正本で、`~/.pm/memory.db` が横断検索を可能にします。

---

## CLAUDE.md / AGENTS.md 統合

プロジェクトの `CLAUDE.md` に以下を追加すると、セッション中の PM 操作が自動化されます（`pm-server update-rules` で自動追加も可能）:

```markdown
## PM Server 自動行動ルール（必ず従うこと）

### セッション開始時（最初の応答の前に必ず実行）
1. pm_status を MCP ツールとして実行し、現在の進捗を表示する
2. pm_next で次に着手すべきタスクを3件表示する
3. pm_recall で前回セッションの文脈を取得する
4. ブロッカーや期限超過があれば警告する
5. pm_status の claudemd.other_rule_sections に他のルールセクションが報告された場合、この CLAUDE.md 内の該当セクションのルールも全て実行する

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

### タスク完了時（コードが動作確認できたら）
1. pm_update_task で done に変更する
2. all_issues_resolved フラグが返された場合、親タスクの完了もユーザーに提案する
3. pm_log に完了内容を記録する
4. 次の推薦タスクを pm_next で表示する
5. アトミックコミットを作成する

### タスク完了確認中にイシュー（課題）が見つかった時
1. pm_add_issue で親タスクに紐づくイシュー（子タスク）を作成する
   - phase は親タスクから自動継承される
   - 親タスクが done だった場合、自動で review に戻される
2. イシューを解消したら pm_update_task で done に変更する
3. 全イシューが解消されると all_issues_resolved フラグが返される
4. 親タスクの完了をユーザーに提案する

### 設計上の意思決定が発生した時
1. ユーザーに「ADRとして記録しますか？」と確認する
2. 承認されたら pm_add_decision で保存する

### コーディングセッション終了時
1. 進行中のタスクの状態を確認し、必要に応じて更新する
2. pm_log にセッションの成果を記録する
3. pm_session_summary で要約を保存する
4. 未コミットの変更があればコミットする
```

---

## Tips: pm-server を最大限に活用するために

### 推奨ワークフロー

```
1. インストール＆登録      →  pip install pm-server && pm-server install
2. Claude Code を起動      →  (インストール後に再起動)
3. プロジェクト初期化      →  「PM初期化して」
4. タスク追加              →  「タスク追加：ユーザー認証を実装」
5. タスクに着手            →  「MYAPP-001 に着手」
6. タスク完了              →  「MYAPP-001 完了」
7. レビューで課題発見      →  「MYAPP-001 に課題がある：…」（子イシュー作成）
8. セッション終了          →  「セッションまとめて」（要約＋ログを自動保存）
```

### Compaction（コンテキスト圧縮）対策

Claude Code はセッションが長くなると、会話のコンテキストを自動的に圧縮（compact）します。これにより、前半のやり取りの詳細が失われる場合があります。pm-server のメモリ機能でこれを防げます：

| 状況 | やるべきこと |
|---|---|
| 重要な発見をした | `pm_remember` で即座に記録 — セッション終了を待たない |
| 設計の議論が終わった | 結論を `pm_remember` でまとめて保存 |
| `/clear` する前 | 先に `pm_session_summary` を実行 |
| Compaction 後にコンテキストが薄い | `pm_recall` で前の文脈を復元 |
| 新しいセッションを開始 | `pm_recall` + `pm_status`（CLAUDE.md ルール設定済みなら自動） |

**基本原則:** 早めに、こまめに保存する。Compaction のタイミングは予測できません — 残す価値のある情報は、その場で記録しましょう。

### セッション継続性

pm-server のメモリ層が、セッション間の情報断絶を防ぎます：

```
セッション 1                          セッション 2
  │                                     │
  ├─ pm_remember（発見を記録）           ├─ pm_recall ← コンテキスト復元
  ├─ pm_remember（判断を記録）           ├─ pm_status ← 現在の状態
  ├─ pm_session_summary                 │
  └─ （セッション終了）                  └─ （シームレスに継続）
```

### 自動 Hook（ライフサイクル強制）

pm-server は初回セッション開始時（`pm_status`）に Claude Code の hook を自動インストールします。`git commit` 後に PostToolUse hook がリマインドを会話に注入し、`pm_log`、`pm_update_task`、`pm_next` の呼び出しを促します。

- Hook は `~/.claude/settings.json` にグローバルにインストール
- 既存のユーザー hook は保全（pm-server の hook は追記、上書きしない）
- 手動操作不要 — アップグレード時も自動インストール
- 手動管理: `pm-server install-hooks` / `pm-server uninstall-hooks`

### マルチプロジェクト管理

```
> 「~/projects 以下のプロジェクトを探して」    # 自動スキャン＆登録
> 「全プロジェクトの状態」                      # ポートフォリオ一覧
> 「'auth' で横断検索して」                     # 全プロジェクト横断検索
> 「全プロジェクトのダッシュボード」             # ポートフォリオ HTML
```

---

## CLI コマンド

```bash
pm-server install          # MCP サーバー登録 (default: Claude Code のみ — 後方互換)。
                           # マルチホスト対応で --target {auto,all,claude-code,codex} を指定可能。
                           # --dry-run で書き込みなしの preview。詳細は「マルチホスト対応」セクション参照。
pm-server uninstall        # install と対称（同じ --target / --dry-run セマンティクス）
pm-server serve            # MCP Server 起動（Claude Code が自動で呼び出す）
pm-server discover .       # .pm/ を持つプロジェクトをスキャン
pm-server status           # ターミナルからステータス確認
pm-server context-inject   # セッションコンテキストを stdout に出力（hook 連携用）
pm-server migrate          # pm-agent からの移行（MCP 登録の切り替え）
pm-server update-rules     # PM Server ルールを CLAUDE.md / AGENTS.md に注入（ADR-008）。
                           # --target {auto,all,claude-code,codex} (default: auto)
                           # --dry-run / --all (登録された全プロジェクトに適用)
pm-server update-claudemd  # レガシー alias of `update-rules --target=claude-code`。v0.6.0 で deprecation 予定
pm-server install-hooks    # Claude Code の hook を手動インストール（通常は pm_status で自動）
pm-server uninstall-hooks  # PM Server の hook を削除
```

---

## アーキテクチャ

```
Claude Code Session
  │
  ├── CLAUDE.md 自動行動ルール
  ├── PostToolUse hooks（自動インストール）
  ├── Skills（super-research 等）
  │
  └── MCP Server (stdio)
        └── pm-server serve
              │
              ├── server.py    → 31 MCP ツール (FastMCP)
              ├── models.py    → Pydantic v2 データモデル (17 models, 15 enums)
              ├── storage.py   → YAML 読み書き
              ├── workflow.py  → ワークフローエンジン (state machine)
              ├── memory.py    → SQLite メモリストア + FTS5 検索
              ├── recall.py    → セッションコンテキスト構築（トークン予算制御）
              ├── hooks.py     → Claude Code hook ハンドラ + インストーラー
              ├── context.py   → CLI コンテキスト注入
              ├── velocity.py  → ベロシティ計算・リスク検知
              ├── dashboard.py → HTML/テキスト ダッシュボード (Jinja2) + ワークフロー進捗 + ナレッジマップ
              ├── discovery.py → プロジェクト情報自動推定
              └── installer.py → マルチホスト MCP 登録 (ADR-007)
                                   ├─ install_claude_code() → claude mcp add (subprocess)
                                   ├─ install_codex()       → ~/.codex/config.toml (tomlkit)
                                   └─ install(target=...)   → orchestrator + InstallSummary

データレイヤー (pm-server serve 経由でアクセス):
  ├── project-A/.pm/ (YAML + workflows + knowledge + memory.db)
  ├── project-B/.pm/ (YAML + workflows + knowledge + memory.db)
  └── ~/.pm/registry.yaml + memory.db
```

---

## pm-agent からの移行

以前の `pm-agent` パッケージから移行する場合:

```bash
pip uninstall pm-agent
pip install pm-server
pm-server migrate       # MCP 登録を pm-agent → pm-server に切り替え
# Claude Code を再起動
```

`migrate` コマンドの実行内容:
- 旧 `pm-agent` の MCP 登録を解除
- 新 `pm-server` を MCP サーバーとして登録
- `~/.pm/registry.yaml` の整合性チェック
- `CLAUDE.md` 内の `pm-agent` への言及があれば警告

`.pm/` ディレクトリのデータは**そのまま使えます** — データ移行は不要です。

---

## 動作要件

- Python 3.11+
- Claude Code（MCP サポート付き）

### 依存パッケージ

- [FastMCP](https://github.com/jlowin/fastmcp) — MCP サーバーフレームワーク
- [Pydantic](https://docs.pydantic.dev/) v2 — データバリデーション
- [PyYAML](https://pyyaml.org/) — データ永続化
- [Click](https://click.palletsprojects.com/) — CLI フレームワーク
- [Jinja2](https://jinja.palletsprojects.com/) — ダッシュボードテンプレート

---

## 開発

```bash
git clone https://github.com/flc-design/pm-server.git
cd pm-server
pip install -e ".[dev]"
pytest                  # 400+ テスト
ruff check src/         # リント
ruff format src/        # フォーマット
```

---

## 設計原則

1. **Zero Configuration** — `pip install` + 1コマンドで完了
2. **Auto-everything** — 検出・登録・推定はすべて自動
3. **Git-friendly** — プレーンテキスト YAML、`git diff` で追跡可能
4. **Human-readable** — 手動編集しても壊れない
5. **AI-native** — Claude Code が自然に読み書きできるフォーマット
6. **Non-invasive** — `.pm/` を追加するだけ。プロジェクト構造を変更しない

---

## 商標に関する注記

「PM Server」はプロジェクト管理サーバーの一般的な呼称であり、本パッケージは
PyPI 上で `pm-server` として配布される際の表示名として使用しています。

本プロジェクトは以下の製品・サービスと一切の **提携・後援・スポンサー関係はありません**:

- **Microsoft Project Server** / Project Online / Project for the web（Microsoft Corporation）
- **Percona Monitoring and Management**（PMM Server）（Percona LLC）
- **Apple Carbon Print Manager**（廃止された ApplicationServices フレームワーク内の `PMServer` opaque type）（Apple Inc.）
- **Informatica PowerCenter**（`pmserver.exe` Integration Service デーモン）（Informatica LLC）
- その他、類似の用語を使用する製品・ベンダー・サービス全般

すべての商標は各所有者に帰属します。

---

## ライセンス

MIT — Shinichi Nakazato / FLC design co., ltd.
