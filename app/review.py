#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""复核 / 抽检（后端 + 命令行；界面下一轮做）。

两个「库」的落地方式（不复制数据，避免双写漂移）：
  · 自动淘汰库 = `evaluation.decision = 'auto_reject'` 的件  → **抽检从它抽**（漏放风险就在这里）
  · 人工淘汰库 = `review_decision.action='reject'` 且未清理的件 → 抽检不再抽它，支持手动清理（软删除 + 审计）

抽检三条纪律（都是明确要求，别改）：
  1. **抽过不再抽**：`spot_check_item (batch_id, application_id)` 唯一 + 已复核过的永久排除；
  2. **不累加**：一天一批；旧批次的未复核项标记 `expired` 后不再展示，第二天仍是 5 条；
  3. **未复核不算被抽过**：旧批次里没人看的件仍可能被抽中——否则它既没人看、又永不入库，漏放样本就丢了。

复核动作（HR 逐份看：通过 / 淘汰）：
  · 通过 = 系统把它自动淘汰是错的（漏放）→ 记一条人工标注，并把简历归档到 `<archive>/简历/<日期>/`；
  · 淘汰 = 系统判对了 → 该件进人工淘汰库，抽检不再抽。

用法：
    python -m app.review pool    --job config/job_game_dev.json
    python -m app.review draw    --job config/job_game_dev.json --want 5
    python -m app.review list    --job config/job_game_dev.json
    python -m app.review pass    --job config/job_game_dev.json --application 123 [--note "项目经历扎实"]
    python -m app.review reject  --job config/job_game_dev.json --application 124
    python -m app.review clear   --job config/job_game_dev.json --application 124   # 人工淘汰库手动清理（软删除）
    python -m app.review stats   --job config/job_game_dev.json                     # 含「当日误差数」→ 决定是否该校准
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from .contracts import JobConfig                 # noqa: E402
from .pipeline.stats import leak_upper_bound, samples_needed   # noqa: E402
from .store import Repository, db                # noqa: E402

DEFAULT_ARCHIVE_DIR = os.environ.get("RESUME_ARCHIVE_DIR") or "out/passed"
CN_TZ = ZoneInfo("Asia/Shanghai")          # 「新的一天」按北京时间过 0 点算（不跟机器时区走）


def today_cn() -> str:
    """当天的日期（北京时间）。抽检批次、归档目录都用它，避免机器时区/UTC 导致的跨天错位。"""
    return datetime.now(CN_TZ).date().isoformat()
ARCHIVE_TOP = "简历"                              # 通过件归档顶层目录名（子目录按天）
ARCHIVE_REJECT_TOP = "淘汰"                        # 淘汰件归档顶层目录名（要留证：复看/申诉/复现）
DEFAULT_WANT = 5                                  # 每天抽几份（旧口径：固定份数，1000 份规模下覆盖太低）
# 分层抽检（推荐，按批）：漏放几乎只发生在"擦线带"，人力要集中在那儿
STRATUM_RATIOS = (0.50, 0.10, 0.03)               # 擦线带 / 中间带 / 远端 的抽检比例
NEAR_WIDTH = 10.0                                 # 擦线带宽度：分数在 [线-band-10, 线-band)
MID_WIDTH = 30.0                                  # 中间带：再往下 10~30 分


def strata_of(score: float, line: float, band: float) -> str:
    """按分数把被淘汰的简历分层——越靠近线，判定越容易漂，越要人工看。"""
    cut = line - band                              # 低于它是自动淘汰判据
    if cut - NEAR_WIDTH <= score < cut:
        return "near"                              # 擦线带（重点）
    if cut - MID_WIDTH <= score < cut - NEAR_WIDTH:
        return "mid"                               # 中间带
    return "far"                                   # 远端


# ---------------------------------------------------------------- 抽检池与标记位（路线 A）
# 池子从「只有自动淘汰件」扩成两类，各自带标记；不复制数据（避免双写漂移）——都从同一张 evaluation 读。
# 背景：冷启动期自动淘汰件少，实测池子 0~33 件 → 攒不出人工标注 → 校准永远「样本不足」。空转比关掉更危险。
SOURCE_AUTO_REJECT = "auto_reject"          # 自动淘汰件（漏放风险的直接来源，唯一能进校准池的一类）
SOURCE_MANUAL_REVIEW = "manual_review"      # 其余转人工件（按分数降序抽）
SOURCE_AUTO_PASS = "auto_pass"              # 自动达标件（默认不抽；--include-auto-pass 才纳入）
SOURCE_BORDERLINE = "borderline"            # 擦线件：|分 − 自动达标线| ≤ 2
SOURCE_MODEL_CONFLICT = "model_conflict"    # 模型两路判据互相打架
SOURCE_HARD_GATE_UNKNOWN = "hard_gate_unknown"   # 硬门槛证据不足（unknown）——人工最容易补判

# 抽样优先级（用户的路线 A 口径）：擦线 → 打架 → 硬门槛 unknown → 其余按分数降序
PRIORITY_CLASSES = ("borderline", "model_conflict", "hard_gate_unknown", "descending")
PRIORITY_LABEL = {"borderline": "擦线", "model_conflict": "打架",
                  "hard_gate_unknown": "unknown", "descending": "降序"}
BORDERLINE_WIDTH = 2.0                      # 擦线带宽：|分 − 线| ≤ 2
# ⚠️「打架」只认这两类**判据冲突**。实测教训：把 `edge_band` 也算进来会让 155/236 件都变成「打架」
#    （它只是"分数落在线内"的标记，不是两路矛盾）——一个标记 96% 的件都命中，等于没有优先级。
CONFLICT_TYPES = ("llm_vs_rule", "fit_vs_verdict")


def classify_source(decision: str, score: float, line: float, conflict_types: set[str],
                    hard_gate_unknown: bool) -> str:
    """一条件属于哪一类（决定能不能进校准池）。互斥、有优先级、可复现。

    优先级：auto_reject > 擦线 > 打架 > 硬门槛 unknown > auto_pass > manual_review
    （auto_reject 优先是因为它是唯一能进校准池的一类，标记不能丢；其余按抽样优先级排。）
    """
    if decision == "auto_reject":
        return SOURCE_AUTO_REJECT
    if abs(float(score) - float(line)) <= BORDERLINE_WIDTH:
        return SOURCE_BORDERLINE
    if set(conflict_types) & set(CONFLICT_TYPES):
        return SOURCE_MODEL_CONFLICT
    if hard_gate_unknown:
        return SOURCE_HARD_GATE_UNKNOWN
    if decision == "auto_pass":
        return SOURCE_AUTO_PASS
    return SOURCE_MANUAL_REVIEW


def priority_class(source: str, decision: str, conflict_types: set[str], hard_gate_unknown: bool) -> str:
    """抽样优先级类别（与 source 解耦：auto_reject 是"哪一类"，它仍可能有打架/unknown 等抽取优先级）。"""
    if source == SOURCE_BORDERLINE:
        return "borderline"                                   # ① 擦线件（最可能被判错）
    if set(conflict_types) & set(CONFLICT_TYPES):
        return "model_conflict"                               # ② 模型内部打架
    if hard_gate_unknown:
        return "hard_gate_unknown"                            # ③ 硬门槛证据不足
    return "descending"                                       # ④ 其余按分数降序


def _row_labels(r: dict, cfg: JobConfig) -> dict:
    """给一行候选算出 source / priority_class / 是否能进校准池（纯函数，离线与线上共用）。"""
    ev = r.get("payload") or {}
    ct = {c.get("type") for c in (ev.get("conflicts") or [])}
    unk = any(h.get("result") == "unknown" for h in (ev.get("hard_filters") or []))
    line = float(cfg.scoring.pass_threshold())
    src = classify_source(r["decision"], float(r["gate_score"] or 0), line, ct, unk)
    return {"source": src, "priority_class": priority_class(src, r["decision"], ct, unk),
            "calibration_eligible_candidate": src == SOURCE_AUTO_REJECT,
            "conflicts": sorted(ct), "hard_gate_unknown": unk}


def _pick_by_priority(rows: list[dict], cfg: JobConfig, want: int | None) -> tuple[list[dict], dict]:
    """按优先级抽：擦线 → 打架 → unknown → 其余分数降序；并**保证非空类别每类至少 1 条**。

    为什么要"每类保底 1 条"：纯按优先级排，前两类（擦线+打架）会把名额吃光，
    降序类与 unknown 类永远进不了抽样 → 分布检查看起来"只有两类非空"，
    实际是抽取器把它们排除了，而不是本批没有。
    """
    labeled = []
    for r in rows:
        lab = _row_labels(r, cfg)
        labeled.append({**r, **lab})
    buckets = {k: [] for k in PRIORITY_CLASSES}
    for r in labeled:
        buckets[r["priority_class"]].append(r)
    for k in buckets:
        buckets[k].sort(key=lambda z: (-float(z["gate_score"] or 0), str(z["file_name"])))
    total = sum(len(v) for v in buckets.values())
    if want is None or want >= total:
        picked = [r for k in PRIORITY_CLASSES for r in buckets[k]]
        return picked, {"pool": total, "class_size": {k: len(v) for k, v in buckets.items()},
                        "class_picked": {k: len(buckets[k]) for k in PRIORITY_CLASSES}}
    picked, taken = [], {k: 0 for k in PRIORITY_CLASSES}
    for k in PRIORITY_CLASSES:                                 # 第一轮：每类保底 1 条
        if buckets[k] and len(picked) < want:
            picked.append(buckets[k][0]); taken[k] = 1
    for k in PRIORITY_CLASSES:                                 # 第二轮：按优先级填满
        for r in buckets[k][taken[k]:]:
            if len(picked) >= want:
                break
            picked.append(r); taken[k] += 1
    picked.sort(key=lambda z: (PRIORITY_CLASSES.index(z["priority_class"]),
                               -float(z["gate_score"] or 0), str(z["file_name"])))
    return picked, {"pool": total, "class_size": {k: len(v) for k, v in buckets.items()},
                    "class_picked": taken}


# 抽样配额（用户口径，want=40 时的一版）：样本要**同时服务两个目的**——
#   · 校准池只认 auto_reject（33 件，抽 30% = 10 条）；
#   · 其余类是"发现型"（找规则问题，不进校准池）；
#   · 纯按优先级填充会让打架类吃掉 85% 的名额（实测 34/40），那是"优先级列表"不是抽样：
#     打架类抽样率 31%、降序类 0.6% → 抽样率差 50 倍，用它推断池子结构是错的。
# 配额按 **source** 口径给（manual_review ≡ 降序类：转人工件里不擦线/不打架/不 unknown 的，按定义就在降序类）。
SAMPLE_WEIGHTS = {
    SOURCE_AUTO_REJECT: 10,          # 校准池来源（全池 33 → 抽 30%）
    SOURCE_BORDERLINE: 5,            # 擦线件：全抽（池子里只有 5）
    SOURCE_MODEL_CONFLICT: 10,       # 发现型：降采样（池子 48）
    SOURCE_MANUAL_REVIEW: 15,        # 降序类：主体必须覆盖（池子 150）
    SOURCE_HARD_GATE_UNKNOWN: 0,     # 本批为空；不为凑分布造语料
    SOURCE_AUTO_PASS: 0,             # 默认不抽自动达标件
}
MAX_BUCKET_SHARE = 0.40              # 单一类占比上限（超过说明又变回优先级列表了）


def _allocate(want: int | None, weights: dict[str, int], sizes: dict[str, int]) -> dict[str, int]:
    """按权重配额分配名额：先按比例取整 → 余数按小数部分补 → 再按权重顺序补 → 不足则让位。

    另外保留**非空类保底 1 条**（用户口径）：某类池子非空但比例算下来是 0 时，
    从权重最低且有余量的类挪一个名额给它——否则"本类为空"和"本类被抽取器排除"分不清。
    """
    total = sum(int(v or 0) for v in sizes.values())
    if want is None or want >= total:
        return {k: int(sizes.get(k, 0) or 0) for k in weights}
    tw = sum(w for k, w in weights.items() if int(sizes.get(k, 0) or 0) > 0) or 1
    ideal = {k: (want * w / tw if int(sizes.get(k, 0) or 0) > 0 else 0.0) for k, w in weights.items()}
    alloc = {k: min(int(ideal[k]), int(sizes.get(k, 0) or 0)) for k in weights}
    left = want - sum(alloc.values())
    for k in sorted(weights, key=lambda z: (-(ideal[z] - int(ideal[z])), -weights.get(z, 0), z)):
        if left <= 0:
            break
        if int(sizes.get(k, 0) or 0) > alloc[k]:
            alloc[k] += 1
            left -= 1
    while left > 0:
        moved = False
        for k in sorted(weights, key=lambda z: (-weights.get(z, 0), z)):
            if left <= 0:
                break
            if int(sizes.get(k, 0) or 0) > alloc[k]:
                alloc[k] += 1
                left -= 1
                moved = True
        if not moved:
            break
    for k, sz in sizes.items():                       # 非空类保底 1 条
        if int(sz or 0) > 0 and alloc.get(k, 0) == 0:
            donors = [d for d in weights if alloc.get(d, 0) > 1]
            if donors:
                d = min(donors, key=lambda z: (weights.get(z, 0), z))
                alloc[d] -= 1
                alloc[k] = 1
    return alloc


def _pick_by_quota(rows: list[dict], cfg: JobConfig, want: int | None,
                   weights: dict[str, int] | None = None) -> tuple[list[dict], dict]:
    """按配额抽样：每个 source 桶按权重拿名额，桶内按（优先级类别 → 分数降序）排序。"""
    labeled = [{**r, **_row_labels(r, cfg)} for r in rows]
    w = weights or SAMPLE_WEIGHTS
    buckets: dict[str, list[dict]] = {k: [] for k in w}
    for r in labeled:
        buckets.setdefault(r["source"], []).append(r)
    sizes = {k: len(v) for k, v in buckets.items()}
    alloc = _allocate(want, w, sizes)
    picked: list[dict] = []
    for k, need in alloc.items():
        buckets[k].sort(key=lambda z: (PRIORITY_CLASSES.index(z["priority_class"]),
                                       -float(z["gate_score"] or 0), str(z["file_name"])))
        picked += buckets[k][:need]
    picked.sort(key=lambda z: (list(w).index(z["source"]) if z["source"] in w else 99,
                               PRIORITY_CLASSES.index(z["priority_class"]),
                               -float(z["gate_score"] or 0), str(z["file_name"])))
    return picked, {"pool": len(labeled), "class_size": sizes, "class_picked": alloc}


# ---------------------------------------------------------------- 池子
POOL_SQL = """
SELECT a.application_id, a.resume_id, r.file_name, r.file_path, e.gate_score, e.verdict, e.decision,
       e.payload,
       EXISTS (SELECT 1 FROM spot_check_item i
                WHERE i.application_id = a.application_id AND i.state IN ('pending', 'passed', 'rejected'))
         AS drawn_before,
       EXISTS (SELECT 1 FROM review_decision d
                WHERE d.application_id = a.application_id AND d.cleared_at IS NULL) AS reviewed
  FROM application a
  JOIN evaluation e ON e.application_id = a.application_id
  JOIN resume r ON r.resume_id = a.resume_id
 WHERE a.company_id = %s AND a.job_id = %s AND a.config_version = %s
   AND e.decision = ANY(%s)
 ORDER BY e.gate_score ASC, random()
"""

DEFAULT_POOL_SOURCES = (SOURCE_AUTO_REJECT, SOURCE_MANUAL_REVIEW)


def pool(cfg: JobConfig, *, sources: tuple[str, ...] = DEFAULT_POOL_SOURCES,
         with_labels: bool = True) -> list[dict]:
    """抽检池：还没人工结论的件（已复核的不再出现）。

    默认两类 = 自动淘汰件（漏放风险的直接来源）+ 转人工件（冷启动期唯一可观的量）。
    自动达标件默认不进池（有稳定性闸门兜着，抽检成本留给风险段）；要抽就显式传 sources。
    """
    rows = db.fetch_all(POOL_SQL, (cfg.company_id, cfg.job_id, cfg.config_version, list(sources)))
    out = [r for r in rows if not r["reviewed"]]
    if with_labels:
        out = [{**r, **_row_labels(r, cfg)} for r in out]
    return out


def manual_reject_library(cfg: JobConfig) -> list[dict]:
    """人工淘汰库（软删除的不算）：HR 手动淘汰 + 抽检认定该淘汰的。"""
    return db.fetch_all(
        """SELECT d.review_id, d.application_id, d.resume_id, r.file_name, d.action, d.source, d.note,
                  d.created_at, d.cleared_at
             FROM review_decision d
             LEFT JOIN resume r ON r.resume_id = d.resume_id
            WHERE d.company_id = %s AND d.job_id = %s AND d.action = 'reject' AND d.cleared_at IS NULL
            ORDER BY d.created_at DESC""",
        (cfg.company_id, cfg.job_id))


# ---------------------------------------------------------------- 抽检
def _pick_stratified(rows: list[dict], cfg: JobConfig, want: int | None) -> tuple[list[dict], dict]:
    """分层抽检：按分数段配额抽，而不是"谁先来抽谁"。

    为什么这么抽：实测同一份简历重复跑总分极差可达 8 分，所以**擦线带**（分数刚低于线−band）
    才是漏放高发区；远端（分数低 30 分以上）几乎不可能漏放。把人力按 50%/10%/3% 分配，
    整体漏放才能压到 1% 以内，而不用把淘汰件全看一遍。
    """
    line, band = float(cfg.scoring.pass_line), float(cfg.scoring.band_width)
    buckets: dict[str, list[dict]] = {"near": [], "mid": [], "far": []}
    for r in rows:
        buckets[strata_of(float(r["gate_score"]), line, band)].append(r)
    total = sum(len(v) for v in buckets.values())
    ratio = {k: v for k, v in zip(("near", "mid", "far"), STRATUM_RATIOS)}
    quota = {k: int(len(buckets[k]) * ratio[k]) for k in buckets}
    if want is not None:                           # 给了总量上限就按比例缩放
        ask = sum(quota.values())
        if ask > want and ask > 0:
            for k in quota:
                quota[k] = int(quota[k] * want / ask)
    picked: list[dict] = []
    for k in ("near", "mid", "far"):               # 擦线带优先占额度
        picked += buckets[k][:quota[k]]
    return picked[:want] if want is not None else picked, {
        "strata_size": {k: len(v) for k, v in buckets.items()},
        "strata_quota": quota, "pool": total}


def draw(cfg: JobConfig, *, want: int | None = None, check_date: str | None = None,
         archive_dir: str = DEFAULT_ARCHIVE_DIR, stratified: bool = True) -> dict:
    """生成本日抽检清单。旧批次的未复核项先过期（不累加）。

    stratified=True（默认）：按擦线带/中间带/远端的比例抽（1000 份规模下抽 10~12%）。
    stratified=False：老的"固定 want 份"口径（want 默认 5）。
    """
    day = check_date or today_cn()

    # 不累加：昨天没看的清单过期，不再出现在待办里
    expired = db.execute(
        """WITH upd AS (
               UPDATE spot_check_item i
                  SET state = 'expired'
                 FROM spot_check_batch b
                WHERE b.batch_id = i.batch_id AND i.job_id = %s AND i.state = 'pending'
                  AND i.check_date < %s
            RETURNING 1)
           SELECT count(*) AS n FROM upd""",
        (cfg.job_id, day), returning=True)

    rows = pool(cfg)
    fresh = [r for r in rows if not r["drawn_before"]]        # 优先没抽过的
    retry = [r for r in rows if r["drawn_before"]]            # 抽过但没人看过的，可以再抽
    ordered = fresh + retry
    if stratified:
        # 路线 A 配额制：校准池（auto_reject 30%）+ 擦线全抽 + 打架降采样 + 降序覆盖主体
        picked, info = _pick_by_quota(ordered, cfg, want)
        want_eff = len(picked)
        info["strata_size"] = info.get("class_size", {})
        info["strata_quota"] = info.get("class_picked", {})
    else:
        want = DEFAULT_WANT if want is None else want
        picked, info, want_eff = ordered[:want], {"strata_size": {}, "strata_quota": {}, "pool": len(rows)}, want

    batch = db.execute(
        """INSERT INTO spot_check_batch (company_id, job_id, config_version, check_date, drawn, want, pool_size, note)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
           ON CONFLICT (company_id, job_id, config_version, check_date)
           DO UPDATE SET drawn = EXCLUDED.drawn, want = EXCLUDED.want,
                         pool_size = EXCLUDED.pool_size, note = EXCLUDED.note
           RETURNING batch_id""",
        (cfg.company_id, cfg.job_id, cfg.config_version, day, len(picked), want_eff, len(rows),
         "" if len(rows) >= want_eff else f"池子只有 {len(rows)} 份，抽不满 {want_eff} 份"), returning=True)
    batch_id = int(batch["batch_id"])

    for r in picked:
        # 标记位随抽检一起落库：source 决定能不能进校准池，priority_class 是抽样优先级（审计用）
        db.execute(
            """INSERT INTO spot_check_item (batch_id, application_id, resume_id, job_id, config_version,
                                            check_date, score, state, source, priority_class,
                                            calibration_eligible)
               VALUES (%s, %s, %s, %s, %s, %s, %s, 'pending', %s, %s, false)
               ON CONFLICT (batch_id, application_id) DO UPDATE
                 SET source = EXCLUDED.source, priority_class = EXCLUDED.priority_class""",
            (batch_id, r["application_id"], r["resume_id"], cfg.job_id, cfg.config_version, day,
             r["gate_score"], r.get("source", ""), r.get("priority_class", "")))

    Repository(cfg.company_id).audit("spot_check_draw", "job", f"{cfg.job_id}@{cfg.config_version}",
                                     {"date": day, "drawn": len(picked), "pool": len(rows), "want": want,
                                      "expired_prev": int((expired or {}).get("n", 0))})
    return {"date": day, "batch_id": batch_id, "drawn": len(picked), "pool": len(rows), "want": want_eff,
            "strata_size": info.get("strata_size", {}), "strata_quota": info.get("strata_quota", {}),
            "expired_prev": int((expired or {}).get("n", 0)),
            "items": [{"application_id": r["application_id"], "file_name": r["file_name"],
                       "score": r["gate_score"], "source": r.get("source", ""),
                       "priority_class": r.get("priority_class", ""),
                       "calibration_eligible_candidate": bool(r.get("calibration_eligible_candidate")),
                       "stratum": strata_of(float(r["gate_score"]), float(cfg.scoring.pass_line),
                                            float(cfg.scoring.band_width))} for r in picked]}


def listing(cfg: JobConfig, *, check_date: str | None = None) -> list[dict]:
    """当日抽检清单（含复核结果）。默认取最近一批（不含过期批次）。"""
    day = check_date
    if day is None:
        row = db.fetch_one(
            """SELECT max(check_date) AS d FROM spot_check_batch
                WHERE company_id = %s AND job_id = %s AND config_version = %s""",
            (cfg.company_id, cfg.job_id, cfg.config_version))
        day = str(row["d"]) if row and row["d"] else None
    if not day:
        return []
    return db.fetch_all(
        """SELECT i.item_id, i.application_id, i.state, i.score, i.check_date,
                  r.file_name, e.verdict, e.decision, e.gate_score,
                  d.action AS hr_action, d.note AS hr_note
             FROM spot_check_item i
             JOIN resume r ON r.resume_id = i.resume_id
             LEFT JOIN evaluation e ON e.application_id = i.application_id
             LEFT JOIN review_decision d ON d.application_id = i.application_id AND d.cleared_at IS NULL
            WHERE i.job_id = %s AND i.check_date = %s AND i.state <> 'expired'
            ORDER BY i.score ASC""",
        (cfg.job_id, day))


# ---------------------------------------------------------------- 复核结论（步骤 4）与校准池准入（步骤 5）
REVIEW_VERDICTS = ("reject", "keep", "uncertain")
REVIEW_VERDICT_CN = {"reject": "确实该淘汰", "keep": "该留（系统判错了）", "uncertain": "拿不准"}
DEFAULT_SAMPLE_FILE = "out/audit_sample_v1.csv"      # 冻结的抽检样本（回灌的唯一输入源）
VERDICT_TO_ACTION = {"reject": "reject", "keep": "pass", "uncertain": "uncertain"}


def load_frozen_sample(path: str = DEFAULT_SAMPLE_FILE) -> tuple[dict[int, dict], str]:
    """读冻结的抽检样本 CSV → {application_id: 行}，并返回文件指纹（sha256 前 16 位）。

    为什么回灌必须认样本文件：抽检结论只有落在**冻结样本**上才有抽样学意义；
    事后手工挑的件进校准池 = 用非随机样本标线，是把线拉歪的经典姿势。
    """
    import csv as _csv
    import hashlib
    if not os.path.exists(path):
        raise SystemExit(f"找不到冻结样本 {path}（先跑 tools/audit_sample.py 生成并冻结）")
    blob = open(path, "rb").read()
    fp = hashlib.sha256(blob).hexdigest()[:16]
    out: dict[int, dict] = {}
    with open(path, encoding="utf-8-sig") as f:
        for r in _csv.DictReader(f):
            out[int(r["application_id"])] = r
    return out, fp


def calibration_decision(source: str, verdict: str, reviewer: str, review_ts) -> tuple[bool, str]:
    """**回灌判据的唯一入口**：只有 `auto_reject` + `reject`(确实该淘汰) + 复核人/时间非空 → 置真。

    用户口径：转人工件的复核结论一律不进校准池（它不是"系统的漏放"，而是"系统没能自动判断"，
    混进去会把线拉歪）。所以这里对非 auto_reject 的行**即使 verdict=reject 也拒绝置真**。
    """
    if source != SOURCE_AUTO_REJECT:
        if verdict == "reject":
            return False, f"非 auto_reject（source={source or '空'}）→ 复核结论不进校准池"
        return False, f"source={source or '空'} 不是校准池来源"
    if verdict != "reject":
        if verdict == "keep":
            return False, "auto_reject + keep = 确认漏放（单列 leak 信号，不进校准池）"
        return False, f"review_verdict={verdict or '空'} → 未复核/拿不准，不置真"
    if not (reviewer or "").strip():
        return False, "reviewer 为空 → 不置真"
    if not review_ts:
        return False, "review_ts 为空 → 不置真"
    return True, "auto_reject + reject + 复核人/时间齐全 → 进校准池"


def record_verdict(cfg: JobConfig, application_id: int, verdict: str, reviewer: str, *,
                   note: str = "", sample_file: str = DEFAULT_SAMPLE_FILE,
                   archive_dir: str = DEFAULT_ARCHIVE_DIR, day: str | None = None,
                   now: str | None = None) -> dict:
    """复核结论落库（三列）+ 顺带记一条 review_decision（动作映射见 VERDICT_TO_ACTION）。

    三条硬校验（步骤 4）：
      · review_verdict 必须在枚举里；
      · reviewer / review_ts 非空；
      · 样本必须来自冻结的 audit_sample_v1.csv（不是事后挑件）。
    并且**强制 calibration_eligible=false**——只有回灌脚本能置真。
    """
    if verdict not in REVIEW_VERDICTS:
        raise SystemExit(f"review_verdict 只能是 {REVIEW_VERDICTS}，收到 {verdict!r}")
    if not (reviewer or "").strip():
        raise SystemExit("reviewer 不能为空（没有署名的人工结论无法追溯）")
    sample, fp = load_frozen_sample(sample_file)
    if int(application_id) not in sample:
        raise SystemExit(f"application_id={application_id} 不在冻结样本 {sample_file}（指纹 {fp}）里 → 拒绝落库")
    row = db.fetch_one(
        """SELECT i.item_id, i.source, i.state, r.resume_id, e.decision, e.gate_score
             FROM spot_check_item i
             JOIN application a ON a.application_id = i.application_id
             LEFT JOIN resume r ON r.resume_id = a.resume_id
             LEFT JOIN evaluation e ON e.application_id = a.application_id
            WHERE i.application_id = %s AND i.job_id = %s AND i.state <> 'expired'
            ORDER BY i.item_id DESC LIMIT 1""", (application_id, cfg.job_id))
    if not row:
        raise SystemExit(f"application_id={application_id} 不在 {cfg.job_id} 的抽检清单里")
    ts = now or datetime.now(CN_TZ).isoformat(timespec="seconds")
    state = {"reject": "rejected", "keep": "passed", "uncertain": "pending"}[verdict]
    db.execute(
        """UPDATE spot_check_item
              SET review_verdict = %s, reviewer = %s, review_ts = %s, state = %s,
                  calibration_eligible = false, reviewed_at = now()
            WHERE application_id = %s AND job_id = %s""",
        (verdict, reviewer, ts, state, application_id, cfg.job_id))
    archived = ""
    if verdict in ("reject", "keep"):
        archived = archive_resume(cfg, application_id, archive_dir=archive_dir, day=day,
                                  top=ARCHIVE_REJECT_TOP if verdict == "reject" else ARCHIVE_TOP)
    db.execute(
        """INSERT INTO review_decision (company_id, application_id, resume_id, job_id, config_version,
                                        action, source, note, actor, archived_path, created_at)
           VALUES (%s, %s, %s, %s, %s, %s, 'spot_check', %s, %s, %s, now())""",
        (cfg.company_id, application_id, row["resume_id"], cfg.job_id, cfg.config_version,
         VERDICT_TO_ACTION[verdict], note, reviewer, archived))
    Repository(cfg.company_id).audit("review_verdict", "application", str(application_id),
                                     {"verdict": verdict, "reviewer": reviewer, "note": note,
                                      "sample_file": os.path.basename(sample_file), "sample_fp": fp,
                                      "system_decision": row["decision"],
                                      "calibration_eligible": False})
    return {"application_id": application_id, "verdict": verdict, "reviewer": reviewer, "review_ts": ts,
            "source": row["source"] or "", "system_decision": row["decision"], "archived": archived,
            "sample_fingerprint": fp,
            # 漏放信号：系统自动淘汰、人工判该留（按用户口径不进校准池，但要能被统计看见）
            "leak_signal": (row["source"] == SOURCE_AUTO_REJECT and verdict == "keep"),
            "calibration_eligible": False}


def archive_resume(cfg: JobConfig, application_id: int, *, archive_dir: str = DEFAULT_ARCHIVE_DIR,
                   day: str | None = None, top: str = ARCHIVE_TOP) -> str:
    """归档原件：`<archive_dir>/<顶层目录>/<YYYY-MM-DD>/<原文件名>`。

    通过件进 `简历/`（HR 拿去面试），淘汰件进 `淘汰/`（要留证：抽检复看、申诉、复现都靠它）。
    同一天的多份放同一文件夹——按天分文件夹是为了"这一批是几号筛的"一眼可见。
    """
    meta = db.fetch_one(
        """SELECT r.file_name, r.file_path, r.resume_id FROM application a
             JOIN resume r ON r.resume_id = a.resume_id WHERE a.application_id = %s""",
        (application_id,))
    if not meta:
        raise SystemExit(f"找不到 application_id={application_id}")
    src = meta.get("file_path") or ""
    if not src or not os.path.exists(src):
        return ""                                     # 文件不在了：不阻塞复核，但归档路径留空（报告里能看到）
    day = day or today_cn()
    dest_dir = os.path.join(archive_dir, top, day)
    os.makedirs(dest_dir, exist_ok=True)
    dest = os.path.join(dest_dir, meta["file_name"])
    shutil.copy2(src, dest)
    return os.path.abspath(dest)


def review(cfg: JobConfig, application_id: int, action: str, *, note: str = "", source: str = "manual",
           actor: str = "hr", archive_dir: str = DEFAULT_ARCHIVE_DIR, day: str | None = None) -> dict:
    """HR 的逐份动作：pass（该留）/ reject（该淘汰）。"""
    if action not in ("pass", "reject"):
        raise SystemExit("action 只能是 pass / reject")
    row = db.fetch_one(
        """SELECT a.application_id, a.resume_id, e.decision, e.gate_score FROM application a
             LEFT JOIN evaluation e ON e.application_id = a.application_id
            WHERE a.application_id = %s""", (application_id,))
    if not row:
        raise SystemExit(f"找不到 application_id={application_id}")

    archived = archive_resume(cfg, application_id, archive_dir=archive_dir, day=day,
                              top=ARCHIVE_TOP if action == "pass" else ARCHIVE_REJECT_TOP)
    db.execute(
        """INSERT INTO review_decision (company_id, application_id, resume_id, job_id, config_version,
                                        action, source, note, actor, archived_path, created_at)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())""",
        (cfg.company_id, application_id, row["resume_id"], cfg.job_id, cfg.config_version,
         action, source, note, actor, archived))
    db.execute(
        """UPDATE spot_check_item SET state = %s, reviewed_at = now()
            WHERE application_id = %s AND job_id = %s AND state = 'pending'""",
        ("passed" if action == "pass" else "rejected", application_id, cfg.job_id))
    Repository(cfg.company_id).audit("review", "application", str(application_id),
                                     {"action": action, "source": source, "note": note,
                                      "archived": archived, "system_decision": row["decision"]})
    # 系统判自动淘汰、HR 判该留 = 一条误差（漏放），当日累计 ≥2 就该触发校准
    c = stats(cfg)
    return {"application_id": application_id, "action": action, "archived": archived,
            "system_decision": row["decision"], "error": row["decision"] == "auto_reject" and action == "pass",
            "today_errors": c["today_errors"], "calibration_due": c["today_errors"] >= 2}


def clear_manual_reject(cfg: JobConfig, application_id: int, *, actor: str = "hr") -> int:
    """人工淘汰库手动清理（软删除：清掉标记位，记录保留可追溯）。"""
    row = db.execute(
        """WITH upd AS (
               UPDATE review_decision SET cleared_at = now()
                WHERE company_id = %s AND job_id = %s AND application_id = %s
                  AND action = 'reject' AND cleared_at IS NULL
            RETURNING 1)
           SELECT count(*) AS n FROM upd""",
        (cfg.company_id, cfg.job_id, application_id), returning=True)
    n = int((row or {}).get("n", 0))
    Repository(cfg.company_id).audit("manual_reject_clear", "application", str(application_id), {"n": n})
    return n


# ---------------------------------------------------------------- 统计
def stats(cfg: JobConfig, *, day: str | None = None) -> dict:
    """池子/清单/复核/误差计数。`today_errors` = 当日抽检里「系统判淘汰但 HR 判该留」的份数。"""
    day = day or today_cn()
    rows = db.fetch_all(
        """SELECT i.state, i.score, d.action AS hr_action, i.score AS s
             FROM spot_check_item i
             LEFT JOIN review_decision d ON d.application_id = i.application_id AND d.cleared_at IS NULL
            WHERE i.job_id = %s AND i.check_date = %s AND i.state <> 'expired'""",
        (cfg.job_id, day))
    errors = sum(1 for r in rows if r["hr_action"] == "pass")
    decided = sum(1 for r in rows if r["hr_action"])
    # 累计抽检：多批累积才能证明"漏放 ≤1%"（单批 30 份的上界约 9.5%）
    cum = db.fetch_one(
        """SELECT count(*) AS checked,
                  sum(CASE WHEN d.action = 'pass' THEN 1 ELSE 0 END) AS leaks
             FROM spot_check_item i
             LEFT JOIN review_decision d ON d.application_id = i.application_id AND d.cleared_at IS NULL
            WHERE i.job_id = %s AND i.state IN ('passed', 'rejected')""",
        (cfg.job_id,))
    checked, leaks = int((cum or {}).get("checked") or 0), int((cum or {}).get("leaks") or 0)
    return {
        "date": day,
        "pool_size": len(pool(cfg)),
        "drawn_today": len(rows),
        "reviewed_today": decided,
        "today_errors": errors,
        "manual_reject_library": len(manual_reject_library(cfg)),
        "passed_total": db.fetch_one(
            "SELECT count(*) AS n FROM review_decision WHERE company_id=%s AND job_id=%s AND action='pass' AND cleared_at IS NULL",
            (cfg.company_id, cfg.job_id))["n"],
        "labels_total": db.fetch_one(
            "SELECT count(*) AS n FROM review_decision WHERE company_id=%s AND job_id=%s AND cleared_at IS NULL",
            (cfg.company_id, cfg.job_id))["n"],
        "spotcheck_cumulative": {
            "checked": checked, "leaks": leaks,
            "leak_rate": round(leaks / checked, 4) if checked else None,
            "leak_upper_bound_95": round(leak_upper_bound(leaks, checked), 4) if checked else None,
            "samples_to_prove_1pct": samples_needed(0.01),
        },
    }


# ---------------------------------------------------------------- CLI
def main() -> int:
    ap = argparse.ArgumentParser(description="复核 / 抽检（后端 + 命令行）")
    ap.add_argument("cmd", choices=["pool", "draw", "list", "pass", "reject", "clear", "stats", "library"])
    ap.add_argument("--job", default="config/job_game_dev.json")
    ap.add_argument("--application", type=int, default=None)
    ap.add_argument("--want", type=int, default=DEFAULT_WANT, help="当日抽检份数（默认 5）")
    ap.add_argument("--date", default=None, help="指定日期 YYYY-MM-DD（默认按北京时间算的今天）")
    ap.add_argument("--note", default="", help="复核备注（HR 的一句话理由）")
    ap.add_argument("--archive-dir", default=DEFAULT_ARCHIVE_DIR, help="通过件归档根目录")
    ap.add_argument("--flat", action="store_true", help="退回固定份数口径（默认是分层抽检）")
    ap.add_argument("--json", dest="json_out", action="store_true", help="以 JSON 输出（给脚本/界面用）")
    a = ap.parse_args()

    cfg = JobConfig.model_validate(json.load(open(a.job, encoding="utf-8")))

    if a.cmd == "pool":
        rows = pool(cfg)
        print(f"自动淘汰库（池子）共 {len(rows)} 份 · 岗位 {cfg.job_id}@{cfg.config_version}")
        for r in rows:
            print(f"  app={r['application_id']:<5} {r['file_name']:<20} 分 {r['gate_score']:>5} "
                  f"{'（曾抽过没人看）' if r['drawn_before'] else ''}")
        print(f"\n（人工淘汰库 {len(manual_reject_library(cfg))} 份：`{sys.argv[0]} library` 查看）")
        return 0

    if a.cmd == "library":
        rows = manual_reject_library(cfg)
        print(f"人工淘汰库共 {len(rows)} 份（已清理的不算）")
        for r in rows:
            print(f"  app={r['application_id']:<5} {str(r['file_name']):<20} 来源 {r['source']:<10} "
                  f"{r['created_at']:%Y-%m-%d %H:%M} {r['note'][:30]}")
        return 0

    if a.cmd == "draw":
        out = draw(cfg, want=a.want or None, check_date=a.date, archive_dir=a.archive_dir,
                   stratified=not a.flat)
        mode = "分层（擦线带 50% / 中间带 10% / 远端 3%）" if not a.flat else f"固定 {a.want} 份"
        print(f"已生成 {out['date']} 的抽检清单：{mode} → 抽到 {out['drawn']} 份"
              f"（池子 {out['pool']} 份，过期旧清单 {out['expired_prev']} 条）")
        if out.get("strata_size"):
            need = " / ".join(f"{k} {out['strata_size'][k]} 份"
                              for k in ("near", "mid", "far") if out["strata_size"].get(k))
            print(f"  池子分层：{need}")
            print(f"  本批配额：{out['strata_quota']}")
        for it in out["items"]:
            tag = {"near": "★擦线", "mid": "中间", "far": "远端"}.get(it.get("stratum", ""), "")
            print(f"  {tag:<4} app={it['application_id']:<5} {it['file_name']:<20} 分 {it['score']}")
        if out["drawn"] < out["want"]:
            print(f"⚠️  池子不足：自动淘汰件只有 {out['pool']} 份（池子大小由达标线决定，见 tools/line_sweep.py）")
        return 0

    if a.cmd == "list":
        rows = listing(cfg, check_date=a.date)
        if a.json_out:
            print(json.dumps(rows, ensure_ascii=False, default=str))
            return 0
        if not rows:
            print("当日没有抽检清单（先跑 draw）")
            return 0
        print(f"抽检清单（{rows[0]['check_date']}）：")
        for r in rows:
            mark = {"pending": "待看", "passed": "已判通过", "rejected": "已判淘汰"}.get(r["state"], r["state"])
            print(f"  app={r['application_id']:<5} {r['file_name']:<20} 分 {r['gate_score']:>5} "
                  f"系统判 {r['decision']:<12} → {mark} {r['hr_note'] or ''}")
        return 0

    if a.cmd in ("pass", "reject"):
        if not a.application:
            print("需要 --application <application_id>")
            return 2
        res = review(cfg, a.application, "pass" if a.cmd == "pass" else "reject",
                     note=a.note, source="spot_check" if a.date else "manual", archive_dir=a.archive_dir)
        tag = "通过（该留）" if res["action"] == "pass" else "淘汰"
        print(f"app={res['application_id']} 已记 {tag}；系统原判 {res['system_decision']}"
              + (f"；归档到 {res['archived']}" if res["archived"] else ""))
        if res["error"]:
            print(f"⚠️  这是一条误差（系统自动淘汰，但 HR 判该留）→ 今日累计 {res['today_errors']} 条")
        if res["calibration_due"]:
            print("⚠️  今日误差已达 2 条 → 可以跑校准：python -m app.calibrate run --job "
                  f"{a.job} --trigger spot_check_error")
        return 0

    if a.cmd == "clear":
        if not a.application:
            print("需要 --application <application_id>")
            return 2
        n = clear_manual_reject(cfg, a.application)
        print(f"已从人工淘汰库清理 {n} 条（软删除，review_decision 里仍可追溯）")
        return 0

    if a.cmd == "stats":
        s = stats(cfg, day=a.date)
        print(json.dumps(s, ensure_ascii=False, indent=2))
        cum = s["spotcheck_cumulative"]
        print(f"累计抽检：{cum['checked']} 份 · 发现漏放 {cum['leaks']} 个 · "
              f"漏放率 {cum['leak_rate'] if cum['leak_rate'] is not None else '—'} · "
              f"95% 置信上界 {cum['leak_upper_bound_95'] if cum['leak_upper_bound_95'] is not None else '—'}"
              f"（证明 1% 需累计约 {cum['samples_to_prove_1pct']} 份）")
        if s["today_errors"] >= 2:
            print("→ 今日误差 ≥2，可触发自动校准")
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
