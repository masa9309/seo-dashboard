# CHANGELOG

`~/seo-report/system/` 配下の主要な変更履歴。

---

## 2026-05-11

### リライト対象抽出ロジックを GSC ベースに刷新

- **背景**: 過去の `spread_*.py` 系作業で WP の `modified` / `date` が広範囲に汚染され、`recency_basis` 判定が機能不全(morinotakumi 0/153件 / mri-tmg 0/101件 抽出)
- **新ロジック (extractors/extract_rewrite_targets.py)**:
  - 必須要件: imp_30d ≥ サイト別閾値 (codev/morino=100、mri-tmg=50) / position 6.0〜20.0 / pos > 3 安全弁 / last_rewrite ≥ 30日
  - スコア再設計: `s_imp(0.30) + s_ctr_gap(0.40) + s_decline(0.10) + s_kws(0.20)`
  - URL fragment 集約: `/post#sec1` と `/post` を同一記事として imp 合算
  - **modified 由来コード完全削除** (`recency_basis`, `s_mod`, 公開60日要件)
  - `days_since_modified` は出力 JSON に参考メタとして残置(スコアには非関与)
- **sites.yaml**: `rewrite_recency_basis` 削除、`rewrite_criteria` / `priority_weights` を新キー構成に変更、`mri-tmg.min_imp_30d_override: 50` 追加
- **抽出結果 (2026-05-11 実測)**:
  - codevillage: **11 件** (旧: 6) — 集約で重複統合
  - mri-tmg: **10 件** (旧: 0)
  - morinotakumi: **74 件** (旧: 0)
  - ※ Phase 1 シミュレーション (集約なし版) 46/17/174 から減ったのは Q3 で承認した URL fragment 集約の効果。GSC データの大半が fragment 違いの重複行(例: morino 30d は 537 行 → 集約後 173 URL)
- **後退防止テスト追加**: `test_modified_field_unused` で post.modified を変えても抽出結果が変わらないことを保証
- **テスト**: 新規 10 ケース全 PASS (`tests/test_extract_rewrite_targets.py`)

### 後藤さんの判断事項(Phase 1 で確定)

| Q | 判断 |
|---|---|
| Q1. position 上限 | 上限あり(20 位) |
| Q2. mri-tmg の閾値 | サイト別調整: mri-tmg のみ imp ≥ 50、他は imp ≥ 100 |
| Q3. 重複 URL の扱い | 集約する(URL fragment を切って imp 合算) |
| Q4. CTR < 50% の格上げ | 必須要件にしない(スコア入力に留める) |
| Q5. 1日あたりの処理本数 | 現状維持(朝1+夕1)+ Q2 で対応 |
