# -*- coding: utf-8 -*-
"""离线重放：用库里已存的模型输出，按**指定口径**重新算一遍处置（0 token）。

为什么要单独一层：达标线/权重/开关这些口径参数，改一次就要重跑一次 LLM 的话，
一条线的取舍要花 4 分钟 + 上百次调用。把「模型输出」与「口径」分开之后，改口径是毫秒级的事：
  · `tools/line_sweep.py` 用它扫描达标线（看把人送进哪个口袋）；
  · `app/calibrate.py` 用它把人工标注换成新的达标线（自动校准）。
"""
from __future__ import annotations

from ..contracts import Evaluation, JobConfig
from ..contracts.resume_parse import ResumeParse
from ..store import db
from .equal_split import equal_split_outcome
from .hard_filters import check_hard_filters
from .rules import bonus_layer, decide, rule_layer, sync_decisions_with_evidence
from .stability import apply_gate, latest as stability_latest


def load_rows(cfg: JobConfig, src_version: str | None = None) -> list[dict]:
    """取出某配置版本下的评估结果 + 解析结果，还原成「可重放」的行。"""
    rows = db.fetch_all(
        """SELECT e.payload, e.base_total, r.file_name, a.resume_id
             FROM application a
             JOIN resume r ON r.resume_id = a.resume_id
             LEFT JOIN evaluation e ON e.application_id = a.application_id
            WHERE a.company_id = %s AND a.job_id = %s AND a.config_version = %s
              AND e.payload IS NOT NULL""",
        (cfg.company_id, cfg.job_id, src_version or cfg.config_version))
    out = []
    for r in rows:
        old = Evaluation.model_validate(r["payload"])
        prow = db.fetch_one("SELECT payload FROM resume_parse WHERE resume_id = %s", (r["resume_id"],))
        if not prow:
            continue
        parse = ResumeParse.model_validate(prow["payload"])
        labeled = []
        for d in old.dimensions:
            for it in d.items:
                labeled.append({"item_id": it.item_id, "capability": it.capability, "relevance": it.relevance,
                                "relevance_score": it.relevance_score, "difficulty": it.difficulty,
                                "difficulty_score": it.difficulty_score,
                                "dimension": d.id, "reason": it.reason,
                                "confidence": it.confidence, "span_id": it.span_id})
        for u in old.unmapped_items:
            labeled.append({"item_id": u.item_id, "capability": u.capability, "relevance": u.relevance,
                            "difficulty": u.difficulty, "dimension": None, "reason": u.reason,
                            "confidence": None, "span_id": u.span_id})
        out.append({"file": r["file_name"], "resume_id": r["resume_id"], "parse": parse,
                    "labeled": labeled, "old": old})
    return out


def _finish(cfg: JobConfig, outcome, *, gate: bool):
    """离线重放的收尾：默认也过稳定性闸门——线上与离线必须是同一套政策，否则调线会调出假象。"""
    if not gate:
        return outcome
    return apply_gate(outcome, stability_latest(cfg.job_id, cfg.config_version),
                      job_id=cfg.job_id, config_version=cfg.config_version)


def decide_with_line(cfg: JobConfig, row: dict, line: float, band: float,
                     reject_border_when_low: bool | None = None, *, gate: bool = True) -> str:
    """按指定达标线重算处置（模型输出用库里存的，只有分数线在变）。"""
    parse, labeled, old = row["parse"], row["labeled"], row["old"]
    upd = {"pass_line": line, "band_width": band}
    if reject_border_when_low is not None:
        upd["reject_border_when_low"] = reject_border_when_low
    sc = cfg.scoring.model_copy(update=upd)
    cfg2 = cfg.model_copy(update={"scoring": sc})
    rules = rule_layer(cfg2, labeled)
    bonus = bonus_layer(cfg2, parse, labeled)
    hard = check_hard_filters(cfg2, parse, old.llm_compare)
    anchored = {c.span_id: c.anchored for c in old.cited_evidence}
    outcome = decide(cfg2, rules.base_total, old.llm_compare, old.fit, old.stage_status, set(anchored), hard,
                     dim_points={d.id: d.points for d in rules.dimensions},
                     bonus_names={b.name for b in bonus})
    outcome = sync_decisions_with_evidence(outcome, anchored, [d.best_span_id or "" for d in rules.dimensions])
    return _finish(cfg, outcome, gate=gate).decision


def decide_with_equal_split(cfg: JobConfig, row: dict, line: float, band: float, *,
                            budget: float = 60.0, basic_items: list[str] | None = None,
                            aggregation: str = "best", use_continuous: bool = True, quality_baseline: float | None = None,
                            reject_border_when_low: bool | None = None, gate: bool = True) -> str:
    """按**等分制**算分 + 指定达标线重算处置（离线，0 token）。

    与 decide_with_line 的唯一区别：分数由 equal_split 产出（每项基础分 = budget/N × 相似度 × 难度），
    双通道判定/硬门槛/冲突检测/三段带全部照旧——口径变了，判定机制不变，才谈得上可比。
    """
    upd = {"pass_line": line, "band_width": band}
    if reject_border_when_low is not None:
        upd["reject_border_when_low"] = reject_border_when_low
    cfg2 = cfg.model_copy(update={"scoring": cfg.scoring.model_copy(update=upd)})
    rules = equal_split_outcome(cfg2, row["labeled"], budget=budget, basic_items=basic_items,
                                aggregation=aggregation, use_continuous=use_continuous,
                                quality_baseline=quality_baseline)
    parse, old = row["parse"], row["old"]
    bonus = bonus_layer(cfg2, parse, row["labeled"])
    hard = check_hard_filters(cfg2, parse, old.llm_compare)
    anchored = {c.span_id: c.anchored for c in old.cited_evidence}
    outcome = decide(cfg2, rules.base_total, old.llm_compare, old.fit, old.stage_status, set(anchored), hard,
                     dim_points={d.id: d.points for d in rules.dimensions},
                     bonus_names={b.name for b in bonus})
    outcome = sync_decisions_with_evidence(outcome, anchored, [d.best_span_id or "" for d in rules.dimensions])
    return _finish(cfg, outcome, gate=gate).decision
