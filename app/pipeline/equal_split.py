# -*- coding: utf-8 -*-
"""等分制评分（HR 口径）：每项基础分 = 及格分 ÷ 及格项目数，项得分 = 基础分 × 相似度 × 难度。

为什么另起一套而不是改现有口径：现有口径是「维度权重 × 最强项质量」，维度权重是拍出来的
（30/20/20/10/10/10），所以总分天然往中间挤、单峰。等分制的思路是：
**先定"及格要满足哪些项"，再把及格分等分给这些项**——一项满配就拿到它那一份，
满足全部及格项 = 恰好及格分。权重的争议被"及格项目清单"取代（可人工勾选，也可从岗位维度初始化）。

名词对齐（跟现有规则层用同一套标签，避免两套词汇）：
  · 相似度 = relevance（强/中/弱/无关），系数 1.0 / 0.66 / 0.33 / 0；
  · 难度   = difficulty（高/中/低），系数 3 / 2 / 1 —— 但要做成**质量系数**必须归一化到 ≤1，
    否则"基础分 × 难度"会超过该项的基础分（1.0 是"满配"的上限）：高=1.0 / 中=0.667 / 低=0.333。
  · 项得分 = (及格分 / N) × 相似度系数 × 难度质量系数   →  满配 = 及格分 / N。

本模块只做**离线计算**（0 token）：输入是落库的标注结果，输出是与规则层同构的 RuleOutcome，
这样双通道判定、硬门槛、冲突检测、三段带全都不用改。
"""
from __future__ import annotations

from ..contracts.evaluation import DimensionItemScore, DimensionScore, UnmappedItem
from ..contracts.job_config import JobConfig
from .rules import DIFFICULTY_W, RELEVANCE_W, RuleOutcome, _aggregate

MAX_PRODUCT = max(RELEVANCE_W.values()) * max(DIFFICULTY_W.values())      # 3.0
DIFFICULTY_Q = {k: round(v / max(DIFFICULTY_W.values()), 3) for k, v in DIFFICULTY_W.items()}
DEFAULT_BUDGET = 60.0                    # 及格总分（= 满足全部及格项且质量满配时的得分）


def basic_items_of(job: JobConfig) -> list[str]:
    """及格项清单：默认取该岗位所有参与评分的维度（权重 > 0）。

    将来 HR 在界面上勾「基本项」时，这里换成勾选结果（basic_item_label 表）即可，
    算法本身不用动——这也是把它单独写成函数的原因。
    """
    return [d.id for d in job.dimensions if d.weight > 0]


def equal_split_outcome(job: JobConfig, labeled: list[dict], *, budget: float = DEFAULT_BUDGET,
                        basic_items: list[str] | None = None, aggregation: str = "best",
                        quality_cap: float = 1.0, quality_baseline: float | None = None) -> RuleOutcome:
    """等分制算分：返回与 rule_layer 同构的 RuleOutcome（可直接喂 decide()）。

    quality_baseline：把"合格基准质量"归一化成 1.0。默认 None = 强+高 才算满配（1.0）；
    传 0.44（= 中×中）时，"中/中"这一档就等同于"满足该项"，分数会以 budget 为中心——
    这是让"满足全部及格项 = 恰好及格分"真正成立的做法（见 tools/equal_split_sweep.py 的对照）。
    """
    basics = basic_items or basic_items_of(job)
    n = len(basics) or 1
    per = budget / n
    by_id = {d.id: d for d in job.dimensions}
    base = quality_baseline or 1.0

    rows: dict[str, list[DimensionItemScore]] = {b: [] for b in basics}
    unmapped: list[UnmappedItem] = []
    for i in labeled:
        rel = i.get("relevance")
        diff = i.get("difficulty")
        rel_c = RELEVANCE_W.get(rel, 0.0)
        diff_c = DIFFICULTY_Q.get(diff, DIFFICULTY_Q["低"])
        rec = DimensionItemScore(
            item_id=i["item_id"], capability=i.get("capability") or "", relevance=rel or "无关",
            difficulty=diff or "低", raw=round(rel_c * diff_c, 4), span_id=i.get("span_id"),
            reason=i.get("reason") or "", confidence=i.get("confidence"))
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
        quality = _aggregate([x.raw for x in items], aggregation) / base
        quality = min(quality, quality_cap)
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
