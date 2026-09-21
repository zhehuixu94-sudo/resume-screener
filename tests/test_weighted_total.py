# -*- coding: utf-8 -*-
"""第二步的测试：新总分公式 + 具体性/空话信号。

盯住四条容易退化的性质：
  1. 空话率/具体性对"条目数"免疫（均值口径）——否则注水型靠堆术语就能得分；
  2. 加法口径下基础项为空也能拿加分项（旧乘法口径会把加分项清零）；
  3. 分位归一抗离群（一份吹得特别猛的简历不能把别人全压低）；
  4. 高置信注水判定必须苛刻（浅层规则塞一个数字就绕过 → 阈值要含 number_rate=0）。
"""
from __future__ import annotations

from app.pipeline.honesty import (batch_percentiles, honesty_penalty, is_fluff,
                                  is_high_confidence_fluff, quantile_flags, quantile_penalty,
                                  resume_signals, span_concreteness)
from app.pipeline.weighted_total import (BONUS_BUDGET, base_points, bonus_raw, bonus_raw_v2,
                                         percentile_rank, score_batch, skill_words,
                                         span_quality, total_score)

GOOD = "考勤统计与社保公积金资料整理（每月 200 余人次，零差错）"
FLUFF = "沟通协调能力强，做事细致有耐心"
MIXED = "2024.7-2024.12  南昌睿信人力资源有限公司  行政助理实习生"


def test_span_concreteness_orders_good_above_fluff() -> None:
    assert span_concreteness(GOOD) > span_concreteness(FLUFF)
    assert span_concreteness(GOOD) > 0.6            # 数字 + 结果 → 4/6 以上
    assert span_concreteness(FLUFF) <= 1 / 6        # 只有空话词，四要素全无


def test_fluff_requires_no_number() -> None:
    assert is_fluff(FLUFF) is True
    assert is_fluff("熟悉 Office 办公软件，日均处理 30 余份文件") is False   # 有数字 → 不算空话


def test_signals_are_immune_to_span_count() -> None:
    """把同一批空话重复 10 遍，空话率不能变——否则注水型靠堆术语就能改分。"""
    few = resume_signals([FLUFF, GOOD])
    many = resume_signals([FLUFF] * 10 + [GOOD] * 10)
    assert abs(few["fluff_rate"] - many["fluff_rate"]) < 1e-9
    assert abs(few["concreteness"] - many["concreteness"]) < 1e-9


def test_empty_resume_is_treated_as_fluff() -> None:
    s = resume_signals([])
    assert s["n_spans"] == 0 and s["fluff_rate"] == 1.0
    assert honesty_penalty(s, cap=30) == 30.0


def test_penalty_variants_and_cap() -> None:
    s = {"fluff_rate": 0.8, "concreteness": 0.2}
    assert honesty_penalty(s, cap=30, signal="fluff") == 24.0
    assert honesty_penalty(s, cap=30, signal="concreteness") == 24.0
    assert honesty_penalty(s, cap=30, signal="sqrt_fluff") == round(30 * 0.8 ** 0.5, 10) or \
        abs(honesty_penalty(s, cap=30, signal="sqrt_fluff") - 30 * 0.8 ** 0.5) < 1e-9
    assert honesty_penalty({"fluff_rate": 2.0}, cap=30) == 30.0      # 越界被夹住


def test_high_confidence_requires_no_numbers_at_all() -> None:
    base = {"n_spans": 20, "fluff_rate": 0.9, "number_rate": 0.0}
    assert is_high_confidence_fluff(base) is True
    assert is_high_confidence_fluff({**base, "number_rate": 0.05}) is False   # 塞一个数字就绕过
    assert is_high_confidence_fluff({**base, "n_spans": 3}) is False          # 样本太少不许直接淘汰


def test_base_points_uses_strongest_item_per_dimension() -> None:
    dims = [{"id": "a", "items": [{"relevance_score": 1.0}, {"relevance_score": 0.2}]},
            {"id": "b", "items": []}]
    assert base_points(dims, {"a": 1, "b": 1}) == 30.0      # a 满、b 有证据但为空 → 0
    assert base_points(dims, {"a": 3, "b": 1}) == 45.0      # 权重按占比分预算
    assert base_points([], {"a": 1}) == 0.0


def test_additive_not_multiplicative() -> None:
    """基础项为 0 时，加分项仍然全额给——这是和旧乘法口径最关键的区别。"""
    assert total_score(0.0, 1.0, 0.0) == BONUS_BUDGET          # 定稿 scale=25（原 40）
    assert total_score(60.0, 1.0, 0.0) == 60.0 + BONUS_BUDGET
    assert total_score(60.0, 1.0, 30.0) == 60.0 + BONUS_BUDGET - 30.0
    assert total_score(0.0, 0.0, 50.0) == 0.0               # 下界 0，不出现负分


def test_percentile_rank_is_outlier_resistant() -> None:
    flat = {"a": 5.0, "b": 6.0, "c": 7.0}
    crazy = {**flat, "d": 1000.0}                            # 一个吹上天的
    r1, r2 = percentile_rank(flat), percentile_rank(crazy)
    assert r1["a"] == 0.0 and r1["c"] == 1.0
    assert r2["a"] == 0.0 and r2["c"] == 2 / 3               # a/c 的相对位置不因离群点改变
    assert percentile_rank({}) == {} and percentile_rank({"x": 1.0}) == {"x": 0.5}


def test_ties_get_identical_rank() -> None:
    """同分的简历必须拿到同一个加分——否则等于按文件名顺序发分（并列取平均位置）。"""
    r = percentile_rank({"a": 5.0, "b": 5.0, "c": 9.0, "d": 1.0})
    assert r["a"] == r["b"] == (1 + 2) / 2 / 3
    assert r["d"] == 0.0 and r["c"] == 1.0


def test_score_batch_end_to_end() -> None:
    good = {"key": "g", "dimensions": [{"id": "a", "items": [{"relevance_score": 1.0, "difficulty_score": 1.2}]}]}
    fluff = {"key": "f", "dimensions": [{"id": "a", "items": [{"relevance_score": 1.0, "difficulty_score": 1.2}]}]}
    res = score_batch([good, fluff], {"a": 1.0},
                      honesty_signals={"g": {"fluff_rate": 0.1, "n_spans": 10},
                                       "f": {"fluff_rate": 0.9, "n_spans": 10}}, penalty_cap=30)
    assert res["g"]["bonus_raw"] == res["f"]["bonus_raw"]           # 打分相同
    # 扣分是**非线性**的：30 × 空话率^1.5
    assert res["g"]["penalty"] == round(30 * 0.1 ** 1.5, 2)
    assert res["f"]["penalty"] == round(30 * 0.9 ** 1.5, 2)
    assert res["g"]["total"] > res["f"]["total"]                    # 靠注水扣分拉开


def test_nonlinear_penalty_shape_is_measured_not_guessed() -> None:
    """非线性扣分的方向与取值，按 300 份对照语料实测（out/weighted_sweep.py）钉死。

    指数 >1 ⇒ 每单位空话率的代价随空话率上升（低空话便宜、高空话贵），这正是我们要的方向：
      · 真实简历（空话率 p90 = 0.29）扣分 ≤5 分，不许拿措辞风格开刀；
      · 注水型（空话率 p50 = 0.62）实打实扣十几分。
    指数 <1 会把这两条都破坏（0.29 就要扣 12 分），所以固定 ≥1。
    """
    from app.pipeline.honesty import fluff_penalty
    real_p90, fluff_p50 = 0.29, 0.62
    assert fluff_penalty({"fluff_rate": real_p90}, cap=30) <= 5.0        # 不误伤真人
    assert fluff_penalty({"fluff_rate": fluff_p50}, cap=30) >= 12.0      # 注水型真被扣
    # 每单位代价递增（非线性，而不是按比例平摊）
    unit_low = fluff_penalty({"fluff_rate": 0.1}, cap=30) / 0.1
    unit_high = fluff_penalty({"fluff_rate": 0.9}, cap=30) / 0.9
    assert unit_high > unit_low
    assert fluff_penalty({"fluff_rate": 2.0}, cap=30) == 30.0            # 越界夹住
    assert fluff_penalty({"fluff_rate": 0.0}, cap=30) == 0.0


# ————— 分位口径（真实数据标定用：绝对阈值是拿构造语料定的，搬到真实数据会错位）—————


def test_batch_percentiles_monotone_and_tie_stable() -> None:
    sig = {"low": {"fluff_rate": 0.0}, "mid": {"fluff_rate": 0.5},
           "also_mid": {"fluff_rate": 0.5}, "high": {"fluff_rate": 0.9}}
    p = batch_percentiles(sig)
    assert p["low"] == 0.0 and p["high"] == 1.0
    assert p["mid"] == p["also_mid"] == (1 + 2) / 2 / 3      # 并列同分位
    assert batch_percentiles({}) == {}
    assert batch_percentiles({"only": {"fluff_rate": 0.4}}) == {"only": 0.5}


def test_quantile_penalty_only_punishes_the_tail() -> None:
    assert quantile_penalty(0.0) == 0.0
    assert quantile_penalty(0.5) == 0.0
    assert quantile_penalty(0.90) == 0.0                     # 尾部起点不扣
    assert abs(quantile_penalty(0.95) - 15.0) < 1e-9         # 尾部中点 → 一半
    assert quantile_penalty(1.0) == 30.0                     # 最水 → 满扣
    assert abs(quantile_penalty(0.95, cap=20) - 10.0) < 1e-6      # 浮点：0.95−0.9 不精确


def test_quantile_flags_mark_only_top_tails() -> None:
    sig = {f"r{i}": {"fluff_rate": i / 99} for i in range(100)}     # 0.00 ~ 0.99
    fl = quantile_flags(sig)
    assert sum(1 for v in fl.values() if v["high_confidence"]) <= 2
    assert 8 <= sum(1 for v in fl.values() if v["suspect"]) <= 12   # 约 10%
    assert fl["r0"]["suspect"] is False and fl["r99"]["high_confidence"] is True


def test_quantile_is_scale_invariant() -> None:
    """整体都水的一批 vs 整体都干的一批：分位口径挑出的是同一批人（相对位置），绝对阈值做不到。"""
    dry = {"a": {"fluff_rate": 0.01}, "b": {"fluff_rate": 0.02}, "c": {"fluff_rate": 0.30}}
    wet = {"a": {"fluff_rate": 0.50}, "b": {"fluff_rate": 0.52}, "c": {"fluff_rate": 0.90}}
    assert [quantile_penalty(batch_percentiles(d)[k]) > 0 for d in (dry, wet) for k in ("a", "b", "c")] == \
        [False, False, True] * 2

def test_bonus_not_constant_when_difficulty_missing() -> None:
    """加分项 = Σ 相关度 × 难度：只给 relevance_score 会让它恒为 0（整批并列 → 加分项变常数）。

    实测事故：整批 310 份的加分项都算成 20.0 分（分位 0.50），占 40 分的一项完全不区分人。
    """
    from app.pipeline.score import dimensions_from_labeled
    from app.contracts import JobConfig
    import json as _json
    import os as _os
    root = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
    cfg = JobConfig.model_validate(
        _json.load(open(_os.path.join(root, "config", "job_game_dev.json"), encoding="utf-8")))
    dim = cfg.dimensions[0].id
    labeled = [{"dimension": dim, "relevance_score": 1.0, "difficulty_score": 1.4},
               {"dimension": dim, "relevance_score": 0.6, "difficulty_score": 0.5}]
    dims = dimensions_from_labeled(cfg, labeled)
    assert bonus_raw(dims) > 0, "difficulty_score 丢了 → 加分项恒为 0"
    assert abs(bonus_raw(dims) - (1.0 * 1.4 + 0.6 * 0.5)) < 1e-6
