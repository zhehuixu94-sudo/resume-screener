# -*- coding: utf-8 -*-
"""硬门槛判定：规则层预检 + 模型兜底。

为什么必须规则化：实测模型会把简历里明明写了的"本科""网络空间安全"判成"未提及"，
并据此把三条硬门槛都标成触发 —— 于是一个对口候选人被自动淘汰。硬门槛是要淘汰人的，
判定依据必须能在简历全文里被复现，不能交给模型的印象。

口径（每条纹硬门槛独立判定，三态）：
  pass     命中 keywords_pass
  fail     未命中 pass 且命中 keywords_fail（或 unknown_policy=fail 且两边都没命中）
  unknown  两边都没命中 → 默认转人工（宁可多看，不可漏放）

证据：命中关键词时，把包含该关键词的 evidence span 附在结果上；一个 span 都没有时
说明"规则层是从全文匹配到的，但抽取出的证据里没有对应条目"，报告里要如实写出来。
"""
from __future__ import annotations

import re

from ..contracts.evaluation import HardFilterResult
from ..contracts.job_config import HardFilter, JobConfig
from ..contracts.llm_outputs import LlmCompareOutput
from ..contracts.resume_parse import ResumeParse


def _find_spans(parse: ResumeParse, keywords: list[str]) -> tuple[list[str], list[str], bool]:
    """返回（命中的关键词, 相关证据 span_id, 是否全部有 span 可指）。

    三级定位：① 关键词在抽取出的证据片段里 → 直接引用该 span；
             ② 关键词在原文里、且落在某个证据 span 的坐标区间内 → 引用那个 span；
             ③ 都落不上（全文命中但抽取的条目没覆盖到）→ 返回空并让调用方如实说明，
                绝不能编一个 span 出来——报告点不开的证据等于没有证据。
    """
    hits, span_ids = [], []
    for k in keywords:
        if not k or k.lower() not in parse.text.lower():
            continue
        hits.append(k)
        for e in parse.evidence:
            if k.lower() in e.text.lower():
                if e.span_id not in span_ids:
                    span_ids.append(e.span_id)
                break
        else:
            pos = parse.text.lower().find(k.lower())
            cover = next((e for e in parse.evidence
                          if e.span and e.span.start <= pos < e.span.end and e.span_id not in span_ids), None)
            if cover is not None:
                span_ids.append(cover.span_id)
    return sorted(set(hits)), span_ids, len(span_ids) > 0


EDU_PERIOD = re.compile(r"(20\d{2})\s*[.\-/年]?\s*\d{0,2}\s*[-—~至到]{1,2}\s*(20\d{2})")


def _education_hint(text: str) -> str:
    """学历没写层次时的辅助线索：只做提示，不改变判定（不许靠推断把人判通过）。"""
    years = [int(y2) - int(y1) for y1, y2 in EDU_PERIOD.findall(text)]
    span = max(years) if years else 0
    if span >= 4 and re.search(r"(大学|学院)", text):
        return f"另有线索：教育经历跨度 {span} 年（疑似本科）→ 建议人工确认是否本科"
    if span:
        return f"另有线索：教育经历跨度 {span} 年（未写学历层次）"
    return ""


def check_hard_filters(job: JobConfig, parse: ResumeParse,
                       compare: LlmCompareOutput | None = None) -> list[HardFilterResult]:
    text = parse.text
    lower = text.lower()
    results: list[HardFilterResult] = []

    for f in job.hard_filters:
        if f.keywords_pass or f.keywords_fail:
            kw_pass = [k for k in f.keywords_pass if k]
            kw_fail = [k for k in f.keywords_fail if k]
            hit_pass = [k for k in kw_pass if k.lower() in lower]
            hit_fail = [k for k in kw_fail if k.lower() in lower]
            if hit_pass:
                hits, spans, covered = _find_spans(parse, hit_pass)
                results.append(HardFilterResult(
                    id=f.id, name=f.name, rule=f.rule, veto=f.veto, result="pass",
                    reason=f"简历全文命中：{'、'.join(hits[:4])}"
                           + (f"（同时也出现：{'、'.join(hit_fail[:3])}，已按通过处理，需人工留意）" if hit_fail else "")
                           + ("" if covered else "（全文命中，但抽取出的证据条目没覆盖到这句，报告无法高亮）"),
                    evidence_span_ids=spans))
            elif hit_fail:
                hits, spans, covered = _find_spans(parse, hit_fail)
                results.append(HardFilterResult(
                    id=f.id, name=f.name, rule=f.rule, veto=f.veto, result="fail",
                    reason=f"简历全文命中不满足条件的关键词：{'、'.join(hits[:4])}"
                           + ("" if covered else "（全文命中但无证据 span）"),
                    evidence_span_ids=spans))
            else:
                hint = _education_hint(parse.text) if f.id == "edu" else ""
                results.append(HardFilterResult(
                    id=f.id, name=f.name, rule=f.rule, veto=f.veto,
                    result="fail" if f.unknown_policy == "fail" else "unknown",
                    reason="简历里既没写满足条件的证据，也没写不满足的证据 → "
                           + ("按配置判不通过" if f.unknown_policy == "fail" else "证据不足，转人工核实")
                           + (f"；{hint}" if hint else ""),
                    evidence_span_ids=[]))
            continue

        # ---------------- 没配关键词：退回模型判定（旧行为，只认能定位到具体门槛的）
        model_ids = list(compare.veto_filter_ids) if compare else []
        if model_ids:
            hit = f.id in model_ids
            results.append(HardFilterResult(
                id=f.id, name=f.name, rule=f.rule, veto=f.veto,
                result="fail" if hit else "pass",
                reason=("模型指出该门槛被触发（该门槛未配规则关键词，依据来自模型整体判定）" if hit
                        else "模型未指出触发"),
                evidence_span_ids=[]))
        elif compare and compare.veto_triggered:
            results.append(HardFilterResult(
                id=f.id, name=f.name, rule=f.rule, veto=f.veto, result="unknown",
                reason="模型指出触发硬门槛但未定位到具体门槛，且该门槛未配规则关键词 → 转人工",
                evidence_span_ids=[]))
        else:
            results.append(HardFilterResult(
                id=f.id, name=f.name, rule=f.rule, veto=f.veto, result="pass",
                reason="模型整体判定未指出触发",
                evidence_span_ids=[]))
    return results


def veto_summary(results: list[HardFilterResult]) -> tuple[list[str], list[str], list[str]]:
    """返回（触发不通过的门槛 id, 证据不足的门槛 id, 通过的门槛 id）。"""
    fails = [r.id for r in results if r.veto and r.result == "fail"]
    unknown = [r.id for r in results if r.veto and r.result == "unknown"]
    passes = [r.id for r in results if r.veto and r.result == "pass"]
    return fails, unknown, passes
