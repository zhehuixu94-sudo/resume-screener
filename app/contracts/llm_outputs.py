# -*- coding: utf-8 -*-
"""五段模型输出契约（LLM 侧的"必须返回什么"）。

口径：
- 这里是**模型必须返回**的字段，校验失败先重试（带错误反馈），仍失败才降级。
- 枚举被钉死。模型常见的同义写法（"高/较高"、"符合"）由 `app/contracts/coerce.py` 在
  校验前归一，归一动作记进 ValidationReport.repairs——不静默放过。
- 所有"可选"字段都满足一条：缺省值与"缺失"的语义一致（如 veto_triggered 缺失 = 未触发）。
  其余字段一律 required，模型漏字段就是契约违约。
"""
from __future__ import annotations

from pydantic import Field

from .common import AnchorLevel, Difficulty, FitVerdict, QLevel, Relevance, Tolerant, Verdict


# ---------------------------------------------------------------- ① 解析
class LlmParseItem(Tolerant):
    capability: str = Field(min_length=2, description="能力项名称")
    evidence: str = Field(min_length=4, max_length=80, description="简历原文连续片段，≤60 字，不得改写/拼接")
    note: str = ""


class LlmParseBlock(Tolerant):
    section: str = Field(min_length=1, description="段落类型：教育背景/项目经历/实习经历/技能/…")
    title: str = Field(min_length=1)
    period: str = ""
    items: list[LlmParseItem] = Field(min_length=1)


class LlmParseQuality(Tolerant):
    sections_found: list[str] = Field(min_length=1)
    issues: list[str] = Field(default_factory=list)


class LlmParseOutput(Tolerant):
    blocks: list[LlmParseBlock] = Field(min_length=1)
    parse_quality: LlmParseQuality


# ---------------------------------------------------------------- ② 标注
class LlmLabelItem(Tolerant):
    idx: int = Field(ge=1, description="对应输入清单的编号，用于对齐，避免顺序错位")
    block: str = ""
    capability: str = ""
    evidence: str = ""
    relevance: Relevance
    difficulty: Difficulty
    relevance_score: float | None = Field(
        default=None, ge=0.0, le=1.0,
        description="相似度的**连续值**（0~1）：强=1.0 是基准，中间自己取（0.53、0.6…）。"
                    "不填则由 relevance 标签推导。三档离散会把「沾边程度不同」的人抹平。")
    difficulty_score: float | None = Field(
        default=None, ge=0.5, le=1.5,
        description="难度的**连续值**（0.5~1.5）：中=1.0、低=0.5、高=1.5，中间线性取值。"
                    "不填则由 difficulty 标签推导。三档离散会让项得分只有 0.5/1.0/1.5 三种倍数。")
    dimension: str | None = Field(default=None, description="必须命中岗位配置的维度 id；映射不到时为 null（该条目进 unmapped 单列，不硬塞进权重最高的维度）")
    reason: str = Field(min_length=2)
    confidence: float = Field(ge=0.0, le=1.0)


class LlmLabelOutput(Tolerant):
    items: list[LlmLabelItem] = Field(min_length=1)


# ---------------------------------------------------------------- ③ 整体判定（相对比较）
class LlmCompareOutput(Tolerant):
    verdict: Verdict = Field(description="整体判定：达标 / 边缘 / 不达标")
    closest_anchor: AnchorLevel = Field(description="最像哪一档锚点")
    confidence: float = Field(ge=0.0, le=1.0)
    veto_triggered: str | None = Field(default=None, description="触发硬门槛的人话说明；未触发必须为 null")
    veto_filter_ids: list[str] = Field(default_factory=list,
                                       description="被触发的硬门槛 id（必须来自给定清单）；无法定位具体门槛时留空 → 判定层会转人工，不自动淘汰")
    reason: str = Field(min_length=10, description="2~4 句，必须引用候选人的具体条目")
    strengths: list[str] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------- ④ 匹配度
class FitItem(Tolerant):
    item: str = Field(min_length=1)
    why: str = ""


class LlmFitOutput(Tolerant):
    matched: list[FitItem] = Field(default_factory=list)
    partial: list[FitItem] = Field(default_factory=list)
    unmatched: list[FitItem] = Field(default_factory=list)
    fit_ratio: float = Field(ge=0.0, le=1.0)
    direction: str = Field(min_length=5, description="候选人偏什么方向 vs 岗位要什么方向")
    verdict: FitVerdict


# ---------------------------------------------------------------- ⑤ 面试题
class Question(Tolerant):
    level: QLevel
    difficulty: Difficulty
    target: str = Field(min_length=2, description="考的是哪项能力或哪条岗位要求")
    question: str = Field(min_length=5)
    intent: str = ""
    red_flag: str = Field(default="", description="什么样的回答说明是注水")


class LlmQuestionsOutput(Tolerant):
    questions: list[Question] = Field(default_factory=list)

# ---------------------------------------------------------------- 按维度取证（④a）
class CoverItem(Tolerant):
    """一条定向取证的结果：能力项 + 简历原文片段。"""
    capability: str = Field(min_length=1)
    evidence: str = Field(min_length=4, description="必须是简历原文里的连续片段，不得改写")


class CoverDimension(Tolerant):
    """一个维度的取证结果：found=false 表示"该维度在这份简历里找不到支撑证据"。"""
    id: str = Field(min_length=1)
    found: bool = True
    note: str = ""
    items: list[CoverItem] = Field(default_factory=list)


class LlmCoverOutput(Tolerant):
    """按岗位维度定向取证的输出。

    为什么需要这一步：实测 100 份语料里，6 个评分维度有 3 个对 7 成简历是空的
    （项目 31%、游戏理解 29%、学习与竞赛 16% 有证据），原因是解析阶段的条目抽取
    只按"简历自己强调什么"来抽，不知道岗位有哪些评分维度；分数因此被压在 2~3 个维度上。
    这一步对每个维度单独问一次"有没有证据"，把缺失变成**显式结论**而不是静默的 0 分。
    """
    dimensions: list[CoverDimension] = Field(default_factory=list)
