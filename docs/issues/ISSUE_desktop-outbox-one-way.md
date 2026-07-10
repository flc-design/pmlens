# ISSUE ｜ Desktop outbox が「片道」で、Desktop間の文脈引き継ぎができない

作成: 2026-07-01 JST ／ 起票者: 経営戦略セッション（Claude Desktop）での実地テスト
設計・実装は本プロジェクト（pm-server）の Claude Code で行う。本書は問題定義と要求のみ。

---

## 1. 背景（何が起きたか・事実）

2026-06-28〜07-01、business-strategy プロジェクトで「Desktopで記録 → 別のDesktopチャットで引き継ぐ」テストを実施。結果:

1. Desktop(Lens) から `pm_outbox_remember` で投函 → `~/.pm/desktop/desktop.db` に保存成功（outbox_id:5, status:saved）。
2. 別の Desktop チャットで `pm_recall` → **0件**。docs も（フルパス未提示のため）発見できず、引き継ぎに実質失敗。
3. 原因を切り分けた結果、記録は「消えた」のではなく「**在るのに Desktop からは読めない**」ことが確定。

## 2. 問題点の洗い出し（確定事実）

### P1: Desktop(Lens) ビルドに outbox の読み取りツールがない【最重要】
- Lens に載っているのは `pm_outbox_remember` / `pm_outbox_log`（書き込み）のみ。
- `pm_outbox_pending` / `pm_outbox_merge` は Claude Code 専用。
- → **投函はできるが、自分が投函したものを読み返せない「片道」**。

### P2: `pm_recall` は memory.db しか見ない
- 未マージの outbox エントリは recall の対象外。仕様としては正しいが、Desktop ユーザー視点では「記録したのに思い出せない」体験になる。

### P3: merge に Claude Code が必須（Desktop単体で完結しない）
- outbox → プロジェクト memory.db への昇格は `pm_outbox_merge`（Code側）が必要。
- Desktop 中心ユーザー（非エンジニア・Codeを使わない人）は自力で昇格できない。

### P4: 未登録プロジェクト（pm_init 前）へのガイドが弱い
- source_project に registry 未登録のパスを渡しても保存は成功するが、その後 merge / recall の導線が利用者に見えない。
- 実例: business-strategy は pm_init 未実施 → registry 不在 → project 指定 recall が機能せず。

### P5: FTS5 の日本語クエリ取りこぼし（疑い・未確定）
- 別チャットの cross_project 検索で「経営戦略 2つのエンジン…」等の複合日本語クエリが 0 件。
- 記録不在が主因だったが、日本語の形態素分割/トークナイザ起因の取りこぼし可能性は残る（要検証: unicode61 での分かち書き、trigram 検討）。

## 3. 事業インパクト（なぜ直すべきか）

- クラウドMVPの想定顧客は「チームの非エンジニア含むメンバーが Web/Desktop で文脈を読む」層。**Desktop 片道問題は、その中核価値（cross-host の文脈維持）の直接の欠陥**。
- セルフホスト志向の顧客（Code を全員には配らない企業）ほど Desktop 的な閉じた利用が中心 → 刺さり方が大きい。
- 自社（FLC）のドッグフーディングで実際に引き継ぎ失敗が発生済み。顧客でも必ず起きる。

## 4. 改善要求（設計は Claude Code で。ここでは要求と優先度のみ）

### R1【P0・小】Lens に outbox 読み取りツールを追加
- `pm_outbox_pending` の **read-only 版**（一覧＋内容表示、フィルタ: source_project / type / since）を Desktop ビルドに含める。
- Lens の read-only 思想と矛盾しない（読むだけ）。片道 → 双方向読みに。

### R2【P1・中】`pm_recall` に outbox オーバーレイ（オプション）
- 例: `include_outbox=true` で、未マージの outbox エントリも結果に含める。
- 各ヒットに provenance を明示（`source: outbox(unmerged)` vs `source: memory.db`）。誤って「正式記録」と混同させない。

### R3【P1・小】未登録プロジェクトへの導線改善
- source_project が registry 未登録の場合、保存レスポンスに「pm_init が未実施。Claude Code で `pm_init <path>` → `pm_outbox_merge`」の明示ガイドを含める。
- merge 側でも、対象未登録なら init を提案（自動 init はしない。明示操作を守る）。

### R4【P2・要検証】日本語 FTS の改善
- 現行トークナイザで日本語複合クエリの再現率を計測 → 必要なら trigram / bigram インデックス併用を検討。
- まず再現テストケース（「経営戦略」「2つのエンジン」等）を fixture 化。

### R5【P2・検討のみ】Desktop からの merge は「やらない」を明文化
- Lens で merge まで許すと read-only 設計・安全性（誤マージ）を壊す。現段階では**非スコープと明記**し、将来クラウド版の同期機能で解決する方針とする（docs/03_pmlens-mvp-spec.md と整合）。

## 5. 受け入れ条件（Definition of Done）

1. Desktop の新チャットで、直前の Desktop セッションが投函した outbox エントリを**一覧・閲覧できる**（R1）。
2. `pm_recall(include_outbox=true)` で未マージ分が provenance 付きで返る（R2）。
3. 未登録 source_project への投函時、次にやるべき操作（init/merge）がレスポンスで案内される（R3）。
4. 上記が読み取り専用の安全性を壊していない（Lens から書き込みは outbox のみ、という不変条件の維持）。
5. 日本語検索の再現テストが追加され、現状の再現率が計測されている（R4 は計測まで、改善は別issue可）。

## 6. 非スコープ

- Desktop からの merge 実行（R5 のとおり明示的に対象外）
- クラウド同期そのもの（MVP spec 側: business-strategy/docs/03_pmlens-mvp-spec.md）
- Web ダッシュボード

## 7. 関連ドキュメント

- 教訓の原本: `/Users/flc001/Desktop/work/Develop/00_project/01_flc-app/business-strategy/docs/04_handoff-lessons.md`
- クラウドMVP仕様: `/Users/flc001/Desktop/work/Develop/00_project/01_flc-app/business-strategy/docs/03_pmlens-mvp-spec.md`
- 再現ログ: 2026-07-01 Desktop 新チャットの引き継ぎテスト（outbox_id:5 が recall 不可だった件）
