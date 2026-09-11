# -*- coding: utf-8 -*-
"""截断 JSON 修复的回归测试（用 100 份跑批里真实抓到的截断样本）。"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.llm.structured import repair_truncated_json  # noqa: E402

# resume_026 那次失败的真实片段：reason 写到中文左引号处被切断
REAL_TRUNCATED = (
    '{\n'
    '  "verdict": "边缘",\n'
    '  "closest_anchor": "边缘",\n'
    '  "confidence": 0.85,\n'
    '  "veto_triggered": null,\n'
    '  "veto_filter_ids": [],\n'
    '  "reason": "候选人具备C#基础与Unity独立开发Demo经历，能实现基础游戏逻辑，符合边缘档中“跟着教程做过demo'
)


def test_真实截断样本能修复并保住已完整字段():
    d = repair_truncated_json(REAL_TRUNCATED)
    assert d is not None, "被截断的 JSON 应该能补齐结尾后解析出来"
    assert d["verdict"] == "边缘"
    assert d["confidence"] == 0.85
    assert d["veto_triggered"] is None
    assert d["veto_filter_ids"] == []
    assert isinstance(d["reason"], str) and d["reason"].startswith("候选人具备C#基础")


def test_截断在嵌套结构里也能补():
    t = '{"a": 1, "strengths": ["[3] 优点", "[5] 另一个'
    d = repair_truncated_json(t)
    assert d is not None and d["a"] == 1 and isinstance(d["strengths"], list)


def test_合法输出不需要修复():
    """契约：这个函数只在解析失败时被调用；对完整 JSON 返回 None（"无需修复"）。
    返回 None 而不是原样回吐，是为了让调用方一眼看出"这是修过的"还是"本来就好的"。"""
    ok = '{"verdict": "达标", "confidence": 0.9}'
    assert repair_truncated_json(ok) is None, "完整 JSON 不该被当成截断件修复"
    assert json.loads(ok)["verdict"] == "达标"


def test_不是对象或彻底坏掉时返回空():
    assert repair_truncated_json("[1, 2, 3") is None, "顶层不是对象就别硬修（契约要求对象）"
    assert repair_truncated_json("完全不是 JSON 的一段话") is None
    assert repair_truncated_json("") is None


def test_转义引号不会被误判成未闭合():
    t = '{"reason": "他说\\"完成了\\"，还差一个引号'
    d = repair_truncated_json(t)
    assert d is not None and "完成了" in d["reason"]
