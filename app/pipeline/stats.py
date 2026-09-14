# -*- coding: utf-8 -*-
"""小样本统计：漏放率的置信上界，以及"在漏放预算内选线"。

为什么需要它：HR 说"漏放率控制在 1% 以内"，这句话在几十份样本上是**无法证明**的——
0 漏放 / n 份样本的 95% 单侧上界 ≈ 3/n：20 份 → 14%、100 份 → 2.95%、299 份才到 1%。
所以校准不能只看"样本里有没有漏放"，必须看"这批样本能证明到什么程度"，
否则等于用 5 份样本的"零漏放"去宣称 1% 的安全水平。
"""
from __future__ import annotations

import math


def binom_cdf(k: int, n: int, p: float) -> float:
    """P(X ≤ k)，X ~ B(n, p)。n 只有几百，直接求和足够快。"""
    if p <= 0:
        return 1.0
    if p >= 1:
        return 0.0
    return sum(math.comb(n, i) * p ** i * (1 - p) ** (n - i) for i in range(0, min(k, n) + 1))


def leak_upper_bound(leaks: int, n: int, confidence: float = 0.95) -> float:
    """漏放率的单侧置信上界（Clopper-Pearson，二分求解）。"""
    if n <= 0:
        return 1.0
    lo, hi = 0.0, 1.0
    for _ in range(60):
        mid = (lo + hi) / 2
        if binom_cdf(leaks, n, mid) > 1 - confidence:
            lo = mid
        else:
            hi = mid
    return hi


def samples_needed(budget: float, confidence: float = 0.95, max_n: int = 5000) -> int:
    """零漏放时，要证明"漏放率 ≤ budget"至少需要多少份标注样本。"""
    for n in range(1, max_n + 1):
        if leak_upper_bound(0, n, confidence) <= budget:
            return n
    return max_n


def choose_line_within_budget(counts: list[dict], n_pos: int, *, budget: float = 0.01,
                              confidence: float = 0.95) -> tuple[dict | None, str]:
    """在"漏放的置信上界 ≤ budget"的候选线里，挑人工负担最小（其次误放最少、线更高）的那条。

    counts 每项：{"line", "leak", "wrong", "manual"}；n_pos = 正样本（该留）总数。
    返回 (选中项或 None, 人话说明)。
    """
    feasible = [c for c in counts if leak_upper_bound(c["leak"], n_pos, confidence) <= budget]
    if not feasible:
        need = samples_needed(budget, confidence)
        best_leak = min((c["leak"] for c in counts), default=0)
        bound = leak_upper_bound(best_leak, n_pos, confidence) if n_pos else 1.0
        return None, (f"没有任何候选线能证明「漏放 ≤{budget:.1%}」：现有 {n_pos} 份「该留」样本，"
                      f"即便 0 漏放，置信上界也有 {bound:.1%}。要证明该目标需要 ≥{need} 份标注样本"
                      f"（或把目标放宽到 {leak_upper_bound(0, n_pos, confidence):.1%}）")
    return (min(feasible, key=lambda c: (c["wrong"], c["manual"], -c["line"])),
            f"{len(feasible)}/{len(counts)} 条候选线满足漏放预算 {budget:.1%}（{n_pos} 份样本）")
