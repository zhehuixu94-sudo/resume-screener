# -*- coding: utf-8 -*-
"""演练：把合成复核结论灌进 audit_sample_v1.csv 对应的抽检行，跑一遍样本入口 + 回灌，
最后**复位**（不留任何合成结论在库里）。

为什么要有这个演练：光看代码说"转人工件的结论不进池"不算数，得让 8 条合成结论真的走一遍入口，
把 neg / pos / 被拒 / reason 四组数打印出来。跑完即复位，生产库不留假标注。
"""
from __future__ import annotations

import argparse
import csv
import subprocess
import sys

sys.path.insert(0, ".")
try:                                    # 子进程与自己共用 stdout，必须行缓冲，否则输出会交错
    sys.stdout.reconfigure(line_buffering=True)
except Exception:  # noqa: BLE001
    pass
from app.store import db  # noqa: E402

JOBS = ("config/job_hr_admin.json",)
PY = sys.executable


def sh(*args: str) -> None:
    print(f"\n$ {' '.join(args)}")
    subprocess.run(list(args), check=False)


REVIEWS = "out/calibration_reviews_demo.csv"
LEAK_BUDGET = "0.01"   # 默认 1%：3 条正样本证明不了，会 skipped（统计事实）


def apply_demo(reviews: str) -> int:
    rows = list(csv.DictReader(open(reviews, encoding="utf-8-sig")))
    for r in rows:
        db.execute(
            """UPDATE spot_check_item SET review_verdict = %s, reviewer = %s, review_ts = %s, state = %s
                WHERE job_id = 'hr_admin' AND application_id = %s""",
            (r["review_verdict"], r["reviewer"], r["review_ts"],
             {"reject": "rejected", "keep": "passed"}.get(r["review_verdict"], "pending"),
             int(r["application_id"])))
    return len(rows)


def reset() -> None:
    db.execute("""UPDATE spot_check_item SET review_verdict = '', reviewer = '', review_ts = NULL,
                         calibration_eligible = false, state = 'pending'
                   WHERE job_id = 'hr_admin'""")
    n = db.fetch_one("""SELECT count(*) AS n FROM spot_check_item WHERE job_id = 'hr_admin'""")
    print(f"\n[复位] hr_admin 抽检行全部回到未复核：{n['n']} 行（合成标注不留在生产库）")


def state(tag: str) -> None:
    r = db.fetch_one("""SELECT count(*) AS n, count(*) FILTER (WHERE review_verdict <> '') AS reviewed,
                               count(*) FILTER (WHERE calibration_eligible) AS elig
                          FROM spot_check_item WHERE job_id = 'hr_admin'""")
    print(f"[{tag}] 抽检行 {r['n']} · 已复核 {r['reviewed']} · eligible=true {r['elig']}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="演练：灌合成复核结论 → 回灌 → 入口 → dry-run → 复位")
    ap.add_argument("--leak-budget", default=LEAK_BUDGET,
                    help="漏放预算；默认 0.01（严格）。要看到 apply 分支用 0.7")
    ap.add_argument("--reviews", default=REVIEWS,
                    help="合成复核结论 CSV（application_id/review_verdict/reviewer/review_ts）")
    _a = ap.parse_args()
    LEAK_BUDGET = _a.leak_budget
    reset()                            # 从干净状态起（上一次演练可能留了合成结论）
    print("=" * 78)
    print(f"演练（合成复核结论，**跑完复位**）· 校准样本入口：选项 A · {_a.reviews} · 漏放预算 {LEAK_BUDGET}")
    print("=" * 78)
    state("起点")
    n = apply_demo(_a.reviews)
    print(f"\n[演练] 灌入合成复核结论 {n} 条（{_a.reviews}）")
    state("灌入后")
    sh(PY, "tools/calibration_backfill.py", "--apply")
    sh(PY, "-m", "app.calibrate", "pool", "--job", JOBS[0])
    sh(PY, "-m", "app.calibrate", "run", "--job", JOBS[0], "--dry-run", "--leak-budget", LEAK_BUDGET)
    reset()
    state("复位后")
    sh(PY, "tools/calibration_backfill.py")
