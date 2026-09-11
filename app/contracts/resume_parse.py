# -*- coding: utf-8 -*-
"""① 简历解析契约：ResumeParse（域对象）。

关键立场：**偏移由代码锚定，模型只给原文片段。**
模型自报偏移再校验等于多一个误差源；而"原文片段 → 偏移"可以由代码确定性还原
（精确匹配 → 空白不敏感匹配 → 模糊匹配 → 锚定失败），并且能把失败暴露出来。
未锚定的证据（anchoring.method == "none"）不允许出现在报告的原文高亮里，
也不允许作为自动淘汰的唯一依据（见 evaluation 契约的 conflicts）。
"""
from __future__ import annotations

from typing import Literal

from pydantic import Field

from .common import (
    EvidenceSpan,
    Normalization,
    Quality,
    SCHEMA_VERSION,
    SourceInfo,
    StageMeta,
    Strict,
    utcnow,
)
from .llm_outputs import LlmParseOutput


class Block(Strict):
    block_id: str = Field(min_length=1)
    section: str = Field(min_length=1)
    title: str = Field(min_length=1)
    period: str = ""
    item_ids: list[str] = Field(default_factory=list)


class ParsedItem(Strict):
    item_id: str = Field(min_length=1)
    block_id: str = Field(min_length=1)
    capability: str = Field(min_length=2)
    note: str = ""
    span_id: str | None = Field(default=None, description="指向 evidence[] 中的证据；None = 锚定失败，报告里单列")
    anchored: bool = False


class ParseStats(Strict):
    blocks: int = Field(ge=0)
    items: int = Field(ge=0)
    evidence_total: int = Field(ge=0)
    evidence_anchored: int = Field(ge=0)
    evidence_unanchored: int = Field(ge=0)
    evidence_coverage: float = Field(ge=0.0, le=1.0, description="anchored / items，即报告里『证据可点开』的达标率")


class ResumeParse(Strict):
    schema_version: Literal[SCHEMA_VERSION] = SCHEMA_VERSION
    resume_id: str = Field(min_length=1, description="默认 = 文件 sha256 前 16 位（同一份简历重复上传视为同一实体）")
    source: SourceInfo
    normalization: Normalization
    quality: Quality
    blocks: list[Block] = Field(min_length=1)
    items: list[ParsedItem] = Field(min_length=1)
    evidence: list[EvidenceSpan] = Field(default_factory=list)
    stats: ParseStats
    text: str = Field(description="规范化后的简历文本（报告/高亮用；原文另存加密列）")
    llm: StageMeta
    created_at: str = Field(default_factory=utcnow)

    def span_of(self, item_id: str) -> EvidenceSpan | None:
        item = next((i for i in self.items if i.item_id == item_id), None)
        if not item or not item.span_id:
            return None
        return next((e for e in self.evidence if e.span_id == item.span_id), None)

    def text_of(self, span_id: str) -> str:
        e = next((x for x in self.evidence if x.span_id == span_id), None)
        return e.text if e else ""


__all__ = [
    "Block",
    "ParsedItem",
    "ParseStats",
    "ResumeParse",
    "LlmParseOutput",
]
