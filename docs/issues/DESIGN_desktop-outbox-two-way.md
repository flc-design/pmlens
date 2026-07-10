# DESIGN ｜ Desktop outbox 双方向化（read-back / recall overlay / 未登録導線）

- 対象 ISSUE: `docs/issues/ISSUE_desktop-outbox-one-way.md`（R1〜R5）
- 設計プロセス: WF-032（brainstorming テンプレート: 3エージェント発散76案 → 独立6軸採点 → 収束、ユーザー承認済み）
- 要求定義: KR-015（FR-1〜13 / NFR-1〜6）
- 実装モデル: **Sonnet 5**（1タスク=1コミット粒度、本書のスペックに従うこと）
- 作成: 2026-07-10 ／ クロスチェック済み（独立レビュー GO-with-fixes → 全修正反映済み。BLOCKER だった「RO ストア missing キャッシュ固定化」は T1-1/T1-3 で解消）

---

## 1. アーキテクチャ決定（AD）

| # | 決定 | 根拠 |
|---|---|---|
| AD-1 | 読み口は新ツールを作らず **pm_outbox_pending を Lens surface に開放**する | 機能重複ゼロ・API 肥大回避。フィルタ/ページングは実装済み |
| AD-2 | outbox の read 経路を**純粋読み**にする: desktop.db 不在→生成せず空、存在→ `mode=ro` で open | 現行は `_ensure_schema()` が「読むだけで DB を生成」する。Lens の「read は書かない」思想に反する |
| AD-3 | desktop.db の RO 接続に **immutable=1 を使わない**（memory.db の RO 戦略をコピペしない） | desktop.db は「常に書き手がいる」DB。immutable は並行 WAL 書き込み中に破損読み/偽 SQLITE_CORRUPT を踏む |
| AD-4 | recall オーバーレイは**別キー additive**（`outbox_entries[]`）+ provenance + truncate/件数 cap | 正式記録との混同防止（DoD2）。track キーの前例踏襲で後方互換。コンテキスト予算保護 |
| AD-5 | オーバーレイ既定スコープ=**現在プロジェクトのみ**。プロジェクト解決不能時は scope="all" フォールバック | desktop.db はグローバル共有。無スコープだと他プロジェクトの未マージメモが recall に混入する |
| AD-6 | merge の**暗黙 .pm/memory.db 生成を禁止**（pm_init 誘導、auto-init しない） | 現行 pm_outbox_merge は未初期化パスに memory.db を暗黙生成し得る = 既存原則の違反。本件は欠陥修正 |
| AD-7 | 「未登録」の判定述語は **`.pm/project.yaml` の存在**で統一（registry.yaml は判定に使わない） | registry はキャッシュで遅延し得る。判定を単一述語にして R3 の警告と C13 集計と merge ガードで共有 |
| AD-8 | R4 は**計測 + FTS 0件時 LIKE フォールバック**まで。trigram インデックスは別 issue | tokenizer 移行はインデックス倍増・SQLite 版依存・RO Lens 互換まで波及しスコープが死ぬ |

**延期（別 issue 化推奨)**: trigram shadow index（C16、計測数値が判断材料）/ Lens での include_outbox 既定 ON（C7、AD-5 安定後）/ pm_status Lens 件数表示（C4）/ マシン跨ぎ同期（クラウド MVP スコープ）

---

## 2. タスク分割（T1〜T7、実装順)

依存: T1 → T2 → T3 / T4（T3・T4 は並行可)、T5 は独立、T6 は T1〜T3 後、T7 は最後。

登録済みタスク ID: T1=PMSERV-142 / T2=PMSERV-145 / T3=PMSERV-146 / T4=PMSERV-147 / T5=PMSERV-143 / T6=PMSERV-148 / T7=PMSERV-149。関連欠陥（本設計スコープ外・クロスチェック発見）: PMSERV-144（pm_status が PM_LENS=1 でも install_hooks 実行）。ADR=ADR-039、要求=KR-015、スペック=KR-016。

### T1 ｜ outbox.py: read-pure 化(FR-2)【S】

**対象**: `src/pmlens/outbox.py`

1. `DesktopOutboxStore.__init__` に `readonly: bool = False` を追加。
   - `readonly=True`: `_ensure_schema()` を**呼ばない**。
   - `readonly=False`: 現行どおり（スキーマ生成）。
   - **重要**: DB 不在の判定を `__init__` で固定**しない**こと。missing 判定は `pending` / `get` / `get_pending_count` の**各呼び出し先頭**で `self.db_path.exists()` を評価する。理由: RO ストアはプロセス寿命キャッシュされるため、init 時に固定すると「①recall の count probe で RO ストア生成（missing 固定）→ ②remember が desktop.db を生成 → ③pending が空を返し続ける」という『在るのに読めない』の再導入になる（クロスチェック BLOCKER）。別プロセスが後から DB を作るケースも同様。
2. `_connect()` を分岐:
   - readonly: `sqlite3.connect(f"file:{path}?mode=ro", uri=True, check_same_thread=False)`。PRAGMA は **busy_timeout のみ**（journal_mode/synchronous は ro 接続に適用しない）。
   - 通常: 現行どおり `_apply_pragmas`。
3. read メソッド（`pending` / `get` / `get_pending_count`）: 呼び出し先頭で `db_path.exists()` が False なら DB に触れず空を返す（`{"items": [], "total": 0, "has_more": False, "next_offset": offset}` / `None` / `0`）。さらに **connect と execute を含む read 処理全体**を `except sqlite3.Error` で包み、失敗時も同じ空形を返す（sqlite3 のエラーは connect ではなく最初の execute で発生する。crashed writer の stale -shm/-wal による SQLITE_READONLY_RECOVERY や corrupt 時の DatabaseError もここで拾う）。呼び出し側で note を付与（エラーで落とさない）。
4. write メソッド（`append` / `mark_merged` / `mark_rejected`）: readonly ストアでは `PmServerError("outbox store is read-only")` を raise。
5. ファクトリ: `get_outbox_store(db_path: Path | None = None, readonly: bool = False)` — **既存の `db_path=` 引数互換を維持**（server.py の既存5呼び出し箇所はすべて `db_path=default_outbox_db_path()` を渡している）。RO/RW で**別キャッシュスロット**（`_outbox_store` / `_outbox_store_ro`）。`clear_outbox_store()` は両方クリア。
6. RW 経路の追加ガード: `pm_outbox_merge` / `pm_outbox_reject`（server.py 側）は `get_outbox_store(readonly=False)` を呼ぶ**前**に `default_outbox_db_path().exists()` を検査し、不在なら全 id を not_found として即返す（RW `__init__` のスキーマ生成には手を入れない。`append`＝remember/log のみが正当な生成トリガー）。

**テスト**（`tests/test_outbox.py` に追記 or 新設):
- desktop.db 不在で readonly ストアの pending/get/count → 空/None/0、**ファイルが生成されない**
- readonly ストアへの append → PmServerError
- **キャッシュ staleness 検収**: DB 不在時に RO ストアを生成し空を確認 → その後 RW ストアで append → **同じ RO ストアインスタンス**で読めること（missing の呼び出し毎評価の検収）
- clear_outbox_store() が両キャッシュをクリアする

**受け入れ条件**: 読み経路のどの呼び出しでも desktop.db・-wal・-shm が新規生成されない。

### T2 ｜ server.py: Lens ゲーティング開放 + note + エコーバック（FR-1, FR-7)【S】

**対象**: `src/pmlens/server.py`（`RO_ALLOWLIST` 定義部・`_tool()`・outbox ツール群）、`tests/test_server.py`

1. 新定数 `OUTBOX_READ_ALLOWLIST: frozenset[str] = frozenset({"pm_outbox_pending"})`。
2. `_tool()` のゲーティングに追加: `PM_LENS_ENABLED` 時、`RO_ALLOWLIST` / `OUTBOX_WRITE_ALLOWLIST`（PM_DESKTOP_WRITE 必要）に加え **`OUTBOX_READ_ALLOWLIST` は PM_DESKTOP_WRITE に関わらず登録**。
3. `pm_outbox_pending` を `get_outbox_store(readonly=True)` に切り替え。レスポンスに host-aware note:
   - PM_LENS=1: 「Desktop outbox の閲覧です。プロジェクト記憶への取り込み（merge）は Claude Code 側の pm_outbox_merge で行います（Desktop からの merge は仕様上できません）。」
   - PM_LENS=0: 現行 note 踏襲(pm_outbox_merge / pm_outbox_reject への導線)。
4. `pm_outbox_remember` / `pm_outbox_log` レスポンスに `pending_total`（insert 後の **DB 全体の** pending 総数、同一 RW ストアで COUNT）を追加。note 末尾に「読み返すには pm_outbox_pending、recall に重ねるには pm_recall(include_outbox=true)」を追記。
5. `pm_status` の outbox 件数取得（server.py:480-491）も readonly ストアに切り替え（現行は Code ホストの read 経路が desktop.db を暗黙生成している = 修正対象の現行違反）。
6. `pm_outbox_pending` に `filter_since: str | None = None`（ISO 8601、`created_at >= ?`）を追加（ISSUE R1 が明記する since フィルタ。outbox.py `pending()` への WHERE 句追加とツールパラメータの両方）。
7. 件数表記の統一: pending 件数を返す全表面（pm_status.diagnostics.outbox_pending / pending_total / outbox_pending_count / outbox_summary.pending_total）は「**DB 全体の pending 件数**」で統一し、各 docstring にその旨を明記（表面ごとに意味が違うと LLM が混同する）。

**テスト**:
- PM_LENS=1 / PM_DESKTOP_WRITE=0 で pm_outbox_pending が REGISTERED_TOOLS に載る（write 2種は載らない）
- PM_LENS=1 / PM_DESKTOP_WRITE=1 で read+write 3種が載る
- PM_LENS=0 で全ツール登録（現行維持）
- **既存 allowlist 断言の更新対象は `tests/test_lens_mode.py`**（113行目の厳密一致 `REGISTERED_TOOLS == set(RO_ALLOWLIST)` を `RO_ALLOWLIST | OUTBOX_READ_ALLOWLIST` に変更。test_server.py ではない）
- `tests/test_ro_surface_disjoint.py` の静的到達性証明の走査対象に OUTBOX_READ_ALLOWLIST を追加（pm_outbox_pending を subprocess/git 非到達の証明対象に含める）
- remember 応答に pending_total が入る / filter_since で絞れる

**受け入れ条件**: DoD1 — Desktop 新チャットで前セッションの投函が一覧・閲覧できる。

### T3 ｜ pm_recall: include_outbox オーバーレイ（FR-3〜6)【M】

**対象**: `src/pmlens/server.py` `pm_recall`、`tests/test_server.py`（or `test_recall.py`）

1. シグネチャに `include_outbox: bool = False` を追加。
2. ヘルパー `_build_outbox_overlay(project_path: str | None, query: str | None, limit: int) -> dict` を新設:
   ```python
   # 返り値（include_outbox=true 時に recall 応答へマージする additive キー群）
   {
     "outbox_entries": [
       {
         "outbox_id": 5, "type": "memory",
         "content": "...(500字で切る)", "content_truncated": True,
         "source_project": "/path/to/proj", "tags": "a,b",
         "created_at": "...", "host_id": "claude-desktop",
         "source_session": "sess-...",
         "source": "outbox(unmerged)",
         # query 経路のヒットのみ:
         "match_source": "outbox_like",
       }, ...
     ],
     "outbox_summary": {
       "pending_total": 7,        # DB 全体の pending 数
       "project_pending": 3,      # 現在プロジェクト一致分
       "unscoped_pending": 2,     # source_project 無し（一覧には出さない）
       "scope": "project",        # "project" | "all"
     },
   }
   ```
3. スコープ規則（AD-5）:
   - `resolve_project_path(project_path)` が解決できたら scope="project": `Path(source_project).expanduser().resolve() == root` のエントリのみ一覧化。source_project 無しは `unscoped_pending` 件数のみ。
   - **実装位置の注意（クロスチェック指摘）**: 現行 pm_recall は冒頭の `_get_memory_store(project_path)` （server.py:945）が `ProjectNotFoundError` を raise するため、素朴に書くとオーバーレイ到達前に全体が失敗する。**include_outbox=true の場合は pm_recall 側でこの例外を catch し、outbox オーバーレイのみ（scope="all"）を含む応答を返す**（`last_session: null, recent_memories: []` + outbox キー群 + note）。include_outbox=false 時は現行どおり例外のまま（後方互換）。
   - scope="all" 時の note: 「プロジェクト未解決のため全プロジェクトの未マージ分を表示しています」（未登録プロジェクトのインシデントケース救済）。
   - マッチ規則の非対称に注意: `pm_outbox_pending(filter_project=)` は生文字列一致（現行仕様のまま）、オーバーレイは resolve() 済み比較。両ツールの docstring にこの差を明記する。
4. 経路別:
   - default 経路(query も task_id も無し): pending を created_at DESC で `min(limit, 10)` 件。
   - query 経路: `WHERE status='pending' AND (content LIKE ? ESCAPE '\' OR tags LIKE ? ESCAPE '\')` — **括弧必須**（OR/AND 優先順位バグ防止）、query 中の `%` `_` は `\` でエスケープしてからバインド。CJK は UTF-8 部分一致で有効。ヒットに `match_source: "outbox_like"`。件数 cap 同上。
   - task_id 経路・cross_project 経路: オーバーレイ**対象外**（エラーにせず無視。docstring に明記）。
5. FR-6（include_outbox=false 時の存在通知): **count probe は PM_LENS=1 のときのみ実行**（Code では pm_status.diagnostics が既に告知しており重複になるため）。readonly ストアで pending 総数を取得し、
   - count>0 なら `outbox_pending_count: N` を additive 付与。**count=0 のときはキー自体を付与しない**（テスト決定性のため二者択一を確定）。
   - **recall 結果が空**（recent_memories/results が空 かつ last_session 無し）かつ count>0 の時のみ、**`outbox_note` キー**（既存の `note` キーは `_maybe_add_lens_note` が Lens fallback 時に使用するため衝突させない）: 「未マージの Desktop 記録が N 件あります。pm_recall(include_outbox=true) か pm_outbox_pending で閲覧できます。」
6. truncate は 500 文字 + `content_truncated` フラグ。全文は pm_outbox_pending で参照（note に書く）。

**テスト**:
- include_outbox=true で outbox_entries / outbox_summary が返る（provenance 各フィールド検証）
- 他プロジェクト source_project のエントリが scope="project" で一覧に出ない・unscoped は件数のみ
- プロジェクト解決不能 → scope="all" フォールバック + note
- query 経路 LIKE ヒット + match_source 付与
- 501 文字以上のエントリが truncate + フラグ
- include_outbox=false: count>0 で outbox_pending_count、空 recall 時のみ救済 note
- desktop.db 不在: outbox_entries=[] / count キー無し（または 0）でエラーにならない

**受け入れ条件**: DoD2 — 未マージ分が provenance 付きで返り、正式記録と構造的に区別される。

### T4 ｜ R3: 未登録プロジェクト導線 + merge ガード（FR-8〜10)【S-M】

**対象**: `src/pmlens/server.py`（remember/log/merge/pending）、`src/pmlens/utils.py`（述語ヘルパー）、テスト

1. `utils.py` に述語 `is_initialized_project(path_str: str) -> bool`: `Path(path_str).expanduser().resolve() / ".pm" / "project.yaml"` の存在（AD-7。registry.yaml は見ない）。例外時 False。
2. `pm_outbox_remember` / `pm_outbox_log`: `source_project` 指定かつ未初期化なら、保存成功のまま `warnings[]` を追加:
   ```python
   {"code": "unregistered_project",
    "project": source_project,
    "message": "source_project は pm_init 未実施です（.pm/project.yaml がありません）。",
    "remediation": "Claude Code がある場合: `pm_init <path>` 実行後に pm_outbox_merge で取り込めます。"
                   "Claude Code が無い環境でも記録は desktop.db に安全に保管され、"
                   "pm_outbox_pending / pm_recall(include_outbox=true) でいつでも閲覧できます。"}
   ```
   （dual-audience: Code 非保有者に「実行不能な指示だけ」を返さない）
   また **source_project 未指定**の remember/log には note で「source_project を付けると pm_recall(include_outbox=true) の project スコープに表示されます」と案内する（未指定エントリはオーバーレイで件数のみになるため。Desktop で最も起きやすい形の救済）。
3. `pm_outbox_merge`（AD-6、**欠陥修正**): 各 id 処理で `_get_memory_store` / `add_daily_log` を呼ぶ**前に** `is_initialized_project(project_path)` を検査。False なら skip して:
   ```python
   {"id": outbox_id, "reason": "unregistered_project",
    "remediation": "pm_init <path> を実行してから再度 pm_outbox_merge してください（自動 init はしません）。"}
   ```
   これにより未初期化パスへの .pm/memory.db 暗黙生成が構造的に不可能になる。`pm_outbox_reject` / merge は desktop.db 不在時、生成せず全 id not_found を返す（T1-6）。
4. `pm_outbox_pending` レスポンスに集計を追加（C13）: 全 pending（ページではなく全体）の distinct source_project のうち未初期化のもの:
   ```python
   "unregistered_projects": [{"project": "/path/a", "count": 3}, ...]
   ```

**テスト**: 未初期化 source_project への remember → saved + warnings[]。merge → skip + remediation、**.pm が生成されていない**こと。pending 集計に project/count が出ること。初期化済みプロジェクトでは warnings 無し。

**受け入れ条件**: DoD3 — 投函時に次操作（init/merge）が案内される。merge が .pm を自動生成しない。

### T5 ｜ R4: 日本語 FTS 計測 + LIKE フォールバック（FR-11, 12)【S-M】

**対象**: `src/pmlens/memory.py`、`tests/test_memory_ja_fts.py`（新設）、`docs/reports/ja-fts-baseline.md`（新設）

1. golden-query fixture: 日本語コーパス約15件（ISSUE 実障害クエリ「経営戦略」「2つのエンジン」「引き継ぎ」等 + 複合語・かな交じり・英日混在)を MemoryStore に保存し、クエリ→期待ヒットの対で unicode61 FTS の再現率を計測。結果は `KNOWN_BASELINE` dict に対する assert で固定（改善したら更新を強制される形）。fixture コメントに「SQLite バージョン差で結果がずれた場合は当該環境の sqlite3.sqlite_version と共に KNOWN_BASELINE を更新する」手順を書く。
2. `MemoryStore` に `search_ex(query, type=None, limit=...) -> tuple[list[Memory], str]` を新設（strategy は `"fts"` | `"like_fallback"`）。**limit の既定値は既存 `search()` に合わせる**こと（委譲時は呼び出し側の limit をそのまま渡す。既定値の食い違いを作らない）。FTS が 0 件なら `content LIKE ? ESCAPE '\' OR tags LIKE ? ESCAPE '\' ORDER BY created_at DESC LIMIT ?` にフォールバック。**type フィルタは既存 search() と同じ扱い（LIMIT 後のポストフィルタ）に合わせる**（挙動の一貫性優先）。既存 `search()` は `search_ex()[0]` の薄い委譲にし**シグネチャ不変**。
3. `pm_recall`（query 経路）と `pm_memory_search` を search_ex に切り替え、レスポンスに `search_strategy` キーを additive 付与。
4. 計測数値（クエリ別 hit/miss、再現率、LIKE フォールバック後の改善値)を `docs/reports/ja-fts-baseline.md` に記録。レポートに「**cross_project（search_global）は未計測**（インシデントの実経路だが v1 対象外、別 issue で対応）」と正直に明記する。**trigram 化は本タスクでやらない**（AD-8、別 issue 起票のこと）。
5. `search_global`（cross_project）は v1 対象外。別 issue に含める。

**受け入れ条件**: DoD5 — 再現テストが存在し、現状再現率が数値で記録されている。

### T6 ｜ Lens 不変条件回帰テスト（NFR-1 / C18)【S】

**対象**: `tests/test_lens_invariant.py`（**既存ファイルに追記**。新設 `test_lens_invariants.py` は既存の単数形ファイルとほぼ同名になり衝突・混乱を招くため禁止）

- tmp HOME + tmp プロジェクトを用意し、PM_LENS=1 で登録される全 read ツールを一巡実行。**各ツールはデフォルト引数で呼び、エラー応答は許容**し、FS スナップショット不変のみを assert する（引数不足でエラーを返すツールがあっても FS が無変化なら合格）。実行前後で `~/.pm` 配下と project `.pm` 配下の (path, size, mtime_ns) スナップショットが**完全一致**することを assert。desktop.db 不在時に生成されないことも明示 assert。
- 既存の Lens 系テスト（PMSERV-079/080、同ファイル内の subprocess パターン）の env monkeypatch + reimport パターンに従うこと。

**受け入れ条件**: DoD4 — 「Lens からの書き込みは outbox のみ」の不変条件がテストで担保される。

### T7 ｜ docs 一式（FR-13 / R5)【S】

**対象**: `README.md`、`README.ja.md`、`docs/design.md`、`CHANGELOG.md`

1. 不変条件マトリクス（ビルド × 代表ツール × 可否）:
   | ツール | Code | Lens viewer (PM_LENS=1) | Desktop outbox host (+PM_DESKTOP_WRITE=1) |
   |---|---|---|---|
   | pm_recall / pm_status 等 read | ✅ | ✅ (main DB は ro) | ✅ |
   | pm_outbox_pending | ✅ | ✅ **(new)** | ✅ **(new)** |
   | pm_outbox_remember / log | ✅ | ❌ | ✅ |
   | pm_outbox_merge / reject | ✅ | ❌ | ❌ |
   | pm_add_task 等 mutator | ✅ | ❌ | ❌ |
2. R5 の明文化: 「Desktop からの merge は非対応（誤マージ防止・RO 設計維持）。将来はクラウド同期で解決予定」— 銀行窓口型の「案内」トーンで。
3. 正直なスコープ記述: desktop.db は **HOME ディレクトリ単位**。同一マシン内の Desktop⇔Code/Desktop⇔Desktop の引き継ぎを解決するもので、マシン跨ぎはクラウド MVP のスコープ。
4. include_outbox の使用例、未登録プロジェクト時のフロー例。
5. **README は EN/JA 両方を同時更新**（片側更新ドリフトの再発防止 — memory 256 の教訓。見出し diff `grep -nE '^#{1,3} ' README.md README.ja.md` で確認）。
6. CHANGELOG `[Unreleased]` に Added/Fixed(merge 暗黙生成の欠陥修正)を記録。

---

## 3. 実装セッションへの指示（Sonnet 5)

1. 実装開始時に本書と `docs/design.md` を読むこと。development ワークフロー（pm_workflow_start template="development"）で進める。
2. 1タスク=1アトミックコミット。コミット前に `/lint` と対象テストを実行。
3. T1 完了までは T2〜T4 に着手しない（read-pure 化が前提条件）。
4. レスポンススキーマは additive のみ。既存キーの変更・削除は禁止。
5. 実装中に本書と実コードの乖離を見つけたら、黙って逸脱せず pm_add_issue で起票 or ユーザーに確認。
6. 完了後の別 issue 起票を忘れないこと:
   - trigram shadow index（C16、T5 の計測数値が判断材料）
   - Lens での include_outbox 既定 ON（C7、AD-5 安定後）
   - pm_status の Lens 件数表示（C4）
   - search_global（cross_project）の日本語計測 + LIKE フォールバック
   - **【欠陥・本設計スコープ外】pm_status が PM_LENS=1 でも `install_hooks()` を無条件実行し `~/.claude/settings.json` へ書き込む**（server.py:464-467、hooks.py に Lens ゲート無し。「Lens の read は書かない」思想への違反。クロスチェックで発見）

## 4. 非スコープ（再掲)

Desktop からの merge 実行 / クラウド同期 / Web ダッシュボード / trigram インデックス移行（別 issue）/ マシン跨ぎの outbox 共有（desktop.db は HOME 単位）
