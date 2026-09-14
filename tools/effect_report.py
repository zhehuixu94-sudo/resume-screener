#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""方案效果度量：一次跑批的「覆盖率 / 分数分布 / 判定处置」三个口径，并与事先写死的基线对照。

为什么单独做这个工具：改动（如"按维度定向取证"）的效果判断标准必须**事先写死**，
否则就是事后自我说服。这个脚本把基线固化在 BASELINE 里，跑完直接给"达标/未达标"，不给人看数据挑口径的空间。

用法：
    python tools/effect_report.py --job config/job_game_dev.json --json out/effect_v3.json
    python tools/effect_report.py --job config/job_game_dev.json --baseline-version 2   # 顺手算基线版同口径数字
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import statistics as st
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.store import db  # noqa: E402

# 事先写死的判断标准（写于改动前，不许事后改）：见 Desktop《简历初筛系统_明日开工清单.md》第 2 节
BASELINE = {
    "version": 2,
    "coverage": {"project": 0.31, "gamedesign": 0.29, "learning": 0.16},
    "score": {"median": 33.3, "sd": 7.62, "lo": 13.2, "hi": 60.0},
    "decision": {"manual_review": 90, "auto_reject": 10, "auto_pass": 0},
    "criteria": {
        "coverage": "三个空缺维度覆盖率都 ≥65%",
        "score_sd": "标准差明显高于 7.62，且不再是单峰挤在 30~39",
        "auto_pass": "自动达标不再恒为 0",
    },
}

Q = """
SELECT r.file_name, a.state, e.verdict, e.decision, e.base_total, e.confidence,
       e.pass_line,
       e.payload -> 'coverage'      AS coverage,
       e.payload -> 'stages'        AS stages,
       e.payload -> 'conflicts'     AS conflicts,
       e.payload -> 'cited_evidence' AS cited,
       e.payload -> 'questions'     AS questions,
       e.payload -> 'bonus_points'  AS bonus,
       e.payload -> 'hard_filters'  AS hard_filters,
       e.payload -> 'total_with_bonus' AS total_bonus
  FROM evaluation e
  JOIN application a ON a.application_id = e.application_id
  JOIN resume r ON r.resume_id = e.resume_id
 WHERE a.job_id = %s AND a.config_version = %s
"""


def pct(x: float) -> str:
    return f"{x * 100:.0f}%"


def quantiles(xs: list[float]) -> dict[str, float]:
    if not xs:
        return {}
    n = len(xs)
    idx = lambda p: xs[max(0, min(n - 1, int(p * n)))]           # noqa: E731
    return {"min": xs[0], "p10": idx(0.10), "p25": idx(0.25), "median": st.median(xs),
            "p75": idx(0.75), "p90": idx(0.90), "max": xs[-1],
            "mean": round(st.fmean(xs), 2), "sd": round(st.pstdev(xs), 2)}


def collect(job_id: str, version: str, dim_names: dict[str, str]) -> dict:
    rows = db.fetch_all(Q, (job_id, version))
    out: dict = {"version": version, "n": len(rows)}
    if not rows:
        return out

    scores = sorted(float(r["base_total"]) for r in rows)
    out["score"] = quantiles(scores)
    out["hist"] = {f"{lo}~{lo + 9}": sum(1 for s in scores if lo <= s <= lo + 9)
                   for lo in range(0, 100, 10)}
    out["verdict"] = dict(collections.Counter(r["verdict"] for r in rows).most_common())
    out["decision"] = dict(collections.Counter(r["decision"] for r in rows).most_common())
    out["pass_line"] = str(rows[0]["pass_line"])

    # 维度覆盖率：coverage 里 found=false 的维度 = 「该维度无证据」
    per_dim = collections.Counter()
    per_dim_reason = collections.Counter()
    found_counts = []
    for r in rows:
        cov = r["coverage"] or []
        if isinstance(cov, str):
            cov = json.loads(cov)
        found = [c for c in cov if c.get("found")]
        found_counts.append(len(found))
        for c in cov:
            if c.get("found"):
                per_dim[c["id"]] += 1
            elif c.get("note"):
                per_dim_reason[c["id"]] += 1
    out["coverage"] = {dim_names.get(k, k): round(v / len(rows), 4) for k, v in per_dim.items()}
    out["coverage_raw"] = dict(per_dim)
    out["coverage_reason_given"] = dict(per_dim_reason)
    out["dims_with_evidence"] = {"median": st.median(found_counts),
                                 "hist": dict(sorted(collections.Counter(found_counts).items()))}

    # 质量：阶段降级、置信度、空题、证据条数
    stage_stat = collections.Counter()
    degraded_rows = 0
    for r in rows:
        stg = r["stages"] or {}
        if isinstance(stg, list):
            stg = {s.get("name"): s for s in stg}
        bad = False
        for name, s in stg.items():
            status = ((s or {}).get("validation") or {}).get("status")
            stage_stat[f"{name}={status}"] += 1
            if status in ("degraded", "failed"):
                bad = True
        degraded_rows += 1 if bad else 0
    out["stages"] = dict(stage_stat.most_common())
    out["rows_with_degraded_stage"] = degraded_rows
    conf = sorted(float(r["confidence"] or 0) for r in rows)
    out["confidence"] = {"median": round(st.median(conf), 2), "min": conf[0],
                         "zero": sum(1 for c in conf if c == 0)}
    out["n_cited_evidence"] = {"median": st.median([len(r["cited"] or []) for r in rows]),
                               "min": min(len(r["cited"] or []) for r in rows),
                               "max": max(len(r["cited"] or []) for r in rows)}
    out["empty_questions"] = sum(1 for r in rows if not (r["questions"] or []))
    out["bonus_hit"] = sum(1 for r in rows if int(r["bonus"] or 0) > 0)
    out["conflicts"] = dict(collections.Counter(
        c.get("type") for r in rows for c in (r["conflicts"] or [])).most_common())
    # 本批实际用的模型（同口径对照的前提：整批只能有一个模型）
    models = collections.Counter()
    for r in rows:
        stg = r["stages"] or {}
        if isinstance(stg, list):
            stg = {s.get("name"): s for s in stg}
        for s in stg.values():
            if (s or {}).get("model_id"):
                models[(s or {})["model_id"]] += 1
    out["models"] = dict(models.most_common())
    out["model"] = models.most_common(1)[0][0] if len(models) == 1 else \
        "、".join(f"{k}({v} 段)" for k, v in models.most_common())

    out["hard_filters"] = dict(collections.Counter(
        f"{h['id']}={h['result']}" for r in rows for h in (r["hard_filters"] or [])).most_common())
    return out


def verdicts(cur: dict, base: dict) -> list[dict]:
    """按事先写死的三条标准判达标/未达标。"""
    res = []
    cov, best = cur.get("coverage", {}), base["coverage"]
    # 基线键 → 维度名字（v2 的关键词探针口径 ↔ v3 的 coverage 字段，同一批维度）
    pairs = [("项目完整度与交付", best["project"]), ("游戏理解与玩法素养", best["gamedesign"]),
             ("学习与竞赛产出", best["learning"])]
    for name, b in pairs:
        now = cov.get(name, 0)
        res.append({"criterion": f"覆盖率 {name} ≥65%", "baseline": pct(b), "now": pct(now),
                    "pass": now >= 0.65})
    sd = cur.get("score", {}).get("sd", 0)
    res.append({"criterion": "标准差明显高于 7.62", "baseline": "7.62", "now": str(sd),
                "pass": sd > base["score"]["sd"] * 1.15})
    ap = cur.get("decision", {}).get("auto_pass", 0)
    res.append({"criterion": "自动达标不再恒为 0", "baseline": "0", "now": str(ap), "pass": ap > 0})
    return res


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--job", required=True)
    ap.add_argument("--json", dest="json_out", default="")
    ap.add_argument("--baseline-version", default="")
    a = ap.parse_args()
    cfg = json.load(open(a.job, encoding="utf-8"))
    dims = {d["id"]: d.get("name", d["id"]) for d in cfg.get("dimensions", [])}

    cur = collect(cfg["job_id"], str(cfg["config_version"]), dims)
    print(f"=== 效果度量：{cfg['job_title']} v{cfg['config_version']}（{cur['n']} 份）===")
    print("分数   :", cur.get("score"))
    print("分布   :", cur.get("hist"))
    print("判定   :", cur.get("verdict"), " 处置:", cur.get("decision"))
    print("覆盖率 :", {k: pct(v) for k, v in cur.get("coverage", {}).items()})
    print("有证据维度数:", cur.get("dims_with_evidence"))
    print("降级行 :", cur.get("rows_with_degraded_stage"), " 阶段:", cur.get("stages"))
    print("置信度 :", cur.get("confidence"), " 空题:", cur.get("empty_questions"),
          " 加分命中:", cur.get("bonus_hit"))
    print("冲突   :", cur.get("conflicts"))

    v = verdicts(cur, BASELINE)
    print("\n--- 对照事先写死的标准（基线 v2）---")
    for x in v:
        print(f"  [{'达标' if x['pass'] else '未达标'}] {x['criterion']}：{x['baseline']} → {x['now']}")

    payload = {"current": cur, "baseline": BASELINE, "verdicts": v}
    if a.baseline_version:
        b = collect(cfg["job_id"], a.baseline_version, dims)
        payload["baseline_collected"] = b
        print(f"\n基线 v{a.baseline_version} 同口径：分数 {b.get('score')} 处置 {b.get('decision')}")
    if a.json_out:
        json.dump(payload, open(a.json_out, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        print(f"\n已写出 {a.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
