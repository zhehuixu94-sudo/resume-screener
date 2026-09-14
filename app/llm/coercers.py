# -*- coding: utf-8 -*-
"""五段输出的清洗器：把模型爱写的同义写法归一到受控词表，并记录每一次改动。

原则：
- 能被确定性修正的（枚举同义、百分数置信度、fit_ratio 重算、条目顺序对齐）→ 就地修，记 repairs；
- 属于"内容缺失"的（字段没给、给的是垃圾）→ 不动，交给校验失败触发带反馈的重试；
- 属于"模型不该管的"（自报维度名、改写证据片段）→ 以系统侧为准，记 repairs。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..contracts.coerce import (
    ANCHOR_MAP,
    DIFFICULTY_MAP,
    FIT_MAP,
    LEVEL_MAP,
    RELEVANCE_MAP,
    VERDICT_MAP,
    clean_confidence,
    clean_enum,
    clean_free_text,
    drop_unknown,
)

# 连续值的兜底映射（模型没给 relevance_score / difficulty_score 时按标签推导）
RELEVANCE_SCORE = {"强": 1.0, "中": 0.66, "弱": 0.33, "无关": 0.0}      # 强 = 1.0 是基准
DIFFICULTY_SCORE = {"高": 1.5, "中": 1.0, "低": 0.5}                     # 中 = 1.0 是基准


def _score_from_label(value, fallback: float, lo: float, hi: float, field: str,
                      repairs: list[str]) -> float:
    """连续值：模型给了就用（夹到合法区间）；没给或不合法 → 用标签推导的兜底值。"""
    try:
        x = float(value)
    except (TypeError, ValueError):
        if value not in (None, ""):
            repairs.append(f"{field}: 非数值 {value!r} → 按标签推导 {fallback}")
        return fallback
    if x < lo or x > hi:
        repairs.append(f"{field}: {x} 越界 → 夹到 [{lo}, {hi}]")
        return min(max(x, lo), hi)
    return x

from ..contracts.llm_outputs import (
    LlmCompareOutput,
    LlmCoverOutput,
    LlmFitOutput,
    LlmLabelOutput,
    LlmParseOutput,
    LlmQuestionsOutput,
)
from ..text.normalize import collapse_ws

MAX_QUESTIONS = 3
MIN_EVIDENCE = 4


@dataclass
class CoerceResult:
    """清洗结果：数据 + 修正记录（repairs）+ 丢弃的未知字段（dropped）。"""

    data: dict[str, Any]
    repairs: list[str] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)


# ---------------------------------------------------------------- ① 解析
def coerce_parse(data: dict[str, Any], job: Any = None) -> CoerceResult:
    repairs: list[str] = []
    dropped: list[str] = []
    data = drop_unknown(LlmParseOutput, data, "", dropped)
    blocks_out = []
    for bi, b in enumerate(data.get("blocks") or []):
        if not isinstance(b, dict):
            continue
        items_out = []
        for ii, it in enumerate(b.get("items") or []):
            if not isinstance(it, dict):
                continue
            ev = collapse_ws(str(it.get("evidence") or ""))
            if len(ev) < MIN_EVIDENCE:                # 没证据的条目直接丢（记 repair，不烧一次重试）
                repairs.append(f"blocks[{bi}].items[{ii}]: evidence 缺失或过短（{len(ev)} 字）→ 丢弃该条")
                continue
            if len(ev) > 80:
                ev = ev[:80]
                repairs.append(f"blocks[{bi}].items[{ii}]: evidence 超长 → 截断到 80 字")
            item = {"capability": collapse_ws(str(it.get("capability") or "")), "evidence": ev,
                    "note": str(it.get("note") or "")}
            if len(item["capability"]) < 2:
                item["capability"] = ev[:12]
                repairs.append(f"blocks[{bi}].items[{ii}]: capability 缺失 → 用证据片段前 12 字兜底")
            items_out.append(item)
        block = {"section": str(b.get("section") or "未分类"), "title": str(b.get("title") or b.get("section") or "未命名块"),
                 "period": str(b.get("period") or ""), "items": items_out}
        if not items_out:
            repairs.append(f"blocks[{bi}] 无有效条目 → 丢弃该块")
            continue
        blocks_out.append(block)
    data["blocks"] = blocks_out

    pq = data.get("parse_quality") or {}
    sections = [str(s) for s in (pq.get("sections_found") or []) if str(s).strip()]
    if not sections:
        sections = [b["section"] for b in blocks_out]
        repairs.append("parse_quality.sections_found 为空 → 用块 section 回填")
    data["parse_quality"] = {"sections_found": sections,
                             "issues": [str(x) for x in (pq.get("issues") or [])]}
    return CoerceResult(data=data, repairs=repairs, dropped=dropped)


# ---------------------------------------------------------------- ② 标注
def coerce_label_factory(flat_items: list[dict[str, Any]], job: Any):
    """标注段清洗需要对齐输入清单（idx → 解析段的能力项），所以做成工厂。"""
    by_idx = {int(i["idx"]): i for i in flat_items}
    dim_ids = set(job.dimension_ids) if job else set()
    name2id = {d.name: d.id for d in job.dimensions} if job else {}

    def coerce(data: dict[str, Any], _job: Any = None) -> CoerceResult:
        repairs: list[str] = []
        dropped: list[str] = []
        data = drop_unknown(LlmLabelOutput, data, "", dropped)
        out = []
        for k, it in enumerate(data.get("items") or []):
            if not isinstance(it, dict):
                continue
            try:
                idx = int(it.get("idx"))
            except (TypeError, ValueError):
                repairs.append(f"items[{k}]: idx 非法（{it.get('idx')!r}）→ 丢弃该条")
                continue
            src = by_idx.get(idx)
            if src is None:
                repairs.append(f"items[{k}]: idx={idx} 不在输入清单里 → 丢弃该条")
                continue
            rec = dict(it)
            rec["idx"] = idx
            rec["relevance"] = clean_enum(rec.get("relevance"), RELEVANCE_MAP, f"items[{k}].relevance", repairs)
            rec["difficulty"] = clean_enum(rec.get("difficulty"), DIFFICULTY_MAP, f"items[{k}].difficulty", repairs)
            rec["relevance_score"] = _score_from_label(
                rec.get("relevance_score"), RELEVANCE_SCORE.get(rec["relevance"], 0.0), 0.0, 1.0,
                f"items[{k}].relevance_score", repairs)
            rec["difficulty_score"] = _score_from_label(
                rec.get("difficulty_score"), DIFFICULTY_SCORE.get(rec["difficulty"], 1.0), 0.5, 1.5,
                f"items[{k}].difficulty_score", repairs)
            rec["confidence"] = clean_confidence(rec.get("confidence"), f"items[{k}].confidence", repairs, default=0.0)
            # 维度：先按 id，再按名称映射；都映射不到 → None（不硬塞）
            dim = rec.get("dimension")
            if isinstance(dim, str):
                dim = dim.strip()
                if dim and dim not in dim_ids:
                    if dim in name2id:
                        repairs.append(f"items[{k}].dimension: 维度名 {dim!r} → id {name2id[dim]!r}")
                        dim = name2id[dim]
                    else:
                        repairs.append(f"items[{k}].dimension: {dim!r} 不在岗位维度里 → 置空（进未归类）")
                        dim = None
            else:
                dim = None
            rec["dimension"] = dim
            # 证据：以解析段为准（改写证据会让锚定与高亮失效）
            if collapse_ws(str(rec.get("evidence") or "")) != src["evidence"]:
                if rec.get("evidence"):
                    repairs.append(f"items[{k}].evidence: 与解析段不一致 → 采用解析段原文")
                rec["evidence"] = src["evidence"]
            rec["capability"] = str(rec.get("capability") or src["capability"])
            rec["block"] = str(rec.get("block") or src["block"])
            rec.setdefault("reason", "")
            out.append(rec)

        # 模型漏标的条目 → 用解析段补齐，标成中性并声明降级（不许悄悄少算维度分）
        missing = [i for i in flat_items if int(i["idx"]) not in {r["idx"] for r in out}]
        for m in missing:
            out.append({"idx": int(m["idx"]), "capability": m["capability"], "evidence": m["evidence"],
                        "block": m["block"], "relevance": "中", "difficulty": "中",
                        "dimension": None, "reason": "模型未标注，系统按中性值兜底", "confidence": 0.0})
            repairs.append(f"idx={m['idx']}: 模型漏标 → 中性兜底（相关性中 × 难度中）")
        data["items"] = out
        return CoerceResult(data=data, repairs=repairs, dropped=dropped)

    return coerce


# ---------------------------------------------------------------- ③ 整体判定
def coerce_compare(data: dict[str, Any], job: Any = None) -> CoerceResult:
    repairs: list[str] = []
    dropped: list[str] = []
    data = drop_unknown(LlmCompareOutput, data, "", dropped)
    data["verdict"] = clean_enum(data.get("verdict"), VERDICT_MAP, "verdict", repairs)
    data["closest_anchor"] = clean_enum(data.get("closest_anchor"), ANCHOR_MAP, "closest_anchor", repairs)
    conf = clean_confidence(data.get("confidence"), "confidence", repairs, default=0.0)
    data["confidence"] = conf
    if conf == 0.0:
        repairs.append("confidence 缺失/无法解析 → 置 0.0（低于置信度阈值将强制转人工，不允许据此自动淘汰）")
    data["veto_triggered"] = clean_free_text(data.get("veto_triggered"), min_len=6, field="veto_triggered", repairs=repairs)
    valid_fids = {f.id for f in job.hard_filters} if job else set()
    ids = data.get("veto_filter_ids")
    if not isinstance(ids, list):
        ids = []
        if data["veto_triggered"]:
            repairs.append("veto_filter_ids 缺失 → 置空（判定层将转人工，不自动淘汰）")
    clean_ids = [str(x).strip() for x in ids if str(x).strip()]
    unknown = [x for x in clean_ids if valid_fids and x not in valid_fids]
    if unknown:
        repairs.append(f"veto_filter_ids: 出现不在配置里的 id {unknown} → 丢弃")
    data["veto_filter_ids"] = [x for x in clean_ids if x in valid_fids]
    for key in ("strengths", "gaps"):
        vals = data.get(key)
        data[key] = [str(x) for x in vals if str(x).strip()] if isinstance(vals, list) else []
    return CoerceResult(data=data, repairs=repairs, dropped=dropped)


# ---------------------------------------------------------------- ④ 匹配度
def coerce_fit(data: dict[str, Any], job: Any = None) -> CoerceResult:
    repairs: list[str] = []
    dropped: list[str] = []
    data = drop_unknown(LlmFitOutput, data, "", dropped)
    for key in ("matched", "partial", "unmatched"):
        vals = data.get(key)
        data[key] = [{"item": str(v.get("item")), "why": str(v.get("why") or "")}
                     for v in (vals if isinstance(vals, list) else []) if isinstance(v, dict) and str(v.get("item") or "").strip()]
    total = len(data["matched"]) + len(data["partial"]) + len(data["unmatched"])
    ratio = round((len(data["matched"]) + 0.5 * len(data["partial"])) / total, 2) if total else 0.0
    given = clean_confidence(data.get("fit_ratio"), "fit_ratio", repairs, default=None)
    if given is None or abs(float(given) - ratio) > 0.05:
        repairs.append(f"fit_ratio: {data.get('fit_ratio')!r} 与三清单重算值 {ratio} 不符 → 以重算值为准")
    data["fit_ratio"] = ratio
    data["verdict"] = clean_enum(data.get("verdict"), FIT_MAP, "verdict", repairs)
    return CoerceResult(data=data, repairs=repairs, dropped=dropped)


# ---------------------------------------------------------------- ④a 按维度取证
def coerce_cover(data: dict[str, Any], job: Any = None) -> CoerceResult:
    """清洗定向取证输出：维度 id 必须落在岗位配置里，证据片段要够长，空维度显式标 found=false。"""
    repairs: list[str] = []
    dropped: list[str] = []
    data = drop_unknown(LlmCoverOutput, data, "", dropped)
    valid_ids = list(getattr(job, "dimension_ids", []) or [])
    seen: dict[str, dict] = {}
    for d in data.get("dimensions") or []:
        if not isinstance(d, dict):
            continue
        did = str(d.get("id") or "").strip()
        if valid_ids and did not in valid_ids:
            dropped.append(f"取证维度 id 非法：{did!r}")
            continue
        items = []
        for it in d.get("items") or []:
            if not isinstance(it, dict):
                continue
            ev = collapse_ws(str(it.get("evidence") or ""))
            cap = str(it.get("capability") or "").strip()
            if len(ev) < 4 or not cap:
                dropped.append(f"{did}: 证据过短或缺能力项名")
                continue
            items.append({"capability": cap, "evidence": ev[:90]})
        found = bool(d.get("found")) and bool(items)
        if bool(d.get("found")) and not items:
            repairs.append(f"{did}: 标了 found=true 但没给证据 → 记为无证据")
        seen[did] = {"id": did, "found": found, "note": str(d.get("note") or "")[:200], "items": items[:3]}
    # 配置里有、模型没答的维度 → 显式补成 found=false（不许静默漏掉一个维度）
    for did in valid_ids:
        if did not in seen:
            seen[did] = {"id": did, "found": False, "note": "模型未给出该维度结论 → 按无证据处理", "items": []}
            repairs.append(f"{did}: 模型漏答该维度 → 补为无证据")
    data["dimensions"] = [seen[d] for d in valid_ids] if valid_ids else list(seen.values())
    return CoerceResult(data=data, repairs=repairs, dropped=dropped)


# ---------------------------------------------------------------- ⑤ 面试题
def coerce_questions(data: dict[str, Any], job: Any = None) -> CoerceResult:
    repairs: list[str] = []
    dropped: list[str] = []
    data = drop_unknown(LlmQuestionsOutput, data, "", dropped)
    out = []
    for k, q in enumerate(data.get("questions") or []):
        if not isinstance(q, dict):
            continue
        q = dict(q)
        q["level"] = clean_enum(q.get("level"), LEVEL_MAP, f"questions[{k}].level", repairs)
        q["difficulty"] = clean_enum(q.get("difficulty"), DIFFICULTY_MAP, f"questions[{k}].difficulty", repairs)
        if len(str(q.get("question") or "")) < 5:
            repairs.append(f"questions[{k}]: 题干过短 → 丢弃该题")
            continue
        q.setdefault("target", "")
        q.setdefault("intent", "")
        q.setdefault("red_flag", "")
        out.append(q)
    if len(out) > MAX_QUESTIONS:                       # 三档各一道，多的丢掉
        by_level: dict[str, dict] = {}
        for q in out:
            by_level.setdefault(q["level"], q)
        out = list(by_level.values())[:MAX_QUESTIONS]
        repairs.append(f"questions: 超过 {MAX_QUESTIONS} 道 → 按 level 去重截断")
    data["questions"] = out
    return CoerceResult(data=data, repairs=repairs, dropped=dropped)


# ---------------------------------------------------------------- 动态业务校验
def make_label_check(flat_items: list[dict], job: Any):
    dim_ids = set(job.dimension_ids)

    def check(data: dict[str, Any], notes: list[str]) -> None:
        items = data.get("items") or []
        unmapped = [i for i in items if i.get("dimension") is None]
        if unmapped:
            notes.append(f"{len(unmapped)} 条未能归入维度（进未归类，不计维度分）")
        if len(items) != len(flat_items):
            notes.append(f"标注条数 {len(items)} ≠ 输入条数 {len(flat_items)}（已按 idx 对齐）")
        bad = sorted({i.get("dimension") for i in items if i.get("dimension") and i["dimension"] not in dim_ids})
        if bad:
            notes.append(f"出现不在岗位配置里的维度 id：{bad}")

    return check


def make_questions_check(expected_levels=("基础盘", "深度", "上限")):
    def check(data: dict[str, Any], notes: list[str]) -> None:
        qs = data.get("questions") or []
        levels = [q.get("level") for q in qs]
        missing = [l for l in expected_levels if l not in levels]
        if missing:
            notes.append(f"面试题缺档：{missing}（要求低/中/高三档各一）")
        if len({q.get("difficulty") for q in qs}) == 1 and qs:
            notes.append("3 道题难度相同，失去区分度")

    return check
