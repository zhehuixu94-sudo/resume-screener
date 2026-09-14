#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""等分制口径验证：每项基础分 = 及格分 ÷ 及格项目数，项得分 = 基础分 × 相似度 × 难度。

要回答的问题（HR 提的）：
  1. 这套算法跑真实数据，分数分布长什么样？
  2. 怎么定线才能达到 ≥20% 的自动淘汰率？代价是什么？
  3. 现在被"转人工"困住的那一大块，按等分制会怎么动？

全部离线（0 token）：模型输出用库里已有的（v3 全量 100 份），只有算分口径与线在变。

用法：
    python tools/equal_split_sweep.py --job config/job_game_dev.json
    python tools/equal_split_sweep.py --job config/job_game_dev.json --budget 60 --band 5 \
        --lines 20,22,24,26,28,30,32,35
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import statistics as st
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.contracts import JobConfig                                            # noqa: E402
from app.pipeline.equal_split import basic_items_of, equal_split_outcome       # noqa: E402
from app.pipeline.replay import decide_with_equal_split, load_rows             # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--job", required=True)
    ap.add_argument("--ignore-gate", action="store_true",
                    help="绕过稳定性闸门看'纯口径效果'——只在验证算法时用；线上判定有这道闸门")
    ap.add_argument("--from-version", default=None, help="用哪个版本的落库模型输出（默认当前）")
    ap.add_argument("--label-only", action="store_true",
                    help="忽略模型给的连续值，只用 强/中/弱·高/中/低 标签换算（同数据的对照口径）")
    ap.add_argument("--budget", type=float, default=60.0, help="及格总分（默认 60）")
    ap.add_argument("--band", type=float, default=5.0)
    ap.add_argument("--lines", default="", help="候选线（逗号分隔）；留空 = 自动覆盖分布范围")
    ap.add_argument("--reject-border", action="store_true",
                    help="同时试 reject_border_when_low=开（模型判边缘但分数低也允许淘汰）")
    ap.add_argument("--compare-line", type=float, default=None,
                    help="指定一条线，逐份对照现口径（含逐项零分情况）；配 --reject-border 一起看")
    ap.add_argument("--switch", dest="switch_on", action="store_true",
                    help="逐份对照时把 reject_border_when_low 打开")
    a = ap.parse_args()

    cfg = JobConfig.model_validate(json.load(open(a.job, encoding="utf-8")))
    if a.ignore_gate:
        print("⚠️  已绕过稳定性闸门（--ignore-gate）：下面是'纯口径效果'，线上判定会拦住 auto_reject，"
              "直到稳定性测量达标（tools/stability_check.py --record）\n")
    rows = load_rows(cfg, a.from_version)
    if not rows:
        print("没有可用的评估结果（先跑一遍 run）")
        return 1
    basics = basic_items_of(cfg)
    n = len(basics)
    print(f"=== 等分制验证：{cfg.job_title}（{cfg.job_id}）× {len(rows)} 份 ===")
    qb = "质量基准 相似度强×难度中=1（难度中=1.0/低=0.5/高=1.5，中间线性取值）"
    print(f"及格项 {n} 个（{', '.join(basics)}）→ 每项基础分 {a.budget / n:.2f} 分；"
          f"项得分 = 基础分 × 相似度 × 难度（高 1.0 / 中 0.667 / 低 0.333；{qb}）")
    print(f"上限（每项都强+高难度）= {a.budget * 1.5:.0f} 分；刚好及格 = 每项强×中；现口径的达标线是 {cfg.scoring.pass_line}（满分 100）\n")

    # 1) 等分制分布
    totals = []
    per_dim = collections.defaultdict(list)
    detail = {}
    for r in rows:
        out = equal_split_outcome(cfg, r["labeled"], budget=a.budget, basic_items=basics,
)
        totals.append(out.base_total)
        detail[r["file"]] = out
        for d in out.dimensions:
            per_dim[d.id].append(d.points)
    s = sorted(totals)
    q = lambda p: s[max(0, min(len(s) - 1, int(p * len(s))))]        # noqa: E731
    print(f"【分数分布】中位 {st.median(s):.1f} · p10 {q(.10):.1f} · p25 {q(.25):.1f} · "
          f"p75 {q(.75):.1f} · p90 {q(.90):.1f} · 范围 {s[0]:.1f}~{s[-1]:.1f} · "
          f"标准差 {st.pstdev(s):.2f} · 均值 {st.fmean(s):.1f}")
    bins = collections.Counter(int(x // 10) * 10 for x in s)
    for lo in sorted(bins):
        print(f"  {lo:>2}~{lo + 9:<3} {bins[lo]:>3} 份 {'█' * bins[lo]}")
    print("\n【各及格项的平均得分】")
    for d in basics:
        pts = per_dim[d]
        print(f"  {d:<14} 平均 {st.fmean(pts):>5.1f} / 满配 {a.budget / n:.1f}"
              f" · 零分 {sum(1 for x in pts if x == 0)} 份")

    # 2) 扫描线：淘汰率能不能到 20%
    lines = [float(x) for x in a.lines.split(",") if x.strip()] if a.lines else \
        [round(q(p), 1) for p in (0.05, 0.10, 0.15, 0.20, 0.25, 0.30, 0.40, 0.50, 0.60, 0.70, 0.80)]
    print(f"\n【按等分制重算处置】线：该岗位候选（band={a.band}）；"
          f"淘汰率 ≥20% = 至少 {max(1, round(len(rows) * 0.2))} 份")
    print(f"{'线':>5} {'自动达标':>8} {'转人工':>7} {'自动淘汰':>8} {'淘汰率':>7}   说明")
    print("-" * 74)
    best = None
    for line in lines:
        dec = [decide_with_equal_split(cfg, r, line, a.band, budget=a.budget, basic_items=basics,
                                       gate=not a.ignore_gate) for r in rows]
        c = collections.Counter(dec)
        rate = c["auto_reject"] / len(rows) * 100
        ok = "✅ ≥20%" if rate >= 20 else ""
        print(f"{line:>5.0f} {c['auto_pass']:>8} {c['manual_review']:>7} {c['auto_reject']:>8} "
              f"{rate:>6.1f}%   {ok}")
        if rate >= 20 and (best is None or rate < best[2]):
            best = (line, c, rate)

    # 3) 与现口径逐份对照（现口径转人工的那一大块会怎么动）
    def compare(line: float, *, switch: bool, headline: str) -> None:
        moved = []
        for r in rows:
            now = r["old"].decision
            new = decide_with_equal_split(cfg, r, line, a.band, budget=a.budget, basic_items=basics,
                                                                                    reject_border_when_low=True if switch else None,
                                          gate=not a.ignore_gate)
            if now != new:
                moved.append((r["file"], now, new, float(r["old"].base_total),
                              detail[r["file"]].base_total))
        print(f"\n【逐份对照】{headline}")
        print(f"  处置发生变化 {len(moved)}/{len(rows)} 份（模型判'不达标'的只有 "
              f"{sum(1 for r in rows if r['old'].llm_compare.verdict == '不达标')} 份——"
              f"这就是不开开关时淘汰率封顶的原因）：")
        kinds = collections.Counter(f"{x}→{y}" for _, x, y, _, _ in moved)
        for k, v in kinds.most_common():
            print(f"    {k:<28} {v} 份")
        newly_rejected = [m for m in moved if m[2] == "auto_reject"]
        if newly_rejected:
            zero = collections.Counter()
            for f, *_ in newly_rejected:
                row = next(r for r in rows if r["file"] == f)
                for d in equal_split_outcome(cfg, row["labeled"], budget=a.budget, basic_items=basics,
           ).dimensions:
                    if d.points == 0:
                        zero[d.id] += 1
            print(f"  新增自动淘汰 {len(newly_rejected)} 份；其中'某及格项零证据'的分布：{dict(zero)}")
            print(f"  新增淘汰件的等分制分："
                  f"{sorted(round(m[4], 1) for m in newly_rejected)}")
        print(f"  {'简历':<18} {'现口径':<16} {'现分':>6} {'等分制分':>8}")
        for f, x, y, old_s, new_s in sorted(moved, key=lambda t: t[4])[:12]:
            print(f"  {f:<18} {x + '→' + y:<16} {old_s:>6.1f} {new_s:>8.1f}")

    if a.compare_line is not None:
        compare(a.compare_line, switch=a.switch_on,
                headline=f"指定线 {a.compare_line:.0f}"
                         f"{'（开关：开）' if a.switch_on else '（开关：关）'}")
    elif best:
        compare(best[0], switch=False, headline=f"取线 {best[0]:.0f}（≥20% 里最保守的一条，开关未开）")

    if a.reject_border:
        print("\n【策略开关】reject_border_when_low=开（模型判「边缘」但分数 < 线−band 时也允许自动淘汰）")
        for line in lines[:6]:
            dec = [decide_with_equal_split(cfg, r, line, a.band, budget=a.budget, basic_items=basics,
                                                                                      reject_border_when_low=True, gate=not a.ignore_gate) for r in rows]
            c = collections.Counter(dec)
            print(f"  线 {line:>5.0f} → 达标 {c['auto_pass']} / 转人工 {c['manual_review']} / "
                  f"淘汰 {c['auto_reject']}（淘汰率 {c['auto_reject'] / len(rows) * 100:.1f}%）")

    print("\n注意：本页只有'把人分到哪个口袋'，没有'分得对不对'——准不准要靠人工标注/对照语料。"
          "\n      淘汰率上去了 ≠ 该淘汰的人被淘汰了；这就是校准与抽检要补的洞。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
