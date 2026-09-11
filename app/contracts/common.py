# -*- coding: utf-8 -*-
"""契约公共层：枚举、溯源元数据、校验报告、span/归一化/质量门禁结构。

单一真相来源：本目录的 Pydantic 模型。`schemas/*.schema.json` 由
`tools/export_schemas.py` 从这些模型导出（不要手改 JSON）。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

SCHEMA_VERSION = "1.0.0"

# ---------------------------------------------------------------- 受控词表
Relevance = Literal["强", "中", "弱", "无关"]
Difficulty = Literal["高", "中", "低"]
Verdict = Literal["达标", "边缘", "不达标"]
AnchorLevel = Literal["合格", "边缘", "不达标"]
FitVerdict = Literal["对口", "部分对口", "方向不符"]
QLevel = Literal["基础盘", "深度", "上限"]
Decision = Literal["auto_pass", "manual_review", "auto_reject"]
StageName = Literal["parse", "label", "compare", "fit", "questions"]
ValidationStatus = Literal["ok", "repaired", "degraded", "failed"]
AnchorMethod = Literal["exact", "ws_insensitive", "fuzzy", "none"]


class Strict(BaseModel):
    """领域对象：多一个字段就是契约被破坏，直接报错。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Tolerant(BaseModel):
    """面向模型的输出：多字段只记录不报错（未知字段由 structured.py 记录后丢弃）。"""

    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)


# ---------------------------------------------------------------- 溯源与校验
class Usage(BaseModel):
    model_config = ConfigDict(extra="ignore")
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class ValidationReport(BaseModel):
    """模型输出经过清洗/校验/重试/降级后的状态。落库用于评测与排查。"""

    model_config = ConfigDict(extra="forbid")
    status: ValidationStatus = "ok"
    attempts: int = 1
    schema_ref: str = ""                  # 校验用的契约（模型类名 / schema $id）
    errors: list[str] = Field(default_factory=list)      # 最终失败仍未修好的字段级错误
    repairs: list[str] = Field(default_factory=list)     # 自动修正记录（枚举同义、百分数、垃圾值…）
    extra_keys_dropped: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)

    @property
    def usable(self) -> bool:
        return self.status in ("ok", "repaired", "degraded")


class StageMeta(BaseModel):
    """一段模型调用的可复现性元数据（分数可复现的根基）。"""

    model_config = ConfigDict(extra="forbid")
    stage: StageName
    model_id: str
    prompt_version: str
    prompt_hash: str                      # sha256(system+user 模板)[:16]
    config_version: str = ""              # 岗位配置版本
    temperature: float = 0.0
    trace_ids: list[str] = Field(default_factory=list)   # x-siliconcloud-trace-id
    usage: Usage = Field(default_factory=Usage)
    elapsed_s: float = 0.0
    validation: ValidationReport = Field(default_factory=ValidationReport)


# ---------------------------------------------------------------- 文本与证据
class CharRange(Strict):
    """字符区间 [start, end)，左闭右开。"""

    start: int = Field(ge=0)
    end: int = Field(ge=1)

    def check(self) -> None:
        if self.end <= self.start:
            raise ValueError(f"非法区间：{self.start}..{self.end}")


class Anchoring(Strict):
    """原文片段是怎么落到偏移上的。method=none 表示锚定失败（证据不可高亮、不可作自动淘汰依据）。"""

    method: AnchorMethod
    similarity: float = Field(ge=0.0, le=1.0, default=0.0)
    matched_text: str = ""                # 实际匹配上的原文
    note: str = ""


class Normalization(Strict):
    """抽取原文 → 规范化文本 的映射元数据（偏移都指规范化文本，raw_range 指回原文）。"""

    version: str = "n1"
    ops: list[str] = Field(default_factory=list)         # collapse_ws / strip / drop_control
    raw_char_count: int = Field(ge=0)
    norm_char_count: int = Field(ge=0)
    raw_sha256: str = ""                                 # 原文哈希（字节级抽取结果）
    norm_sha256: str = ""
    map_sha256: str = ""                                 # 偏移映射表哈希（复现校验用）


class QualityCheck(Strict):
    name: str
    value: str
    ok: bool
    hint: str = ""


class TimelineIssue(Strict):
    kind: Literal["reversed_range", "overlap", "gap", "unparsable", "future_date"]
    detail: str
    span_id: str | None = None


class TimelineCheck(Strict):
    status: Literal["not_checked", "ok", "issues"] = "not_checked"
    issues: list[TimelineIssue] = Field(default_factory=list)


class FieldAnomaly(Strict):
    field: str
    detail: str
    severity: Literal["warn", "error"] = "warn"
    span_id: str | None = None


class Quality(Strict):
    """解析质量：机器门禁 + 模型自报 + 字段/时间线级校验（后两者 v1 先占位）。"""

    gate_passed: bool
    checks: list[QualityCheck] = Field(default_factory=list)
    issues: list[str] = Field(default_factory=list)
    model_sections_found: list[str] = Field(default_factory=list)
    model_issues: list[str] = Field(default_factory=list)
    field_anomalies: list[FieldAnomaly] = Field(default_factory=list)
    timeline: TimelineCheck = Field(default_factory=TimelineCheck)


class EvidenceSpan(Strict):
    """一条可追溯的证据：模型给的原文片段 + 代码锚定出的偏移。

    span/raw_range 的坐标都由代码计算，模型不得自报偏移。
    """

    span_id: str
    item_id: str
    text: str = Field(min_length=1)                       # 模型给的原文片段
    span: CharRange | None = None                         # 规范化文本中的区间（None = 未锚定）
    raw_range: CharRange | None = None                    # 抽取原文中的区间
    page: int | None = Field(default=None, ge=1)          # PDF 页号（PDF 才有）
    block_title: str = ""
    anchoring: Anchoring
    alt_spans: list[CharRange] = Field(default_factory=list)   # 同片段在文中的其它出现位置

    @property
    def anchored(self) -> bool:
        return self.span is not None and self.anchoring.method != "none"


class SourceInfo(Strict):
    file_name: str
    file_sha256: str
    file_format: Literal["pdf", "docx", "doc", "txt"]
    extractor: str
    pages: int = Field(ge=1)
    page_offsets: list[int] = Field(default_factory=list)  # 各页在规范化文本中的起始偏移


def utcnow() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def sha256_text(s: str) -> str:
    import hashlib

    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def sha256_file(path: str) -> str:
    import hashlib

    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def dump_json(obj: Any, indent: int = 2) -> str:
    import json

    if isinstance(obj, BaseModel):
        obj = obj.model_dump(mode="json", exclude_none=True)
    return json.dumps(obj, ensure_ascii=False, indent=indent)
