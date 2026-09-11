# -*- coding: utf-8 -*-
"""规则层（不加 LLM）：单项分 → 维度分 → 加权总分 → 三段带判定。

为什么判定不让 LLM 直接下：LLM 的整体评估是"印象"，规则分是"算术"，两路互相制衡；
两者打架、或置信度低、或证据没锚定 → 一律转人工。自动淘汰只在"印象也不达标 + 算术也低分"时发生。

相对原型的修正：
- 维度分聚合做成配置项（best / top2_mean），不再把"取最强项"写死（任务 4 的开关）。
- 加分项关键词只从岗位配置读，不再用代码里的硬编码关键词表，也不做名称模糊匹配。
- 三段带（pass_line ± band）真正参与判定，不再只有一条线。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..contracts.evaluation import (
    BonusHit,
    Conflict,
    DimensionItemScore,
    DimensionScore,
    HardFilterResult,
    UnmappedItem,
)
from ..contracts.job_config import JobConfig
from ..contracts.llm_outputs import LlmCompareOutput, LlmFitOutput

RELEVANCE_W = {"强": 1.0, "中": 0.66, "弱": 0.33, "无关": 0.0}
DIFFICULTY_W = {"高": 3, "中": 2, "低": 1}
MAX_ITEM_SCORE = 3.0


@dataclass
class RuleOutcome:
    dimensions: list[DimensionScore] = field(default_factory=list)
    unmapped: list[UnmappedItem] = field(default_factory=list)
    base_total: float = 0.0


def _aggregate(raws: list[float], mode: str) -> float:
    if not raws:
        return 0.0
    if mode == "top2_mean":
        top2 = sorted(raws, reverse=True)[:2]
        return round(sum(top2) / len(top2), 3)
    return round(max(raws), 3)


def rule_layer(job: JobConfig, labeled: list[dict], *, aggregation: str | None = None) -> RuleOutcome:
    mode = aggregation or job.scoring.dimension_aggregation
    spans_by_item = {i["item_id"]: i.get("span_id") for i in labeled}

    rows: dict[str, dict] = {d.id: {"def": d, "items": []} for d in job.dimensions}
    unmapped: list[UnmappedItem] = []
    for i in labeled:
        raw = round(RELEVANCE_W.get(i.get("relevance"), 0.0) * DIFFICULTY_W.get(i.get("difficulty"), 1), 2)
        rec = DimensionItemScore(item_id=i["item_id"], capability=i.get("capability") or "",
                                 relevance=i.get("relevance") or "无关", difficulty=i.get("difficulty") or "低",
                                 raw=raw, span_id=i.get("span_id"), reason=i.get("reason") or "",
                                 confidence=i.get("confidence"))
        dim = i.get("dimension")
        if dim in rows:
            rows[dim]["items"].append(rec)
        else:
            unmapped.append(UnmappedItem(item_id=i["item_id"], capability=rec.capability, relevance=rec.relevance,
                                         difficulty=rec.difficulty, span_id=rec.span_id, reason=rec.reason))

    dims: list[DimensionScore] = []
    total = 0.0
    for dim_id, row in rows.items():
        d = row["def"]
        items: list[DimensionItemScore] = row["items"]
        score = _aggregate([x.raw for x in items], mode)
        rate = score / MAX_ITEM_SCORE
        points = round(d.weight * rate, 1)
        total += points
        best = max(items, key=lambda x: x.raw, default=None)
        dims.append(DimensionScore(id=dim_id, name=d.name, weight=d.weight, aggregation=mode, score=score,
                                   rate=round(rate, 3), points=points,
                                   best_item_id=best.item_id if best else None,
                                   best_span_id=best.span_id if best else None, items=items))
    dims.sort(key=lambda x: -x.weight)
    return RuleOutcome(dimensions=dims, unmapped=unmapped, base_total=round(total, 1))


def bonus_layer(job: JobConfig, resume_text: str, labeled: list[dict], compare: LlmCompareOutput | None) -> list[BonusHit]:
    """加分项：关键词在【简历全文】里匹配（关键词由 HR 在配置里显式声明，不猜）。"""
    blob = " ".join([resume_text] + [f"{i.get('evidence','')} {i.get('capability','')}" for i in labeled])
    if compare is not None:
        blob += " " + " ".join([compare.reason, *compare.strengths, *compare.gaps])
    low = blob.lower()
    hits: list[BonusHit] = []
    for b in job.bonus:                     # type: ignore[union-attr]
        matched = [k for k in b.keywords if k.lower() in low]
        if matched:
            hits.append(BonusHit(name=b.name, points=b.points, matched_keywords=matched[:5], applied=True))
    return hits


# ---------------------------------------------------------------- 判定
@dataclass
class DecisionOutcome:
    verdict: str
    decision: str
    reason: str
    conflicts: list[Conflict] = field(default_factory=list)
    hard_filters: list[HardFilterResult] = field(default_factory=list)


def decide(job: JobConfig, base_total: float, compare: LlmCompareOutput | None, fit: LlmFitOutput | None,
           stage_status: dict[str, str], anchored_span_ids: set[str]) -> DecisionOutcome:
    sc = job.scoring
    pass_thr = sc.pass_line + sc.band_width
    rej_thr = sc.pass_line - sc.band_width
    conflicts: list[Conflict] = []

    # 硬门槛结果：模型定位到具体门槛才算 fail；只给了描述没给 id → unknown（不许据此自动淘汰）
    veto_text = compare.veto_triggered if compare else None
    veto_ids = list(compare.veto_filter_ids) if compare else []
    hard: list[HardFilterResult] = []
    for f in job.hard_filters:
        if veto_ids:
            res = "fail" if f.id in veto_ids else "pass"
            reason = "整体判定指出该门槛被触发" if res == "fail" else "整体判定未指出触发"
        elif veto_text:
            res, reason = "unknown", "指出触发硬门槛但未定位到具体门槛"
        else:
            res, reason = "pass", "整体判定未指出触发任何硬门槛"
        hard.append(HardFilterResult(id=f.id, name=f.name, rule=f.rule, veto=f.veto, result=res, reason=reason))

    llm_v = compare.verdict if compare else "边缘"
    conf = compare.confidence if compare else 0.0

    # 0) 降级：任何一段模型输出降级/失败 → 只能转人工
    degraded = [k for k, v in stage_status.items() if v in ("degraded", "failed")]
    if degraded:
        conflicts.append(Conflict(type="degraded_validation", detail=f"以下阶段输出降级/失败：{degraded}"))
        return DecisionOutcome(verdict=llm_v if llm_v != "达标" else "边缘", decision="manual_review",
                               reason=f"模型输出降级（{'、'.join(degraded)}）→ 转人工复核，不做自动处置",
                               conflicts=conflicts, hard_filters=hard)

    # 1) 硬门槛
    if veto_ids:
        conflicts.append(Conflict(type="veto_triggered", detail=veto_text or "触发硬门槛"))
        return DecisionOutcome(verdict="不达标", decision="auto_reject",
                               reason=f"触发硬性门槛（{'、'.join(veto_ids)}）：{veto_text}", conflicts=conflicts,
                               hard_filters=hard)
    if veto_text:
        conflicts.append(Conflict(type="veto_triggered", detail=f"未定位门槛：{veto_text}"))
        return DecisionOutcome(verdict="边缘", decision="manual_review",
                               reason=f"指出触发硬门槛但无法定位具体门槛（{veto_text}）→ 转人工确认",
                               conflicts=conflicts, hard_filters=hard)

    llm_pass, llm_border = llm_v == "达标", llm_v == "边缘"
    in_band = rej_thr <= base_total < pass_thr
    fit_mismatch = bool(fit and fit.verdict == "方向不符")

    # 2) 匹配度与整体判定打架：不自动淘汰
    if fit_mismatch:
        if llm_pass:
            conflicts.append(Conflict(type="fit_vs_verdict", detail=f"匹配度方向不符（{fit.fit_ratio}）但整体判定达标"))
            return DecisionOutcome(verdict="边缘", decision="manual_review",
                                   reason="匹配度检测与整体判定冲突（方向不符 vs 达标）→ 转人工确认，不自动淘汰",
                                   conflicts=conflicts, hard_filters=hard)
        if llm_border or base_total >= rej_thr:
            conflicts.append(Conflict(type="fit_vs_verdict", detail=f"方向不符（{fit.fit_ratio}），规则分 {base_total} 未低到可自动淘汰"))
            return DecisionOutcome(verdict="边缘", decision="manual_review",
                                   reason=f"方向疑似不符（匹配率 {fit.fit_ratio}）且分数不低 → 转人工确认",
                                   conflicts=conflicts, hard_filters=hard)
        conflicts.append(Conflict(type="fit_vs_verdict", detail=f"方向不符（{fit.fit_ratio}）且规则分 {base_total} < {rej_thr}"))
        return DecisionOutcome(verdict="不达标", decision="auto_reject",
                               reason=f"方向不符（技能画像与岗位无交集，匹配率 {fit.fit_ratio}）且规则分 {base_total} < {rej_thr} → 自动淘汰，可申诉",
                               conflicts=conflicts, hard_filters=hard)

    # 3) 双通道（整体判定 × 规则分 + 三段带）
    if llm_pass and base_total >= pass_thr:
        return DecisionOutcome(verdict="达标", decision="auto_pass",
                               reason=f"整体判定达标且规则分 {base_total} ≥ {pass_thr}（达标线 {sc.pass_line} + {sc.band_width}）",
                               conflicts=conflicts, hard_filters=hard)

    if llm_pass and base_total < pass_thr:
        conflicts.append(Conflict(type="edge_band" if in_band else "llm_vs_rule",
                                  detail=f"整体达标但规则分 {base_total} 未过 {pass_thr}"))
        return DecisionOutcome(verdict="边缘", decision="manual_review",
                               reason=f"判据不一致（整体评估=达标 / 规则分 {base_total} < {pass_thr}）→ 转人工确认",
                               conflicts=conflicts, hard_filters=hard)

    if llm_border:
        conflicts.append(Conflict(type="edge_band", detail=f"整体评估=边缘，规则分 {base_total}"))
        return DecisionOutcome(verdict="边缘", decision="manual_review",
                               reason=f"整体评估为边缘（最像 {compare.closest_anchor if compare else '—'} 档）→ 转人工",
                               conflicts=conflicts, hard_filters=hard)

    # 4) 整体不达标
    if base_total >= rej_thr:
        conflicts.append(Conflict(type="edge_band", detail=f"整体不达标但规则分 {base_total} ≥ {rej_thr}"))
        return DecisionOutcome(verdict="边缘", decision="manual_review",
                               reason=f"落在线内（规则分 {base_total} ≥ {rej_thr}）→ 转人工",
                               conflicts=conflicts, hard_filters=hard)

    if conf < sc.confidence_floor:
        conflicts.append(Conflict(type="low_confidence", detail=f"置信度 {conf} < {sc.confidence_floor}"))
        return DecisionOutcome(verdict="边缘", decision="manual_review",
                               reason=f"整体不达标且规则分 {base_total} < {rej_thr}，但置信度 {conf} 低于 {sc.confidence_floor} → 转人工",
                               conflicts=conflicts, hard_filters=hard)

    return DecisionOutcome(verdict="不达标", decision="auto_reject",
                           reason=f"整体判定不达标且规则分 {base_total} < {rej_thr}（达标线 {sc.pass_line} − {sc.band_width}）→ 自动淘汰，可申诉",
                           conflicts=conflicts, hard_filters=hard)


def sync_decisions_with_evidence(outcome: DecisionOutcome, cited_anchored: dict[str, bool],
                                  best_spans: list[str]) -> DecisionOutcome:
    """证据侧安全兜底：自动淘汰若有任何未锚定引用 → 降级为转人工（契约里也有同样的硬约束）。"""
    if outcome.decision == "auto_reject":
        bad = sorted({s for s, ok in cited_anchored.items() if not ok})
        if bad:
            outcome.decision = "manual_review"
            outcome.verdict = "边缘"
            outcome.reason = f"存在未锚定证据 {bad[:3]}（共 {len(bad)} 条），不允许自动淘汰 → 转人工"
            outcome.conflicts.append(Conflict(type="unanchored_evidence", detail=f"未锚定证据：{bad[:5]}"))
    if outcome.decision == "auto_pass":
        weak = [s for s in best_spans if s and not cited_anchored.get(s, False)]
        if weak:
            outcome.decision = "manual_review"
            outcome.verdict = "边缘"
            outcome.reason = f"计分维度的最强证据 {weak[:3]} 未锚定 → 转人工"
            outcome.conflicts.append(Conflict(type="unanchored_evidence", detail=f"最强证据未锚定：{weak[:5]}"))
    return outcome
