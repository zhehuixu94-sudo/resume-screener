# -*- coding: utf-8 -*-
"""稳定性闸门测试：**非对称**判据（危险侧必须 0，安全侧宽容）+ 冷启动不做自动淘汰。

为什么改成非对称（2026-09-15 实测）：分数已经锁死（极差 0.0），但"判定一致率"仍是 0.67——
翻开明细，两次翻转全是往**保守侧**（达标/达标/边缘 → 转人工）。把"多让人看一眼"和
"随机刷人"算进同一个指标，等于因为前者而拒绝自动化，方向错了。
初筛是不对称问题：漏放一个合格候选人的代价 ≫ 让 HR 多看一眼。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.pipeline.rules import DecisionOutcome                       # noqa: E402
from app.pipeline.stability import apply_gate, flip_metrics, is_ok   # noqa: E402


def outcome(decision: str = "auto_reject") -> DecisionOutcome:
    return DecisionOutcome(verdict="不达标", decision=decision, reason="规则分低于线−band", conflicts=[])


def rec(**kw) -> dict:
    base = {"n_samples": 4, "score_range": 1.0, "danger_flip_rate": 0.0, "safe_flip_rate": 0.25,
            "agreement": 0.67}
    base.update(kw)
    return base


# ————————————————————————————— 两侧翻转率怎么数 —————————————————————————————

def test_安全侧翻转_多数票自动达标_个别次转人工() -> None:
    m = flip_metrics([{"resume": "a", "decisions": ["auto_pass", "auto_pass", "manual_review"],
                       "verdicts": ["达标", "达标", "边缘"]}])
    assert m["danger_flip_rate"] == 0.0 and m["safe_flip_rate"] == 1.0


def test_危险侧翻转_从严翻到淘汰() -> None:
    m = flip_metrics([{"resume": "a", "decisions": ["auto_pass", "manual_review", "auto_reject"],
                       "verdicts": ["达标", "边缘", "不达标"]}])
    assert m["danger_flip_rate"] == 1.0, "有一次跑出淘汰而多数票不是淘汰 = 危险侧翻转"


def test_全部一致时两侧都是零() -> None:
    m = flip_metrics([{"resume": "a", "decisions": ["auto_pass"] * 3, "verdicts": ["达标"] * 3},
                      {"resume": "b", "decisions": ["auto_reject"] * 3, "verdicts": ["不达标"] * 3}])
    assert m["danger_flip_rate"] == 0.0 and m["safe_flip_rate"] == 0.0


# ————————————————————————————— 放行条件 —————————————————————————————

def test_危险侧为零且极差达标就放行_安全侧只告警() -> None:
    ok, why = is_ok(rec())
    assert ok, why
    assert "安全侧" in why, "安全侧翻转率要随放行理由一起报出来（告警不拦截）"


def test_危险侧非零必须拦截() -> None:
    ok, why = is_ok(rec(danger_flip_rate=0.25))
    assert not ok and "危险侧" in why, "往淘汰方向翻一次都不许放行"


def test_分数极差超限拦截() -> None:
    ok, why = is_ok(rec(score_range=9.2))
    assert not ok and "极差" in why


def test_旧记录没有两侧指标时不许放行() -> None:
    ok, why = is_ok({"n_samples": 4, "agreement": 0.9})
    assert not ok and "重跑" in why, "旧口径记录判据无法评估，必须重测"


def test_样本太少拦截() -> None:
    ok, why = is_ok(rec(n_samples=1))
    assert not ok and "样本" in why


# ————————————————————————————— 冷启动：不做自动淘汰 —————————————————————————————

def test_冷启动默认关闭自动淘汰() -> None:
    o = apply_gate(outcome("auto_reject"), rec(), job_id="j", config_version="1")
    assert o.decision == "manual_review", "没开自动淘汰时，淘汰一律降级为转人工"
    assert any("冷启动" in c.detail for c in o.conflicts)


def test_开启自动淘汰后仍要过稳定性() -> None:
    o = apply_gate(outcome("auto_reject"), rec(), allow_auto_reject=True)
    assert o.decision == "auto_reject"
    o2 = apply_gate(outcome("auto_reject"), rec(danger_flip_rate=1.0), allow_auto_reject=True)
    assert o2.decision == "manual_review"
    o3 = apply_gate(outcome("auto_reject"), None, allow_auto_reject=True)
    assert o3.decision == "manual_review", "没测过就更不许"


def test_自动达标也要过闸门() -> None:
    good = DecisionOutcome(verdict="达标", decision="auto_pass", reason="分数够", conflicts=[])
    assert apply_gate(good, rec()).decision == "auto_pass", "危险侧 0 + 极差达标 → 自动达标放行"
    blocked = apply_gate(DecisionOutcome(verdict="达标", decision="auto_pass", reason="", conflicts=[]),
                         rec(score_range=9.2))
    assert blocked.decision == "manual_review", "分数会漂时自动达标也不许"
    assert apply_gate(DecisionOutcome(verdict="边缘", decision="manual_review", reason="", conflicts=[]),
                      None).decision == "manual_review", "闸门只降不升"


def test_is_ok_给得出可执行的原因() -> None:
    ok, why = is_ok(None)
    assert not ok and "stability_check.py" in why, "拦截原因要告诉人怎么放行"
