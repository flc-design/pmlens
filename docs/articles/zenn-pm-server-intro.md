---
title: "Claude Code が\"プロジェクトを覚えていない\"問題を、MCP Server で解決した — pm-server"
emoji: "📋"
type: "tech"
topics: ["claudecode", "mcp", "codexcli", "ai", "個人開発"]
published: false
---

## こんな課題はないか

Claude Code（や Codex CLI）で開発していて、こんな経験はないだろうか。

- セッションを閉じたら「何をどこまでやったか」が消える
- 会話が長くなると **compaction（自動圧縮）** で文脈が縮み、重要な技術判断が飛ぶ
- 「なぜこの設計にしたか」を ADR として残したいが、結局メモが散らばる
- そして **複数のプロジェクトを並行管理していると、各プロジェクトの文脈を思い出し、タスクを整理・分析するだけで毎回時間がかかる**

AI コーディングアシスタントは「いまこの瞬間のコード」を書くのは得意だが、**プロジェクトという時間軸の長い文脈を覚えておくのは苦手**だ。人間が頭の中でやっている「進捗の把握・次の一手・過去の判断の想起」を、誰も肩代わりしてくれない。

これを「自然言語のまま」解決する MCP Server を作った。**pm-server** という。

https://github.com/flc-design/pm-server

## pm-server とは

pm-server は、Claude Code / Codex CLI 向けの**プロジェクト管理 MCP Server**だ。タスク追跡・進捗可視化・ブロッカー検知・ADR 記録・セッション記憶・全プロジェクト横断ダッシュボードを、**コマンドを覚えずに自然言語で**呼び出せる。

```
> 進捗は？
✓ Phase 1 "Backend API": 60% complete (12/20 tasks)
  - 3 tasks in progress, 1 blocked
  - Velocity: 8 tasks/week (↑ trending up)

> 次にやること
1. [P0] MYAPP-014: Add user authentication endpoint
2. [P1] MYAPP-015: Implement rate limiting
3. [P1] MYAPP-018: Write integration tests

> MYAPP-014 に着手
✓ MYAPP-014 → in_progress
```

`pm_dashboard` を呼べば、フェーズ進捗・ベロシティ・ブロッカー・リスクを一望できる HTML ダッシュボードも生成される。

![pm-server の HTML ダッシュボード](https://raw.githubusercontent.com/flc-design/pm-server/main/docs/assets/dashboard.png)

「進捗は？」「次やること」「これを ADR に残して」と話しかけるだけ。裏側では 42 個の MCP ツール（`pm_status` / `pm_add_task` / `pm_remember` / `pm_add_decision` …）が動く。

## 主要機能

| 機能 | 内容 |
|---|---|
| **タスク / フェーズ管理** | ID 自動採番、依存関係、子イシュー、優先度、フェーズ進捗 |
| **セッション記憶** | SQLite + FTS5 全文検索。`pm_remember` / `pm_recall` で世代をまたいで想起 |
| **ADR（意思決定記録）** | 「なぜこの設計か」を context / decision / consequences で構造化保存 |
| **ワークフローエンジン** | テンプレートベースの状態マシン（discovery → development をチェイン） |
| **ダッシュボード** | 単体プロジェクト／全プロジェクト横断（ポートフォリオ）の HTML 可視化 |
| **横断検索** | グローバルレジストリ越しに、全プロジェクトの記憶をまとめて検索 |

冒頭の「複数プロジェクトの文脈想起に時間がかかる」課題には、この**横断検索 + ポートフォリオダッシュボード**が効く。プロジェクトごとに `.pm/` を持ちつつ、グローバルレジストリで全体を俯瞰し、`pm_recall(cross_project=True)` で過去の知見を横串で引ける。

## 設計思想 — 3 つの読みどころ

ただのタスク管理ツールではなく、「AI と人間の両方が読み書きする前提」で設計している。特徴的な判断を 3 つ紹介する。

### 1. データは YAML を SSoT（Single Source of Truth）に

タスクや ADR は、すべて `.pm/` ディレクトリ配下の**プレーン YAML**で保存する。

```
.pm/
├── project.yaml      # プロジェクト・フェーズ定義
├── tasks.yaml        # タスク
├── decisions.yaml    # ADR
├── knowledge.yaml    # ナレッジレコード
└── memory.db         # セッション記憶（SQLite + FTS5）
```

YAML を選んだのは **git-friendly** だから。`git diff` で「いつ・誰が・どのタスクを動かしたか」がそのまま追跡でき、レビューもマージもできる。プロジェクトに足すのは `.pm/` だけ。`rm -rf .pm/` で完全に消せる非侵襲設計だ。

一方、**全文検索が要る「記憶」だけは SQLite + FTS5** に分離している。構造データ（YAML）と検索インデックス（SQLite）で役割を分け、それぞれ得意なことをやらせている。

### 2. セッションをまたいでも忘れない

Claude Code は会話が伸びると compaction で文脈を縮める。そのタイミングは予測できない。だから pm-server は「重要な発見・判断はその場で `pm_remember` に逃がす」という運用を前提にしている。

さらに **branch-aware recall**（ブランチ単位のセッション継続）を持つ。複数の作業ライン（feature ブランチや git worktree）を行き来しても、`pm_recall(track="<branch>")` でそのライン専用の前回文脈だけを復元できる。実装上の面白いところは、**読み出し経路は git に一切触れない**点だ。ブランチ名は書き込み時に記録され、想起はクエリ時にラベルで解決するので、副作用ゼロで「ブランチごとに文脈が切り替わる」体験になる。

### 3. Claude Code と Codex CLI、両対応

タスク管理が host ごとに分断していたら意味がない。pm-server は 1 コマンドで両方に登録できる。

```bash
pm-server install --target=auto   # 検出した host すべてに登録
```

プロジェクトのルールは `CLAUDE.md` と `AGENTS.md` の両方に自動同期される。host をまたいでも、同じ `.pm/` データ・同じワークフローがそのまま使える。「1 つの PM 基盤、複数の AI ホスト」というわけだ。

## 使ってみる

導入は 3 ステップ。

```bash
pip install pm-server
pm-server install     # MCP Server を登録
# Claude Code を再起動
```

あとは Claude Code でプロジェクトのディレクトリに入り、こう言うだけ。

```
> PM初期化して
```

`.pm/` が作られ、グローバルレジストリに登録される。以降は「進捗は？」「次やること」「これブロッカーね」と話しかければいい。

## おわりに

pm-server は **MIT ライセンスの OSS**で、PyPI から `pip install pm-server` で入る。Python 3.11+、FastMCP / Pydantic v2 で実装している。

- GitHub: https://github.com/flc-design/pm-server
- PyPI: https://pypi.org/project/pm-server/

「AI に書かせる」だけでなく「AI とプロジェクトを進める」ための基盤として、よかったら触ってみてほしい。フィードバックや Issue も歓迎です。
