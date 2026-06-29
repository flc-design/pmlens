---
name: super-research
description: >
  Super Research — 3並列エージェントによる多角的調査スキル。
  Depth Check (6次元品質評価) + Fact Check + Cross-Check で信頼性を担保。
  「調査」「リサーチ」「research」「深掘り」「分析」「比較検討」「技術選定」
  「市場調査」「実現可能性」「spike」のキーワードでトリガーすること。
  PM Lens のワークフローエンジンと連携し、知識レコード (pm_record) に結果を永続化する。
---

# Super Research Skill

## 概要

Super Research は Claude Code のサブエージェントを活用した多角的調査フレームワーク。
3つの視点（ドメイン専門家・批判的分析家・水平思考家）から並列に調査し、
6次元の品質チェックで信頼性を担保する。

結果は PM Lens の Knowledge Records (`pm_record`) に構造化して保存され、
ワークフローの後続ステップで参照可能。

## アーキテクチャ

```
                          ┌─────────────────┐
                          │  Research Query  │
                          └────────┬────────┘
                                   │
                    ┌──────────────┼──────────────┐
                    ▼              ▼              ▼
            ┌──────────┐  ┌──────────────┐  ┌──────────┐
            │  Domain   │  │  Critical    │  │  Lateral  │
            │  Expert   │  │  Analyst     │  │  Thinker  │
            └─────┬────┘  └──────┬───────┘  └─────┬────┘
                  │              │                │
                  └──────────────┼────────────────┘
                                 ▼
                       ┌─────────────────┐
                       │  Depth Check    │
                       │  (6 dimensions) │
                       └────────┬────────┘
                                │
                       ┌────────┴────────┐
                       ▼                 ▼
               ┌──────────────┐  ┌──────────────┐
               │  Fact Check  │  │ Cross-Check  │
               └──────┬───────┘  └──────┬───────┘
                      │                 │
                      └────────┬────────┘
                               ▼
                    ┌─────────────────────┐
                    │  Synthesis Report   │
                    │  → pm_record (KR)   │
                    └─────────────────────┘
```

## 3並列エージェント

### 1. Domain Expert (ドメイン専門家)
- **役割**: 対象領域の深い専門知識に基づく調査
- **焦点**: 技術的正確性、ベストプラクティス、先行事例
- **出力**: 技術的事実、推奨アプローチ、既知の制約

### 2. Critical Analyst (批判的分析家)
- **役割**: 仮説や提案に対する批判的評価
- **焦点**: リスク、トレードオフ、隠れた前提、失敗パターン
- **出力**: リスク一覧、反証、代替案の評価

### 3. Lateral Thinker (水平思考家)
- **役割**: 予想外の視点やアプローチの探索
- **焦点**: 異分野の類似パターン、創造的解決策、見落とされた選択肢
- **出力**: 非自明な洞察、異分野の事例、革新的アプローチ

## Depth Check (6次元品質評価)

各エージェントの出力を以下の6次元で評価:

| 次元 | 評価内容 | スコア |
|---|---|---|
| **Accuracy** (正確性) | 事実に基づいているか、検証可能か | 1-5 |
| **Completeness** (網羅性) | 重要な側面を漏れなくカバーしているか | 1-5 |
| **Currency** (最新性) | 情報は最新か、時間的に有効か | 1-5 |
| **Relevance** (関連性) | 調査目的に対して適切か | 1-5 |
| **Bias** (偏りのなさ) | 特定の立場に偏っていないか | 1-5 |
| **Source Quality** (出典品質) | 信頼できる情報源に基づいているか | 1-5 |

**品質基準:**
- 平均スコア 4.0 以上: `confidence: high` で記録
- 平均スコア 3.0 以上: `confidence: medium` で記録
- 平均スコア 3.0 未満: 再調査を推奨

## 調査モード

### Quick Mode (~50K tokens)
- **用途**: 既知の領域の確認、簡単な技術選定
- **エージェント**: Domain Expert のみ
- **チェック**: Depth Check (簡易版: 3次元)
- **所要時間**: 1-2分

### Standard Mode (~150K tokens)
- **用途**: 新技術の評価、設計判断の裏付け
- **エージェント**: Domain Expert + Critical Analyst
- **チェック**: Depth Check (6次元) + Fact Check
- **所要時間**: 3-5分

### Full Mode (~300K+ tokens)
- **用途**: 重要な技術選定、アーキテクチャ判断
- **エージェント**: 3並列全て
- **チェック**: Depth Check (6次元) + Fact Check + Cross-Check
- **所要時間**: 5-10分

## 使い方

### ワークフロー内での使用

Discovery ワークフローの `research` ステップで自動的にトリガーされる:

```
> ワークフロー開始：認証方式の調査 (template: discovery)
→ Step 1: Research & Investigation
  skill_hint: super-research
  → 3並列エージェントで調査開始
  → Depth Check + Fact Check
  → pm_record に知識レコードとして保存
```

### 直接使用

```
> 認証方式について調査して (standard mode)
→ Domain Expert: JWT vs Session vs OAuth2 の技術比較
→ Critical Analyst: 各方式のリスクとトレードオフ
→ Depth Check: 6次元評価
→ Fact Check: 事実確認
→ KR-001 として pm_record に保存
```

## PM Lens 連携

### Knowledge Records への出力

調査結果は `pm_record` ツールで構造化して保存:

```yaml
# 自動生成される Knowledge Record の例
id: KR-001
category: research        # or: market, spike, tradeoff, etc.
title: "認証方式の比較調査"
status: validated
confidence: high          # Depth Check スコアから自動判定
findings: |
  ## Domain Expert
  JWT は stateless でマイクロサービスに適する...

  ## Critical Analyst
  JWT のトークン失効は即時反映が困難...

  ## Lateral Thinker
  WebAuthn/Passkey の採用で UX とセキュリティを両立...
conclusion: |
  JWT (RS256) + リフレッシュトークンを推奨。
  WebAuthn はフェーズ2で段階的に導入。
sources:
  - "RFC 7519 (JWT)"
  - "OWASP Authentication Cheat Sheet"
tags: [auth, jwt, security]
```

### ワークフローステップとの連携

```yaml
# discovery.yaml の research ステップ
- id: research
  skill_hint: super-research
  produces:
    - research_findings
    - market_analysis
```

`produces` に指定された成果物は Knowledge Record の ID で追跡され、
後続ステップの `consumes` で参照可能。

## エージェントプロンプトテンプレート

### Domain Expert

```
あなたは {domain} の専門家です。
以下のトピックについて、技術的に正確で実践的な調査を行ってください。

トピック: {query}
コンテキスト: {context}

以下の観点で調査してください:
1. 技術的事実と現状
2. ベストプラクティスと推奨アプローチ
3. 既知の制約と前提条件
4. 実装上の注意点

出力は構造化されたマークダウンで記述してください。
```

### Critical Analyst

```
あなたは批判的分析の専門家です。
以下の調査結果に対して、批判的な評価を行ってください。

調査結果: {findings}
トピック: {query}

以下の観点で評価してください:
1. リスクと潜在的な問題
2. トレードオフの分析
3. 隠れた前提や仮定
4. 代替アプローチの検討
5. 過去の失敗パターンとの類似性

特に「なぜこのアプローチが失敗する可能性があるか」を重点的に分析してください。
```

### Lateral Thinker

```
あなたは異分野の知識を横断的に活用するクリエイティブな思考家です。
以下のトピックについて、予想外の視点からアプローチしてください。

トピック: {query}
既存の調査: {findings}

以下の観点で探索してください:
1. 異分野での類似パターン
2. 見落とされている選択肢
3. 制約を外した場合の理想的解決策
4. 将来のトレンドを見据えたアプローチ

「当たり前」を疑い、新しい可能性を提案してください。
```

## Depth Check テンプレート

```
以下の調査結果を6つの次元で評価してください。
各次元について 1-5 のスコアと根拠を記述してください。

調査結果: {combined_findings}

## 評価次元

1. **Accuracy** (正確性): 事実に基づいているか
2. **Completeness** (網羅性): 重要な側面をカバーしているか
3. **Currency** (最新性): 情報は最新か
4. **Relevance** (関連性): 目的に対して適切か
5. **Bias** (偏りのなさ): 特定の立場に偏っていないか
6. **Source Quality** (出典品質): 信頼できる情報源か

## 出力形式
各次元: スコア(1-5), 根拠(1行), 改善提案(該当する場合)
総合評価: confidence レベル (high/medium/low)
```
