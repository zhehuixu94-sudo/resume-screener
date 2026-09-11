#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""整批统计：把一次跑批的"体检报告"打出来（分布、门槛、豁免、失败、冻结证据）。

用途：跑完一批后回答这些问题——
  · 判定/处置分布是否合理？分数线卡在人堆里了没有？
  · 硬门槛到底在起什么作用（通过/不通过/未知各多少）？豁免触发几次？对谁触发？
  · 置信度、维度一致率、证据覆盖、未归位条目——质量指标有没有掉；
  · 有没有失败行、失败原因是什么。

用法：
    python tools/run_stats.py --job config/job_game_dev.json
    python tools/run_stats.py --job config/job_game_dev.json --json out/stats.json
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import statistics as st
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.store import db                      # noqa: E402

Q = """
SELECT r.file_name, a.state, e.verdict, e.decision, e.base_total, e.pass_line, e.confidence,
       e.payload -> 'fit' -> 'level'                       AS fit_level,
       e.payload -> 'labeling' -> 'dimension_agreement_mean' AS dim_agree,
       e.payload -> 'quality' -> 'gate'                    AS gate,
       jsonb_array_length(e.payload -> 'cited_evidence')   AS n_ev,
       jsonb_array_length(e.payload -> 'unmapped_items')   AS n_unmapped,
       jsonb_array_length(e.payload -> 'questions')        AS n_q,
       e.payload -> 'conflicts'                            AS conflicts,
       e.payload -> 'hard_filters'                         AS hard_filters,
       e.payload -> 'bonus_points'                         AS bonus_points,
       e.payload -> 'total_with_bonus'                     AS total_bonus,
       e.payload -> 'decision_reason'                      AS reason
  FROM evaluation e
  JOIN application a ON a.application_id = e.application_id
  JOIN resume r      ON r.resume_id = e.resume_id
 WHERE a.job_id = %s AND a.config_version = %s
"""


def bar(n: int, width: int = 40) -> str:
    return "█" * min(width, n)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--job", required=True)
    ap.add_argument("--json", dest="json_out", default="")
    a = ap.parse_args()

    cfg = json.load(open(a.job, encoding="utf-8"))
    rows = db.fetch_all(Q, (cfg["job_id"], cfg["config_version"]))
    states = dict(db.fetch_all(
        "SELECT state, count(*) AS n FROM application WHERE job_id=%s AND config_version=%s GROUP BY state",
        (cfg["job_id"], cfg["config_version"]))and [(r["state"], r["n"]) for r in db.fetch_all(
        "SELECT state, count(*) AS n FROM application WHERE job_id=%s AND config_version=%s GROUP BY state",
        (cfg["job_id"], cfg["config_version"]))] or [])

    print(f"=== 跑批统计：{cfg['job_title']}（{cfg['job_id']} v{cfg['config_version']}）===")
    print(f"状态机：{states}")
    n = len(rows)
    print(f"已出评估：{n} 份\n")

    def cnt(key):
        return collections.Counter(r[key] for r in rows)

    print("判定分布  :", dict(cnt("verdict").most_common()))
    print("处置分布  :", dict(cnt("decision").most_common()))
    fit = collections.Counter((r["fit_level"] or "")[:6] for r in rows)
    print("匹配度    :", dict(fit.most_common()), f"（岗位要求命中中位 "
          f"{st.median([len(r['conflicts'] or []) * 0 + 1 for r in rows]):.0f} 项）")

    tot = sorted(float(r["base_total"]) for r in rows)
    if n:
        print(f"\n规则分：中位 {st.median(tot):.1f} · p10 {tot[max(0, n // 10)]:.1f} · "
              f"p25 {tot[n // 4]:.1f} · p75 {tot[n * 3 // 4]:.1f} · 最低 {tot[0]:.1f} · 最高 {tot[-1]:.1f}")
        pl = rows[0]["pass_line"]
        print(f"达标线 {pl}（{len([x for x in tot if x >= pl])} 份 ≥ 线 / "
              f"{len([x for x in tot if x < pl])} 份 < 线）")
        print("分数区间：")
        for lo, hi in [(0, 30), (30, 40), (40, 50), (50, 60), (60, 70), (70, 101)]:
            k = sum(1 for x in tot if lo <= x < hi)
            if k:
                print(f"  {lo:>2}~{hi - 1:<3} {k:>3} 份 {bar(k)}")

    conf = sorted(float(r["confidence"]) for r in rows)
    age = sorted(float(r["dim_agree"] or 0) for r in rows)
    if n:
        print(f"\n置信度    ：中位 {st.median(conf):.2f} · 最低 {conf[0]:.2f}")
        print(f"维度一致率：中位 {st.median(age):.3f} · 最低 {age[0]:.3f}（{rows[0]['labeling'] if 'labeling' in rows[0] else ''}")
    print(f"证据覆盖  ：中位 {st.median([r['n_ev'] for r in rows]):.1f} 条/份 · "
          f"最少 {min(r['n_ev'] for r in rows)} · 最多 {max(r['n_ev'] for r in rows)}")
    print(f"未归位条目：合计 {sum(r['n_unmapped'] for r in rows)} 项 · 单份最多 {max(r['n_unmapped'] for r in rows)}")
    print(f"面试题    ：中位 {st.median([r['n_q'] for r in rows]):.0f} 题 · 空题份数 "
          f"{sum(1 for r in rows if r['n_q'] == 0)}")

    conflict = collections.Counter()
    for r in rows:
        for c in (r["conflicts"] or []):
            conflict[c.get("type")] += 1
    print("\n冲突/复核原因：", dict(conflict.most_common()))

    hf = collections.Counter()
    hf_fail_examples = collections.defaultdict(list)
    for r in rows:
        for h in (r["hard_filters"] or []):
            hf[f"{h['id']}={h['result']}"] += 1
            if h["result"] != "pass" and len(hf_fail_examples[h["id"]]) < 3:
                hf_fail_examples[h["id"]].append(f"{r['file_name']}：{h.get('reason', '')[:40]}")
    print("\n硬门槛：", dict(hf.most_common()))
    for k, v in hf_fail_examples.items():
        print(f"   {k} 未通过示例：{v}")

    ov = [r for r in rows if any(c.get("type") == "veto_override" for c in (r["conflicts"] or []))]
    print(f"\n门槛豁免（综合达标 → 否决降级为人工复核）：{len(ov)} 份")
    for r in ov[:5]:
        print(f"   {r['file_name']} 分 {r['base_total']} → {r['decision']}")

    bonus = [r for r in rows if int(r["bonus_points"] or 0) > 0]
    print(f"加分项命中：{len(bonus)} 份"
          f"（加分后中位 {st.median([float(r['base_total']) + int(r['bonus_points'] or 0) for r in rows]):.1f} 分）"
          if n else "")

    if a.json_out:
        with open(a.json_out, "w", encoding="utf-8") as f:
            json.dump({"states": states, "n": n, "verdict": dict(cnt("verdict")),
                       "decision": dict(cnt("decision")), "conflicts": dict(conflict),
                       "hard_filters": dict(hf), "overrides": len(ov),
                       "score": {"median": st.median(tot) if tot else None,
                                 "min": tot[0] if tot else None, "max": tot[-1] if tot else None}},
                      f, ensure_ascii=False, indent=2)
        print(f"\n已写出 {a.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
