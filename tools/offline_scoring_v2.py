# -*- coding: utf-8 -*-
"""加分项纯代码化：**离线验证**（只读数据库，不碰生产口径、不改代码）。

为什么要做：实测加分项（40 分，基于「模型给的相关度 × 难度」的本批分位）与证据条数相关系数
ρ = **+0.73** —— 它实质是个"条目计数器"，因此注水型（术语多、条数多）拿到高加分，
出现"注水型 50.4 > 明显不匹配 38.4"的排序倒挂；把加分项摘掉后倒挂消失、但区分度塌到 6.2 分。
结论是**重设计输入**：相关度/难度改由纯代码算，目标是 |ρ| < 0.15。

四类特征（全部纯代码、0 token，只看证据片段文本）：
  A 技能词典匹配：命中岗位/通用技能词典的词数（去重）
  B 动作词权重：命中动作词（负责/完成/组织/录入/归档…）的词数（去重）
  C 量化结果加成：片段里含数字 + 成果动词的比例（沿用 honesty 的四要素思路）
  D 条目数惩罚：q × √n（G2）或 q × (1+ln n)（G3），q = 每条特征的均值 —— 让"条数多"只按亚线性计分

对比组：G0 旧口径（模型打分分位）/ G1 仅 A / G2 A+B+C+√n / G3 A+B+C+(1+ln n)

用法：python tools/offline_scoring_v2.py [--job config/job_hr_admin.json] [--out out/scoring_v2]
"""
from __future__ import annotations

import argparse
import collections
import csv
import json
import math
import os
import re
import statistics as st
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # noqa: E402
from app.contracts import Evaluation, JobConfig       # noqa: E402
from app.pipeline.honesty import NUM, RESULT          # noqa: E402
from app.pipeline.weighted_total import percentile_rank   # noqa: E402
from app.store import db                              # noqa: E402

CN = {1: "明显合格", 2: "边缘合格", 3: "明显不匹配", 4: "注水型"}
ACTIONS = re.compile(r"负责|完成|参与|组织|策划|执行|协调|录入|归档|统计|整理|维护|巡检|优化|"
                     r"对接|汇报|编制|核对|跟踪|处理|安排|发放|报销|接待|培训|支持|监控|分析")
# 通用技能词典（岗位相关的部分从 job_config 的维度 keywords / 加分项关键词里取）
GENERIC_SKILLS = ["office", "excel", "word", "ppt", "wps", "erp", "oa", "cad", "python", "sql",
                  "linux", "英语", "四级", "六级", "普通话", "驾照", "会计", "人力资源", "档案",
                  "考勤", "社保", "公积金", "财务", "ps", "剪辑", "新媒体", "公文", "excel", "报表"]

ap = argparse.ArgumentParser()
ap.add_argument("--job", default="config/job_hr_admin.json")
ap.add_argument("--corpus", default="local/samples_admin")
ap.add_argument("--out", default="out/scoring_v2")
a = ap.parse_args()
cfg = JobConfig.model_validate(json.load(open(a.job, encoding="utf-8")))
tier = {m["file"]: m["tier"] for m in json.load(open(os.path.join(a.corpus, "manifest.json"),
                                                    encoding="utf-8"))}
# 岗位技能词典：维度 keywords + 加分项 must_match/keywords + 通用词
skills = []
for d in cfg.dimensions:
    skills += list(getattr(d, "keywords", []) or [])
for b in (cfg.bonus or []):
    for g in (b.must_match or []):
        skills += list(g)
    skills += list(b.keywords or [])
skills = sorted({str(s).lower() for s in skills + GENERIC_SKILLS if str(s).strip()})
print(f"技能词典 {len(skills)} 词 · 动作词表就绪 · 证据片段来自解析结果（read-only）\n")

rows = db.fetch_all(
    """SELECT r.file_name, r.resume_id, e.payload, e.gate_score, e.decision
         FROM evaluation e JOIN application ap USING(application_id)
         JOIN resume r ON r.resume_id = e.resume_id
        WHERE ap.job_id = %s AND ap.config_version = %s""", (cfg.job_id, cfg.config_version))

snap = []
for r in rows:
    ev = Evaluation.model_validate(r["payload"])
    p = ev.score_parts
    prow = db.fetch_one("SELECT payload FROM resume_parse WHERE resume_id = %s", (r["resume_id"],))
    spans = [e["text"] for e in ((prow or {}).get("payload") or {}).get("evidence", [])] if prow else []
    spans = [s for s in spans if s and len(s.strip()) >= 6]
    n = len(spans)
    low = " ".join(spans).lower()

    # 特征归一：**按密度**（每 100 字命中数）再截断到 [0,1]，不按命中次数——
    # 上一轮 min(1, hits/2) 在 2 次命中就饱和，导致每条得分趋近常数、原始量退化为 f(n)。
    def dens(hits: int, chars: int) -> float:
        return min(1.0, hits * 100.0 / max(1, chars))

    per_A, per_B, per_C, sat = [], [], [], {"A": 0, "B": 0, "C": 0}
    for sp in spans:
        chars = len(sp.strip())
        ha = sum(1 for k in skills if k in sp.lower())
        hb = len(set(ACTIONS.findall(sp)))
        hc = (1 if NUM.search(sp) else 0) + (1 if RESULT.search(sp) else 0)
        a_i, b_i, c_i = dens(ha, chars), dens(hb, chars), dens(hc, chars)
        sat["A"] += a_i >= 1.0; sat["B"] += b_i >= 1.0; sat["C"] += c_i >= 1.0
        per_A.append(a_i); per_B.append(b_i); per_C.append(c_i)

    # 单条质量分：权重按用户口径翻转 —— C（量化结果）是唯一实测能把注水型分出来的量
    W = (0.2, 0.2, 0.6)
    per_Q = [W[0] * per_A[i] + W[1] * per_B[i] + W[2] * per_C[i] for i in range(n)]
    per_Q_noc = [q_ * (0.5 if per_C[i] == 0 else 1.0) for i, q_ in enumerate(per_Q)]   # C 缺失二次惩罚
    A_cnt, B_cnt, C_cnt = sum(per_A), sum(per_B), sum(per_C)
    qa = st.mean(per_A) if n else 0.0
    qb = st.mean(per_B) if n else 0.0
    qc = st.mean(per_C) if n else 0.0
    q_all = st.mean(per_Q) if n else 0.0
    sq = math.sqrt(n) if n else 0.0
    sn = (1 + math.log(n)) if n else 1.0
    snap.append({
        "file": r["file_name"], "resume_id": r["resume_id"], "tier": tier.get(r["file_name"]),
        "source": ("real" if r["file_name"].startswith("real_")
                   else ("admin_corpus" if tier.get(r["file_name"]) else "unknown")),
        "calibration_eligible": False,        # 冷启动：无淘汰件 → 无复核结论可进校准池（语义固化）
        "n_spans": n, "old_bonus": (p.bonus if p else 0.0), "old_total": float(r["gate_score"] or 0),
        "old_decision": r["decision"], "base": (p.base if p else 0.0), "penalty": (p.penalty if p else 0.0),
        "A": round(A_cnt, 3), "B": round(B_cnt, 3), "C": round(C_cnt, 3),
        "qa": round(qa, 4), "qb": round(qb, 4), "qc": round(qc, 4), "q": round(q_all, 4),
        "sat_A": round(sat["A"] / max(1, n), 3), "sat_B": round(sat["B"] / max(1, n), 3),
        "sat_C": round(sat["C"] / max(1, n), 3),
        "sumQ": round(sum(per_Q), 4), "sumQ_noc": round(sum(per_Q_noc), 4),
    })

os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
snap_path = a.out + "_snapshot.csv"
with open(snap_path, "w", encoding="utf-8-sig", newline="") as f:
    w = csv.DictWriter(f, fieldnames=list(snap[0].keys()))
    w.writeheader(); w.writerows(snap)
print(f"[步骤1] 快照已冻结：{snap_path}（{len(snap)} 份，含 resume_id/条数/旧加分/档位/旧总分/source/calibration_eligible）")

# —— 四个对比组的加分项原始量
variants = {
    "G0_旧口径": {x["file"]: x["old_bonus"] / 40.0 for x in snap},                 # 模型打分分位
    "G1_仅A(密度)": {x["file"]: x["A"] for x in snap},
    "G2_Σq/√n": {x["file"]: x["sumQ"] / math.sqrt(x["n_spans"]) if x["n_spans"] else 0.0
                 for x in snap},
    "G3_Σq/(1+ln n)": {x["file"]: x["sumQ"] / (1 + math.log(x["n_spans"])) if x["n_spans"] else 0.0
                       for x in snap},
    "G2b_Σq/√n(C缺失×0.5)": {x["file"]: x["sumQ_noc"] / math.sqrt(x["n_spans"]) if x["n_spans"] else 0.0
                             for x in snap},
    "G4_Σq/n(纯均值·诊断)": {x["file"]: x["sumQ"] / x["n_spans"] if x["n_spans"] else 0.0
                             for x in snap},
}
# G5/G6：按证据条数分桶（5 桶），**桶内**算分位 —— 直接把"条数多就分高"这条通道切断
def bucket_rank(raw: dict[str, float], key: str = "n_spans", nb: int = 5) -> dict[str, float]:
    order = sorted(snap, key=lambda z: z[key])
    out = {}
    for i, x in enumerate(order):
        out[x["file"]] = i // max(1, (len(order) + nb - 1) // nb)     # 桶号
    for b in set(out.values()):
        members = [f for f, bb in out.items() if bb == b]
        sub = {f: raw[f] for f in members}
        rk = percentile_rank(sub)
        for f in members:
            out[f] = rk[f]
    return out

variants["G5_桶内分位(Σq/√n)"] = {x["file"]: v for x, v in
                                 ((x, 0) for x in snap)}
_raw5 = {x["file"]: x["sumQ"] / math.sqrt(x["n_spans"]) if x["n_spans"] else 0.0 for x in snap}
_rk5 = bucket_rank(_raw5)
variants["G5_桶内分位(Σq/√n)"] = _rk5
_raw6 = {x["file"]: x["q"] for x in snap}      # 纯单条质量均值
_rk6 = bucket_rank(_raw6)
variants["G6_桶内分位(单条均值q)"] = _rk6

metrics = []
for name, raw in variants.items():
    if name.startswith("G5_") or name.startswith("G6_"):
        rank = raw
    elif name == "G0_旧口径":
        rank = {x["file"]: x["old_bonus"] / 40.0 for x in snap}
    else:
        rank = percentile_rank(raw)
    for x in snap:
        x["rank_" + name] = rank[x["file"]]
        x["bonus_" + name] = round(40 * rank[x["file"]], 2)
        x["total_" + name] = round(x["base"] + 40 * rank[x["file"]] - x["penalty"], 2)
    med = {t: st.median([x["total_" + name] for x in snap if x["tier"] == t]) for t in (1, 2, 3, 4)}
    ns = [x["n_spans"] for x in snap]; bs = [40 * rank[x["file"]] for x in snap]
    mx, my = st.mean(ns), st.mean(bs)
    cov = sum((p_ - mx) * (q_ - my) for p_, q_ in zip(ns, bs)) / len(ns)
    rho = cov / (st.pstdev(ns) * st.pstdev(bs)) if st.pstdev(ns) and st.pstdev(bs) else 0.0
    passed70 = sum(1 for x in snap if x["total_" + name] >= 70 and x["base"] + 40 * rank[x["file"]] - x["penalty"] >= 70)
    old_top20 = {x["file"] for x in sorted(snap, key=lambda z: -z["old_total"])[:max(1, len(snap) // 5)]}
    new_top20 = {x["file"] for x in sorted(snap, key=lambda z: -z["total_" + name])[:max(1, len(snap) // 5)]}
    overlap = len(old_top20 & new_top20) / len(old_top20)
    metrics.append({"group": name, "rho_条数": round(rho, 3), "四档中位": " / ".join(f"{med[t]:.1f}" for t in (1, 2, 3, 4)),
                    "明显合格−边缘": round(med[1] - med[2], 1), "注水<不匹配": med[4] < med[3],
                    "线70放行": passed70, "Top20%重叠": round(overlap, 2)})

mp = a.out + "_metrics.csv"
with open(mp, "w", encoding="utf-8-sig", newline="") as f:
    w = csv.DictWriter(f, fieldnames=list(metrics[0].keys())); w.writeheader(); w.writerows(metrics)
with open(a.out + "_rows.csv", "w", encoding="utf-8-sig", newline="") as f:
    w = csv.DictWriter(f, fieldnames=list(snap[0].keys())); w.writeheader(); w.writerows(snap)

print(f"\n[步骤4/5] 对比组指标（{len(snap)} 份；硬判据见下）")
print(f"{'组':<20}{'ρ(条数)':>9}{'四档中位':>28}{'明显−边缘':>10}{'注水<不匹配':>12}{'线70放行':>9}{'Top20%重叠':>11}")
for m in metrics:
    print(f"{m['group']:<20}{m['rho_条数']:>9.2f}{m['四档中位']:>28}{m['明显合格−边缘']:>10.1f}"
          f"{str(m['注水<不匹配']):>12}{m['线70放行']:>9}{m['Top20%重叠']:>11.2f}")

print("\n[步骤6] 硬判据（全部满足才可进生产口径）")
for m in metrics:
    c = [("|ρ|<0.15", abs(m["rho_条数"]) < 0.15), ("明显合格−边缘>20", m["明显合格−边缘"] > 20),
         ("注水<不匹配", m["注水<不匹配"]), ("线70放行 65~75", 65 <= m["线70放行"] <= 75),
         ("Top20%重叠>0.7", m["Top20%重叠"] > 0.7)]
    bad = [n for n, ok in c if not ok]
    print(f"  {m['group']:<20}{'✅ 全过' if not bad else '❌ 未过：' + '、'.join(bad)}")
print(f"\nCSV：{mp} · {a.out}_rows.csv · 快照 {snap_path}")
