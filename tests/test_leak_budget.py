# -*- coding: utf-8 -*-
"""漏放率置信上界与「预算内选线」的测试。

盯住的核心事实：0 漏放 / n 份样本的 95% 上界 ≈ 3/n —— 所以"用 20 份样本宣称漏放 1%"是错的，
代码必须在样本不足时拒绝动线，并告诉人还缺多少样本。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.pipeline.stats import (binom_cdf, choose_line_within_budget, leak_upper_bound,   # noqa: E402
                                samples_needed)


def test_binom_cdf_basics() -> None:
    assert abs(binom_cdf(0, 10, 0.0) - 1.0) < 1e-12
    assert abs(binom_cdf(10, 10, 1.0) - 0.0) < 1e-12
    assert abs(binom_cdf(5, 10, 0.5) - 0.623) < 0.001, "对称分布的中位点"


def test_zero_leak_upper_bound_matches_rule_of_three() -> None:
    """0 漏放的 95% 上界应落在 3/n 附近（n=100 → ~2.95%）。"""
    assert abs(leak_upper_bound(0, 100) - 0.0295) < 0.002
    assert abs(leak_upper_bound(0, 300) - 0.0099) < 0.001
    assert leak_upper_bound(0, 20) > 0.10, "20 份样本证明不了 10% 以下"


def test_upper_bound_is_monotone() -> None:
    prev = -1.0
    for n in (5, 10, 20, 50, 100, 300):
        cur = leak_upper_bound(0, n)
        assert cur < prev if prev > 0 else True
        prev = cur
    assert leak_upper_bound(3, 100) > leak_upper_bound(0, 100), "漏放越多上界越高"


def test_samples_needed() -> None:
    assert samples_needed(0.01) == 299, "证明 1% 需要 299 份标注样本（0 漏放）"
    assert 95 <= samples_needed(0.03) <= 100


def test_choose_line_refuses_without_enough_samples() -> None:
    counts = [{"line": 60.0, "leak": 0, "wrong": 0, "manual": 10},
              {"line": 55.0, "leak": 0, "wrong": 1, "manual": 20}]
    best, why = choose_line_within_budget(counts, n_pos=5, budget=0.01)
    assert best is None and "299" in why, "样本不足时必须拒绝并报出还缺多少"
    best, why = choose_line_within_budget(counts, n_pos=5, budget=0.50)   # 放宽到能证明的范围
    assert best is not None and best["line"] == 60.0


def test_choose_line_picks_least_manual_within_budget() -> None:
    counts = [{"line": 60.0, "leak": 0, "wrong": 0, "manual": 30},
              {"line": 55.0, "leak": 0, "wrong": 0, "manual": 12},
              {"line": 50.0, "leak": 1, "wrong": 0, "manual": 4}]     # 1 漏放 → 上界超 1%
    best, _ = choose_line_within_budget(counts, n_pos=299, budget=0.01)
    assert best["line"] == 55.0, "预算内挑人工负担最小的（50 那条证明不了 1%）"


def test_budget_is_respected_not_just_observed_leaks() -> None:
    """观测到 0 漏放但样本太少 → 依然不算达标。"""
    counts = [{"line": 60.0, "leak": 0, "wrong": 0, "manual": 5}]
    best, why = choose_line_within_budget(counts, n_pos=20, budget=0.01)
    assert best is None and "13" in why or best is None
