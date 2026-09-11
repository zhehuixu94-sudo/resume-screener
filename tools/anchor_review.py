# -*- coding: utf-8 -*-
"""按岗位导出逐份明细（硬门槛结论 / 维度分 / 匹配度 / 处置），供锚点标定用。

用法：python tools/anchor_review.py config/job_jushi_teacher.json
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.contracts import Evaluation, JobConfig  # noqa: E402
from app.store import Repository, db  # noqa: E402


def main() -> int:
    path = sys.argv[1] if len(sys.argv) > 1 else "config/job_jushi_teacher.json"
    cfg = JobConfig.model_validate(json.load(open(path, encoding="utf-8")))
    repo = Repository(cfg.company_id)
    rows = repo.list_evaluations(cfg)
    print(f"岗位：{cfg.job_title}（{cfg.job_id}@{cfg.config_version}）｜ 达标线 {cfg.scoring.pass_line}"
          f"（{cfg.scoring.pass_line_source}）±{cfg.scoring.band_width}")
    print(f"{'简历':<26} {'判定':<5} {'处置':<14} {'分数':>6}  硬门槛(edu/major/cet4)  匹配度")
    detail = []
    for r in rows:
        if not r.get("payload"):
            print(f"{r['file_name']:<26} （未完成：{r.get('state')}）")
            continue
        ev = Evaluation.model_validate(r["payload"])
        hf = {h.id: h.result for h in ev.hard_filters}
        fit = f"{ev.fit.verdict}({ev.fit.fit_ratio})" if ev.fit else "—"
        print(f"{r['file_name']:<26} {ev.verdict:<5} {ev.decision:<14} {ev.base_total:>6.1f}  "
              f"{hf.get('edu','-'):<8}{hf.get('major','-'):<8}{hf.get('cet4','-'):<8}  {fit}")
        detail.append((r["file_name"], ev))

    print("\n维度分明细：")
    for name, ev in detail:
        dims = " | ".join(f"{d.name[:8]} {d.points:.1f}" for d in ev.dimensions)
        print(f"  {name:<26} {dims}")
        if ev.llm_compare and ev.llm_compare.veto_triggered:
            print(f"     门槛说明：{ev.llm_compare.veto_triggered}")

    print("\n适合当锚点的候选（按分数排序）：")
    for name, ev in sorted(detail, key=lambda x: -x[1].base_total):
        tag = "合格候选" if ev.decision == "auto_pass" else ("不合格候选" if ev.decision == "auto_reject" else "待定")
        print(f"  {ev.base_total:>6.1f}  {name:<26} {tag}  {ev.decision_reason[:70]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
