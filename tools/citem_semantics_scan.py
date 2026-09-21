# -*- coding: utf-8 -*-
"""新 c_item 语义下的「基础项单独」扫描：不进网格、不动 λ/scale，只出 明显−边缘 一个决策数。

语义改动（本轮唯一改动，仍在离线工具里；生产 score.py / decide() 未动）：
  旧：c_item = 1.0(数字+成果) / 0.7(其一) / 0.4(都没有)   ← 把「没写数字」当「不具体」惩罚
  新：只惩罚空话。空话 = 命中空话模板 且 无任何可核实要素（数字/成果词/动作词/机构名）
      · 缺失文本 → 0.4（兜底不变）
      · 空话     → 0.4
      · 其余     → 1.0（**不再因为没数字扣分**）

三种口径对照：old（现口径）/ A（新语义·二分，本轮主口径）/ B（新语义·留一档 0.7 给"纯名词短语"）
文本源两套都打印：span 字段（修测量前）/ 真原文（修测量后）。

用法：python tools/citem_semantics_scan.py
"""
from __future__ import annotations

import collections
import json
import os
import re
import statistics as st
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # noqa: E402
from app.contracts import Evaluation                         # noqa: E402
from app.pipeline.honesty import FLUFF_DEFAULT, NUM, RESULT   # noqa: E402
from app.store import db                                     # noqa: E402

AP_W = {"admin": 30.0, "hr_ops": 25.0, "comm": 20.0, "office2": 15.0, "attitude": 10.0}
BUDGET = 60.0
CN = {1: "明显合格", 2: "边缘合格", 3: "明显不匹配", 4: "注水型"}
ACTIONS = re.compile(r"负责|完成|参与|组织|策划|执行|协调|录入|归档|统计|整理|维护|巡检|优化|对接|汇报|"
                     r"编制|核对|跟踪|处理|安排|发放|报销|接待|培训|支持|监控|分析|采购|验收|填写|留存|"
                     r"巡查|交底|定位|传递|监督|审核|发起|推动|交付|迭代|修复|部署|编写|测试|建模")
ORG = re.compile(r"有限公司|公司|集团|学校|学院|大学|中学|中学|中心|协会|学生会|社团|部门|部门|银行|医院|事务所")
tier = {m["file"]: m["tier"] for m in
        json.load(open("local/samples_admin/manifest.json", encoding="utf-8"))}

RECS = []
for r in db.fetch_all(
        """SELECT r.file_name, r.resume_id, e.payload FROM evaluation e
             JOIN application a USING(application_id) JOIN resume r ON r.resume_id = e.resume_id
            WHERE a.job_id = 'hr_admin' AND a.config_version = '2'"""):
    ev = Evaluation.model_validate(r["payload"])
    pr = db.fetch_one("SELECT payload FROM resume_parse WHERE resume_id = %s", (r["resume_id"],))
    pmap = {s["span_id"]: (s.get("text") or "") for s in ((pr or {}).get("payload") or {}).get("evidence", []) or []}
    tmap = {c["span_id"]: (c.get("text") or "") for c in (r["payload"].get("cited_evidence") or [])}
    dims = []
    for d in ev.dimensions:
        items = []
        for i in d.items or []:
            sid = i.span_id
            items.append({"sid": sid, "rel": i.relevance_score or 0.0,
                          "t_span": pmap.get(sid, ""), "t_true": tmap.get(sid) or pmap.get(sid, "")})
        dims.append({"id": d.id, "items": items})
    RECS.append({"f": r["file_name"], "t": tier.get(r["file_name"]), "dims": dims})


def concrete(t: str) -> bool:
    return bool(NUM.search(t) or RESULT.search(t) or ACTIONS.search(t) or ORG.search(t))


def c_old(t: str) -> float:
    if not t:
        return 0.4
    num, res = bool(NUM.search(t)), bool(RESULT.search(t))
    return 1.0 if (num and res) else (0.7 if (num or res) else 0.4)


def c_A(t: str) -> float:                    # 主口径：只惩罚空话
    if not t:
        return 0.4
    vague = bool(FLUFF_DEFAULT.search(t)) and not concrete(t)
    return 0.4 if vague else 1.0


def c_B(t: str) -> float:                    # 敏感性：非空话但一点可核实要素都没有 → 0.7
    if not t:
        return 0.4
    if FLUFF_DEFAULT.search(t) and not concrete(t):
        return 0.4
    return 1.0 if concrete(t) else 0.7


CS = {"old": c_old, "A": c_A, "B": c_B}


def dim_c(d, fn, src):
    cs = [fn(i[src]) for i in d["items"]]
    return max(cs) if cs else 0.4


def base(x, scheme, fn, src):
    tot = sum(AP_W.values())
    s = 0.0
    for d in x["dims"]:
        rel = max([i["rel"] for i in d["items"]] or [0.0])
        c = dim_c(d, fn, src)
        r2 = (0.25 * rel + 0.75 * c) if scheme == "D" else rel * c
        s += (AP_W.get(d["id"], 0.0) / tot) * r2
    return round(BUDGET * s, 2)


def gaps(scheme, fn, src):
    med = {t: st.median([base(x, scheme, fn, src) for x in RECS if x["t"] == t]) for t in (1, 2, 3, 4)}
    return med, med[1] - med[2], med[4] - med[3]


print("=== 基础项单独（不进网格、不动 λ/scale）===")
print(f"{'文本源':<14}{'语义':<6}{'方案':<6}{'明显合格':>9}{'边缘合格':>9}{'明显不匹配':>11}{'注水型':>8}"
      f"{'明显−边缘':>10}{'注水−不匹配':>12}")
res = {}
for src, slab in (("t_span", "span 字段"), ("t_true", "真原文")):
    for sem in ("old", "A", "B"):
        for scheme in ("D", "C"):
            med, g12, g34 = gaps(scheme, CS[sem], src)
            res[(src, sem, scheme)] = (g12, g34, med)
            print(f"{slab:<14}{sem:<6}{scheme:<6}{med[1]:>9.1f}{med[2]:>9.1f}{med[3]:>11.1f}{med[4]:>8.1f}"
                  f"{g12:>10.1f}{g34:>+12.1f}")
# 参考：完全不加 c（纯 rel）——上一轮实测的 6.2 分就是这一档
for src, slab in (("t_true", "真原文"),):
    med = {}
    for t in (1, 2, 3, 4):
        vals = []
        for x in RECS:
            if x["t"] != t:
                continue
            tot = sum(AP_W.values())
            vals.append(round(BUDGET * sum((AP_W.get(d["id"], 0.0) / tot) *
                                           max([i["rel"] for i in d["items"]] or [0.0]) for d in x["dims"]), 2))
        med[t] = st.median(vals)
    print(f"{slab:<14}{'无c':<6}{'rel':<6}{med[1]:>9.1f}{med[2]:>9.1f}{med[3]:>11.1f}{med[4]:>8.1f}"
          f"{med[1] - med[2]:>10.1f}{med[4] - med[3]:>+12.1f}")

# —— 分类器体检：各 c 档的条目数与占比（真原文口径）
print("\n=== 新语义 A 的条目分布（真原文口径，4522 条）===")
cnt = collections.Counter()
for x in RECS:
    for d in x["dims"]:
        for i in d["items"]:
            cnt[("A", c_A(i["t_true"]))] += 1
            cnt[("old", c_old(i["t_true"]))] += 1
tot = sum(v for k, v in cnt.items() if k[0] == "A")
for sem in ("old", "A"):
    print(f"  {sem}: " + " · ".join(f"c={c}: {cnt[(sem, c)]} 条（{100 * cnt[(sem, c)] / tot:.1f}%）" for c in (1.0, 0.7, 0.4)))
print("\n判为空话（新语义 A 扣分）的样例 12 条：")
shown = 0
for x in RECS:
    for d in x["dims"]:
        for i in d["items"]:
            if c_A(i["t_true"]) == 0.4 and i["t_true"] and shown < 12:
                print(f"   · {i['t_true'][:60]}")
                shown += 1
print("\n旧口径扣分、新语义 A 不扣分的样例 12 条（= 被错误惩罚的那批）：")
shown = 0
for x in RECS:
    for d in x["dims"]:
        for i in d["items"]:
            if c_old(i["t_true"]) == 0.4 and c_A(i["t_true"]) == 1.0 and shown < 12:
                print(f"   · {i['t_true'][:60]}")
                shown += 1
print(f"\n决策数（真原文 · 新语义 A · 方案 D/C 基础项单独 明显−边缘）= "
      f"{res[('t_true', 'A', 'D')][0]:.1f} / {res[('t_true', 'A', 'C')][0]:.1f}")
