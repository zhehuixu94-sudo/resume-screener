# -*- coding: utf-8 -*-
"""口径校准的纯逻辑测试：挑线规则、版本号、配置文件就地更新与备份。

挑线规则是「安全优先」写死的：漏放优先 → 误放 → 人工负担 → 线更低者优先。
这条规则决定了系统会不会把该面的人自动淘汰，必须有测试盯着。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.calibrate import _version_plus1, choose_line, config_patch      # noqa: E402


def test_choose_line_prefers_no_leak() -> None:
    counts = [
        {"line": 60.0, "leak": 2, "wrong": 0, "manual": 10},
        {"line": 50.0, "leak": 0, "wrong": 3, "manual": 40},
        {"line": 45.0, "leak": 0, "wrong": 0, "manual": 60},
    ]
    assert choose_line(counts)["line"] == 45.0, "先把漏放压到 0，其次才是误放与人工负担"


def test_choose_line_then_fewer_wrong_then_less_manual() -> None:
    counts = [
        {"line": 40.0, "leak": 0, "wrong": 1, "manual": 5},
        {"line": 42.0, "leak": 0, "wrong": 0, "manual": 30},
        {"line": 44.0, "leak": 0, "wrong": 0, "manual": 20},
    ]
    assert choose_line(counts)["line"] == 44.0, "同为 0 漏放 0 误放时，选人工负担小的那条"


def test_choose_line_tie_prefers_higher_line() -> None:
    """三项全平时取更高的线（保守方向）：反过来会一路滑向"人人自动达标"。"""
    counts = [{"line": 12.0, "leak": 0, "wrong": 0, "manual": 8},
              {"line": 34.0, "leak": 0, "wrong": 0, "manual": 8},
              {"line": 26.0, "leak": 0, "wrong": 0, "manual": 8}]
    assert choose_line(counts)["line"] == 34.0


def test_choose_line_empty() -> None:
    assert choose_line([]) is None


def test_version_plus1() -> None:
    assert _version_plus1("3") == "4"
    assert _version_plus1("v9") == "v9-cal1", "非数字版本号不能崩，要退化成可读的派生版本"


def test_config_patch_keeps_other_keys_and_backs_up() -> None:
    cfg = {"job_id": "j", "config_version": "1", "job_title": "标题",
           "scoring": {"pass_line": 60, "band_width": 5, "weights": {"a": 50, "b": 50}},
           "dimensions": [{"id": "d1"}]}
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "job_x.json")
        json.dump(cfg, open(path, "w", encoding="utf-8"), ensure_ascii=False)
        out = config_patch(path, pass_line=47.5, config_version="2", source="自动校准 测试")
        now = json.load(open(path, encoding="utf-8"))
        assert now["config_version"] == "2" and now["scoring"]["pass_line"] == 47.5
        assert now["scoring"]["weights"] == {"a": 50, "b": 50}, "只动必要字段，别把配置写丢"
        assert now["dimensions"] == [{"id": "d1"}]
        assert out["before"]["config_version"] == "1", "快照要能还原到改前"
        assert os.path.exists(out["backup"]), "旧版本必须备份（回滚/对照都靠它）"
