#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""按档位评估：用带标准答案的对照语料算漏放率、误放率，并给出"该定哪条线"。

为什么这个才是真正的评测：随机语料只能看分布（"分数是不是挤在一起"），
分层语料（明显合格 / 边缘合格 / 明显不匹配 / 注水型）才有分母算：
  · 漏放率 = 明显合格档被判 auto_reject 的比例  ← 最贵的错，HR 说 ≤1%
  · 误放率 = 不匹配/注水档被判 auto_pass 的比例
  · 该定哪条线 = 在漏放约束内，把淘汰率/人工负担调到目标的那条线
另外给出擦线带规模（决定抽检要抽多少份）。

用法：
  python tools/tier_eval.py --job config/job_hr_admin.json --corpus local/samples_admin \
      [--equal-split] [--lines 45,50,55,60]
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import statistics as st
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.contracts import JobConfig                                    # noqa: E402
from app.pipeline.equal_split import basic_items_of, equal_split_outcome   # noqa: E402
from app.pipeline.replay import decide_with_equal_split, decide_with_line, load_rows  # noqa: E402
from app.pipeline.stats import leak_upper_bound                        # noqa: E402
from app.store import db                                               # noqa: E402

GOOD, EDGE, BAD, FLUFF = 1, 2, 3, 4
CN = {GOOD: "明显合格", EDGE: "边缘合格", BAD: "明显不匹配", FLUFF: "注水型"}


def fetch(cfg: JobConfig, corpus: str) -> list[dict]:
    manifest = json.load(open(os.path.join(corpus, "manifest.json"), encoding="utf-8"))
    tier = {m["file"]: m["tier"] for m in manifest}
    rows = db.fetch_all(
        """SELECT r.file_name, e.verdict, e.decision, e.gate_score, e.base_total, e.confidence
             FROM evaluation e JOIN application a USING(application_id)
             JOIN resume r ON r.resume_id = e.resume_id
            WHERE a.job_id = %s AND a.config_version = %s""",
        (cfg.job_id, cfg.config_version))
    out = []
    for r in rows:
        if r["file_name"] in tier:
            r["tier"] = tier[r["file_name"]]
            out.append(r)
    return out


def dist(rows: list[dict], key: str) -> dict:
    return dict(collections.Counter(r[key] for r in rows).most_common())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--job", required=True)
    ap.add_argument("--corpus", required=True, help="带 manifest.json（含 tier）的语料目录")
    ap.add_argument("--equal-split", action="store_true", help="同时按等分制口径重算")
    ap.add_argument("--lines", default="45,50,55,60,65")
    ap.add_argument("--band", type=float, default=5.0)
    a = ap.parse_args()

    cfg = JobConfig.model_validate(json.load(open(a.job, encoding="utf-8")))
    rows = fetch(cfg, a.corpus)
    if not rows:
        print("没有可评估的结果（先跑 run）")
        return 1
    n = len(rows)
    print(f"=== 按档位评估：{cfg.job_title}（{cfg.job_id} v{cfg.config_version}）× {n} 份 ===")
    print(f"处置分布：{dist(rows, 'decision')}\n")

    print(f"{'档位':<10}{'份数':>5}{'达标':>6}{'转人工':>8}{'淘汰':>6}{'淘汰率':>8}  分数中位/p25~p75")
    for t in (GOOD, EDGE, BAD, FLUFF):
        sub = [r for r in rows if r["tier"] == t]
        if not sub:
            continue
        c = collections.Counter(r["decision"] for r in sub)
        s = sorted(float(r["gate_score"]) for r in sub)
        q = lambda p: s[max(0, min(len(s) - 1, int(p * len(s))))]      # noqa: E731
        print(f"{CN[t]:<10}{len(sub):>5}{c['auto_pass']:>6}{c['manual_review']:>8}{c['auto_reject']:>6}"
              f"{c['auto_reject'] / len(sub) * 100:>7.1f}%  {st.median(s):.1f} / {q(.25):.1f}~{q(.75):.1f}")

    good = [r for r in rows if r["tier"] == GOOD]
    bad_fluff = [r for r in rows if r["tier"] in (BAD, FLUFF)]
    leaks = [r for r in good if r["decision"] == "auto_reject"]
    print(f"\n【现口径 · 线 {cfg.scoring.pass_line}】")
    print(f"  漏放（明显合格被自动淘汰）：{len(leaks)}/{len(good)} = {len(leaks) / max(1, len(good)) * 100:.1f}%"
          f" · 95% 置信上界 {leak_upper_bound(len(leaks), len(good)) * 100:.1f}%"
          f"{'  ⚠️ 超过 1%' if len(leaks) / max(1, len(good)) > 0.01 else ''}")
    wrong = [r for r in bad_fluff if r["decision"] == "auto_pass"]
    print(f"  误放（不匹配/注水被自动达标）：{len(wrong)}/{len(bad_fluff)} = "
          f"{len(wrong) / max(1, len(bad_fluff)) * 100:.1f}%")

    # 等分制口径下的线扫描：找"漏放=0 且淘汰率接近目标"的线
    if a.equal_split:
        basics = basic_items_of(cfg)
        replay = {r["file"]: r for r in load_rows(cfg, cfg.config_version)}   # load_rows 用 "file" 键
        print(f"\n【等分制口径扫描】（{len(basics)} 个及格项，每项 {60 / len(basics):.2f} 分，band {a.band}）")
        print(f"{'线':>5}{'淘汰率':>8}{'漏放':>6}{'漏放上界':>10}{'误放':>6}{'擦线带':>8}  结论")
        for line in [float(x) for x in a.lines.split(",") if x.strip()]:
            dec = {}
            for r in rows:
                row = replay.get(r["file_name"])
                if not row:
                    continue
                dec[r["file_name"]] = decide_with_equal_split(
                    cfg, row, line, a.band, budget=60.0, basic_items=basics,
                    reject_border_when_low=True, gate=False)
            if not dec:
                continue
            c = collections.Counter(dec.values())
            lk = sum(1 for r in good if dec.get(r["file_name"]) == "auto_reject")
            wr = sum(1 for r in bad_fluff if dec.get(r["file_name"]) == "auto_pass")
            # 擦线带 = 分数落在 [线-band-10, 线-band) 的份数（抽检要重点看的那一段）
            scores = {}
            for r in rows:
                row = replay.get(r["file_name"])
                if row:
                    scores[r["file_name"]] = equal_split_outcome(
                        cfg, row["labeled"], budget=60.0, basic_items=basics).base_total
            lo, hi = line - a.band - 10, line - a.band
            near = sum(1 for v in scores.values() if lo <= v < hi)
            rate = c["auto_reject"] / len(dec) * 100
            ub = leak_upper_bound(lk, len(good)) * 100
            verdict = "✅ 漏放≤1%" if ub <= 1.0 else ("⚠️ 漏放超 1%" if lk else "样本不足以证明 1%")
            print(f"{line:>5.0f}{rate:>7.0f}%{lk:>6}{ub:>9.1f}%{wr:>6}{near:>8}  {verdict}")
        print("\n  擦线带 = 分数在 [线−band−10, 线−band) 的份数：抽检建议按 50% 抽它、其余带 3~10%")

    print("\n提醒：档位是**构造语料时给的**，不是人工复核过的真实标注；它能测出明显的漏放/误放，"
          "\n      但替代不了真实简历上的人工抽检。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
