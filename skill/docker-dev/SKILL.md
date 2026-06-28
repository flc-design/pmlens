---
name: docker-dev
description: >
  Docker Dev — pmlens のグローバル副作用（installer/hooks/migrate が
  ~/.claude・~/.codex・~/.pm を書き換える）を、使い捨て HOME のコンテナ内で
  安全に開発・検証するスキル。「docker開発」「コンテナでテスト」「dev環境」
  「サンドボックス」「隔離環境」「devcontainer」「make dev」「install/migrate を試す」
  のキーワードでトリガーすること。道具(production)=ホスト安定版 / 開発(dev)=隔離
  コンテナ、の分離を徹底する（PMSERV-140 / ADR-036）。
---

# Docker Dev Skill

## Overview / 概要

pmlens は「ホストのグローバル設定（`~/.claude/settings.json`・`~/.codex/config.toml`・
`~/.pm/`）を書き換える」のが機能の一部。だから editable 一本で開発すると、開発＝全
プロジェクトが共有する MCP サーバ（道具）を直接破壊し得る。本スキルは **道具(production)
= ホストの安定版 pmlens** と **開発(dev) = 使い捨て HOME の Docker コンテナ** を分離し、
危険な副作用パスをホストを汚さずに検証する手順を提供する。

## When to use / 使うとき

- `installer.py` / `hooks.py` / `migrate_*` など **グローバル設定を書き換えるコード**を変更/デバッグするとき
- `pmlens install` / `migrate-from-pm-server` / hook install を**実機で動かして確認したい**とき
- リリース前に「ホストの本物の `~/.claude` を汚さずに」install/migrate の挙動を見たいとき

> 純粋なロジック変更（global 副作用なし）はホストの repo venv + `pytest` で十分。
> 本スキルは「副作用がホスト HOME に及ぶ変更」に対する隔離レイヤー。

## The split / 道具と開発の分離

| | 実体 | 用途 |
|---|---|---|
| 道具 (production) | ホストに `pipx install pmlens`（または専用 venv）→ user-scope 登録 | 全プロジェクトの PM 運用 |
| 開発 (dev) | Docker コンテナの使い捨て HOME + bind-mount した repo の editable | コード変更・テスト・install/migrate 検証 |

両者は **HOME を共有しない**。これが安全の核心。

## Commands / コマンド（Makefile）

```bash
make dev-build     # dev コンテナイメージをビルド（初回 / Dockerfile 変更後）
make dev-test      # 隔離コンテナ内で全テスト実行
make dev-lint      # コンテナ内で ruff check + format --check
make dev-shell     # 隔離サンドボックスに対話シェルで入る
make dev-sandbox   # installer を使い捨て HOME に dry-run（ホスト無傷を実証）
make dev-clean     # 使い捨て HOME / venv volume を破棄（サンドボックス初期化）
```

VS Code / devcontainer CLI 派は `.devcontainer/devcontainer.json` がそのまま使える
（`postCreateCommand` が editable install を実行）。

## Safety invariants / 安全不変条件

1. **ホストの dotfile を絶対 bind-mount しない**（`~/.claude`・`~/.codex`・`~/.ssh` 等）。mount するのは repo ディレクトリのみ。
2. **HOME はコンテナローカル**（`/home/pmdev`、named volume）。install/hook はそこへ書く。
3. **非 root ユーザー**で動かす。
4. `.venv` はコンテナ側 named volume（ホストの `.venv` で上書きしない／`.dockerignore` で除外）。
5. install/migrate を試した後は `make dev-clean` で HOME を初期化すれば毎回まっさらから再現できる。

## Exercising global side-effects safely / 副作用の安全な検証

```bash
make dev-shell
# コンテナ内（HOME=/home/pmdev は使い捨て）:
python -c "from pmlens import installer; print(installer.install(target='all', dry_run=True).message)"
python -m pmlens --help
# 生成された設定を覗く:
ls -la ~/.claude ~/.codex ~/.pm 2>/dev/null
```

ホストの本物の `~/.claude/settings.json` は一切変更されない。これが本スキルの提供する保証。

## Integration / 連携

- **workflow**: `Docker Development`（`docker-dev.yaml`）の各ステップが本スキルを `skill_hint` で参照する。
- **agent**: `pmlens-docker-dev` サブエージェントがビルド/テスト/install 検証を駆動する。
- **ADR**: ADR-036（道具=ホスト安定版 / 開発=隔離 HOME コンテナ）。
- **task**: PMSERV-140。
