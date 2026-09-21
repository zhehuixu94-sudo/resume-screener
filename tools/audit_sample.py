# -*- coding: utf-8 -*-
"""路线 A · 抽检抽样与标记位（**离线只读**，不写库）：产出 out/audit_sample_v1.csv 供分布检查。

抽检池 = 自动淘汰件 + 转人工件（不复制数据：两类都从同一张 evaluation 读，靠标记位区分）：
  source            这一条属于哪一类 —— auto_reject / borderline / model_conflict /
                    hard_gate_unknown / auto_pass / manual_review（互斥、有优先级）
  calibration_eligible         仅当 source=auto_reject **且** 复核结论=确实该淘汰 → True（未复核 = False）
  calibration_eligible_candidate   = (source == auto_reject)：复核判 reject 后它才会变成 True

抽样优先级（用户口径）：① 擦线件（|分−线| ≤ 2）→ ② 模型内部打架 → ③ 硬门槛 unknown → ④ 其余分数降序；
**每类非空保底 1 条**，否则纯按优先级排会让后面两类永远抽不到、分布检查误以为本批没有那类。

用法：
  python tools/audit_sample.py --job config/job_hr_admin.json --want 40
  python tools/audit_sample.py --want 40 --include-auto-pass      # 顺带抽查自动达标件（默认不抽）
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # noqa: E402
from app.contracts import JobConfig                              # noqa: E402
from app.review import (DEFAULT_POOL_SOURCES, MAX_BUCKET_SHARE, PRIORITY_CLASSES,  # noqa: E402
                        PRIORITY_LABEL, SAMPLE_WEIGHTS, SOURCE_AUTO_PASS, _pick_by_quota, pool)

ap = argparse.ArgumentParser()
ap.add_argument("--job", default="config/job_hr_admin.json")
ap.add_argument("--want", type=int, default=40, help="抽几份（None/0 = 全池）")
ap.add_argument("--out", default="out/audit_sample_v1.csv")
ap.add_argument("--pool-out", default="out/audit_pool_v1.csv")
ap.add_argument("--write-db", action="store_true",
                help="把这次抽样落库成当天批次（spot_check_item，含 source/priority_class，eligible=false）——复核的前置")
ap.add_argument("--include-auto-pass", action="store_true",
                help="把自动达标件也纳入池子（默认不抽：有稳定性闸门兜着，抽检成本留给风险段）")
a = ap.parse_args()

cfg = JobConfig.model_validate(json.load(open(a.job, encoding="utf-8")))
sources = DEFAULT_POOL_SOURCES + ((SOURCE_AUTO_PASS,) if a.include_auto_pass else ())
rows = pool(cfg, sources=sources)
want = None if not a.want else a.want
picked, info = _pick_by_quota(rows, cfg, want)
line = float(cfg.scoring.pass_threshold())

COLS = ["rank", "application_id", "file_name", "gate_score", "system_decision", "system_verdict",
        "source", "priority_class", "priority_cn", "calibration_eligible_candidate",
        "calibration_eligible", "review_verdict", "reviewer", "review_ts",
        "conflicts", "hard_gate_unknown", "score_minus_line"]


def row_of(i: int, r: dict) -> dict:
    return {
        "rank": i, "application_id": r["application_id"], "file_name": r["file_name"],
        "gate_score": r["gate_score"], "system_decision": r["decision"], "system_verdict": r.get("verdict"),
        "source": r["source"], "priority_class": r["priority_class"],
        "priority_cn": PRIORITY_LABEL.get(r["priority_class"], ""),
        # 未复核 → False；复核判「确实该淘汰」（action=reject）后才由回灌脚本置真（步骤 4~5）
        "calibration_eligible_candidate": r["calibration_eligible_candidate"],
        "calibration_eligible": False,
        "review_verdict": "", "reviewer": "", "review_ts": "",
        "conflicts": "|".join(r.get("conflicts") or []),
        "hard_gate_unknown": r.get("hard_gate_unknown", False),
        "score_minus_line": round(float(r["gate_score"] or 0) - line, 2),
    }


os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
with open(a.out, "w", encoding="utf-8-sig", newline="") as f:
    w = csv.DictWriter(f, fieldnames=COLS)
    w.writeheader()
    for i, r in enumerate(picked, start=1):
        w.writerow(row_of(i, r))

with open(a.pool_out, "w", encoding="utf-8-sig", newline="") as f:
    w = csv.DictWriter(f, fieldnames=[c for c in COLS if c != "rank"])
    w.writeheader()
    for r in sorted(rows, key=lambda z: (PRIORITY_CLASSES.index(z["priority_class"]),
                                         -float(z["gate_score"] or 0))):
        d = row_of(0, r)
        d.pop("rank")
        w.writerow(d)


if a.write_db:
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from app.store import db
    from app.review import today_cn
    day = today_cn()
    batch = db.execute(
        """INSERT INTO spot_check_batch (company_id, job_id, config_version, check_date, drawn, want, pool_size, note)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
           ON CONFLICT (company_id, job_id, config_version, check_date)
           DO UPDATE SET drawn = EXCLUDED.drawn, want = EXCLUDED.want, pool_size = EXCLUDED.pool_size,
                         note = EXCLUDED.note RETURNING batch_id""",
        (cfg.company_id, cfg.job_id, cfg.config_version, day, len(picked), len(picked), len(rows),
         "由 tools/audit_sample.py 离线抽样后落库（配额制）"), returning=True)
    for r in picked:
        db.execute(
            """INSERT INTO spot_check_item (batch_id, application_id, resume_id, job_id, config_version,
                                            check_date, score, state, source, priority_class, calibration_eligible)
               VALUES (%s, %s, %s, %s, %s, %s, %s, 'pending', %s, %s, false)
               ON CONFLICT (batch_id, application_id) DO UPDATE
                 SET source = EXCLUDED.source, priority_class = EXCLUDED.priority_class""",
            (int(batch["batch_id"]), r["application_id"], r["resume_id"], cfg.job_id, cfg.config_version,
             day, r["gate_score"], r["source"], r["priority_class"]))
    print(f"[落库] 批次 {day} · batch={batch['batch_id']} · {len(picked)} 条待复核（eligible 一律 false）")


def dist(rs: list[dict]) -> dict:
    out: dict[str, int] = {}
    for r in rs:
        out[r["source"]] = out.get(r["source"], 0) + 1
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))


print(f"池子 {len(rows)} 份（sources={list(sources)}）· 抽 {len(picked)} 份 · 线 {line:g}")
print("\n=== 配额 vs 实际（source 口径；manual_review ≡ 降序类）===")
for k in SAMPLE_WEIGHTS:
    sz = info["class_size"].get(k, 0)
    got = info["class_picked"].get(k, 0)
    if sz == 0 and got == 0:
        continue
    share = got / len(picked) if picked else 0.0
    flag = "   ⚠️ 占比 >40%（又变回优先级列表了）" if share > MAX_BUCKET_SHARE else ""
    print(f"  {k:<20} 池 {sz:>4} → 抽 {got:>3}（权重 {SAMPLE_WEIGHTS[k]:>2}，占比 {share:>4.0%}，"
          f"抽样率 {got / sz if sz else 0:.0%}）{flag}")
print("\n=== 抽样样本的 priority_class 分布（审计口径：桶内按它优先）===")
pc_s, pc_p = {}, {}
for r in picked:
    pc_s[r["priority_class"]] = pc_s.get(r["priority_class"], 0) + 1
for r in rows:
    pc_p[r["priority_class"]] = pc_p.get(r["priority_class"], 0) + 1
for k in PRIORITY_CLASSES:
    print(f"  {PRIORITY_LABEL[k]:<9}({k:<18}){pc_s.get(k, 0):>5} 条   池中该类共 {pc_p.get(k, 0)}"
          + ("（本批为空 = 数据事实，不为凑分布造语料）" if pc_p.get(k, 0) == 0 else ""))
print("\n=== 全池 source 分布 ===")
for k, v in dist(rows).items():
    print(f"  {k:<20}{v:>5} 条")
print(f"\ncalibration_eligible_candidate = True 的条数：{sum(1 for r in picked if r['calibration_eligible_candidate'])}"
      f"（全池 {sum(1 for r in rows if r['calibration_eligible_candidate'])}）")
print(f"calibration_eligible = True 的条数：0（未复核；复核判『确实该淘汰』后由回灌脚本置真）")
print(f"\nCSV：{a.out} · 全池 {a.pool_out}")
