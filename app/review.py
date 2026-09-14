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
from .store import Repository, db                # noqa: E402

DEFAULT_ARCHIVE_DIR = os.environ.get("RESUME_ARCHIVE_DIR") or "out/passed"
CN_TZ = ZoneInfo("Asia/Shanghai")          # 「新的一天」按北京时间过 0 点算（不跟机器时区走）


def today_cn() -> str:
    """当天的日期（北京时间）。抽检批次、归档目录都用它，避免机器时区/UTC 导致的跨天错位。"""
    return datetime.now(CN_TZ).date().isoformat()
ARCHIVE_TOP = "简历"                              # 通过件归档顶层目录名（子目录按天）
ARCHIVE_REJECT_TOP = "淘汰"                        # 淘汰件归档顶层目录名（要留证：复看/申诉/复现）
DEFAULT_WANT = 5                                  # 每天抽几份


# ---------------------------------------------------------------- 池子
POOL_SQL = """
SELECT a.application_id, a.resume_id, r.file_name, r.file_path, e.base_total, e.verdict, e.decision,
       EXISTS (SELECT 1 FROM spot_check_item i
                WHERE i.application_id = a.application_id AND i.state IN ('pending', 'passed', 'rejected'))
         AS drawn_before,
       EXISTS (SELECT 1 FROM review_decision d
                WHERE d.application_id = a.application_id AND d.cleared_at IS NULL) AS reviewed
  FROM application a
  JOIN evaluation e ON e.application_id = a.application_id
  JOIN resume r ON r.resume_id = a.resume_id
 WHERE a.company_id = %s AND a.job_id = %s AND a.config_version = %s
   AND e.decision = 'auto_reject'
 ORDER BY e.base_total ASC, random()
"""


def pool(cfg: JobConfig) -> list[dict]:
    """自动淘汰库现状：所有自动淘汰、且还没有人工结论的件（已复核的不再出现在池子里）。"""
    rows = db.fetch_all(POOL_SQL, (cfg.company_id, cfg.job_id, cfg.config_version))
    return [r for r in rows if not r["reviewed"]]


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
def draw(cfg: JobConfig, *, want: int = DEFAULT_WANT, check_date: str | None = None,
         archive_dir: str = DEFAULT_ARCHIVE_DIR) -> dict:
    """生成本日抽检清单。旧批次的未复核项先过期（不累加），再抽 want 份。"""
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
    picked = (fresh + retry)[:want]

    batch = db.execute(
        """INSERT INTO spot_check_batch (company_id, job_id, config_version, check_date, drawn, want, pool_size, note)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
           ON CONFLICT (company_id, job_id, config_version, check_date)
           DO UPDATE SET drawn = EXCLUDED.drawn, want = EXCLUDED.want,
                         pool_size = EXCLUDED.pool_size, note = EXCLUDED.note
           RETURNING batch_id""",
        (cfg.company_id, cfg.job_id, cfg.config_version, day, len(picked), want, len(rows),
         "" if len(rows) >= want else f"池子只有 {len(rows)} 份，抽不满 {want} 份"), returning=True)
    batch_id = int(batch["batch_id"])

    for r in picked:
        db.execute(
            """INSERT INTO spot_check_item (batch_id, application_id, resume_id, job_id, config_version,
                                            check_date, score, state)
               VALUES (%s, %s, %s, %s, %s, %s, %s, 'pending')
               ON CONFLICT (batch_id, application_id) DO NOTHING""",
            (batch_id, r["application_id"], r["resume_id"], cfg.job_id, cfg.config_version, day,
             r["base_total"]))

    Repository(cfg.company_id).audit("spot_check_draw", "job", f"{cfg.job_id}@{cfg.config_version}",
                                     {"date": day, "drawn": len(picked), "pool": len(rows), "want": want,
                                      "expired_prev": int((expired or {}).get("n", 0))})
    return {"date": day, "batch_id": batch_id, "drawn": len(picked), "pool": len(rows), "want": want,
            "expired_prev": int((expired or {}).get("n", 0)),
            "items": [{"application_id": r["application_id"], "file_name": r["file_name"],
                       "score": r["base_total"]} for r in picked]}


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
                  r.file_name, e.verdict, e.decision, e.base_total,
                  d.action AS hr_action, d.note AS hr_note
             FROM spot_check_item i
             JOIN resume r ON r.resume_id = i.resume_id
             LEFT JOIN evaluation e ON e.application_id = i.application_id
             LEFT JOIN review_decision d ON d.application_id = i.application_id AND d.cleared_at IS NULL
            WHERE i.job_id = %s AND i.check_date = %s AND i.state <> 'expired'
            ORDER BY i.score ASC""",
        (cfg.job_id, day))


# ---------------------------------------------------------------- 复核动作
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
        """SELECT a.application_id, a.resume_id, e.decision, e.base_total FROM application a
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
    ap.add_argument("--json", dest="json_out", action="store_true", help="以 JSON 输出（给脚本/界面用）")
    a = ap.parse_args()

    cfg = JobConfig.model_validate(json.load(open(a.job, encoding="utf-8")))

    if a.cmd == "pool":
        rows = pool(cfg)
        print(f"自动淘汰库（池子）共 {len(rows)} 份 · 岗位 {cfg.job_id}@{cfg.config_version}")
        for r in rows:
            print(f"  app={r['application_id']:<5} {r['file_name']:<20} 分 {r['base_total']:>5} "
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
        out = draw(cfg, want=a.want, check_date=a.date, archive_dir=a.archive_dir)
        print(f"已生成 {out['date']} 的抽检清单：抽到 {out['drawn']} 份 / 目标 {out['want']} 份 "
              f"（池子 {out['pool']} 份，过期旧清单 {out['expired_prev']} 条）")
        for it in out["items"]:
            print(f"  app={it['application_id']:<5} {it['file_name']:<20} 分 {it['score']}")
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
            print(f"  app={r['application_id']:<5} {r['file_name']:<20} 分 {r['base_total']:>5} "
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
        if s["today_errors"] >= 2:
            print("→ 今日误差 ≥2，可触发自动校准")
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
