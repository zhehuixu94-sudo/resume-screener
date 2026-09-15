# -*- coding: utf-8 -*-
"""阶段 2~5 + 规则层 → Evaluation（评估契约）。

顺序：标注 → 整体判定（相对比较）→ 匹配度 → 面试题（触发硬门槛则跳过一次调用）
      → 规则层（维度分/加权/加分项）→ 三段带判定 → 证据引用落地 → 契约校验
任何一段降级/失败都不会让流程断掉：降级 → 判定层强制转人工，证据与原因都写进 conflicts。
"""
from __future__ import annotations

import json
import statistics
from collections import Counter

from pydantic import ValidationError

from ..contracts.evaluation import (
    CitedEvidence,
    LabelingMeta,
    Conflict,
    EvalQuality,
    Evaluation,
    EvalStages,
    ScoreParts,
    ScoringSnapshot,
    UnmappedItem,
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
from .cover import CoverOutcome, cover_dimensions
from .hard_filters import check_hard_filters
from .rules import RELEVANCE_W, DIFFICULTY_W, bonus_layer, decide, rule_layer, sync_decisions_with_evidence
from .score import gate_score
from .stability import apply_gate, latest as stability_latest


def _flat_items(parse: ResumeParse) -> list[dict]:
    out = []
    for i in parse.items:
        block = next((b for b in parse.blocks if b.block_id == i.block_id), None)
        out.append({"idx": len(out) + 1, "item_id": i.item_id, "block": block.title if block else "",
                    "capability": i.capability, "evidence": parse.text_of(i.span_id) if i.span_id else ""})
    return out


def _merge_label_runs(runs: list[list[dict]]) -> tuple[list[dict], dict]:
    """把多次标注合成一次：维度取多数票，相关性/难度取与中位分数最接近的那一轮的标签。

    为什么这么做：单次标注的总分极差实测能到 10~16 分（同一个条目在"弱/中"之间翻），
    取中位数把这种单点翻转吸收掉；维度归属同理（一个条目从"竞赛"翻到"编程"会整段改分）。
    """
    by_item: dict[str, list[dict]] = {}
    for labeled in runs:
        for it in labeled:
            by_item.setdefault(it["item_id"], []).append(it)

    merged, agree = [], []
    for item_id, group in by_item.items():
        base = dict(group[0])
        dims = [g.get("dimension") for g in group if g.get("dimension")]
        if dims:
            counts = Counter(dims)
            top = counts.most_common()
            tied = sorted(d for d, c in top if c == top[0][1])
            base["dimension"] = tied[0]                   # 并列取字典序最小，保证确定性
            agree.append(top[0][1] / len(dims))
        else:
            base["dimension"] = None
        raws = sorted(RELEVANCE_W.get(g.get("relevance"), 0.0) * DIFFICULTY_W.get(g.get("difficulty"), 1)
                      for g in group)
        median = statistics.median(raws)
        pick = min(group, key=lambda g: (abs(RELEVANCE_W.get(g.get("relevance"), 0.0)
                                              * DIFFICULTY_W.get(g.get("difficulty"), 1) - median),
                                         RELEVANCE_W.get(g.get("relevance"), 0.0)))
        base["relevance"], base["difficulty"] = pick.get("relevance"), pick.get("difficulty")
        base["confidence"] = round(statistics.mean(g.get("confidence") or 0.0 for g in group), 3)
        merged.append(base)
    stats = {"repeats": len(runs),
             "dimension_agreement_mean": round(statistics.mean(agree), 3) if agree else None,
             "per_item_agreement_min": round(min(agree), 3) if agree else None}
    return merged, stats


def _span_id_of(item_id: str, parse: ResumeParse, span_map: dict[str, str]) -> str | None:
    """先查解析阶段锚好的 span，再查外部（定向取证）带来的 span。"""
    sp = parse.span_of(item_id)
    if sp is not None:
        return sp.span_id
    return span_map.get(item_id) or None


def label_items(parse: ResumeParse, job: JobConfig, client: SiliconFlowClient, *, model: str | None = None,
                temperature: float = 0.0, repeats: int | None = None,
                extra_items: list[dict] | None = None,
                span_map: dict[str, str] | None = None) -> tuple[list[dict], object, dict]:
    """标注段（可单独调用）：返回（对齐到解析条目的标注结果, StageMeta）。

    抽出来是为了做稳定性实验 —— 同一份解析结果重复标注 N 次，才能把"标注噪声"和"解析噪声"分开。
    """
    flat = _flat_items(parse)
    for extra in (extra_items or []):                      # 定向取证带来的条目，一起交给标注段
        extra = dict(extra)
        extra["idx"] = len(flat) + 1
        flat.append(extra)
    by_idx = {f["idx"]: f for f in flat}
    dim_desc = "\n".join(f'  {d.id} = {d.name}' for d in job.dimensions)
    label_user = TPL_LABEL.format(job_title=job.job_title, dim_ids=", ".join(job.dimension_ids),
                                  dim_desc=dim_desc, items=json.dumps(flat, ensure_ascii=False, indent=1))
    n = repeats if repeats is not None else getattr(job.scoring, "labeling_repeats", 1)
    runs: list[list[dict]] = []
    res = None
    for _ in range(max(1, n)):
        res = call_structured(client, stage="label", system=SYS_LABEL, user=label_user,
                              model_cls=LlmLabelOutput, coercer=coerce_label_factory(flat, job), job=job,
                              prompt_version=PROMPT_VERSION, prompt_hash=hash_of(SYS_LABEL, TPL_LABEL),
                              config_version=job.config_version, model=model, max_tokens=6000,
                              temperature=temperature, dynamic_check=make_label_check(flat, job))
        runs.append(_parse_label_payload(res, by_idx, parse, flat, span_map or {}))
    if len(runs) == 1:
        merged, stats = runs[0], {"repeats": 1, "dimension_agreement_mean": None, "per_item_agreement_min": None}
    else:
        merged, stats = _merge_label_runs(runs)
    return merged, res.meta, stats


def _parse_label_payload(res, by_idx: dict, parse: ResumeParse, flat: list[dict],
                         span_map: dict[str, str] | None = None) -> list[dict]:
    labeled: list[dict] = []
    if res.obj is not None:
        for it in res.obj.items:
            src = by_idx[int(it.idx)]
            sid = _span_id_of(src["item_id"], parse, span_map or {})
            labeled.append({"item_id": src["item_id"], "idx": int(it.idx), "block": it.block,
                            "capability": it.capability, "evidence": it.evidence, "relevance": it.relevance,
                            "relevance_score": it.relevance_score, "difficulty": it.difficulty,
                            "difficulty_score": it.difficulty_score,
                            "dimension": it.dimension, "reason": it.reason,
                            "confidence": it.confidence, "span_id": sid})
    else:
        for f in flat:                                   # 降级：标注段彻底失败 → 中性值（分数偏低，只能转人工）
            sp = _span_id_of(f["item_id"], parse, span_map or {})
            labeled.append({"item_id": f["item_id"], "idx": f["idx"], "block": f["block"],
                            "capability": f["capability"], "evidence": f["evidence"], "relevance": "中",
                            "relevance_score": None, "difficulty": "中", "difficulty_score": None,
                            "dimension": None, "reason": "标注段失败，系统中性兜底",
                            "confidence": 0.0, "span_id": sp})
    return labeled


def _merge_compare_runs(runs: list) -> object:
    """多次「整体判定」取**多数票**（verdict 众数 + 与中位置信度最接近的那一轮）。

    为什么：分数是代码算的、可以做到极差 0；整体判定是模型说的，同一份简历三次会在
    达标/边缘之间跳。3 次里翻 1 次，处置就从 auto_pass 变 manual_review——稳定性一致率卡在 0.67，
    闸门（要求 ≥0.8）永远过不去。多数票是唯一能压住"模型口径抖动"的手段。

    并列取字典序、置信度并列取 trace_id，保证**同一批数据每次合并结果一致**。
    有效轮次不到一半时视为该阶段失败（返回第一轮，让降级路径接管，不许拿少数派当结论）。
    """
    valid = [r for r in runs if r.obj is not None]
    if not valid:
        return runs[0]
    if len(valid) * 2 < len(runs) + 1:          # 有效轮次 < 半数
        return next(r for r in runs if r.obj is None)
    counts = Counter(r.obj.verdict for r in valid)
    top = counts.most_common()
    order = {"不达标": 0, "边缘": 1, "达标": 2}
    winner = max((v for v, c in top if c == top[0][1]), key=lambda v: order.get(v, 1))
    same = [r for r in valid if r.obj.verdict == winner]
    # 并列的候选轮里取**置信度更低**的那一轮（= 更保守的一份判断与理由）：
    # 用户口径"3 次里 2 次达标就取那 2 次里较低的值"；置信度并列再用 trace_id 兜底保证确定性。
    return min(same, key=lambda r: (r.obj.confidence, (r.meta.trace_ids or [""])[-1]))


def questions_skip_reason(hard: list[HardFilterResult], compare_data: dict | None) -> tuple[str, list[str]]:
    """面试题是否跳过：**只看规则层的门槛结论**，返回 (跳过原因, 备注)。

    为什么不信模型的 veto 自述：实测 100 份里模型有 6 次自称"触发硬性门槛"（理由是"专业不对口"），
    而规则层按配置关键词对 100 份全部判定 major=pass。采信模型自述会直接吞掉这些人的面试题。
    """
    notes: list[str] = []
    rule_veto = [h for h in hard if h.veto and h.result == "fail"]
    if rule_veto:
        return "规则层硬门槛未通过（" + "、".join(h.id for h in rule_veto) + "），不出面试题", notes
    md = compare_data or {}
    if md.get("veto_triggered") or md.get("veto_filter_ids"):
        notes.append("模型自称触发硬门槛、规则层判定未触发：已按规则层结论继续出题（自述见 llm_compare）")
    return "", notes


def evaluate(parse: ResumeParse, job: JobConfig, client: SiliconFlowClient, *, model: str | None = None,
             eval_id: str | None = None, temperature: float = 0.0,
             bonus_rank: float | None = None) -> tuple[Evaluation, list[str]]:
    """跑一份简历的完整评估。

    `bonus_rank` = 这份简历在本批「加分项原始量」里的分位（0~1）：
      · 单份评估（命令行/测试）拿不到整批 → None → 中性 0.5；
      · 整批跑完后由 `app/pipeline/rescore.py` 回填真实分位并重算判定分。
    """
    notes: list[str] = []
    flat = _flat_items(parse)

    # ---------------------------------------------------------- ①b 按维度定向取证
    # 解析阶段的条目抽取与岗位无关（那是简历属性、跨岗位复用），所以它不知道岗位有哪些评分维度，
    # 导致部分维度整段无条目（实测 6 维里 3 维对 7 成简历为空）。这里补一次岗位相关的定向取证。
    cover = cover_dimensions(job, parse, client, model=model, temperature=temperature)
    if not cover.ok:
        # 取证段失败 = 这份简历少了十几条维度证据（实测判定分差 20 分）→ 不许静默降分自动处置。
        # 这里记 note；真正拦住自动处置的是 stage_status["cover"]（decide() 的降级分支）。
        notes.append("取证段失败：本次评估未做维度覆盖校验，判据仅按原有条目计算 → 已记录为降级，强制转人工")
    span_map = dict(cover.span_map)
    pool = cover.span_pool(parse)          # 解析证据 + 取证证据的统一池（门槛/加分/引用都用它）

    # ---------------------------------------------------------- ② 标注
    labeled, label_meta, label_stats = label_items(parse, job, client, model=model, temperature=temperature,
                                                   extra_items=cover.items if cover.ok else None,
                                                   span_map=span_map)
    label_res_status = label_meta.validation.status

    # 锚定兜底（实测教训：一刀切转人工会把 77 分的人刷掉）
    # 锚定失败的条目**剔出计分**：它自己那部分分数不算，其余证据照常算——比"整份强制转人工"平滑得多。
    # 剔除后不再有"未锚定的最强证据"，契约的安全约束自然满足；被剔的条目记进 unmapped_items 留痕。
    kept, dropped = [], []
    for it in labeled:
        (kept if (it.get("span_id") or "") else dropped).append(it)
    if dropped:
        notes.append(f"有 {len(dropped)} 条证据锚定失败 → 已剔出计分（只扣这些条目，其余证据照常算）")
    labeled = kept

    # ---------------------------------------------------------- ③ 整体判定（可重复 + 多数票）
    # 为什么重复：实测分数已经锁死（同一份简历三次极差 0.0），但「整体判定」仍在达标/边缘之间跳；
    # 3 次里翻 1 次 → 处置从 auto_pass 变 manual_review → 稳定性一致率卡在 0.67（闸门 0.8 过不去）。
    # 分数是代码算的、判定是模型说的——模型的抖动只能用多数票压。
    veto_lines = "\n".join(f"  - [{f.id}] {f.name}：{f.rule}" for f in job.hard_filters) or "  （无硬门槛）"
    anchors = job.anchor_prompt()
    compare_user = TPL_COMPARE.format(job_title=job.job_title, jd=job.jd, veto=veto_lines,
                                      good=anchors.get("合格", "-"), border=anchors.get("边缘", "-"),
                                      bad=anchors.get("不达标", "-"), items=render_items(labeled))
    n_compare = max(1, getattr(job.scoring, "compare_repeats", 1))
    compare_runs = [call_structured(client, stage="compare", system=SYS_COMPARE, user=compare_user,
                                    model_cls=LlmCompareOutput, coercer=coerce_compare, job=job,
                                    prompt_version=PROMPT_VERSION, prompt_hash=hash_of(SYS_COMPARE, TPL_COMPARE),
                                    config_version=job.config_version, model=model, max_tokens=4000,
                                    temperature=temperature)
                    for _ in range(n_compare)]
    compare_res = _merge_compare_runs(compare_runs)

    # ---------------------------------------------------------- ④ 匹配度
    fit_user = TPL_FIT.format(job_title=job.job_title, jd=job.jd,
                              dims="、".join(d.name for d in job.dimensions), resume=parse.text)
    fit_res = call_structured(client, stage="fit", system=SYS_FIT, user=fit_user, model_cls=LlmFitOutput,
                              coercer=coerce_fit, job=job, prompt_version=PROMPT_VERSION,
                              prompt_hash=hash_of(SYS_FIT, TPL_FIT), config_version=job.config_version,
                              model=model, max_tokens=3000, temperature=temperature)

    # ---------------------------------------------------------- 规则层入口（提前：面试题是否出题要听规则层的）
    compare_obj: LlmCompareOutput | None = compare_res.obj if compare_res.obj else None
    fit_obj: LlmFitOutput | None = fit_res.obj if fit_res.obj else None
    hard = check_hard_filters(job, parse, compare_obj)

    # ---------------------------------------------------------- ⑤ 面试题
    skipped, q_notes = questions_skip_reason(hard, compare_res.data)
    notes.extend(q_notes)
    q_res = None
    if not skipped:
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
    bonus = bonus_layer(job, parse, labeled, spans=pool)   # 匹配池只含简历原文+已锚定证据，模型的话不算（见 rules.bonus_layer）

    # ---------------------------------------------------------- 判定分（新公式的唯一入口）
    # ⚠️ 判定必须吃这个分。曾经这里的 decide() 用的是 rules.base_total（旧口径），
    #    新公式只被写进 payload「供展示」——结果注水型 44 份并列同分、空话率 0.889 的简历拿 72.3 分。
    #    rules.base_total 现在只用于报告与审计（维度分合计，不含加分项/扣分）。
    gate_total, parts = gate_score(job, [d.model_dump(mode="json") for d in rules.dimensions],
                                   [e.text for e in parse.evidence], bonus_rank=bonus_rank)

    stage_status = {"cover": cover.meta.validation.status if cover.meta else "failed",
                    "label": label_res_status, "compare": compare_res.status, "fit": fit_res.status}
    if q_res is not None:
        stage_status["questions"] = q_res.status

    # ---------------------------------------------------------- 证据引用
    # 注意：硬门槛命中的证据也要进 cited_evidence —— 它可能不被任何能力项引用，
    # 但契约要求"引用了就必须能查到"（100 份跑批时就是这里漏了，导致整份评估校验失败）。
    cited: list[CitedEvidence] = []
    seen: set[str] = set()
    for sid in [sid for h in hard for sid in h.evidence_span_ids]:
        sp = pool.get(sid)
        if sp and sid not in seen:
            seen.add(sid)
            cited.append(CitedEvidence(span_id=sp.span_id, text=sp.text, span=sp.span, page=sp.page,
                                       anchored=sp.anchored, anchor_method=sp.anchoring.method))
    for it in labeled + rules.unmapped:
        sid = it.get("span_id") if isinstance(it, dict) else getattr(it, "span_id", None)
        if not sid or sid in seen:
            continue
        seen.add(sid)
        sp = pool.get(sid)
        if sp is None:
            continue
        cited.append(CitedEvidence(span_id=sp.span_id, text=sp.text, span=sp.span, page=sp.page,
                                   anchored=sp.anchored, anchor_method=sp.anchoring.method))
    anchored_map = {c.span_id: c.anchored for c in cited}

    outcome = decide(job, gate_total, compare_obj, fit_obj, stage_status, set(anchored_map), hard,
                     dim_points={d.id: d.points for d in rules.dimensions},
                     bonus_names={b.name for b in bonus})
    outcome = sync_decisions_with_evidence(outcome, anchored_map,
                                           [d.best_span_id or "" for d in rules.dimensions if d.points > 0])
    # 稳定性闸门：没测过/不达标时不许自动淘汰（分数会漂，拿它刷人等于随机刷人）
    outcome = apply_gate(outcome, stability_latest(job.job_id, job.config_version),
                         job_id=job.job_id, config_version=job.config_version,
                         allow_auto_reject=bool(getattr(job.scoring, "auto_reject_enabled", False)))

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
        gate_score=gate_total,
        score_parts=ScoreParts(**parts),
        bonus_hits=bonus, bonus_points=round(sum(b.points for b in bonus), 1),
        total_with_bonus=round(rules.base_total + sum(b.points for b in bonus), 1),
        unmapped_items=[*rules.unmapped] + [
            UnmappedItem(item_id=it.get("item_id") or "", capability=it.get("capability") or "",
                         relevance=it.get("relevance") or "无关", difficulty=it.get("difficulty") or "低",
                         span_id=None, reason="锚定失败 → 未参与计分（只扣该条，不阻断其余证据）")
            for it in dropped],
        coverage=cover.coverage,
        fit=fit_obj,
        cited_evidence=cited,
        llm_compare=compare_obj,
        questions=[Question(**q.model_dump()) for q in q_res.obj.questions] if (q_res and q_res.obj) else [],
        questions_skipped_reason=skipped,
        conflicts=outcome.conflicts,
        model_id=compare_res.meta.model_id, prompt_version=PROMPT_VERSION, temperature=temperature,
        stages=EvalStages(cover=cover.meta, label=label_meta, compare=compare_res.meta, fit=fit_res.meta,
                          questions=q_res.meta if q_res else None),
        labeling=LabelingMeta(
            method="single" if label_stats["repeats"] <= 1 else "median_majority",
            repeats=label_stats["repeats"],
            dimension_agreement_mean=label_stats["dimension_agreement_mean"],
            per_item_agreement_min=label_stats["per_item_agreement_min"]),
        scoring_snapshot=ScoringSnapshot(
            aggregation=sc.dimension_aggregation, pass_line=sc.pass_line, pass_line_source=sc.pass_line_source,
            band_width=sc.band_width, pass_threshold=sc.pass_line + sc.band_width,
            reject_threshold=sc.pass_line - sc.band_width, confidence_floor=sc.confidence_floor,
            compare_repeats=n_compare,
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
