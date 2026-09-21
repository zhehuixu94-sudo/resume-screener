# -*- coding: utf-8 -*-
"""第二段流程（推导及格项 → HR 勾选 → 清单生效）的回归测试。

这批测试钉的是**接线**，不是算法本身：
- `basic_items_of` 什么时候读岗位清单、什么时候退回占位
- 清单里的脏数据（未配置的维度、空清单、坏 JSON）不许被当成标准用
"""
from __future__ import annotations

import json

import pytest

from app.contracts import JobConfig
from app.pipeline.equal_split import basic_items_of


@pytest.fixture()
def cfg() -> JobConfig:
    """拿真配置当模板（JobConfig 有一堆必填字段），只替换 dimensions。"""
    import os
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    raw = json.load(open(os.path.join(root, "config", "job_hr_admin.json"), encoding="utf-8"))
    raw = {**raw, "job_id": "t_job", "config_version": "1"}
    dims = [dict(d) for d in raw["dimensions"][:2]]          # 保留真维度的必填字段（anchors 等）
    for i, d in enumerate(dims, 1):
        d["id"] = f"d{i}"
        d["weight"] = 50.0                                    # 契约要求权重合计 100
    raw["dimensions"] = dims
    return JobConfig.model_validate(raw)


def _write(tmp_path, monkeypatch, payload) -> None:
    """把清单写到 basic_items_of 真正会去读的位置。"""
    import app.pipeline.equal_split as es
    fake_root = tmp_path
    (fake_root / "config").mkdir(exist_ok=True)
    (fake_root / "config" / "basic_items_t_job.json").write_text(
        payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(es.os.path, "abspath", lambda p: str(fake_root / "app" / "pipeline" / "equal_split.py"))


def test_没有清单时退回占位(cfg) -> None:
    assert basic_items_of(cfg) == ["d1", "d2"]


def test_有清单时按清单走(tmp_path, monkeypatch, cfg) -> None:
    _write(tmp_path, monkeypatch, {"basic_items": ["d1"]})
    assert basic_items_of(cfg) == ["d1"]


def test_清单里的未配置维度要被丢掉(tmp_path, monkeypatch, cfg) -> None:
    """脏清单不许静默生效：清单写 d1 + 一个不存在的维度 → 只保留 d1。"""
    _write(tmp_path, monkeypatch, {"basic_items": ["d1", "不存在的维度"]})
    assert basic_items_of(cfg) == ["d1"]


def test_清单里全是非法维度时退回占位(tmp_path, monkeypatch, cfg) -> None:
    """全非法 = 等于没有标准，退回占位（而不是返回空清单让下游除以 0）。"""
    _write(tmp_path, monkeypatch, {"basic_items": ["X", "Y"]})
    assert basic_items_of(cfg) == ["d1", "d2"]


def test_坏json退回占位不抛(tmp_path, monkeypatch, cfg) -> None:
    _write(tmp_path, monkeypatch, "{ 这不是合法 json")
    assert basic_items_of(cfg) == ["d1", "d2"]


def test_空清单退回占位(tmp_path, monkeypatch, cfg) -> None:
    _write(tmp_path, monkeypatch, {"basic_items": []})
    assert basic_items_of(cfg) == ["d1", "d2"]
