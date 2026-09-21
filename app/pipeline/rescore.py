# -*- coding: utf-8 -*-
"""整批跑完后的**判定分回填**：把「本批分位」这一项算进来，重算总分并重新判定（0 token）。

为什么需要单独一步：
加分项 = 40 × 本批分位(Σ 相关度×难度)。它是**相对量**——单份评估时根本不知道"本批"是谁，
所以 `evaluate()` 先用中性位次 0.5 保证可复现，整批结束后在这里回填真实位次。
这一步只吃库里已存的模型输出（dimensions / llm_compare / fit / hard_filters / cited_evidence），
不调用任何 LLM：口径改了、分位变了，几秒钟就能重算一遍。

⚠️ 只更新「分数与处置」，不改任何模型输出字段——审计要求模型原始判定永远留档。
"""
from __future__ import annotations

from ..contracts import Evaluation, JobConfig
from ..store import db
from .rules import decide, sync_decisions_with_evidence
from .score import gate_score
from .stability import apply_gate, latest as stability_latest
from .weighted_total import bonus_raw_v2, percentile_rank, rank_key, skill_words


def _load(job: JobConfig) -> list[dict]:
    return db.fetch_all(
        """SELECT a.application_id, e.payload FROM application a
             JOIN evaluation e ON e.application_id = a.application_id
            WHERE a.company_id = %s AND a.job_id = %s AND a.config_version = %s
              AND e.payload IS NOT NULL""",
        (job.company_id, job.job_id, job.config_version))


def rescore_batch(job: JobConfig, *, apply: bool = True, verbose: bool = True) -> dict:
    """重算一批的判定分与处置。返回统计（含处置变化明细）。"""
    rows = _load(job)
    if not rows:
        return {"n": 0}

    loaded: list[tuple[int, Evaluation, dict, list[str]]] = []
    for r in rows:
        ev = Evaluation.model_validate(r["payload"])
        parse_row = db.fetch_one("SELECT payload FROM resume_parse WHERE resume_id = %s", (ev.resume_id,))
        texts: list[str] = []
        if parse_row:
            payload = parse_row["payload"]
            if isinstance(payload, str):
                import json
                payload = json.loads(payload)
            texts = [e.get("text") or "" for e in (payload.get("evidence") or [])]
        # ⚠️ 加分项原始量已换成定稿口径 G2 = Σq/√n（q 由证据文本 + 岗位技能词典纯代码算出）：
        #    旧口径 Σ相关度×难度 与「证据条数」ρ=+0.73，等于按条数发分 →
        #    这里必须传**证据文本**（不是 difficulty_score），否则分位算的是另一个量。
        dims = [{"id": d.id,
                 "items": [{"relevance_score": i.relevance_score,
                            "difficulty_score": i.difficulty_score} for i in d.items]}
                for d in ev.dimensions]
        loaded.append((r["application_id"], ev, {"dims": dims, "texts": texts}, []))

    # 一次算整批：加分项原始量（G2）→ 本批分位
    skills = skill_words(job)
    raw_by_resume = {str(ev.resume_id): bonus_raw_v2(ctx["texts"], skills) for _, ev, ctx, _ in loaded}
    rank = percentile_rank(raw_by_resume)

    stats = {"n": len(loaded), "decision_changed": 0, "score_changed": 0, "moves": {}}
    updated: list[tuple[int, Evaluation]] = []
    for app_id, ev, ctx, _ in loaded:
        total, parts = gate_score(job, ctx["dims"], ctx["texts"],
                                  bonus_rank=rank.get(str(ev.resume_id)))
        anchored = {c.span_id: c.anchored for c in ev.cited_evidence}
        outcome = decide(job, total, ev.llm_compare, ev.fit, ev.stage_status, set(anchored),
                         ev.hard_filters,
                         dim_points={d.id: d.points for d in ev.dimensions},
                         bonus_names={b.name for b in ev.bonus_hits},
                         fluff_rate=parts.get("fluff_rate"), score_base=parts.get("base"))
        outcome = sync_decisions_with_evidence(outcome, anchored,
                                               [d.best_span_id or "" for d in ev.dimensions if d.points > 0])
        outcome = apply_gate(outcome, stability_latest(job.job_id, job.config_version),
                             job_id=job.job_id, config_version=job.config_version,
                             allow_auto_reject=bool(getattr(job.scoring, "auto_reject_enabled", False)))
        ev2 = ev.model_copy(update={
            "gate_score": total,
            "score_parts": ev.score_parts.model_copy(update=parts) if ev.score_parts else None,
            "verdict": outcome.verdict, "decision": outcome.decision,
            "decision_reason": outcome.reason, "conflicts": outcome.conflicts,
            "scoring_snapshot": ev.scoring_snapshot.model_copy(update={"gate_score_field": "gate_score"}),
        })
        if ev2.decision != ev.decision:
            stats["decision_changed"] += 1
            key = f"{ev.decision} → {ev2.decision}"
            stats["moves"][key] = stats["moves"].get(key, 0) + 1
        if abs(float(ev2.gate_score) - float(ev.gate_score)) > 0.005:
            stats["score_changed"] += 1
        updated.append((app_id, ev2))

    if apply:
        for app_id, ev2 in updated:
            db.execute(
                """UPDATE evaluation SET verdict = %s, decision = %s, gate_score = %s, payload = %s
                    WHERE application_id = %s""",
                (ev2.verdict, ev2.decision, ev2.gate_score,
                 _json(ev2), app_id))
    if verbose:
        print(f"  回填分位：{stats['n']} 份 · 分数变化 {stats['score_changed']} · "
              f"处置变化 {stats['decision_changed']} {stats['moves'] or ''}")
    return stats


def _json(ev: Evaluation) -> str:
    import json
    return json.dumps(ev.model_dump(mode="json"), ensure_ascii=False)


__all__ = ["rescore_batch", "rank_key"]
