"""Lens モード (PMSERV-079, WF-025) 回帰テスト.

WF-024 Cross-Check (memory:134) と WF-025 Cross-Check (memory:137) で
特定された Phase 1 R1 (RO ツール allowlist + mutator/subprocess 到達不能)
の不変条件を、reload-based fixture で検証する。

検証対象:
  - R1: Lens モードで mutator/subprocess 経路ツールが MCP に登録されない
  - B-1: pm_knowledge (multi-action with update mutator) は Lens 外、
         代わりに pm_knowledge_query (read-only) が登録される
  - B-2: pm_session_summary(action=save) の到達不能 + pm_recall 無書込のペアテスト
  - I-2: pm_memory_cleanup(dry_run=False) の到達不能
  - I-3: _memory_stores キャッシュクリア fixture
  - N-1: RO_ALLOWLIST 定数化と test からの import 検証
"""

from __future__ import annotations

import importlib

import pytest

import pm_server.server

# ─── 既知の mutator 集合 (Lens 排除対象) ──────────────────
# memory:134 + B-1 (pm_knowledge を multi-action mutator として扱う) より。

MUTATOR_TOOLS: frozenset[str] = frozenset(
    {
        "pm_init",
        "pm_add_task",
        "pm_update_task",
        "pm_add_issue",
        "pm_remember",
        "pm_session_summary",
        "pm_memory_cleanup",
        "pm_log",
        "pm_add_decision",
        "pm_discover",
        "pm_cleanup",
        "pm_update_claudemd",
        "pm_update_rules",
        "pm_record",
        "pm_knowledge",
        "pm_workflow_start",
        "pm_workflow_advance",
        "pm_workflow_abandon",
    }
)


# ─── Fixtures ─────────────────────────────────────────


def _reload_server(env_overrides: dict[str, str | None]) -> None:
    """server モジュールを env 反映の上でリロードする (I-3 対応).

    PM_LENS は import 時に評価されるため、テストごとにリロードして
    REGISTERED_TOOLS / RO_ALLOWLIST の状態をフレッシュにする。
    """
    import os

    for key, value in env_overrides.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    # I-3: モジュール cache クリア (RO/RW 切替で stale store が再利用されない)
    pm_server.server._memory_stores.clear()
    pm_server.server.REGISTERED_TOOLS.clear()
    importlib.reload(pm_server.server)


@pytest.fixture
def lens_server(monkeypatch):
    """PM_LENS=1 を有効化して server を reload する."""
    monkeypatch.setenv("PM_LENS", "1")
    _reload_server({"PM_LENS": "1"})
    yield pm_server.server
    # teardown: PM_LENS を外して元の状態に戻す
    monkeypatch.delenv("PM_LENS", raising=False)
    _reload_server({"PM_LENS": None})


@pytest.fixture
def normal_server(monkeypatch):
    """PM_LENS 無効化を保証して server を reload する."""
    monkeypatch.delenv("PM_LENS", raising=False)
    _reload_server({"PM_LENS": None})
    yield pm_server.server


# ─── R1: Lens モードで mutator が登録されない ────────────


def test_lens_mode_excludes_all_mutators(lens_server):
    """Lens モードで mutator ツールが MCP に一切登録されないこと."""
    leaked = MUTATOR_TOOLS & lens_server.REGISTERED_TOOLS
    assert not leaked, f"Mutator tools leaked into Lens mode: {sorted(leaked)}"


def test_lens_mode_includes_full_ro_allowlist(lens_server):
    """Lens モードで RO_ALLOWLIST のツールが全て登録されること."""
    missing = lens_server.RO_ALLOWLIST - lens_server.REGISTERED_TOOLS
    assert not missing, f"RO tools missing from Lens registration: {sorted(missing)}"


def test_lens_mode_registered_equals_allowlist(lens_server):
    """Lens モードで登録ツール集合が RO_ALLOWLIST と厳密一致すること.

    N-1: RO_ALLOWLIST が test から import 可能で、登録ツールとの一致を assert できる.
    """
    assert lens_server.REGISTERED_TOOLS == set(lens_server.RO_ALLOWLIST)


def test_lens_mode_enabled_flag(lens_server):
    """PM_LENS_ENABLED が True になっていること."""
    assert lens_server.PM_LENS_ENABLED is True


# ─── 既定モード (Lens OFF) の回帰テスト ─────────────────


def test_default_mode_registers_all_tools(normal_server):
    """PM_LENS 未設定で全てのツール (RO + mutator) が登録されること."""
    expected = MUTATOR_TOOLS | normal_server.RO_ALLOWLIST
    missing = expected - normal_server.REGISTERED_TOOLS
    assert not missing, f"Tools missing from default registration: {sorted(missing)}"


def test_default_mode_disabled_flag(normal_server):
    """PM_LENS 未設定で PM_LENS_ENABLED=False."""
    assert normal_server.PM_LENS_ENABLED is False


# ─── B-1: pm_knowledge multi-action 露出問題 ────────────


def test_lens_excludes_pm_knowledge(lens_server):
    """B-1: pm_knowledge は action=update mutator を内包するため Lens 外."""
    assert "pm_knowledge" not in lens_server.REGISTERED_TOOLS


def test_lens_includes_pm_knowledge_query(lens_server):
    """B-1: 新規 pm_knowledge_query (read-only) は Lens 登録される."""
    assert "pm_knowledge_query" in lens_server.REGISTERED_TOOLS


def test_pm_knowledge_query_rejects_update(normal_server, tmp_path):
    """pm_knowledge_query は action=update を拒否する (read-only 契約)."""
    # tmp_path に最小の .pm を作って読み込み可能にする
    pm_path = tmp_path / ".pm"
    pm_path.mkdir()
    (pm_path / "knowledge.yaml").write_text("- []\n", encoding="utf-8")

    result = normal_server.pm_knowledge_query(
        action="update",
        record_id="K-001",
        project_path=str(tmp_path),
    )
    assert result.get("status") == "error"
    assert "read-only" in result.get("message", "").lower()


# ─── B-2: invariant lock test ペア ───────────────────────


def test_lens_excludes_pm_session_summary(lens_server):
    """B-2: pm_session_summary (save action mutator) は Lens 外."""
    assert "pm_session_summary" not in lens_server.REGISTERED_TOOLS


def test_lens_includes_pm_recall(lens_server):
    """B-2 pair: pm_recall (read-only) は Lens 内."""
    assert "pm_recall" in lens_server.REGISTERED_TOOLS


def test_pm_recall_is_read_only(normal_server, tmp_path):
    """B-2: pm_recall は永続状態を変更しないこと (logical invariant lock).

    繰り返し呼び出して memory_stats が変わらないことで read-only 性を保証する。
    将来 pm_recall に save 経路 (例えば access log 記録) が追加された場合、
    memory count の変化として silent ADR-015 違反を検出できる。

    制約: CPython の sqlite3.Cursor.execute は C 実装の immutable type で
    monkeypatch 不可なため、SQL 文レベルの spy ではなく論理不変条件で検証する。
    """
    pm_path = tmp_path / ".pm"
    pm_path.mkdir()
    (pm_path / "daily").mkdir()

    # Pre-warm: schema 初期化を完了させる
    normal_server.pm_recall(project_path=str(tmp_path))

    stats_before = normal_server.pm_memory_stats(project_path=str(tmp_path))

    # 複数回 recall を呼んで状態が変わらないこと
    for _ in range(3):
        normal_server.pm_recall(project_path=str(tmp_path))

    stats_after = normal_server.pm_memory_stats(project_path=str(tmp_path))

    assert stats_before == stats_after, (
        f"pm_recall mutated memory state: before={stats_before}, after={stats_after}"
    )


# ─── I-2: pm_memory_cleanup multi-mode 排除 ─────────────


def test_lens_excludes_pm_memory_cleanup(lens_server):
    """I-2: pm_memory_cleanup (dry_run=False で mutator) は Lens 外."""
    assert "pm_memory_cleanup" not in lens_server.REGISTERED_TOOLS


# ─── subprocess 経路の到達不能テスト (ADR-015 / PMSERV-077 不変条件) ──


def test_lens_excludes_subprocess_invoking_tools(lens_server):
    """Lens モードで installer.py subprocess 経路を持つツールが排除されること.

    installer.py:137/159/218/240/585 の subprocess 5箇所を呼び得る経路
    (pm_update_claudemd / pm_init 系) が Lens に到達できないことを確認する.
    """
    subprocess_paths = {"pm_update_claudemd", "pm_init"}
    leaked = subprocess_paths & lens_server.REGISTERED_TOOLS
    assert not leaked, f"Subprocess-invoking tools leaked: {sorted(leaked)}"
