# -*- coding: utf-8 -*-
"""**跨线稳定性**：抽自动达标线附近的件（默认 70±2），每份独立跑 N 次，看会不会"今天过、明天不过"。

为什么单独做这个（用户提的，确实是证据盲区）：
现有的稳定性测量（tools/stability_check.py）用的样本分数是 73.99~80，全在**高分区**——
高分区的"极差 0.0"不能担保边界区。而拆掉隐性余量之后，70±1 那一段的任何抖动都会直接改变处置。

判据（与闸门同一套非对称口径）：
  · 危险侧：某次跑出淘汰/不达标而淘汰非严格多数 → 必须 0
  · 安全侧：多数票自动达标但个别次转人工 → 宽容
  · 另外单独报"跨线率"：N 次里有几次落在线的另一侧

用法：
  python tools/cross_line_stability.py --job config/job_hr_admin.json --window 2 --n 3 --limit 10 \
      --model Qwen/Qwen3.5-35B-A3B --record
"""
from __future__ import annotations

import argparse
import json
import os
import statistics as st
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # noqa: E402
from app.contracts import JobConfig                                  # noqa: E402
from app.contracts.resume_parse import ResumeParse                   # noqa: E402
from app.llm.client import SiliconFlowClient                         # noqa: E402
from app.pipeline.evaluate import evaluate                           # noqa: E402
from app.pipeline.stability import flip_metrics, record as stability_record   # noqa: E402
from app.store import db                                            # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--job", default="config/job_hr_admin.json")
ap.add_argument("--window", type=float, default=2.0, help="线两侧取样宽度（默认 70±2）")
ap.add_argument("--n", type=int, default=3, help="每份独立跑几次")
ap.add_argument("--limit", type=int, default=10, help="最多取几份（成本 ≈ 份数 × N × 9 次调用）")
ap.add_argument("--model", default=None)
ap.add_argument("--record", action="store_true", help="把结果写进 stability_check（闸门会读）")
a = ap.parse_args()

cfg = JobConfig.model_validate(json.load(open(a.job, encoding="utf-8")))
line = cfg.scoring.pass_threshold()
rows = db.fetch_all(
    """SELECT r.file_name, r.resume_id, e.gate_score FROM evaluation e
         JOIN application ap USING(application_id) JOIN resume r ON r.resume_id = e.resume_id
        WHERE ap.job_id = %s AND ap.config_version = %s AND e.gate_score BETWEEN %s AND %s
        ORDER BY abs(e.gate_score - %s)""",
    (cfg.job_id, cfg.config_version, line - a.window, line + a.window, line))
rows = rows[: a.limit]
print(f"线 {line:g} · 取 [{line - a.window:g}, {line + a.window:g}] 内 {len(rows)} 份 · 每份跑 {a.n} 次\n")
if not rows:
    print("区间内没有件——线附近是空的（这本身是个有用的结论：边界没人，抖动也就伤不到人）")
    raise SystemExit(0)

client = SiliconFlowClient(model=a.model, timeout=120, tpm=150000)
samples, per_file = [], []
for r in rows:
    prow = db.fetch_one("SELECT payload FROM resume_parse WHERE resume_id = %s", (r["resume_id"],))
    if not prow:
        print(f"  {r['file_name']}: 缺解析结果，跳过")
        continue
    parse = ResumeParse.model_validate(prow["payload"])
    scores, decs, verds = [], [], []
    for _ in range(a.n):
        ev, _notes = evaluate(parse, cfg, client, model=a.model)
        scores.append(float(ev.gate_score)); decs.append(ev.decision); verds.append(ev.verdict)
    crossed = sum(1 for s in scores if (s >= line) != (scores[0] >= line))
    per_file.append((r["file_name"], scores, decs, crossed))
    samples.append({"resume": r["file_name"], "decisions": decs, "verdicts": verds})
    print(f"  {r['file_name']:<22} 原分 {float(r['gate_score']):5.1f} · 各次 {['%.1f' % s for s in scores]}"
          f"（极差 {max(scores) - min(scores):.1f}）· 跨线 {crossed}/{a.n} · 各次处置 {decs}")

m = flip_metrics(samples)
gaps = [max(s) - min(s) for _, s, _, _ in per_file]
crossed_files = sum(1 for *_, c in per_file if c > 0)
print(f"\n汇总（{len(per_file)} 份 × {a.n} 次）")
print(f"  分数极差  中位 {st.median(gaps):.1f} · 最大 {max(gaps):.1f}")
print(f"  跨线件数  {crossed_files}/{len(per_file)}"
      f"（'N 次里落在线的另一侧'——这就是'今天过、明天不过'的直接证据）")
print(f"  危险侧翻转率 {m['danger_flip_rate']:.0%}（必须 0）· 安全侧翻转率 {m['safe_flip_rate']:.0%}")
print("  结论口径：跨线件 = 0 且危险侧 = 0 → 余量拆得干净；否则边界需要**显式的**缓冲（禁止再塞隐性余量）")

if a.record and per_file:
    best = [x for x in per_file if x[2] == "auto_pass" or True]
    cid = stability_record(cfg.job_id, cfg.config_version, n_samples=len(per_file),
                           agreement=round(1 - crossed_files / len(per_file), 2),
                           score_range=round(max(gaps), 2),
                           danger_flip_rate=m["danger_flip_rate"], safe_flip_rate=m["safe_flip_rate"],
                           detail={"kind": "cross_line", "line": line, "window": a.window,
                                   "repeat": a.n, "model": a.model, "metrics": m,
                                   "per_resume": {f: {"scores": s, "decisions": d, "crossed": c}
                                                  for f, s, d, c in per_file}})
    print(f"\n已记录（cross_line）：check_id={cid} —— 闸门会把它当作最新稳定性记录")
