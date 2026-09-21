# -*- coding: utf-8 -*-
"""重跑后的**自动 sanity check**：三条硬断言不过，报告不许出。

为什么要有它（用户提的流程漏洞，确实存在）：加分项恒为 0（`bonus_raw` 全 0）→ 分位全并列 0.50
→ 加分项恒等于 20.0 分，这三个信号任何一个都该在重跑结束后的第一份检查里自动拦住。
实测是**人肉看出来的**，不是系统报出来的——静默 bug 因此活到了重跑结束。

三条硬断言（用户口径）：
  1. 加分项标准差 > 0        —— 加分项不许退化成常数
  2. 分位取值数 > 10          —— 并列不能吃掉分位（整批并列 = 归一化失效）
  3. 四档中位数两两差距 > 2 分 —— 档位之间必须有可分辨的距离
另外报两条告警（不阻断）：加分项与条目数的相关性、注水型是否高于明显不匹配（倒挂）。

用法：python tools/sanity_check.py --job config/job_hr_admin.json [--corpus local/samples_admin]
退出码：0 通过 / 1 有断言不过（报告脚本应先调它）
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import statistics as st
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # noqa: E402
from app.contracts import Evaluation, JobConfig      # noqa: E402
from app.store import db                            # noqa: E402

CN = {1: "明显合格", 2: "边缘合格", 3: "明显不匹配", 4: "注水型"}
ap = argparse.ArgumentParser()
ap.add_argument("--job", default="config/job_hr_admin.json")
ap.add_argument("--corpus", default="local/samples_admin")
ap.add_argument("--min-gap", type=float, default=2.0)
ap.add_argument("--min-ranks", type=int, default=10)
a = ap.parse_args()

cfg = JobConfig.model_validate(json.load(open(a.job, encoding="utf-8")))
tier = {m["file"]: m["tier"] for m in json.load(open(os.path.join(a.corpus, "manifest.json"),
                                                    encoding="utf-8"))}
rows = db.fetch_all(
    """SELECT r.file_name, e.payload FROM evaluation e JOIN application ap USING(application_id)
         JOIN resume r ON r.resume_id = e.resume_id
        WHERE ap.job_id = %s AND ap.config_version = %s""", (cfg.job_id, cfg.config_version))
if not rows:
    print("❌ 没有任何评估结果"); raise SystemExit(1)

bonus, ranks, nspans, by_tier = [], [], [], collections.defaultdict(list)
for r in rows:
    p = Evaluation.model_validate(r["payload"]).score_parts
    if p is None:
        continue
    bonus.append(p.bonus); ranks.append(round(p.rank01, 3)); nspans.append(p.n_spans)
    by_tier[tier.get(r["file_name"])].append(p.total)

fails, warns = [], []
# 断言 1：加分项标准差 > 0
sd = st.pstdev(bonus) if len(bonus) > 1 else 0.0
print(f"[1] 加分项：n={len(bonus)} 中位 {st.median(bonus):.2f} · 标准差 {sd:.2f} · 区间 {min(bonus):.1f}~{max(bonus):.1f}")
if not sd > 0:
    fails.append("加分项标准差 = 0（加分项退化成常数 → 分位/难度系数丢了字段）")
# 断言 2：分位取值数 > 10
uniq = len(set(ranks))
print(f"[2] 分位：不同取值 {uniq} 个 · 恰好 0.50 的 {ranks.count(0.5)} 份")
if uniq <= a.min_ranks:
    fails.append(f"分位只有 {uniq} 个取值（≤{a.min_ranks}）：整批并列，归一化失效")
# 断言 3：四档中位数两两差距 > 2 分
med = {t: st.median(v) for t, v in by_tier.items() if t in CN and v}
print("[3] 四档中位数：" + " · ".join(f"{CN[t]} {med[t]:.1f}" for t in (1, 2, 3, 4) if t in med))
for t1, t2 in ((1, 2), (2, 3), (3, 4), (1, 4), (2, 4)):
    if t1 in med and t2 in med and abs(med[t1] - med[t2]) <= a.min_gap:
        fails.append(f"{CN[t1]} 与 {CN[t2]} 中位相差 {abs(med[t1] - med[t2]):.1f} ≤ {a.min_gap}，档位分不开")
# 告警：加分项与条目数的相关性（若强相关，说明它在奖励条数）
if len(nspans) > 2 and st.pstdev(nspans) > 0 and st.pstdev(bonus) > 0:
    mx, my = st.mean(nspans), st.mean(bonus)
    cov = sum((x - mx) * (y - my) for x, y in zip(nspans, bonus)) / len(nspans)
    corr = cov / (st.pstdev(nspans) * st.pstdev(bonus))
    print(f"[W] 加分项与证据条数的相关系数 {corr:+.2f}" + ("（强相关 → 它在奖励'条目多'）" if corr > 0.7 else ""))
    if corr > 0.7:
        warns.append(f"加分项与条数相关 {corr:+.2f}：模型给注水型的条目打分偏高，加分项会偏向注水")
if 3 in med and 4 in med and med[4] > med[3]:
    warns.append(f"注水型 {med[4]:.1f} > 明显不匹配 {med[3]:.1f}（排序倒挂；判定层面无害，排序层面会乱）")
    print(f"[W] 倒挂：注水型 {med[4]:.1f} > 明显不匹配 {med[3]:.1f}")

print()
for w in warns:
    print(f"⚠️  告警（不阻断）：{w}")
if fails:
    print()
    for f in fails:
        print(f"❌ 断言失败：{f}")
    print("\n结论：**报告不许出**，先查上面这几条。")
    raise SystemExit(1)
print("✅ 三条硬断言全部通过 —— 可以出报告。")
