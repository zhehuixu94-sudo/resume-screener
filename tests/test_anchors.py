# -*- coding: utf-8 -*-
"""标尺（anchor_prompt）回归测试：三档锚点必须覆盖**所有维度**。

背景：原实现每个档位只取"第一个维度的锚点"就 break，另外 5 个维度的锚点从没进过对照提示词，
等于拿一把只量某个维度的尺子量整个人。
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.contracts import JobConfig  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _cfg(name: str) -> JobConfig:
    return JobConfig.model_validate(json.load(open(os.path.join(ROOT, "config", name), encoding="utf-8")))


def test_三档标尺覆盖所有维度():
    cfg = _cfg("job_game_dev.json")
    ap = cfg.anchor_prompt()
    assert set(ap) == {"合格", "边缘", "不达标"}
    for lvl, text in ap.items():
        for d in cfg.dimensions:
            assert d.name in text, f"{lvl} 档缺了维度「{d.name}」的锚点"
        assert text.count("- [") == len(cfg.dimensions), f"{lvl} 档锚点条数与维度数不一致"


def test_标尺按权重从高到低且做了截断():
    cfg = _cfg("job_game_dev.json")
    ap = cfg.anchor_prompt(per_dim_chars=30)
    lines = ap["合格"].splitlines()
    weights = [float(l.split("权重")[1].split("]")[0]) for l in lines]
    assert weights == sorted(weights, reverse=True), "标尺应按维度权重降序（重要的先给模型看）"
    # 截断生效：每行维度说明不超过 per_dim_chars + 前缀
    assert all(len(l.split("] ", 1)[1]) <= 30 for l in lines), "单维度锚点没有被截断，提示词会被撑爆"


def test_所有岗位配置的标尺都不空():
    for f in os.listdir(os.path.join(ROOT, "config")):
        if not f.startswith("job_") or not f.endswith(".json"):
            continue
        cfg = _cfg(f)
        ap = cfg.anchor_prompt()
        assert len(ap) == 3, f"{f} 的三档标尺不全"
        assert all(v.strip() for v in ap.values()), f"{f} 有空档标尺"
