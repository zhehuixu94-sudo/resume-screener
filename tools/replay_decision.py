#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""离线重放判定：用库里已存的解析结果 + 模型输出重算硬门槛 / 规则分 / 判定，不调用 LLM。

用途：
  1) 改岗位配置（硬门槛关键词、权重、达标线、聚合方式）后，先看影响再决定要不要重跑真实的 LLM 链路；
  2) 回归对比：同一批简历在新旧配置下的判定差在哪。

原理：evaluation.payload 里已经存了模型唯一一次的输出（llm_compare / dimensions[].items / fit / questions /
各段 StageMeta），resume_parse.payload 里有原文与证据 span。判定层本来就不加 LLM，所以可以完整重放。

用法：
  python tools/replay_decision.py config/job_jushi_teacher.json                  # 重放当前配置
  python tools/replay_decision.py config/job_x.json --compare-to config/job_jushi_teacher.json
  python tools/replay_decision.py config/job_x.json --dump out/replay.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.contracts import Evaluation, JobConfig  # noqa: E402
from app.contracts.resume_parse import ResumeParse  # noqa: E402
from app.pipeline.hard_filters import check_hard_filters, veto_summary  # noqa: E402
from app.pipeline.rules import bonus_layer, decide, rule_layer, sync_decisions_with_evidence  # noqa: E402
from app.store import Repository, db  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def replay(cfg: JobConfig, rows: list[dict]) -> list[dict]:
    out = []
    for r in rows:
        if not r.get("payload"):
            continue
        old = Evaluation.model_validate(r["payload"])
        parse_row = db.fetch_one("SELECT payload FROM resume_parse WHERE resume_id = %s", (r["resume_id"],))
        if not parse_row:
            continue
        parse = ResumeParse.model_validate(parse_row["payload"])

        # 从旧评估里还原"标注结果"（维度分就是从这里来的），再按新配置重算
        labeled = []
        for d in old.dimensions:
            for it in d.items:
                labeled.append({"item_id": it.item_id, "capability": it.capability, "relevance": it.relevance,
                                "difficulty": it.difficulty, "dimension": d.id, "reason": it.reason,
                                "confidence": it.confidence, "span_id": it.span_id})
        for u in old.unmapped_items:
            labeled.append({"item_id": u.item_id, "capability": u.capability, "relevance": u.relevance,
                            "difficulty": u.difficulty, "dimension": None, "reason": u.reason,
                            "confidence": None, "span_id": u.span_id})

        rules = rule_layer(cfg, labeled)
        bonus = bonus_layer(cfg, parse, labeled)
        hard = check_hard_filters(cfg, parse, old.llm_compare)
        stage_status = old.stage_status
        anchored = {c.span_id: c.anchored for c in old.cited_evidence}
        outcome = decide(cfg, rules.base_total, old.llm_compare, old.fit, stage_status, set(anchored), hard)
        outcome = sync_decisions_with_evidence(outcome, anchored, [d.best_span_id or "" for d in rules.dimensions])

        fails, unknown, passes = veto_summary(hard)
        out.append({
            "file": r["file_name"], "resume_id": r["resume_id"],
            "old": {"verdict": old.verdict, "decision": old.decision, "total": old.base_total,
                    "hard": {h.id: h.result for h in old.hard_filters}},
            "new": {"verdict": outcome.verdict, "decision": outcome.decision, "total": rules.base_total,
                    "hard": {h.id: h.result for h in hard}, "reason": outcome.reason,
                    "conflicts": [c.type for c in outcome.conflicts]},
            "hard_reasons": {h.id: h.reason for h in hard},
            "bonus_points": round(sum(b.points for b in bonus), 1),
            "dim_points": {d.id: d.points for d in rules.dimensions},
        })
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("job", help="岗位配置 JSON")
    ap.add_argument("--dump", default=None, help="把逐份重放结果写成 JSON")
    ap.add_argument("--show-reasons", action="store_true", help="打印硬门槛判定依据")
    ap.add_argument("--from-version", default=None,
                    help="拿哪个 config_version 的模型输出重放（默认当前；改配置后想立刻看影响就用旧的）")
    a = ap.parse_args()

    cfg = JobConfig.model_validate(json.load(open(a.job, encoding="utf-8")))
    repo = Repository(cfg.company_id)
    src_version = a.from_version or cfg.config_version
    rows = db.fetch_all(
        """SELECT a.application_id, a.state, e.verdict, e.decision, e.base_total, e.confidence, e.payload,
                  r.file_name, r.resume_id, a.attempts, a.last_error
             FROM application a JOIN resume r ON r.resume_id = a.resume_id
             LEFT JOIN evaluation e ON e.application_id = a.application_id
            WHERE a.company_id = %s AND a.job_id = %s AND a.config_version = %s
            ORDER BY e.base_total DESC NULLS LAST, r.file_name""",
        (cfg.company_id, cfg.job_id, src_version))
    if not rows:
        print(f"没有找到 {cfg.job_id}@{src_version} 的评估结果（用 --from-version 指定别的版本）")
        return 1
    print(f"重放源：{cfg.job_id}@{src_version} 的模型输出 × 配置 {cfg.job_id}@{cfg.config_version}")
    print(f"岗位 {cfg.job_id}@{cfg.config_version} ｜ 达标线 {cfg.scoring.pass_line}"
          f"（{cfg.scoring.pass_line_source}）±{cfg.scoring.band_width} ｜ 聚合 {cfg.scoring.dimension_aggregation}")
    results = replay(cfg, rows)
    print(f"\n{'简历':<26} {'旧分':>6} {'新分':>6}  {'旧判定/处置':<18} {'新判定/处置':<18} 硬门槛(旧→新)  变化")
    for r in sorted(results, key=lambda x: -x["new"]["total"]):
        o, n = r["old"], r["new"]
        hard_old = "/".join(f"{k}:{v[:1]}" for k, v in o["hard"].items())
        hard_new = "/".join(f"{k}:{v[:1]}" for k, v in n["hard"].items())
        changed = []
        if o["verdict"] != n["verdict"]:
            changed.append(f"{o['verdict']}→{n['verdict']}")
        if o["decision"] != n["decision"]:
            changed.append(f"{o['decision']}→{n['decision']}")
        if o["hard"] != n["hard"]:
            changed.append("门槛结论变了")
        if abs(o["total"] - n["total"]) > 0.05:
            changed.append(f"分数{o['total']:.1f}→{n['total']:.1f}")
        print(f"{r['file']:<26} {o['total']:>6.1f} {n['total']:>6.1f}  {o['verdict']}/{o['decision']:<12} "
              f"{n['verdict']}/{n['decision']:<12} {hard_old:<14}→{hard_new:<14} {'、'.join(changed) or '—'}")

    if a.show_reasons:
        print("\n硬门槛判定依据：")
        for r in results:
            print(f"  {r['file']}")
            for k, v in r["hard_reasons"].items():
                print(f"    {k}: {v}")

    if a.dump:
        json.dump(results, open(a.dump, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        print(f"\n已写出 {a.dump}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
