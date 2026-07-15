"""Golden-query baseline for Japanese FTS5 recall + LIKE fallback.

PMSERV-143 (ADR-039 T5): this file locks in the *currently measured* behaviour
of :meth:`MemoryStore.search_ex` against a small, realistic Japanese memory
corpus. It is NOT a target/spec test — ``KNOWN_BASELINE`` records what the
unicode61 FTS5 tokenizer + LIKE fallback actually do today, per
docs/reports/ja-fts-baseline.md. If a future change measurably improves (or
regresses) a query's result, update ``KNOWN_BASELINE`` to match the new
reality and say so in the report — do not silently "fix" the test to keep it
green without updating the report.

NOTE on portability: FTS5's unicode61 tokenizer segmentation has shifted
across SQLite releases. These numbers were measured against
``sqlite3.sqlite_version`` printed by ``test_baseline_records_sqlite_version``
below. If your environment reports a different version and this suite fails,
re-measure (see docs/reports/ja-fts-baseline.md for the measurement method)
and update both KNOWN_BASELINE and the report — do not just widen the
assertions.
"""

from __future__ import annotations

import sqlite3

import pytest

from pmlens.memory import MemoryStore
from pmlens.models import Memory, MemoryType

# ─── Golden corpus ──────────────────────────────────────────────
#
# ~15 Japanese memory entries covering the real failure queries documented in
# docs/issues/ISSUE_desktop-outbox-one-way.md (P5: "経営戦略", "2つのエンジン",
# "引き継ぎ"), plus compound-word, kana-mixed, and English/Japanese-mixed
# content. Each tuple is (content, tags).

GOLDEN_CORPUS: list[tuple[str, list[str]]] = [
    (
        "経営戦略セッションでドッグフーディングを実施し、実地テストで引き継ぎに失敗した",
        ["経営", "戦略"],
    ),
    (
        "2つのエンジン（YAML観測ログとSQLite全文検索）を組み合わせたハイブリッド設計を採用する",
        ["アーキテクチャ", "設計"],
    ),
    (
        "Desktop間の文脈引き継ぎがFTS5の日本語検索で失敗するケースを確認した",
        ["バグ", "FTS"],
    ),
    (
        "pm_recallのブランチ単位セッション継続機能をADR-028として記録した",
        ["ADR", "recall"],
    ),
    (
        "SQLiteのFTS5はunicode61トークナイザーだとCJKの複合語検索に弱い",
        ["SQLite", "FTS5"],
    ),
    (
        "経営戦略とプロダクトロードマップのすり合わせミーティングを実施した",
        ["経営", "ロードマップ"],
    ),
    (
        "outboxのread-pure化によりdesktop.dbの暗黙生成を防止した",
        ["outbox", "設計"],
    ),
    (
        "かな交じりのクエリ「にっぽん」や「ニホン」の表記ゆれで検索がヒットしないことがある",
        ["かな", "表記ゆれ"],
    ),
    (
        "英語と日本語が混在するメモ: refactor完了、テストはpytestで実行した",
        ["refactor", "test"],
    ),
    (
        "引き継ぎドキュメントをREADME.jaに追記した",
        ["引き継ぎ", "ドキュメント"],
    ),
    (
        "複合語「機械学習」「自然言語処理」を含むタグ付けルールを整理した",
        ["機械学習", "NLP"],
    ),
    (
        "デスクトップアプリとCLIの二重運用について意思決定を行った",
        ["デスクトップ", "CLI"],
    ),
    (
        "「2つのエンジン」構想はYAML+SQLiteのハイブリッドアーキテクチャの通称である",
        ["アーキテクチャ"],
    ),
    (
        "search_exのLIKEフォールバック実装でPMSERV-143を完了させた",
        ["PMSERV-143", "search"],
    ),
    (
        "プロジェクト横断のcross_project検索はv1スコープ外として別issue化した",
        ["cross_project", "scope"],
    ),
]

# ─── Known baseline (measured, not aspirational) ────────────────
#
# Measured against sqlite3.sqlite_version == "3.51.0" (see
# test_baseline_records_sqlite_version). For each query: the strategy
# search_ex actually took ("fts" or "like_fallback"), whether it produced any
# hit, and the exact result count. Re-measure and update if your environment's
# sqlite3.sqlite_version differs and this file's assertions fail.
#
# Includes deliberate true-negative controls (queries with no matching
# content at all, and a multi-word AND-style query the LIKE fallback cannot
# satisfy since it only does a literal substring match of the whole query
# string) to keep the recall rate honest rather than artificially 100%.

KNOWN_BASELINE: dict[str, dict[str, object]] = {
    "経営戦略": {"strategy": "like_fallback", "hit": True, "count": 2},
    "2つのエンジン": {"strategy": "fts", "hit": True, "count": 2},
    "引き継ぎ": {"strategy": "fts", "hit": True, "count": 1},
    "FTS5": {"strategy": "fts", "hit": True, "count": 1},
    "unicode61": {"strategy": "like_fallback", "hit": True, "count": 1},
    "機械学習": {"strategy": "fts", "hit": True, "count": 1},
    "デスクトップ": {"strategy": "fts", "hit": True, "count": 1},
    "PMSERV-143": {"strategy": "fts", "hit": True, "count": 1},
    "cross_project": {"strategy": "fts", "hit": True, "count": 1},
    "にっぽん": {"strategy": "fts", "hit": True, "count": 1},
    "アーキテクチャ": {"strategy": "fts", "hit": True, "count": 2},
    "outbox": {"strategy": "fts", "hit": True, "count": 1},
    "refactor": {"strategy": "fts", "hit": True, "count": 1},
    "ADR-028": {"strategy": "like_fallback", "hit": True, "count": 1},
    "ロードマップ": {"strategy": "fts", "hit": True, "count": 1},
    "自然言語処理": {"strategy": "fts", "hit": True, "count": 1},
    "表記ゆれ": {"strategy": "fts", "hit": True, "count": 1},
    "ハイブリッド設計": {"strategy": "like_fallback", "hit": True, "count": 1},
    "セッション継続": {"strategy": "like_fallback", "hit": True, "count": 1},
    # True negatives: LIKE fallback runs (FTS found nothing) but still misses.
    "経営戦略 2つのエンジン": {"strategy": "like_fallback", "hit": False, "count": 0},
    "検索エンジン": {"strategy": "like_fallback", "hit": False, "count": 0},
    "存在しない用語XYZ": {"strategy": "like_fallback", "hit": False, "count": 0},
    "OAuth認証": {"strategy": "like_fallback", "hit": False, "count": 0},
}


@pytest.fixture
def golden_store(memory_store: MemoryStore) -> MemoryStore:
    """memory_store pre-loaded with GOLDEN_CORPUS."""
    for content, tags in GOLDEN_CORPUS:
        mem = Memory(
            session_id="sess-golden",
            type=MemoryType.OBSERVATION,
            content=content,
            tags=tags,
            project="pm-server",
        )
        memory_store.save(mem)
    return memory_store


class TestJapaneseGoldenBaseline:
    def test_baseline_records_sqlite_version(self):
        """Document the SQLite version these numbers were measured against.

        Not a real assertion beyond "it runs" — a loud signpost. If this
        environment's sqlite3.sqlite_version differs from 3.51.0, the
        parametrized baseline test below may need re-measuring (see the
        module docstring and docs/reports/ja-fts-baseline.md).
        """
        assert isinstance(sqlite3.sqlite_version, str)

    @pytest.mark.parametrize("query", sorted(KNOWN_BASELINE))
    def test_matches_known_baseline(self, golden_store: MemoryStore, query: str):
        expected = KNOWN_BASELINE[query]
        results, strategy = golden_store.search_ex(query, limit=5)
        assert strategy == expected["strategy"], (
            f"query={query!r}: strategy drifted from baseline "
            f"({expected['strategy']!r} -> {strategy!r}); if this is a "
            f"genuine improvement, update KNOWN_BASELINE and "
            f"docs/reports/ja-fts-baseline.md, don't just widen the assert"
        )
        assert (len(results) > 0) == expected["hit"], (
            f"query={query!r}: hit/miss drifted from baseline"
        )
        assert len(results) == expected["count"], (
            f"query={query!r}: result count drifted from baseline "
            f"({expected['count']} -> {len(results)})"
        )

    def test_overall_recall_rate_matches_report(self, golden_store: MemoryStore):
        """Cross-check the aggregate recall rate reported in the baseline doc.

        18/23 queries hit under the combined fts+like_fallback strategy
        (recall ~78%); pure-FTS-only recall (queries where strategy=="fts")
        is 14/23 (~61%) — see docs/reports/ja-fts-baseline.md for the full
        breakdown and the delta the LIKE fallback provides.
        """
        total = len(KNOWN_BASELINE)
        hits = 0
        fts_only_hits = 0
        for query, expected in KNOWN_BASELINE.items():
            results, strategy = golden_store.search_ex(query, limit=5)
            if results:
                hits += 1
            if strategy == "fts" and results:
                fts_only_hits += 1
            assert strategy == expected["strategy"]

        assert total == 23
        assert hits == 19
        assert fts_only_hits == 14


class TestSearchExDelegation:
    """search() must stay a thin, signature-unchanged delegation to search_ex()."""

    def test_search_returns_same_list_as_search_ex(self, golden_store: MemoryStore):
        ex_results, _strategy = golden_store.search_ex("経営戦略", limit=5)
        plain_results = golden_store.search("経営戦略", limit=5)
        assert [m.id for m in plain_results] == [m.id for m in ex_results]

    def test_search_default_limit_unchanged(self, golden_store: MemoryStore):
        # search()'s default limit must still be 5 (unchanged signature).
        results = golden_store.search("設計")
        assert len(results) <= 5

    def test_type_filter_applied_as_post_filter_in_both_strategies(self, golden_store: MemoryStore):
        # FTS-hit path: "経営" only appears in observation-typed memories, so
        # filtering by a different type collapses results to empty — same
        # post-LIMIT-filter behaviour pre-T5 search() always had.
        fts_results, fts_strategy = golden_store.search_ex("アーキテクチャ", type="lesson", limit=5)
        assert fts_strategy == "fts"
        assert fts_results == []

        like_results, like_strategy = golden_store.search_ex("経営戦略", type="lesson", limit=5)
        assert like_strategy == "like_fallback"
        assert like_results == []


# ─── Cross-project baseline (PMSERV-153, ADR-039 followup) ──────────────────
#
# The memory_store fixture passes a global_db_path, so golden_store already
# mirrors every saved memory into the cross-project index. search_global_ex
# uses the SAME tokenize='unicode61' + FTS→LIKE fallback as search_ex, so on
# this corpus it tracks the per-project KNOWN_BASELINE query-for-query
# (measured: 0 divergences across all 23 queries). We assert *parity* with the
# per-project baseline rather than copying the 23 numbers: both indexes drift
# in lockstep if the tokenizer shifts across SQLite versions, so parity is the
# rot-proof lock. See docs/reports/ja-fts-baseline.md (cross-project section).


class TestJapaneseGoldenBaselineCrossProject:
    @pytest.mark.parametrize("query", sorted(KNOWN_BASELINE))
    def test_global_matches_perproject_baseline(self, golden_store: MemoryStore, query: str):
        expected = KNOWN_BASELINE[query]
        g_results, g_strategy = golden_store.search_global_ex(query, limit=5)

        # Cross-project result matches the locked per-project baseline exactly.
        assert g_strategy == expected["strategy"], (
            f"query={query!r}: cross-project strategy drifted from baseline "
            f"({expected['strategy']!r} -> {g_strategy!r}); if genuine, update "
            f"KNOWN_BASELINE and docs/reports/ja-fts-baseline.md, don't widen"
        )
        assert (len(g_results) > 0) == expected["hit"], (
            f"query={query!r}: cross-project hit/miss drifted from baseline"
        )
        assert len(g_results) == expected["count"], (
            f"query={query!r}: cross-project count drifted from baseline "
            f"({expected['count']} -> {len(g_results)})"
        )

        # And it tracks per-project search_ex on THIS corpus, query-for-query.
        pp_results, pp_strategy = golden_store.search_ex(query, limit=5)
        assert g_strategy == pp_strategy
        assert len(g_results) == len(pp_results)

    def test_global_overall_recall_matches_report(self, golden_store: MemoryStore):
        """Cross-project aggregate recall mirrors the per-project report:
        19/23 combined (fts + like_fallback), 14/23 FTS-only."""
        total = len(KNOWN_BASELINE)
        hits = 0
        fts_only_hits = 0
        for query in KNOWN_BASELINE:
            results, strategy = golden_store.search_global_ex(query, limit=5)
            if results:
                hits += 1
            if strategy == "fts" and results:
                fts_only_hits += 1

        assert total == 23
        assert hits == 19
        assert fts_only_hits == 14


class TestSearchGlobalDelegation:
    """search_global() must stay a thin, list-only delegation to search_global_ex()."""

    def test_search_global_returns_same_list_as_ex(self, golden_store: MemoryStore):
        ex_results, _strategy = golden_store.search_global_ex("経営戦略", limit=5)
        plain_results = golden_store.search_global("経営戦略", limit=5)
        assert [r["memory_id"] for r in plain_results] == [r["memory_id"] for r in ex_results]

    def test_search_global_wrapper_surfaces_like_fallback(self, golden_store: MemoryStore):
        # "経営戦略" is a like_fallback query (FTS MATCH is empty): the list-only
        # wrapper must still return the fallback rows, not a hard empty result.
        results = golden_store.search_global("経営戦略", limit=5)
        assert len(results) == 2
