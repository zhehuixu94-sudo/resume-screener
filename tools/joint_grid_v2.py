# -*- coding: utf-8 -*-
"""joint_grid 的「只修测量」版本：c_item 文本源从 resume_parse.evidence 换成**真原文**
（evaluation.cited_evidence 的同 span_id 文本，覆盖 evc_* 那 1847 条），c_item 语义不动。

其余与 tools/joint_grid.py 逐行一致：基础项两套 × G7 λ 扫 × G2-s scale 扫；五判据不变，
另加第六条（真实/半合成不得出现 旧≥70 → 新<70 的漏放）。

用法：python tools/joint_grid_v2.py
产出：out/joint_v2_per_file.csv（逐份 base/ct/n/nq/old_total）、out/joint_v2_cells.csv（50 组全量单元格）
"""
from __future__ import annotations

import collections
import csv as _csv
import json
import math
import os
import statistics as st
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # noqa: E402
from app.contracts import Evaluation                  # noqa: E402
from app.pipeline.honesty import NUM, RESULT          # noqa: E402
from app.store import db                              # noqa: E402

AP_W = {"admin": 30.0, "hr_ops": 25.0, "comm": 20.0, "office2": 15.0, "attitude": 10.0}
BUDGET = 60.0
tier = {m["file"]: m["tier"] for m in
        json.load(open("local/samples_admin/manifest.json", encoding="utf-8"))}
rows = db.fetch_all(
    """SELECT r.file_name, r.resume_id, e.payload FROM evaluation e
         JOIN application a USING(application_id) JOIN resume r ON r.resume_id = e.resume_id
        WHERE a.job_id = 'hr_admin' AND a.config_version = '2'""")
recs = []
n_fixed_items = n_c_of_old = n_c_of_new = 0
for r in rows:
    ev = Evaluation.model_validate(r["payload"])
    pr = db.fetch_one("SELECT payload FROM resume_parse WHERE resume_id = %s", (r["resume_id"],))
    pmap = {}
    for sp in ((pr or {}).get("payload") or {}).get("evidence", []) or []:
        sid = sp.get("span_id") or sp.get("id")
        if sid:
            pmap[sid] = sp.get("text") or sp.get("content") or sp.get("quote") or ""
    # —— 本轮唯一改动：真原文来源 = 评估落库的 cited_evidence（解析证据 + 取证证据的统一池）
    tmap = {c["span_id"]: (c.get("text") or "") for c in (r["payload"].get("cited_evidence") or [])}
    rel, ct_old, ct_new, ni, nq_old, nq_new = {}, {}, {}, 0, 0, 0

    def c_of(t: str) -> float:
        if not t:
            return 0.4
        num, res = bool(NUM.search(t)), bool(RESULT.search(t))
        return 1.0 if (num and res) else (0.7 if (num or res) else 0.4)

    for d in ev.dimensions:
        items = d.items or []
        rel[d.id] = max([i.relevance_score or 0.0 for i in items] or [0.0])
        cs_old = [c_of(pmap.get(i.span_id, "")) for i in items]
        cs_new = [c_of(tmap.get(i.span_id) or pmap.get(i.span_id, "")) for i in items]
        ct_old[d.id] = max(cs_old or [0.4])
        ct_new[d.id] = max(cs_new or [0.4])
        ni += len(items)
        nq_old += sum(1 for c in cs_old if c >= 0.7)
        nq_new += sum(1 for c in cs_new if c >= 0.7)
    n_c_of_old += sum(1 for d in ev.dimensions for i in (d.items or []) if pmap.get(i.span_id, "F") == "F")
    recs.append({"f": r["file_name"], "rid": r["resume_id"], "t": tier.get(r["file_name"]),
                 "rel": rel, "ct_old": ct_old, "ct_new": ct_new, "pen": ev.score_parts.penalty,
                 "n": ni, "nq_old": nq_old, "nq_new": nq_new})


def base_of(x, scheme, key):
    ct = x[key]
    if scheme == "D":
        r2 = {k: 0.25 * v + 0.75 * ct.get(k, 0.4) for k, v in x["rel"].items()}
    else:
        r2 = {k: v * ct.get(k, 0.4) for k, v in x["rel"].items()}
    tot = sum(AP_W.values())
    return round(BUDGET * sum((w / tot) * r2.get(k, 0.0) for k, w in AP_W.items()), 2)


for sch in ("D", "C"):
    for x in recs:
        x["base_" + sch] = base_of(x, sch, "ct_new")      # 修测量后（本轮口径）
        x["base_" + sch + "_old"] = base_of(x, sch, "ct_old")  # 修测量前（上一轮口径）

sf = {r["file"]: r for r in _csv.DictReader(open("out/scoring_v2_rows.csv", encoding="utf-8-sig"))}
for x in recs:
    s = sf[x["f"]]
    x["y"] = float(s["sumQ"]) / math.sqrt(max(1, x["n"]))
    x["old_total"] = float(s["old_total"])
F = [x["f"] for x in recs]
by = {x["f"]: x for x in recs}
REAL = [f for f in F if f.startswith("real_")]


def z(v):
    m, s = st.mean(v), st.pstdev(v)
    return [(i - m) / s if s else 0.0 for i in v]


xx = [math.log(max(1, x["n"])) for x in recs]
yv = [x["y"] for x in recs]
mx, my = st.mean(xx), st.mean(yv)
bcoef = sum((a - c) * (b - my) for a, b, c in zip(xx, yv, [mx] * len(xx))) / sum((a - mx) ** 2 for a in xx)
acoef = my - bcoef * mx
resid_raw = [yv[j] - (acoef + bcoef * xx[j]) for j in range(len(recs))]
expl_raw = [bcoef * (xx[j] - mx) for j in range(len(recs))]
rz, ez = z(resid_raw), z(expl_raw)
for i, x in enumerate(recs):
    x["resid"], x["expl"] = rz[i], ez[i]


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


old_top = set(sorted(F, key=lambda f: -by[f]["old_total"])[:len(F) // 5])
LAMS = (0.0, 0.25, 0.5, 0.75, 1.0)
SCALES = (15, 20, 25, 30, 40)
grid, cells = {}, []
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
            ok5 = (abs(rho) < 0.35 and med[1] - med[2] > 20 and med[4] < med[3]
                   and 65 <= p70 <= 75 and ov > 0.7)
            leak = sorted([(f, by[f]["old_total"], round(tot[f], 1)) for f in REAL
                           if by[f]["old_total"] >= 70 and tot[f] < 70])
            ok6 = not leak
            grid[(sch, lam, s)] = {"rho": rho, "med": med, "ov": ov, "p70": p70, "ok5": ok5,
                                   "leak": leak, "ok6": ok6}
            cells.append({"scheme": sch, "lambda": lam, "scale": s, "rho": round(rho, 3),
                          "med1": round(med[1], 2), "med2": round(med[2], 2), "med3": round(med[3], 2),
                          "med4": round(med[4], 2), "gap12": round(med[1] - med[2], 2),
                          "inv34": round(med[4] - med[3], 2), "p70": p70, "top20_overlap": round(ov, 3),
                          "ok5": ok5, "ok6": ok6, "leak_files": ";".join(f"{a}({b}→{c})" for a, b, c in leak)})

with open("out/joint_v2_per_file.csv", "w", encoding="utf-8-sig", newline="") as f:
    w = _csv.DictWriter(f, fieldnames=["file", "tier", "base_D_old", "base_D", "base_C_old", "base_C",
                                       "pen", "n", "nq_old", "nq_new", "resid", "expl", "old_total"])
    w.writeheader()
    for x in recs:
        w.writerow({"file": x["f"], "tier": x["t"] or "", "base_D_old": x["base_D_old"], "base_D": x["base_D"],
                    "base_C_old": x["base_C_old"], "base_C": x["base_C"], "pen": x["pen"], "n": x["n"],
                    "nq_old": x["nq_old"], "nq_new": x["nq_new"], "resid": round(x["resid"], 4),
                    "expl": round(x["expl"], 4), "old_total": x["old_total"]})
with open("out/joint_v2_cells.csv", "w", encoding="utf-8-sig", newline="") as f:
    w = _csv.DictWriter(f, fieldnames=list(cells[0].keys()))
    w.writeheader()
    w.writerows(cells)

print(f"样本 {len(F)} 份 · 条目 {sum(x['n'] for x in recs)} 条 · "
      f"c_item≥0.7 条目数 修前 {sum(x['nq_old'] for x in recs)} → 修后 {sum(x['nq_new'] for x in recs)}")
for sch, label in (("D", "D γ=0.75（主）"), ("C", "C 门控（对照）")):
    bo = {t: st.median([by[f]["base_" + sch + "_old"] for f in F if by[f]["t"] == t]) for t in (1, 2, 3, 4)}
    bn = {t: st.median([by[f]["base_" + sch] for f in F if by[f]["t"] == t]) for t in (1, 2, 3, 4)}
    print(f"\n【{label}】λ × scale 联合网格（{len(LAMS) * len(SCALES)} 组；基础项单独 明显−边缘 "
          f"修测量前 {bo[1] - bo[2]:.1f} → 修测量后 {bn[1] - bn[2]:.1f}）")
    print(f"{'λ':>5}{'scale':>6}{'ρ':>7}{'明显−边缘':>10}{'注水−不匹配':>12}{'放行':>6}{'重叠':>7}{'第六条':>8}  判据")
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
            verdict = "✅ 五判据全过" if not why else "❌ " + "、".join(why)
            print(f"{lam:>5.2f}{s_:>6}{g['rho']:>7.2f}{g['med'][1] - g['med'][2]:>10.1f}"
                  f"{g['med'][4] - g['med'][3]:>+12.1f}{g['p70']:>6}{g['ov']:>7.2f}"
                  f"{'❌' + str(len(g['leak'])) if g['leak'] else '✅':>8}  {verdict}")
        print()
    hits5 = [k for k in grid if k[0] == sch and grid[k]["ok5"]]
    hits6 = [k for k in grid if k[0] == sch and grid[k]["ok6"]]
    both = [k for k in grid if k[0] == sch and grid[k]["ok5"] and grid[k]["ok6"]]
    print(f"  → {label}：五判据全过 {len(hits5)}/{len(LAMS) * len(SCALES)} 组；第六条全过 {len(hits6)} 组；两条同时过 {len(both)} 组")

print("\n=== real_* 在新网格下的 旧≥70 → 新<70 命中行 ===")
print(f"{'文件':<18}{'旧判定分':>9}" + "".join(f"{'D s=%d' % s:>9}" for s in SCALES) +
      "".join(f"{'C s=%d' % s:>9}" for s in SCALES) + f"{'命中组数 D/C':>14}")
for f in sorted(REAL, key=lambda k: -by[k]["old_total"]):
    row = f"{f:<18}{by[f]['old_total']:>9.1f}"
    cnt = {"D": 0, "C": 0}
    for sch in ("D", "C"):
        for s_ in SCALES:
            g = grid[(sch, 0.25, s_)]           # λ=0.25 与判定候选一致
            tot = by[f]["base_" + sch] + s_ * prank({k: by[k]["resid"] + 0.25 * by[k]["expl"] for k in F})[f] - by[f]["pen"]
            row += f"{tot:>9.1f}"
            if by[f]["old_total"] >= 70 and tot < 70:
                cnt[sch] += 1
    print(row + f"{cnt['D']:>7} /{cnt['C']:>2}")
print("\n（λ=0.25 时逐组数值；全部 50 组中 旧≥70→新<70 的完整命中行见下）")
for sch in ("D", "C"):
    seen = collections.Counter()
    for lam in LAMS:
        for s_ in SCALES:
            for f, o, n in grid[(sch, lam, s_)]["leak"]:
                seen[f] += 1
    print(f"  {sch}：命中文件 " + (", ".join(f"{f} {c}/25 组" for f, c in seen.most_common()) or "无"))
print("\nCSV：out/joint_v2_per_file.csv · out/joint_v2_cells.csv")
