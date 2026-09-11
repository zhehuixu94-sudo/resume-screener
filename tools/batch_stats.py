# -*- coding: utf-8 -*-
"""一次性统计：把批次结果汇总出来（跑完批处理看效果用）。"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from collections import Counter
from app.store import db

rows = db.fetch_all(
    """SELECT r.file_name, e.verdict, e.decision, e.base_total, e.confidence, a.attempts, a.state
         FROM application a
         JOIN resume r ON r.resume_id = a.resume_id
         LEFT JOIN evaluation e ON e.application_id = a.application_id
        ORDER BY e.base_total DESC NULLS LAST""")
print(f"{'简历':<26} {'判定':<5} {'处置':<14} {'规则分':>7}  置信度  尝试")
for r in rows:
    total = f"{r['base_total']:.1f}" if r["base_total"] is not None else "-"
    print(f"{r['file_name']:<26} {r['verdict'] or '-':<5} {r['decision'] or '-':<14} {total:>7}  "
          f"{r['confidence'] if r['confidence'] is not None else '-':<6} {r['attempts']}")

print("\n处置分布:", dict(Counter(r["decision"] or "未完成" for r in rows)))
print("判定分布:", dict(Counter(r["verdict"] or "未完成" for r in rows)))
scores = [r["base_total"] for r in rows if r["base_total"] is not None]
print(f"规则分：min {min(scores):.1f} / max {max(scores):.1f} / 均值 {sum(scores)/len(scores):.1f}")
print("证据 span：", db.fetch_one(
    "SELECT count(*) AS total, sum(CASE WHEN anchored THEN 1 ELSE 0 END) AS anchored FROM evidence_span"))
print("审计日志条数:", db.fetch_one("SELECT count(*) AS n FROM audit_log")["n"])
