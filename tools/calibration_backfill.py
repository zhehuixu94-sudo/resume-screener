# -*- coding: utf-8 -*-
"""步骤 5 · 校准池回灌（默认 dry-run）：把**冻结样本**上的人工复核结论算成"能不能进校准池"。

唯一判据在 `app.review.calibration_decision()`：
  只有 `source=auto_reject` + `review_verdict=reject`(确实该淘汰) + `reviewer`/`review_ts` 非空 → 置真。
  非 auto_reject 的行**即使 verdict=reject 也一律拒绝置真**（转人工件的结论不是"系统漏放"，
  混进校准池会把线拉歪）。

用法：
  python tools/calibration_backfill.py                                  # dry-run（默认，只算不写）
  python tools/calibration_backfill.py --reviews out/xx.csv             # 用外部结论文件演练（不碰库）
  python tools/calibration_backfill.py --apply                          # 真写库（只置真合格行；顺带把不合格行复位为 false）
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # noqa: E402
from app.contracts import JobConfig                                        # noqa: E402
from app.review import (DEFAULT_SAMPLE_FILE, SOURCE_AUTO_REJECT,           # noqa: E402
                        calibration_decision, load_frozen_sample)
from app.store import db                                                  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--job", default="config/job_hr_admin.json")
ap.add_argument("--sample", default=DEFAULT_SAMPLE_FILE, help="冻结的抽检样本 CSV")
ap.add_argument("--reviews", default="", help="外部复核结论 CSV（application_id,review_verdict,reviewer,review_ts）")
ap.add_argument("--out", default="out/calibration_pool_v1.csv")
ap.add_argument("--apply", action="store_true", help="真写库（默认 dry-run）")
a = ap.parse_args()

cfg = JobConfig.model_validate(json.load(open(a.job, encoding="utf-8")))
sample, sample_fp = load_frozen_sample(a.sample)

# 人工结论来源：① 外部文件（演练/离线）② 库里的 spot_check_item 三列（正式路径）
external: dict[int, dict] = {}
if a.reviews:
    with open(a.reviews, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            external[int(r["application_id"])] = r

db_rows = {}
if not a.reviews:
    ids = list(sample)
    rows = db.fetch_all(
        """SELECT application_id, source, review_verdict, reviewer, review_ts, calibration_eligible
             FROM spot_check_item WHERE job_id = %s AND application_id = ANY(%s)
            ORDER BY application_id""", (cfg.job_id, ids))
    for r in rows:
        db_rows[int(r["application_id"])] = r

out_rows, reasons = [], Counter()
n_eligible = n_refused = n_pending = 0
for aid, s in sorted(sample.items(), key=lambda kv: kv[1]["file_name"]):
    src = (external.get(aid, {}).get("source") if a.reviews else None) \
        or (db_rows.get(aid) or {}).get("source") or s["source"]
    verdict = (external.get(aid, {}).get("review_verdict") if a.reviews
               else (db_rows.get(aid) or {}).get("review_verdict") or "") or ""
    reviewer = (external.get(aid, {}).get("reviewer") if a.reviews
                else (db_rows.get(aid) or {}).get("reviewer") or "") or ""
    ts = (external.get(aid, {}).get("review_ts") if a.reviews
          else (db_rows.get(aid) or {}).get("review_ts")) or ""
    ok, why = calibration_decision(src, verdict, reviewer, ts)
    reasons[why] += 1
    if ok:
        n_eligible += 1
    elif verdict:
        n_refused += 1
    else:
        n_pending += 1
    out_rows.append({
        "application_id": aid, "file_name": s["file_name"], "gate_score": s["gate_score"],
        "system_decision": s["system_decision"], "source": src, "priority_class": s["priority_class"],
        "review_verdict": verdict, "reviewer": reviewer, "review_ts": ts,
        "calibration_eligible_now": ok, "reason": why,
        "already_true_in_db": bool((db_rows.get(aid) or {}).get("calibration_eligible")),
    })

os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
with open(a.out, "w", encoding="utf-8-sig", newline="") as f:
    w = csv.DictWriter(f, fieldnames=list(out_rows[0].keys()))
    w.writeheader()
    w.writerows(out_rows)

applied = 0
if a.apply and not a.reviews:
    for r in out_rows:
        if r["calibration_eligible_now"] and not r["already_true_in_db"]:
            db.execute("""UPDATE spot_check_item SET calibration_eligible = true
                           WHERE job_id = %s AND application_id = %s""", (cfg.job_id, r["application_id"]))
            applied += 1
        elif not r["calibration_eligible_now"] and r["already_true_in_db"]:
            # 自愈：任何"不该置真却为真"的行一律复位（非 auto_reject 绝不允许留在校准池）
            db.execute("""UPDATE spot_check_item SET calibration_eligible = false
                           WHERE job_id = %s AND application_id = %s""", (cfg.job_id, r["application_id"]))
            applied += 1

print(f"样本 {len(sample)} 份（{a.sample} · 指纹 {sample_fp}）· "
      f"结论来源 {'外部文件 ' + os.path.basename(a.reviews) if a.reviews else '库内 spot_check_item 三列'}")
print(f"  {'[dry-run] 未写库' if not a.apply else f'[apply] 已更新 {applied} 行'}")
print(f"\n应置真（进校准池）：{n_eligible}")
print(f"拒绝置真：{n_refused}   未复核/拿不准（不置真）：{n_pending}")
print("\n按 source 的置真分布：")
by_src = Counter((r["source"], r["calibration_eligible_now"]) for r in out_rows)
for src in ("auto_reject", "borderline", "model_conflict", "manual_review", "hard_gate_unknown", "auto_pass"):
    yes = by_src.get((src, True), 0)
    no = by_src.get((src, False), 0)
    if yes or no:
        print(f"  {src:<20} 置真 {yes:>3} · 不置真 {no:>3}")
print("\n排除原因分布：")
for why, n in reasons.most_common():
    print(f"  {n:>3} 条 · {why}")
leaks = [r for r in out_rows if r["source"] == SOURCE_AUTO_REJECT and r["review_verdict"] == "keep"]
print(f"\n漏放信号（auto_reject + keep）：{len(leaks)} 条"
      + ("（按用户口径不进校准池，单列统计）" if leaks else "")
      + ("".join(f"\n  {r['file_name']} 分 {r['gate_score']}" for r in leaks)))
print(f"\nCSV：{a.out}")
