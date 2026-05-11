"""extractors.extract_rewrite_targets の新ロジック (GSC ベース) のテスト。

設計ドキュメント: ~/seo-report/system/CHANGELOG.md (2026-05-11 エントリ)
"""

import csv
import json
import shutil
from datetime import datetime, timezone

import pytest

from extractors import extract_rewrite_targets as ert


PAGE_FIELDS = ["page", "clicks", "impressions", "ctr", "position"]
QP_FIELDS = ["query", "page", "clicks", "impressions", "ctr", "position"]


def _write_csv(path, rows, fieldnames):
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)


def _make_post(pid, link, modified="2025-01-01T00:00:00", title="Test"):
    return {
        "id": pid,
        "link": link,
        "modified": modified,
        "date": "2025-01-01T00:00:00",
        "title": {"rendered": title},
        "categories": [1],
    }


def _page_row(url, *, imp=200, clicks=10, ctr=None, position=8.0):
    if ctr is None:
        ctr = clicks / imp if imp else 0
    return {"page": url, "clicks": clicks, "impressions": imp, "ctr": ctr, "position": position}


def _qp_row(url, *, query="kw1", imp=50, clicks=2, ctr=None, position=8.0):
    if ctr is None:
        ctr = clicks / imp if imp else 0
    return {"query": query, "page": url, "clicks": clicks, "impressions": imp, "ctr": ctr, "position": position}


def _make_fixture(root, slug, page_90, page_30, qp, posts):
    site_dir = root / slug
    site_dir.mkdir(parents=True, exist_ok=True)
    _write_csv(site_dir / "page_90d.csv", page_90, PAGE_FIELDS)
    _write_csv(site_dir / "page_30d.csv", page_30, PAGE_FIELDS)
    _write_csv(site_dir / "query_page_90d.csv", qp, QP_FIELDS)
    (site_dir / "wp_posts.json").write_text(json.dumps(posts, ensure_ascii=False))


@pytest.fixture
def now_utc():
    return datetime(2026, 5, 11, tzinfo=timezone.utc)


@pytest.fixture
def base_criteria():
    return {
        "min_imp_30d": 100,
        "pos_min": 6.0,
        "pos_max": 20.0,
        "exclude_position_top": 3.0,
        "min_days_since_last_rewrite": 30,
    }


@pytest.fixture
def base_weights():
    return {
        "high_imp": 0.30,
        "ctr_below_expected": 0.40,
        "position_decline": 0.10,
        "kw_breadth": 0.20,
    }


@pytest.fixture
def patch_gsc(tmp_path, monkeypatch):
    monkeypatch.setattr(ert, "GSC_DATA", tmp_path)
    return tmp_path


def test_imp_below_threshold_skipped(patch_gsc, base_criteria, base_weights, now_utc):
    """imp_30d < min_imp_30d の記事は除外される"""
    url = "https://example.com/post-1/"
    _make_fixture(
        patch_gsc, "codev",
        page_90=[_page_row(url, imp=200, position=8.0)],
        page_30=[_page_row(url, imp=99, position=8.0)],  # 100 未満
        qp=[_qp_row(url, imp=99)],
        posts=[_make_post(1, url)],
    )
    cands = ert.extract_for_site("codev", {}, base_criteria, base_weights, now_utc, {})
    assert cands == []


def test_position_above_3_excluded(patch_gsc, base_criteria, base_weights, now_utc):
    """安全弁: pos ≤ 3 は絶対対象外"""
    url = "https://example.com/post-2/"
    _make_fixture(
        patch_gsc, "codev",
        page_90=[_page_row(url, imp=500, position=2.5)],
        page_30=[_page_row(url, imp=200, position=2.5)],
        qp=[_qp_row(url, imp=200)],
        posts=[_make_post(2, url)],
    )
    cands = ert.extract_for_site("codev", {}, base_criteria, base_weights, now_utc, {})
    assert cands == []


def test_position_below_6_excluded(patch_gsc, base_criteria, base_weights, now_utc):
    """pos < 6 (4-5位帯) は除外"""
    url = "https://example.com/post-3/"
    _make_fixture(
        patch_gsc, "codev",
        page_90=[_page_row(url, imp=500, position=5.0)],
        page_30=[_page_row(url, imp=200, position=5.0)],
        qp=[_qp_row(url, imp=200)],
        posts=[_make_post(3, url)],
    )
    cands = ert.extract_for_site("codev", {}, base_criteria, base_weights, now_utc, {})
    assert cands == []


def test_position_above_20_excluded(patch_gsc, base_criteria, base_weights, now_utc):
    """pos > 20 はリライト ROI 低として除外"""
    url = "https://example.com/post-4/"
    _make_fixture(
        patch_gsc, "codev",
        page_90=[_page_row(url, imp=500, position=21.0)],
        page_30=[_page_row(url, imp=200, position=21.0)],
        qp=[_qp_row(url, imp=200)],
        posts=[_make_post(4, url)],
    )
    cands = ert.extract_for_site("codev", {}, base_criteria, base_weights, now_utc, {})
    assert cands == []


def test_recent_rewrite_excluded(patch_gsc, base_criteria, base_weights, now_utc):
    """rewrite_log 直近 30 日以内のリライトは除外"""
    url = "https://example.com/post-5/"
    _make_fixture(
        patch_gsc, "codev",
        page_90=[_page_row(url, imp=500, position=8.0)],
        page_30=[_page_row(url, imp=200, position=8.0)],
        qp=[_qp_row(url, imp=200)],
        posts=[_make_post(5, url)],
    )
    log = {"codev_5": {"rewritten_at": "2026-05-01T00:00:00+00:00"}}  # 10 日前
    cands = ert.extract_for_site("codev", {}, base_criteria, base_weights, now_utc, log)
    assert cands == []


def test_score_calculation(patch_gsc, base_criteria, base_weights, now_utc):
    """s_imp / s_ctr_gap / s_decline / s_kws の合算が weight と一致する"""
    url = "https://example.com/post-6/"
    # imp_30d = 1000 → s_imp = 0.5
    # cur_ctr = 0.01, expected_ctr(8) = 0.025 → ctr_ratio = 0.4 → s_ctr_gap = 0.6
    # delta_pos = 0 → s_decline = 0.0
    # n_kws (imp ≥ 10) = 6 → s_kws = 6/30 = 0.2
    _make_fixture(
        patch_gsc, "codev",
        page_90=[_page_row(url, imp=2000, clicks=20, ctr=0.01, position=8.0)],
        page_30=[_page_row(url, imp=1000, clicks=10, ctr=0.01, position=8.0)],
        qp=[_qp_row(url, query=f"kw{i}", imp=20) for i in range(6)],
        posts=[_make_post(6, url)],
    )
    cands = ert.extract_for_site("codev", {}, base_criteria, base_weights, now_utc, {})
    assert len(cands) == 1
    c = cands[0]
    expected = 0.5 * 0.30 + 0.6 * 0.40 + 0.0 * 0.10 + 0.2 * 0.20
    assert c["score"] == round(expected, 4)
    assert c["score_breakdown"]["high_imp"] == 0.5
    assert c["score_breakdown"]["ctr_below_expected"] == 0.6
    assert c["score_breakdown"]["position_decline"] == 0.0
    assert c["score_breakdown"]["kw_breadth"] == 0.2


def test_modified_field_unused(patch_gsc, base_criteria, base_weights, now_utc):
    """post.modified を変えても抽出結果が変わらない (後退防止)"""
    url = "https://example.com/post-7/"
    page_90 = [_page_row(url, imp=500, position=8.0)]
    page_30 = [_page_row(url, imp=200, position=8.0)]
    qp = [_qp_row(url, imp=200)]

    # 1回目: modified=昨日 (旧ロジックでは 60日要件 NG で除外されていた)
    _make_fixture(
        patch_gsc, "codev",
        page_90=page_90, page_30=page_30, qp=qp,
        posts=[_make_post(7, url, modified="2026-05-10T00:00:00")],
    )
    cands_recent = ert.extract_for_site("codev", {}, base_criteria, base_weights, now_utc, {})

    # 同 URL で modified だけ 2 年前に書き換えて再抽出
    shutil.rmtree(patch_gsc / "codev")
    _make_fixture(
        patch_gsc, "codev",
        page_90=page_90, page_30=page_30, qp=qp,
        posts=[_make_post(7, url, modified="2024-01-01T00:00:00")],
    )
    cands_old = ert.extract_for_site("codev", {}, base_criteria, base_weights, now_utc, {})

    assert len(cands_recent) == 1
    assert len(cands_old) == 1
    # スコアと breakdown が完全一致 = modified に依存していない
    assert cands_recent[0]["score"] == cands_old[0]["score"]
    assert cands_recent[0]["score_breakdown"] == cands_old[0]["score_breakdown"]


def test_high_ctr_records_get_low_score(patch_gsc, base_criteria, base_weights, now_utc):
    """CTR > expected の記事は s_ctr_gap = 0 になる"""
    url = "https://example.com/post-8/"
    # cur_ctr = 0.05、expected_ctr(8) = 0.025 → ratio = 2.0 → s_ctr_gap = max(0, 1-2) = 0
    _make_fixture(
        patch_gsc, "codev",
        page_90=[_page_row(url, imp=2000, clicks=100, ctr=0.05, position=8.0)],
        page_30=[_page_row(url, imp=1000, clicks=50, ctr=0.05, position=8.0)],
        qp=[_qp_row(url, imp=200)],
        posts=[_make_post(8, url)],
    )
    cands = ert.extract_for_site("codev", {}, base_criteria, base_weights, now_utc, {})
    assert len(cands) == 1
    assert cands[0]["score_breakdown"]["ctr_below_expected"] == 0.0


def test_mri_tmg_uses_lower_imp_threshold(patch_gsc, base_criteria, base_weights, now_utc):
    """サイト別 override: mri-tmg は imp=50 で通過、他サイトは同じ imp=80 で除外"""
    url = "https://example.com/post-9/"
    page_90 = [_page_row(url, imp=200, position=8.0)]
    page_30 = [_page_row(url, imp=80, position=8.0)]  # 100 未満だが 50 以上
    qp = [_qp_row(url, imp=80)]
    posts = [_make_post(9, url)]

    # mri-tmg: override 50 → 通過
    _make_fixture(patch_gsc, "mri-tmg", page_90, page_30, qp, posts)
    cands_mri = ert.extract_for_site(
        "mri-tmg", {"min_imp_30d_override": 50},
        base_criteria, base_weights, now_utc, {},
    )
    assert len(cands_mri) == 1

    # codev: override なし (default 100) → 除外
    _make_fixture(patch_gsc, "codev", page_90, page_30, qp, posts)
    cands_codev = ert.extract_for_site("codev", {}, base_criteria, base_weights, now_utc, {})
    assert cands_codev == []


def test_url_fragment_aggregation(patch_gsc, base_criteria, base_weights, now_utc):
    """/post#sec1 と /post を同一記事として imp 合算する"""
    base = "https://example.com/post-10/"
    posts = [_make_post(10, base)]
    # GSC は fragment 違いで複数行に出すことがある
    page_90 = [
        _page_row(base, imp=300, clicks=15, ctr=0.05, position=8.0),
        _page_row(base + "#section1", imp=200, clicks=10, ctr=0.05, position=10.0),
    ]
    page_30 = [
        _page_row(base, imp=100, clicks=5, ctr=0.05, position=8.0),
        _page_row(base + "#section1", imp=80, clicks=4, ctr=0.05, position=10.0),
    ]
    qp = [_qp_row(base, imp=80)]
    _make_fixture(patch_gsc, "codev", page_90, page_30, qp, posts)
    cands = ert.extract_for_site("codev", {}, base_criteria, base_weights, now_utc, {})

    assert len(cands) == 1
    c = cands[0]
    # fragment 集約後: imp_30d = 100 + 80 = 180、imp_90d = 300 + 200 = 500
    assert c["imp_30d"] == 180
    assert c["imp_90d"] == 500
    # position は imp 加重平均 (90d): (8*300 + 10*200)/500 = 8.8
    assert c["pos_90d"] == pytest.approx(8.8, abs=0.01)
