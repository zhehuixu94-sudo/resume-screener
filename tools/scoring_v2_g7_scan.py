# -*- coding: utf-8 -*-
"""G7 残差法 λ 权衡曲线 + G2-s 加分刻度标定（离线，读 scoring_v2_rows.csv，不碰生产）。

G7 的思路（用户口径）：ρ=0.73 里混着真信号（长简历确实含更多量化成果）。分桶/纯残差把真信号
一起抹了（明显−边缘掉到 ~15、Top20% 换 40% 的人）。改成：把加分依据拆成
    y = Σq/√n  ，对 x = ln(n) 回归
    residual = y − (a + b·x)        ← 与条数无关的部分
    explained = b·(x − x̄)           ← 由条数解释的部分（其中含真信号）
    combined(λ) = z(residual) + λ · z(explained)
λ=0 → 结构去相关（ρ≈0，区分度低）；λ=1 → 原信号（ρ≈0.74，区分度高）。扫 λ 找第一个
满足 ρ<0.35、明显−边缘>20、Top20%重叠>0.7 的点。

G2-s：分位制天然让 ~25~30% 的人拿高加分，额度 40 会把放行人数顶到 90+；
扫 scale ∈ {40,30,25,20,15}，看线 70 放行能否落回 65~75。
"""
from __future__ import annotations

import csv
import math
import statistics as st

rows = list(csv.DictReader(open("out/scoring_v2_rows.csv", encoding="utf-8-sig")))
for r in rows:
    for k in ("n_spans",):
        r[k] = int(float(r[k]))
    for k in ("base", "penalty", "sumQ", "q", "old_total", "old_bonus"):
        r[k] = float(r[k])
    r["tier"] = int(float(r["tier"])) if r["tier"] else None
    n = max(1, r["n_spans"])
    r["y"] = r["sumQ"] / math.sqrt(n)


def z(vals: list[float]) -> list[float]:
    m, s = st.mean(vals), st.pstdev(vals)
    return [(v - m) / s if s else 0.0 for v in vals]


def prank(raw: dict) -> dict:
    """分位（并列取平均位次），与生产 percentile_rank 同口径。"""
    order = sorted(raw, key=lambda k: raw[k])
    out, i, n = {}, 0, len(order)
    while i < n:
        j = i
        while j + 1 < n and raw[order[j + 1]] == raw[order[i]]:
            j += 1
        avg = (i + j) / 2 / n
        for k in order[i:j + 1]:
            out[k] = avg
        i = j + 1
    return out


def corr(a: list[float], b: list[float]) -> float:
    if st.pstdev(a) == 0 or st.pstdev(b) == 0:
        return 0.0
    ma, mb = st.mean(a), st.mean(b)
    return sum((x - ma) * (y - mb) for x, y in zip(a, b)) / len(a) / (st.pstdev(a) * st.pstdev(b))


files = [r["file"] for r in rows]
n = {r["file"]: r["n_spans"] for r in rows}
y = {r["file"]: r["y"] for r in rows}
base = {r["file"]: r["base"] for r in rows}
pen = {r["file"]: r["penalty"] for r in rows}
tier = {r["file"]: r["tier"] for r in rows}
old_total = {r["file"]: r["old_total"] for r in rows}
old_top = set(sorted(files, key=lambda f: -old_total[f])[:max(1, len(files) // 5)])

# —— 回归 y ~ ln(n)
x = [math.log(n[f]) for f in files]
yv = [y[f] for f in files]
mx, my = st.mean(x), st.mean(yv)
b = sum((xi - mx) * (yi - my) for xi, yi in zip(x, yv)) / sum((xi - mx) ** 2 for xi in x)
a = my - b * mx
resid = z([yv[i] - (a + b * x[i]) for i in range(len(files))])
expl = z([b * (x[i] - mx) for i in range(len(files))])
r_map = dict(zip(files, resid))
e_map = dict(zip(files, expl))
print(f"回归：y = {a:.3f} + {b:.3f}·ln(n)   （b>0 = 条数越多、每条质量越高 → ρ 里的真信号）\n")

print("【表一】G7 残差法 λ 权衡曲线（λ=0 纯残差 → λ=1 原信号）")
print(f"{'λ':>5}{'ρ(条数)':>9}{'四档中位':>28}{'明显−边缘':>10}{'注水<不匹配':>12}{'线70放行':>9}{'Top20%重叠':>11}  判据")
best = None
for lam in (0.0, 0.25, 0.5, 0.75, 1.0):
    comb = {f: r_map[f] + lam * e_map[f] for f in files}
    rk = prank(comb)
    bonus = {f: 40 * rk[f] for f in files}
    total = {f: base[f] + bonus[f] - pen[f] for f in files}
    med = {t: st.median([total[f] for f in files if tier[f] == t]) for t in (1, 2, 3, 4)}
    rho = corr([bonus[f] for f in files], [n[f] for f in files])
    top = set(sorted(files, key=lambda f: -total[f])[:max(1, len(files) // 5)])
    ov = len(old_top & top) / len(old_top)
    passed = sum(1 for f in files if total[f] >= 70)
    ok = (abs(rho) < 0.35) and (med[1] - med[2] > 20) and (med[4] < med[3]) and (65 <= passed <= 75) and (ov > 0.7)
    flag = "✅ 全过" if ok else "❌ " + "、".join(
        [t for t, v in (("ρ", abs(rho) < 0.35), ("明显−边缘", med[1] - med[2] > 20),
                        ("注水<不匹配", med[4] < med[3]), ("放行65~75", 65 <= passed <= 75),
                        ("重叠>0.7", ov > 0.7)) if not v])
    print(f"{lam:>5.2f}{rho:>9.2f}"
          f"{f'{med[1]:.1f} / {med[2]:.1f} / {med[3]:.1f} / {med[4]:.1f}':>28}"
          f"{med[1]-med[2]:>10.1f}{str(med[4]<med[3]):>12}{passed:>9}{ov:>11.2f}  {flag}")
    if ok and best is None:
        best = lam

print("\n【表二】G2-s 加分刻度标定（在 G2=Σq/√n 上扫 scale；线 70）")
print(f"{'scale':>7}{'放行(线70)':>11}{'明显合格达标':>13}{'边缘合格误放':>13}{'四档中位':>28}")
g2 = {f: y[f] for f in files}
rk2 = prank(g2)
for s in (40, 30, 25, 20, 15):
    total = {f: base[f] + s * rk2[f] - pen[f] for f in files}
    med = {t: st.median([total[f] for f in files if tier[f] == t]) for t in (1, 2, 3, 4)}
    p70 = sum(1 for f in files if total[f] >= 70)
    good = sum(1 for f in files if tier[f] == 1 and total[f] >= 70)
    bad = sum(1 for f in files if tier[f] == 2 and total[f] >= 70)
    mark = "  ← 落在 65~75" if 65 <= p70 <= 75 else ""
    print(f"{s:>7}{p70:>11}{f'{good}/75':>13}{f'{bad}/75':>13}"
          f"{f'{med[1]:.1f} / {med[2]:.1f} / {med[3]:.1f} / {med[4]:.1f}':>28}{mark}")

print(f"\n选点：G7 的第一个全过 λ = {best if best is not None else '（没有 λ 能同时满足五条）'}")
