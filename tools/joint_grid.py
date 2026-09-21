# -*- coding: utf-8 -*-
"""步骤 3：两套基础项（D γ=0.75 主 / C 门控 对照）× G7 λ 扫 × G2-s scale 扫 = 50 组联合网格。

基础项两套（纯代码，离线）：
  D(γ=0.75)：每维相关度 rel' = 0.25·rel_model + 0.75·c_item     → 基础项单独 明显−边缘 17.5
  C 门控   ：每维取 max(rel_model × c_item)                      → 基础项单独 明显−边缘 22.7（含塌陷）

加分项：G7 残差法 combined(λ) = z(residual) + λ·z(explained)，λ=0 纯残差 → λ=1 原信号；
判定分 = 基础项 + scale × 分位(combined) − 扣分。scale=40 是现刻度，缩小可压放行人数（表二：25 → 放行 74）。

五判据：ρ<0.35 / 明显−边缘>20 / 注水<不匹配 / 线70放行 65~75 / Top20%重叠>0.7。
用法：python tools/joint_grid.py
"""
from __future__ import annotations

import collections
import json
import math
import os
import statistics as st
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # noqa: E402
from app.contracts import Evaluation                  # noqa: E402
from app.pipeline.honesty import NUM, RESULT          # noqa: E402
from app.store import db                              # noqa: E402
from tools.evidence_source import c_of, span_text_map  # noqa: E402

AP_W = {"admin": 30.0, "hr_ops": 25.0, "comm": 20.0, "office2": 15.0, "attitude": 10.0}
BUDGET = 60.0
tier = {m["file"]: m["tier"] for m in
        json.load(open("local/samples_admin/manifest.json", encoding="utf-8"))}
rows = db.fetch_all(
    """SELECT r.file_name, r.resume_id, e.payload FROM evaluation e
         JOIN application a USING(application_id) JOIN resume r ON r.resume_id = e.resume_id
        WHERE a.job_id = 'hr_admin' AND a.config_version = '2'""")
recs = []
for r in rows:
    ev = Evaluation.model_validate(r["payload"])
    # 文本源：cited_evidence 为主、resume_parse.evidence 补齐（两者重叠部分实测逐字一致）。
    # 原实现只用 resume_parse，导致 40.8% 的 evc_* span 取不到文本被兜底成 0.4（测量 bug）。
    pr = db.fetch_one("SELECT payload FROM resume_parse WHERE resume_id = %s", (r["resume_id"],))
    tmap = span_text_map(r["payload"], (pr or {}).get("payload"))
    rel, ct, ni, nq = {}, {}, 0, 0

    for d in ev.dimensions:
        items = d.items or []
        rel[d.id] = max([i.relevance_score or 0.0 for i in items] or [0.0])
        cs = [c_of(tmap.get(i.span_id, "")) for i in items]
        ct[d.id] = max(cs or [0.4])
        ni += len(items)
        nq += sum(1 for c in cs if c >= 0.7)
    recs.append({"f": r["file_name"], "t": tier.get(r["file_name"]), "rel": rel, "ct": ct,
                 "pen": ev.score_parts.penalty, "n": ni, "nq": nq,
                 # 真实/半合成件（real_* 与 real_*_degN）——第六条判据只认这些
                 "real": r["file_name"].startswith("real_")})


def base_of(x, scheme):
    if scheme == "D":
        r2 = {k: 0.25 * v + 0.75 * x["ct"].get(k, 0.4) for k, v in x["rel"].items()}
    else:
        r2 = {k: v * x["ct"].get(k, 0.4) for k, v in x["rel"].items()}
    tot = sum(AP_W.values())
    return round(BUDGET * sum((w / tot) * r2.get(k, 0.0) for k, w in AP_W.items()), 2)


for sch in ("D", "C"):
    for x in recs:
        x["base_" + sch] = base_of(x, sch)

# —— 分支加分依据：y = Σq/√n（来自 offline_scoring_v2 的密度特征），回归 ln n 取残差
import csv as _csv
sf = {r["file"]: r for r in _csv.DictReader(open("out/scoring_v2_rows.csv", encoding="utf-8-sig"))}
for x in recs:
    s = sf[x["f"]]
    x["y"] = float(s["sumQ"]) / math.sqrt(max(1, x["n"]))
    x["old_total"] = float(s["old_total"])
F = [x["f"] for x in recs]


def z(v):
    m, s = st.mean(v), st.pstdev(v)
    return [(i - m) / s if s else 0.0 for i in v]


xx = [math.log(max(1, x["n"])) for x in recs]
yv = [x["y"] for x in recs]
mx, my = st.mean(xx), st.mean(yv)
bcoef = sum((a - mx) * (c - my) for a, c in zip(xx, yv)) / sum((a - mx) ** 2 for a in xx)
acoef = my - bcoef * mx
for i, x in enumerate(recs):
    x["resid"] = z([yv[j] - (acoef + bcoef * xx[j]) for j in range(len(recs))])[i]
    x["expl"] = z([bcoef * (xx[j] - mx) for j in range(len(recs))])[i]


def prank(raw):
    order = sorted(raw, key=lambda k: raw[k])
    out, i, n = {}, 0, len(order)
    while i < n:
        j = i
        while j + 1 < n and raw[order[j + 1]] == raw[order[i]]:
            j += 1
        for k in order[i:j + 1]:
            out[k] = (i + j) / 2 / n
        i = j + 1
    return out


def corr(a, b):
    if st.pstdev(a) == 0 or st.pstdev(b) == 0:
        return 0.0
    ma, mb = st.mean(a), st.mean(b)
    return sum((p - ma) * (q - mb) for p, q in zip(a, b)) / len(a) / (st.pstdev(a) * st.pstdev(b))


old_top = set(sorted(F, key=lambda f: -[x for x in recs if x["f"] == f][0]["old_total"])[:len(F) // 5])
by = {x["f"]: x for x in recs}
LAMS = (0.0, 0.25, 0.5, 0.75, 1.0)
SCALES = (15, 20, 25, 30, 40)
grid = {}
for sch in ("D", "C"):
    for lam in LAMS:
        comb = {f: by[f]["resid"] + lam * by[f]["expl"] for f in F}
        rk = prank(comb)
        for s in SCALES:
            tot = {f: by[f]["base_" + sch] + s * rk[f] - by[f]["pen"] for f in F}
            med = {t: st.median([tot[f] for f in F if by[f]["t"] == t]) for t in (1, 2, 3, 4)}
            rho = corr([s * rk[f] for f in F], [by[f]["n"] for f in F])
            top = set(sorted(F, key=lambda f: -tot[f])[:len(F) // 5])
            ov = len(old_top & top) / len(old_top)
            p70 = sum(1 for f in F if tot[f] >= 70)
            # 第六条（2026-09-15 暴露的判据漏洞）：五判据只在 310 份语料上算，其中 300 份是合成件，
            # 10 份真实/半合成权重太小 → 必须单独判「真实件不得出现旧≥70 → 新<70 的漏放」。
            leak6 = [f for f in F if by[f]["real"] and by[f]["old_total"] >= 70 and tot[f] < 70]
            ok = (abs(rho) < 0.35 and med[1] - med[2] > 20 and med[4] < med[3]
                  and 65 <= p70 <= 75 and ov > 0.7 and not leak6)
            bmed = {t: st.median([by[f]["base_" + sch] for f in F if by[f]["t"] == t]) for t in (1, 2, 3, 4)}
            grid[(sch, lam, s)] = {"rho": rho, "med": med, "ov": ov, "p70": p70, "ok": ok,
                                   "bdiff": bmed[1] - bmed[2], "inv": med[4] - med[3], "leak6": leak6}

import csv as _c2
with open("out/joint_per_file.csv", "w", encoding="utf-8-sig", newline="") as _f:
    _w = _c2.DictWriter(_f, fieldnames=["file", "tier", "base_D", "base_C", "pen", "n", "nq",
                                        "resid", "expl", "old_total"])
    _w.writeheader()
    for _x in recs:
        _w.writerow({"file": _x["f"], "tier": _x["t"] or "", "base_D": _x["base_D"],
                     "base_C": _x["base_C"], "pen": _x["pen"], "n": _x["n"], "nq": _x["nq"],
                     "resid": round(_x["resid"], 4), "expl": round(_x["expl"], 4),
                     "old_total": _x["old_total"]})

for sch, label in (("D", "D γ=0.75（主）"), ("C", "C 门控（对照）")):
    print(f"\n【{label}】λ × scale 联合网格（50 组；基础项单独 明显−边缘 "
          f"{grid[(sch, 0, 40)]['bdiff']:.1f}）")
    print(f"{'λ':>5}{'scale':>6}{'ρ':>7}{'明显−边缘':>10}{'注水−不匹配':>12}{'放行':>6}{'重叠':>7}{'真实漏放':>9}  判据")
    for lam in LAMS:
        for s_ in SCALES:
            g = grid[(sch, lam, s_)]
            why = []
            if abs(g["rho"]) >= 0.35:
                why.append("ρ")
            if g["med"][1] - g["med"][2] <= 20:
                why.append("明显−边缘")
            if not g["med"][4] < g["med"][3]:
                why.append("注水<不匹配")
            if not 65 <= g["p70"] <= 75:
                why.append("放行")
            if g["ov"] <= 0.7:
                why.append("重叠")
            if g["leak6"]:
                why.append(f"第六条({len(g['leak6'])}件)")
            verdict = "✅ 六判据全过" if not why else "❌ " + "、".join(why)
            print(f"{lam:>5.2f}{s_:>6}{g['rho']:>7.2f}{g['med'][1] - g['med'][2]:>10.1f}"
                  f"{g['inv']:>+12.1f}{g['p70']:>6}{g['ov']:>7.2f}{len(g['leak6']):>9}  {verdict}")
        print()
    hits = [k for k in grid if k[0] == sch and grid[k]["ok"]]
    print(f"  → {label} 解：{len(hits)} 组" + (f" {hits}" if hits else "（无）"))

# —— 第六条判据明细：真实/半合成件在三个定稿候选参数下的 old → new
print("\n【第六条判据 · 真实/半合成件逐件】旧口径 ≥70 而新口径 <70 = 漏放（必须 0 件）")
REAL = sorted(f for f in F if by[f]["real"])
print(f"  真实/半合成件 {len(REAL)} 份 / 全批 {len(F)} 份")
for sch, lam, s_, tag in (("D", 0.25, 30, "D λ=.25 s=30（推荐）"),
                          ("D", 0.00, 30, "D λ=0  s=30（备选）"),
                          ("C", 0.25, 40, "C λ=.25 s=40（对照）")):
    comb = {f: by[f]["resid"] + lam * by[f]["expl"] for f in F}
    rk = prank(comb)
    leaks = []
    print(f"\n  {tag}")
    for f in REAL:
        new = by[f]["base_" + sch] + s_ * rk[f] - by[f]["pen"]
        bad = by[f]["old_total"] >= 70 and new < 70
        leaks.append(f) if bad else None
        print(f"    {f:<20} 旧 {by[f]['old_total']:>6.2f} → 新 {new:>6.2f}"
              f"   {'❌ 漏放' if bad else '✅'}")
    print(f"    → 漏放 {len(leaks)}/{len(REAL)} 件" + (f"：{leaks}" if leaks else "（过第六条）"))
