# -*- coding: utf-8 -*-
"""流水线接线冒烟测试：用假客户端把 evaluate() 整条链路跑一遍，不打网络、不花 token。

为什么需要它：实测踩过一次——新增"按维度定向取证"后，`pool`（证据池）定义在加分阶段之后，
每次评估都抛 UnboundLocalError，全量 100 份跑了 54 次失败才被发现。
接线类错误（变量顺序、字段名、契约不一致）不该靠花钱跑批来发现。
"""
from __future__ import annotations

import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.contracts import JobConfig                       # noqa: E402
from app.llm.client import ChatResult                     # noqa: E402
from app.pipeline.evaluate import evaluate                # noqa: E402
from tests.test_contracts import _parse_stub              # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESUME_TEXT = ("教育背景 示例大学 计算机科学与技术 本科 2022.9-2026.6\n"
               "项目经历 独立开发 Unity 2D 平台跳跃游戏，实现角色移动、碰撞检测与三个关卡\n"
               "竞赛获奖 校级程序设计竞赛三等奖\n"
               "技能 熟悉 C# 与 Git，了解数据结构与算法")


class FakeClient:
    """按提示词角色分发固定输出，模拟五段调用；用于验证接线而不是验证模型。"""

    model = "fake-model"

    def __init__(self, text: str, dims: list[str]):
        self.text = text
        self.dims = dims
        self.calls: list[str] = []

    def chat(self, system: str, user: str, **kw) -> ChatResult:      # noqa: ANN003
        if "简历证据检索员" in system:                     # cover
            self.calls.append("cover")
            return self._res(json.dumps({"dimensions": [
                {"id": d, "found": i == 0, "note": "" if i == 0 else "简历里没有该维度内容",
                 "items": ([{"capability": "Unity 独立开发", "evidence": "独立开发 Unity 2D 平台跳跃游戏"}]
                           if i == 0 else [])}
                for i, d in enumerate(self.dims)]}, ensure_ascii=False))
        if "招聘评估助手" in system:                       # label
            self.calls.append("label")
            n = len(re.findall(r'"idx":\s*\d+', user))
            items = [{"idx": i, "block": "b1", "capability": f"能力项{i}", "evidence": "独立开发 Unity 2D 平台跳跃游戏",
                      "relevance": "中", "difficulty": "中", "dimension": self.dims[0],
                      "reason": "有证据", "confidence": 0.8} for i in range(1, n + 1)]
            return self._res(json.dumps({"items": items}, ensure_ascii=False))
        if "资深招聘评审" in system:                       # compare
            self.calls.append("compare")
            return self._res(json.dumps({"verdict": "边缘", "closest_anchor": "边缘", "confidence": 0.8,
                                         "veto_triggered": None, "veto_filter_ids": [],
                                         "reason": "与锚点比较偏边缘", "strengths": ["[1] 有项目"],
                                         "gaps": ["[2] 缺深度"]}, ensure_ascii=False))
        if "匹配度分析员" in system:                       # fit
            self.calls.append("fit")
            return self._res(json.dumps({"verdict": "部分对口", "direction": "偏 Web 前端",
                                         "matched": [{"item": "C#", "why": "对口"}], "partial": [],
                                         "unmatched": [], "fit_ratio": 0.5}, ensure_ascii=False))
        self.calls.append("questions")
        return self._res(json.dumps({"questions": [
            {"question": "讲讲你的存档模块怎么设计的", "level": "深度", "difficulty": "中",
             "target": "[1]", "intent": "验证实现细节", "red_flag": "只会背概念"}]}, ensure_ascii=False))

    def _res(self, text: str) -> ChatResult:
        return ChatResult(text=text, usage={}, elapsed_s=0.01, trace_id="ti_fake", model=self.model)


def _cfg() -> JobConfig:
    return JobConfig.model_validate(
        json.load(open(os.path.join(ROOT, "config", "job_game_dev.json"), encoding="utf-8")))


def test_整条流水线能跑通且覆盖结论落进评估():
    cfg = _cfg()
    dims = [d.id for d in cfg.dimensions]
    parse = _parse_stub(RESUME_TEXT, [("b1.i1", "独立开发 Unity 2D 平台跳跃游戏"),
                                      ("b1.i2", "校级程序设计竞赛三等奖")])
    client = FakeClient(RESUME_TEXT, dims)
    ev, notes = evaluate(parse, cfg, client)            # type: ignore[arg-type]

    assert ev.coverage, "定向取证的结论没有落进评估结果"
    assert [c.id for c in ev.coverage] == dims, "覆盖结论必须覆盖岗位的每个维度，且顺序一致"
    assert len([c for c in ev.coverage if c.found]) == 1, "假客户端只给了第一个维度证据"
    assert {"cover", "label", "compare", "fit", "questions"} <= set(client.calls), \
        f"五段调用没有全部发生：{sorted(set(client.calls))}"
    assert ev.dimensions and ev.base_total >= 0
    assert ev.stages.cover is not None, "stages 里应记录取证段"


def test_取证条目进了标注池并带上证据():
    cfg = _cfg()
    dims = [d.id for d in cfg.dimensions]
    parse = _parse_stub(RESUME_TEXT, [("b1.i1", "独立开发 Unity 2D 平台跳跃游戏")])
    client = FakeClient(RESUME_TEXT, dims)
    ev, _ = evaluate(parse, cfg, client)                # type: ignore[arg-type]
    # 标注池 = 解析条目 + 取证条目：取证那一条带的是 evc_ 前缀的 span
    assert any(c.span_id.startswith("evc_") for c in ev.cited_evidence), \
        "取证阶段的证据没有进入引用池（报告里会点不开）"
    assert any(not c.anchored for c in ev.cited_evidence) is not None


def test_parse_coverage_excludes_achievements() -> None:
    """量化成就镜像进 evidence 后，能力项覆盖率不能因此 >1（曾经直接抛 ValidationError）。"""
    from app.contracts.resume_parse import ParseStats
    st = ParseStats(blocks=1, items=4, evidence_total=4, evidence_anchored=4, evidence_unanchored=0,
                    evidence_coverage=1.0, achievements=9, achievements_anchored=9,
                    achievements_evidence=9)
    assert st.evidence_coverage == 1.0 and st.evidence_total == 4      # 成就段不计入能力项分母
    assert st.achievements_evidence == 9
