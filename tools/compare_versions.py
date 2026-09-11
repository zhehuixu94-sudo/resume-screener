#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""两个配置版本的跑批对照：v1 → v2 到底改了什么、谁被改动了、往哪个方向动。

用途：改了配置（阈值/权重/加分口径/prompt）重跑之后，先回答"这次改动带来什么影响"，
而不是只看新一版的绝对数字。对照维度：
  · 分布层：判定/处置/分数分位/冲突类型/加分命中
  · 逐份：分数变化、判定变化、处置变化，以及变化的原因（冲突类型差异）

用法：
    python tools/compare_versions.py --job game_dev --v1 1 --v2 2
    python tools/compare_versions.py --job game_dev --v1 1 --v2 2 --top 15
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

Q = """
SELECT r.file_name, a.config_version, e.verdict, e.decision, e.base_total, e.confidence,
       e.payload -> 'llm_compare' ->> 'verdict'   AS model_verdict,
       e.payload -> 'bonus_points'                AS bonus_points,
       e.payload -> 'conflicts'                   AS conflicts,
       e.payload -> 'questions'                   AS questions
  FROM application a
  JOIN resume r ON r.resume_id = a.resume_id
  JOIN evaluation e ON e.application_id = a.application_id
 WHERE a.job_id = %s AND a.config_version = %s
"""


def load(job_id: str, version: str) -> dict[str, dict]:
    out = {}
    for r in db.fetch_all(Q, (job_id, version)):
        r["conflict_types"] = tuple(sorted({c.get("type") for c in (r["conflicts"] or [])}))
        r["n_conflicts"] = len(r["conflicts"] or [])
        out[r["file_name"]] = r
    return out


def dist(rows: dict[str, dict], key: str) -> str:
    return dict(collections.Counter(r[key] for r in rows.values()).most_common())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--job", required=True)
    ap.add_argument("--v1", required=True)
    ap.add_argument("--v2", required=True)
    ap.add_argument("--top", type=int, default=10, help="逐份变化最多打印几条")
    a = ap.parse_args()

    A, B = load(a.job, a.v1), load(a.job, a.v2)
    common = sorted(set(A) & set(B))
    print(f"=== {a.job}：配置 v{a.v1}（{len(A)} 份） → v{a.v2}（{len(B)} 份），可对照 {len(common)} 份 ===\n")
    if not common:
        return 1

    print("┌ 分布对照")
    for label, key in (("规则层判定 verdict", "verdict"), ("模型整体判定", "model_verdict"), ("处置 decision", "decision")):
        print(f"│ {label}")
        print(f"│   v{a.v1}: {dist(A, key)}")
        print(f"│   v{a.v2}: {dist(B, key)}")

    for label, ver in ((f"v{a.v1}", A), (f"v{a.v2}", B)):
        s = sorted(float(r["base_total"]) for r in ver.values())
        print(f"│ 分数 {label}：中位 {st.median(s):.1f} · p25 {s[len(s)//4]:.1f} · p75 {s[len(s)*3//4]:.1f} · 范围 {s[0]:.1f}~{s[-1]:.1f}")
    for label, ver in ((f"v{a.v1}", A), (f"v{a.v2}", B)):
        cf = collections.Counter(t for r in ver.values() for t in r["conflict_types"])
        bonus = collections.Counter(float(r["bonus_points"] or 0) for r in ver.values())
        print(f"│ 冲突 {label}：{dict(cf.most_common())}")
        print(f"│ 加分 {label}：命中点数分布 {dict(sorted(bonus.items()))}")
    print("└\n")

    moved = []
    for f in common:
        score_d = float(B[f]["base_total"]) - float(A[f]["base_total"])
        if (A[f]["decision"] != B[f]["decision"]) or (A[f]["verdict"] != B[f]["verdict"]) or abs(score_d) > 1e-6:
            moved.append((f, A[f], B[f], score_d))
    print(f"发生变化的：{len(moved)}/{len(common)} 份（判定或处置或分数变了）")
    print(f"\n{'简历':<20} {'v1 分':>6} {'v2 分':>6} {'Δ分':>6}  判定 v1→v2           处置 v1→v2")
    def mv(r: dict) -> str:
        return r["model_verdict"] or "降级"

    for f, x, y, d in sorted(moved, key=lambda t: -abs(t[3]))[:a.top]:
        print(f"{f:<20} {float(x['base_total']):>6.1f} {float(y['base_total']):>6.1f} {d:>+6.1f}  "
              f"{mv(x):>4}→{mv(y):<4}          {x['decision']:>12}→{y['decision']:<12}")
    print(f"\n结论提示：分数普遍下移/上移说明规则分口径变了；处置变化集中在哪一类，就去看那类的冲突类型差异。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
