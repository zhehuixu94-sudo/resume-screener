#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""口径自动校准：把 HR 的「通过 / 淘汰」标注换成新的达标线（全自动生效、可一键回滚）。

触发条件（只有这两种，别自己加）：
  1. 用户主动：`python -m app.calibrate run --job ... --trigger manual`
  2. 当日抽检误差 ≥2 条（系统判自动淘汰、HR 却判该留）：`--trigger spot_check_error`

校准做法：把 HR 的标注当正/负样本，用**落库的模型输出离线重放**（0 token）扫描候选达标线，
每个候选线算三个数：漏放（正样本被自动淘汰）/ 误放（负样本被自动达标）/ 人工负担（转人工份数），
然后按 **漏放优先、其次误放、最后少给人添活** 的字典序挑一条线（安全优先，宁可多转人工）。

两条硬约束（防抖）：
  · 正负样本各 ≥3 条才动线；
  · 两组分数必须分得开（max(不合格) < min(合格)）；分不开说明要么锚点标错了、要么维度区分不出来，
    这时**只记录不动线**，把原因写进 calibration 表。

生效方式：config_version +1、就地更新配置文件（旧版本备份到 `config/history/`），
calibration 表存改前改后的完整配置快照 → `rollback` 就能一键回到上一版。
"""
from __future__ import annotations

import argparse
import json
import os
import statistics as st
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from .contracts import JobConfig                              # noqa: E402
from .pipeline.replay import decide_with_line, load_rows      # noqa: E402
from .pipeline.stability import is_ok as stability_is_ok       # noqa: E402
from .pipeline.stats import choose_line_within_budget          # noqa: E402
from .pipeline.stability import latest as stability_latest     # noqa: E402
from .store import Repository, db                             # noqa: E402

MIN_SAMPLES = 3              # 每组最少几条标注才动线
LINE_STEP = 1.0              # 候选线扫描步长
LINE_SPAN = 30.0             # 从样本分数范围向两侧各扩多少
HISTORY_DIR = "config/history"


# ---------------------------------------------------------------- 样本
def labels(cfg: JobConfig) -> tuple[list[dict], list[dict]]:
    """HR 的标注拆成正/负样本（pass=该留，reject=该淘汰），已清理的不算。"""
    rows = db.fetch_all(
        """SELECT d.application_id, d.resume_id, d.action, d.source, d.note, d.created_at
             FROM review_decision d
            WHERE d.company_id = %s AND d.job_id = %s AND d.cleared_at IS NULL
            ORDER BY d.created_at""",
        (cfg.company_id, cfg.job_id))
    pos = [r for r in rows if r["action"] == "pass"]
    neg = [r for r in rows if r["action"] == "reject"]
    return pos, neg


def choose_line(counts: list[dict]) -> dict | None:
    """按「漏放优先 → 误放 → 人工负担 → 并列取更高线」挑线（纯函数，便于测试）。

    最后那一项是安全方向：三项相同时，**取更高的线**（少放人进门，多给 HR 留一点活），
    反过来取最低线会一路滑到"人人自动达标"，那是另一种事故。
    counts 每项：{"line": float, "leak": int, "wrong": int, "manual": int}
    """
    if not counts:
        return None
    return min(counts, key=lambda c: (c["leak"], c["wrong"], c["manual"], -c["line"]))


def sweep(cfg: JobConfig, rows_by_resume: dict[str, dict], pos_ids: set[str], neg_ids: set[str],
          band: float) -> dict:
    """扫描候选线，统计每条的漏放/误放/人工负担。返回 {candidates, samples, separated}。"""
    samples = []
    for rid, row in rows_by_resume.items():
        if rid in pos_ids:
            samples.append((row, "pos"))
        elif rid in neg_ids:
            samples.append((row, "neg"))
    if not samples:
        return {"candidates": [], "samples": [], "separated": False,
                "pos_scores": [], "neg_scores": []}

    scores = [float(r["old"].base_total) for r, _ in samples]
    lo = max(0.0, min(scores) - LINE_SPAN)          # 线不能是负数（挑出过 -5.8 这种没意义的线）
    hi = max(scores) + LINE_SPAN
    pos_scores = sorted(float(r["old"].base_total) for r, lab in samples if lab == "pos")
    neg_scores = sorted(float(r["old"].base_total) for r, lab in samples if lab == "neg")
    separated = bool(pos_scores and neg_scores and max(neg_scores) < min(pos_scores))

    counts: list[dict] = []
    line = lo
    while line <= hi + 1e-9:
        leak = wrong = manual = 0
        for row, lab in samples:
            # gate=False：校准要找的是"判定机制本身"会不会漏放；稳定性闸门是另一层纪律，
            # 它由下面的「生效前提」把关，不能混进扫描（否则漏放恒为 0，校准失去目标）。
            d = decide_with_line(cfg, row, line, band, gate=False)
            if lab == "pos" and d == "auto_reject":
                leak += 1
            elif lab == "neg" and d == "auto_pass":
                wrong += 1
            if d == "manual_review":
                manual += 1
        counts.append({"line": round(line, 1), "leak": leak, "wrong": wrong, "manual": manual})
        line += LINE_STEP
    return {"candidates": counts, "samples": samples, "separated": separated,
            "pos_scores": sorted(pos_scores), "neg_scores": sorted(neg_scores)}


# ---------------------------------------------------------------- 配置版本
def _version_plus1(v: str) -> str:
    try:
        return str(int(str(v)) + 1)
    except ValueError:
        return f"{v}-cal1"


def config_patch(path: str, *, pass_line: float, config_version: str, source: str) -> dict:
    """就地更新配置文件（只动必要字段，其它内容原样保留）并备份旧版本。

    备份目录是**配置文件旁边的 `history/`**（不是 cwd 下的 config/history）——按 cwd 存会在
    不同工作目录下把备份散得到处都是。
    """
    data = json.load(open(path, encoding="utf-8"))
    history_dir = os.path.join(os.path.dirname(os.path.abspath(path)), "history")
    os.makedirs(history_dir, exist_ok=True)
    backup = os.path.join(history_dir, f"{os.path.basename(path)}.v{data.get('config_version')}.json")
    if not os.path.exists(backup):
        with open(backup, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    before = json.dumps(data, ensure_ascii=False)
    data.setdefault("scoring", {})
    data["scoring"]["pass_line"] = round(float(pass_line), 2)
    data["scoring"]["pass_line_source"] = source
    data["config_version"] = config_version
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    return {"before": json.loads(before), "after": data, "backup": backup}


# ---------------------------------------------------------------- 校准
def run(job_path: str, *, trigger: str = "manual", band: float | None = None,
        dry_run: bool = False, leak_budget: float = 0.01) -> dict:
    cfg = JobConfig.model_validate(json.load(open(job_path, encoding="utf-8")))
    repo = Repository(cfg.company_id)
    pos, neg = labels(cfg)
    band = band if band is not None else cfg.scoring.band_width
    reason = ""
    status = "skipped"

    rows_by_resume = {r["resume_id"]: r for r in load_rows(cfg)}
    labeled_ids = {p["resume_id"] for p in pos} | {n["resume_id"] for n in neg}
    # 有标注但重放时看不到的（缺解析结果/无评估行）——必须报出来，不能静默少算样本
    invisible = sorted(labeled_ids - set(rows_by_resume))
    sw = {"candidates": [], "samples": [], "separated": False}

    stable_ok, stable_why = stability_is_ok(stability_latest(cfg.job_id, cfg.config_version))
    if len(pos) < MIN_SAMPLES or len(neg) < MIN_SAMPLES:
        reason = f"样本不足：该留 {len(pos)} 条 / 该淘汰 {len(neg)} 条，各需 ≥{MIN_SAMPLES} 条才动线"
    elif not stable_ok:
        reason = f"稳定性未达标，不动线：{stable_why}"
    else:
        sw = sweep(cfg, rows_by_resume, {p["resume_id"] for p in pos}, {n["resume_id"] for n in neg}, band)
        if not sw["separated"]:
            reason = (f"两组分数分不开：该淘汰最高 {max(sw['neg_scores']):.1f} ≥ "
                      f"该留最低 {min(sw['pos_scores']):.1f} —— 不动线，先查锚点/维度设计")
        else:
            best, why = choose_line_within_budget(sw["candidates"], len(pos), budget=leak_budget)
            if best is None:
                reason = why
            else:
                status = "applied"
                reason = (f"漏放 {best['leak']} · 误放 {best['wrong']} · 转人工 {best['manual']}"
                          f"（{why}；含 {len(pos)} 该留 / {len(neg)} 该淘汰）")

    samples_meta = {
        "pos": [{"resume_id": p["resume_id"], "score": _score_of(rows_by_resume, p["resume_id"])} for p in pos],
        "neg": [{"resume_id": n["resume_id"], "score": _score_of(rows_by_resume, n["resume_id"])} for n in neg],
        "band": band,
        "separated": sw.get("separated"),
        "skipped_no_result": invisible,
    }
    if invisible:
        reason += f"；（{len(invisible)} 条标注在重放时取不到评估/解析结果，已跳过）"

    if status != "applied":
        if not dry_run:
            db.execute(
                """INSERT INTO calibration (company_id, job_id, trigger, from_version, to_version, status,
                                            reason, samples, changed, before_payload, after_payload)
                   VALUES (%s, %s, %s, %s, %s, 'skipped', %s, %s, '{}'::jsonb, %s, %s)""",
                (cfg.company_id, cfg.job_id, trigger, cfg.config_version, cfg.config_version, reason,
                 json.dumps(samples_meta, ensure_ascii=False),
                 json.dumps(cfg.model_dump(mode="json"), ensure_ascii=False),
                 json.dumps(cfg.model_dump(mode="json"), ensure_ascii=False)))
            repo.audit("calibrate_skipped", "job", f"{cfg.job_id}@{cfg.config_version}",
                       {"reason": reason, "trigger": trigger})
        return {"status": status, "reason": reason, "before_line": cfg.scoring.pass_line,
                "after_line": cfg.scoring.pass_line, "samples": samples_meta,
                "candidates": len(sw.get("candidates", []))}

    best = choose_line(sw["candidates"])
    new_version = _version_plus1(cfg.config_version)
    detail = (f"自动校准 {datetime.now().strftime('%Y-%m-%d %H:%M')}："
              f"{len(pos)} 该留 / {len(neg)} 该淘汰，漏放 {best['leak']} 误放 {best['wrong']}")
    if dry_run:
        return {"status": "dry_run", "reason": reason, "before_line": cfg.scoring.pass_line,
                "after_line": best["line"], "samples": samples_meta, "new_version": new_version}

    # pass_line_source 是契约里的枚举（manual_default / anchor_calibrated）：
    # 人工标注标定出来的线属于 anchor_calibrated；可读的细节记在 calibration.reason 与审计里。
    patch = config_patch(job_path, pass_line=best["line"], config_version=new_version,
                         source="anchor_calibrated")
    new_cfg = JobConfig.model_validate(patch["after"])
    repo.upsert_job_config(new_cfg)
    db.execute(
        """INSERT INTO calibration (company_id, job_id, trigger, from_version, to_version, status,
                                    reason, samples, changed, before_payload, after_payload)
           VALUES (%s, %s, %s, %s, %s, 'applied', %s, %s, %s, %s, %s)""",
        (cfg.company_id, cfg.job_id, trigger, cfg.config_version, new_version, reason,
         json.dumps(samples_meta, ensure_ascii=False),
         json.dumps({"pass_line": {"before": cfg.scoring.pass_line, "after": best["line"]},
                     "band_width": band}, ensure_ascii=False),
         json.dumps(patch["before"], ensure_ascii=False), json.dumps(patch["after"], ensure_ascii=False)))
    repo.audit("calibrate_applied", "job", f"{cfg.job_id}@{new_version}",
               {"from": cfg.config_version, "pass_line": best["line"], "trigger": trigger,
                "leak": best["leak"], "wrong": best["wrong"], "manual": best["manual"],
                "detail": detail})
    return {"status": "applied", "reason": reason, "before_line": cfg.scoring.pass_line,
            "after_line": best["line"], "new_version": new_version, "backup": patch["backup"],
            "samples": samples_meta, "candidates": len(sw["candidates"])}


def _score_of(rows_by_resume: dict[str, dict], resume_id: str) -> float | None:
    row = rows_by_resume.get(resume_id)
    return float(row["old"].base_total) if row else None


def rollback(job_path: str) -> dict:
    """回滚到最近一次已生效的校准之前（用 calibration 里存的 before_payload 覆盖配置文件）。"""
    cfg = JobConfig.model_validate(json.load(open(job_path, encoding="utf-8")))
    row = db.fetch_one(
        """SELECT calibration_id, from_version, to_version, before_payload, created_at
             FROM calibration
            WHERE company_id = %s AND job_id = %s AND status = 'applied' AND reverted_at IS NULL
            ORDER BY created_at DESC LIMIT 1""",
        (cfg.company_id, cfg.job_id))
    if not row:
        return {"status": "nothing_to_rollback"}
    before = row["before_payload"]
    with open(job_path, "w", encoding="utf-8") as f:
        json.dump(before, f, ensure_ascii=False, indent=2)
        f.write("\n")
    restored = JobConfig.model_validate(before)
    Repository(cfg.company_id).upsert_job_config(restored)
    db.execute("UPDATE calibration SET reverted_at = now() WHERE calibration_id = %s",
               (row["calibration_id"],))
    Repository(cfg.company_id).audit("calibrate_rollback", "job", f"{cfg.job_id}@{restored.config_version}",
                                     {"calibration_id": row["calibration_id"],
                                      "reverted_to": row["from_version"],
                                      "undone_version": row["to_version"]})
    return {"status": "reverted", "calibration_id": row["calibration_id"],
            "back_to": row["from_version"], "undone": row["to_version"],
            "pass_line": restored.scoring.pass_line}


def status(job_path: str) -> list[dict]:
    cfg = JobConfig.model_validate(json.load(open(job_path, encoding="utf-8")))
    return db.fetch_all(
        """SELECT calibration_id, trigger, from_version, to_version, status, reason, changed,
                  reverted_at, created_at
             FROM calibration WHERE company_id = %s AND job_id = %s
            ORDER BY created_at DESC LIMIT 20""",
        (cfg.company_id, cfg.job_id))


# ---------------------------------------------------------------- CLI
def main() -> int:
    ap = argparse.ArgumentParser(description="口径自动校准（可回滚）")
    ap.add_argument("cmd", choices=["run", "status", "rollback"])
    ap.add_argument("--job", default="config/job_game_dev.json")
    ap.add_argument("--trigger", default="manual", choices=["manual", "spot_check_error"])
    ap.add_argument("--band", type=float, default=None)
    ap.add_argument("--dry-run", action="store_true", help="只算不改（看会把线改成多少）")
    ap.add_argument("--leak-budget", type=float, default=0.01,
                    help="漏放率预算（默认 1%%）：候选线必须能**在统计上证明**漏放不超过它，"
                         "证明不了就不动线并告诉你还缺多少样本")
    a = ap.parse_args()

    if a.cmd == "run":
        r = run(a.job, trigger=a.trigger, band=a.band, dry_run=a.dry_run,
                leak_budget=a.leak_budget)
        print(f"[{r['status']}] {r['reason']}")
        print(f"达标线：{r['before_line']} → {r['after_line']}"
              + (f"（新版本 v{r['new_version']}，旧配置已备份）" if r.get("new_version") else ""))
        if r["samples"]:
            print(f"样本：该留 {len(r['samples']['pos'])} 条 {[s['score'] for s in r['samples']['pos']]} · "
                  f"该淘汰 {len(r['samples']['neg'])} 条 {[s['score'] for s in r['samples']['neg']]} · "
                  f"两组分得开：{r['samples']['separated']}")
        if r["status"] == "applied":
            print("（全自动生效：下次跑批/重放就用新线；不满意可 `python -m app.calibrate rollback`）")
        return 0

    if a.cmd == "status":
        rows = status(a.job)
        if not rows:
            print("还没有校准记录（先积累人工标注：抽检 + 逐份复核）")
            return 0
        for r in rows:
            rev = f"（已于 {r['reverted_at']:%Y-%m-%d %H:%M} 回滚）" if r["reverted_at"] else ""
            print(f"#{r['calibration_id']} {r['created_at']:%Y-%m-%d %H:%M} [{r['status']}] "
                  f"v{r['from_version']}→v{r['to_version']} 触发={r['trigger']} {rev}")
            print(f"    {r['reason']}")
        return 0

    if a.cmd == "rollback":
        r = rollback(a.job)
        if r["status"] != "reverted":
            print("没有可回滚的校准")
            return 0
        print(f"已回滚：撤销 v{r['undone']} 的校准，配置回到 v{r['back_to']}（达标线 {r['pass_line']}）")
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
