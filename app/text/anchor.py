# -*- coding: utf-8 -*-
"""证据锚定：把模型给的原文片段落到规范化文本的字符偏移上。

四级策略（逐个降级，全部失败才算锚定失败）：
  1. exact             片段与原文逐字符相同
  2. ws_insensitive    空白折叠差异 / 片段里用 | 、 / 等分隔符拼接（实测模型爱这么干）
  3. fuzzy             difflib 相似度 ≥ 阈值（模型轻微改写、漏字、OCR 噪声）
  4. none              锚定失败 → 该证据不可高亮、不可作为自动淘汰的唯一依据
锚定失败是**暴露出来**的信号，不是被吞掉的异常。
"""
from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field

from ..contracts.common import Anchoring, CharRange
from .normalize import Normalized, collapse_ws

MIN_LEN = 4
FUZZY_THRESHOLD = 0.85
SEP_CHARS = "|｜/／·、,，;；"
SEPARATOR = rf"\s*(?:[{SEP_CHARS}]\s*)?"
SPLIT = rf"[\s{SEP_CHARS}]+"

# 去噪后可包含匹配：空白 + 中英标点 + 全角符号。模型改写标点/加空格/换全角时，
# "逐字相同"会失败，但去掉这些噪声后就是原文的连续切片——实测这一类是锚定失败的主体。
NOISE = re.compile(r"[\s\u3000\u200b-\u200f!-/:-@\[-`{-~，。、；：？！\u201c\u201d\u2018\u2019（）"
                   r"《》〈〉【】〔〕…—－·～￥$%‰°™®©]+")


def _denoise(text: str) -> tuple[str, list[int]]:
    """去掉空白/标点并小写化，同时保留"净化后第 i 个字符 → 原文下标"的映射。"""
    keep, idx = [], []
    for i, ch in enumerate(text):
        if NOISE.match(ch):
            continue
        keep.append(ch.lower())
        idx.append(i)
    return "".join(keep), idx


def _denoised_containment(ev: str, text: str) -> tuple[int, int, float] | None:
    """净化后包含匹配：模型改写标点/大小写/空格，但内容仍是原文连续切片时命中。"""
    tn, tmap = _denoise(text)
    en, _ = _denoise(ev)
    if len(en) < MIN_LEN or not tn:
        return None
    i = tn.find(en)
    if i < 0:
        return None
    sim = round(len(en) / max(1, len(ev)), 3)      # 净化丢掉的字符比例反映"改写程度"
    return (tmap[i], tmap[i + len(en) - 1] + 1, max(0.9, sim))


@dataclass
class AnchorResult:
    anchoring: Anchoring
    span: CharRange | None
    alt_spans: list[CharRange] = field(default_factory=list)


def _regex_pattern(ev: str) -> str:
    """按空白/分隔符切词，中间允许空白或一个分隔符——模型爱把不同位置的片段用 | 拼起来，
    这样拼出来的片段仍然能落到原文上，不会因为一个分隔符就锚定失败。"""
    tokens = [t for t in re.split(SPLIT, ev) if t]
    return SEPARATOR.join(re.escape(t) for t in tokens)


def _fuzzy_search(ev: str, text: str, threshold: float) -> tuple[int, int, float] | None:
    """模型轻微改写/漏字时的兜底锚定：按词序贪心匹配，再用字符相似度复核。

    similarity = 0.5 × 词覆盖率 + 0.5 × SequenceMatcher 相似度 —— 只按词覆盖率会让
    "示例大学 本科" 这类丢词片段虚高，只按字符相似度又会对漏字片段过于苛刻。
    命中跨度过大（> 2.5 倍片段长度）一律判为未命中，避免把相隔很远的两段硬连起来。
    """
    tokens = [t for t in re.split(SPLIT, ev) if t]
    mass = sum(len(t) for t in tokens) or 1
    pos, found, start, last_end = 0, [], None, None
    for t in tokens:
        i = text.find(t, pos)
        if i < 0:
            continue
        if start is None:
            start = i
        found.append(t)
        last_end = i + len(t)
        pos = i + len(t)
    if start is None or last_end is None:
        return None

    coverage = sum(len(t) for t in found) / mass
    span_len = last_end - start
    if coverage < 0.6 or span_len > max(2.5 * len(ev), len(ev) + 20):
        return None
    ratio = difflib.SequenceMatcher(None, ev, text[start:last_end]).ratio()
    sim = round(0.5 * coverage + 0.5 * ratio, 3)
    if sim >= threshold:
        return (start, last_end, sim)
    return None


def anchor(evidence: str, norm: Normalized, *, min_len: int = MIN_LEN,
           fuzzy_threshold: float = FUZZY_THRESHOLD) -> AnchorResult:
    text = norm.text
    ev = collapse_ws(evidence or "")
    if len(ev) < min_len:
        return AnchorResult(Anchoring(method="none", similarity=0.0,
                                      note=f"片段过短（{len(ev)}<{min_len} 字），不可锚定"), None)
    if not text:
        return AnchorResult(Anchoring(method="none", similarity=0.0, note="规范化文本为空"), None)

    # 1) 精确
    first = text.find(ev)
    if first >= 0:
        alts, start = [], first
        while True:
            i = text.find(ev, start)
            if i < 0:
                break
            if i != first:
                alts.append(CharRange(start=i, end=i + len(ev)))
            start = i + len(ev)
        return AnchorResult(Anchoring(method="exact", similarity=1.0, matched_text=ev),
                            CharRange(start=first, end=first + len(ev)), alts[:5])

    # 2) 空白/分隔符不敏感
    m = re.search(_regex_pattern(ev), text)
    if m:
        matched = m.group(0)
        return AnchorResult(Anchoring(method="ws_insensitive", similarity=0.95, matched_text=matched,
                                      note="空白或分隔符与原文不一致"),
                            CharRange(start=m.start(), end=m.end()))

    # 3) 去噪包含匹配（空白/标点/大小写差异 + 分隔符拼接）
    #    实测教训：模型给的片段经常只是"改写了标点或加了空格"，严格逐字匹配却判为失败 →
    #    该条证据当上"维度最强证据"后触发 unanchored_evidence → 77 分的人被强制转人工。
    dc = _denoised_containment(ev, text)
    if dc:
        s, e, sim = dc
        return AnchorResult(Anchoring(method="ws_insensitive", similarity=round(sim, 3),
                                      matched_text=text[s:e],
                                      note="去空白/标点/大小写后与原文一致（模型改写了标点或空格）"),
                            CharRange(start=s, end=e))

    # 4) 模糊
    fz = _fuzzy_search(ev, text, fuzzy_threshold)
    if fz:
        s, e, ratio = fz
        return AnchorResult(Anchoring(method="fuzzy", similarity=round(ratio, 3), matched_text=text[s:e],
                                      note="片段与原文有差异（模型轻微改写/OCR 噪声）"),
                            CharRange(start=s, end=e))

    return AnchorResult(Anchoring(method="none", similarity=0.0, note="三级策略全部未命中：片段可能被模型改写或不在本文本中"), None)


def highlight(text: str, spans: list[tuple[int, int]]) -> list[tuple[str, bool]]:
    """把若干区间渲染成 [(片段, 是否高亮)]，重叠区间自动合并。供报告层用。"""
    merged: list[list[int]] = []
    for s, e in sorted(spans):
        if merged and s <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    out, prev = [], 0
    for s, e in merged:
        if s > prev:
            out.append((text[prev:s], False))
        out.append((text[s:e], True))
        prev = e
    if prev < len(text):
        out.append((text[prev:], False))
    return out
