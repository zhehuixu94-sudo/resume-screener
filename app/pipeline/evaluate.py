# -*- coding: utf-8 -*-
"""阶段 2~5 + 规则层 → Evaluation（评估契约）。

顺序：标注 → 整体判定（相对比较）→ 匹配度 → 面试题（触发硬门槛则跳过一次调用）
      → 规则层（维度分/加权/加分项）→ 三段带判定 → 证据引用落地 → 契约校验
任何一段降级/失败都不会让流程断掉：降级 → 判定层强制转人工，证据与原因都写进 conflicts。
"""
from __future__ import annotations

import json

from pydantic import ValidationError

from ..contracts.evaluation import (
    CitedEvidence,
    Conflict,
    EvalQuality,
    Evaluation,
    EvalStages,
    ScoringSnapshot,
)
from ..contracts.job_config import JobConfig
from ..contracts.llm_outputs import (
    LlmCompareOutput,
    LlmFitOutput,
    LlmLabelOutput,
    LlmQuestionsOutput,
    Question,
)
from ..contracts.resume_parse import ResumeParse
from ..llm.client import SiliconFlowClient
from ..llm.coercers import (
    coerce_compare,
    coerce_fit,
    coerce_label_factory,
    coerce_questions,
    make_label_check,
    make_questions_check,
)
from ..llm.prompts import (
    PROMPT_VERSION,
    SYS_COMPARE,
    SYS_FIT,
    SYS_LABEL,
    SYS_Q,
    TPL_COMPARE,
    TPL_FIT,
    TPL_LABEL,
    TPL_Q,
    hash_of,
    render_items,
)
from ..llm.structured import call_structured
from .hard_filters import check_hard_filters
from .rules import bonus_layer, decide, rule_layer, sync_decisions_with_evidence


def _flat_items(parse: ResumeParse) -> list[dict]:
    out = []
    for i in parse.items:
        block = next((b for b in parse.blocks if b.block_id == i.block_id), None)
        out.append({"idx": len(out) + 1, "item_id": i.item_id, "block": block.title if block else "",
                    "capability": i.capability, "evidence": parse.text_of(i.span_id) if i.span_id else ""})
    return out


def evaluate(parse: ResumeParse, job: JobConfig, client: SiliconFlowClient, *, model: str | None = None,
             eval_id: str | None = None, temperature: float = 0.0) -> tuple[Evaluation, list[str]]:
    notes: list[str] = []
    flat = _flat_items(parse)
    by_idx = {f["idx"]: f for f in flat}

    # ---------------------------------------------------------- ② 标注
    dim_desc = "\n".join(f'  {d.id} = {d.name}' for d in job.dimensions)
    label_user = TPL_LABEL.format(job_title=job.job_title, dim_ids=", ".join(job.dimension_ids),
                                  dim_desc=dim_desc, items=json.dumps(flat, ensure_ascii=False, indent=1))
    label_res = call_structured(client, stage="label", system=SYS_LABEL, user=label_user,
                                model_cls=LlmLabelOutput, coercer=coerce_label_factory(flat, job), job=job,
                                prompt_version=PROMPT_VERSION, prompt_hash=hash_of(SYS_LABEL, TPL_LABEL),
                                config_version=job.config_version, model=model, max_tokens=6000,
                                temperature=temperature, dynamic_check=make_label_check(flat, job))

    labeled: list[dict] = []
    if label_res.obj is not None:
        for it in label_res.obj.items:                     # type: ignore[attr-defined]
            src = by_idx[int(it.idx)]
            labeled.append({"item_id": src["item_id"], "idx": int(it.idx), "block": it.block,
                            "capability": it.capability, "evidence": it.evidence, "relevance": it.relevance,
                            "difficulty": it.difficulty, "dimension": it.dimension, "reason": it.reason,
                            "confidence": it.confidence,
                            "span_id": parse.span_of(src["item_id"]).span_id if parse.span_of(src["item_id"]) else None})
    else:
        # 降级：标注段彻底失败 → 全部按中性值进规则层（分数会明显偏低，因此只能转人工）
        for f in flat:
            sp = parse.span_of(f["item_id"])
            labeled.append({"item_id": f["item_id"], "idx": f["idx"], "block": f["block"],
                            "capability": f["capability"], "evidence": f["evidence"], "relevance": "中",
                            "difficulty": "中", "dimension": None, "reason": "标注段失败，系统中性兜底",
                            "confidence": 0.0, "span_id": sp.span_id if sp else None})

    # ---------------------------------------------------------- ③ 整体判定
    veto_lines = "\n".join(f"  - [{f.id}] {f.name}：{f.rule}" for f in job.hard_filters) or "  （无硬门槛）"
    anchors = job.anchor_prompt()
    compare_user = TPL_COMPARE.format(job_title=job.job_title, jd=job.jd, veto=veto_lines,
                                      good=anchors.get("合格", "-"), border=anchors.get("边缘", "-"),
                                      bad=anchors.get("不达标", "-"), items=render_items(labeled))
    compare_res = call_structured(client, stage="compare", system=SYS_COMPARE, user=compare_user,
                                  model_cls=LlmCompareOutput, coercer=coerce_compare, job=job,
                                  prompt_version=PROMPT_VERSION, prompt_hash=hash_of(SYS_COMPARE, TPL_COMPARE),
                                  config_version=job.config_version, model=model, max_tokens=4000,
                                  temperature=temperature)

    # ---------------------------------------------------------- ④ 匹配度
    fit_user = TPL_FIT.format(job_title=job.job_title, jd=job.jd,
                              dims="、".join(d.name for d in job.dimensions), resume=parse.text)
    fit_res = call_structured(client, stage="fit", system=SYS_FIT, user=fit_user, model_cls=LlmFitOutput,
                              coercer=coerce_fit, job=job, prompt_version=PROMPT_VERSION,
                              prompt_hash=hash_of(SYS_FIT, TPL_FIT), config_version=job.config_version,
                              model=model, max_tokens=3000, temperature=temperature)

    # ---------------------------------------------------------- ⑤ 面试题
    skipped = ""
    q_res = None
    veto_text = compare_res.data.get("veto_triggered") if compare_res.data else None
    veto_ids = compare_res.data.get("veto_filter_ids", []) if compare_res.data else []
    if veto_text or veto_ids:
        skipped = f"触发硬性门槛（{veto_text or '、'.join(veto_ids)}），不出面试题"
    else:
        strong = [i for i in labeled if i["relevance"] in ("强", "中") and i["difficulty"] in ("高", "中")]
        strong = strong[:3] or labeled[:3]
        q_user = TPL_Q.format(job_title=job.job_title, jd=job.jd, focus=job.interview_focus,
                              items=render_items(strong))
        q_res = call_structured(client, stage="questions", system=SYS_Q, user=q_user,
                                model_cls=LlmQuestionsOutput, coercer=coerce_questions, job=job,
                                prompt_version=PROMPT_VERSION, prompt_hash=hash_of(SYS_Q, TPL_Q),
                                config_version=job.config_version, model=model, max_tokens=3000,
                                temperature=temperature, dynamic_check=make_questions_check())

    # ---------------------------------------------------------- 规则层
    rules = rule_layer(job, labeled)
    compare_obj: LlmCompareOutput | None = compare_res.obj if compare_res.obj else None
    fit_obj: LlmFitOutput | None = fit_res.obj if fit_res.obj else None
    bonus = bonus_layer(job, parse.text, labeled, compare_obj)

    stage_status = {"label": label_res.status, "compare": compare_res.status, "fit": fit_res.status}
    if q_res is not None:
        stage_status["questions"] = q_res.status

    # ---------------------------------------------------------- 证据引用
    cited: list[CitedEvidence] = []
    seen: set[str] = set()
    for it in labeled + rules.unmapped:
        sid = it.get("span_id") if isinstance(it, dict) else getattr(it, "span_id", None)
        if not sid or sid in seen:
            continue
        seen.add(sid)
        sp = next((e for e in parse.evidence if e.span_id == sid), None)
        if sp is None:
            continue
        cited.append(CitedEvidence(span_id=sp.span_id, text=sp.text, span=sp.span, page=sp.page,
                                   anchored=sp.anchored, anchor_method=sp.anchoring.method))
    anchored_map = {c.span_id: c.anchored for c in cited}

    # 硬门槛：规则层按配置关键词预检（模型只负责解释与给证据，不允许它随手把门槛标成触发）
    hard = check_hard_filters(job, parse, compare_obj)
    outcome = decide(job, rules.base_total, compare_obj, fit_obj, stage_status, set(anchored_map), hard,
                     dim_points={d.id: d.points for d in rules.dimensions},
                     bonus_names={b.name for b in bonus})
    outcome = sync_decisions_with_evidence(outcome, anchored_map,
                                           [d.best_span_id or "" for d in rules.dimensions])

    sc = job.scoring
    reasons = []
    if compare_obj:
        reasons.append(f"整体判定（相对比较锚点档）：{compare_obj.reason}")
    reasons.append(outcome.reason)

    evaluation = Evaluation(
        eval_id=eval_id or f"{parse.resume_id}-{job.job_id}-{job.config_version}",
        resume_id=parse.resume_id, job_id=job.job_id, job_config_version=job.config_version,
        company_id=job.company_id,
        verdict=outcome.verdict,            # type: ignore[arg-type]
        decision=outcome.decision,          # type: ignore[arg-type]
        decision_reason=outcome.reason,
        reasons=reasons,
        confidence=compare_obj.confidence if compare_obj else 0.0,
        hard_filters=outcome.hard_filters,
        dimensions=rules.dimensions,
        base_total=rules.base_total,
        bonus_hits=bonus, bonus_points=round(sum(b.points for b in bonus), 1),
        total_with_bonus=round(rules.base_total + sum(b.points for b in bonus), 1),
        unmapped_items=rules.unmapped,
        fit=fit_obj,
        cited_evidence=cited,
        llm_compare=compare_obj,
        questions=[Question(**q.model_dump()) for q in q_res.obj.questions] if (q_res and q_res.obj) else [],
        questions_skipped_reason=skipped,
        conflicts=outcome.conflicts,
        model_id=compare_res.meta.model_id, prompt_version=PROMPT_VERSION, temperature=temperature,
        stages=EvalStages(label=label_res.meta, compare=compare_res.meta, fit=fit_res.meta,
                          questions=q_res.meta if q_res else None),
        scoring_snapshot=ScoringSnapshot(
            aggregation=sc.dimension_aggregation, pass_line=sc.pass_line, pass_line_source=sc.pass_line_source,
            band_width=sc.band_width, pass_threshold=sc.pass_line + sc.band_width,
            reject_threshold=sc.pass_line - sc.band_width, confidence_floor=sc.confidence_floor,
            config_version=job.config_version),
        quality=EvalQuality(gate_passed=parse.quality.gate_passed, issues=parse.quality.issues,
                            evidence_coverage=parse.stats.evidence_coverage,
                            unanchored_items=parse.stats.evidence_unanchored),
    )
    return evaluation, notes


def evaluate_safe(parse: ResumeParse, job: JobConfig, client: SiliconFlowClient, **kw) -> tuple[Evaluation | None, str]:
    """契约兜底：万一 Evaluation 组装仍不合契约，降级成"转人工"再组装一次；再不行就返回 None。"""
    try:
        ev, _ = evaluate(parse, job, client, **kw)
        return ev, ""
    except ValidationError as e:
        first = "; ".join(f"{'.'.join(str(x) for x in err['loc'])}: {err['msg']}" for err in e.errors()[:3])
        return None, f"Evaluation 契约校验失败：{first}"
