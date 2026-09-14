# -*- coding: utf-8 -*-
"""端到端接线测试：**判定必须吃新公式的分数**（这条盯住一次真实事故）。

事故：第四步实现了「基础项60 + 加分项40(本批分位) − 注水扣分」，评测脚本按新口径算出
「线 55 → 淘汰 61%」，但线上 `evaluate()` 仍把旧的 `rules.base_total` 交给 `decide()`，
新分数只是被写进 payload 的展示字段。结果线上淘汰率 **0%**、注水型 44 份并列同分、
空话率 0.889 的简历拿 72.3 分。测试全绿，生产全错。

所以这里断言的不是"分数算得对"（那由 test_weighted_total 管），而是
**"把分数换掉，处置必须跟着变"**——任何绕过 `score.gate_score()` 的改动都会被这条测试抓住。
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.contracts import JobConfig                                  # noqa: E402
from app.pipeline.evaluate import evaluate                            # noqa: E402
from app.pipeline.rules import decide                                 # noqa: E402
from tests.test_contracts import _parse_stub                          # noqa: E402
from tests.test_pipeline_wiring import RESUME_TEXT, FakeClient        # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONCRETE_SPANS = [("b1.i1", "负责20余台变压器的运行监控"),
                  ("b1.i2", "录入与分析数据3000余条"),
                  ("b1.i3", "平均故障响应时间缩短至30分钟内")]
FLUFF_SPANS = [("b1.i1", "熟悉办公软件，沟通协调能力强"),
               ("b1.i2", "有责任心，团队协作能力良好"),
               ("b1.i3", "学习能力强，积极主动，抗压能力好"),
               ("b1.i4", "综合素质优秀，熟练使用办公软件")]


class GateFakeClient(FakeClient):
    """比共用假客户端多两处：compare 的 reason 够长（契约要求 ≥10 字）、整体判定可指定。

    共用假客户端的 compare 输出 reason 只有 8 个字，会被契约判为 failed → 强制转人工，
    那样就测不出"线上到底用哪个分数判定"了。
    """

    def __init__(self, text: str, dims: list[str], verdict: str = "边缘"):
        super().__init__(text, dims)
        self.verdict = verdict

    def chat(self, system: str, user: str, **kw):                     # noqa: ANN003, ANN201
        res = super().chat(system, user, **kw)
        if "资深招聘评审" in system:
            payload = json.loads(res.text)
            payload["verdict"] = self.verdict
            payload["reason"] = "与锚点档位比较后整体落在边缘，证据不足以判达标"
            return res.__class__(text=json.dumps(payload, ensure_ascii=False), usage=res.usage,
                                 elapsed_s=res.elapsed_s, trace_id=res.trace_id, model=res.model)
        return res


def _cfg() -> JobConfig:
    return JobConfig.model_validate(
        json.load(open(os.path.join(ROOT, "config", "job_game_dev.json"), encoding="utf-8")))


def _eval(spans, cfg: JobConfig | None = None, verdict: str = "边缘"):
    cfg = cfg or _cfg()
    parse = _parse_stub(RESUME_TEXT, spans)
    client = GateFakeClient(RESUME_TEXT, [d.id for d in cfg.dimensions], verdict=verdict)
    ev, _ = evaluate(parse, cfg, client)                 # type: ignore[arg-type]
    return cfg, ev


def test_判定分已落库且与拆解一致() -> None:
    _, ev = _eval(CONCRETE_SPANS)
    assert ev.score_parts is not None, "判定分拆解没落库（报告将无法解释分数）"
    assert abs(ev.gate_score - ev.score_parts.total) < 0.01
    assert ev.scoring_snapshot.gate_score_field == "gate_score", \
        "复现参数必须说明判定用的是 gate_score，否则历史分数没法复现"
    p = ev.score_parts
    assert abs(p.base + p.bonus - p.penalty - p.total) < 0.02


def test_空话证据被扣分且判定分低于维度合计() -> None:
    _, concrete = _eval(CONCRETE_SPANS)
    _, fluff = _eval(FLUFF_SPANS)
    assert concrete.score_parts.penalty < 1.0, f"具体证据不该被扣分：{concrete.score_parts}"
    assert fluff.score_parts.penalty > 10.0, f"全空话必须实打实扣分：{fluff.score_parts}"
    assert fluff.gate_score < fluff.base_total, "判定分没有把扣分算进去（又挂成展示字段了）"


def test_换掉分数必须改变处置() -> None:
    """核心回归：**判定必须受 gate_score 影响**。

    构造一条达标线，使 `base_total` 在线以上、`gate_score` 在线以下：
      · 用 base_total 判 → 整体达标 + 分数够 = auto_pass
      · 用 gate_score 判 → 分数不够 = manual_review
    再断言线上产出的处置 == 用 gate_score 判出来的那个（即线上吃的确实是 gate_score）。
    """
    cfg, ev = _eval(FLUFF_SPANS, verdict="达标")
    assert ev.llm_compare is not None and ev.llm_compare.verdict == "达标", \
        f"假客户端没能给出整体达标，测试失去区分度：{ev.stages.compare.validation.errors}"
    assert abs(float(ev.base_total) - float(ev.gate_score)) > 5, \
        f"扣分没有拉开差距，测试没有意义：base {ev.base_total} / gate {ev.gate_score}"

    mid = (float(ev.base_total) + float(ev.gate_score)) / 2
    sc = cfg.scoring.model_copy(update={"pass_line": mid - cfg.scoring.band_width})
    cfg_mid = cfg.model_copy(update={"scoring": sc})
    anchored = {c.span_id: c.anchored for c in ev.cited_evidence}

    def _decide(score: float) -> str:
        return decide(cfg_mid, score, ev.llm_compare, ev.fit, ev.stage_status, set(anchored),
                      ev.hard_filters, dim_points={d.id: d.points for d in ev.dimensions},
                      bonus_names={b.name for b in ev.bonus_hits}).decision

    by_base, by_gate = _decide(float(ev.base_total)), _decide(float(ev.gate_score))
    assert (by_base, by_gate) == ("auto_pass", "manual_review"), \
        f"这条线没能把两种口径分开（base {ev.base_total} / gate {ev.gate_score}）"

    _, ev2 = _eval(FLUFF_SPANS, cfg=cfg_mid, verdict="达标")
    assert ev2.decision == by_gate, (
        f"线上判定没有吃 gate_score：得到 {ev2.decision}（判定分 {ev2.gate_score}、"
        f"维度合计 {ev2.base_total}），按 gate_score 应为 {by_gate}")
