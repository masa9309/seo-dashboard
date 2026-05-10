#!/usr/bin/env python3
"""1日の処理オーケストレータ (DRY_RUN 既定)。

スケジュール:
  朝 (--shift morning):
    - 各サイト 1 リライト (計3記事)
    - Phase A 期間中: morinotakumi タイトル年号更新を 1-2 件
  夕 (--shift evening):
    - 各サイト 1 リライト (計3記事)
    - CV 直結記事 1 本 (各サイトをローテ)

cron 例 (DRY_RUN):
  0 9 * * *  python3 ~/seo-report/system/orchestrator/daily_orchestrator.py --shift morning
  0 18 * * * python3 ~/seo-report/system/orchestrator/daily_orchestrator.py --shift evening

本番 (--execute) に切り替えるのは品質確認後・後藤承認後。
"""

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import yaml

ROOT = Path.home() / "seo-report"
SYSTEM = ROOT / "system"
TARGETS_DIR = SYSTEM / "output" / "rewrite_targets"
LOG_PATH = SYSTEM / "logs" / "orchestrator.json"
LOG_PATH.parent.mkdir(parents=True, exist_ok=True)

REWRITE = SYSTEM / "generators" / "rewriter.py"
CVGEN = SYSTEM / "generators" / "cv_article_generator.py"
PHASE_A = SYSTEM / "phase_a" / "phase_a_mt002_title_year.py"

CV_DONE_KWS_FILE = SYSTEM / "config" / "cv_done_kws.json"


def pop_next_rewrite_target(slug):
    """rewrite_targets から「まだ処理していない」TOP1 を取り出す (state ファイル管理)"""
    targets_path = TARGETS_DIR / f"{slug}_targets.json"
    if not targets_path.exists():
        print(f"  [warn] {targets_path} なし")
        return None
    targets = json.load(open(targets_path, encoding="utf-8"))
    state_file = SYSTEM / "config" / f"rewrite_done_{slug}.json"
    done = set(json.load(open(state_file)).get("done_post_ids", [])) if state_file.exists() else set()
    for t in targets:
        if t["post_id"] not in done:
            return t["post_id"]
    return None


def _flat_cv_kws():
    """sites.yaml の cv_direct_kws をフラット化して [(site, kw), ...] を返す"""
    sites_yaml = yaml.safe_load(open(SYSTEM / "config" / "sites.yaml", encoding="utf-8"))
    cv_kws = sites_yaml["cv_direct_kws"]
    flat = []
    for slug, groups in cv_kws.items():
        if isinstance(groups, dict):
            for sub, kws in groups.items():
                if isinstance(kws, dict):
                    for k2, kws2 in kws.items():
                        for kw in kws2:
                            flat.append((slug, kw))
                elif isinstance(kws, list):
                    for kw in kws:
                        flat.append((slug, kw))
        elif isinstance(groups, list):
            for kw in groups:
                flat.append((slug, kw))
    return flat


def _load_cv_done_set():
    if CV_DONE_KWS_FILE.exists():
        try:
            data = json.loads(CV_DONE_KWS_FILE.read_text())
            return {(d["site"], d["kw"]) for d in data.get("done", [])}
        except Exception:
            pass
    return set()


def next_cv_kw(execute_mode=False):
    """旧仕様: 全体ローテで未生成の (site, kw) を 1 件返す。全件 done なら None。
    parallel_mode=false の場合のみ使用。
    """
    flat = _flat_cv_kws()
    done_set = _load_cv_done_set()
    for site, kw in flat:
        if (site, kw) not in done_set:
            return site, kw
    return None


def next_cv_kw_for_site(site_slug, done_set=None):
    """parallel_mode 用: 指定サイトの未生成 KW を 1 件返す。サイト全消化なら None。

    done_set を渡せば I/O 不要 (3 サイト連続呼び出しを 1 回の cv_done 読み取りで)。
    """
    flat = _flat_cv_kws()
    if done_set is None:
        done_set = _load_cv_done_set()
    for site, kw in flat:
        if site != site_slug:
            continue
        if (site, kw) not in done_set:
            return (site, kw)
    return None


def cv_remaining_per_site():
    """各サイトの残 KW 数の dict を返す (ログ用)"""
    flat = _flat_cv_kws()
    done_set = _load_cv_done_set()
    counts = {}
    for site, kw in flat:
        counts.setdefault(site, {"total": 0, "done": 0})
        counts[site]["total"] += 1
        if (site, kw) in done_set:
            counts[site]["done"] += 1
    return {s: {**c, "remaining": c["total"] - c["done"]} for s, c in counts.items()}


def run(cmd):
    """サブプロセスを実行し結果を返す (--execute フラグの有無は cmd 側で制御済の前提)"""
    print(f"\n  [run] {' '.join(cmd)}")
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        return {"returncode": result.returncode,
                "stdout_tail": result.stdout[-500:], "stderr_tail": result.stderr[-300:]}
    except Exception as e:
        return {"returncode": -1, "error": str(e)}


# rewriter / cv_article_generator の終了コード規約 (修正4)
RC_SUCCESS = 0
RC_API_ERROR = 1
RC_LENGTH_VIOLATION = 2
RC_NO_REGISTER_POST_META = 3


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--shift", choices=["morning", "evening"], required=True)
    ap.add_argument("--execute", action="store_true",
                    help="各サブスクリプトに --execute --confirm を渡す (本番)")
    ap.add_argument("--phase-a", action="store_true",
                    help="Phase A の morinotakumi タイトル更新も実行 (期間限定)")
    args = ap.parse_args()

    started = datetime.now().isoformat(timespec="seconds")
    print(f"\n=== orchestrator: shift={args.shift} mode={'EXECUTE' if args.execute else 'DRY-RUN'} ({started}) ===")

    sub_results = []
    sites = ["codevillage", "mri-tmg", "morinotakumi"]

    # 1. 各サイト 1 リライト
    print(f"\n[1] リライト 各サイト1記事 (計{len(sites)}記事)")
    for slug in sites:
        pid = pop_next_rewrite_target(slug)
        if pid is None:
            print(f"  [skip] {slug}: 未処理ターゲットなし")
            sub_results.append({"step": "rewrite", "slug": slug, "skipped": "no_target"})
            continue
        cmd = ["python3", str(REWRITE), "--site", slug, "--post-id", str(pid)]
        if args.execute:
            cmd += ["--use-claude", "--execute", "--confirm"]
        r = run(cmd)
        rc = r.get("returncode", -1)
        # mark_done は rewriter.py 側が成功時に書く (修正3-B)。orchestrator では returncode を解釈してログのみ。
        rc_label = {
            RC_SUCCESS: "ok",
            RC_API_ERROR: "api_error_retry_next",
            RC_LENGTH_VIOLATION: "length_violation_blocked",
            RC_NO_REGISTER_POST_META: "no_register_post_meta_blocked",
        }.get(rc, f"unknown_{rc}")
        sub_results.append({"step": "rewrite", "slug": slug, "post_id": pid, "rc_label": rc_label, **r})
        if rc != RC_SUCCESS and args.execute:
            print(f"  ⚠ rewriter rc={rc} ({rc_label}) — done に記録せず、次回も対象として残る")

    # 2. (Phase A 期間中) morinotakumi タイトル年号更新 1-2 件
    if args.phase_a and args.shift == "morning":
        print(f"\n[2] Phase A: morinotakumi タイトル年号更新 (1件)")
        cmd = ["python3", str(PHASE_A), "--limit", "1"]
        if args.execute:
            cmd += ["--execute", "--confirm"]
        r = run(cmd)
        sub_results.append({"step": "phase_a", **r})

    # 3. 夕方は CV直結記事を生成
    #    parallel_mode=true (default): 各サイト 1 本 (計3記事/日)
    #    parallel_mode=false: 全体ローテで 1 本/日 (旧仕様)
    if args.shift == "evening":
        cv_cfg = (yaml.safe_load(open(SYSTEM / "config" / "sites.yaml", encoding="utf-8"))
                  .get("cv_direct", {}))
        parallel_mode = cv_cfg.get("parallel_mode", True)
        # 残 KW 数を表示
        remaining = cv_remaining_per_site()
        print(f"\n[3] CV直結記事 (mode={'parallel' if parallel_mode else 'rotation'})")
        for slug, c in remaining.items():
            print(f"   - {slug}: 残 {c['remaining']}/{c['total']} (done {c['done']})")

        if parallel_mode:
            # 全サイト並行モード
            done_set = _load_cv_done_set()  # 1 回だけ I/O
            picks = []
            for slug in sites:
                nxt = next_cv_kw_for_site(slug, done_set=done_set)
                if nxt is None:
                    print(f"   [skip] {slug}: 全KW消化済")
                    sub_results.append({"step": "cv_direct", "slug": slug, "skipped": "site_exhausted"})
                else:
                    picks.append(nxt)
            if not picks:
                print(f"\n   ✅ 全サイト・全KW消化済 — CV処理スキップ (リライトのみ継続)")
                sub_results.append({"step": "cv_direct", "skipped": "all_sites_exhausted"})
            for slug, kw in picks:
                print(f"\n   → CV生成: site={slug} kw={kw!r}")
                cmd = ["python3", str(CVGEN), "--site", slug, "--kw", kw]
                if args.execute:
                    cmd += ["--use-claude", "--execute", "--confirm"]
                r = run(cmd)
                rc = r.get("returncode", -1)
                sub_results.append({"step": "cv_direct", "slug": slug, "kw": kw, "rc": rc, **r})
        else:
            # 旧仕様: 全体ローテ
            nxt = next_cv_kw(execute_mode=args.execute)
            if nxt is None:
                print(f"\n[3] CV直結: 全KW生成済 (cv_done_kws.json) — スキップ")
                sub_results.append({"step": "cv_direct", "skipped": "all_done"})
            else:
                slug, kw = nxt
                print(f"\n[3] CV直結記事 1本: site={slug} kw={kw!r}")
                cmd = ["python3", str(CVGEN), "--site", slug, "--kw", kw]
                if args.execute:
                    cmd += ["--use-claude", "--execute", "--confirm"]
                r = run(cmd)
                rc = r.get("returncode", -1)
                sub_results.append({"step": "cv_direct", "slug": slug, "kw": kw, "rc": rc, **r})

    finished = datetime.now().isoformat(timespec="seconds")
    log_entry = {"started": started, "finished": finished, "shift": args.shift,
                 "execute": args.execute, "phase_a": args.phase_a,
                 "results": sub_results}
    log = []
    if LOG_PATH.exists():
        try: log = json.loads(LOG_PATH.read_text())
        except: pass
    log.append(log_entry)
    LOG_PATH.write_text(json.dumps(log, ensure_ascii=False, indent=2))
    print(f"\n[done] {finished} → {LOG_PATH}")


if __name__ == "__main__":
    main()
