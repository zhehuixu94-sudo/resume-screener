# -*- coding: utf-8 -*-
"""基础项补区分度：把 C（量化成果密度，纯代码算）接进基础项，扫维度权重（离线，只读）。

现状：基础项 = 60 × Σ_维度 [(w/Σw) × 该维度最强证据的相关度]，5 维权重 30/25/20/15/10。
问题是 `max(relevance)` 对谁都是 ~0.9（模型打分 ≈ 条目计数器），于是基础项对谁都是 ~54，
**明显合格 − 边缘合格 只有 6.2 分** → 区分度全靠 40 分加分项撑着，导致"放行数"与"区分度"互斥。

本脚本扫三种把 C 接进基础项的方式（全部纯代码，0 token）：
  A 新增第 6 维「量化成果」，权重 wc 扫 10~50（其余 5 维等比缩放）
  B 乘法门控：base' = 60 × Σ (w/Σw) × max(rel) × (1 − β + β × C_row)
  C 条目级门控：每维取 max(rel × c_item)，c_item 由该条目文本是否含数字/成果动词决定
判据：基础项单独的 明显−边缘 > 15（前置条件，不占五判据名额）。
"""
from __future__ import annotations

import collections
import csv
import json
import os
import re
import statistics as st
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # noqa: E402
from app.contracts import Evaluation, JobConfig      # noqa: E402
from app.pipeline.honesty import NUM, RESULT         # noqa: E402
from app.store import db                            # noqa: E402

CN = {1: "明显合格", 2: "边缘合格", 3: "明显不匹配", 4: "注水型"}
BUDGET = 60.0
ap_w = {"admin": 30.0, "hr_ops": 25.0, "comm": 20.0, "office2": 15.0, "attitude": 10.0}
txt_keys = ("evidence_span", "span", "text", "quote", "evidence", "evidence_text")

tier = {m["file"]: m["tier"] for m in
        json.load(open("local/samples_admin/manifest.json", encoding="utf-8"))}
rows = db.fetch_all(
    """SELECT r.file_name, r.resume_id, e.payload FROM evaluation e JOIN application a USING(application_id)
         JOIN resume r ON r.resume_id = e.resume_id
        WHERE a.job_id = 'hr_admin' AND a.config_version = '2'""")

recs, probe_done = [], False
for r in rows:
    ev = Evaluation.model_validate(r["payload"])
    dims = [d.model_dump(mode="json") for d in ev.dimensions]
    # 条目只有 span_id → 从解析件取原文，才能按条算 C（上一版没取到 → C 恒 0，扫描无效）
    pmap = {}
    pr = db.fetch_one("SELECT payload FROM resume_parse WHERE resume_id = %s", (r["resume_id"],))
    for sp in ((pr or {}).get("payload") or {}).get("evidence", []) or []:
        sid = sp.get("span_id") or sp.get("id") or sp.get("evidence_id")
        txt = sp.get("text") or sp.get("content") or sp.get("quote") or ""
        if sid:
            pmap[sid] = txt
    if not probe_done:
        print(f"[探针] 解析件 span 数 {len(pmap)} · 样例 id={list(pmap)[:3]}")
    if not probe_done:
        it = (dims[0].get("items") or [{}])[0] if dims else {}
        print(f"[探针] 条目字段：{sorted(it.keys())}")
        probe_done = True
    C_row, n_items = 0.0, 0
    for d in dims:
        for i in (d.get("items") or []):
            n_items += 1
            sp = pmap.get(i.get("span_id"), "") or next((str(i[k]) for k in txt_keys if i.get(k)), "")
            if sp and NUM.search(sp) and RESULT.search(sp):
                C_row += 1

    C_row = C_row / n_items if n_items else 0.0
    rel = {d.get("id"): max([float(i.get("relevance_score") or 0) for i in (d.get("items") or [])] or [0.0])
           for d in dims}
    def c_item(i) -> float:
        sp = pmap.get(i.get("span_id"), "") or next((str(i[k]) for k in txt_keys if i.get(k)), "")
        if not sp:
            return 0.4
        num, res = bool(NUM.search(sp)), bool(RESULT.search(sp))
        return 1.0 if (num and res) else (0.7 if (num or res) else 0.4)

    ct = {d.get("id"): max([c_item(i) for i in (d.get("items") or [])] or [0.4]) for d in dims}
    recs.append({"f": r["file_name"], "t": tier.get(r["file_name"]), "rel": rel, "ct": ct,
                 "C": C_row, "base_now": ev.score_parts.base})


def gap(vals_by_tier: dict) -> float:
    return st.median(vals_by_tier[1]) - st.median(vals_by_tier[2])


def base_plain(rel, weights, extra_id=None, extra_val=0.0):
    tot = sum(weights.values()) or 1.0
    got = sum(BUDGET * (w / tot) * rel.get(k, 0.0) for k, w in weights.items() if k != extra_id)
    if extra_id:
        got += BUDGET * (weights.get(extra_id, 0.0) / tot) * extra_val
    return got


print(f"\n基准（现基础项）：明显合格 {st.median([x['base_now'] for x in recs if x['t']==1]):.1f}"
      f" − 边缘合格 {st.median([x['base_now'] for x in recs if x['t']==2]):.1f}"
      f" = {gap({t: [x['base_now'] for x in recs if x['t']==t] for t in (1,2)}):.1f}\n")

print("【表】基础项权重 → 明显−边缘（判据：>15）")
print(f"{'方案':<34}{'四档中位(基础项)':>30}{'明显−边缘':>10}  判据")
out_rows = []


def report(name, val_fn, extra=None):
    by = collections.defaultdict(list)
    for x in recs:
        by[x["t"]].append(val_fn(x))
    med = {t: st.median(by[t]) for t in (1, 2, 3, 4) if by.get(t)}
    g = med[1] - med[2]
    ok = "✅ 过" if g > 15 else "❌ 未过"
    print(f"{name:<34}{f'{med[1]:.1f} / {med[2]:.1f} / {med[3]:.1f} / {med[4]:.1f}':>30}{g:>10.1f}  {ok}")
    out_rows.append({"scheme": name, "med": med, "gap": g, "ok": g > 15})


for wc in (10, 20, 30, 40, 50):
    report(f"A 第6维C · wc={wc}",
           lambda x, wc=wc: base_plain(x["rel"], dict(ap_w, quant=wc), "quant", x["C"]))
for beta in (0.3, 0.5, 0.7, 1.0):
    report(f"B 乘法门控 · β={beta}",
           lambda x, beta=beta: base_plain({k: v * (1 - beta + beta * x["C"]) for k, v in x["rel"].items()}, ap_w))
for _ in (0,):
    report("C 条目级门控(rel×c_item)", lambda x: base_plain(
        {k: v * ct for (k, v), ct in zip(x["rel"].items(), [x["ct"].get(k, 0.4) for k in x["rel"]])}, ap_w))

for gamma in (0.25, 0.5, 0.75):
    report(f"D 相关度混入代码q · γ={gamma}", lambda x, gamma=gamma: base_plain(
        {k: (1 - gamma) * v + gamma * x["ct"].get(k, 0.4) for k, v in x["rel"].items()}, ap_w))
print("诊断：纯代码单条质量 q 的档位中位（看代码特征本身能把两档拉开多少）")
print("      " + " · ".join(
    f"{CN[t]} {st.median([st.mean(list(x['ct'].values())) for x in recs if x['t']==t]):.3f}"
    for t in (1, 2, 3, 4)))
print("      " + " · ".join(
    f"{CN[t]} 含数字+成果条目占比 {st.median([x['C'] for x in recs if x['t']==t]):.3f}"
    for t in (1, 2, 3, 4)))
best = max(out_rows, key=lambda r: r["gap"])
print(f"\n最优方案：{best['scheme']} · 明显−边缘 {best['gap']:.1f}"
      f"{'（过 >15，可进步骤 3）' if best['gap'] > 15 else '（仍未过 >15 → 按纪律继续调基础项，不动加分项、不动生产）'}")
with open("out/base_gap_scan.csv", "w", encoding="utf-8-sig", newline="") as f:
    w = csv.DictWriter(f, fieldnames=["scheme", "gap", "ok"]); w.writeheader()
    for r in out_rows:
        w.writerow({"scheme": r["scheme"], "gap": round(r["gap"], 2), "ok": r["ok"]})
print("CSV：out/base_gap_scan.csv")
