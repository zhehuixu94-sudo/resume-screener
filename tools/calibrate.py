#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""任务 3：用 HR 给定的锚点样本标定达标线（替代"拍 60"）。

机制：
  1. 从库里取这些锚点样本在当前岗位配置下的**判定分**（gate_score —— 与 decide() 同一把尺子，
     不是契约里那个"用于报告与审计"的 base_total）；
  2. 校验两条硬条件 —— 合格组 ≥3 份、不合格组 ≥2 份；**不满足则拒绝写盘，没有绕过开关**
     （`--dry-run` 仍照常显示便于诊断；「合格 <3」同时也是 JobConfig 的硬约束，改工具也绕不过）；
  3. **分得开才算标定成功**：max(不合格组) < min(合格组)；分不开时照样写线，但明确警告并记录
     （分不开说明要么样本没标对，要么维度设计区分不出来，此时不能上线自动淘汰）；
     —— 注意「分不开」是**有意**照样写线（只警告），与「数量不足」的拒绝写盘**不同**：
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

MIN_GOOD = 3      # 硬条件：合格锚点份数下限（不满足 → 拒绝写盘）
MIN_BAD = 2       # 硬条件：不合格锚点份数下限


def undersized_blockers(n_good: int, n_bad: int) -> list[str]:
    """硬条件检查（纯函数，便于测试）：返回**阻止写盘**的原因列表，空 = 通过。

    为什么必须是硬条件：样本不足时算出来的"线"只是这几个样本的下限，不是一个可信的达标线。
    实测踩过：2 份合格 + 1 份不合格照样写出一条线并标成 `anchor_calibrated`，等于让 3 份样本冒充结论。
    注意与「两组分不开」区分 —— 后者是**有意**只警告、照样写线（便于留证据、交人工看）。
    """
    out = []
    if n_good < MIN_GOOD:
        out.append(f"合格锚点只有 {n_good} 份，硬条件要求 ≥{MIN_GOOD}（HR 侧建议 3~5 份）")
    if n_bad < MIN_BAD:
        out.append(f"不合格锚点只有 {n_bad} 份，硬条件要求 ≥{MIN_BAD}")
    return out


def scores_of(cfg: JobConfig, filenames: list[str], version: str) -> tuple[list[tuple[str, float]], list[str]]:
    """取锚点样本的**判定分**（`gate_score`），不是审计分。

    ⚠️ 这里曾经读 `base_total` —— 契约明写它是「规则层维度分合计（不含加分项/扣分）；用于报告与审计」，
    而 `evaluate()` 传给 `decide()` 的是 `gate_total`（= gate_score）。两把尺子不同刻度：
    实测同一批锚点，用 base_total 标出 **62.2**、用 gate_score 标出 **76.4**，**差 14.2 分** ——
    标定出的线随后被拿去和 gate_score 比，等于线被系统性低估。
    旧口径下 bonus/penalty 恒为 0、两者恰好相等，所以这个 bug 潜伏了很久，只在换新口径后才咬人。
    """
    found, missing = [], []
    for name in filenames:
        row = db.fetch_one(
            """SELECT r.file_name, e.base_total, e.gate_score, e.verdict, e.decision
                 FROM application a
                 JOIN resume r ON r.resume_id = a.resume_id
                 JOIN evaluation e ON e.application_id = a.application_id
                WHERE a.company_id = %s AND a.job_id = %s AND a.config_version = %s AND r.file_name = %s""",
            (cfg.company_id, cfg.job_id, version, name))
        if row is None or row["gate_score"] is None:
            missing.append(name)
            continue
        # 尺子必须与判定一致；不一致时把差异打印出来，别再让它悄悄发生
        if row["base_total"] is not None and abs(float(row["base_total"]) - float(row["gate_score"])) > 0.05:
            print(f"  [尺子] {row['file_name']}: 判定分 {float(row['gate_score']):.2f} "
                  f"≠ 审计分 {float(row['base_total']):.2f}（标定用判定分）")
        found.append((row["file_name"], float(row["gate_score"])))
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

    # ⛔ blockers = 阻止写盘的硬条件；⚠️ warnings = 只警告（分不开时照样写线，是 docstring 明写的有意设计）
    blockers, warnings = [], []
    blockers = undersized_blockers(len(good), len(bad))
    if b_max >= g_min:
        warnings.append(f"两组分数没分开（不合格组上限 {b_max:.1f} ≥ 合格组下限 {g_min:.1f}）："
                        f"要么锚点标错了，要么维度设计区分不出来，此时不能上线自动淘汰")
    for p in warnings:
        print(f"  ⚠️ {p}")
    for p in blockers:
        print(f"  ⛔ {p}")

    line = round(g_min + a.margin, 1)
    print(f"\n标定结果：达标线 {line:.1f}（合格组下限 {g_min:.1f} + 余量 {a.margin:.1f}），"
          f"三段带 ±{cfg.scoring.band_width} → 自动达标 ≥{line + cfg.scoring.band_width:.1f}，自动淘汰 <{line - cfg.scoring.band_width:.1f}")
    if a.dry_run:
        if blockers:
            print("  （--dry-run：以上是诊断；真跑会被拒绝写盘，没有绕过开关）")
        print("（--dry-run，不写配置）")
        return 0

    # 硬条件不满足 → 拒绝写盘。样本不足时标出的"线"只是这几个样本的下限，
    # 不是一个可信的达标线；静默落盘会让它冒充结论（实测踩过：2 份合格 + 1 份不合格照样出线）。
    if blockers:
        print("\n❌ 拒绝写盘：锚点不满足硬条件 ——")
        for p in blockers:
            print(f"   ⛔ {p}")
        print("   只能补足锚点后重跑；没有绕过开关（「合格 <3」本身也是 JobConfig 的硬约束）。")
        return 3

    raw["scoring"]["pass_line"] = line
    raw["scoring"]["pass_line_source"] = "anchor_calibrated"
    raw["calibration"] = {
        "status": "calibrated",
        "anchors": ([{"label": "合格", "resume_ref": n, "score": s} for n, s in good]
                    + [{"label": "不合格", "resume_ref": n, "score": s} for n, s in bad]),
        "good_group_min": g_min,
        "calibrated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "notes": "；".join(warnings + blockers) or "合格组与不合格组分数分得开",
    }
    raw["config_version"] = str(int(cfg.config_version) + 1)
    # ⚠️ 必须**先验后写**。这里曾经先 `json.dump` 再 `model_validate`：校验抛错时文件已被写成
    # version+1 的**非法配置**（实测：2 份合格锚点 → 写出 v9 再崩，留下一个永远过不了契约的配置）。
    # 契约是最终防线，得在落盘前当门。
    try:
        JobConfig.model_validate(raw)
    except Exception as e:
        print("\n❌ 拒绝写盘：标定结果过不了配置契约 ——")
        print(f"   {str(e).splitlines()[0] if str(e) else type(e).__name__}")
        if blockers:
            print("   锚点不足很可能是原因。注意「合格 <3」是 JobConfig 的硬约束，"
                  "本工具没有绕过开关——只能补锚点。")
        print(f"   配置文件未改动：{path}")
        return 1

    json.dump(raw, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(f"已写入 {path}：配置版本 → {raw['config_version']}，达标线 {line:.1f}（来源 anchor_calibrated）")
    print("提示：配置改了，用 tools/replay_decision.py --from-version <旧版本> 可以秒看重放效果，不必重跑 LLM。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
