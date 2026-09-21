#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从 HR 给的合格/不合格锚点样本**推导及格项**，产出待 HR 勾选校对的清单。

## 这套流程的两段（用户 2026-09-18 定的原始流程）

1. **定线**（`tools/calibrate.py`）：HR 给合格 3~5 份 + 不合格 2~3 份 → 达标线 = 合格组下限
2. **定及格项**（本工具）：**系统从同一批样本推导"及格必须满足哪些项"** → HR 逐条勾选校对我选得对不对

第 2 段是等分制的前置：`equal_split` 的"每项基础分 = 及格分 ÷ 及格项数"里，那份清单不能拍，
要么系统推导、要么 HR 手写。原始设计是**系统推导 + HR 校对**（落 `basic_item_label` 表）。

## 推导口径（可解释，不拍脑袋）

"某维度达成" = 该维度内**存在一条 quality ≥ 基准**的证据（quality = 相似度 × 难度；
基准 1.0 = 强相关 × 中等难度 = 等分制里"恰好拿到它那一份"）。

对每个维度算两个量：

- **必要性** = 合格组里达成该维度的比例 —— 高 = "及格的人普遍有这一项"
- **区分性** = 合格组达成率 − 不合格组达成率 —— 高 = "这一项能把这批人分开"

建议规则：必要性 ≥ 0.6 且区分性 ≥ 0.4 → 建议作及格项；必要性高但区分性低 → 区分不了（人人都有）；
必要性低 → **不能作及格项**（连及格的人都普遍不满足，要求它等于误杀）。

条目级输出 = 合格样本里**实际达成该维度的那几条证据**，作为 HR 逐条勾选的对象。

## 用法

    # 推导 + 产出待勾选清单
    python tools/derive_basic_items.py --job config/job_hr_admin.json \\
        --good admin_001.txt admin_002.txt admin_003.txt \\
        --bad  admin_151.txt admin_152.txt

    # HR 在 out/basic_items_proposal.csv 里填 hr_tick 列（y/n）后回灌
    python tools/derive_basic_items.py --job config/job_hr_admin.json \\
        --apply out/basic_items_proposal.csv
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.contracts import JobConfig, Evaluation                    # noqa: E402
from app.pipeline.rules import DIFFICULTY_SCORE, RELEVANCE_SCORE   # noqa: E402
from app.store import db                                           # noqa: E402
from tools.evidence_source import span_text_map                    # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MIN_GOOD, MIN_BAD = 3, 2          # 与 tools/calibrate.py 同一套硬条件
QUALITY_BASELINE = 1.0            # 强 × 中
NECESSITY_MIN = 0.6               # 必要性下限：合格组里至少这么多人满足，才谈得上"必须满足"
SEPARATION_MIN = 0.4              # 区分性下限


def items_of(cfg: JobConfig, filenames: list[str], version: str) -> dict[str, list[dict]]:
    """取每份样本的标注条目：{file: [{dimension, item_id, text, quality, rel, diff}]}"""
    out: dict[str, list[dict]] = {}
    for name in filenames:
        row = db.fetch_one(
            """SELECT r.file_name, e.payload FROM application a
                 JOIN resume r ON r.resume_id = a.resume_id
                 JOIN evaluation e ON e.application_id = a.application_id
                WHERE a.company_id = %s AND a.job_id = %s AND a.config_version = %s AND r.file_name = %s""",
            (cfg.company_id, cfg.job_id, version, name))
        if not row:
            continue
        ev = Evaluation.model_validate(row["payload"])
        tmap = span_text_map(row["payload"], None)      # payload 自带 cited_evidence（真原文）
        items = []
        for d in ev.dimensions:
            for it in (d.items or []):
                rel = it.relevance_score if it.relevance_score is not None else RELEVANCE_SCORE.get(it.relevance or "", 0.0)
                dif = it.difficulty_score if it.difficulty_score is not None else DIFFICULTY_SCORE.get(it.difficulty or "", 1.0)
                quote = (tmap.get(it.span_id or "", "") or it.reason or "").strip()
                items.append({"dimension": d.id, "item_id": it.item_id,
                              "text": (it.capability or "").strip(),
                              "evidence": quote[:100],
                              "quality": float(rel) * float(dif),
                              "rel": float(rel), "diff": float(dif)})
        out[row["file_name"]] = items
    return out


def reach_of(items: list[dict], dim: str) -> float:
    """该样本在维度 dim 的达成度 = 该维度最强条目的 quality（无条目 = 0）。"""
    qs = [i["quality"] for i in items if i["dimension"] == dim]
    return max(qs) if qs else 0.0


def empirical_baseline(good: dict, bad: dict) -> float:
    """实测质量基准：把"实际上的满足"拉回 1.0。

    `equal_split` 的 docstring 写得很清楚：理论基准 = 强 × 中 = 1.0，但**模型实测的"强×中"中位只有 ~0.77**。
    用理论的 1.0 当"达成"门槛，会把合格样本里本该算达成的条目判成没达成
    （实测：5 份合格样本里 top 维度只有 3/5 达成）。这里按样本里「强 × 中」的实际中位取基准。
    """
    qs = [i["quality"] for items in list(good.values()) + list(bad.values())
          for i in items if i["rel"] >= RELEVANCE_SCORE["强"] - 1e-9 and abs(i["diff"] - 1.0) < 1e-9]
    if not qs:
        return QUALITY_BASELINE
    qs.sort()
    mid = qs[len(qs) // 2]
    return max(mid, 1e-6)


def derive(cfg: JobConfig, good: dict, bad: dict, baseline: float) -> dict:
    rows = []
    for d in cfg.dimensions:
        g = [reach_of(its, d.id) >= baseline for its in good.values()]
        b = [reach_of(its, d.id) >= baseline for its in bad.values()]
        nec = sum(g) / len(g) if g else 0.0
        sep = nec - (sum(b) / len(b) if b else 0.0)
        if nec < NECESSITY_MIN:
            verdict, why = "不作及格项", f"合格组只有 {nec:.0%} 满足 —— 要求它会误杀及格的人"
        elif sep < SEPARATION_MIN:
            verdict, why = "建议不强（区分不了）", f"合格组 {nec:.0%} 满足，但不合格组也普遍满足（区分性 {sep:+.0%}）"
        else:
            verdict, why = "建议作及格项", f"合格组 {nec:.0%} 满足、区分性 {sep:+.0%}"
        rows.append({"id": d.id, "name": d.name, "weight": d.weight,
                     "nec": nec, "sep": sep, "verdict": verdict, "why": why,
                     "g_hit": sum(g), "g_n": len(g), "b_hit": sum(b), "b_n": len(b)})
    return {"dims": rows, "baseline": baseline}


def representatives(cfg: JobConfig, good: dict, baseline: float) -> list[dict]:
    """条目级待勾选清单：合格样本里**实际达成该维度**的那些条目（按维度分组，文本去重）。"""
    out = []
    for d in cfg.dimensions:
        seen: dict[str, dict] = {}
        for fname, items in good.items():
            top = [i for i in items if i["dimension"] == d.id and i["quality"] >= baseline]
            for i in top:
                key = i["text"] or i["item_id"]
                if key in seen:
                    seen[key]["n_files"] += 1
                    seen[key]["files"].append(fname)
                    seen[key]["quality"] = max(seen[key]["quality"], i["quality"])
                else:
                    seen[key] = {"dimension": d.id, "item_id": i["item_id"], "text": key,
                                 "evidence": i["evidence"][:80], "quality": i["quality"],
                                 "n_files": 1, "files": [fname]}
        for v in sorted(seen.values(), key=lambda x: (-x["n_files"], -x["quality"])):
            out.append(v)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--job", required=True)
    ap.add_argument("--good", nargs="+", help="HR 认可的合格样本（文件名）")
    ap.add_argument("--bad", nargs="+", help="HR 认可的不合格样本")
    ap.add_argument("--from-version", default=None)
    ap.add_argument("--out", default="out/basic_items_proposal.csv")
    ap.add_argument("--apply", default=None, help="HR 勾选后的 CSV：回灌清单（落库 + 写岗位清单）")
    ap.add_argument("--actor", default="hr",
                    help="落 basic_item_label 时记的操作者。默认 hr；**自测/演练必须传 sim**，"
                         "免得把模拟勾选冒充成 HR 的真实判断")
    a = ap.parse_args()

    path = a.job if os.path.exists(a.job) else os.path.join(ROOT, a.job)
    cfg = JobConfig.model_validate(json.load(open(path, encoding="utf-8")))

    if a.apply:
        return apply_ticks(cfg, a.apply, path_version=cfg.config_version, actor=a.actor)

    if not a.good or not a.bad:
        print("❌ 需要 --good 与 --bad（HR 给的锚点样本）")
        return 2
    version = a.from_version or cfg.config_version
    good, bad = items_of(cfg, a.good, version), items_of(cfg, a.bad, version)
    miss = [n for n in a.good if n not in good] + [n for n in a.bad if n not in bad]
    for n in miss:
        print(f"  ⚠️ 找不到评估结果：{n}")

    # 与 calibrate.py 同一套硬条件：样本不足不许往下走（推导出的清单同样会冒充结论）
    problems = []
    if len(good) < MIN_GOOD:
        problems.append(f"合格样本只有 {len(good)} 份，硬条件要求 ≥{MIN_GOOD}")
    if len(bad) < MIN_BAD:
        problems.append(f"不合格样本只有 {len(bad)} 份，硬条件要求 ≥{MIN_BAD}")
    print(f"\n岗位 {cfg.job_id}@{cfg.config_version} ｜ 合格 {len(good)} 份 · 不合格 {len(bad)} 份 · 取自版本 {version}")
    for p in problems:
        print(f"  ⛔ {p}")
    if problems:
        print("\n❌ 拒绝推导：锚点样本不满足硬条件 —— 推导出的清单会冒充结论。补足样本后重跑。")
        return 3

    base = empirical_baseline(good, bad)
    res = derive(cfg, good, bad, base)
    print(f"\n达成门槛（质量基准）：实测 {base:.3f}（理论值 {QUALITY_BASELINE} = 强×中；"
          f"模型实测的强×中中位偏低，直接用理论值会漏判达成）")
    print(f"\n{'维度':<10}{'必要性':>8}{'区分性':>8}  建议")
    for r in res["dims"]:
        print(f"  {r['id']:<8}{r['nec']:>8.0%}{r['sep']:>+8.0%}  {r['verdict']}")
        print(f"  {'':<8}{'':<16}  {r['why']}（合格 {r['g_hit']}/{r['g_n']} · 不合格 {r['b_hit']}/{r['b_n']}）")

    reps = representatives(cfg, good, base)
    os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
    cols = ["dimension", "item_id", "item_text", "evidence", "quality", "n_files", "files", "hr_tick"]
    with open(a.out, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for v in reps:
            w.writerow({"dimension": v["dimension"], "item_id": v["item_id"], "item_text": v["text"],
                        "evidence": v["evidence"], "quality": round(v["quality"], 3),
                        "n_files": v["n_files"], "files": "；".join(v["files"]), "hr_tick": ""})
    print(f"\n待勾选清单已写出：{a.out}（{len(reps)} 条，按维度分组）")
    print("  HR 请填 `hr_tick` 列：y = 认可这条能代表该维度的及格标准；n = 不认可。")
    print(f"  填完回灌：python tools/derive_basic_items.py --job {a.job} --apply {a.out}")
    return 0


def apply_ticks(cfg: JobConfig, csv_path: str, *, path_version: str, actor: str = "hr") -> int:
    """回灌 HR 勾选结果：落 `basic_item_label` 表 + 写岗位清单文件（供 equal_split 读）。"""
    rows = list(csv.DictReader(open(csv_path, encoding="utf-8-sig")))
    if not rows:
        print("❌ 勾选清单为空")
        return 2
    ticked = [r for r in rows if (r.get("hr_tick") or "").strip().lower() in ("y", "yes", "1", "是", "√")]
    if not ticked:
        print("❌ 没有一条被勾选（hr_tick 列全空或全 n）—— 拒绝回灌：空标准比没标准更糟")
        return 2

    dims = sorted({r["dimension"] for r in ticked})
    unknown = [d for d in dims if d not in {x.id for x in cfg.dimensions}]
    if unknown:
        print(f"❌ 勾选里出现未配置的维度：{unknown}")
        return 2

    # 1) 条目级：落 basic_item_label（沉淀岗位标准）
    repo_rows = 0
    for r in ticked:
        first_file = (r.get("files") or "").split("；")[0].strip()
        if not first_file:
            continue
        app = db.fetch_one(
            """SELECT a.application_id FROM application a JOIN resume r ON r.resume_id = a.resume_id
                WHERE a.company_id = %s AND a.job_id = %s AND a.config_version = %s AND r.file_name = %s""",
            (cfg.company_id, cfg.job_id, path_version, first_file))
        if not app:
            continue
        db.execute(
            """INSERT INTO basic_item_label (company_id, application_id, job_id, config_version,
                                             item_key, dimension, is_basic, actor)
               VALUES (%s,%s,%s,%s,%s,%s,true,%s)
               ON CONFLICT (application_id, item_key)
               DO UPDATE SET is_basic = true, dimension = EXCLUDED.dimension""",
            (cfg.company_id, app["application_id"], cfg.job_id, path_version,
             r["item_id"], r["dimension"], actor))
        repo_rows += 1

    # 2) 维度级：写岗位清单文件（equal_split 的 basic_items_of 读它）
    out_path = os.path.join(ROOT, "config", f"basic_items_{cfg.job_id}.json")
    payload = {"job_id": cfg.job_id, "config_version": cfg.config_version,
               "source": f"derive_basic_items.py ← {os.path.basename(csv_path)}（HR 勾选 {len(ticked)} 条）",
               "basic_items": dims}
    json.dump(payload, open(out_path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    per = 60.0 / len(dims) if dims else 0.0
    print(f"✅ 回灌完成：勾选 {len(ticked)}/{len(rows)} 条 → 及格项清单 {dims}")
    print(f"   条目级落库 {repo_rows} 行 · 维度级清单写入 {out_path}")
    print(f"   等分制会读这份清单：每项基础分 = 及格分 60 ÷ {len(dims)} = {per:.2f} 分")
    return 0


if __name__ == "__main__":
    sys.exit(main())
