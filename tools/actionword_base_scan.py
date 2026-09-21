# -*- coding: utf-8 -*-
"""动作词权重进基础项：量「基础项单独 明显−边缘」能不能 >15（不进网格、不动 λ/scale）。

两个口径（都基于已修测量的真原文 + 新语义 A 的 c_item）：
  口径 1 · 独立第 6 维：基础项 = 60 × [ Σ_5维 (w_d/(W+w6))·rel_d·c_d  +  (w6/(W+w6))·ACT ]
      ACT = 整份简历的条目级动作词得分均值（0~1）
  口径 2 · 作为某维加权项：每维 r_d = (1−γ)·(rel_d·c_d) + γ·act_d     （act_d = 维度内条目均值）

动作词得分（每条证据）：distinct 命中词按权重求和 → 密度归一 min(1, Σw·100/字数)，避免"命中 2 个就饱和"。
权重：强=1.0（可核实产出）· 中=0.6（执行动作）· 弱=0.3（无宾语也常见）。

用法：python tools/actionword_base_scan.py
"""
from __future__ import annotations

import json
import os
import re
import statistics as st
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # noqa: E402
from app.contracts import Evaluation                          # noqa: E402
from app.pipeline.honesty import FLUFF_DEFAULT, NUM, RESULT    # noqa: E402
from app.store import db                                      # noqa: E402

AP_W = {"admin": 30.0, "hr_ops": 25.0, "comm": 20.0, "office2": 15.0, "attitude": 10.0}
BUDGET = 60.0
CN = {1: "明显合格", 2: "边缘合格", 3: "明显不匹配", 4: "注水型"}

# ---------------- 动作词权重表（distinct 命中后按权重累加） ----------------
STRONG = ["完成", "落地", "上线", "交付", "发布", "交付上线", "修复", "部署", "迁移", "重构", "搭建", "开发",
          "编制", "制定", "设计", "组建", "推动", "标准化", "自动化", "优化", "提升方案", "闭环"]
MID = ["负责", "参与", "组织", "策划", "执行", "协调", "录入", "归档", "统计", "整理", "维护", "巡检", "对接",
       "汇报", "核对", "跟踪", "处理", "安排", "发放", "报销", "接待", "培训", "支持", "监控", "分析",
       "采购", "询价", "验收", "填写", "留存", "巡查", "交底", "审核", "测试", "建模", "排班", "盘点",
       "结算", "复核", "汇总", "录入台账", "抽查", "走访", "讲解", "带教"]
WEAK = ["协助", "配合", "参加", "学习", "熟悉", "熟练", "使用", "帮忙", "服从", "了解", "掌握"]
WORD_W = {**{w: 1.0 for w in STRONG}, **{w: 0.6 for w in MID}, **{w: 0.3 for w in WEAK}}
ACT_RE = re.compile("|".join(sorted(WORD_W, key=len, reverse=True)))

tier = {m["file"]: m["tier"] for m in
        json.load(open("local/samples_admin/manifest.json", encoding="utf-8"))}


def concrete(t: str) -> bool:
    return bool(NUM.search(t) or RESULT.search(t) or ACT_RE.search(t))


def c_A(t: str) -> float:                       # 上一轮定的新语义（只惩罚空话）
    if not t:
        return 0.4
    return 0.4 if (FLUFF_DEFAULT.search(t) and not concrete(t)) else 1.0


def act_score(t: str) -> float:
    if not t:
        return 0.0
    hits = {m.group(0) for m in ACT_RE.finditer(t)}
    if not hits:
        return 0.0
    return min(1.0, sum(WORD_W[h] for h in hits) * 100.0 / max(1, len(t.strip())))


RECS = []
for r in db.fetch_all(
        """SELECT r.file_name, r.resume_id, e.payload FROM evaluation e
             JOIN application a USING(application_id) JOIN resume r ON r.resume_id = e.resume_id
            WHERE a.job_id = 'hr_admin' AND a.config_version = '2'"""):
    ev = Evaluation.model_validate(r["payload"])
    tmap = {c["span_id"]: (c.get("text") or "") for c in (r["payload"].get("cited_evidence") or [])}
    pr = db.fetch_one("SELECT payload FROM resume_parse WHERE resume_id = %s", (r["resume_id"],))
    pmap = {s["span_id"]: (s.get("text") or "") for s in ((pr or {}).get("payload") or {}).get("evidence", []) or []}
    dims, all_items = [], []
    for d in ev.dimensions:
        items = []
        for i in d.items or []:
            t = tmap.get(i.span_id) or pmap.get(i.span_id, "")
            it = {"rel": i.relevance_score or 0.0, "c": c_A(t), "a": act_score(t), "t": t}
            items.append(it)
            all_items.append(it)
        dims.append({"id": d.id, "items": items})
    RECS.append({"f": r["file_name"], "t": tier.get(r["file_name"]), "dims": dims, "items": all_items})


def base_6th(x, w6: float) -> float:
    """口径 1：ACT 作为独立第 6 维（整份简历条目级动作分均值）。"""
    tot = sum(AP_W.values()) + w6
    s = 0.0
    for d in x["dims"]:
        rel = max([i["rel"] for i in d["items"]] or [0.0])
        c = max([i["c"] for i in d["items"]] or [0.4])
        s += (AP_W.get(d["id"], 0.0) / tot) * rel * c
    act = st.mean([i["a"] for i in x["items"]]) if x["items"] else 0.0
    s += (w6 / tot) * act
    return round(BUDGET * s, 2)


def base_weighted(x, gamma: float) -> float:
    """口径 2：act 作为各维的加权项。"""
    tot = sum(AP_W.values())
    s = 0.0
    for d in x["dims"]:
        rel = max([i["rel"] for i in d["items"]] or [0.0])
        c = max([i["c"] for i in d["items"]] or [0.4])
        act = st.mean([i["a"] for i in d["items"]]) if d["items"] else 0.0
        s += (AP_W.get(d["id"], 0.0) / tot) * ((1 - gamma) * rel * c + gamma * act)
    return round(BUDGET * s, 2)


def med_gap(fn) -> tuple[dict, float, float]:
    med = {t: st.median([fn(x) for x in RECS if x["t"] == t]) for t in (1, 2, 3, 4)}
    return med, med[1] - med[2], med[4] - med[3]


print("=== 动作词在四档上的分布（条目级，真原文）===")
print(f"{'档位':<10}{'条目数':>7}{'有动作词比例':>12}{'动作分均值':>11}{'动作分中位':>11}{'强词比例':>9}")
for t in (1, 2, 3, 4):
    items = [i for x in RECS if x["t"] == t for i in x["items"]]
    strong = [i for i in items if any(w in i["t"] for w in STRONG)]
    print(f"{CN[t]:<10}{len(items):>7}{100 * sum(1 for i in items if i['a'] > 0) / max(1, len(items)):>11.1f}%"
          f"{st.mean([i['a'] for i in items]):>11.3f}{st.median([i['a'] for i in items]):>11.3f}"
          f"{100 * len(strong) / max(1, len(items)):>8.1f}%")

print("\n=== 基础项单独 明显−边缘（不进网格）===")
print(f"{'口径':<34}{'明显合格':>9}{'边缘合格':>9}{'明显不匹配':>11}{'注水型':>8}{'明显−边缘':>10}{'注水−不匹配':>12}")
ref = med_gap(lambda x: round(BUDGET * sum((AP_W.get(d["id"], 0.0) / sum(AP_W.values())) *
               max([i["rel"] for i in d["items"]] or [0.0]) * max([i["c"] for i in d["items"]] or [0.4])
               for d in x["dims"]), 2))
print(f"{'参考：新语义A、无动作词':<34}{ref[0][1]:>9.1f}{ref[0][2]:>9.1f}{ref[0][3]:>11.1f}{ref[0][4]:>8.1f}"
      f"{ref[1]:>10.1f}{ref[2]:>+12.1f}")
best = {}
for w6 in (10.0, 15.0, 20.0, 30.0):
    med, g12, g34 = med_gap(lambda x, w6=w6: base_6th(x, w6))
    best[("6th", w6)] = g12
    print(f"{'口径1 · 独立第6维 w6=%-3g' % w6:<34}{med[1]:>9.1f}{med[2]:>9.1f}{med[3]:>11.1f}{med[4]:>8.1f}"
          f"{g12:>10.1f}{g34:>+12.1f}")
for g in (0.2, 0.3, 0.5, 0.7):
    med, g12, g34 = med_gap(lambda x, g=g: base_weighted(x, g))
    best[("w", g)] = g12
    print(f"{'口径2 · 各维加权 γ=%-3g' % g:<34}{med[1]:>9.1f}{med[2]:>9.1f}{med[3]:>11.1f}{med[4]:>8.1f}"
          f"{g12:>10.1f}{g34:>+12.1f}")

k6 = max([k for k in best if k[0] == "6th"], key=lambda k: best[k])
kw = max([k for k in best if k[0] == "w"], key=lambda k: best[k])
print(f"\n口径1 最好格：w6={k6[1]:g} → {best[k6]:.1f}")
print(f"口径2 最好格：γ={kw[1]:g} → {best[kw]:.1f}")
print(f"\n判定线：>15 才进网格。动作词权重表：强 {len(STRONG)} 词×1.0 · 中 {len(MID)} 词×0.6 · 弱 {len(WEAK)} 词×0.3 "
      f"（密度归一：Σw×100/字数，截断 1.0）")
