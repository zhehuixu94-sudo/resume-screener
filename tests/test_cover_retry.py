# -*- coding: utf-8 -*-
"""取证段（cover）失败的两种处理：**重试一次**，仍失败则**不许自动处置**。

实测教训（稳定性复跑撞出来的）：同一份简历同一份缓存解析，
    第 1 次 取证失败 → 计分维度只有 4 条 → 判定分 54.8 / 边缘
    第 2 次 取证成功 → 计分维度 19 条 → 判定分 76.2 / 达标
一次取证失败差 21 分，而当时的实现只写一句备注、**照样可以自动处置**。
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.contracts import JobConfig                                  # noqa: E402
from app.pipeline.evaluate import evaluate                            # noqa: E402
from tests.test_contracts import _parse_stub                          # noqa: E402
from tests.test_e2e_gate import CONCRETE_SPANS, GateFakeClient, RESUME_TEXT   # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _cfg() -> JobConfig:
    return JobConfig.model_validate(
        json.load(open(os.path.join(ROOT, "config", "job_game_dev.json"), encoding="utf-8")))


class FlakyCoverClient(GateFakeClient):
    """取证段前 `fail_times` 次返回垃圾（走契约 failed），之后正常。"""

    def __init__(self, text: str, dims: list[str], fail_times: int):
        super().__init__(text, dims)
        self.fail_times = fail_times
        self.cover_calls = 0

    def chat(self, system: str, user: str, **kw):                     # noqa: ANN003, ANN201
        if "简历证据检索员" in system:
            self.cover_calls += 1
            if self.cover_calls <= self.fail_times:
                res = super().chat(system, user, **kw)
                return res.__class__(text="这不是 JSON", usage=res.usage, elapsed_s=res.elapsed_s,
                                     trace_id=res.trace_id, model=res.model)
        return super().chat(system, user, **kw)


def test_取证失败会重试一次() -> None:
    cfg = _cfg()
    parse = _parse_stub(RESUME_TEXT, CONCRETE_SPANS)
    client = FlakyCoverClient(RESUME_TEXT, [d.id for d in cfg.dimensions], fail_times=1)
    ev, _ = evaluate(parse, cfg, client)                  # type: ignore[arg-type]
    assert client.cover_calls == 2, f"取证失败没有重试（调用 {client.cover_calls} 次）"
    assert ev.stages.cover is not None and ev.stages.cover.validation.status != "failed"
    assert ev.decision != "manual_review" or ev.gate_score > 0


def test_取证彻底失败不许自动处置() -> None:
    cfg = _cfg()
    parse = _parse_stub(RESUME_TEXT, CONCRETE_SPANS)
    client = FlakyCoverClient(RESUME_TEXT, [d.id for d in cfg.dimensions], fail_times=99)
    ev, notes = evaluate(parse, cfg, client)              # type: ignore[arg-type]
    assert ev.stages.cover is not None and ev.stages.cover.validation.status == "failed"
    assert ev.decision == "manual_review", "取证段失败却给了自动处置（分数会凭空掉 20 分）"
    assert any(c.type == "degraded_validation" for c in ev.conflicts)
    assert any("取证段失败" in n for n in notes)
