# -*- coding: utf-8 -*-
"""阈值语义：**配置里写的自动达标线，就是生效值**（不许有内部余量）。

为什么专门钉一条测试：曾经的实现里 `pass_line=60 / band=5` 之外还有一个隐性的
`auto_pass_margin=5`，于是"用户设 60、实际 70 才自动过"——界面上调线怎么调都对不上，
这是产品逻辑层面的失真（比多放行几份严重得多）。现在只有一个可见参数 `auto_pass_line`。
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.contracts import JobConfig                     # noqa: E402
from app.pipeline.rules import decide                   # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _cfg(**scoring):
    cfg = JobConfig.model_validate(
        json.load(open(os.path.join(ROOT, "config", "job_game_dev.json"), encoding="utf-8")))
    return cfg.model_copy(update={"scoring": cfg.scoring.model_copy(update=scoring)})


def _cmp():
    from app.contracts.llm_outputs import LlmCompareOutput
    return LlmCompareOutput(verdict="达标", closest_anchor="合格", confidence=0.95,
                            reason="整体与合格锚点档一致，证据充分", strengths=["[1] 有产出"],
                            gaps=[], veto_triggered=None, veto_filter_ids=[])


def _decide(cfg, score: float) -> str:
    return decide(cfg, score, _cmp(), None, {"label": "ok", "compare": "ok", "fit": "ok"}, set(),
                  [], dim_points={}, bonus_names=set()).decision


def test_设定的自动达标线就是生效值() -> None:
    cfg = _cfg(pass_line=60, band_width=5, auto_pass_line=70)
    assert cfg.scoring.pass_threshold() == 70, "配置写 70 就必须是 70"
    assert _decide(cfg, 70.0) == "auto_pass", "正好到线就该自动达标（不是 > 线）"
    assert _decide(cfg, 69.9) == "manual_review", "差一点点就转人工，没有隐性余量"


def test_不再叠加任何隐性余量() -> None:
    cfg = _cfg(pass_line=60, band_width=5, auto_pass_line=70, auto_pass_margin=5)
    assert cfg.scoring.pass_threshold() == 70, "旧余量字段不许再叠加（已废弃为 no-op）"
    assert _decide(cfg, 70.0) == "auto_pass"


def test_未设定时回退到线加band() -> None:
    cfg = _cfg(pass_line=60, band_width=5, auto_pass_line=None)
    assert cfg.scoring.pass_threshold() == 65
    assert _decide(cfg, 65.0) == "auto_pass"
    assert _decide(cfg, 64.9) == "manual_review"


def test_淘汰线跟着线走且可显示() -> None:
    cfg = _cfg(pass_line=60, band_width=5, auto_pass_line=70)
    assert cfg.scoring.reject_threshold() == 55
    # 冷启动：即便判到淘汰，闸门也会降级为转人工（auto_reject_enabled 默认 False）
    assert cfg.scoring.auto_reject_enabled is False


def test_hr_admin_配置的生效值就是70() -> None:
    cfg = JobConfig.model_validate(
        json.load(open(os.path.join(ROOT, "config", "job_hr_admin.json"), encoding="utf-8")))
    assert cfg.scoring.pass_threshold() == 70, "线上配置：自动达标线 70，写多少是多少"
