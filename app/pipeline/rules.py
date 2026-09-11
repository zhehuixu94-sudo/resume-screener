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
from ..contracts.resume_parse import ResumeParse  # noqa: TC001

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


def bonus_layer(job: JobConfig, parse: "ResumeParse", labeled: list[dict]) -> list[BonusHit]:
    """加分项：**只在简历原文 + 已锚定的证据片段里**匹配关键词。

    两条硬规矩（都是实测踩出来的）：
    1. 匹配池绝不能放模型自己的话（原来把 compare.reason/strengths/gaps 也拼进来了，
       等于模型夸候选人两句就能给他加分）；
    2. 命中判据按配置：must_match（OR-of-AND，所有组都要满足）优先，否则退化为"任意关键词"。
       纯"任意关键词"会让加分项人人有份（实测 100/100 全命中，加分彻底不区分人）。
    """
    # 池子：简历原文 + 已锚定条目的证据原文（未锚定的是模型转述，不算）
    pool = [parse.text]
    for it in labeled:
        sid = it.get("span_id") if isinstance(it, dict) else None
        if not sid:
            continue
        sp = next((e for e in parse.evidence if e.span_id == sid), None)
        if sp is not None and sp.anchored:
            pool.append(sp.text)
    low = " ".join(pool).lower()

    hits: list[BonusHit] = []
    for b in job.bonus or []:                                   # type: ignore[union-attr]
        matched: list[str] = []
        ok = False
        if b.must_match:
            groups_ok, matched = True, []
            for group in b.must_match:
                g_hit = [k for k in group if k.lower() in low]
                if not g_hit:
                    groups_ok = False
                    break
                matched.extend(g_hit[:2])
            ok = groups_ok
        elif b.keywords:
            matched = [k for k in b.keywords if k.lower() in low]
            ok = bool(matched)
        if not ok:
            continue
        # 证据：原文里含任一命中关键词的 span（最多 3 条，够点开看即可）
        ev_ids = [e.span_id for e in parse.evidence
                  if any(k.lower() in e.text.lower() for k in matched)][:3]
        hits.append(BonusHit(name=b.name, points=b.points, matched_keywords=matched[:5],
                             applied=True, evidence_span_ids=ev_ids))
    return hits


# ---------------------------------------------------------------- 判定
@dataclass
class DecisionOutcome:
    verdict: str
    decision: str
    reason: str
    conflicts: list[Conflict] = field(default_factory=list)
    hard_filters: list[HardFilterResult] = field(default_factory=list)


def _override_hit(job: JobConfig, fails: list[HardFilterResult], dims: dict[str, float],
                  bonus_names: set[str]) -> tuple[str, str] | None:
    """看豁免规则是否命中：维度分够高（且要求的加分项也命中）→ 返回（命中规则说明）。"""
    for o in job.overrides:
        scope = o.applies_to or [h.id for h in fails]
        if not any(h.id in scope for h in fails):
            continue
        if dims.get(o.when_dimension, 0.0) < o.min_points:
            continue
        if o.require_bonus and not set(o.require_bonus) <= bonus_names:
            continue
        return o.id, o.reason
    return None


def decide(job: JobConfig, base_total: float, compare: LlmCompareOutput | None, fit: LlmFitOutput | None,
           stage_status: dict[str, str], anchored_span_ids: set[str],
           hard_filters: list[HardFilterResult] | None = None,
           dim_points: dict[str, float] | None = None,
           bonus_names: set[str] | None = None) -> DecisionOutcome:
    """三段带 + 双通道判定。

    hard_filters 由 `pipeline.hard_filters.check_hard_filters()` 规则层给出（三态）：
      fail    → 自动淘汰（要有证据，未锚定会被 sync_decisions_with_evidence 降级为转人工）
      unknown → 转人工（证据不足，不许淘汰）
      pass    → 继续走分数判定
    模型自己说"触发了门槛"但规则层判通过时 → 转人工（两路打架一律交人，不自动放行也不自动淘汰）。
    """
    sc = job.scoring
    pass_thr = sc.pass_line + sc.band_width
    rej_thr = sc.pass_line - sc.band_width
    conflicts: list[Conflict] = []
    hard = hard_filters or []

    llm_v = compare.verdict if compare else "边缘"
    conf = compare.confidence if compare else 0.0
    model_veto = bool(compare and (compare.veto_triggered or compare.veto_filter_ids))

    # 0) 降级：任何一段模型输出降级/失败 → 只能转人工
    degraded = [k for k, v in stage_status.items() if v in ("degraded", "failed")]
    if degraded:
        conflicts.append(Conflict(type="degraded_validation", detail=f"以下阶段输出降级/失败：{degraded}"))
        return DecisionOutcome(verdict=llm_v if llm_v != "达标" else "边缘", decision="manual_review",
                               reason=f"模型输出降级（{'、'.join(degraded)}）→ 转人工复核，不做自动处置",
                               conflicts=conflicts, hard_filters=hard)

    # 1) 硬门槛：规则层 fail 才自动淘汰
    fails = [h for h in hard if h.veto and h.result == "fail"]
    unknown = [h for h in hard if h.veto and h.result == "unknown"]
    if fails:
        detail = "；".join(f"{h.name}：{h.reason}" for h in fails)
        # 综合达标的落点：单项不满足但整体实力足够强 → 不自动淘汰，交人判断
        hit = _override_hit(job, fails, dim_points or {}, bonus_names or set())
        if hit:
            rule_id, why = hit
            conflicts.append(Conflict(type="veto_override",
                                      detail=f"触发门槛（{'、'.join(h.id for h in fails)}）但命中豁免规则 {rule_id}：{why}"))
            return DecisionOutcome(verdict="边缘", decision="manual_review",
                                   reason=f"触发硬性门槛（{'、'.join(h.id for h in fails)}）：{detail}"
                                          f" —— 但{why} → 转人工综合判断，不自动淘汰",
                                   conflicts=conflicts, hard_filters=hard)
        conflicts.append(Conflict(type="veto_triggered", detail=detail))
        return DecisionOutcome(verdict="不达标", decision="auto_reject",
                               reason=f"触发硬性门槛（{'、'.join(h.id for h in fails)}）：{detail}",
                               conflicts=conflicts, hard_filters=hard)
    if unknown:
        detail = "；".join(f"{h.name}：{h.reason}" for h in unknown)
        conflicts.append(Conflict(type="veto_triggered", detail=f"证据不足：{detail}"))
        return DecisionOutcome(verdict="边缘", decision="manual_review",
                               reason=f"硬门槛证据不足（{'、'.join(h.id for h in unknown)}）→ 转人工核实：{detail}",
                               conflicts=conflicts, hard_filters=hard)
    if model_veto:
        conflicts.append(Conflict(type="llm_vs_rule",
                                  detail=f"规则层判所有硬门槛通过，但模型认为触发：{compare.veto_triggered if compare else ''}"))
        return DecisionOutcome(verdict="边缘", decision="manual_review",
                               reason="模型认为触发硬门槛，规则层关键词预检未确认 → 转人工确认",
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
