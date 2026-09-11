# -*- coding: utf-8 -*-
"""② 岗位配置契约（HR 侧唯一配置入口）。

相对原型 `config/job_*.json` 的改动（v1）：
1. 加 `schema_version` / `config_version`：配置改动必须可追溯（评分行记 config_version）。
2. `pass_line` 移入 `scoring`，并加 `pass_line_source`：区分「HR 拍的人工兜底值」与「锚点标定值」。
3. 维度加 `keywords`：兜底归类的关键词从代码（score_resume.py 里硬编码的 kw 表）搬进配置，
   没配关键词的维度就不参与兜底归类（宁可留空，不硬塞）。
4. 加 `scoring.dimension_aggregation`：维度分取「最强项 / top-2 平均」，任务 4 在这里切换。
5. 加 `calibration`：锚点标定（任务 3）的落点——HR 给的合格/不合格锚点与推导出的达标线。
"""
from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from .common import AnchorLevel, SCHEMA_VERSION, Strict, Version


class HardFilter(Strict):
    """硬门槛（一票否决）。

    判定口径：**先规则后模型**。配了 keywords_pass / keywords_fail 的门槛由规则层在简历全文里判定，
    模型不再有机会误报"哪条门槛被触发"（实测模型会把明明写了的本科判成"未提及"）；
    没配关键词的门槛退回模型判定（旧行为），并且只认模型能定位到具体门槛的情形。
    """

    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    rule: str = Field(min_length=2, description="人话规则，如「本科及以上」，会原样进 prompt")
    veto: bool = True
    require_evidence: bool = Field(default=True, description="要求系统必须找到证据才判触发")
    keywords: list[str] = Field(default_factory=list, description="可选：辅助定位与该门槛相关的原文（正负关键词的并集）")
    keywords_pass: list[str] = Field(default_factory=list, description="命中任一 → 判通过")
    keywords_fail: list[str] = Field(default_factory=list, description="命中任一且未命中 pass 关键词 → 判不通过")
    unknown_policy: Literal["manual_review", "fail"] = Field(default="manual_review",
        description="两边关键词都没命中时的处置：转人工（默认，宁可多看）或不通过")


WEAK_ANCHORS = {"无", "无证据", "一般", "不熟悉", "无相关经历", "无经验", "不清楚", "无相关经验"}
MIN_ANCHOR_CHARS = 8                      # 软门槛：低于它会出警告，不是硬失败


class DimensionAnchors(Strict):
    """三档锚点 = 相对比较的标尺。每档写人话示例，别写抽象形容词。

    硬约束只有"非空且三档不完全相同"；"这段锚点够不够当尺子"交给 anchor_lint() 出警告——
    契约负责不让人写出非法配置，不负责替 HR 判定锚点写得好不好。
    """

    合格: str = Field(min_length=2)
    边缘: str = Field(min_length=2)
    不达标: str = Field(min_length=2)


class Dimension(Strict):
    id: str = Field(min_length=1, pattern=r"^[a-z][a-z0-9_]*$")
    name: str = Field(min_length=1)
    weight: float = Field(gt=0, le=100)
    anchors: DimensionAnchors
    keywords: list[str] = Field(default_factory=list, description="兜底归类用；空 = 不参与兜底")


class BonusItem(Strict):
    name: str = Field(min_length=1)
    points: float = Field(ge=0)
    keywords: list[str] = Field(min_length=1, description="必须显式声明，靠名称模糊匹配会漏（实测）")


class CalibrationAnchor(Strict):
    """HR 给的锚点样本：合格 3~5 份 + 不合格 2~3 份 → 系统算分 → 达标线 = 合格组下限。"""

    label: Literal["合格", "不合格"]
    resume_ref: str = Field(min_length=1, description="简历标识：文件名 / resume_id")
    score: float | None = None            # 标定时由系统回填
    note: str = ""


class Calibration(Strict):
    status: Literal["uncalibrated", "calibrated"] = "uncalibrated"
    anchors: list[CalibrationAnchor] = Field(default_factory=list)
    good_group_min: float | None = None
    calibrated_at: str = ""
    notes: str = Field(default="", description="标定诊断：两组分数是否分得开、样本是否够数、缺哪些硬证据")


class Scoring(Strict):
    pass_line: float = Field(ge=0, le=200, description="达标线")
    pass_line_source: Literal["manual_default", "anchor_calibrated"] = "manual_default"
    band_width: float = Field(default=5.0, ge=0, description="三段带：≥线+band 自动达标 / ±band 转人工 / <线−band 自动淘汰")
    dimension_aggregation: Literal["best", "top2_mean"] = "best"
    fallback_keyword_mapping: bool = Field(default=True, description="模型没给维度时是否用维度 keywords 兜底归类")
    confidence_floor: float = Field(default=0.6, ge=0, le=1, description="低于此置信度不自动淘汰")


class JobConfig(Strict):
    schema_version: Version = SCHEMA_VERSION
    job_id: str = Field(min_length=1, pattern=r"^[a-z0-9_]+$")
    config_version: str = Field(default="1", min_length=1, description="配置改动递增；评分行记它才能复现历史分数")
    company_id: str = Field(default="local", description="多租户预留：不做 RLS，但每条数据都带它")
    job_title: str = Field(min_length=1)
    jd: str = Field(min_length=20)

    # 评分口径（模型侧 prompt 会原样拿到）
    dimensions: list[Dimension] = Field(min_length=1)
    hard_filters: list[HardFilter] = Field(default_factory=list)
    bonus: list[BonusItem] = Field(default_factory=list)

    scoring: Scoring
    calibration: Calibration = Field(default_factory=Calibration)
    interview_focus: str = ""
    notes: str = ""

    # ------------------------------------------------------------ 约束
    @model_validator(mode="after")
    def _check(self) -> "JobConfig":
        ids = [d.id for d in self.dimensions]
        if len(ids) != len(set(ids)):
            raise ValueError(f"维度 id 重复：{ids}")
        total = round(sum(d.weight for d in self.dimensions), 3)
        if abs(total - 100.0) > 0.01:
            raise ValueError(f"维度权重合计必须为 100，当前 {total}")
        fids = [f.id for f in self.hard_filters]
        if len(fids) != len(set(fids)):
            raise ValueError(f"硬门槛 id 重复：{fids}")
        if self.calibration.status == "calibrated" and not self.calibration.anchors:
            raise ValueError("calibrated 状态必须有锚点样本")
        if self.calibration.status == "calibrated" and self.scoring.pass_line_source != "anchor_calibrated":
            raise ValueError("已标定但 pass_line_source 仍是 manual_default：标定后必须回填")
        good = [a for a in self.calibration.anchors if a.label == "合格"]
        if self.calibration.status == "calibrated" and len(good) < 3:
            raise ValueError("锚点标定至少需要 3 份合格样本（HR 侧要求 3~5 份）")
        # 锚点是评分标尺：合格档和不达标档写成一模一样 = 标尺失效，属于功能性错误
        for d in self.dimensions:
            if d.anchors.合格 == d.anchors.不达标:
                raise ValueError(f"维度 {d.id} 的合格锚点与不达标锚点完全相同，标尺失效")
        return self

    # ------------------------------------------------------------ 软性 lint
    def anchor_lint(self) -> list[str]:
        """锚点质量警告（不阻断校验）：太短或只写了"无/不熟悉"这类抽象词。"""
        out: list[str] = []
        for d in self.dimensions:
            for lvl, txt in (("合格", d.anchors.合格), ("边缘", d.anchors.边缘), ("不达标", d.anchors.不达标)):
                t = txt.strip()
                if len(t) < MIN_ANCHOR_CHARS or t in WEAK_ANCHORS:
                    out.append(f"{d.id}.{lvl} 锚点偏薄（{len(t)} 字：{t!r}）——它是相对比较的尺子，建议写成一句人话示例")
        return out

    # ------------------------------------------------------------ 便捷
    @property
    def dimension_ids(self) -> list[str]:
        return [d.id for d in self.dimensions]

    def dimension(self, dim_id: str) -> Dimension | None:
        return next((d for d in self.dimensions if d.id == dim_id), None)

    def anchor_prompt(self) -> dict[str, str]:
        """给 compare 阶段的三档标尺：每个维度取第一条非空锚点拼一句。"""
        out: dict[str, str] = {}
        for lvl in ("合格", "边缘", "不达标"):
            for d in self.dimensions:
                txt = getattr(d.anchors, lvl)
                if txt:
                    out[lvl] = f"[{d.name}] {txt}"
                    break
        return out

    def anchor_by_dimension(self) -> dict[str, dict[AnchorLevel, str]]:
        return {d.id: {"合格": d.anchors.合格, "边缘": d.anchors.边缘, "不达标": d.anchors.不达标} for d in self.dimensions}
