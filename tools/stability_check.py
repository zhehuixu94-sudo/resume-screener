#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""任务 4：规则分稳定性测量。

同一份简历重复 N 次，看：判定一致率、总分极差、各维度得分波动。
同时用**同一批模型输出**离线比较两种维度聚合方式（best 取最强项 / top2_mean 取 top-2 平均），
所以对比聚合方式不需要多跑一遍 LLM。

用法：
  python tools/stability_check.py --job config/job_jushi_teacher.json \\
      --resumes "示例候选人简历3.0.docx" resume_001.txt resume_a.pdf --repeat 3
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.contracts import Evaluation, JobConfig  # noqa: E402
from app.llm.client import SiliconFlowClient  # noqa: E402
from app.pipeline.evaluate import evaluate  # noqa: E402
from app.pipeline.parse import parse_resume  # noqa: E402
from app.pipeline.rules import decide, rule_layer, sync_decisions_with_evidence  # noqa: E402
from app.pipeline.score import gate_score                                            # noqa: E402
from app.pipeline.stability import MAX_DANGER_FLIP, MAX_SAFE_FLIP, MAX_SCORE_GAP   # noqa: E402
from app.pipeline.stability import flip_metrics                                    # noqa: E402
from app.pipeline.stability import record as stability_record                      # noqa: E402
from app.pipeline.stability import status_text as stability_status_text            # noqa: E402
from app.pipeline.hard_filters import check_hard_filters  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ACCEPT_GAP = 5.0          # 验收标准：总分极差 < 5


def labeled_from_eval(ev: Evaluation) -> list[dict]:
    """从评估里还原标注结果，用于离线换聚合方式重算规则层。"""
    out = []
    for d in ev.dimensions:
        for it in d.items:
            out.append({"item_id": it.item_id, "capability": it.capability, "relevance": it.relevance,
                        "difficulty": it.difficulty, "dimension": d.id, "reason": it.reason,
                        "confidence": it.confidence, "span_id": it.span_id})
    for u in ev.unmapped_items:
        out.append({"item_id": u.item_id, "capability": u.capability, "relevance": u.relevance,
                    "difficulty": u.difficulty, "dimension": None, "reason": u.reason,
                    "confidence": None, "span_id": u.span_id})
    return out


def recount(ev: Evaluation, cfg: JobConfig, mode: str, parse) -> dict:
    """用同一份模型输出，按指定聚合方式重算规则分与判定（不调 LLM）。"""
    labeled = labeled_from_eval(ev)
    rules = rule_layer(cfg, labeled, aggregation=mode)
    hard = check_hard_filters(cfg, parse, ev.llm_compare)
    anchored = {c.span_id: c.anchored for c in ev.cited_evidence}
    total, _parts = gate_score(cfg, [d.model_dump(mode='json') for d in rules.dimensions],
                              [e.text for e in parse.evidence])
    out = decide(cfg, total, ev.llm_compare, ev.fit, ev.stage_status, set(anchored), hard,
                 dim_points={d.id: d.points for d in rules.dimensions}, bonus_names=set())
    out = sync_decisions_with_evidence(out, anchored, [d.best_span_id or "" for d in rules.dimensions])
    return {"total": total, "verdict": out.verdict, "decision": out.decision,
            "dims": {d.id: d.points for d in rules.dimensions}}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--job", required=True)
    ap.add_argument("--resumes", nargs="+", required=True, help="简历文件名（会在 local/ 与桌面目录里找）或绝对路径")
    ap.add_argument("--repeat", type=int, default=3)
    ap.add_argument("--model", default=os.environ.get("RESUME_MODEL") or "Qwen/Qwen3.5-35B-A3B")
    ap.add_argument("--search", nargs="*", default=["local/samples", "local/samples_all",
                    r"C:\Users\user\Desktop\毕设及简历", r"C:\Users\user\Desktop"], help="按文件名找简历的目录")
    ap.add_argument("--out", default="out/stability")
    ap.add_argument("--record", action="store_true",
                    help="把结果写进 stability_check 表：**自动淘汰闸门**靠它放行（不写就永远不许自动淘汰）")
    ap.add_argument("--reparse", action="store_true",
                    help="每次重跑都重新解析（默认复用第一次的解析结果，与线上一致：简历只解析一次）")
    a = ap.parse_args()

    job_path = a.job if os.path.exists(a.job) else os.path.join(ROOT, a.job)
    cfg = JobConfig.model_validate(json.load(open(job_path, encoding="utf-8")))
    client = SiliconFlowClient(model=a.model)
    if not client.probe():
        print(f"⚠️  {client.model} 探活失败，换降本档 Qwen/Qwen3.5-35B-A3B")
        client.model = "Qwen/Qwen3.5-35B-A3B"

    def resolve(name: str) -> str:
        if os.path.exists(name):
            return name
        for d in a.search:
            p = os.path.join(d, name)
            if os.path.exists(p):
                return p
            p2 = os.path.join(ROOT, d, name)
            if os.path.exists(p2):
                return p2
        raise SystemExit(f"找不到简历：{name}")

    results = []
    for name in a.resumes:
        path = resolve(name)
        print(f"\n=== {name}（{a.repeat} 次）===")
        runs = []
        cached_parse = None
        for i in range(1, a.repeat + 1):
            if cached_parse is None or a.reparse:
                outcome = parse_resume(path, cfg, client, model=a.model)
                if outcome.parse is None:
                    print(f"  第 {i} 次：解析失败，跳过")
                    continue
                cached_parse = outcome.parse
            ev, _ = evaluate(cached_parse, cfg, client, model=a.model)
            runs.append({"ev": ev, "parse": outcome.parse})
            print(f"  第 {i} 次：{ev.verdict}/{ev.decision} 判定分 {ev.gate_score} "
                  f"｜置信度 {ev.confidence} ｜ 能力项 {len(ev.dimensions) and sum(len(d.items) for d in ev.dimensions)}")
        if not runs:
            continue
        entry = {"resume": name, "path": path, "runs": len(runs),
                 "verdicts": [r["ev"].verdict for r in runs],
                 "decisions": [r["ev"].decision for r in runs],
                 "totals": [r["ev"].gate_score for r in runs],
                 "confidence": [r["ev"].confidence for r in runs],
                 "item_counts": [sum(len(d.items) for d in r["ev"].dimensions) for r in runs],
                 "modes": {}, "conflicts": [[c.type for c in r["ev"].conflicts] for r in runs]}
        for mode in ("best", "top2_mean"):
            rec = [recount(r["ev"], cfg, mode, r["parse"]) for r in runs]
            totals = [x["total"] for x in rec]
            verdicts = [x["verdict"] for x in rec]
            entry["modes"][mode] = {
                "totals": totals, "gap": round(max(totals) - min(totals), 1),
                "mean": round(statistics.mean(totals), 1),
                "stdev": round(statistics.pstdev(totals), 2) if len(totals) > 1 else 0.0,
                "verdicts": verdicts,
                "verdict_consistency": round(max(verdicts.count(v) for v in set(verdicts)) / len(verdicts), 2),
                "decisions": [x["decision"] for x in rec],
                "dim_gap": {k: round(max(x["dims"].get(k, 0) for x in rec) - min(x["dims"].get(k, 0) for x in rec), 1)
                            for k in rec[0]["dims"]},
            }
        # 判定一致率（真实处置，而不是离线重算）
        vs = entry["verdicts"]
        entry["verdict_consistency"] = round(max(vs.count(v) for v in set(vs)) / len(vs), 2)
        entry["gap_best"] = entry["modes"]["best"]["gap"]
        entry["gap_top2"] = entry["modes"]["top2_mean"]["gap"]
        results.append(entry)

    print("\n=========== 汇总 ===========")
    print(f"{'简历':<24} {'聚合':<10} {'各次总分':<24} {'极差':>6} {'判定一致率':>10}  验收(极差<5)")
    for e in results:
        for mode, m in e["modes"].items():
            ok = "✅" if m["gap"] < ACCEPT_GAP else "❌"
            print(f"{e['resume'][:22]:<24} {mode:<10} {str(m['totals']):<24} {m['gap']:>6.1f} "
                  f"{m['verdict_consistency']:>10.0%}  {ok}")
    print("\n结论：")
    for e in results:
        better = "top2_mean" if e["gap_top2"] < e["gap_best"] else "best"
        print(f"  {e['resume']}：best 极差 {e['gap_best']:.1f} → top2_mean 极差 {e['gap_top2']:.1f}（{better} 更稳）")

    if a.record and results:
        # 闸门判据已改成**非对称两侧翻转率**（见 app/pipeline/stability.py）：
        #   危险侧（从严翻到淘汰/不达标）必须为 0；安全侧（多让人看一眼）宽容到 30%。
        # 旧口径"判定一致率"只作参考一并落库，不再是判据。
        agreement = min(e["verdict_consistency"] for e in results)
        gap = max(e["gap_best"] for e in results)
        samples = [{"resume": e["resume"],
                    "decisions": e["modes"]["best"]["decisions"],
                    "verdicts": e["modes"]["best"]["verdicts"]} for e in results]
        metrics = flip_metrics(samples)
        print("\n三口径指标（报告的统计口径就以这三条为准）：")
        print(f"  分数极差         {gap:>6.1f}   {'✅ 通过' if gap <= MAX_SCORE_GAP else '❌ 超限'}（上限 {MAX_SCORE_GAP}）")
        print(f"  危险侧翻转率     {metrics['danger_flip_rate']:>6.0%}   "
              f"{'✅ 通过' if metrics['danger_flip_rate'] <= MAX_DANGER_FLIP else '❌ 危险'}"
              f"（必须 0：重复跑时从严翻到淘汰/不达标）")
        print(f"  安全侧翻转率     {metrics['safe_flip_rate']:>6.0%}   "
              f"{'✅ 通过' if metrics['safe_flip_rate'] < MAX_SAFE_FLIP else '⚠️ 偏高'}"
              f"（宽容 <{MAX_SAFE_FLIP:.0%}：多数票自动达标、个别次转人工）")
        print(f"  〔参考〕判定一致率 {agreement:.0%}（旧口径，只作对照，不代表危险程度）")
        for d in metrics["per_resume"]:
            print(f"    {d['resume']:<24} 多数票 {d['majority']:<14} 各次 {d['decisions']}"
                  f"{'  ⚠️危险侧' if d['danger'] else ''}{'  ⚠️安全侧' if d['safe'] else ''}")
        check_id = stability_record(cfg.job_id, cfg.config_version, n_samples=len(results),
                                   agreement=agreement, score_range=gap,
                                   danger_flip_rate=metrics["danger_flip_rate"],
                                   safe_flip_rate=metrics["safe_flip_rate"],
                                   detail={"accept_gap": ACCEPT_GAP, "repeat": a.repeat,
                                           "model": a.model, "metrics": metrics,
                                           "per_resume": {e["resume"]: {"consistency": e["verdict_consistency"],
                                                                        "gap": e["gap_best"]}
                                                          for e in results}})
        print(f"\n已记录稳定性：check_id={check_id} · 样本 {len(results)} 份 · "
              f"极差 {gap:.1f} · 危险侧 {metrics['danger_flip_rate']:.0%} · 安全侧 {metrics['safe_flip_rate']:.0%}")
        print(f"闸门状态：{stability_status_text(cfg.job_id, cfg.config_version)}")

    os.makedirs(os.path.join(ROOT, a.out), exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = os.path.join(ROOT, a.out, f"stability-{stamp}.json")
    json.dump({"job": cfg.job_id, "config_version": cfg.config_version, "model": a.model,
               "repeat": a.repeat, "ts": stamp, "accept_gap": ACCEPT_GAP, "results": results},
              open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=2, default=str)
    print(f"\n明细：{path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
