# -*- coding: utf-8 -*-
"""稳定性闸门的测试：没测过/不达标 → 不许自动淘汰（只降不升）。

为什么必须有它：实测同一份简历重复跑会落在达标线两侧（单跑 66.1 判达标、批量跑 63.9 判边缘），
拿会漂的分数去自动淘汰人等于随机刷人。闸门是这条纪律的代码化，坏了就等于没有。
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.pipeline.rules import DecisionOutcome                  # noqa: E402
from app.pipeline.stability import MIN_AGREEMENT, apply_gate, is_ok    # noqa: E402


def outcome(decision: str = "auto_reject") -> DecisionOutcome:
    return DecisionOutcome(verdict="不达标", decision=decision, reason="规则分低于线−band", conflicts=[])


def test_no_record_blocks_auto_reject() -> None:
    o = apply_gate(outcome(), None, job_id="j", config_version="1")
    assert o.decision == "manual_review", "没测过稳定性就不许自动淘汰"
    assert any(c.type == "stability_gate" for c in o.conflicts), "降级原因要留痕（报告/审计要看）"
    assert "stability_check" in o.conflicts[-1].detail or "稳定性" in o.conflicts[-1].detail


def test_low_agreement_blocks() -> None:
    rec = {"n_samples": 5, "agreement": MIN_AGREEMENT - 0.1}
    assert apply_gate(outcome(), rec).decision == "manual_review"


def test_too_few_samples_blocks() -> None:
    rec = {"n_samples": 1, "agreement": 1.0}
    assert apply_gate(outcome(), rec).decision == "manual_review"


def test_good_record_allows_and_other_decisions_untouched() -> None:
    rec = {"n_samples": 5, "agreement": 0.9}
    assert apply_gate(outcome(), rec).decision == "auto_reject", "达标了就该放行"
    for d in ("manual_review", "auto_pass"):
        assert apply_gate(outcome(d), None).decision == d, "闸门只降不升，也不动别的判定"


def test_is_ok_messages_are_actionable() -> None:
    ok, why = is_ok(None)
    assert not ok and "stability_check.py" in why, "拦截原因要告诉人怎么放行"
    ok, why = is_ok({"n_samples": 4, "agreement": 0.85})
    assert ok and "0.85" in why


@pytest.mark.skipif(not os.environ.get("PGPASSWORD") and not os.path.exists(r"D:\Hermes\secrets\openclaw.env"),
                    reason="没有数据库凭据")
def test_record_and_latest_roundtrip() -> None:
    """写一条测量记录，latest() 要能读回来（闸门的放行依据就靠它）。"""
    from app.pipeline import stability
    from app.store import db

    job_id = "test_stability_gate"
    db.execute("DELETE FROM stability_check WHERE job_id = %s", (job_id,))
    try:
        cid = stability.record(job_id, "1", n_samples=4, agreement=0.75, score_range=3.2,
                               detail={"note": "测试"})
        rec = stability.latest(job_id, "1")
        assert rec and rec["check_id"] == cid and abs(float(rec["agreement"]) - 0.75) < 1e-6
        ok, _ = stability.is_ok(rec)
        assert not ok, "0.75 < 0.8 应拦截"
        stability.record(job_id, "1", n_samples=4, agreement=0.95)
        ok, why = stability.is_ok(stability.latest(job_id, "1"))
        assert ok, why
    finally:
        db.execute("DELETE FROM stability_check WHERE job_id = %s", (job_id,))
