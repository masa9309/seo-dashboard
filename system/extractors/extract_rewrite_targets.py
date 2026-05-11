#!/usr/bin/env python3
"""3サイトのリライト対象を抽出する (analysis only, no PUT)。

抽出条件 (GSC ベース・modified 一切不使用):
  必須:
    - imp_30d ≥ サイト別閾値 (cfg.min_imp_30d_override or criteria.min_imp_30d)
    - position 90d が pos_min ≤ pos ≤ pos_max のレンジ内
    - 安全弁: pos ≤ exclude_position_top の上位記事は対象外
    - 過去 N 日以上リライトしていない (~/seo-report/rewrite_log.json)

スコア (高いほど優先):
    - s_imp        = min(1, imp_30d / 2000)             weight: high_imp
    - s_ctr_gap    = max(0, 1 - cur_ctr / expected_ctr) weight: ctr_below_expected
    - s_decline    = max(0, min(1, (pos_30d - pos_90d)/5))   weight: position_decline
    - s_kws        = min(1, n_kws_imp10 / 30)           weight: kw_breadth

URL fragment は切って imp 合算 (`/post#sec1` と `/post` を同一記事として扱う)。

出力:
  ~/seo-report/system/output/rewrite_targets/{slug}_targets.json
  ~/seo-report/system/output/rewrite_targets/all_targets_summary.md
"""

import argparse
import csv
import json
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

try:
    import yaml
except ImportError:
    sys.exit("[error] PyYAML 未インストール: pip install --user pyyaml")

ROOT = Path.home() / "seo-report"
SYSTEM = ROOT / "system"
CONFIG = SYSTEM / "config" / "sites.yaml"
OUT_DIR = SYSTEM / "output" / "rewrite_targets"
GSC_DATA = ROOT / "output" / "site_analysis" / "data"
REWRITE_LOG = ROOT / "rewrite_log.json"

OUT_DIR.mkdir(parents=True, exist_ok=True)


def load_yaml(p):
    return yaml.safe_load(open(p, encoding="utf-8"))


def load_csv(p):
    rows = list(csv.DictReader(open(p, encoding="utf-8")))
    for r in rows:
        r["clicks"] = int(r["clicks"])
        r["impressions"] = int(r["impressions"])
        r["ctr"] = float(r["ctr"])
        r["position"] = float(r["position"])
    return rows


def expected_ctr_for_position(pos: float) -> float:
    """position 別の経験的期待 CTR (簡易テーブル)"""
    table = [(1, 0.27), (2, 0.15), (3, 0.10), (4, 0.07), (5, 0.05),
             (6, 0.04), (7, 0.03), (8, 0.025), (9, 0.02), (10, 0.018),
             (15, 0.012), (20, 0.008), (30, 0.005), (50, 0.002)]
    for p_thr, ctr in table:
        if pos <= p_thr:
            return ctr
    return 0.001


def load_rewrite_log() -> dict:
    if REWRITE_LOG.exists():
        try:
            return json.loads(REWRITE_LOG.read_text())
        except Exception:
            return {}
    return {}


def days_since(ts_str, now: datetime) -> int:
    try:
        dt = datetime.fromisoformat(str(ts_str).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (now - dt).days
    except Exception:
        return 99999


def _normalize_url(u: str) -> str:
    return u.split("#")[0]


def _aggregate_pages_by_url(rows):
    """imp 加重平均で position・ctr を、imp/clicks は合算。

    GSC は同一 URL を fragment 違いで複数行に出すことがあるため、
    fragment を切ったうえで集約する。
    """
    agg = defaultdict(lambda: {"clicks": 0, "impressions": 0, "_pos_imp": 0.0})
    for r in rows:
        k = _normalize_url(r["page"])
        agg[k]["clicks"] += r["clicks"]
        agg[k]["impressions"] += r["impressions"]
        agg[k]["_pos_imp"] += r["position"] * r["impressions"]
    out = {}
    for k, v in agg.items():
        imp = v["impressions"]
        out[k] = {
            "clicks": v["clicks"],
            "impressions": imp,
            "position": (v["_pos_imp"] / imp) if imp > 0 else 0.0,
            "ctr": (v["clicks"] / imp) if imp > 0 else 0.0,
        }
    return out


def extract_for_site(slug, cfg, criteria, weights, now, log):
    site_dir = GSC_DATA / slug
    if not site_dir.exists():
        print(f"  [warn] {site_dir} が存在しません。先に fetch_3sites_data.py を実行してください")
        return []

    p90 = load_csv(site_dir / "page_90d.csv")
    p30 = load_csv(site_dir / "page_30d.csv")
    qp90 = load_csv(site_dir / "query_page_90d.csv")
    posts = json.loads((site_dir / "wp_posts.json").read_text())

    # URL fragment 集約
    p90_agg = _aggregate_pages_by_url(p90)
    p30_agg = _aggregate_pages_by_url(p30)

    # query×page を URL正規化キーで集計
    qs_by_url = defaultdict(list)
    for r in qp90:
        qs_by_url[_normalize_url(r["page"])].append(r)

    posts_by_link = {p["link"].rstrip("/"): p for p in posts}

    # サイト別 imp 閾値 (cfg override > criteria default)
    min_imp = cfg.get("min_imp_30d_override", criteria["min_imp_30d"])
    pos_min = criteria["pos_min"]
    pos_max = criteria["pos_max"]
    exclude_top = criteria["exclude_position_top"]
    min_rewrite_days = criteria["min_days_since_last_rewrite"]

    candidates = []
    for url_norm, agg in p90_agg.items():
        post = posts_by_link.get(url_norm.rstrip("/"))
        if not post:
            continue
        pid = post["id"]
        pos90 = agg["position"]

        if pos90 <= exclude_top:
            continue
        if pos90 < pos_min or pos90 > pos_max:
            continue

        agg30 = p30_agg.get(url_norm)
        if not agg30:
            continue
        imp30 = agg30["impressions"]
        if imp30 < min_imp:
            continue

        # rewrite_log の最終リライト日 (本格リライト直後の再リライト防止)
        log_keys = [f"{slug}_{pid}"]
        if slug == "morinotakumi":
            log_keys.append(f"1_{pid}")
        if slug == "mri-tmg":
            log_keys.append(f"2_{pid}")
        last_rewrite_ts = None
        for k in log_keys:
            entry = log.get(k)
            if entry:
                ts = entry.get("rewritten_at") if isinstance(entry, dict) else entry
                if ts:
                    last_rewrite_ts = ts
                    break
        days_since_rewrite = days_since(last_rewrite_ts, now) if last_rewrite_ts else None
        if days_since_rewrite is not None and days_since_rewrite < min_rewrite_days:
            continue

        # スコア計算 (modified 由来 s_mod は廃止)
        s_imp = min(1.0, imp30 / 2000.0)
        exp_ctr = expected_ctr_for_position(pos90)
        cur_ctr = agg["ctr"]
        ctr_ratio = cur_ctr / exp_ctr if exp_ctr > 0 else 1.0
        s_ctr_gap = max(0.0, 1.0 - ctr_ratio)
        delta = agg30["position"] - pos90
        s_decline = max(0.0, min(1.0, delta / 5.0))
        n_kws = sum(1 for q in qs_by_url.get(url_norm, []) if q["impressions"] >= 10)
        s_kws = min(1.0, n_kws / 30.0)

        score = (
            s_imp * weights["high_imp"] +
            s_ctr_gap * weights["ctr_below_expected"] +
            s_decline * weights["position_decline"] +
            s_kws * weights["kw_breadth"]
        )

        # 参考メタ (statistics 用に残置・スコア計算には使わない)
        days_mod = days_since(post.get("modified"), now)

        primary_qs = sorted(qs_by_url.get(url_norm, []), key=lambda q: -q["impressions"])
        primary_qs = [q["query"] for q in primary_qs[:3]]

        candidates.append({
            "post_id": pid,
            "url": url_norm,
            "title": re.sub(r"<[^>]+>", "", post["title"]["rendered"]),
            "pos_90d": pos90,
            "pos_30d": agg30["position"],
            "delta_pos": delta,
            "imp_90d": agg["impressions"],
            "imp_30d": imp30,
            "ctr_90d": cur_ctr,
            "expected_ctr": exp_ctr,
            "ctr_ratio": ctr_ratio,
            "n_kws_imp10": n_kws,
            "days_since_modified": days_mod,
            "days_since_rewrite": days_since_rewrite,
            "primary_queries": primary_qs,
            "categories": post.get("categories", []),
            "score": round(score, 4),
            "score_breakdown": {
                "high_imp": round(s_imp, 3),
                "ctr_below_expected": round(s_ctr_gap, 3),
                "position_decline": round(s_decline, 3),
                "kw_breadth": round(s_kws, 3),
            },
        })

    candidates.sort(key=lambda x: -x["score"])
    return candidates


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--site", help="特定サイトのみ (codevillage / mri-tmg / morinotakumi)")
    ap.add_argument("--top", type=int, default=30, help="出力上位件数 (default: 30)")
    args = ap.parse_args()

    config = load_yaml(CONFIG)
    sites = config["sites"]
    criteria = config["rewrite_criteria"]
    weights = config["priority_weights"]

    log = load_rewrite_log()
    now = datetime.now(timezone.utc)

    summary_lines = [f"# リライト対象抽出結果 ({now.strftime('%Y-%m-%d')})\n"]
    summary_lines.append("## 抽出条件 (GSC ベース)")
    for k, v in criteria.items():
        summary_lines.append(f"- {k}: {v}")
    summary_lines.append("- スコア重み: " + ", ".join(f"{k}={v}" for k, v in weights.items()))
    summary_lines.append("")

    target_slugs = [args.site] if args.site else list(sites.keys())
    for slug in target_slugs:
        if slug not in sites:
            print(f"  [skip] unknown site: {slug}")
            continue
        cfg = sites[slug]
        eff_imp = cfg.get("min_imp_30d_override", criteria["min_imp_30d"])
        print(f"\n[extract] {cfg['name']} (min_imp_30d={eff_imp})")
        candidates = extract_for_site(slug, cfg, criteria, weights, now, log)
        out = OUT_DIR / f"{slug}_targets.json"
        out.write_text(json.dumps(candidates[:args.top], ensure_ascii=False, indent=2))
        print(f"  candidates: {len(candidates)} (full) / 上位{args.top}件保存")
        summary_lines.append(f"\n## {cfg['name']}\n")
        summary_lines.append(f"- 候補総数: {len(candidates)} / 上位{args.top}件\n")
        summary_lines.append("| # | post_id | score | pos90→pos30 | imp_30d | ctr_ratio | n_kws | last_rewrite | title |")
        summary_lines.append("|--:|--:|--:|---|--:|--:|--:|---|---|")
        for i, c in enumerate(candidates[:args.top], 1):
            lr = f"{c['days_since_rewrite']}日前" if c['days_since_rewrite'] is not None else "未"
            title = c["title"][:35] + ("…" if len(c["title"]) > 35 else "")
            summary_lines.append(
                f"| {i} | {c['post_id']} | {c['score']:.3f} | "
                f"{c['pos_90d']:.1f}→{c['pos_30d']:.1f} | {c['imp_30d']} | "
                f"{c['ctr_ratio']:.2f} | {c['n_kws_imp10']} | {lr} | {title} |"
            )

    out_md = OUT_DIR / "all_targets_summary.md"
    out_md.write_text("\n".join(summary_lines))
    print(f"\n[ok] サマリー: {out_md}")


if __name__ == "__main__":
    main()
