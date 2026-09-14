# -*- coding: utf-8 -*-
"""稳定性闸门：稳定性没测过、或测下来不达标时，**不许自动淘汰**。

为什么要有这道闸门：实测同一份简历重复跑，单次标注的总分极差能到 10~16 分，同一份简历
单跑判"达标"、批量跑判"边缘"（66.1 vs 63.9）。也就是说分数在达标线附近是会漂的——
拿一个会漂的分数去自动淘汰人，等于随机刷人。所以规则是：

    自动淘汰（auto_reject）必须满足：同一 config_version 下有一份**合格的稳定性测量记录**
    （判定一致率 ≥ 阈值、样本数 ≥ 下限）；否则降级为转人工，并在 conflicts 里写明原因。

自动达标（auto_pass）不在这道闸门的拦截范围（它错在"多让人看一眼"，代价小），但要一起看数据。
"""
from __future__ import annotations

from ..contracts.evaluation import Conflict
from ..store import db
from .rules import DecisionOutcome

MIN_AGREEMENT = 0.8      # 判定一致率下限（同一输入重复跑）
MIN_SAMPLES = 3          # 纳入统计的简历份数下限（1 份的话说明不了问题）
METHOD = "repeat_label"  # 测量方式：同一份解析重复标注取中位数，看判定一致率


def latest(job_id: str, config_version: str, *, company_id: str = "local") -> dict | None:
    """取该配置版本下最近一次稳定性测量记录（没有就返回 None = 没测过）。"""
    return db.fetch_one(
        """SELECT check_id, job_id, config_version, method, n_samples, agreement,
                  score_range, detail, created_at
             FROM stability_check
            WHERE company_id = %s AND job_id = %s AND config_version = %s
            ORDER BY created_at DESC LIMIT 1""",
        (company_id, job_id, config_version))


def is_ok(rec: dict | None, *, min_agreement: float = MIN_AGREEMENT,
          min_samples: int = MIN_SAMPLES) -> tuple[bool, str]:
    """闸门放不放行 + 一句人话原因。"""
    if not rec:
        return False, (f"没有 {METHOD} 稳定性记录（同一输入重复跑的一致率未测）→ 不许自动淘汰。"
                       f"先跑 tools/stability_check.py --record")
    if int(rec.get("n_samples") or 0) < min_samples:
        return False, f"稳定性样本只有 {rec.get('n_samples')} 份（要求 ≥{min_samples}）→ 不许自动淘汰"
    if float(rec.get("agreement") or 0) < min_agreement:
        return False, (f"稳定性一致率 {float(rec.get('agreement') or 0):.2f} < {min_agreement}"
                       f"（判定会漂，实测同一份简历单跑/批量跑会落在达标线两侧）→ 不许自动淘汰")
    return True, f"稳定性已达标（一致率 {float(rec['agreement']):.2f}，样本 {rec['n_samples']} 份）"


def apply_gate(outcome: DecisionOutcome, rec: dict | None, *,
               job_id: str = "", config_version: str = "") -> DecisionOutcome:
    """把闸门作用在判定结果上：auto_reject 而稳定性不达标 → 降级为 manual_review。

    只降不升（和门槛豁免同一条纪律）：闸门永远不会把谁变成自动淘汰。
    就地改（与 sync_decisions_with_evidence 一致），返回同一个对象便于链式调用。
    """
    if outcome.decision != "auto_reject":
        return outcome
    ok, why = is_ok(rec)
    if ok:
        return outcome
    outcome.decision = "manual_review"
    outcome.conflicts.append(Conflict(type="stability_gate",
                                      detail=f"{why}（岗位 {job_id}@{config_version}）"))
    return outcome


def record(job_id: str, config_version: str, *, n_samples: int, agreement: float,
           score_range: float = 0.0, detail: dict | None = None, method: str = METHOD,
           company_id: str = "local") -> int:
    """写一条稳定性测量记录（供 tools/stability_check.py 落库）。"""
    import json

    row = db.execute(
        """INSERT INTO stability_check (company_id, job_id, config_version, method, n_samples,
                                        agreement, score_range, detail)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING check_id""",
        (company_id, job_id, config_version, method, n_samples, agreement, score_range,
         json.dumps(detail or {}, ensure_ascii=False)), returning=True)
    return int((row or {}).get("check_id", 0))


def status_text(job_id: str, config_version: str) -> str:
    rec = latest(job_id, config_version)
    ok, why = is_ok(rec)
    return f"[{'放行' if ok else '拦截'}] 自动淘汰闸门：{why}"
