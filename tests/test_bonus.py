# -*- coding: utf-8 -*-
"""加分层（bonus_layer）的回归测试。

背景：100 份语料跑完后发现加分项对 **100/100 全部命中**（"游戏公司实习"只要出现"游戏"二字就算），
加分彻底失去区分度；而且匹配池里混了模型自己的话（compare.reason/strengths/gaps），
等于模型夸两句就给候选人加分。这里把两条规矩钉死。
"""
from __future__ import annotations

import inspect
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.contracts import JobConfig                      # noqa: E402
from app.pipeline.rules import bonus_layer               # noqa: E402
from tests.test_contracts import _parse_stub             # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _cfg(bonus: list[dict]) -> JobConfig:
    """拿真实岗位配置做底，只替换 bonus 段（保证其它字段都合法）。"""
    raw = json.load(open(os.path.join(ROOT, "config", "job_game_dev.json"), encoding="utf-8"))
    raw["bonus"] = bonus
    return JobConfig.model_validate(raw)


def test_任意关键词口径会把加分项变成人人有份():
    """弱判据的老行为还在（兼容），但正因如此规则里必须显式声明 must_match。"""
    cfg = _cfg([{"name": "游戏公司实习", "points": 5, "keywords": ["游戏", "实习"]}])
    parse = _parse_stub("参与游戏相关课程设计，无实习", [("b1.i1", "课程设计")])
    hits = bonus_layer(cfg, parse, [{"span_id": "b1.i1", "capability": "课程设计"}])
    assert [h.name for h in hits] == ["游戏公司实习"], "关键字命中就会加分的弱行为（这是要避免的用法）"


def test_must_match_要求所有组都满足():
    """游戏公司实习 = 游戏方向雇主 AND 实习经历，缺一组不算。"""
    cfg = _cfg([{"name": "游戏公司实习", "points": 5,
                 "must_match": [["游戏公司", "游戏工作室", "互娱"], ["实习"]]}])

    only_game = _parse_stub("参与游戏课程设计，熟悉 Unity", [("b1.i1", "游戏课程设计")])
    assert bonus_layer(cfg, only_game, []) == [], "只提游戏、没有实习 → 不该加分"

    only_intern = _parse_stub("在某网络科技公司实习三个月，负责后端", [("b1.i1", "后端实习")])
    assert bonus_layer(cfg, only_intern, []) == [], "只提实习、不是游戏方向 → 不该加分"

    both = _parse_stub("在雷霆游戏工作室实习，参与手游战斗模块", [("b1.i1", "游戏工作室实习")])
    hits = bonus_layer(cfg, both, [])
    assert [h.name for h in hits] == ["游戏公司实习"], "两组都满足才加分"
    assert hits[0].evidence_span_ids, "命中必须带证据 span，加分也要能点开原文"


def test_加分不看模型的话():
    """bonus_layer 的签名里就不该有模型输出——匹配池只能是简历原文 + 已锚定证据。"""
    params = list(inspect.signature(bonus_layer).parameters)
    assert params == ["job", "parse", "labeled"], f"匹配池不该再接收模型输出，实际参数：{params}"

    cfg = _cfg([{"name": "上线或可玩作品", "points": 8, "must_match": [["Steam", "上架", "已上线"]]}])
    # 简历原文里没有这些词；即便模型在别处夸了"作品已上线"，也不该加分
    parse = _parse_stub("课程设计：贪吃蛇小游戏（本地运行）", [("b1.i1", "贪吃蛇小游戏")])
    assert bonus_layer(cfg, parse, [{"span_id": "b1.i1", "capability": "贪吃蛇小游戏"}]) == []


def test_未锚定的证据不算加分依据():
    """未锚定的条目是模型转述，不能当加分的证据来源。"""
    cfg = _cfg([{"name": "开源 / 技术产出", "points": 6, "must_match": [["开源"]]}])
    parse = _parse_stub("普通简历全文", [("b1.i1", "某个能力项")])
    unanchored = [{"span_id": "b1.i1", "capability": "开源项目贡献者"}]
    # 证据文本里没有"开源"，只有条目名里有 → 不该命中
    assert bonus_layer(cfg, parse, unanchored) == []


def test_加分项必须给出判据():
    """配置写错要当场报错，不能默默变成"永远不加分"或"永远加分"。"""
    with pytest.raises(Exception) as e:
        _cfg([{"name": "空的加分项", "points": 5}])
    assert "keywords 或 must_match" in str(e.value)
