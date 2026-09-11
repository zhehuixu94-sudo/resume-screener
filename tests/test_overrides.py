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


def test_硬门槛引用的证据必须进cited_evidence():
    """回归：100 份跑批时出现过『硬门槛 major 引用了不在 cited_evidence 里的 ev_0001』，
    因为硬门槛命中的证据若不被任何能力项引用，就不会被并进 cited_evidence，契约校验随即失败。"""
    import json as _json
    from app.contracts import JobConfig
    from app.pipeline.hard_filters import check_hard_filters
    cfg = JobConfig.model_validate(_json.load(open(os.path.join(ROOT, "config", "job_game_dev.json"),
                                                  encoding="utf-8")))
    # 证据里只有一条『数字媒体技术』（专业关键词），且它不会被任何维度条目引用
    parse = _parse_stub("教育经历 某学院 数字媒体技术 2021.9-2025.6", [("b1.i1", "数字媒体技术")])
    res = check_hard_filters(cfg, parse, None)
    major = next(r for r in res if r.id == "major")
    assert major.result == "pass" and major.evidence_span_ids == ["ev_0001"]

    # 用最小评估对象验证契约：cited_evidence 含该 span 时通过，不含时被拒
    from app.contracts import Evaluation
    import copy
    from tests.test_contracts import _cfg
    base = _json.load(open(os.path.join(ROOT, "out", "_eval_fixture.json"), encoding="utf-8")) \
        if os.path.exists(os.path.join(ROOT, "out", "_eval_fixture.json")) else None
    if base is None:
        return          # 没有 fixture 时只验证规则层那一半（fixture 由 validate_contracts 生成）
    ev = Evaluation.model_validate(base)
    bad = copy.deepcopy(base)
    bad["hard_filters"][0]["evidence_span_ids"] = ["ev_9999"]
    try:
        Evaluation.model_validate(bad)
        raise AssertionError("引用了不存在的 span 竟然通过了契约校验")
    except Exception as e:
        assert "ev_9999" in str(e)


def test_面试题跳过只信规则层():
    """回归：100 份跑批时模型有 6 次自称"触发硬性门槛"（借口是专业不对口），
    而规则层对 100 份全部判定 major=pass —— 采信模型自述会白吞这些人的面试题。"""
    from app.contracts import HardFilterResult
    from app.pipeline.evaluate import questions_skip_reason

    ok = [HardFilterResult(id="major", name="专业对口", rule="计算机类相关专业", veto=True,
                           result="pass", reason="简历全文命中：软件工程", evidence_span_ids=["ev_0001"])]
    reason, notes = questions_skip_reason(ok, {"veto_triggered": "候选人专业不对口", "veto_filter_ids": ["major"]})
    assert reason == "", "规则层没触发门槛，却因模型自述跳过了面试题"
    assert any("模型自称" in n for n in notes), "模型自述与规则层结论冲突时必须留痕"

    bad = [HardFilterResult(id="major", name="专业对口", rule="计算机类相关专业", veto=True,
                            result="fail", reason="简历全文未命中任一关键词", evidence_span_ids=[])]
    reason2, _ = questions_skip_reason(bad, None)
    assert "major" in reason2, "规则层真触发时必须跳过，且理由要写规则层的结论"
