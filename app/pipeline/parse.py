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
from ..contracts.resume_parse import Block, ParseStats, ParsedItem, ResumeParse
from ..llm.client import SiliconFlowClient
from ..llm.coercers import coerce_parse
from ..llm.prompts import PROMPT_VERSION, SYS_PARSE, TPL_PARSE, hash_of
from ..llm.structured import StructuredResult, call_structured
from ..text.anchor import anchor
from ..text.extract import extract
from ..text.normalize import Normalized, normalize, page_offsets
from ..text.quality import field_anomalies, gate


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

    anchored = sum(1 for e in evidence if e.anchored)
    stats = ParseStats(blocks=len(blocks), items=len(items), evidence_total=len(evidence),
                       evidence_anchored=anchored, evidence_unanchored=len(evidence) - anchored,
                       evidence_coverage=round(anchored / max(1, len(items)), 3))

    parse = ResumeParse(
        resume_id=sha256_file(path)[:16],
        source=SourceInfo(file_name=path.replace("\\", "/").split("/")[-1], file_sha256=sha256_file(path),
                          file_format=ex.file_format, extractor=ex.extractor, pages=ex.pages,
                          page_offsets=page_offsets(norm, ex.page_texts)),
        normalization=Normalization(ops=norm.ops, raw_char_count=len(norm.raw), norm_char_count=len(norm.text),
                                   raw_sha256=norm.raw_sha256, norm_sha256=norm.norm_sha256,
                                   map_sha256=norm.map_sha256),
        quality=quality, blocks=blocks, items=items, evidence=evidence, stats=stats,
        text=norm.text, llm=result.meta)
    return ParseOutcome(parse=parse, result=result, normalized=norm)
