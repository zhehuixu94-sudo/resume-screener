# -*- coding: utf-8 -*-
"""豁免与综合达标的回归测试：单项不满足但整体实力强 → 不自动淘汰。

对应用户给的真实口径："有的人四级没过但是 ACM 金牌，这种情况刷下去就很不合理"。
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.contracts import JobConfig  # noqa: E402
from app.pipeline.hard_filters import check_hard_filters, veto_summary  # noqa: E402
from app.pipeline.rules import decide  # noqa: E402
from tests.test_contracts import _cmp, _parse_stub  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _teacher_cfg() -> JobConfig:
    return JobConfig.model_validate(
        json.load(open(os.path.join(ROOT, "config", "job_jushi_teacher.json"), encoding="utf-8")))


def test_四级不再是硬门槛():
    cfg = _teacher_cfg()
    assert "cet4" not in [f.id for f in cfg.hard_filters], "四级已降为维度/加分项，不该再一票否决"
    assert "english" in cfg.dimension_ids, "英语能力应当作为一个维度参与综合评分"


def test_专科但拿过ACM金牌_不自动淘汰():
    """核心场景：学历不满足，但竞赛维度足够强 → 转人工综合判断。"""
    cfg = _teacher_cfg()
    text = ("大专 某某职业技术学院 计算机应用技术 2021.9-2024.6 "
            "ACM/ICPC 区域赛银牌，蓝桥杯省赛一等奖，独立实现过 Dijkstra 与线段树模板库")
    parse = _parse_stub(text, [("b1.i1", "ACM/ICPC 区域赛银牌"), ("b1.i2", "蓝桥杯省赛一等奖")])
    hard = check_hard_filters(cfg, parse, None)
    fails, _unknown, _passes = veto_summary(hard)
    assert "edu" in fails, "大专应当判为学历门槛不满足"

    # 竞赛维度拿高分（30 分制里 27 分）→ 命中豁免
    out = decide(cfg, base_total=52.0, compare=_cmp("边缘", 0.9), fit=None,
                 stage_status={"label": "ok", "compare": "ok", "fit": "ok"}, anchored_span_ids=set(),
                 hard_filters=hard, dim_points={"contest": 27.0, "coding": 12.0}, bonus_names=set())
    assert out.decision == "manual_review", "触发学历门槛但竞赛极强 → 必须转人工，不许自动淘汰"
    assert "veto_override" in [c.type for c in out.conflicts]
    assert "豁免" in out.reason or "综合" in out.reason


def test_竞赛不强时门槛照旧淘汰():
    """豁免不能滥用：竞赛维度不够高，学历不满足就该自动淘汰。"""
    cfg = _teacher_cfg()
    text = "大专 某某职业技术学院 计算机应用技术"
    parse = _parse_stub(text, [("b1.i1", "计算机应用技术")])
    hard = check_hard_filters(cfg, parse, None)
    out = decide(cfg, base_total=18.0, compare=_cmp("不达标", 0.9), fit=None,
                 stage_status={"label": "ok", "compare": "ok", "fit": "ok"}, anchored_span_ids=set(),
                 hard_filters=hard, dim_points={"contest": 6.6, "coding": 5.5}, bonus_names=set())
    assert out.decision == "auto_reject"
    assert "veto_triggered" in [c.type for c in out.conflicts]


def test_豁免只降级不升级():
    """豁免绝不能让人自动达标。"""
    cfg = _teacher_cfg()
    parse = _parse_stub("大专 计算机应用技术 ACM 金牌", [("b1.i1", "ACM 金牌")])
    hard = check_hard_filters(cfg, parse, None)
    out = decide(cfg, base_total=95.0, compare=_cmp("达标", 0.99), fit=None,
                 stage_status={"label": "ok", "compare": "ok", "fit": "ok"}, anchored_span_ids=set(),
                 hard_filters=hard, dim_points={"contest": 30.0}, bonus_names={"竞赛获奖（省级及以上）"})
    assert out.decision == "manual_review" and out.verdict != "达标"


def test_豁免规则必须引用存在的维度():
    import pytest
    from pydantic import ValidationError
    cfg = json.load(open(os.path.join(ROOT, "config", "job_jushi_teacher.json"), encoding="utf-8"))
    cfg["overrides"][0]["when_dimension"] = "not_a_dimension"
    with pytest.raises(ValidationError):
        JobConfig.model_validate(cfg)
