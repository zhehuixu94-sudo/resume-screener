# -*- coding: utf-8 -*-
"""结构化输出引擎：调用 → 清洗 → 校验 → （失败）带错误反馈重试 → （仍失败）降级。

四态（落进 StageMeta.validation.status）：
  ok        一次就过
  repaired  清洗/兜底修正后过（枚举归一、置信度折算、fit_ratio 重算……），修正明细在 repairs
  degraded  重试后仍不合契约，但丢得起坏条目：丢掉非法条目后过，判定层强制转人工
  failed    彻底拿不到可用结构：该段返回 None，判定层强制转人工（绝不自动淘汰）

设计原则：**降级不是静默兜底，而是显式降级 + 强制转人工**。拿不准就交给人看，
这是"宁可多看、不可漏放"在代码层的落点。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from pydantic import BaseModel, ValidationError

from ..contracts.common import StageMeta, Usage, ValidationReport
from .client import SiliconFlowClient, parse_json

Coercer = Callable[[dict[str, Any], Any], tuple[dict[str, Any], list[str]]]


@dataclass
class StructuredResult:
    data: dict[str, Any] | None
    obj: BaseModel | None
    meta: StageMeta
    raw_text: str = ""
    dynamic_notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.obj is not None and self.meta.validation.status != "failed"

    @property
    def status(self) -> str:
        return self.meta.validation.status


def _summarize_errors(err: ValidationError) -> list[str]:
    out = []
    for e in err.errors()[:6]:
        loc = ".".join(str(x) for x in e["loc"]) or "(root)"
        out.append(f"{loc}: {e['msg']}")
    return out


def _salvage(model_cls: type[BaseModel], data: dict[str, Any], max_drop: int = 20) -> tuple[dict[str, Any] | None, list[str]]:
    """丢掉校验不过的列表元素，直到能过；丢不动的错误直接放弃（返回 None）。"""
    import copy

    cur = copy.deepcopy(data)
    dropped: list[str] = []
    for _ in range(max_drop):
        try:
            model_cls.model_validate(cur)
            return cur, dropped
        except ValidationError as err:
            target = None
            for e in err.errors():
                loc = [x for x in e["loc"]]
                # 找最后一个整型下标 → 它就是坏元素
                idx_pos = next((i for i in range(len(loc) - 1, -1, -1) if isinstance(loc[i], int)), None)
                if idx_pos is not None:
                    target = loc[: idx_pos + 1]
                    break
            if target is None:
                return None, dropped
            node: Any = cur
            for k in target[:-1]:
                node = node[k]
            bad_idx = target[-1]
            if not isinstance(node, list) or bad_idx >= len(node):
                return None, dropped
            node.pop(bad_idx)
            dropped.append(".".join(str(x) for x in target))
    return None, dropped


def call_structured(client: SiliconFlowClient, *, stage: str, system: str, user: str,
                    model_cls: type[BaseModel], coercer: Coercer, job: Any = None,
                    prompt_version: str = "p1.0", prompt_hash: str = "", config_version: str = "",
                    model: str | None = None, max_tokens: int = 6000, temperature: float = 0.0,
                    json_schema: dict[str, Any] | None = None,
                    dynamic_check: Callable[[dict[str, Any], list[str]], None] | None = None,
                    max_repair: int = 1, salvage: bool = True) -> StructuredResult:
    report = ValidationReport(schema_ref=model_cls.__name__)
    usage = Usage()
    trace_ids: list[str] = []
    elapsed = 0.0
    notes: list[str] = []
    raw_text = ""
    attempt_user = user
    last_data: dict[str, Any] | None = None

    for attempt in range(1, 2 + max_repair):
        report.attempts = attempt
        try:
            res = client.chat(system, attempt_user, model=model, max_tokens=max_tokens,
                              temperature=temperature, json_schema=json_schema)
        except Exception as e:                                    # noqa: BLE001
            report.errors.append(f"调用失败：{type(e).__name__}: {str(e)[:200]}")
            notes.append("网络/平台层失败，未取得输出")
            continue

        raw_text = res.text
        elapsed = res.elapsed_s
        trace_ids.append(res.trace_id)
        usage.prompt_tokens += res.usage.get("prompt_tokens", 0)
        usage.completion_tokens += res.usage.get("completion_tokens", 0)
        usage.total_tokens += res.usage.get("total_tokens", 0)

        # 1) JSON 解析
        fenced = res.text.strip().startswith("```")
        try:
            data = parse_json(res.text)
        except ValueError as e:
            report.errors = [str(e)[:200]]
            attempt_user = _feedback(user, ["输出不是合法 JSON（可能带了代码围栏或额外文字）"])
            continue
        if fenced:
            report.repairs.append("输出带代码围栏（```json）→ 已剥离后解析")
        if not isinstance(data, dict):
            report.errors = [f"顶层不是对象，而是 {type(data).__name__}"]
            attempt_user = _feedback(user, ["顶层必须是 JSON 对象"])
            continue

        # 2) 清洗（枚举归一 / 垃圾值 / 未知字段）
        res_coerce = coercer(data, job)
        if hasattr(res_coerce, "data"):
            data, repairs, dropped_keys = res_coerce.data, res_coerce.repairs, res_coerce.dropped
        else:                                            # 兼容旧式 (data, repairs) 签名
            data, repairs = res_coerce
            dropped_keys = []
        report.repairs.extend(repairs)
        report.extra_keys_dropped.extend(dropped_keys)

        # 3) 动态业务校验（依赖岗位配置，静态 schema 表达不了）
        if dynamic_check is not None:
            dynamic_check(data, notes)
        last_data = data

        # 4) 静态校验
        try:
            obj = model_cls.model_validate(data)
        except ValidationError as e:
            errs = _summarize_errors(e)
            report.errors = errs
            if attempt <= max_repair:
                attempt_user = _feedback(user, errs)
                continue
            break

        report.status = "repaired" if (report.repairs or notes or report.extra_keys_dropped) else "ok"
        report.errors = []
        report.notes = notes
        return StructuredResult(data=data, obj=obj, meta=_meta(stage, res.model, prompt_version, prompt_hash,
                                                              config_version, temperature, trace_ids, usage,
                                                              elapsed, report), raw_text=raw_text,
                                dynamic_notes=notes)

    # ---------------------------------------------------------- 降级路径
    if salvage and last_data is not None:
        kept, dropped = _salvage(model_cls, last_data)
        if kept is not None:
            obj = model_cls.model_validate(kept)
            report.status = "degraded"
            report.notes = notes + [f"降级：丢弃不合契约的条目 {len(dropped)} 个（{', '.join(dropped[:5])}）"]
            return StructuredResult(data=kept, obj=obj, meta=_meta(stage, model or client.model, prompt_version,
                                                                   prompt_hash, config_version, temperature,
                                                                   trace_ids, usage, elapsed, report),
                                    raw_text=raw_text, dynamic_notes=notes)

    report.status = "failed"
    report.notes = notes
    return StructuredResult(data=None, obj=None,
                            meta=_meta(stage, model or client.model, prompt_version, prompt_hash,
                                       config_version, temperature, trace_ids, usage, elapsed, report),
                            raw_text=raw_text, dynamic_notes=notes)


def _meta(stage: str, model_id: str, prompt_version: str, prompt_hash: str, config_version: str,
          temperature: float, trace_ids: list[str], usage: Usage, elapsed: float,
          report: ValidationReport) -> StageMeta:
    from ..contracts.common import dump_json  # noqa: F401  (保持依赖显式)

    return StageMeta(stage=stage, model_id=model_id, prompt_version=prompt_version, prompt_hash=prompt_hash,
                     config_version=config_version, temperature=temperature, trace_ids=trace_ids,
                     usage=usage, elapsed_s=round(elapsed, 2), validation=report)


def _feedback(user: str, errors: list[str]) -> str:
    lines = "\n".join(f"- {e}" for e in errors)
    return (f"{user}\n\n【上一次输出不合契约，必须修正后重发】\n{lines}\n"
            f"只输出符合上面格式要求的 JSON 对象，不要解释文字，不要代码围栏。")
