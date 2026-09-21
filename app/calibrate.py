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

生效方式：**只动线、不动配置世代** —— `scoring.calibration_version +1` + 就地更新配置文件
（旧版本备份到配置文件旁的 `history/`，名字带 `v<配置世代>-cv<线版本>`），`config_version` 保持不变
（它是 application/evaluation 的键值，`batch status` 等按当前版本过滤，动它会让整批结果从操作视图消失）。
calibration 表存改前改后的完整配置快照 → `rollback` 就能一键回到上一版。

**动线必留快照（纪律机器化）**：apply 在写完 pass_line + config_version+1 后，**内部**调用
`tools/freeze_config_snapshot.freeze()` 冻结配置快照（路径按命名规则自动生成：
`config/snapshots/<job>_v<配置世代>-cv<新线版本>-<scoring_semantics>.json`（cv=0 时不写 cv 段））。冻结失败 → **整个 apply 回滚**
（配置文件逐字还原、库里 job_config 还原、删掉半写快照、抹掉 applied 行、记一条 status='failed'），
不允许出现"线动了快照没动"。dry-run 只打印"若 apply 将冻结到 <path>"，不写快照也不写库。
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
from .review import SOURCE_AUTO_REJECT, calibration_decision   # noqa: E402
from .store import Repository, db                             # noqa: E402

# 口径变更的证据链：apply 内部**同事务**冻结快照（不新增独立逻辑，复用生成器）
from tools.freeze_config_snapshot import freeze as freeze_snapshot   # noqa: E402
from tools.freeze_config_snapshot import snapshot_path               # noqa: E402

MIN_SAMPLES = 3              # 每组最少几条标注才动线
LINE_STEP = 1.0              # 候选线扫描步长
LINE_SPAN = 30.0             # 从样本分数范围向两侧各扩多少
HISTORY_DIR = "config/history"


# ---------------------------------------------------------------- 样本（选项 A：唯一入口）
# ⚠️ 为什么不再读 `review_decision.action`（2026-09-15 定夺）：
#    review_decision 会把「转人工件的复核结论」也记成 pass/reject，而"转人工件的结论不进校准池"
#    是硬边界——入口不改，边界在 calibrate 侧就失效，等于把线拉歪的口子没堵死；
#    而且那样正样本(该留)也进不来，自动校准会永远卡在"正/负各 ≥3"，静默空转。
def calibration_pool(cfg: JobConfig) -> dict:
    """**校准样本的唯一入口**：只认 `spot_check_item` 的标记位 + 复核结论。

      · 负样本 neg ：`calibration_eligible=true` 且 `source=auto_reject` 且 `review_verdict=reject`
        （系统淘汰 + 人工确认"确实该淘汰" → 用来压误放）
      · 正样本 pos ：`source=auto_reject` 且 `review_verdict=keep`
        （**确认漏放**：系统判淘汰、人工判该留 → 线设高了的直接证据，单列 `leak_signal=True`）
      · 其余（转人工件的 reject/keep 结论）**一律丢弃并逐条记 reason**（被误读要看得见）
    """
    from collections import Counter as _Counter
    rows = db.fetch_all(
        """SELECT i.application_id, i.resume_id, i.source, i.priority_class, i.review_verdict,
                  i.reviewer, i.review_ts, i.calibration_eligible, i.score,
                  e.gate_score, r.file_name
             FROM spot_check_item i
             JOIN application a ON a.application_id = i.application_id
             LEFT JOIN evaluation e ON e.application_id = i.application_id
             LEFT JOIN resume r ON r.resume_id = i.resume_id
            WHERE a.company_id = %s AND i.job_id = %s AND i.state <> 'expired'
            ORDER BY i.item_id""", (cfg.company_id, cfg.job_id))
    pos: list[dict] = []
    neg: list[dict] = []
    dropped: list[dict] = []
    neg_bad: list[dict] = []          # calibration_eligible=true 但 source≠auto_reject（必须为 0）
    manual_with_verdict = 0
    for r in rows:
        src, verdict = (r["source"] or ""), (r["review_verdict"] or "")
        row = dict(r)
        row["gate_score"] = r["gate_score"] if r["gate_score"] is not None else r["score"]
        if r["calibration_eligible"] and src != SOURCE_AUTO_REJECT:
            neg_bad.append(row)
        if src == SOURCE_AUTO_REJECT and verdict == "keep":
            pos.append({**row, "leak_signal": True})
            continue
        if src == SOURCE_AUTO_REJECT and verdict == "reject":
            ok, why = calibration_decision(src, verdict, r["reviewer"] or "", r["review_ts"])
            if ok and r["calibration_eligible"]:
                neg.append({**row, "leak_signal": False})
            else:
                dropped.append({**row, "drop_reason": f"auto_reject+reject 但未通过回灌判据：{why}"})
            continue
        if verdict:
            manual_with_verdict += 1
            dropped.append({**row, "drop_reason": f"source={src or '空'}（人工复核件）→ 复核结论不进 pos/neg"})
        else:
            dropped.append({**row, "drop_reason": "未复核（无 review_verdict）"})
    reasons = _Counter(d["drop_reason"] for d in dropped)
    return {
        "pos": pos, "neg": neg, "dropped": dropped, "reasons": reasons, "rows": rows,
        "counts": {
            "pos": len(pos), "neg": len(neg), "dropped": len(dropped),
            "eligible": sum(1 for r in rows if r["calibration_eligible"]),
            "pos_leak_signal": sum(1 for p in pos if p["leak_signal"]),
            "neg_eligible_non_auto_reject": len(neg_bad),
            "manual_review_verdicts_dropped": manual_with_verdict,
            "reviewed_rows": sum(1 for r in rows if (r["review_verdict"] or "")),
        },
        "violations": [r["application_id"] for r in neg_bad],
    }


def labels(cfg: JobConfig) -> tuple[list[dict], list[dict]]:
    """兼容旧签名：返回 (pos, neg)。新代码请用 `calibration_pool()`（带 dropped/reason/counts）。"""
    p = calibration_pool(cfg)
    return p["pos"], p["neg"]


def sanitize_calibration_flags(cfg: JobConfig, *, apply: bool = False) -> dict:
    """自愈：把"不该为真却为真"的 `calibration_eligible` 复位为 false。

    唯一合法条件见 `review.calibration_decision()`：source=auto_reject + verdict=reject + reviewer/review_ts 非空。
    （`--apply` 时真的写库；dry-run 只报告。）
    """
    rows = db.fetch_all(
        """SELECT item_id, application_id, source, review_verdict, reviewer, review_ts
             FROM spot_check_item
            WHERE job_id = %s AND calibration_eligible""",
        (cfg.job_id,))
    bad = [r for r in rows
           if not calibration_decision(r["source"] or "", r["review_verdict"] or "",
                                       r["reviewer"] or "", r["review_ts"])[0]]
    if apply and bad:
        db.execute("UPDATE spot_check_item SET calibration_eligible = false WHERE item_id = ANY(%s)",
                   ([r["item_id"] for r in bad],))
    return {"checked": len(rows), "violations": len(bad),
            "applications": [r["application_id"] for r in bad], "applied": bool(apply and bad)}



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

    scores = [float(r["old"].gate_score) for r, _ in samples]
    lo = max(0.0, min(scores) - LINE_SPAN)          # 线不能是负数（挑出过 -5.8 这种没意义的线）
    hi = max(scores) + LINE_SPAN
    pos_scores = sorted(float(r["old"].gate_score) for r, lab in samples if lab == "pos")
    neg_scores = sorted(float(r["old"].gate_score) for r, lab in samples if lab == "neg")
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


def config_patch(path: str, *, pass_line: float, calibration_version: int, source: str) -> dict:
    """就地更新配置文件（只动必要字段，其它内容原样保留）并备份旧版本。

    **只动线、不动配置世代**：写 `scoring.pass_line` / `pass_line_source` / `scoring.calibration_version`；
    `config_version`（配置世代，application/evaluation 的键值）**保持不变**——否则 `batch status`
    等按当前版本过滤的命令会让整批结果从操作视图里消失。

    备份目录是**配置文件旁边的 `history/`**（不是 cwd 下的 config/history）——按 cwd 存会在
    不同工作目录下把备份散得到处都是。
    """
    with open(path, encoding="utf-8") as _f:
        raw_text = _f.read()
    data = json.loads(raw_text)
    history_dir = os.path.join(os.path.dirname(os.path.abspath(path)), "history")
    os.makedirs(history_dir, exist_ok=True)
    backup = os.path.join(history_dir,
                          f"{os.path.basename(path)}.v{data.get('config_version')}-cv"
                          f"{data.get('scoring', {}).get('calibration_version', 0)}.json")
    if not os.path.exists(backup):
        with open(backup, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    before = json.dumps(data, ensure_ascii=False)
    data.setdefault("scoring", {})
    data["scoring"]["pass_line"] = round(float(pass_line), 2)
    data["scoring"]["pass_line_source"] = source
    data["scoring"]["calibration_version"] = int(calibration_version)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    return {"before": json.loads(before), "after": data, "backup": backup, "raw_before": raw_text}


# ---------------------------------------------------------------- 校准
def preconditions(pos: list[dict], neg: list[dict], counts: dict) -> tuple[bool, str]:
    """校准启用前提（纯函数，便于测试）。

    用户口径：**正样本（auto_reject+keep 确认漏放）不足时先说"校准未启用"**，
    不许静默返回"样本不足"——静默空转比关掉更危险（界面看起来闭环在转，实际一条样本都进不来）。
    """
    # 「无真实 HR 复核结论」是**既定前提**，不是等待项：pos/neg/eligible 全 0 时点名根因，
    # 不伪造标注、不 apply、不静默空转（静默空转比关掉更危险：界面看着闭环在转，一条样本都进不来）。
    if not pos and not neg and not counts.get("eligible", 0):
        return False, ("校准未启用（calibration_enabled=false）：无真实 HR 复核结论 —— "
                       f"pos={len(pos)}, neg={len(neg)}, eligible={counts.get('eligible', 0)}。"
                       "没有真实 HR 标注就不动线：不伪造结论、不 apply、不拿合成数据冒充真实标注进校准池。"
                       "此状态下自动校准不可用，真实动线只能走人工锚点标定（anchor_calibrated）；"
                       "HR 复核结论到达是**独立触发事件**，到达后再走 "
                       "tools/calibration_backfill.py --apply → app.calibrate pool → app.calibrate run。"
                       "pass_line_source 保持原值（未动线）。")
    if len(pos) < MIN_SAMPLES:
        return False, (f"校准未启用（calibration_enabled=false）：正样本（auto_reject+keep = 确认漏放）"
                       f"{len(pos)} 条 < {MIN_SAMPLES} 条，负样本 {counts.get('neg', len(neg))} 条；"
                       f"pass_line_source 保持原值（未动线）。"
                       f"继续积累确认漏放样本，或显式改走锚点标定（anchor_calibrated）——两条路都要人定，不许默认。")
    if len(neg) < MIN_SAMPLES:
        return False, (f"负样本不足：auto_reject + reject（calibration_eligible=true）{len(neg)} 条 < "
                       f"{MIN_SAMPLES} 条；正样本 {len(pos)} 条。转人工件的复核结论按口径不进 pos/neg。")
    return True, ""


def run(job_path: str, *, trigger: str = "manual", band: float | None = None,
        dry_run: bool = False, leak_budget: float = 0.01) -> dict:
    cfg = JobConfig.model_validate(json.load(open(job_path, encoding="utf-8")))
    repo = Repository(cfg.company_id)
    pool = calibration_pool(cfg)
    pos, neg = pool["pos"], pool["neg"]
    counts = pool["counts"]
    # 自愈：任何"不该为真却为真"的 calibration_eligible 复位（dry-run 只报告，apply 才写）
    hygiene = sanitize_calibration_flags(cfg, apply=not dry_run)
    band = band if band is not None else cfg.scoring.band_width
    reason = ""
    status = "skipped"
    calibration_enabled = True

    rows_by_resume = {r["resume_id"]: r for r in load_rows(cfg)}
    labeled_ids = {p["resume_id"] for p in pos} | {n["resume_id"] for n in neg}
    # 有标注但重放时看不到的（缺解析结果/无评估行）——必须报出来，不能静默少算样本
    invisible = sorted(labeled_ids - set(rows_by_resume))
    sw = {"candidates": [], "samples": [], "separated": False}

    stable_ok, stable_why = stability_is_ok(stability_latest(cfg.job_id, cfg.config_version))
    calibration_enabled, pre_reason = preconditions(pos, neg, counts)
    if not calibration_enabled:
        reason = pre_reason
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
        "pos": [{"resume_id": p["resume_id"], "score": _score_of(rows_by_resume, p["resume_id"]),
                 "leak_signal": True} for p in pos],
        "neg": [{"resume_id": n["resume_id"], "score": _score_of(rows_by_resume, n["resume_id"]),
                 "leak_signal": False} for n in neg],
        "band": band,
        "separated": sw.get("separated"),
        "skipped_no_result": invisible,
        "calibration_enabled": calibration_enabled,
        "counts": counts,
        "drop_reasons": dict(pool["reasons"]),
        "hygiene": hygiene,
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
                "candidates": len(sw.get("candidates", [])),
                "calibration_enabled": calibration_enabled, "counts": counts,
                "drop_reasons": dict(pool["reasons"]), "hygiene": hygiene}

    best = choose_line(sw["candidates"])
    from_cv = int(cfg.scoring.calibration_version)          # 校准线版本：动线只动它
    new_cv = from_cv + 1
    detail = (f"自动校准 {datetime.now().strftime('%Y-%m-%d %H:%M')}："
              f"{len(pos)} 该留 / {len(neg)} 该淘汰，漏放 {best['leak']} 误放 {best['wrong']}")
    if dry_run:
        prospective = cfg.model_copy(update={
            "scoring": cfg.scoring.model_copy(update={"pass_line": round(float(best["line"]), 2),
                                                      "pass_line_source": "anchor_calibrated",
                                                      "calibration_version": new_cv})})
        would_freeze = snapshot_path(prospective)
        return {"status": "dry_run", "reason": reason, "before_line": cfg.scoring.pass_line,
                "after_line": best["line"], "samples": samples_meta,
                "config_version": cfg.config_version, "calibration_version": new_cv,
                "from_calibration_version": from_cv,
                "snapshot_path": would_freeze, "snapshot_exists": os.path.exists(would_freeze),
                "calibration_enabled": calibration_enabled, "counts": counts,
                "drop_reasons": dict(pool["reasons"]), "hygiene": hygiene}

    # pass_line_source 是契约里的枚举（manual_default / anchor_calibrated）：
    # 人工标注标定出来的线属于 anchor_calibrated；可读的细节记在 calibration.reason 与审计里。
    patch = config_patch(job_path, pass_line=best["line"], calibration_version=new_cv,
                         source="anchor_calibrated")
    new_cfg = JobConfig.model_validate(patch["after"])
    snap_path = snapshot_path(new_cfg)
    try:
        repo.upsert_job_config(new_cfg)
        # 线动了 → 快照必须同批次落。冻结失败 = 整个 apply 回滚（不允许"线动了快照没动"）
        snap = freeze_snapshot(job_path, out=snap_path, note=detail, quiet=True)
        db.execute(
            """INSERT INTO calibration (company_id, job_id, trigger, from_version, to_version, status,
                                        reason, samples, changed, before_payload, after_payload)
               VALUES (%s, %s, %s, %s, %s, 'applied', %s, %s, %s, %s, %s)""",
            (cfg.company_id, cfg.job_id, trigger, str(from_cv), str(new_cv), reason,
             json.dumps(samples_meta, ensure_ascii=False),
             json.dumps({"pass_line": {"before": cfg.scoring.pass_line, "after": best["line"]},
                         "band_width": band, "snapshot": snap_path,
                         "config_version": cfg.config_version,
                         "calibration_version": {"before": from_cv, "after": new_cv}},
                        ensure_ascii=False),
             json.dumps(patch["before"], ensure_ascii=False), json.dumps(patch["after"], ensure_ascii=False)))
        repo.audit("calibrate_applied", "job", f"{cfg.job_id}@{cfg.config_version}·cv{from_cv}→cv{new_cv}",
                   {"config_version": cfg.config_version, "from_calibration_version": from_cv,
                    "to_calibration_version": new_cv, "pass_line": best["line"], "trigger": trigger,
                    "leak": best["leak"], "wrong": best["wrong"], "manual": best["manual"],
                    "detail": detail, "snapshot": snap_path, "batch_fingerprint": snap["batch_fingerprint"]})
    except Exception as exc:                                    # noqa: BLE001
        undone = _undo_apply(job_path, original=cfg, patch=patch, snap_path=snap_path, repo=repo,
                             from_cv=from_cv, new_cv=new_cv)
        fail_reason = (f"apply 整体回滚（{type(exc).__name__}: {exc}）：线未动、配置世代未动、快照未留。"
                       f"已还原 {undone}")
        db.execute(
            """INSERT INTO calibration (company_id, job_id, trigger, from_version, to_version, status,
                                        reason, samples, changed, before_payload, after_payload)
               VALUES (%s, %s, %s, %s, %s, 'failed', %s, %s, '{}'::jsonb, %s, %s)""",
            (cfg.company_id, cfg.job_id, trigger, str(from_cv), str(from_cv), fail_reason,
             json.dumps(samples_meta, ensure_ascii=False),
             json.dumps(patch["before"], ensure_ascii=False), json.dumps(patch["before"], ensure_ascii=False)))
        repo.audit("calibrate_apply_failed", "job", f"{cfg.job_id}@{cfg.config_version}",
                   {"reason": fail_reason, "trigger": trigger, "snapshot_target": snap_path})
        return {"status": "failed", "reason": fail_reason, "before_line": cfg.scoring.pass_line,
                "after_line": cfg.scoring.pass_line, "samples": samples_meta,
                "config_version": cfg.config_version, "calibration_version": from_cv,
                "snapshot_path": snap_path, "rolled_back": True,
                "calibration_enabled": calibration_enabled, "counts": counts,
                "drop_reasons": dict(pool["reasons"]), "hygiene": hygiene}

    return {"status": "applied", "reason": reason, "before_line": cfg.scoring.pass_line,
            "after_line": best["line"], "backup": patch["backup"],
            "config_version": cfg.config_version, "calibration_version": new_cv,
            "from_calibration_version": from_cv,
            "snapshot_path": snap_path, "batch_fingerprint": snap["batch_fingerprint"],
            "snapshot_n": snap["n"],
            "samples": samples_meta, "candidates": len(sw["candidates"]),
            "calibration_enabled": calibration_enabled, "counts": counts,
            "drop_reasons": dict(pool["reasons"]), "hygiene": hygiene}


def _undo_apply(job_path: str, *, original: JobConfig, patch: dict, snap_path: str,
                repo: Repository, from_cv: int, new_cv: int) -> str:
    """apply 失败时的整体回滚：配置逐字还原、库里的 job_config 还原、删掉半写的快照、抹掉 applied 行。

    顺序反着来（先删库痕迹，再还原文件），每一步都尽力而为并汇总成一句话，便于审计里看见。
    ⚠️ 配置世代不变（cv 方案）：所以**绝不能**去删 job_config 的当前版本行——那是这批 310 行的活配置。
    """
    done = []
    try:
        db.execute("""DELETE FROM calibration
                       WHERE company_id = %s AND job_id = %s AND status = 'applied'
                         AND from_version = %s AND to_version = %s AND reverted_at IS NULL""",
                   (original.company_id, original.job_id, str(from_cv), str(new_cv)))
        done.append("清掉 applied 行")
    except Exception:                                           # noqa: BLE001
        pass
    if os.path.exists(snap_path):
        os.remove(snap_path)
        done.append(f"删掉半写的快照 {snap_path}")
    with open(job_path, "w", encoding="utf-8") as f:
        if patch.get("raw_before") is not None:                 # 逐字还原：连缩进/换行都不改
            f.write(patch["raw_before"])
        else:
            json.dump(patch["before"], f, ensure_ascii=False, indent=2)
            f.write("\n")
    done.append(f"配置文件逐字还原到 v{original.config_version}·cv{from_cv}")
    repo.upsert_job_config(original)
    done.append("库里 job_config 还原")
    return "、".join(done)


def _score_of(rows_by_resume: dict[str, dict], resume_id: str) -> float | None:
    row = rows_by_resume.get(resume_id)
    return float(row["old"].gate_score) if row else None


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
            "back_to_cv": row["from_version"], "undone_cv": row["to_version"],
            "calibration_version": restored.scoring.calibration_version,
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
    ap.add_argument("cmd", choices=["run", "status", "rollback", "pool"])
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
        cv_note = ""
        if r.get("calibration_version") is not None and r["status"] in ("dry_run", "applied"):
            cv_note = (f"（校准线版本 cv{r.get('from_calibration_version')} → cv{r['calibration_version']}；"
                       f"配置世代 v{r.get('config_version')} 不变）")
        print(f"达标线：{r['before_line']} → {r['after_line']}" + cv_note)
        if r.get("snapshot_path"):
            if r["status"] == "dry_run":
                print("若 apply 将冻结到：" + r["snapshot_path"]
                      + ("（该路径已有快照：apply 会覆盖它）" if r.get("snapshot_exists")
                         else "（dry-run 不写快照、不写库）"))
            elif r["status"] == "applied":
                print(f"快照已冻结：{r['snapshot_path']}"
                      f"（批次指纹 {r.get('batch_fingerprint')} · {r.get('snapshot_n')} 份）")
            elif r["status"] == "failed":
                print(f"⚠️ 线未动：{r['reason']}")
        if r["samples"]:
            c = r["samples"].get("counts", {})
            print(f"校准启用：{r.get('calibration_enabled')} · "
                  f"负样本（auto_reject+reject，eligible）{c.get('neg')} 条 · "
                  f"正样本（auto_reject+keep，确认漏放）{c.get('pos')} 条"
                  f"（leak_signal={c.get('pos_leak_signal')}）")
            print(f"  被拒入池 {c.get('dropped')} 条 · 其中人工复核件结论 "
                  f"{c.get('manual_review_verdicts_dropped')} 条 · "
                  f"eligible 但非 auto_reject 的违规行 {c.get('neg_eligible_non_auto_reject')} 条")
            if r["samples"].get("drop_reasons"):
                print("  排除原因：")
                for why, n in sorted(r["samples"]["drop_reasons"].items(), key=lambda kv: -kv[1]):
                    print(f"    {n:>3} 条 · {why}")
            if r["samples"]["neg"]:
                print(f"  负样本分数：{[s['score'] for s in r['samples']['neg']]}")
            if r["samples"]["pos"]:
                print(f"  正样本分数：{[s['score'] for s in r['samples']['pos']]}")
            print(f"  两组分得开：{r['samples']['separated']}")
            h = r["samples"].get("hygiene") or {}
            if h.get("violations"):
                print(f"  ⚠️ 自愈：发现 {h['violations']} 行 calibration_eligible 违规（已{'复位' if h.get('applied') else '待复位'}）")
        if r["status"] == "applied":
            print("（全自动生效：下次跑批/重放就用新线；不满意可 `python -m app.calibrate rollback`）")
        return 0

    if a.cmd == "pool":
        cfg = JobConfig.model_validate(json.load(open(a.job, encoding="utf-8")))
        pool = calibration_pool(cfg)
        c = pool["counts"]
        h = sanitize_calibration_flags(cfg, apply=False)
        print(f"校准样本入口（选项 A：只认 spot_check_item 的标记位 + 复核结论）· 岗位 {cfg.job_id}@{cfg.config_version}")
        print(f"  负样本 neg（auto_reject + eligible + reject）：{c['neg']} 条")
        print(f"  正样本 pos（auto_reject + keep = 确认漏放）  ：{c['pos']} 条（leak_signal={c['pos_leak_signal']}）")
        print(f"  被拒入池            ：{c['dropped']} 条（其中人工复核件结论 {c['manual_review_verdicts_dropped']} 条）")
        print(f"  已复核总行数        ：{c['reviewed_rows']} 条")
        print(f"  ⚠️ eligible 但非 auto_reject 的违规行：{c['neg_eligible_non_auto_reject']} 条"
              + (f" {pool['violations']}" if pool["violations"] else "（应为 0）"))
        print(f"  自愈检查：库里 eligible=true 共 {h['checked']} 行 · 违规 {h['violations']} 行")
        if pool["reasons"]:
            print("  排除原因分布：")
            for why, n in sorted(pool["reasons"].items(), key=lambda kv: -kv[1]):
                print(f"    {n:>3} 条 · {why}")
        ok = len(pool["pos"]) >= MIN_SAMPLES and len(pool["neg"]) >= MIN_SAMPLES
        hint = "" if ok else (
            "（正/负样本各需 ≥" + str(MIN_SAMPLES) + "：当前 "
            + str(len(pool["pos"])) + "/" + str(len(pool["neg"])) + "）")
        print("")
        print("  校准是否启用：" + ("是" if ok else "否" + hint))
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
        print(f"已回滚：撤销 cv{r['undone_cv']} 的校准，线版本回到 cv{r['back_to_cv']}"
              f"（达标线 {r['pass_line']}，配置世代不变）")
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
