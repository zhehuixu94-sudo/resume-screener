#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""任务 4 实验：把"标注噪声"和"解析噪声"分开，检验两种稳定化手段。

做法：解析一次（固定条目），标注 N 次。然后比较四种组合的分数极差：
  ① 单次标注 + best（现状）
  ② 单次标注 + top2_mean
  ③ 多次标注取中位数 + 维度多数投票 + best
  ④ 多次标注取中位数 + 维度多数投票 + top2_mean
③④ 的极差要用"分组中位数"来测：把 N 次切成 N/group 组，每组算一次中位数分数，组间极差才是它的稳定性。

用法：
  python tools/label_stability.py --job config/job_jushi_teacher.json \\
      --resumes "示例候选人简历3.0.docx" resume_001.txt --repeat 9 --group 3
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
from collections import Counter
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.contracts import JobConfig  # noqa: E402
from app.llm.client import SiliconFlowClient  # noqa: E402
from app.pipeline.evaluate import label_items  # noqa: E402
from app.pipeline.parse import parse_resume  # noqa: E402
from app.pipeline.rules import rule_layer  # noqa: E402
from tools.sample_paths import resume_search_dirs  # noqa: E402
from app.pipeline.rules import RELEVANCE_W, DIFFICULTY_W  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ACCEPT_GAP = 5.0

MODES = ("single_best", "single_top2", "median_best", "median_top2")


def raw_of(item: dict) -> float:
    return round(RELEVANCE_W.get(item.get("relevance"), 0.0) * DIFFICULTY_W.get(item.get("difficulty"), 1), 2)


def combine(labeled_runs: list[list[dict]], parse, cfg: JobConfig) -> list[dict]:
    """多次标注合成一次：维度取多数票，相关性/难度取 raw 中位数最接近的那一轮的标签。"""
    by_item: dict[str, list[dict]] = {}
    for labeled in labeled_runs:
        for it in labeled:
            by_item.setdefault(it["item_id"], []).append(it)

    merged = []
    for item_id, runs in by_item.items():
        base = dict(runs[0])
        dims = [r.get("dimension") for r in runs if r.get("dimension")]
        if dims:
            counts = Counter(dims)
            top = counts.most_common()
            base["dimension"] = sorted([d for d, c in top if c == top[0][1]])[0]      # 并列时取字典序最小，保证确定性
        else:
            base["dimension"] = None
        raws = sorted(raw_of(r) for r in runs)
        median = statistics.median(raws)
        best_run = min(runs, key=lambda r: (abs(raw_of(r) - median), raw_of(r)))
        base["relevance"], base["difficulty"] = best_run.get("relevance"), best_run.get("difficulty")
        base["_runs"] = len(runs)
        merged.append(base)
    return merged


def score(labeled: list[dict], cfg: JobConfig, mode: str) -> dict:
    agg = "top2_mean" if mode.endswith("top2") else "best"
    rules = rule_layer(cfg, labeled, aggregation=agg)
    return {"total": rules.base_total, "dims": {d.id: d.points for d in rules.dimensions},
            "aggregation": agg}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--job", required=True)
    ap.add_argument("--resumes", nargs="+", required=True)
    ap.add_argument("--repeat", type=int, default=9, help="标注次数（建议 3 的倍数）")
    ap.add_argument("--group", type=int, default=3, help="中位数法的分组大小（每组算一次中位数分数）")
    ap.add_argument("--model", default=os.environ.get("RESUME_MODEL") or "Qwen/Qwen3.5-35B-A3B")
    ap.add_argument("--search", nargs="*", default=resume_search_dirs(),
                    help="按文件名找简历的目录（默认仓库内 local/samples* + 环境变量 RESUME_SEARCH_DIRS）")
    ap.add_argument("--out", default="out/stability")
    a = ap.parse_args()

    cfg = JobConfig.model_validate(json.load(open(os.path.join(ROOT, a.job) if not os.path.exists(a.job) else a.job,
                                                 encoding="utf-8")))
    client = SiliconFlowClient(model=a.model)
    if not client.probe():
        client.model = "Qwen/Qwen3.5-35B-A3B"

    def resolve(name: str) -> str:
        if os.path.exists(name):
            return name
        for d in a.search:
            for cand in (os.path.join(d, name), os.path.join(ROOT, d, name)):
                if os.path.exists(cand):
                    return cand
        raise SystemExit(f"找不到简历：{name}")

    results = []
    for name in a.resumes:
        path = resolve(name)
        print(f"\n=== {name}：解析 1 次 + 标注 {a.repeat} 次 ===")
        outcome = parse_resume(path, cfg, client, model=a.model)
        if outcome.parse is None:
            print("  解析失败，跳过")
            continue
        parse = outcome.parse
        print(f"  条目 {len(parse.items)} 条，证据锚定 {parse.stats.evidence_anchored}/{parse.stats.evidence_total}")
        runs = []
        for i in range(1, a.repeat + 1):
            labeled, _meta, _stats = label_items(parse, cfg, client, model=a.model, repeats=1)
            runs.append(labeled)
            if i <= 3 or i == a.repeat:
                print(f"  标注第 {i} 次：{len(labeled)} 条")

        singles = [score(r, cfg, "single_best") for r in runs]
        singles_top2 = [score(r, cfg, "single_top2") for r in runs]
        groups = [runs[i:i + a.group] for i in range(0, len(runs) - a.group + 1, a.group)]
        medians_best, medians_top2 = [], []
        dim_votes = []
        for gi, g in enumerate(groups, start=1):
            merged = combine(g, parse, cfg)
            b = score(merged, cfg, "median_best")
            t = score(merged, cfg, "median_top2")
            medians_best.append(b["total"])
            medians_top2.append(t["total"])
            dim_votes.append({it["item_id"]: it["dimension"] for it in merged})
            print(f"  组 {gi} 中位数：best {b['total']} ｜ top2 {t['total']}")

        # 维度归属的稳定性：同一份解析多次标注，维度投票的一致率
        dim_agree = []
        for item_id in [it["item_id"] for it in runs[0]]:
            dims = [next((x.get("dimension") for x in r if x["item_id"] == item_id), None) for r in runs]
            dims = [d for d in dims if d]
            if dims:
                dim_agree.append(max(Counter(dims).values()) / len(dims))

        def gap(xs):
            return round(max(xs) - min(xs), 1)

        entry = {
            "resume": name, "items": len(parse.items), "label_runs": len(runs), "groups": len(groups),
            "single_best": {"totals": [s["total"] for s in singles], "gap": gap([s["total"] for s in singles])},
            "single_top2": {"totals": [s["total"] for s in singles_top2], "gap": gap([s["total"] for s in singles_top2])},
            "median_best": {"totals": medians_best, "gap": gap(medians_best)},
            "median_top2": {"totals": medians_top2, "gap": gap(medians_top2)},
            "dim_consistency_mean": round(statistics.mean(dim_agree), 3) if dim_agree else None,
            "dim_consistency_min": round(min(dim_agree), 3) if dim_agree else None,
            "item_count_note": f"标注条数：{[len(r) for r in runs]}",
        }
        results.append(entry)
        print(f"  → 单次极差 best {entry['single_best']['gap']} / top2 {entry['single_top2']['gap']}"
              f" ｜ 中位数法极差 best {entry['median_best']['gap']} / top2 {entry['median_top2']['gap']}"
              f" ｜ 维度归属平均一致率 {entry['dim_consistency_mean']}")

    print("\n================= 汇总 =================")
    print(f"{'简历':<22} {'单次best':>9} {'单次top2':>9} {'中位best':>9} {'中位top2':>9}  维度一致率  达标(<5)")
    for e in results:
        best = min(e[k]["gap"] for k in MODES)
        ok = "✅" if best < ACCEPT_GAP else "❌"
        print(f"{e['resume'][:20]:<22} {e['single_best']['gap']:>9.1f} {e['single_top2']['gap']:>9.1f} "
              f"{e['median_best']['gap']:>9.1f} {e['median_top2']['gap']:>9.1f}  "
              f"{e['dim_consistency_mean']:>10}  {ok}")

    os.makedirs(os.path.join(ROOT, a.out), exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = os.path.join(ROOT, a.out, f"label-stability-{stamp}.json")
    json.dump({"job": cfg.job_id, "model": a.model, "repeat": a.repeat, "group": a.group,
               "accept_gap": ACCEPT_GAP, "results": results},
              open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(f"\n明细：{path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
