# -*- coding: utf-8 -*-
"""稳定性闸门：**非对称**地看"翻转"——往保守侧翻和往危险侧翻，代价完全不对称。

为什么改（2026-09-15 实测，4 份 × 3 次重复跑）：
    分数已经锁死（极差 0.0），但"判定一致率"仍是 0.67，于是闸门把系统整个拦住。
    翻开明细一看，两次翻转全是往保守侧：`达标/达标/边缘`（模型第三次改口）→ 转人工。
    把这两种翻转算进同一个指标，等于**因为"多让人看一眼"而拒绝自动化**——方向错了。

初筛是不对称问题：**漏放一个合格候选人的代价，远大于让 HR 多看一眼边缘简历**。所以闸门拆成两条：

    危险侧翻转率（必须 0）  ：某次跑出 auto_reject / 不达标，而多数票是 auto_pass / 边缘
                              —— 这正是"随机刷人"的前兆，一次都不许有
    安全侧翻转率（宽容 <30%）：多数票 auto_pass，但至少一次跑成 边缘/转人工
                              —— 代价是 HR 多看一眼，不放行反而会白烧人工

放行条件：**危险侧 = 0 且 分数极差 ≤ 5**。安全侧只作为告警随记录一起报出来。

另外：冷启动阶段**默认不做自动淘汰**（`allow_auto_reject=False`）——没有真实人工反馈之前，
"转人工"就是最好的"淘汰"；系统定位为辅助筛选：自动过掉最确定的一批，其余按优先级排序交给人。
"""
from __future__ import annotations

from ..contracts.evaluation import Conflict
from ..store import db
from .rules import DecisionOutcome

MAX_DANGER_FLIP = 0.0     # 危险侧翻转率上限：0（一次都不许）
MAX_SAFE_FLIP = 0.30      # 安全侧翻转率上限：30%（只告警，不硬拦）
MAX_SCORE_GAP = 5.0       # 分数极差上限（沿用本来的验收标准 ACCEPT_GAP=5）
MIN_SAMPLES = 3           # 纳入统计的简历份数下限
METHOD = "repeat_label"   # 测量方式：同一份解析重复评估，看决策翻转
REJECT_SIDES = ("auto_reject",)


def latest(job_id: str, config_version: str, *, company_id: str = "local") -> dict | None:
    """取该配置版本下最近一次稳定性测量记录（没有就返回 None = 没测过）。"""
    return db.fetch_one(
        """SELECT check_id, job_id, config_version, method, n_samples, agreement,
                  score_range, danger_flip_rate, safe_flip_rate, detail, created_at
             FROM stability_check
            WHERE company_id = %s AND job_id = %s AND config_version = %s
            ORDER BY created_at DESC LIMIT 1""",
        (company_id, job_id, config_version))


def flip_metrics(samples: list[dict]) -> dict:
    """从每个样本的多次跑结果里数两条翻转率。

    samples = [{"decisions": ["auto_pass", "manual_review", "manual_review"],
                "verdicts": ["达标", "边缘", "边缘"]}, ...]（每个元素 = 一份简历的 N 次重复跑）

    多数票 = 该样本 N 次里出现最多的那个决策（并列取更保守的那个：auto_reject > manual_review > auto_pass）。
    """
    order = {"auto_pass": 0, "manual_review": 1, "auto_reject": 2}
    danger = safe = 0
    detail = []
    for s in samples:
        decs = [d for d in (s.get("decisions") or []) if d]
        verds = [v for v in (s.get("verdicts") or []) if v]
        if not decs:
            continue
        n = len(decs)
        counts = {d: decs.count(d) for d in set(decs)}
        top = max(counts.values())
        majority = max((d for d, c in counts.items() if c == top), key=lambda d: order.get(d, 1))
        # 危险侧：**有**一次跑出淘汰/不达标，而淘汰并不是严格多数（>半数）→ 随机刷人的前兆。
        # 注意用"严格多数"而不是并列时的保守票：1 淘汰 / 2 转人工 这种必须算危险。
        rej = sum(1 for d in decs if d == "auto_reject")
        nd = sum(1 for v in verds if v == "不达标")
        d_risk = (rej > 0 and rej * 2 <= n) or (nd > 0 and nd * 2 <= max(1, len(verds)))
        # 安全侧：多数票是自动达标，但至少一次被压成转人工（代价 = HR 多看一眼）
        s_risk = majority == "auto_pass" and any(dec != "auto_pass" for dec in decs)
        danger += int(bool(d_risk))
        safe += int(bool(s_risk))
        detail.append({"resume": s.get("resume"), "majority": majority, "decisions": decs,
                       "danger": bool(d_risk), "safe": bool(s_risk)})
    n = max(1, len(detail))
    return {"n_samples": len(detail), "danger_flip_rate": round(danger / n, 4),
            "safe_flip_rate": round(safe / n, 4), "per_resume": detail}


def is_ok(rec: dict | None, *, max_danger: float = MAX_DANGER_FLIP, max_safe: float = MAX_SAFE_FLIP,
          max_gap: float = MAX_SCORE_GAP, min_samples: int = MIN_SAMPLES) -> tuple[bool, str]:
    """闸门放不放行 + 一句人话原因（判据：危险侧=0 且 分数极差≤5）。"""
    if not rec:
        return False, (f"没有 {METHOD} 稳定性记录（同一输入重复跑的两侧翻转率未测）→ 不许自动处置。"
                       f"先跑 tools/stability_check.py --record")
    if int(rec.get("n_samples") or 0) < min_samples:
        return False, f"稳定性样本只有 {rec.get('n_samples')} 份（要求 ≥{min_samples}）→ 不许自动处置"
    if rec.get("danger_flip_rate") is None or rec.get("safe_flip_rate") is None:
        return False, ("旧稳定性记录没有『危险侧/安全侧翻转率』（只有笼统的一致率）→ 判据无法评估，"
                       "重跑 tools/stability_check.py --record 换一条新记录")
    gap = float(rec.get("score_range") or 0.0)
    danger = float(rec.get("danger_flip_rate") or 0.0)
    safe = float(rec.get("safe_flip_rate") or 0.0)
    if danger > max_danger:
        return False, (f"危险侧翻转率 {danger:.0%} > {max_danger:.0%}"
                       f"（有简历重复跑时从严翻到『淘汰/不达标』——随机刷人的前兆）→ 不许自动处置")
    if gap > max_gap:
        return False, f"分数极差 {gap:.1f} > {max_gap:.1f}（分数会漂，线附近的人会被随机处置）→ 不许自动处置"
    warn = f"；安全侧翻转率 {safe:.0%}" + ("（≥30%，建议人工扫一眼这批）" if safe >= max_safe else "")
    return True, f"稳定性通过（危险侧 {danger:.0%} · 极差 {gap:.1f} · 样本 {rec['n_samples']} 份{warn}）"


def apply_gate(outcome: DecisionOutcome, rec: dict | None, *,
               job_id: str = "", config_version: str = "",
               allow_auto_reject: bool = False) -> DecisionOutcome:
    """闸门作用在判定结果上：

    · **自动淘汰**：冷启动默认关闭（`allow_auto_reject=False`）；开启时还要求稳定性通过。
      没有真实人工反馈之前，"转人工"就是最好的"淘汰"——一票把人刷掉且无人复核是不可逆的。
    · **自动达标**：需要稳定性通过（危险侧=0 且 极差≤5）。安全侧翻转只告警。
    只降不升（和门槛豁免同一条纪律）。
    """
    if outcome.decision == "auto_reject":
        if not allow_auto_reject:
            outcome.decision = "manual_review"
            outcome.conflicts.append(Conflict(
                type="stability_gate",
                detail="冷启动：暂不自动淘汰（系统定位为辅助筛选，先积累人工复核反馈）→ 转人工"))
            return outcome
        ok, why = is_ok(rec)
        if ok:
            return outcome
        outcome.decision = "manual_review"
        outcome.conflicts.append(Conflict(type="stability_gate",
                                          detail=f"{why}（岗位 {job_id}@{config_version}）"))
        return outcome

    if outcome.decision == "auto_pass":
        ok, why = is_ok(rec)
        if not ok:
            outcome.decision = "manual_review"
            outcome.verdict = "边缘"
            outcome.conflicts.append(Conflict(type="stability_gate",
                                              detail=f"自动达标被拦：{why}（岗位 {job_id}@{config_version}）"))
        return outcome
    return outcome


def record(job_id: str, config_version: str, *, n_samples: int, agreement: float,
           score_range: float = 0.0, danger_flip_rate: float | None = None,
           safe_flip_rate: float | None = None, detail: dict | None = None,
           method: str = METHOD, company_id: str = "local") -> int:
    """写一条稳定性测量记录（供 tools/stability_check.py 落库）。"""
    import json

    row = db.execute(
        """INSERT INTO stability_check (company_id, job_id, config_version, method, n_samples,
                                        agreement, score_range, danger_flip_rate, safe_flip_rate, detail)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING check_id""",
        (company_id, job_id, config_version, method, n_samples, agreement, score_range,
         danger_flip_rate, safe_flip_rate, json.dumps(detail or {}, ensure_ascii=False)),
        returning=True)
    return int((row or {}).get("check_id", 0))


def status_text(job_id: str, config_version: str) -> str:
    rec = latest(job_id, config_version)
    ok, why = is_ok(rec)
    return (f"[{'自动达标放行' if ok else '拦截'}] 稳定性闸门：{why}"
            f"；自动淘汰：冷启动默认关闭（apply_gate allow_auto_reject=False）")
