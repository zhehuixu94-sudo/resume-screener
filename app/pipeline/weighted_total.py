# -*- coding: utf-8 -*-
"""第二步：新总分公式——基础项（60，固定权重×最强相关）+ 加分项（40，本批分位归一）− 注水扣分。

为什么这么改（用户方案 + 实测修正）：
1. **加法代替乘法**：旧口径「项得分 = 基础分 × 相关 × 难度」会让基础项没覆盖直接清零加分项，
   一处低分把整项拉垮。改成两段独立、相加。
2. **基础项不除以数量**：等分制（60÷及格项数）下每项价值随及格项数变化、颗粒太大（12 分/项）
   容易顶格。改成按维度固定权重（HR 可解释），维度内取「最强的一条证据」定这一维的达成度。
3. **加分项独立归一**：`Σ rel×难度` 在本批内做**分位归一**（不是 Min-Max——实测模型难度值 88%
   挤在 0.5~0.9，Min-Max 会把噪声拉满；分位对离群点免疫）。
4. **注水扣分独立成项**：见 app/pipeline/honesty.py——这是唯一能治「术语堆砌排前面」的信号。

注意：本模块只负责**算分**；处置（硬门槛/反注水闸门/绝对底线+配额排名）在第三步。
"""
from __future__ import annotations

from .honesty import honesty_penalty

BASE_BUDGET = 60.0          # 基础项满分
BONUS_BUDGET = 40.0         # 加分项满分


def base_points(dimensions: list[dict], weights: dict[str, float],
                *, budget: float = BASE_BUDGET) -> float:
    """基础项：Σ_维度 [ budget × (维度权重/权重和) × 该维度最强证据的相关度 ]。

    维度没有证据 → 0 分（不是"给个中位分"）。
    用「最强的一条」而不是平均：一条强证据足以证明这一维达成，多余的弱证据不该拉低它。
    """
    tot = sum(weights.values()) or 1.0
    got = 0.0
    for d in dimensions:
        w = weights.get(d.get("id", ""), 0.0)
        rels = [float(i.get("relevance_score") or 0.0) for i in (d.get("items") or [])]
        got += budget * (w / tot) * (max(rels) if rels else 0.0)
    return round(got, 2)


def bonus_raw(dimensions: list[dict]) -> float:
    """加分项的原始量：Σ 每条的 相关度 × 难度（不归一、不截断）。"""
    return sum(float(i.get("relevance_score") or 0.0) * float(i.get("difficulty_score") or 0.0)
               for d in dimensions for i in (d.get("items") or []))


def percentile_rank(raw: dict[str, float]) -> dict[str, float]:
    """把一批简历的加分原始量映射到 0~1（本批分位）。

    为什么按批：难度值本身分辨力差（实测 88% 挤在 0.5~0.9），所以**只看相对位置**；
    分位比 Min-Max 抗离群（一份吹得特别猛的简历不会把别人全压低）。
    单份/空批 → 0.5（不偏不倚，避免样本不足时给出极端分）。
    """
    if not raw:
        return {}
    if len(raw) == 1:
        return {next(iter(raw)): 0.5}
    order = sorted(raw, key=lambda k: raw[k])
    n = len(order) - 1
    out: dict[str, float] = {}
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and raw[order[j + 1]] == raw[order[i]]:
            j += 1
        # 并列取**平均位置**：分数一样的简历必须拿到一样的加分，否则等于按文件名排序发分
        avg = ((i + j) / 2) / n if n else 0.5
        for k in order[i:j + 1]:
            out[k] = avg
        i = j + 1
    return out


def total_score(base: float, rank01: float, penalty: float, *,
                bonus_budget: float = BONUS_BUDGET) -> float:
    """总分 = 基础项 + 加分项 − 注水扣分（下界 0，上界 100）。"""
    return round(max(0.0, min(100.0, base + bonus_budget * rank01 - penalty)), 2)


def score_batch(resumes: list[dict], weights: dict[str, float], *,
                honesty_signals: dict[str, dict] | None = None, penalty_cap: float = 30.0,
                penalty_signal: str = "fluff") -> dict[str, dict]:
    """一次算一批：resumes = [{"key":..., "dimensions":[...]}]，返回 {key: {...拆解...}}。

    分成一批算是必须的——加分项用的是本批分位。
    """
    raw = {r["key"]: bonus_raw(r["dimensions"]) for r in resumes}
    rank = percentile_rank(raw)
    out = {}
    for r in resumes:
        k = r["key"]
        b = base_points(r["dimensions"], weights)
        pen = honesty_penalty(honesty_signals.get(k, {}), cap=penalty_cap, signal=penalty_signal) \
            if honesty_signals else 0.0
        out[k] = {
            "base": b,
            "bonus_raw": round(raw[k], 3),
            "rank01": round(rank[k], 4),
            "bonus": round(BONUS_BUDGET * rank[k], 2),
            "penalty": round(pen, 2),
            "total": total_score(b, rank[k], pen),
        }
    return out
