# -*- coding: utf-8 -*-
"""③ 评估契约：Evaluation（域对象）= 判定 + 理由 + 证据引用 + 置信度 + 各维度分数 + 硬门槛结果 + 匹配度。

口径：
- 判定（verdict/decision）是**规则层**产出的，不让 LLM 直接决定"淘汰"（双通道：LLM 整体评估 + 规则分）。
- 证据引用一律用 span_id 指向 ResumeParse.evidence，并在 `cited_evidence` 里留一份自包含副本，
  报告不用回查解析结果就能渲染、能高亮、能追溯。
- 契约里写死两条安全约束（model_validator 强制）：
  1) 自动淘汰必须有锚定成功的证据；未锚定证据 → 只能转人工；
  2) 自动达标/自动淘汰不许由降级（degraded/failed）的模型输出支撑 → 转人工。
"""
from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from .common import (
    CharRange,
    Decision,
    Difficulty,
    Relevance,
    SCHEMA_VERSION,
    StageMeta,
    Version,
    Strict,
    ValidationStatus,
    Verdict,
    utcnow,
)
from .llm_outputs import LlmCompareOutput, LlmFitOutput, Question

DISCLAIMER = "本报告由 LLM 自动生成，仅供参考，不参与录用决策；结论须由 HR 复核。"


# ---------------------------------------------------------------- 硬门槛
class HardFilterResult(Strict):
    """硬门槛结果：pass / fail / unknown 三态。unknown（证据不足）不自动淘汰。"""

    id: str
    name: str
    rule: str
    veto: bool = True
    result: Literal["pass", "fail", "unknown"]
    reason: str = ""
    evidence_span_ids: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------- 维度分数
class DimensionItemScore(Strict):
    """进维度分的单条能力项：单项分 = 相关性系数 × 难度系数（解释标签，非模型直接打的分）。"""

    item_id: str
    capability: str
    relevance: Relevance
    difficulty: Difficulty
    raw: float = Field(ge=0.0, le=3.0)
    span_id: str | None = None
    reason: str = ""
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)


class DimensionScore(Strict):
    id: str
    name: str
    weight: float = Field(ge=0, le=100)
    aggregation: Literal["best", "top2_mean"]
    score: float = Field(ge=0.0, le=3.0, description="聚合后的原始分（0~3）")
    rate: float = Field(ge=0.0, le=1.0, description="得分率 = score / 3")
    points: float = Field(ge=0.0)
    best_item_id: str | None = Field(default=None, description="产生该维度最高分的条目——报告必须引用它，不是第一条")
    best_span_id: str | None = None
    items: list[DimensionItemScore] = Field(default_factory=list)


class UnmappedItem(Strict):
    """没归到任何维度的条目：留空并单列，不硬塞进权重最高的维度。"""

    item_id: str
    capability: str
    relevance: Relevance
    difficulty: Difficulty
    span_id: str | None = None
    reason: str = ""


class BonusHit(Strict):
    name: str
    points: float = Field(ge=0)
    matched_keywords: list[str] = Field(default_factory=list)
    applied: bool = True
    evidence_span_ids: list[str] = Field(default_factory=list,
                                         description="命中的原文证据 span（加分也要能点开原文）")


# ---------------------------------------------------------------- 证据引用
class CitedEvidence(Strict):
    span_id: str
    text: str
    span: CharRange | None = None
    page: int | None = None
    anchored: bool = False
    anchor_method: Literal["exact", "ws_insensitive", "fuzzy", "none"] = "none"


# ---------------------------------------------------------------- 冲突与降级
ConflictType = Literal[
    "llm_vs_rule",          # 整体评估与规则分打架
    "fit_vs_verdict",       # 匹配度与整体评估打架
    "low_confidence",       # 置信度低于阈值
    "veto_triggered",       # 触发硬门槛
    "veto_override",        # 触发硬门槛但命中豁免（综合实力足够强）→ 转人工而非淘汰
    "degraded_validation",  # 某段模型输出降级/失败
    "unanchored_evidence",  # 存在锚定失败的证据
    "edge_band",            # 落在达标线 ±band 之内
    "edge_band_low_reject",  # 模型判边缘但分数低于线−band，按岗位策略自动淘汰（reject_border_when_low）
]


class Conflict(Strict):
    type: ConflictType
    detail: str


class ScoringSnapshot(Strict):
    """这一行的分数是怎么算出来的（复现历史分数的全部参数）。"""

    aggregation: Literal["best", "top2_mean"]
    pass_line: float
    pass_line_source: Literal["manual_default", "anchor_calibrated"]
    band_width: float
    pass_threshold: float = Field(description="pass_line + band_width：≥ 它且整体达标 → 自动达标")
    reject_threshold: float = Field(description="pass_line - band_width：< 它且整体不达标 → 自动淘汰")
    confidence_floor: float
    config_version: str
    gate_score_field: Literal["base_total"] = "base_total"


class LabelingMeta(Strict):
    """这一次评估的标注是怎么得到的（稳定性可追溯）。"""

    method: Literal["single", "median_majority"] = "single"
    repeats: int = Field(default=1, ge=1)
    dimension_agreement_mean: float | None = Field(default=None, ge=0, le=1,
        description="同一份解析多次标注时，条目维度归属的平均一致率；<0.8 说明维度划分本身不稳")
    per_item_agreement_min: float | None = Field(default=None, ge=0, le=1)


class EvalQuality(Strict):
    gate_passed: bool
    issues: list[str] = Field(default_factory=list)
    evidence_coverage: float = Field(ge=0.0, le=1.0)
    unanchored_items: int = Field(ge=0)


class EvalStages(Strict):
    label: StageMeta
    compare: StageMeta
    fit: StageMeta
    questions: StageMeta | None = None


# ---------------------------------------------------------------- 主对象
class Evaluation(Strict):
    schema_version: Version = SCHEMA_VERSION
    eval_id: str = Field(min_length=1)
    resume_id: str = Field(min_length=1)
    job_id: str = Field(min_length=1)
    job_config_version: str = Field(min_length=1)
    company_id: str = "local"
    created_at: str = Field(default_factory=utcnow)

    # ---- 判定 / 理由 / 置信度
    verdict: Verdict
    decision: Decision = Field(description="auto_pass / manual_review / auto_reject")
    decision_reason: str = Field(min_length=2, description="一句话说清为什么是这个处置")
    reasons: list[str] = Field(default_factory=list, description="理由明细：LLM 判定理由 + 规则层判据")
    confidence: float = Field(ge=0.0, le=1.0, description="采用 LLM 整体判定的置信度；校验降级时为 0.0（强制转人工）")

    # ---- 硬门槛 / 维度分数 / 匹配度
    hard_filters: list[HardFilterResult] = Field(default_factory=list)
    dimensions: list[DimensionScore] = Field(min_length=1)
    base_total: float = Field(ge=0.0, description="规则层总分（不含加分项）；与达标线比较的就是它")
    bonus_hits: list[BonusHit] = Field(default_factory=list)
    bonus_points: float = Field(default=0.0, ge=0.0)
    total_with_bonus: float = Field(default=0.0, ge=0.0, description="仅供展示，不参与判定")
    unmapped_items: list[UnmappedItem] = Field(default_factory=list)
    fit: LlmFitOutput | None = Field(default=None, description="匹配度：技能画像 vs 岗位方向")

    # ---- 证据引用
    cited_evidence: list[CitedEvidence] = Field(default_factory=list)

    # ---- LLM 原始判定（留档，便于复核与回归对比）
    llm_compare: LlmCompareOutput | None = None
    questions: list[Question] = Field(default_factory=list)
    questions_skipped_reason: str = ""

    # ---- 冲突 / 降级
    conflicts: list[Conflict] = Field(default_factory=list)

    # ---- 复现与审计
    model_id: str
    prompt_version: str
    temperature: float = 0.0
    stages: EvalStages
    labeling: LabelingMeta = Field(default_factory=LabelingMeta)
    scoring_snapshot: ScoringSnapshot
    quality: EvalQuality
    disclaimer: str = DISCLAIMER

    # ------------------------------------------------------------ 契约约束
    @model_validator(mode="after")
    def _check(self) -> "Evaluation":
        # 1) 维度分合计 = 规则总分
        s = round(sum(d.points for d in self.dimensions), 1)
        if abs(s - round(self.base_total, 1)) > 0.05:
            raise ValueError(f"维度得分合计 {s} 与 base_total {self.base_total} 不一致")

        # 2) 判定与处置必须自洽
        if self.decision == "auto_pass" and self.verdict != "达标":
            raise ValueError("auto_pass 必须对应判定=达标")
        if self.decision == "auto_reject" and self.verdict != "不达标":
            raise ValueError("auto_reject 必须对应判定=不达标")

        # 3) 安全约束：自动处置不许建立在"未锚定证据"或"降级输出"之上
        if self.decision in ("auto_pass", "auto_reject"):
            for st in (self.stages.label, self.stages.compare, self.stages.fit):
                if st.validation.status in ("degraded", "failed"):
                    raise ValueError(f"{st.stage} 阶段输出 {st.validation.status}：自动处置不被允许（应转人工）")
            anchored = {c.span_id: c.anchored for c in self.cited_evidence}
            if self.decision == "auto_reject":
                bad = [sid for sid in self._cited_by_decision() if not anchored.get(sid, False)]
                if bad:
                    raise ValueError(f"自动淘汰引用了未锚定证据：{bad} → 应转人工")
            else:   # auto_pass：每条计分维度的"最强证据"必须锚定成功
                weak = [d.id for d in self.dimensions if d.points > 0 and not anchored.get(d.best_span_id or "", False)]
                if weak:
                    raise ValueError(f"自动达标但维度 {weak} 的最强证据未锚定 → 应转人工")
                if self.fit is not None and self.fit.verdict == "方向不符":
                    raise ValueError("匹配度判方向不符时不许自动达标（两路打架一律交人）")

        # 4) 引用完整性
        ids = {c.span_id for c in self.cited_evidence}
        for d in self.dimensions:
            for it in d.items:
                if it.span_id and it.span_id not in ids:
                    raise ValueError(f"维度 {d.id} 的条目 {it.item_id} 引用了不在 cited_evidence 里的 {it.span_id}")
        for h in self.hard_filters:
            for sid in h.evidence_span_ids:
                if sid not in ids:
                    raise ValueError(f"硬门槛 {h.id} 引用了不在 cited_evidence 里的 {sid}")
        return self

    def _cited_by_decision(self) -> set[str]:
        used = {d.best_span_id for d in self.dimensions if d.best_span_id}
        for d in self.dimensions:
            used |= {i.span_id for i in d.items if i.span_id}
        for h in self.hard_filters:
            used |= set(h.evidence_span_ids)
        return used

    # ------------------------------------------------------------ 便捷
    @property
    def stage_status(self) -> dict[str, ValidationStatus]:
        out = {s.stage: s.validation.status for s in (self.stages.label, self.stages.compare, self.stages.fit)}
        if self.stages.questions:
            out["questions"] = self.stages.questions.validation.status
        return out

    def conflict_types(self) -> list[str]:
        return [c.type for c in self.conflicts]
