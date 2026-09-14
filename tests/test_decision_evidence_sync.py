# -*- coding: utf-8 -*-
"""自动处置的证据侧安全兜底：`sync_decisions_with_evidence` 与契约必须逐字一致。

实测踩过的坑：某个维度的最强条目**锚定失败**（`span_id` 为空），兜底函数里 `if s` 把它跳过了，
契约却把它当"未锚定证据"→ 整份评估直接抛 ValidationError（跑批里表现为偶发"评估异常"，
稳定性复跑时撞上）。修法是两处口径统一：**空 span 与未锚定同等对待**。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.pipeline.rules import DecisionOutcome, sync_decisions_with_evidence   # noqa: E402


def _pass_outcome() -> DecisionOutcome:
    return DecisionOutcome(verdict="达标", decision="auto_pass", reason="整体达标且分数够")


def test_最强证据为空时不许自动达标() -> None:
    out = sync_decisions_with_evidence(_pass_outcome(), {"ev_0001": True}, ["ev_0001", ""])
    assert out.decision == "manual_review" and out.verdict == "边缘"
    assert any(c.type == "unanchored_evidence" for c in out.conflicts)


def test_最强证据未锚定时不许自动达标() -> None:
    out = sync_decisions_with_evidence(_pass_outcome(), {"ev_0001": False}, ["ev_0001"])
    assert out.decision == "manual_review"


def test_全部锚定时允许自动达标() -> None:
    out = sync_decisions_with_evidence(_pass_outcome(), {"ev_0001": True, "ev_0002": True},
                                       ["ev_0001", "ev_0002"])
    assert out.decision == "auto_pass"


def test_自动淘汰遇到未锚定证据也要降级() -> None:
    out = DecisionOutcome(verdict="不达标", decision="auto_reject", reason="整体不达标且分数低")
    out = sync_decisions_with_evidence(out, {"ev_0001": False}, [])
    assert out.decision == "manual_review" and out.verdict == "边缘"
