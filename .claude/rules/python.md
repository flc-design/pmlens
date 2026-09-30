---
paths:
  - "src/**/*.py"
  - "tests/**/*.py"
---

# Python コーディング規約（pmlens）

## Python スタイル
- Python 3.11+ の機能を積極利用（`str | None`, `match-case` 等）
- 型ヒントを全関数に記述
- docstring は Google style
- フォーマッター: ruff
- リンター: ruff
- テスト: pytest

## 命名規則
- モジュール: snake_case
- クラス: PascalCase
- 関数/変数: snake_case
- 定数: UPPER_SNAKE_CASE
- MCP ツール名: `pm_` プレフィクス（例: `pm_status`, `pm_add_task`）

## エラーハンドリング
- カスタム例外クラス: `PmServerError`, `ProjectNotFoundError`, `TaskNotFoundError`, `DecisionNotFoundError`
- MCP ツールはエラー時に明確なメッセージを返す
- YAML パースエラーは `PmServerError` にラップして伝播

## YAML 規約
- `pyyaml` の `safe_load` / `safe_dump` のみ使用
- 出力は `default_flow_style=False`, `allow_unicode=True`, `sort_keys=False`
- ファイル先頭にコメントヘッダーを付与

## テスト規約
- 各モジュールに対応する test ファイルを作成
- `tmp_path` fixture で一時ディレクトリを使用
- 正常系・異常系・エッジケースを網羅
- テストデータは conftest.py に fixture として定義
