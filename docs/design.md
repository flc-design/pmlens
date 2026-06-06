# pm-server — Claude Code プロジェクト管理システム設計書

**Version**: 0.5.0
**Date**: 2026-04-16
**Author**: Shinichi Nakazato / FLC design co., ltd.
**Status**: Implemented (Phase 1-7 complete)
**License**: MIT
**PyPI**: `pm-server`
**GitHub**: `github.com/flc-design/pm-server`

---

## 変更履歴

| Version | Date | 変更内容 |
|---|---|---|
| 0.1.0 | 2026-04-03 | 初版設計 |
| 0.2.0 | 2026-04-03 | MCP ツール詳細化、パッケージ構成追加 |
| 0.3.0 | 2026-04-08 | パッケージ名 `pm-agent` → `pm-server` に変更。installer.py を `claude mcp add` 方式に修正。pm_discover デフォルトパス修正。migrate コマンド追加。実装完了状況を反映 |
| 0.4.0 | 2026-04-15 | Memory Layer (Phase 1-4) 完了。SQLite + FTS5 セッションメモリ、横断検索、運用ツール実装。子イシュー機能追加。PyPI v0.3.3 公開 |
| 0.5.0 | 2026-04-16 | Workflow Engine (Phase 5)、Knowledge Records (Phase 6)、Super Research & Skill エコシステム (Phase 7) 実装。ダッシュボードにワークフロー進捗・知識マップ追加。30 MCP ツール、406テスト |
| 0.9.x | 2026-06-06 | branch-aware セッション継続性 (§3.4 / ADR-028)。`pm_recall(track=)` + `SessionSummary.branch`、`.git/HEAD` テキスト解析によるブランチ検出（git に shell out しない）、worktree 主トポロジ。SynapticLedger ADR-034 対応 |

---

## 1. 概要

### 問題

Claude Code でコードは高速に書けるが、以下が欠如している：

- **進捗の可視化** — 今どこまで進んでいるか分からない
- **タスクの優先順位** — 次に何をやるべきか判断できない
- **プロジェクト横断の俯瞰** — 複数プロジェクトの状態を一覧できない
- **意思決定の記録** — なぜその設計にしたかが消える
- **ブロッカーの検知** — 依存関係で止まっているタスクに気づかない

### 解決策

Claude Code の MCP Server として動作する pm-server。
**ワンコマンドインストール、ゼロ設定で動く。**

```
$ pip install pm-server
$ pm-server install     ← Claude Code MCP 設定を自動注入

# Claude Code で
> PM初期化して
✓ .pm/ 作成
✓ レジストリ自動登録
✓ git/README からプロジェクト情報推定
```

---

## 2. ユーザー体験

### 2.1 インストール（1回だけ）

```bash
pip install pm-server
pm-server install
```

`pm-server install` が実行すること：

1. `~/.pm/` ディレクトリ作成
2. `~/.pm/registry.yaml` 初期化（空のプロジェクトリスト）
3. Claude Code MCP 設定の自動注入（`claude mcp add` コマンド経由）：

```python
subprocess.run([
    "claude", "mcp", "add",
    "--scope", "user",
    "pm-server",
    "--",
    shutil.which("pm-server"), "serve"
], check=True)
```

4. 完了メッセージ：
```
✓ pm-server installed successfully!
  - MCP server registered in Claude Code (user scope)
  - Restart Claude Code to activate
```

### 2.2 プロジェクト初期化（プロジェクトごと）

Claude Code で対象プロジェクトに `cd` して、自然言語で指示するだけ：

```
> PM初期化して
> このプロジェクトのPM始めて
> pm init
```

pm-server が自動でやること：

1. カレントディレクトリに `.pm/` を作成
2. `~/.pm/registry.yaml` にパスを自動登録
3. プロジェクト情報の自動推定：
   - `package.json` / `pyproject.toml` / `Cargo.toml` → プロジェクト名・バージョン
   - `.git` → リポジトリURL
   - `README.md` → プロジェクト概要
4. 推定結果を表示して確認を求める

```yaml
# 自動生成される project.yaml の例
name: my-app
display_name: "My App"
version: 1.2.0
status: development
started: 2026-04-08
repository: https://github.com/user/my-app
description: "Web application with REST API"
```

### 2.3 日常の使い方

```
> 進捗は？              → pm_status（自動でカレントプロジェクトを検出）
> 次にやること           → pm_next
> ダッシュボード見せて    → pm_dashboard（HTMLを生成・表示）
> 全プロジェクトの状態    → pm_dashboard()（横断ビュー）
> タスク追加：○○を実装   → pm_add_task
> MYAPP-003 完了         → pm_update_task
> この設計にした理由を記録 → pm_add_decision
> ブロッカーある？        → pm_blockers
```

### 2.4 自動行動（CLAUDE.md による）

各プロジェクトの CLAUDE.md に自動行動ルールを記述：

- **セッション開始時** — pm_status + pm_next を自動実行
- **タスク着手時** — ステータスを in_progress に変更
- **タスク完了時** — ステータス更新 + pm_log + 次の推薦
- **設計決定時** — ADR 記録を提案
- **ブロッカー発見時** — タスクを blocked に変更 + リスク登録

---

## 3. アーキテクチャ

### 3.1 全体構成

```
┌──────────────────────────────────────────────┐
│  Claude Code Session                         │
│                                              │
│  ┌──────────────┐    ┌────────────────────┐  │
│  │  CLAUDE.md   │    │  PM MCP Server     │  │
│  │  自動行動    │───▶│ (pm-server serve)  │  │
│  │  ルール      │    │                    │  │
│  │              │    │  FastMCP (stdio)   │  │
│  └──────────────┘    └─────────┬──────────┘  │
│                                │              │
└────────────────────────────────┼──────────────┘
                                 │
                    ┌────────────┴────────────┐
                    ▼                         ▼
        project-A/.pm/              project-B/.pm/
        ├── project.yaml            ├── project.yaml
        ├── tasks.yaml              ├── tasks.yaml
        ├── decisions.yaml          ├── decisions.yaml
        └── daily/                  └── daily/
                    ▲
                    │
            ~/.pm/registry.yaml
            (全プロジェクトのインデックス)
```

### 3.2 プロジェクトパスの自動検出

MCP ツールの `project_path` を省略可能にする。省略時のフォールバック：

```python
def resolve_project_path(project_path: str | None = None) -> Path:
    if project_path:
        return Path(project_path)
    
    # 1. 環境変数 PM_PROJECT_PATH
    if env_path := os.environ.get("PM_PROJECT_PATH"):
        return Path(env_path)
    
    # 2. カレントディレクトリから上方向に .pm/ を探索
    cwd = Path.cwd()
    for parent in [cwd, *cwd.parents]:
        if (parent / ".pm").is_dir():
            return parent
    
    # 3. 見つからなければエラー
    raise ProjectNotFoundError("No .pm/ directory found. Run pm_init first.")
```

### 3.3 データモデル

```
.pm/
├── project.yaml        # プロジェクトメタ情報
├── tasks.yaml          # タスク一覧・状態
├── decisions.yaml      # ADR (Architecture Decision Records)
├── milestones.yaml     # マイルストーン定義
├── risks.yaml          # リスク・ブロッカー
└── daily/
    └── 2026-04-08.yaml # 日次ログ（自動生成）
```

#### project.yaml

```yaml
name: my-app
display_name: "My App — Web Application"
version: 1.2.0
status: development  # design | development | testing | maintenance | archived
started: 2026-04-08
owner: user
repository: https://github.com/user/my-app
description: "REST API を持つ Web アプリケーション"

phases:
  - id: phase-1
    name: "Backend API"
    status: active    # planned | active | completed
    target_date: 2026-05-15
  - id: phase-2
    name: "Frontend"
    status: planned
    target_date: 2026-06-15

health:
  velocity: null
  blockers: 0
  overdue: 0
```

#### tasks.yaml

```yaml
tasks:
  - id: MYAPP-001
    title: "ユーザー認証 API 実装"
    phase: phase-1
    status: todo      # todo | in_progress | review | done | blocked
    priority: P0      # P0 | P1 | P2 | P3
    assignee: claude-code
    estimate_hours: 8
    actual_hours: null
    depends_on: []
    blocked_by: []
    tags: [api, auth]
    created: 2026-04-08
    updated: 2026-04-08
    description: |
      JWT ベースのユーザー認証エンドポイント実装。
    acceptance_criteria:
      - POST /auth/login でトークン発行
      - トークンの有効期限と更新フロー
```

#### decisions.yaml

```yaml
decisions:
  - id: ADR-001
    title: "認証方式に JWT を採用"
    date: 2026-04-08
    status: accepted  # proposed | accepted | deprecated | superseded
    context: |
      セッションベース認証と JWT 認証を比較検討。
      マイクロサービス化を見据えてステートレスな方式が望ましい。
    decision: |
      JWT (RS256) を採用。リフレッシュトークンで長期セッションに対応。
    consequences:
      positive:
        - サーバー側でセッション状態を保持しない
        - マイクロサービス間の認証が容易
      negative:
        - トークン失効の即時反映が困難
      mitigations:
        - 短い有効期限（15分）+ リフレッシュトークンで緩和
```

---

### 3.4 branch-aware セッション継続性 (ADR-028)

複数の作業ライン（例: 本流／論文／教材）を 1 リポジトリで並行させる時、
`pm_recall` がライン単位で「そのラインの最新セッション要約」を返せるようにする。
SynapticLedger ADR-034 / SYNAPT-079 への pm-server 側の対応。

#### 設計の肝: 検出は write、利用は read（CQRS 分割）

`pm_recall` は `RO_ALLOWLIST`（§4 / PM_LENS）に属する読み取り専用ツールで、
PM_LENS=1 では mutator/subprocess 経路が構造的に除外される（ADR-015/017/018）。
したがって **`pm_recall` は git を一切触らない**。ブランチの「検出」は mutator 側
（`pm_session_summary(action="save")`）でのみ行い、`pm_recall` は呼び出し元が渡す
`track` 引数でブランチを「利用」する。

```
保存時 (mutator):  read_git_branch(.git/HEAD) → SessionSummary.branch に記録
想起時 (RO tool):  pm_recall(track="<branch>") → branch 列で絞り込み
```

#### git に shell out しない（CVE-2026-45033 対策）

ブランチ検出は `git rev-parse` を呼ばず、`.git/HEAD`（`ref: refs/heads/<branch>`）を
**テキストとして読む**。`discovery.py:_read_git_remote_origin_url`（`.git/config` の
テキスト解析）と同じ方針で、悪意ある `.git/config`（`core.fsmonitor` /
`core.sshCommand` / `core.pager` / `core.hookspath`）が `git` 実行時に任意コードを
走らせる git config-exec クラスを構造的に回避する。共通実装
`discovery.read_git_branch()` を save 経路（Python）と SessionStart hook（shell）の
双方が使い、保存文字列と recall 文字列の一致を保証する。

#### 主トポロジ = git worktree（コード 0）

データ層は §3.2 のとおり `.pm/` を含む**ディレクトリ単位**で解決される
（`resolve_project_path` + `_get_memory_store`）。`.pm/memory.db` は gitignore 対象で
worktree ごとに独立するため、**1 ライン = 1 worktree** にすれば追加実装なしで
`pm_recall` がライン単位の最新を返す。これを主トポロジとして推奨し、上記の
`track` モードは「1 ディレクトリ + ブランチ切替」派向けの副モードとして提供する。

#### データモデル / 後方互換

- `session_summaries` に nullable `branch TEXT` 列を追加。マイグレーションは
  `_migrate_session_summaries_branch()`（`updated_at` 移行と同型：冪等・追加のみ・
  backfill なし）。複合 index `(branch, updated_at DESC)`。
- 既存行は `branch IS NULL`。`get_latest_summary_by_branch(branch)` は無マッチ時に
  overall-latest へ graceful fallback し `(summary, track_matched=False)` を返すため、
  既存 DB でも初日から壊れない。
- ブランチ別「最新」は `ORDER BY updated_at DESC, id DESC`（UPSERT で id が据え置かれる
  ため「最後に触ったライン」を id 順では表せない）。
- UPSERT の branch 更新は `COALESCE(NULLIF(excluded.branch, ''), ...)` で非破壊化
  （検出失敗時の '' で既知ブランチを潰さない／実際の checkout 先には更新する）。
- `track` は `pm_recall` レスポンスのトップレベルキー（`track` / `track_matched`）として
  追加し、`last_session` の形（6 キー）は不変に保つ。`track` 未指定時の応答は従来と
  バイト一致。

## 4. MCP Server 設計

### 4.1 ツール一覧

```python
from fastmcp import FastMCP

mcp = FastMCP("pm-server")

# ─── プロジェクト管理 ───

@mcp.tool()
def pm_init(project_path: str | None = None, project_name: str | None = None) -> dict:
    """プロジェクトの PM を初期化する。
    .pm/ ディレクトリを作成し、グローバルレジストリに自動登録する。
    project_path 省略時はカレントディレクトリ。
    project_name 省略時はディレクトリ名 or package.json/pyproject.toml から推定。
    git リポジトリURLやREADMEからの情報も自動収集する。"""

@mcp.tool()
def pm_status(project_path: str | None = None) -> dict:
    """プロジェクトの現在状態を返す。
    フェーズ進捗率、タスク集計、ブロッカー数、期限超過数、ベロシティを含む。"""

@mcp.tool()
def pm_tasks(project_path: str | None = None, status: str | None = None,
             phase: str | None = None, priority: str | None = None,
             tag: str | None = None) -> list:
    """タスク一覧をフィルタ付きで返す。"""

@mcp.tool()
def pm_add_task(title: str, phase: str, priority: str = "P1",
                description: str = "", project_path: str | None = None,
                depends_on: list[str] | None = None, tags: list[str] | None = None,
                estimate_hours: float | None = None,
                acceptance_criteria: list[str] | None = None) -> dict:
    """新規タスクを追加。IDは自動採番（{PROJECT_PREFIX}-{連番}）。"""

@mcp.tool()
def pm_update_task(task_id: str, status: str | None = None,
                   priority: str | None = None, actual_hours: float | None = None,
                   notes: str | None = None, blocked_by: list[str] | None = None,
                   project_path: str | None = None) -> dict:
    """タスクのフィールドを更新。task_id は 'MYAPP-001' 形式。"""

@mcp.tool()
def pm_next(project_path: str | None = None, count: int = 3) -> list:
    """次にやるべきタスクを優先度・依存関係・フェーズから推薦。
    blocked なタスクは除外。depends_on が未完了のタスクも除外。"""

@mcp.tool()
def pm_blockers(project_path: str | None = None) -> list:
    """ブロッカーと blocked 状態のタスクを一覧。
    project_path=None の場合は全プロジェクトのブロッカーを集計。"""

@mcp.tool()
def pm_add_issue(parent_id: str, title: str, priority: str = "P1",
                 description: str = "", tags: list[str] | None = None,
                 project_path: str | None = None) -> dict:
    """完了済み/レビュー中のタスクに対してイシュー（子タスク）を追加。
    phase は親タスクを自動継承。parent_id で紐付け。
    親タスクが done の場合、自動で review に戻す。"""

# ─── 記録 ───

@mcp.tool()
def pm_log(entry: str, category: str = "progress",
           project_path: str | None = None) -> dict:
    """日次ログにエントリを追加。
    category: progress | decision | blocker | note | milestone"""

@mcp.tool()
def pm_add_decision(title: str, context: str, decision: str,
                    consequences_positive: list[str] | None = None,
                    consequences_negative: list[str] | None = None,
                    project_path: str | None = None) -> dict:
    """ADR（Architecture Decision Record）を追加。IDは自動採番。"""

# ─── 分析 ───

@mcp.tool()
def pm_velocity(project_path: str | None = None, weeks: int = 4) -> dict:
    """過去N週のベロシティ（完了タスク数/週）を計算。
    トレンド（上昇/下降/横ばい）も判定。"""

@mcp.tool()
def pm_risks(project_path: str | None = None) -> list:
    """リスク・課題を一覧。期限超過タスク、長期blocked、
    フェーズ遅延を自動検知して含める。"""

# ─── ビジュアライゼーション ───

@mcp.tool()
def pm_dashboard(project_path: str | None = None, format: str = "html") -> str:
    """ダッシュボードを生成。
    project_path 指定時: 単体プロジェクトビュー
    project_path=None: 全プロジェクト横断ビュー
    format: html | text"""

# ─── ディスカバリー & 管理 ───

@mcp.tool()
def pm_discover(scan_path: str = ".") -> list:
    """指定パス配下を再帰スキャンし、
    .pm/ を持つ未登録プロジェクトを自動でレジストリに追加。
    デフォルトはカレントディレクトリ。"""

@mcp.tool()
def pm_cleanup() -> dict:
    """レジストリのヘルスチェック。
    パスが存在しないプロジェクトを検出し、除去を提案。"""

@mcp.tool()
def pm_list() -> list:
    """レジストリに登録された全プロジェクトの一覧と概要を返す。"""

# ─── メンテナンス ───

@mcp.tool()
def pm_update_rules(project_path: str | None = None,
                    target: str = "auto",
                    dry_run: bool = False) -> dict:
    """CLAUDE.md / AGENTS.md の PM Server ルールセクションを最新テンプレートに更新（ADR-008、§6 参照）。
    target は {auto, all, claude-code, codex} の 4 値。auto は filesystem/marker/CLAUDECODE
    で検知された host のみ対象。マーカーで識別して PM Server セクションのみ置換。"""

@mcp.tool()
def pm_update_claudemd(project_path: str | None = None) -> dict:
    """[Legacy alias] pm_update_rules(target="claude-code") に delegate。
    v0.4.x の dict response shape を完全保持。v0.6.0 以降 DeprecationWarning を発火、
    v1.0.0 で削除予定（PMSERV-055）。"""
```

### 4.2 CLI エントリポイント

```python
# pm_server/__main__.py
import click

@click.group()
def cli():
    """pm-server — Claude Code Project Management"""
    pass

@cli.command()
def install():
    """Claude Code にMCPサーバーを登録する。
    内部で `claude mcp add --scope user pm-server -- <path> serve` を実行。"""

@cli.command()
def uninstall():
    """Claude Code からMCPサーバー登録を解除する。
    内部で `claude mcp remove pm-server --scope user` を実行。"""

@cli.command()
def serve():
    """MCP Server を起動（Claude Code から呼ばれる）。"""
    mcp.run(transport="stdio")

@cli.command()
@click.argument("scan_path", default=".")
def discover(scan_path):
    """ローカルプロジェクトをスキャンしてレジストリに登録。"""

@cli.command()
def status():
    """CLI から直接プロジェクト状態を確認（MCP不要）。"""

@cli.command()
def migrate():
    """pm-agent からの移行。旧 MCP 登録を解除し、新 pm-server として再登録。
    1. claude mcp remove pm-agent --scope user
    2. claude mcp add --scope user pm-server -- <path> serve
    3. ~/.pm/registry.yaml の整合性チェック
    4. CLAUDE.md 内の pm-agent 言及を警告"""

@cli.command("update-rules")
@click.option("--target", "-t",
              type=click.Choice(["auto", "all", "claude-code", "codex"]),
              default="auto", show_default=True,
              help="Which host's rule file to update.")
@click.option("--dry-run", is_flag=True,
              help="Show planned changes without writing.")
@click.option("--all", "all_projects", is_flag=True,
              help="Apply to every registered project.")
def update_rules_cmd(target, dry_run, all_projects):
    """CLAUDE.md / AGENTS.md の PM Server ルールを最新版に更新（ADR-008、multi-host 対応）。"""

@cli.command("update-claudemd")
@click.option("--all", "all_projects", is_flag=True, help="Update all registered projects.")
def update_claudemd_cmd(all_projects):
    """[Legacy] update-rules --target=claude-code と等価。v0.6.0 以降 deprecated。"""

if __name__ == "__main__":
    cli()
```

---

## 5. installer.py 設計

### 5.1 `claude mcp add` 方式（v0.4.x までの主 API、現在は legacy alias）

> **v0.5.0 以降の primary API は §5.2 のマルチホスト戦略**。本節は
> 後方互換 `install_mcp() / uninstall_mcp()` の歴史的記録。
> これらは PMSERV-055 により **v0.6.0 以降 `DeprecationWarning` を発火、v1.0.0 で削除** 予定。
> 内部的には `install_claude_code() / uninstall_claude_code()` (§5.2.3) に
> delegate するシンプルな wrapper として残置されている。

設計書 v0.2.0 では `~/.claude/settings.json` を直接編集する方式だったが、
実運用で Claude Code が MCP 設定を `~/.claude.json` で管理していることが判明。
公式の `claude mcp add` コマンド経由に修正済み。

```python
"""Claude Code MCP 設定の自動インストーラー。"""

import shutil
import subprocess
from pathlib import Path

def install_mcp():
    """pm-server を Claude Code の MCP サーバーとして登録。
    claude mcp add --scope user コマンドを使用。"""
    pm_server_path = shutil.which("pm-server")
    if pm_server_path is None:
        raise RuntimeError("pm-server command not found in PATH")
    
    try:
        subprocess.run(
            ["claude", "mcp", "add", "--scope", "user",
             "pm-server", "--", pm_server_path, "serve"],
            check=True, capture_output=True, text=True
        )
        print("✓ pm-server registered in Claude Code (user scope)")
        print("  Restart Claude Code to activate")
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"Failed to register MCP server: {e.stderr}")
    except FileNotFoundError:
        raise RuntimeError("'claude' command not found. Is Claude Code installed?")

def uninstall_mcp():
    """pm-server の MCP 登録を解除。"""
    try:
        subprocess.run(
            ["claude", "mcp", "remove", "pm-server", "--scope", "user"],
            check=True, capture_output=True, text=True
        )
        print("✓ pm-server unregistered from Claude Code")
    except subprocess.CalledProcessError:
        print("pm-server was not registered")
    except FileNotFoundError:
        print("'claude' command not found")

def migrate_from_pm_agent():
    """pm-agent から pm-server への移行。
    1. 旧 pm-agent の MCP 登録を解除
    2. 新 pm-server を MCP サーバーとして登録
    3. registry.yaml の整合性チェック
    4. 各プロジェクトの CLAUDE.md に pm-agent 言及があれば警告
    """
    # 1. 旧登録解除
    try:
        subprocess.run(
            ["claude", "mcp", "remove", "pm-agent", "--scope", "user"],
            check=True, capture_output=True, text=True
        )
        print("✓ Old pm-agent MCP registration removed")
    except subprocess.CalledProcessError:
        print("  pm-agent was not registered (skipping)")
    
    # 2. 新登録
    install_mcp()
    
    # 3. registry チェック
    registry_path = Path.home() / ".pm" / "registry.yaml"
    if registry_path.exists():
        print(f"✓ Registry at {registry_path} is intact")
    else:
        print("⚠ Registry not found at ~/.pm/registry.yaml")
    
    # 4. CLAUDE.md 警告
    if registry_path.exists():
        import yaml
        registry = yaml.safe_load(registry_path.read_text()) or {}
        projects = registry.get("projects", [])
        for proj in projects:
            claude_md = Path(proj["path"]) / "CLAUDE.md"
            if claude_md.exists():
                content = claude_md.read_text()
                if "pm-agent" in content or "pm_agent" in content:
                    print(f"⚠ {claude_md} contains 'pm-agent' references — please update manually")
    
    print("\n✓ Migration complete. Restart Claude Code to activate.")
```

### 5.2 Multi-Host インストーラー戦略 (ADR-007)

#### 5.2.1 背景

2026-04-27 の運用検証で、ユーザーが Codex CLI から pm-server を呼び出そうとした際、
`~/.codex/config.toml` に登録が無く起動できないことが判明 (実例: iterm-color プロジェクト、
手動追記で復旧済)。Claude Code (`~/.claude/`) と Codex CLI (`~/.codex/`) は
MCP 設定ストアが完全に分離しており、片方への登録はもう片方に伝播しない。

ADR-007 では、(A) ドキュメント追記のみ / (B) 別バイナリ pm-server-codex 切り出し /
(C) 既存 installer に target 引数追加 / (D) シェルスクリプトでの同時セットアップ
を比較検討した結果、**案 (C)** を採用。

#### 5.2.2 設計原則 (ADR-007 + ADR-008 共通)

1. **detect-then-patch**: 設定ファイルの存在を検知してから処理。Codex 未インストール環境では skip (副作用ゼロ)
2. **冪等性**: 既存セクションがあれば skip または command のみ更新。重複セクション化を防ぐ
3. **タイムスタンプ付きバックアップ**: 編集前に `~/.codex/config.toml.bak.<YYYYMMDD-HHMMSS>` を生成
4. **dry-run モード**: `--dry-run` で書き込まないプレビュー。実際の差分を表示
5. **絶対パス埋め込み**: `Path(sys.executable).parent / "pm-server"` を resolve した値を書き込む。Codex のサンドボックス (PATH を絞る) でも確実に起動できる
6. **対称的アンインストーラ**: `install` と同じ `target` 解決ロジックで逆操作

#### 5.2.3 関数構成 (現行 primary API)

```python
"""Multi-host MCP installer (Claude Code + Codex CLI)."""

# Claude Code 側 — claude mcp add subprocess 経由
def install_claude_code(*, dry_run: bool = False) -> InstallResult: ...
def uninstall_claude_code(*, dry_run: bool = False) -> InstallResult: ...

# Codex 側 — ~/.codex/config.toml を tomlkit で部分編集
def install_codex(*, dry_run: bool = False) -> InstallResult: ...
def uninstall_codex(*, dry_run: bool = False) -> InstallResult: ...

# オーケストレータ (target 解決 + 各 host 呼び出し + 失敗の構造化)
def install(target: str = "claude-code", *, dry_run: bool = False) -> InstallSummary: ...
def uninstall(target: str = "claude-code", *, dry_run: bool = False) -> InstallSummary: ...
```

`install` / `uninstall` の **デフォルトは `target="claude-code"`** であり、`auto` ではない。
これは v0.4.x からの後方互換のための保守的選択 — 既存ユーザーが `pm-server install` を打ったとき、
知らずに Codex 設定が編集されるのを防ぐ。multi-host 化したい場合は明示的に
`--target auto` / `--target all` / `--target codex` を渡す opt-in モデル。

#### 5.2.4 target 値の意味論

| target          | 検知ロジック                                    | 効果                                                    |
| --------------- | ---------------------------------------------- | ------------------------------------------------------ |
| `claude-code`   | 単一 host (CLI default for install/uninstall)   | Codex 側を一切 open しない                              |
| `codex`         | 単一 host                                       | Claude Code 側を一切 open しない                        |
| `auto`          | filesystem (`~/.codex/config.toml` 存在) で判定 | 検知された host のみ register                           |
| `all`           | 強制全 host                                     | Codex config 不在でも作成 (ホスト追加時に明示的に opt-in) |

関連コマンド `pm-server update-rules` (ADR-008、§5.3 参照) は **デフォルト `target=auto`** であり、
新規 API として最初から multi-host 検知を採用している。この非対称性は API design の
「新 API は理想的に、既存 API は保守的に」原則の実例。

#### 5.2.5 tomlkit を採用した理由

Codex CLI の `~/.codex/config.toml` にはユーザー手書き設定 (model 選択、tool 設定、
profile 等) が含まれる可能性が高く、編集時にコメント・順序・空行を保持することが必須要件。

| ライブラリ           | 機能                | コメント保持 | 備考                                  |
| -------------------- | ------------------- | ------------ | ------------------------------------- |
| `tomllib` (標準)     | read-only           | —            | Python 3.11+ 同梱、書き込み不可       |
| `tomli_w`            | write-only          | × (破壊)     | コメント・順序を再生成、整形が変わる  |
| **`tomlkit`** (採用) | round-trip 部分編集 | ✓ (保持)     | ~150KB pure python、新規依存に追加    |

#### 5.2.6 InstallResult / InstallSummary

```python
from dataclasses import dataclass, field

@dataclass(frozen=True)
class InstallResult:
    """Outcome of (un)registering pm-server in a single host."""

    target: str                        # "claude-code" or "codex"
    status: str                        # "installed" | "uninstalled" | "already_registered" | "skipped" | "failed"
    message: str                       # Human-readable detail (back-compat substrings preserved)
    backup_path: str | None = None     # Set only when host edits a file (Codex), None for CLI-driven hosts
    is_dry_run: bool = False           # True → status describes what *would* have happened


@dataclass(frozen=True)
class InstallSummary:
    """Aggregated results across hosts processed by install / uninstall."""

    results: list[InstallResult] = field(default_factory=list)

    @property
    def overall_status(self) -> str:
        """Priority: failed > installed > uninstalled > already_registered > skipped."""
        ...

    @property
    def message(self) -> str:
        """One ``[target] message`` line per result, joined by newlines."""
        ...
```

`overall_status` を property にしているのは **per-host 結果から派生する値** のため。
固定フィールドだと per-host 状態と矛盾するリスクがあり、状態の単一ソースを `results` に集約する設計。

#### 5.2.7 UI presentation の単一ソース

CLI 側 (`pm-server install`) は `_print_install_summary(summary)` ヘルパで出力を構築する。
このヘルパが **`[dry-run]` タグ表示と prefix (`✓` / `✗`) 判定の単一ソース** であり、
`InstallResult.message` には含めない。これにより:

- Python API 利用者は構造化された `InstallResult` を直接処理できる
- CLI 利用者は人間可読な 1 行/host のフォーマットを受け取る
- 将来 dashboard / HTML 出力を追加しても data layer に変更不要

```python
# __main__.py:_print_install_summary
prefix = "✗" if r.status == "failed" else "✓"
dry_tag = "[dry-run] " if r.is_dry_run else ""
click.echo(f"{prefix} {dry_tag}{r.target}: {r.message}")
```

`failed` 以外 (`installed`, `uninstalled`, `already_registered`, `skipped`) はすべて
**成功扱い** (`✓`)。これは「pm-server がやるべきことをやった or やる必要がなかった」を
肯定的に表示する設計判断。

#### 5.2.8 後方互換と廃止スケジュール

`install_mcp() / uninstall_mcp()` (§5.1) は v0.4.x 互換 alias として残置:

| Version  | Action                                                      |
| -------- | ----------------------------------------------------------- |
| v0.5.0   | alias 残置、新 API (`install` / `uninstall`) が primary    |
| v0.6.0   | `DeprecationWarning` 発火開始 (PMSERV-055)                  |
| v1.0.0   | alias 削除                                                  |

**ADR-007 amendment 候補**: CLAUDE.md backup を AGENTS.md と対称化 (PMSERV-058、当初 v0.6.0 を目標としたが現時点で deferred)。
v0.5.0 では Claude Code 側は `claude mcp add` 経由のため backup_path=None、
Codex 側のみ backup を作成する非対称が一時的に発生する。

#### 5.2.9 既知の制約

- **絶対パス埋め込み**: pyenv 等で Python 環境を切り替えると `which pm-server` の
  resolve 先が変わるため、再 install が必要
- **2 ファイル同期**: `~/.claude/settings.json` と `~/.codex/config.toml` の片方だけ
  ユーザーが手動編集した場合の差分検知ロジックは未実装 (将来課題)
- **`pm-server migrate` の Codex 側**: pm-agent → pm-server 移行ヘルパは Claude Code
  側のみ対象 (旧 pm-agent は Codex CLI に登録されることがそもそも無かったため scope 外)
- **`force_recreate` 等の高度なフラグは v0.5.0 では公開せず**、冪等性で十分なケースを
  優先。必要に応じて follow-up タスクで段階的に公開

---

## 6. rules.py 設計 (ADR-008)

### 6.1 背景と動機

2026-04-27 の運用検証で iterm-color プロジェクトを Codex CLI から `pm_init`
した際、`.pm/` と `CLAUDE.md` は作成されたが `AGENTS.md` (Codex の
プロジェクト指示ファイル) は一切更新されないことが判明。当時の
`pm_server/claudemd.py` は 236 行すべて "CLAUDE.md" にハードコードされており、
AGENTS.md の認知がコードベース内にゼロだった。

Codex は `AGENTS.md` を読んでプロジェクト指示を取得するため、Codex 利用者には
pm-server の運用ルール (`pm_status` 自動実行・タスク状態管理・ADR 提案 等の
行動規範) が一切伝わらない状態。先行事例として SynapticLedger は
`install.sh` で CLAUDE.md と AGENTS.md の両方を更新する設計を採用しており、
pm-server も同様の対称性を確保する必要があった。

### 6.2 モジュール構造の進化 (claudemd.py → rules.py)

| Version | Status                                                                       |
| ------- | ---------------------------------------------------------------------------- |
| v0.4.x  | `claudemd.py` が CLAUDE.md 専用                                              |
| v0.5.0  | `rules.py` に rename + 多ターゲット汎用化、`claudemd.py` は re-export shim   |
| v0.6.0  | `claudemd.py` shim に `DeprecationWarning` (PMSERV-055)                      |
| v1.0.0  | `claudemd.py` shim 削除                                                       |

shim は次のような 1 行モジュールとして残置:

```python
# src/pm_server/claudemd.py (v0.5.0 backward-compat shim)
from .rules import (  # noqa: F401
    TEMPLATE_VERSION,
    ensure_claudemd,
    update_claudemd,
    get_claudemd_status,
)
```

`tests/test_claudemd.py::TestShimIdentity` が `claudemd.X is rules.X` を
assert することで、import path の透過性を構造的に保証している
(PMSERV-044 cross-check R7)。

### 6.3 inject_pm_rules 統合 API

```python
def inject_pm_rules(
    project_root: Path,
    *,
    target: str = "auto",
    dry_run: bool = False,
) -> InjectSummary: ...
```

`target` の許容値は **{"auto", "all", "claude-code", "codex"}** の 4 値
(ADR-008 amendment A1/A2、`utils.TARGET_CHOICES`)。`auto` がデフォルトであり、
`installer.py` の `install` (default `claude-code`) と非対称になっている。
理由: `inject_pm_rules` / `pm_update_rules` は v0.5.0 で **新規追加** の API
なので v0.4.x ベースラインが存在せず、最初から multi-host 対応をデフォルト化
できる。一方 `install` は v0.4.x からの後方互換のため `claude-code` を保つ
("新 API は理想的に、既存 API は保守的に" 原則)。

### 6.4 host 検知ロジック (ADR-008 amendment A3)

`target="auto"` 時の `detect_hosts(project_root) -> tuple[list[str], str]` は
4 段階の signal を集約する:

1. **filesystem (primary)**: `shutil.which("claude")` の存在 → `claude-code`、
   `~/.codex/config.toml` の存在 → `codex`
2. **marker (positive signal)**: 既存 `CLAUDE.md` / `AGENTS.md` 内の
   `<!-- pm-server:begin -->` マーカー → 該当 host
3. **`CLAUDECODE` env (positive only)**: 環境変数 `CLAUDECODE=1` → `claude-code`
4. **tertiary fallback**: 何も検知されなかった場合 `["claude-code"]` を返す
   + `detection_source="fallback"` で UX 警告を促す

Codex CLI の env は **使わない**: Codex は `[shell_environment_policy]` で
env を strip する設計のため env 検知は不可能 (Domain Expert findings、
KR-002)。MCP `clientInfo` 経由検知も却下: FastMCP が public API として
expose していないため依存が unsafe。

戻り値の `detection_source` は以下のいずれか:

| 値                          | 意味                                              |
| --------------------------- | ------------------------------------------------- |
| `filesystem+marker+env`     | 上記 1-3 のいずれか以上で detect された           |
| `fallback`                  | 何も signal が無く `["claude-code"]` を返した     |
| `explicit`                  | `target != "auto"` で呼ばれ、検知をスキップした   |

CLI 側 (`pm-server update-rules`) は `detection_source == "fallback"` を検出すると
警告メッセージ "⚠ No host detected... pass --target=codex" を per-host 出力の
**前** に表示する (`__main__._print_inject_summary`)。

### 6.5 InjectResult / InjectSummary

```python
from dataclasses import dataclass, field
from pathlib import Path

@dataclass(frozen=True)
class InjectResult:
    """Outcome of injecting PM Server rules into a single rule file."""

    target_file: str                 # "CLAUDE.md" or "AGENTS.md"
    host: str                        # "claude-code" or "codex"
    status: str                      # "created" | "appended" | "updated" | "skipped" | "failed"
    message: str                     # Human-readable detail (no [dry-run] / backup path)
    backup_path: Path | None = None  # Set only for AGENTS.md edits in v0.5.0
    is_dry_run: bool = False


@dataclass(frozen=True)
class InjectSummary:
    """Aggregate outcome of an inject_pm_rules invocation."""

    results: list[InjectResult] = field(default_factory=list)
    detected_hosts: list[str] = field(default_factory=list)
    detection_source: str = "explicit"          # see §6.4
    created: list[str] = field(default_factory=list)   # newly created target files
    updated: list[str] = field(default_factory=list)   # existing files whose marker was rewritten
    overall_status: str = "skipped"             # priority: failed > skipped > updated > created
```

`message` フィールドには **`[dry-run]` タグや backup_path を含めない**
設計判断 (PMSERV-044 cross-check R6) — それらは `__main__._print_inject_summary`
が UI 層で唯一付加する。これにより Python API 利用者と CLI 利用者が同じ
data class を矛盾なく消費できる (PMSERV-039 L1 lesson の継承、§5.2.7 と同じ
パターン)。

### 6.6 マーカー規約とテンプレート version

CLAUDE.md / AGENTS.md ともに同一形式のマーカーで PM Server セクションを区切る:

```markdown
<!-- pm-server:begin v=7 -->
## PM Server 自動行動ルール（必ず従うこと）
... (テンプレート本文) ...
<!-- pm-server:end -->
```

- `v=N`: テンプレート version (v0.5.0 では `7`)
- `_replace_pm_section(path, content, template)` がマーカー区間のみを
  in-place 置換し、ユーザー手書き内容を完全保持
- `pm_init` 時はマーカー区間が無ければ末尾に追記 (`status="appended"`)、
  あれば既存内容として尊重 (`status="skipped"`)
- `pm_update_rules` (always-replace) と `pm_init` (skip-if-up-to-date) で
  異なるセマンティクスを持つため、関数を分けている

### 6.7 atomic write 共通化 (Amendment A6 / PMSERV-048 / ADR-011)

`rules.py` と `installer.py` の両方が `utils._atomic_write_text(path, content)`
を使用する。実装は `tempfile.mkstemp(dir=path.parent, suffix=".tmp")` ベース
で、固定 `.tmp` suffix の race 衝突 (cross-check R8 で発見した installer の
latent bug) を排除する。

PMSERV-048 (ADR-011) で `storage.py` の全 yaml 書き込みも `_atomic_write_text`
経由に統一した。さらに `storage._yaml_transaction(base_dir, filename, timeout=5)`
context manager を追加し、12 mutator (`add_*` / `update_*` 系) の read-modify-write
全体を `filelock` ベースの per-file lock で囲むことで、複数プロセス間の
**lost update** を構造的に排除した。lock ファイルは `.pm/.locks/{stem}.lock`
(global registry は `~/.pm/.locks/registry.lock`) に置かれ、`.pm/.locks/.gitignore`
を自動生成して commit 漏れを防ぐ。`workflow.advance_step` / `pm_init` の
project 初期保存 / `pm_cleanup` の registry mutation など mutator を介さない
直接書き込みも同じ transaction で wrap している。

```python
def _atomic_write_text(path: Path, content: str, *, encoding: str = "utf-8") -> None:
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding=encoding) as f:
            f.write(content)
        os.replace(tmp_name, path)
    except Exception:
        Path(tmp_name).unlink(missing_ok=True)
        raise
```

加えて、PMSERV-044 の実環境 smoke で発見された **umask permission bug** も
ここで解消されている: `mkstemp` のデフォルト 0600 mode が destination に
そのまま leak しないよう、書き込み後に umask を尊重した mode (`0o644 & ~umask`)
へ正規化する。

### 6.8 後方互換: claudemd.py shim と pm_update_claudemd alias

`pm_update_claudemd` MCP ツールは v0.5.0 で **legacy alias** として残置され、
内部的には `pm_update_rules(target="claude-code")` に delegate する。ただし
**戻り値の dict shape は v0.4.x のまま完全保持**:

```python
{
    "status": "updated",
    "message": "...",
    "template_version": 7,
    "before": {...},
    "after": {...},
}
```

`pm_update_rules` は新形式の per-host dict を返すが、`pm_update_claudemd` の
v0.4.x 利用者は新形式を観測しないため後方互換が確保される
(PMSERV-044 cross-check R3、`tests/test_server.py::test_pm_update_claudemd_returns_legacy_dict_shape`
で構造的に保証)。

### 6.9 既知の制約

- **AGENTS.md backup 非対称**: v0.5.0 では AGENTS.md のみ timestamped backup
  を作成、CLAUDE.md は v0.4.x 互換のため backup 非作成。PMSERV-058 として
  対称化予定 (当初 v0.6.0 を目標としたが現時点で deferred)
- **マーカー規約に従わない外部ツール**: 古い手書き `CLAUDE.md` (マーカー無し)
  はマーカー検知 (§6.4 の signal 2) に拾われない。filesystem 検知 (signal 1)
  でカバーされる前提
- **並行プロセスからの lost update**: PMSERV-048 (ADR-011) で `.pm/` 配下の
  全 yaml に対して filelock ベースの per-file 排他ロックを実装済。同一プロジェクトを
  Claude Code + Codex CLI など複数セッションが同時操作しても lost update は発生しない。
  `_atomic_write_text` 経由化で partial write 破損も構造的に排除。残課題: `pm_add_issue`
  (defect) の `add_task` + `update_task` 跨ぎ TOCTOU は PMSERV-065 で解消予定、
  `save_*` 関数群の private 化は PMSERV-067、`pm_discover` のバッチ register 最適化は
  PMSERV-066 で別途対応
- **SQLite memory.db の並行 reader/writer**: PMSERV-047 で `journal_mode=WAL` を
  採用済。writer が DB 全体ロックで他 reader を blocking する rollback journal の制約から、
  WAL の snapshot isolation に切り替えて reader/writer 並列化。`synchronous=NORMAL` で
  WAL の torn-write 回避保証下で fsync コスト削減、`busy_timeout=5000ms` で PMSERV-048
  filelock の 5s タイムアウトと統一。`_apply_pragmas()` を `MemoryStore.__init__` /
  `sync_to_global` / `search_global` の 3 接続箇所で呼ぶ。WAL モードは `.db` ヘッダに
  persistent なので既存ファイルは初回接続時に自動マイグレート、データ移行不要
- **テンプレート version の bump**: `TEMPLATE_VERSION = 7` を変更すると
  既存ユーザーの CLAUDE.md / AGENTS.md が次回 `pm_status` で自動更新を促される。
  v0.5.0 では v7 据え置きのため、v0.4.x からアップグレードしてもユーザーの
  指示ファイルは破壊されない

---

## 7. discovery.py 設計

```python
"""プロジェクトの自動検出と情報推定。"""

from pathlib import Path
import tomllib, json, subprocess

def detect_project_info(project_path: Path) -> dict:
    """プロジェクトディレクトリからメタ情報を自動推定。
    package.json, pyproject.toml, Cargo.toml, .git, README.md を読む。"""
    info = {
        "name": project_path.name,
        "display_name": project_path.name,
        "version": "0.1.0",
        "repository": None,
        "description": "",
    }
    
    # package.json (Node.js)
    pkg_json = project_path / "package.json"
    if pkg_json.exists():
        pkg = json.loads(pkg_json.read_text())
        info["name"] = pkg.get("name", info["name"])
        info["version"] = pkg.get("version", info["version"])
        info["description"] = pkg.get("description", "")
    
    # pyproject.toml (Python)
    pyproject = project_path / "pyproject.toml"
    if pyproject.exists():
        with open(pyproject, "rb") as f:
            pyp = tomllib.load(f)
        proj = pyp.get("project", {})
        info["name"] = proj.get("name", info["name"])
        info["version"] = proj.get("version", info["version"])
        info["description"] = proj.get("description", "")
    
    # Cargo.toml (Rust)
    cargo_toml = project_path / "Cargo.toml"
    if cargo_toml.exists():
        with open(cargo_toml, "rb") as f:
            cargo = tomllib.load(f)
        pkg = cargo.get("package", cargo.get("workspace", {}).get("package", {}))
        info["name"] = pkg.get("name", info["name"])
        info["version"] = pkg.get("version", info["version"])
        info["description"] = pkg.get("description", "")
    
    # Git remote URL
    try:
        result = subprocess.run(
            ["git", "-C", str(project_path), "remote", "get-url", "origin"],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            info["repository"] = result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    
    # README.md
    readme = project_path / "README.md"
    if readme.exists() and not info["description"]:
        lines = readme.read_text().splitlines()
        for line in lines:
            stripped = line.strip().lstrip("# ").strip()
            if stripped and not stripped.startswith("!") and len(stripped) > 10:
                info["description"] = stripped[:200]
                break
    
    return info


def discover_projects(scan_path: Path) -> list[dict]:
    """指定パス配下を再帰スキャンし、.pm/ を持つプロジェクトを発見。
    デフォルトはカレントディレクトリ（v0.2.0 ではホームディレクトリだったが修正）。"""
    found = []
    scan_path = scan_path.expanduser().resolve()
    for pm_dir in scan_path.rglob(".pm"):
        if pm_dir.is_dir() and (pm_dir / "project.yaml").exists():
            project_path = pm_dir.parent
            found.append({"path": str(project_path), "name": project_path.name})
    return found
```

---

## 8. ダッシュボード仕様

### 8.1 HTML ダッシュボード

Chart.js CDN + ダークテーマ（Jinja2 テンプレート）。

**単体プロジェクトビュー:**
- プロジェクトヘッダー（名前、ステータス、全体進捗バー）
- フェーズ進捗テーブル（各フェーズの完了率）
- カンバンボード（todo / in_progress / review / done のカード）
- ベロシティチャート（棒グラフ、週次）
- ブロッカー・リスクセクション（赤ハイライト）
- 直近アクティビティ（日次ログの最新5件）
- ADR 一覧

**全プロジェクト横断ビュー:**
- プロジェクト一覧テーブル（名前、ステータス、進捗率、健康度アイコン）
- グローバル集計（アクティブタスク数、ブロッカー数、今週完了数）
- Attention Required セクション（期限超過、長期ブロック）
- プロジェクト別ミニチャート

### 8.2 テキストフォールバック

`format="text"` 指定時は ASCII アートで簡易表示。

---

## 9. パッケージ構成

```
pm-server/                         # ← pm-agent から改名
├── pyproject.toml
├── README.md                      # 英語版
├── README.ja.md                   # 日本語版
├── LICENSE (MIT)
├── CHANGELOG.md
├── CLAUDE.md
├── .github/
│   └── workflows/
│       ├── test.yml
│       └── publish.yml
├── src/
│   └── pm_server/                 # ← pm_agent から改名
│       ├── __init__.py
│       ├── __main__.py            # CLI (click)
│       ├── server.py              # FastMCP Server (16ツール)
│       ├── models.py              # Pydantic v2 (12モデル, 9 Enum)
│       ├── storage.py             # YAML CRUD
│       ├── installer.py           # claude mcp add ラッパー + migrate
│       ├── discovery.py           # プロジェクト情報自動推定
│       ├── dashboard.py           # HTML/テキスト ダッシュボード
│       ├── velocity.py            # ベロシティ・リスク検知
│       ├── utils.py               # パス解決・ID生成・集計
│       └── templates/
│           ├── dashboard_single.html
│           └── dashboard_portfolio.html
├── skill/
│   └── SKILL.md
├── tests/
│   ├── conftest.py                # registry 隔離フィクスチャ
│   ├── test_models.py
│   ├── test_storage.py
│   ├── test_server.py
│   ├── test_installer.py          # subprocess mock
│   ├── test_discovery.py
│   ├── test_dashboard.py
│   └── test_velocity.py
└── docs/
    ├── design.md                  # この設計書
    ├── status.md
    └── handoff.md
```

### 9.1 pyproject.toml

```toml
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "pm-server"
version = "0.2.0"
description = "Project management MCP Server for Claude Code — track tasks, visualize progress, manage decisions"
readme = "README.md"
license = "MIT"
requires-python = ">=3.11"
authors = [
    { name = "Shinichi Nakazato", email = "..." }
]
keywords = ["claude-code", "project-management", "mcp", "mcp-server"]
classifiers = [
    "Development Status :: 3 - Alpha",
    "License :: OSI Approved :: MIT License",
    "Programming Language :: Python :: 3.11",
    "Programming Language :: Python :: 3.12",
    "Programming Language :: Python :: 3.13",
]
dependencies = [
    "fastmcp>=2.0",
    "pydantic>=2.0",
    "pyyaml>=6.0",
    "click>=8.0",
    "jinja2>=3.0",
]

[project.scripts]
pm-server = "pm_server.__main__:cli"

[project.urls]
Homepage = "https://github.com/flc-design/pm-server"
Repository = "https://github.com/flc-design/pm-server"
Issues = "https://github.com/flc-design/pm-server/issues"
```

---

## 10. 実装計画と現在の状態

### Phase 1〜4: 完了済み ✅

Memory Layer 基盤、セッション継続、横断検索・自動化、運用ツール。
全23 MCP ツール、260+テスト。PyPI v0.3.3 公開済み。

### Phase 5: Workflow Engine ✅

テンプレートベースのワークフローエンジン。状態マシンでステップ進行を管理。

- ワークフローテンプレート（Discovery / Development）
- ループ構造（brainstorm loop_group）
- ユーザーゲート（gate: user_approval）
- ワークフロー連鎖（chain_to: development）
- 10タスク全完了

### Phase 6: Knowledge Records ✅

カジュアルな Memory と フォーマルな ADR の間を埋める構造化知識レイヤー。

- 9カテゴリ（research, market, spike, requirement, constraint, tradeoff, risk_analysis, spec, api_design）
- ワークフローステップとの produces/consumes 連携
- pm_record / pm_knowledge ツール
- 6タスク全完了

### Phase 7: Super Research & Skill エコシステム ✅

- Super Research スキル定義（skill/super-research/SKILL.md）
  - 3並列エージェント（Domain Expert, Critical Analyst, Lateral Thinker）
  - Depth Check（6次元品質評価）
  - Fact Check + Cross-Check
  - 3モード（quick / standard / full）
- super-research ワークフローテンプレート
- ダッシュボード拡張（ワークフロー進捗 + 知識マップ）
- ドキュメント更新（本文書 v0.5.0）
- 4タスク全完了

### 現在の規模

- **32 MCP ツール** (server.py)
- **17 Pydantic モデル + 15 Enum** (models.py)
- **406+ テスト** (pytest)
- **3 ワークフローテンプレート** (discovery / development / super-research)
- **1 スキル定義** (super-research)

---

## 11. 設計原則

1. **Zero Configuration** — `pip install` + `pm-server install` で完了
2. **Auto-everything** — 登録・検出・推定は全自動
3. **Git-friendly** — plain text YAML、git diff で追跡可能
4. **Human-readable** — YAML を手動編集しても壊れない
5. **AI-native** — Claude Code が自然に読み書きできるフォーマット
6. **Visual-first** — 数字よりグラフ、テキストよりカンバン
7. **Incremental** — 最小限から始めて段階的に機能追加
8. **Non-invasive** — プロジェクトの構造を変更しない（.pm/ を追加するだけ）
