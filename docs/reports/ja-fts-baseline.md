# 日本語 FTS ベースライン計測レポート（PMSERV-143 / ADR-039 T5）

- 対象: `MemoryStore.search_ex()`（`src/pmlens/memory.py`）— FTS5 (`tokenize='unicode61'`) →
  0件時に LIKE フォールバック（`content LIKE ? ESCAPE '\' OR tags LIKE ? ESCAPE '\'`、`%`/`_` エスケープ済み）
- 再現テスト: `tests/test_memory_ja_fts.py`（`KNOWN_BASELINE` に対する固定 assert。改善したら更新する運用）
- 計測環境: `sqlite3.sqlite_version = 3.51.0`（Python `sqlite3` 標準ライブラリ経由）。
  **FTS5 unicode61 のトークナイズ挙動は SQLite バージョンで揺れうる**ため、別環境でこのレポートの数値と
  ズレる場合は `tests/test_memory_ja_fts.py` を再実行し、`KNOWN_BASELINE` とこのレポートを実測値に合わせて
  更新すること（テストのassertを緩めて帳尻を合わせない）。
- 起票根拠: `docs/issues/ISSUE_desktop-outbox-one-way.md` P5（「経営戦略 2つのエンジン…」等の複合日本語クエリが
  cross_project 検索で 0 件になった実地インシデント、outbox_id:5）

## 計測方法

`docs/issues/ISSUE_desktop-outbox-one-way.md` の実障害クエリ（「経営戦略」「2つのエンジン」「引き継ぎ」）を含む、
複合語・かな交じり・英日混在を含む日本語メモ 15 件を golden corpus としてローカル `MemoryStore` に保存し
（`tests/test_memory_ja_fts.py::GOLDEN_CORPUS`）、23 個のクエリで `search_ex()` を実行して
`(strategy, hit有無, 件数)` を実測した。true negative コントロール（コーパスに存在しない語・LIKE フォール
バックでも救えない複合語 AND クエリ）を意図的に含め、再現率を実態より高く見せないようにしている。

## 結果（クエリ別）

| クエリ | strategy | hit | 件数 | 備考 |
|---|---|---|---|---|
| 経営戦略 | like_fallback | ✅ | 2 | 文中で助詞に接続され孤立トークンにならない典型例（ISSUE P5 の再現） |
| 2つのエンジン | fts | ✅ | 2 | 括弧/行頭で区切られ単独トークン化されたため直撃 |
| 引き継ぎ | fts | ✅ | 1 | |
| FTS5 | fts | ✅ | 1 | |
| unicode61 | like_fallback | ✅ | 1 | |
| 機械学習 | fts | ✅ | 1 | 「」で囲まれ孤立トークン化 |
| デスクトップ | fts | ✅ | 1 | |
| PMSERV-143 | fts | ✅ | 1 | |
| cross_project | fts | ✅ | 1 | |
| にっぽん | fts | ✅ | 1 | 「」で囲まれ孤立トークン化 |
| アーキテクチャ | fts | ✅ | 2 | |
| outbox | fts | ✅ | 1 | |
| refactor | fts | ✅ | 1 | |
| ADR-028 | like_fallback | ✅ | 1 | |
| ロードマップ | fts | ✅ | 1 | |
| 自然言語処理 | fts | ✅ | 1 | 「」で囲まれ孤立トークン化 |
| 表記ゆれ | fts | ✅ | 1 | tags列がカンマ区切り（ASCII区切り文字）のため孤立トークン化してヒット |
| ハイブリッド設計 | like_fallback | ✅ | 1 | 文中に埋め込まれ孤立トークンにならない |
| セッション継続 | like_fallback | ✅ | 1 | 同上 |
| 経営戦略 2つのエンジン | like_fallback | ❌ | 0 | **真の未ヒット**。LIKE フォールバックはクエリ文字列全体の単純部分一致であり、AND結合の複合クエリは救えない |
| 検索エンジン | like_fallback | ❌ | 0 | **真の未ヒット**（コーパスに当該連続文字列が存在しない） |
| 存在しない用語XYZ | like_fallback | ❌ | 0 | ネガティブコントロール |
| OAuth認証 | like_fallback | ❌ | 0 | ネガティブコントロール |

## 集計

- 総クエリ数: 23
- FTS5単独でのヒット数: 14/23 （約 60.9%）
- FTS5 + LIKEフォールバック合算でのヒット数: 19/23 （約 82.6%）
- **LIKEフォールバックによる改善**: +5クエリ（+21.7ポイント）— いずれも「文中に助詞・接続語を挟んで埋め込まれ、
  unicode61 が単独トークンとして切り出せなかった複合語」を救済したケース（「経営戦略」「unicode61」「ADR-028」
  「ハイブリッド設計」「セッション継続」）
- フォールバック後も未ヒットのまま: 4/23 — うち3件はコーパスに元々存在しない語（ネガティブコントロール）、
  1件（「経営戦略 2つのエンジン」）はLIKEフォールバックの構造的限界（クエリ文字列全体をそのまま部分一致させる
  だけで、複数語のAND結合はできない）

## 観察された失敗モード（unicode61 の挙動)

`memories_fts` は `tokenize='unicode61'` を使用しており、句読点・カッコ・ASCIIスペース等の区切り文字が無い限り、
連続する漢字・かな・カタカナの並びは**1つの巨大トークン**として扱われる。日本語は分かち書き（単語間スペース）
をしないため、文中に自然に埋め込まれた複合語（例:「経営戦略セッションで…」の「経営戦略」）は、文全体（または
句読点区切りの節全体）を覆う1つのトークンの一部でしかなく、クエリ側の「経営戦略」という短いトークンとは
完全一致しない → MATCH 失敗、という構造的な取りこぼしが起きる。

一方で、次のケースは孤立トークン化されFTS5で直撃する:
- クエリ語が「」括弧・句読点・行頭/行末などのASCII/記号区切り文字に隣接している場合（「2つのエンジン」「機械学習」等）
- tags 列はカンマ区切り（`_tags_to_str` がASCIIカンマで結合）で保存されるため、タグとして登録された複合語は
  区切り文字に挟まれ孤立トークン化されやすく、content列より仮名文字列の複合語検索に強い

LIKEフォールバックはこの構造的な取りこぼしに対する**計測された安全網**であり、根本修正（トークナイザー変更）
ではない。

## クロスプロジェクト計測（PMSERV-153, ADR-039 followup）

PMSERV-143 では別 issue 送りとしていた `search_global`（cross_project 横断検索）の日本語計測 + LIKE
フォールバック適用を、PMSERV-153 で実施した。

- 対象: `MemoryStore.search_global_ex(query, limit=...) -> tuple[list[dict], str]` を新設
  （`src/pmlens/memory.py`）。グローバル索引 `memory_index_fts` も per-project の `memories_fts` と同じ
  `tokenize='unicode61'` を使うため、`search_ex` と同じ FTS→LIKE フォールバック（`content LIKE ? ESCAPE '\'
  OR tags LIKE ? ESCAPE '\'`）を持たせた。既存 `search_global()` はシグネチャ不変のまま
  `search_global_ex(...)[0]` への薄い委譲に変更（後方互換）。
- 計測: golden corpus（同一 15 件）をグローバル索引へ同期し、per-project と同じ 23 クエリで `search_global_ex`
  を実測（`tests/test_memory_ja_fts.py::TestJapaneseGoldenBaselineCrossProject`）。
- 結果: **全 23 クエリで per-project ベースライン（上表）と strategy・hit・件数が完全一致（乖離 0）**。
  同一コーパス・同一トークナイザ・同一フォールバックであり、グローバル索引が追加で持つ `project` 列
  （本コーパスでは全行 `"pm-server"`）は日本語クエリにヒットしないため。集計も同一（FTS 単独 14/23 ≈ 60.9%、
  フォールバック込み 19/23 ≈ 82.6%）。
- テスト戦略: 23 個の数値を複製せず「per-project `search_ex` との parity」を assert する。両索引は unicode61 が
  SQLite バージョンで揺れても連動して動くため、parity が rot しない固定になる（絶対再現率 19/23・14/23 は
  集計テストで別途固定）。
- ISSUE P5 の実障害（outbox_id:5「経営戦略 2つのエンジン…」）はこの cross_project 経路で発生していたが、
  「経営戦略」単体は LIKE フォールバックで救済される（hit=2）ようになった。複数語 AND クエリ
  「経営戦略 2つのエンジン」は per-project 同様フォールバックの構造的限界で未ヒットのまま（trigram 移行=PMSERV-150 の判断材料）。

## スコープ外（明記）

- **trigram tokenizer への移行は本タスクの対象外**（ADR-039 AD-8、C16 → PMSERV-150 として別 issue 化）。
  インデックスサイズの倍増・SQLiteバージョン依存・RO Lens 経路との互換性まで波及するため。今回計測した再現率
  （FTS単独 60.9% / フォールバック込み 82.6%、per-project / cross-project 共通）が、trigram 移行の投資判断材料になる。

## 実装への反映

- `MemoryStore.search_ex(query, type=None, limit=...) -> tuple[list[Memory], str]` を新設
  （`src/pmlens/memory.py`）。既存 `search()` はシグネチャ不変のまま `search_ex(...)[0]` への薄い委譲に変更。
- `pm_recall` のクエリ経路、`pm_memory_search` を `search_ex` に切り替え、レスポンスへ additive キー
  `search_strategy`（`"fts"` | `"like_fallback"`）を追加。
- `type` フィルタは FTS・LIKE いずれの分岐でも既存 `search()` と同じ「LIMIT 適用後のポストフィルタ」のまま
  変更していない（挙動の一貫性を優先）。
