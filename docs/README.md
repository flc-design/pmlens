# PM Server Documentation

PM Server の各種ドキュメント。読者層と用途で 3 つの HTML ガイドに分かれている。

## 読者別の入口

| 読者 | 推奨ドキュメント | 用途 |
|------|----------------|------|
| **使い始める人** | [user-guide.html](./user-guide.html) | CLI コマンド・1日のセッションフロー・進捗チェック Tips・FAQ |
| **workflow を作る人** | [workflow-guide.html](./workflow-guide.html) | Workflow Engine の仕組み・YAML schema・カスタム template の作り方 |
| **内部実装を読む人** | [architecture.html](./architecture.html) | モジュール構造・data flow・Lens / Outbox の不変条件 |

## 全ドキュメント一覧

### HTML ガイド (v0.10.0 時点)
- **[architecture.html](./architecture.html)** — Architecture &amp; Behavior（42 MCP tools, Lens mode, Phase 2 Desktop Outbox, distribution channels）
- **[user-guide.html](./user-guide.html)** — User Guide（CLI コマンド完全リファレンス・UX Tips・トラブルシューティング）
- **[workflow-guide.html](./workflow-guide.html)** — Workflow Guide（5 builtin templates 詳解 (incl. brainstorming, content-pipeline)・YAML schema・カスタム作成方法）

### 補助ドキュメント
- [cheatsheet.md](./cheatsheet.md) / [cheatsheet.ja.md](./cheatsheet.ja.md) — MCP ツール簡潔リファレンス（quick lookup 用）
- [design.md](./design.md) — 詳細設計書（アーキテクチャ・データモデル・MCP API 一覧）
- [workflow.md](./workflow.md) — Claude Code 開発ワークフロー（プロセス論）
- [memory-layer-design.md](./memory-layer-design.md) — メモリ層 (SQLite + FTS5) の設計
- [memory-layer-prompt.md](./memory-layer-prompt.md) — メモリ層導入時のプロンプト履歴

## どこから読むべきか

- **初めて pm-server を入れる** → `user-guide.html` の §2 Quickstart
- **Claude Code セッションでの日常運用を知りたい** → `user-guide.html` の §4 セッションフロー・§5 進捗チェック Tips
- **設計判断の経緯を理解したい** → `architecture.html` を順に読み、必要なら `design.md` を深堀り
- **自分の組織向けに workflow を増やしたい** → `workflow-guide.html` の §6 schema・§7 カスタム作成
- **Lens / Outbox の安全性を確認したい** → `architecture.html` の §4 Lens Mode・§9 Desktop Outbox
