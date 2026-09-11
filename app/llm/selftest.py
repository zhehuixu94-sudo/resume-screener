# -*- coding: utf-8 -*-
"""离线自测：用假客户端跑通「ok / repaired / degraded / failed」四条路径。

不需要网络，也不花 token —— 这是契约里"重试与降级路径"的回归测试。
validate_contracts.py 与 tests/ 都调它。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from ..contracts.llm_outputs import LlmCompareOutput, LlmFitOutput, LlmParseOutput, LlmQuestionsOutput
from .client import ChatResult
from .coercers import coerce_compare, coerce_fit, coerce_parse, coerce_questions
from .structured import call_structured


class FakeClient:
    """按顺序吐预置响应；响应用完后重复最后一个（模拟"模型就是学不会"）。"""

    def __init__(self, responses: list[str], model: str = "fake/model"):
        self.responses = responses
        self.model = model
        self.i = 0
        self.seen_users: list[str] = []

    def chat(self, system: str, user: str, **kw: Any) -> ChatResult:
        self.seen_users.append(user)
        text = self.responses[min(self.i, len(self.responses) - 1)]
        self.i += 1
        return ChatResult(text=text, usage={"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
                          elapsed_s=0.01, trace_id=f"trace-{self.i}", model=self.model)


GOOD_PARSE = {
    "blocks": [
        {"section": "项目经历", "title": "漏洞扫描器", "period": "2024.03-2024.09",
         "items": [{"capability": "Python 漏洞扫描器开发", "evidence": "独立开发轻量级 Web 漏洞扫描器", "note": "含并发"}]},
        {"section": "教育背景", "title": "示例大学", "period": "2022.09-2026.06",
         "items": [{"capability": "网络空间安全专业", "evidence": "网络空间安全", "note": "本科"}]},
    ],
    "parse_quality": {"sections_found": ["项目经历", "教育背景"], "issues": []},
}


def _j(o: Any) -> str:
    return json.dumps(o, ensure_ascii=False)


@dataclass
class Scenario:
    name: str
    expect: str
    result: dict[str, Any]


def run_scenarios() -> list[Scenario]:
    out: list[Scenario] = []

    # 1) ok：一次就过
    r = call_structured(FakeClient([_j(GOOD_PARSE)]), stage="parse", system="s", user="u",
                        model_cls=LlmParseOutput, coercer=coerce_parse, prompt_hash="x")
    out.append(Scenario("一次通过（合法 JSON）", "ok", {
        "status": r.status, "attempts": r.meta.validation.attempts, "errors": r.meta.validation.errors,
        "blocks": len(r.obj.blocks) if r.obj else 0}))

    # 2) repaired：代码围栏 + 枚举同义 + 置信度百分数 + 未知字段
    sloppy = dict(GOOD_PARSE)
    sloppy["extra_field"] = "模型多给了一个字段"
    r2 = call_structured(FakeClient(["```json\n" + _j(sloppy) + "\n```"]), stage="parse", system="s", user="u",
                         model_cls=LlmParseOutput, coercer=coerce_parse, prompt_hash="x")
    out.append(Scenario("代码围栏 + 未知字段（自动修正并留痕）", "repaired", {
        "status": r2.status, "repairs": r2.meta.validation.repairs[:4],
        "extra_keys_dropped": r2.meta.validation.extra_keys_dropped}))

    # 3) repaired（带错误反馈重试）：第一次漏字段，第二次修好
    broken = {"blocks": [{"section": "项目经历", "title": "X", "items": []}]}      # 缺 parse_quality + 空 items
    c3 = FakeClient([_j(broken), _j(GOOD_PARSE)])
    r3 = call_structured(c3, stage="parse", system="s", user="u",
                         model_cls=LlmParseOutput, coercer=coerce_parse, prompt_hash="x")
    out.append(Scenario("首次不合契约 → 带错误反馈重试后通过", "repaired", {
        "status": r3.status, "attempts": r3.meta.validation.attempts,
        "feedback_sent": any("上一次输出不合契约" in u for u in c3.seen_users),
        "blocks_kept": len(r3.obj.blocks) if r3.obj else 0}))

    # 4) degraded：重试后仍有一道题的难度枚举无法归一 → 丢条目后通过
    qs = {"questions": [
        {"level": "基础盘", "difficulty": "低", "target": "Web 漏洞原理", "question": "SQL 注入的原理是什么？",
         "intent": "基础盘", "red_flag": "只背概念"},
        {"level": "深度", "difficulty": "超高", "target": "扫描器实现", "question": "你的扫描器如何控制并发？",
         "intent": "深度", "red_flag": "说不出取舍"},
        {"level": "上限", "difficulty": "高", "target": "误报处理", "question": "极端场景下如何降误报？",
         "intent": "上限", "red_flag": "只会调参数"},
    ]}
    r4 = call_structured(FakeClient([_j(qs)]), stage="questions", system="s", user="u",
                         model_cls=LlmQuestionsOutput, coercer=coerce_questions, prompt_hash="x")
    out.append(Scenario("两次都不合契约 → 丢坏条目降级", "degraded", {
        "status": r4.status, "attempts": r4.meta.validation.attempts,
        "kept_questions": len(r4.obj.questions) if r4.obj else 0,
        "notes": r4.meta.validation.notes[:2]}))

    # 5) failed：错误不在列表元素上，丢不动 → 彻底失败
    bad_compare = {"verdict": "达标", "closest_anchor": "合格", "confidence": 0.8, "veto_triggered": None,
                   "reason": "太短", "strengths": [], "gaps": []}
    r5 = call_structured(FakeClient([_j(bad_compare)]), stage="compare", system="s", user="u",
                         model_cls=LlmCompareOutput, coercer=coerce_compare, prompt_hash="x")
    out.append(Scenario("不合契约且无法丢条目 → 失败（上层转人工）", "failed", {
        "status": r5.status, "attempts": r5.meta.validation.attempts,
        "errors": r5.meta.validation.errors[:2], "obj_is_none": r5.obj is None}))

    # 6) repaired：fit_ratio 与三清单不符 → 以重算值为准
    fit = {"matched": [{"item": "Python", "why": "岗位要求"}], "partial": [{"item": "Linux", "why": "沾边"}],
           "unmatched": [{"item": "AutoCAD", "why": "无关"}], "fit_ratio": 0.9,
           "direction": "候选人偏安全方向，岗位要求安全服务方向", "verdict": "对口"}
    r6 = call_structured(FakeClient([_j(fit)]), stage="fit", system="s", user="u", model_cls=LlmFitOutput,
                         coercer=coerce_fit, prompt_hash="x")
    out.append(Scenario("fit_ratio 与三清单不符（确定性重算）", "repaired", {
        "status": r6.status, "fit_ratio": r6.obj.fit_ratio if r6.obj else None,
        "expected": round((1 + 0.5 * 1) / 3, 2), "repairs": r6.meta.validation.repairs[:2]}))

    # 7) failed：两次都是垃圾文本
    r7 = call_structured(FakeClient(["模型今天不想输出 JSON", "还是不想"]), stage="parse", system="s", user="u",
                         model_cls=LlmParseOutput, coercer=coerce_parse, prompt_hash="x")
    out.append(Scenario("连续两次非 JSON → 失败", "failed", {
        "status": r7.status, "attempts": r7.meta.validation.attempts, "errors": r7.meta.validation.errors[:1]}))

    for s in out:
        got = s.result["status"]
        s.result["pass"] = got == s.expect
    return out


if __name__ == "__main__":
    for s in run_scenarios():
        flag = "PASS" if s.result.get("pass") else "FAIL"
        print(f"[{flag}] {s.name} → 期望 {s.expect} / 实得 {s.result['status']}")
        print("       ", {k: v for k, v in s.result.items() if k not in ("status", "pass")})
