# -*- coding: utf-8 -*-
"""整体判定的多数票合并（`compare_repeats`）。

实测背景：分数已经锁死（同一份简历三次极差 0.0），但模型的「整体判定」仍在达标/边缘之间跳：
    admin_001  三次条目数 22/22/22、判定分 76.9/76.5/77.3（极差 0.0）
               但 verdict 是 达标/边缘/边缘 → 处置 auto_pass/manual_review/manual_review
    → 稳定性一致率 67%，闸门（≥0.8）过不去。分数是代码算的，判定是模型说的，只能用多数票压。
"""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.pipeline.evaluate import _merge_compare_runs            # noqa: E402


def _run(verdict: str, conf: float, trace: str = "ti", ok: bool = True):
    return SimpleNamespace(obj=(SimpleNamespace(verdict=verdict, confidence=conf) if ok else None),
                           meta=SimpleNamespace(trace_ids=[trace], validation=None))


def test_多数票压住单次翻转() -> None:
    merged = _merge_compare_runs([_run("达标", 0.95), _run("边缘", 0.95), _run("边缘", 0.95)])
    assert merged.obj.verdict == "边缘", "3 次里 2 次边缘，多数票必须取边缘"


def test_全部一致时保持原判定() -> None:
    merged = _merge_compare_runs([_run("达标", 0.9), _run("达标", 0.95), _run("达标", 0.8)])
    assert merged.obj.verdict == "达标"
    assert merged.obj.confidence == 0.9, "应取与中位置信度最接近的那一轮（0.9）"


def test_合并结果确定() -> None:
    runs = [_run("达标", 0.9, "a"), _run("边缘", 0.9, "b"), _run("不达标", 0.9, "c")]
    first = _merge_compare_runs(runs).obj.verdict
    assert all(_merge_compare_runs(runs).obj.verdict == first for _ in range(5)), \
        "同一批数据每次合并结果必须一致（否则等于随机发判定）"


def test_有效轮次不足一半时按失败处理() -> None:
    runs = [_run("达标", 0.9), _run("边缘", 0.9, ok=False), _run("达标", 0.9, ok=False)]
    merged = _merge_compare_runs(runs)
    assert merged.obj is None, "只有 1/3 轮有效，不许拿少数派当结论 → 交给降级路径转人工"
