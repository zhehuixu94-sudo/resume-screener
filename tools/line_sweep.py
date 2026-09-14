#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""达标线扫描：给出若干候选线，用**已落库的模型输出离线重算**处置分布（0 token）。

为什么需要它：达标线是一条政策线，不是算出来的真理。没有人工标注时，"线定多少"只能看
它把多少人送进哪三个口袋（自动达标 / 转人工 / 自动淘汰），以及每个口袋对应的代价：
  · 线太低 → 自动达标的人里混进不该面的（浪费面试官时间）；
  · 线太高 → 该面的人被卡进人工队列（等于没省 HR 的活），甚至被自动淘汰（漏放，最贵）。
有了锚点样本之后应当改用 anchor 标定（见 `tools/calibrate.py`）；在此之前用这个工具把
选择摊开给 HR 看，而不是替他拍一个数。

用法：
    python tools/line_sweep.py --job config/job_game_dev.json
    python tools/line_sweep.py --job config/job_game_dev.json --lines 26,28,30,32,35,38,40 --band 5
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.contracts import JobConfig                                   # noqa: E402
from app.pipeline.replay import decide_with_line, load_rows           # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--job", required=True)
    ap.add_argument("--lines", default="24,26,28,30,32,34,36,38,40,45,50")
    ap.add_argument("--band", type=float, default=5.0)
    ap.add_argument("--ignore-gate", action="store_true",
                    help="绕过稳定性闸门看'纯口径效果'——只在验证算法时用；线上判定有这道闸门")
    ap.add_argument("--from-version", default=None,
                    help="用哪个 config_version 的落库模型输出扫描（默认当前配置版本）")
    a = ap.parse_args()

    cfg = JobConfig.model_validate(json.load(open(a.job, encoding="utf-8")))
    if a.ignore_gate:
        print("⚠️  已绕过稳定性闸门（--ignore-gate）：下面是'纯口径效果'，线上判定会拦住 auto_reject，"
              "直到稳定性测量达标（tools/stability_check.py --record）\n")
    rows = load_rows(cfg, a.from_version)
    if not rows:
        print(f"{cfg.job_id}@{(a.from_version or cfg.config_version)} 没有可用的评估结果（先跑一遍 run）")
        return 1

    scores = sorted(float(r["old"].gate_score) for r in rows)
    n = len(scores)
    import statistics as st
    print(f"=== 达标线扫描：{cfg.job_title}（{cfg.job_id} v{cfg.config_version} 配置 × "
          f"v{a.from_version or cfg.config_version} 模型输出）× {n} 份 ===")
    print(f"规则分：中位 {st.median(scores):.1f} · p25 {scores[n // 4]:.1f} · p75 {scores[n * 3 // 4]:.1f} "
          f"· p90 {scores[int(n * 0.9)]:.1f} · 范围 {scores[0]:.1f}~{scores[-1]:.1f}")
    print(f"当前线 {cfg.scoring.pass_line}（{cfg.scoring.pass_line_source}）±{cfg.scoring.band_width}\n")

    lines = [float(x) for x in a.lines.split(",") if x.strip()]
    print(f"{'线':>5} {'自动达标':>8} {'转人工':>7} {'自动淘汰':>8}   占比（达标/人工/淘汰）")
    print("-" * 62)
    rows_by_line = {}
    for line in lines:
        decisions = [decide_with_line(cfg, r, line, a.band, gate=not a.ignore_gate) for r in rows]
        ap_ = sum(1 for d in decisions if d == "auto_pass")
        mr = sum(1 for d in decisions if d == "manual_review")
        ar = sum(1 for d in decisions if d == "auto_reject")
        marks = f"{ap_ / n * 100:>4.0f}% /{mr / n * 100:>4.0f}% /{ar / n * 100:>4.0f}%"
        cur = " ←当前" if abs(line - cfg.scoring.pass_line) < 1e-6 else ""
        print(f"{line:>5.0f} {ap_:>8} {mr:>7} {ar:>8}   {marks}{cur}")
        rows_by_line[line] = decisions

    # 政策开关对比：同一个线下，"边缘+低分"允不允许自动淘汰
    print("\n【策略对比】reject_border_when_low（模型判边缘且分数低于线−band 时是否自动淘汰）：")
    print(f"{'线':>5} {'关：达标/人工/淘汰':>26} {'开：达标/人工/淘汰':>26}")
    for line in lines:
        off = [decide_with_line(cfg, r, line, a.band, False, gate=not a.ignore_gate) for r in rows]
        on = [decide_with_line(cfg, r, line, a.band, True, gate=not a.ignore_gate) for r in rows]
        f = lambda ds: (sum(1 for d in ds if d == "auto_pass"), sum(1 for d in ds if d == "manual_review"),
                        sum(1 for d in ds if d == "auto_reject"))
        print(f"{line:>5.0f} {str(f(off)):>26} {str(f(on)):>26}")

    print("\n提示：这条线的取舍是「自动达标漏掉多少该面的人」对「转人工给 HR 留多少活」。")
    print("     没有人工标注时别追求'最优点'，选一个能把人工队列压到可承受量的线，再用锚点标定替换它。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
