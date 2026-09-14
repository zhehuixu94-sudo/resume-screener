# -*- coding: utf-8 -*-
"""按岗位维度定向取证（覆盖缺口修复）。

问题（实测）：解析阶段的条目抽取是**岗位无关**的——它只按"简历自己强调什么"抽条目，
不知道岗位有哪些评分维度。结果 100 份语料里 6 个维度有 3 个对 7 成简历是空的
（项目完整度 31%、游戏理解 29%、学习与竞赛 16% 有证据），分数被压在 2~3 个维度上，
区分度低、判定一边倒"边缘"。

这一步在评估阶段（与岗位相关）对**每个维度**单独取证：
  · 有证据 → 产出条目（原文连续片段），进标注与算分；
  · 没证据 → 显式记 found=false（"该维度无证据"是结论，不是静默的 0 分）。

为什么不放进解析阶段：解析结果是**简历的属性**，跨岗位复用；把岗位维度塞进去会污染复用。
坐标基准：证据片段取自 parse.text（规范化文本），因此这里用恒等映射的 Normalized 视图锚定，
span 仍是规范化文本坐标 —— 与报告高亮用的坐标一致。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..contracts.common import EvidenceSpan
from ..contracts.evaluation import DimensionCoverage
from ..contracts.job_config import JobConfig
from ..contracts.llm_outputs import LlmCoverOutput
from ..contracts.resume_parse import ResumeParse
from ..llm.client import SiliconFlowClient
from ..llm.coercers import coerce_cover
from ..llm.prompts import PROMPT_VERSION, SYS_COVER, TPL_COVER, hash_of
from ..llm.structured import StageMeta, call_structured
from ..text.anchor import anchor
from ..text.normalize import Normalized


@dataclass
class CoverOutcome:
    items: list[dict] = field(default_factory=list)          # 供标注段使用（idx/item_id/block/capability/evidence）
    spans: list[EvidenceSpan] = field(default_factory=list)  # 新锚定的证据
    span_map: dict[str, str] = field(default_factory=dict)   # item_id → span_id
    coverage: list[DimensionCoverage] = field(default_factory=list)
    meta: StageMeta | None = None

    @property
    def ok(self) -> bool:
        """取证段是否可用：失败（status=failed）时不许假装覆盖到了。"""
        if self.meta is None:
            return False
        return self.meta.validation.status in ("ok", "repaired", "degraded")

    def span_pool(self, parse: ResumeParse) -> dict[str, EvidenceSpan]:
        pool = {e.span_id: e for e in parse.evidence}
        pool.update({e.span_id: e for e in self.spans})
        return pool


def _norm_view(parse: ResumeParse) -> Normalized:
    """恒等映射的 Normalized 视图：证据片段就是 parse.text 的子串，不需要 raw↔norm 映射表。"""
    t = parse.text or ""
    return Normalized(text=t, rmap=list(range(len(t))), raw=t, ops=[])


def _page_of(parse: ResumeParse, start: int) -> int | None:
    offs = parse.source.page_offsets or []
    if not offs:
        return None
    return max(1, min(len(offs), sum(1 for o in offs if o <= start)))


def cover_dimensions(job: JobConfig, parse: ResumeParse, client: SiliconFlowClient, *,
                     model: str | None = None, temperature: float = 0.0,
                     max_tokens: int = 4000, retries: int = 1) -> CoverOutcome:
    """按维度定向取证。

    ⚠️ **失败必须重试**（实测教训）：这一段偶发 `failed`（模型输出不合契约），一失败标注池就少了
    十几条取证条目 → 同一份简历的判定分从 76.6 掉到 54.8（极差 22 分），而当时只写了一句备注、
    评估照样自动处置。**一次覆盖取证失败的代价是 20 分**，所以这里重试一次；
    重试仍失败则由调用方把 `cover` 记进 stage_status → 强制转人工（不许静默掉分）。
    """
    dim_lines = "\n".join(f"- {d.id} = {d.name}（权重 {d.weight:g}）：{d.anchors.合格[:80]}" for d in job.dimensions)
    user = TPL_COVER.format(job_title=job.job_title, jd=(job.jd or "")[:1200],
                            dim_lines=dim_lines, resume=parse.text or "")
    res = call_structured(client, stage="cover", system=SYS_COVER, user=user,
                          model_cls=LlmCoverOutput, coercer=coerce_cover, job=job,
                          prompt_version=PROMPT_VERSION, prompt_hash=hash_of(SYS_COVER, TPL_COVER),
                          config_version=job.config_version, model=model, max_tokens=max_tokens,
                          temperature=temperature)
    for _ in range(max(0, retries)):
        if res.obj is not None:
            break
        res = call_structured(client, stage="cover", system=SYS_COVER, user=user,
                              model_cls=LlmCoverOutput, coercer=coerce_cover, job=job,
                              prompt_version=PROMPT_VERSION, prompt_hash=hash_of(SYS_COVER, TPL_COVER),
                              config_version=job.config_version, model=model, max_tokens=max_tokens,
                              temperature=temperature)

    out = CoverOutcome(meta=res.meta)
    norm = _norm_view(parse)
    names = {d.id: d.name for d in job.dimensions}
    n_ev = 900        # 覆盖证据的 span 编号与解析证据错开（解析用 ev_0001..），便于溯源
    if res.obj is None:
        # 取证失败：所有维度显式标为"未取证"，交给降级路径（不许假装覆盖到了）
        out.coverage = [DimensionCoverage(id=d.id, name=d.name, found=False, n_items=0,
                                          note="取证段失败，未取得该维度证据") for d in job.dimensions]
        return out

    for bi, cd in enumerate(res.obj.dimensions, start=1):
        bid = f"c{bi}"
        kept = 0
        for ii, it in enumerate(cd.items, start=1):
            iid = f"{bid}.i{ii}"
            n_ev += 1
            sid = f"evc_{n_ev:04d}"
            a = anchor(it.evidence, norm)
            span = a.span
            out.spans.append(EvidenceSpan(
                span_id=sid, item_id=iid, text=it.evidence, span=span, raw_range=None,
                page=_page_of(parse, span.start) if span else None,
                block_title=f"定向取证·{names.get(cd.id, cd.id)}", anchoring=a.anchoring, alt_spans=a.alt_spans))
            out.items.append({"idx": 0, "item_id": iid, "block": f"定向取证·{names.get(cd.id, cd.id)}",
                              "capability": it.capability, "evidence": it.evidence})
            out.span_map[iid] = sid if span else ""
            kept += 1
        out.coverage.append(DimensionCoverage(id=cd.id, name=names.get(cd.id, ""),
                                              found=bool(cd.found and kept), n_items=kept,
                                              note=(cd.note or "")[:160]))

    # idx 在合并进标注池时统一重排
    return out
