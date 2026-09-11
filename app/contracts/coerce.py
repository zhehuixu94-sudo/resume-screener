# -*- coding: utf-8 -*-
"""模型输出的清洗层：枚举同义归一、垃圾值剔除、未知字段记录。

顺序固定：raw JSON → coerce（记录 repairs）→ Pydantic 校验。
不做静默修正：每一次改动都进 ValidationReport.repairs，报告上看得见。
"""
from __future__ import annotations

import re
from typing import Any, get_args, get_origin

from pydantic import BaseModel

RELEVANCE_MAP = {
    "强": "强", "高": "强", "较高": "强", "直接相关": "强", "相关": "中", "完全对口": "强",
    "中": "中", "中等": "中", "部分": "中", "沾边": "中", "可迁移": "中",
    "弱": "弱", "低": "弱", "较低": "弱", "泛泛": "弱", "间接": "弱",
    "无关": "无关", "无": "无关", "不相关": "无关", "无关项": "无关", "none": "无关",
}
DIFFICULTY_MAP = {
    "高": "高", "难度高": "高", "较高": "高", "难": "高",
    "中": "中", "中等": "中", "一般": "中",
    "低": "低", "易": "低", "简单": "低", "较低": "低",
}
VERDICT_MAP = {
    "达标": "达标", "通过": "达标", "合格": "达标", "符合": "达标", "满足": "达标", "pass": "达标",
    "边缘": "边缘", "待定": "边缘", "不确定": "边缘", "borderline": "边缘", "部分达标": "边缘",
    "不达标": "不达标", "不合格": "不达标", "不符合": "不达标", "未通过": "不达标", "fail": "不达标",
}
ANCHOR_MAP = {"合格": "合格", "边缘": "边缘", "不达标": "不达标", "合格档": "合格", "边缘档": "边缘", "不达标档": "不达标"}
FIT_MAP = {
    "对口": "对口", "匹配": "对口", "方向一致": "对口",
    "部分对口": "部分对口", "部分匹配": "部分对口", "沾边": "部分对口",
    "方向不符": "方向不符", "不匹配": "方向不符", "不符": "方向不符", "方向不同": "方向不符",
}
LEVEL_MAP = {"基础盘": "基础盘", "低": "基础盘", "基础": "基础盘", "深度": "深度", "中": "深度", "上限": "上限", "高": "上限"}

GARBAGE = {"b", "B", "无", "未知", "null", "none", "None", "n/a", "N/A", "-", "—", ""}


def clean_enum(value: Any, table: dict[str, str], field: str, repairs: list[str]) -> Any:
    """把枚举值归一到受控词表；归一不了就原样返回（交给校验报错，而不是猜）。"""
    if not isinstance(value, str):
        return value
    raw = value.strip()
    if raw in table:
        key = table[raw]
        if key != raw:
            repairs.append(f"{field}: {raw!r} → {key!r}")
        return key
    # 去掉空格/全角后再试一次
    squeezed = re.sub(r"[\s　/]+", "", raw)
    for k, v in table.items():
        if squeezed == re.sub(r"[\s　/]+", "", k):
            repairs.append(f"{field}: {raw!r} → {v!r}")
            return v
    return raw


def clean_confidence(value: Any, field: str, repairs: list[str], *, default: float | None = None) -> Any:
    """置信度：支持 0.87 / "0.87" / "87%" / "87"；垃圾值 → default（默认 None 交给校验）。"""
    if isinstance(value, bool) or value is None:
        v = None
    elif isinstance(value, (int, float)):
        v = float(value)
    elif isinstance(value, str):
        s = value.strip()
        m = re.match(r"^(\d+(?:\.\d+)?)\s*%$", s)
        if m:
            v = float(m.group(1)) / 100.0
        else:
            try:
                v = float(s)
            except ValueError:
                v = None
    else:
        v = None
    if v is None:
        repairs.append(f"{field}: 无法解析（{value!r}）→ {default!r}")
        return default
    if v > 1.0:
        # 模型给了百分制
        if v <= 100.0:
            v = round(v / 100.0, 4)
            repairs.append(f"{field}: {value!r} 按百分制折算 → {v}")
        else:
            repairs.append(f"{field}: {value!r} 超范围 → {default!r}")
            return default
    v = max(0.0, min(1.0, round(v, 4)))
    if v != value:
        repairs.append(f"{field}: {value!r} → {v}")
    return v


def clean_free_text(value: Any, *, min_len: int = 6, field: str = "", repairs: list[str] | None = None) -> Any:
    """垃圾字符串（'b'、'无'、过短、无中文）→ None。用于 veto_triggered 这类"要么是人话要么是空"的字段。"""
    if not isinstance(value, str):
        return value
    v = value.strip()
    if v in GARBAGE or len(v) < min_len or not re.search(r"[\u4e00-\u9fff]", v):
        if repairs is not None:
            repairs.append(f"{field}: 垃圾值 {value!r} → None")
        return None
    return v


# ---------------------------------------------------------------- 未知字段
def _annotation_model(ann: Any) -> tuple[type[BaseModel] | None, bool]:
    """从字段注解里取出嵌套模型类型；返回 (模型类, 是否列表)。"""
    if isinstance(ann, type) and issubclass(ann, BaseModel):
        return ann, False
    origin = get_origin(ann)
    if origin in (list, tuple):
        args = [a for a in get_args(ann) if a is not Ellipsis]
        if args:
            inner = args[0]
            origin2 = get_origin(inner)
            if origin2 in (list, tuple):
                inner = get_args(inner)[0]
            if isinstance(inner, type) and issubclass(inner, BaseModel):
                return inner, True
    return None, False


def drop_unknown(model: type[BaseModel], data: Any, path: str, dropped: list[str]) -> Any:
    """按模型字段定义递归剔除非契约字段，并记录路径（Pydantic 会静默 ignore，这里要留痕）。"""
    if isinstance(data, list):
        return [drop_unknown(model, x, f"{path}[]", dropped) for x in data]
    if not isinstance(data, dict):
        return data
    allowed = {}
    for name, fi in model.model_fields.items():
        allowed[name] = fi.annotation
    out = {}
    for k, v in data.items():
        if k not in allowed:
            dropped.append(f"{path}.{k}" if path else k)
            continue
        sub, is_list = _annotation_model(allowed[k])
        if sub is not None:
            out[k] = drop_unknown(sub, v, f"{path}.{k}" if path else k, dropped) if not is_list else [
                drop_unknown(sub, x, f"{path}.{k}[]" if path else f"{k}[]", dropped) for x in (v if isinstance(v, list) else [])
            ]
        else:
            out[k] = v
    return out


def strip_optional_nulls(data: Any, model: type[BaseModel]) -> Any:
    """把值为 null 的可选字段删掉，避免 str 字段收到 None 直接报错。"""
    if not isinstance(data, dict):
        return data
    out = {}
    for k, v in data.items():
        fi = model.model_fields.get(k)
        if v is None and fi is not None and fi.is_required():
            out[k] = v
        elif v is None:
            if k in ("veto_triggered",):
                out[k] = None
            continue
        else:
            out[k] = v
    return out
