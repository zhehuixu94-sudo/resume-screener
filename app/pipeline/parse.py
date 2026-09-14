# -*- coding: utf-8 -*-
"""阶段 1：抽取 → 质量门禁 → 模型解析 → 证据锚定 → ResumeParse。"""
from __future__ import annotations

from dataclasses import dataclass

from ..contracts.common import (
    Anchoring,
    CharRange,
    EvidenceSpan,
    Normalization,
    sha256_file,
    SourceInfo,
)
from ..contracts.job_config import JobConfig
from ..contracts.llm_outputs import LlmParseOutput
from ..contracts.resume_parse import Achievement, Block, ParseStats, ParsedItem, ResumeParse
from ..llm.client import SiliconFlowClient
from ..llm.coercers import coerce_parse
from ..llm.prompts import PROMPT_VERSION, SYS_PARSE, TPL_PARSE, hash_of
from ..llm.structured import StructuredResult, call_structured
from ..text.anchor import anchor
from ..text.extract import extract
from ..text.normalize import Normalized, normalize, page_offsets
from ..text.quality import field_anomalies, gate
from ..text.timeline import check_timeline


@dataclass
class ParseOutcome:
    parse: ResumeParse | None
    result: StructuredResult
    normalized: Normalized

    @property
    def ok(self) -> bool:
        return self.parse is not None


def _span_id(n: int) -> str:
    return f"ev_{n:04d}"


def parse_resume(path: str, job: JobConfig, client: SiliconFlowClient, *, model: str | None = None,
                 max_tokens: int = 6000) -> ParseOutcome:
    ex = extract(path)
    norm = normalize(ex.text)
    quality = gate(norm.text, ex.pages)
    quality.field_anomalies = field_anomalies(norm.text)
    quality.timeline = check_timeline(norm.text)          # 纯规则：起止矛盾/重叠/空档/未来日期

    user = TPL_PARSE.format(job_title=job.job_title, jd=job.jd, resume=norm.text)
    result = call_structured(client, stage="parse", system=SYS_PARSE, user=user,
                             model_cls=LlmParseOutput,
                             coercer=coerce_parse, job=job, prompt_version=PROMPT_VERSION,
                             prompt_hash=hash_of(SYS_PARSE, TPL_PARSE), config_version=job.config_version,
                             model=model, max_tokens=max_tokens, temperature=0.0)

    if result.obj is None:
        return ParseOutcome(parse=None, result=result, normalized=norm)

    blocks: list[Block] = []
    items: list[ParsedItem] = []
    evidence: list[EvidenceSpan] = []
    n_ev = 0
    for bi, b in enumerate(result.obj.blocks, start=1):        # type: ignore[attr-defined]
        bid = f"b{bi}"
        item_ids: list[str] = []
        for ii, it in enumerate(b.items, start=1):
            iid = f"{bid}.i{ii}"
            n_ev += 1
            sid = _span_id(n_ev)
            a = anchor(it.evidence, norm)
            span = a.span
            raw = None
            if span:
                rs, re_ = norm.raw_range(span.start, span.end)
                raw = CharRange(start=rs, end=re_)
            page = None
            if span and ex.page_texts:
                offs = page_offsets(norm, ex.page_texts)
                page = max(1, min(len(offs), sum(1 for o in offs if o <= span.start)))
            evidence.append(EvidenceSpan(
                span_id=sid, item_id=iid, text=it.evidence, span=span, raw_range=raw, page=page,
                block_title=b.title, anchoring=a.anchoring, alt_spans=a.alt_spans))
            items.append(ParsedItem(item_id=iid, block_id=bid, capability=it.capability, note=it.note,
                                    span_id=sid if span else None, anchored=span is not None))
            item_ids.append(iid)
        blocks.append(Block(block_id=bid, section=b.section, title=b.title, period=b.period, item_ids=item_ids))

    # —— 量化成就：单独锚定，并镜像到 evidence（item_id 用哨兵 __ach__，便于信号按同样口径读表）
    achievements: list[Achievement] = []
    for ai, a in enumerate(getattr(result.obj, "achievements", []) or [], start=1):
        aa = anchor(a.text, norm)
        sp = aa.span
        raw = None
        if sp:
            rs, re_ = norm.raw_range(sp.start, sp.end)
            raw = CharRange(start=rs, end=re_)
        achievements.append(Achievement(
            achievement_id=f"ach{ai}", text=a.text, kind=a.kind, note=a.note,
            span={"start": sp.start, "end": sp.end} if sp else None, anchored=sp is not None))
        if sp:                                            # 只把锚定成功的镜像进证据表
            n_ev += 1
            evidence.append(EvidenceSpan(
                span_id=_span_id(n_ev), item_id="__ach__", text=a.text, span=sp, raw_range=raw,
                block_title="量化成就", anchoring=aa.anchoring, alt_spans=aa.alt_spans))

    # 覆盖率只统计"能力项证据"：量化成就也镜像进了 evidence，混在一起算会让覆盖率 >1
    item_evidence = [e for e in evidence if e.item_id != "__ach__"]
    ach_evidence = [e for e in evidence if e.item_id == "__ach__"]
    anchored = sum(1 for e in item_evidence if e.anchored)
    stats = ParseStats(blocks=len(blocks), items=len(items), evidence_total=len(item_evidence),
                       evidence_anchored=anchored, evidence_unanchored=len(item_evidence) - anchored,
                       evidence_coverage=round(anchored / max(1, len(items)), 3),
                       achievements=len(achievements),
                       achievements_anchored=sum(1 for a in achievements if a.anchored),
                       achievements_evidence=len(ach_evidence))

    parse = ResumeParse(
        resume_id=sha256_file(path)[:16],
        source=SourceInfo(file_name=path.replace("\\", "/").split("/")[-1], file_sha256=sha256_file(path),
                          file_format=ex.file_format, extractor=ex.extractor, pages=ex.pages,
                          page_offsets=page_offsets(norm, ex.page_texts)),
        normalization=Normalization(ops=norm.ops, raw_char_count=len(norm.raw), norm_char_count=len(norm.text),
                                   raw_sha256=norm.raw_sha256, norm_sha256=norm.norm_sha256,
                                   map_sha256=norm.map_sha256),
        quality=quality, blocks=blocks, items=items, evidence=evidence,
        achievements=achievements, stats=stats,
        text=norm.text, llm=result.meta)
    return ParseOutcome(parse=parse, result=result, normalized=norm)
