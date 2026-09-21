# -*- coding: utf-8 -*-
"""decide() 层「证据全具体 + 零量化」转人工兜底：阈值实测选型（只读库，0 token）。

要回答的问题：G2+scale25 下 40 条危险侧翻转（旧≥55 → 新<55）里，21 条非注水能不能用
一条**显式、可配置**的兜底规则捞回转人工带，同时不把 20 条注水型一起捞回来。

候选信号（全部来自已落库的解析/评估结果，不需要重跑 LLM）：
  C          = 量化结果加成（offline_scoring_v2：Σ per_C，NUM/RESULT 密度）—— C==0 即"零量化"
  fluff_rate = 空话率（生产口径，honesty.resume_signals）
  空话条目占比 = 新 c_item 语义下判空话的条目 / 全部计分条目
  base       = 生产口径基础项（60 分制）
"""
from __future__ import annotations

import collections
import csv
import json
import re
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # noqa: E402
from app.contracts import Evaluation                                 # noqa: E402
from app.pipeline.honesty import FLUFF_DEFAULT, NUM, RESULT           # noqa: E402
from app.store import db                                             # noqa: E402

ACT = re.compile(r"负责|完成|参与|组织|策划|执行|协调|录入|归档|统计|整理|维护|巡检|优化|对接|汇报|编制|核对|跟踪|"
                 r"处理|安排|发放|报销|接待|培训|支持|监控|分析|采购|验收|填写|留存|巡查|交底|定位|传递|监督|"
                 r"审核|发起|推动|交付|迭代|修复|部署|编写|测试|建模")
ORG = re.compile(r"有限公司|公司|集团|学校|学院|大学|中心|协会|学生会|社团|银行|医院|事务所")
CN = {1: "明显合格", 2: "边缘合格", 3: "明显不匹配", 4: "注水型", None: "真实/半合成"}
LINE, BAND, SCALE = 60.0, 5.0, 25.0          # pass_line / band_width / G2 加分 scale
REJ = LINE - BAND                            # 55 = 自动淘汰线
tier = {m["file"]: m["tier"] for m in
        json.load(open("local/samples_admin/manifest.json", encoding="utf-8"))}


def c_A(t: str) -> float:
    if not t:
        return 0.4
    return 0.4 if (FLUFF_DEFAULT.search(t) and not (NUM.search(t) or RESULT.search(t)
                                                    or ACT.search(t) or ORG.search(t))) else 1.0


S = {r["file"]: r for r in csv.DictReader(open("out/scoring_v2_rows.csv", encoding="utf-8-sig"))}
D = {}
for r in db.fetch_all(
        """SELECT r.file_name, r.resume_id, e.payload, e.gate_score, e.decision FROM evaluation e
             JOIN application a USING(application_id) JOIN resume r ON r.resume_id = e.resume_id
            WHERE a.job_id = 'hr_admin' AND a.config_version = '2'"""):
    pay = r["payload"]
    tmap = {c["span_id"]: (c.get("text") or "") for c in (pay.get("cited_evidence") or [])}
    texts = [tmap.get(i.span_id, "") for d in Evaluation.model_validate(pay).dimensions for i in (d.items or [])]
    n = len(texts)
    vague = sum(1 for t in texts if c_A(t) == 0.4)
    D[r["file_name"]] = {"resume_id": r["resume_id"], "n": n, "vague": vague,
                         "vague_share": vague / max(1, n),
                         "fluff": float(pay["score_parts"]["fluff_rate"]),
                         "concrete_share": (n - vague) / max(1, n)}


def new_total(f):
    s = S[f]
    return float(s["base"]) + SCALE * float(s["rank_G2_Σq/√n"]) - float(s["penalty"])


FLIP = []
for f in S:
    o = float(S[f]["old_total"])
    n = new_total(f)
    if o >= REJ and n < REJ:
        FLIP.append(f)
FLIP.sort(key=lambda f: (CN.get(tier.get(f)), -float(S[f]["old_total"])))

print(f"=== 危险侧翻转 {len(FLIP)} 条（旧≥{REJ:g} → 新<{REJ:g}，G2+scale{SCALE:g}）===")
print(f"{'文件':<18}{'档位':<11}{'旧':>6}{'新':>7}{'C':>6}{'空话率':>8}{'空话条目':>9}{'具体占比':>9}{'base':>7}")
for f in FLIP:
    d, s = D[f], S[f]
    print(f"{f:<18}{CN.get(tier.get(f),'?'):<11}{float(s['old_total']):>6.1f}{new_total(f):>7.1f}"
          f"{float(s['C']):>6.2f}{d['fluff']:>8.2f}{d['vague']:>5}/{d['n']:<3}{d['concrete_share']:>9.0%}{float(s['base']):>7.1f}")

print("\n=== 候选规则实测（条件是 AND；『捞回』= 该件从自动淘汰改判转人工）===")
print(f"{'规则':<52}{'捞回非注水':>10}{'构成':>34}{'捞回注水':>9}")
rules = {
    "R0 抄你的原话：C=0 且 空话条目=0": lambda f: float(S[f]["C"]) == 0 and D[f]["vague"] == 0,
    "R1 C=0 且 空话占比≤1/3": lambda f: float(S[f]["C"]) == 0 and D[f]["vague_share"] <= 1 / 3,
    "R2 R1 且 base≥45": lambda f: float(S[f]["C"]) == 0 and D[f]["vague_share"] <= 1 / 3 and float(S[f]["base"]) >= 45,
    "R3 C=0 且 空话率≤0.35": lambda f: float(S[f]["C"]) == 0 and D[f]["fluff"] <= 0.35,
    "R4 R3 且 base≥45": lambda f: (float(S[f]["C"]) == 0 and D[f]["fluff"] <= 0.35
                                    and float(S[f]["base"]) >= 45),
    "R5 C=0 且 空话率≤0.35 且 具体占比≥0.8": lambda f: (float(S[f]["C"]) == 0 and D[f]["fluff"] <= 0.35
                                                        and D[f]["concrete_share"] >= 0.8),
    "R6 C=0 且 空话率≤0.35 且 80≥base≥45": lambda f: (float(S[f]["C"]) == 0 and D[f]["fluff"] <= 0.35
                                                     and 45 <= float(S[f]["base"]) <= 80),
}
for name, fn in rules.items():
    got = [f for f in FLIP if fn(f)]
    comp = collections.Counter(CN.get(tier.get(f)) for f in got)
    nonw = [f for f in got if CN.get(tier.get(f)) != "注水型"]
    print(f"{name:<52}{len(nonw):>10}{str(dict(comp)):>34}{comp.get('注水型',0):>9}")

print("\n=== 全库影响（规则作用域是全部 310 份，不只翻转件）===")
for name, fn in rules.items():
    hit = [f for f in S if fn(f)]
    to_review = [f for f in hit if new_total(f) < REJ]
    comp = collections.Counter(CN.get(tier.get(f)) for f in to_review)
    print(f"{name:<52} 命中 {len(hit):>3} 份 · 其中新分<{REJ:g} 被改判转人工 {len(to_review):>3} 份 {dict(comp)}")

print("\n=== 注水型/非注水的信号分离度（翻转件内）===")
for label, sel in (("非注水", lambda f: CN.get(tier.get(f)) != "注水型"),
                   ("注水型", lambda f: CN.get(tier.get(f)) == "注水型")):
    fs = [f for f in FLIP if sel(f)]
    if not fs:
        continue
    print(f"  {label} {len(fs)} 条：C 中位 {sorted(float(S[f]['C']) for f in fs)[len(fs)//2]:.2f} · "
          f"空话率 中位 {sorted(D[f]['fluff'] for f in fs)[len(fs)//2]:.2f} "
          f"( min {min(D[f]['fluff'] for f in fs):.2f} / max {max(D[f]['fluff'] for f in fs):.2f} ) · "
          f"具体占比 中位 {sorted(D[f]['concrete_share'] for f in fs)[len(fs)//2]:.0%} · "
          f"base 中位 {sorted(float(S[f]['base']) for f in fs)[len(fs)//2]:.1f}")
