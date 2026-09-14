# -*- coding: utf-8 -*-
"""等分制评分（HR 口径）：每项基础分 = 及格分 ÷ 及格项目数，项得分 = 基础分 × 相似度 × 难度。

为什么另起一套而不是改现有口径：现有口径是「维度权重 × 最强项质量」，维度权重是拍出来的
（30/20/20/10/10/10），所以总分天然往中间挤、单峰。等分制的思路是：
**先定"及格要满足哪些项"，再把及格分等分给这些项**——一项满配就拿到它那一份，
满足全部及格项 = 恰好及格分。权重的争议被"及格项目清单"取代（可人工勾选，也可从岗位维度初始化）。

系数（HR 校准过的口径，别自己改回去）：
  · 相似度 relevance_score ∈ [0, 1]：强 = 1.0（基准）；
  · 难度   difficulty_score ∈ [0.5, 1.5]：**中 = 1.0 是基准**，低 = 0.5，高 = 1.5，中间**线性取值**
    （0.53 / 0.6 / 1.15 这种都要能表达）——三档离散会把不同深度的经历抹成同一档；
  · **质量基准 = 相似度强 × 难度中 = 1.0**：一项"强相关 + 中等难度"恰好拿到它那一份基础分；
    高难度项可以拿到 1.5 份（上限 1.5），低难度只有 0.5 份 —— 所以总分上限不是 60，而是 60×1.5 = 90。
  · 项得分 = (及格分 / N) × 相似度分 × 难度分。

本模块只做**离线计算**（0 token）：输入是落库的标注结果，输出是与规则层同构的 RuleOutcome，
这样双通道判定、硬门槛、冲突检测、三段带全都不用改。
"""
from __future__ import annotations

from ..contracts.evaluation import DimensionItemScore, DimensionScore, UnmappedItem
from ..contracts.job_config import JobConfig
from .rules import DIFFICULTY_SCORE, RELEVANCE_SCORE, RuleOutcome, _aggregate

MAX_QUALITY = 1.5                        # 相似度强(1.0) × 难度高(1.5)：单项最多拿 1.5 份基础分
DEFAULT_BUDGET = 60.0                    # 及格总分（= 每个及格项都"强相关 + 中等难度"时的得分）


def basic_items_of(job: JobConfig) -> list[str]:
    """及格项清单：默认取该岗位所有参与评分的维度（权重 > 0）。

    将来 HR 在界面上勾「基本项」时，这里换成勾选结果（basic_item_label 表）即可，
    算法本身不用动——这也是把它单独写成函数的原因。
    """
    return [d.id for d in job.dimensions if d.weight > 0]


def item_quality(i: dict) -> tuple[float, float, float]:
    """一条证据项的（相似度分、难度分、质量=两者之积）。连续值优先，缺了按标签兜底。"""
    rel = i.get("relevance_score")
    if rel is None:
        rel = RELEVANCE_SCORE.get(i.get("relevance"), 0.0)
    diff = i.get("difficulty_score")
    if diff is None:
        diff = DIFFICULTY_SCORE.get(i.get("difficulty"), 1.0)
    rel = min(max(float(rel), 0.0), 1.0)
    diff = min(max(float(diff), 0.5), 1.5)
    return rel, diff, round(rel * diff, 4)


def equal_split_outcome(job: JobConfig, labeled: list[dict], *, budget: float = DEFAULT_BUDGET,
                        basic_items: list[str] | None = None, aggregation: str = "best",
                        quality_cap: float = MAX_QUALITY) -> RuleOutcome:
    """等分制算分：返回与 rule_layer 同构的 RuleOutcome（可直接喂 decide()）。

    质量 = 相似度分 × 难度分，基准（强 × 中）= 1.0；**不顶格截断**（上限 1.5），
    这样才能把"高难度经历"和"中等难度"区分开——截断会把上限压回 1.0，分数重新挤成一团。
    """
    basics = basic_items or basic_items_of(job)
    n = len(basics) or 1
    per = budget / n
    by_id = {d.id: d for d in job.dimensions}

    rows: dict[str, list[DimensionItemScore]] = {b: [] for b in basics}
    unmapped: list[UnmappedItem] = []
    for i in labeled:
        rel_c, diff_c, quality = item_quality(i)
        rec = DimensionItemScore(
            item_id=i["item_id"], capability=i.get("capability") or "",
            relevance=i.get("relevance") or "无关", difficulty=i.get("difficulty") or "低",
            raw=quality, relevance_score=rel_c, difficulty_score=diff_c,
            span_id=i.get("span_id"), reason=i.get("reason") or "", confidence=i.get("confidence"))
        dim = i.get("dimension")
        if dim in rows:
            rows[dim].append(rec)
        else:
            unmapped.append(UnmappedItem(item_id=rec.item_id, capability=rec.capability,
                                         relevance=rec.relevance, difficulty=rec.difficulty,
                                         span_id=rec.span_id, reason=rec.reason))

    dims: list[DimensionScore] = []
    total = 0.0
    for dim_id in basics:
        d = by_id.get(dim_id)
        items = rows[dim_id]
        # 该维度最强项的"质量"（已含相似度与难度）；没有证据 = 0 分（不是"缺省满分"）
        quality = min(_aggregate([x.raw for x in items], aggregation), quality_cap)
        points = round(per * quality, 2)
        total += points
        best = max(items, key=lambda x: x.raw, default=None)
        dims.append(DimensionScore(
            id=dim_id, name=d.name if d else dim_id, weight=round(per, 2), aggregation=aggregation,
            score=round(quality, 3), rate=round(quality, 3), points=points,
            best_item_id=best.item_id if best else None, best_span_id=best.span_id if best else None,
            items=items))
    return RuleOutcome(dimensions=dims, unmapped=unmapped, base_total=round(total, 2))


def explain(job: JobConfig, labeled: list[dict], *, budget: float = DEFAULT_BUDGET,
            basic_items: list[str] | None = None, aggregation: str = "best") -> dict:
    """把一份简历的等分制算分过程摊开（界面/报告要展示"每项拿了几分"）。"""
    out = equal_split_outcome(job, labeled, budget=budget, basic_items=basic_items,
                             aggregation=aggregation)
    basics = basic_items or basic_items_of(job)
    return {"budget": budget, "n_basic": len(basics), "per_item": round(budget / (len(basics) or 1), 2),
            "total": out.base_total,
            "items": [{"id": d.id, "name": d.name, "per_item": d.weight, "quality": d.score,
                       "points": d.points, "best_item_id": d.best_item_id,
                       "n_items": len(d.items)} for d in out.dimensions]}
