#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""任务 3：用 HR 给定的锚点样本标定达标线（替代"拍 60"）。

机制：
  1. 从库里取这些锚点样本在当前岗位配置下的规则分；
  2. 校验两条硬条件 —— 合格组 ≥3 份、不合格组 ≥2 份；
  3. **分得开才算标定成功**：max(不合格组) < min(合格组)；分不开时照样写线，但明确警告并记录
     （分不开说明要么样本没标对，要么维度设计区分不出来，此时不能上线自动淘汰）；
  4. 达标线 = 合格组下限（可加 `--margin` 微调）；同时写三段带与来源标记；
  5. 配置版本 +1，锚点样本与分数全部落进 calibration，历史可追溯。

用法：
  python tools/calibrate.py --job config/job_jushi_teacher.json \\
      --good 示例候选人简历3.0.docx resume_002.txt resume_001.txt \\
      --bad  resume_b.pdf 个人简历.docx resume_a.pdf \\
      --margin 0 [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.contracts import JobConfig  # noqa: E402
from app.store import Repository, db  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def scores_of(cfg: JobConfig, filenames: list[str], version: str) -> tuple[list[tuple[str, float]], list[str]]:
    found, missing = [], []
    for name in filenames:
        row = db.fetch_one(
            """SELECT r.file_name, e.base_total, e.verdict, e.decision
                 FROM application a
                 JOIN resume r ON r.resume_id = a.resume_id
                 JOIN evaluation e ON e.application_id = a.application_id
                WHERE a.company_id = %s AND a.job_id = %s AND a.config_version = %s AND r.file_name = %s""",
            (cfg.company_id, cfg.job_id, version, name))
        if row:
            found.append((row["file_name"], float(row["base_total"])))
        else:
            missing.append(name)
    return found, missing


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--job", required=True)
    ap.add_argument("--good", nargs="+", required=True, help="HR 认可的合格锚点（简历文件名）")
    ap.add_argument("--bad", nargs="+", required=True, help="HR 认可的不合格锚点（简历文件名）")
    ap.add_argument("--margin", type=float, default=0.0, help="达标线在合格组下限基础上再加多少")
    ap.add_argument("--dry-run", action="store_true", help="只算不写")
    ap.add_argument("--from-version", default=None,
                    help="从哪个配置版本取分数（改过配置、还没按新配置跑过 LLM 时用它）")
    a = ap.parse_args()

    path = a.job if os.path.exists(a.job) else os.path.join(ROOT, a.job)
    raw = json.load(open(path, encoding="utf-8"))
    cfg = JobConfig.model_validate(raw)
    print(f"岗位 {cfg.job_id}@{cfg.config_version} ｜ 当前达标线 {cfg.scoring.pass_line}"
          f"（{cfg.scoring.pass_line_source}）")

    version = a.from_version or cfg.config_version
    if version != cfg.config_version:
        print(f"分数取自配置版本 {version} 的评估结果（当前配置版本 {cfg.config_version}）")
    good, miss_g = scores_of(cfg, a.good, version)
    bad, miss_b = scores_of(cfg, a.bad, version)
    for name in miss_g:
        print(f"  ⚠️ 合格锚点找不到评估结果（文件名要对得上库里存的）：{name}")
    for name in miss_b:
        print(f"  ⚠️ 不合格锚点找不到评估结果：{name}")
    if not good or not bad:
        print("锚点样本不足，无法标定")
        return 2

    g_scores = [s for _n, s in good]
    b_scores = [s for _n, s in bad]
    g_min, b_max = min(g_scores), max(b_scores)
    print(f"\n合格组 {len(good)} 份：{', '.join(f'{n}={s:.1f}' for n, s in sorted(good, key=lambda x: x[1]))}")
    print(f"  下限 {g_min:.1f}")
    print(f"不合格组 {len(bad)} 份：{', '.join(f'{n}={s:.1f}' for n, s in sorted(bad, key=lambda x: -x[1]))}")
    print(f"  上限 {b_max:.1f}")

    problems = []
    if len(good) < 3:
        problems.append(f"合格锚点只有 {len(good)} 份，规则要求 ≥3（HR 侧建议 3~5 份）")
    if len(bad) < 2:
        problems.append(f"不合格锚点只有 {len(bad)} 份，规则要求 ≥2")
    if b_max >= g_min:
        problems.append(f"两组分数没分开（不合格组上限 {b_max:.1f} ≥ 合格组下限 {g_min:.1f}）："
                        f"要么锚点标错了，要么维度设计区分不出来，此时不能上线自动淘汰")
    for p in problems:
        print(f"  ⚠️ {p}")

    line = round(g_min + a.margin, 1)
    print(f"\n标定结果：达标线 {line:.1f}（合格组下限 {g_min:.1f} + 余量 {a.margin:.1f}），"
          f"三段带 ±{cfg.scoring.band_width} → 自动达标 ≥{line + cfg.scoring.band_width:.1f}，自动淘汰 <{line - cfg.scoring.band_width:.1f}")
    if a.dry_run:
        print("（--dry-run，不写配置）")
        return 0

    raw["scoring"]["pass_line"] = line
    raw["scoring"]["pass_line_source"] = "anchor_calibrated"
    raw["calibration"] = {
        "status": "calibrated",
        "anchors": ([{"label": "合格", "resume_ref": n, "score": s} for n, s in good]
                    + [{"label": "不合格", "resume_ref": n, "score": s} for n, s in bad]),
        "good_group_min": g_min,
        "calibrated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "notes": "；".join(problems) if problems else "合格组与不合格组分数分得开",
    }
    raw["config_version"] = str(int(cfg.config_version) + 1)
    json.dump(raw, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    JobConfig.model_validate(raw)      # 写完立刻自检：标定后的配置必须过契约
    print(f"已写入 {path}：配置版本 → {raw['config_version']}，达标线 {line:.1f}（来源 anchor_calibrated）")
    print("提示：配置改了，用 tools/replay_decision.py --from-version <旧版本> 可以秒看重放效果，不必重跑 LLM。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
