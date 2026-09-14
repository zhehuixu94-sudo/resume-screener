# -*- coding: utf-8 -*-
"""判定分（gate score）的**唯一入口**：线上 `evaluate()` 与离线 `replay` 必须都走这里。

为什么单独抽一层（这是一次真实事故的教训）：
第四步实现了「基础项60 + 加分项40(本批分位) − 注水扣分」的新公式，但**判定链没人调用它**——
`evaluate()` 仍然把旧的 `rules.base_total` 传给 `decide()`，新公式只是被写进 payload 的
`total_with_bonus` 字段"供展示"。结果：300 份对照语料里注水型 44 份并列同分、
空话率 0.889 的简历照样拿 72.3 分，而离线扫描却报"线 55 → 淘汰 61%"——线上实际淘汰 0%。

结论写死在代码里：**任何"算分"都必须只有一个函数，判定与展示共用它**；
`tests/test_e2e_gate.py` 里有一条集成测试盯住"改分数必须改变 decide() 的结果"。

分数构成（0~100）：
    基础项 = Σ_维度 [60 × (维度权重/权重和) × 该维度最强证据的相关度]     ← 每份简历自洽、可复现
    加分项 = 40 × 本批分位(Σ 相关度×难度)                                  ← 相对量，需整批上下文
    扣分   = 30 × 空话率^1.5                                              ← 非线性，见 honesty.fluff_penalty
单份评估拿不到"本批"时，加分项用中性位次 0.5；整批跑完后由 `app/pipeline/rescore.py` 回填真实分位。
"""
from __future__ import annotations

from ..contracts import JobConfig
from .honesty import resume_signals
from .weighted_total import (BASE_BUDGET, BONUS_BUDGET, NEUTRAL_RANK, PENALTY_CAP, PENALTY_EXPONENT,
                             base_points, bonus_raw, penalty_points, total_score)


def dimensions_from_labeled(cfg: JobConfig, labeled: list[dict]) -> list[dict]:
    """把标注结果按维度分组，还原成 `base_points()` 要的形状（维度内保留每条的相关度）。

    离线重放没有 `rules.DimensionScore` 对象，但算分只用到「维度 id + 该维度最强相关度」，
    所以这里按 dimension 分组即可，与线上口径一致（维度外的条目 = unmapped，不参与算分）。
    """
    by_dim: dict[str, list[dict]] = {}
    for it in labeled:
        dim = it.get("dimension") if isinstance(it, dict) else None
        if not dim:
            continue
        by_dim.setdefault(dim, []).append({"relevance_score": (it.get("relevance_score")
                                                               if isinstance(it, dict) else None)})
    order = [d.id for d in cfg.dimensions]
    return [{"id": dim, "items": by_dim[dim]} for dim in order if dim in by_dim]


def gate_score(cfg: JobConfig, dimensions: list[dict], evidence_texts: list[str], *,
               bonus_rank: float | None = None, penalty_cap: float = PENALTY_CAP,
               penalty_exponent: float = PENALTY_EXPONENT) -> tuple[float, dict]:
    """算判定分。返回 (总分, 拆解明细)。

    dimensions：`[{"id": 维度id, "items": [{"relevance_score": 0~1}, ...]}, ...]`
                （线上用 `rules.DimensionScore` 的 model_dump，离线用 dimensions_from_labeled）
    evidence_texts：这份简历的**全部**证据片段原文（含量化成就镜像条）——扣分信号按同样口径统计，
                否则线上与离线/抽检脚本会各算一套。
    """
    weights = {d.id: d.weight for d in cfg.dimensions}
    sig = resume_signals(evidence_texts or [])
    base = base_points(dimensions, weights, budget=BASE_BUDGET)
    pen = penalty_points(sig, cap=penalty_cap, exponent=penalty_exponent)
    rank01 = NEUTRAL_RANK if bonus_rank is None else max(0.0, min(1.0, float(bonus_rank)))
    total = total_score(base, rank01, pen, bonus_budget=BONUS_BUDGET)
    parts = {
        "base": base,
        "bonus_raw": round(bonus_raw(dimensions), 3),
        "rank01": round(rank01, 4),
        "bonus": round(BONUS_BUDGET * rank01, 2),
        "penalty": pen,
        "penalty_exponent": penalty_exponent,
        "fluff_rate": round(float(sig.get("fluff_rate", 0.0)), 4),
        "concreteness": round(float(sig.get("concreteness", 0.0)), 4),
        "n_spans": int(sig.get("n_spans", 0)),
        "total": total,
    }
    return total, parts
