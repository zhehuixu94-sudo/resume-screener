# -*- coding: utf-8 -*-
"""定稿口径的两条新契约：① 加分项原始量换成 G2(Σq/√n)；② decide() 层「证据干净但量少」兜底。

① 盯住三件事：
   · 量化证据的质量分必须高于空话证据（C 占 0.6 权重，是唯一实测能把注水分开的量）；
   · raw 对条数是**亚线性**（除以 √n）——否则注水型靠条数多就能涨分；
   · 换掉证据文本必须改变 raw（防止接线松成"读的是另一个字段"）。

② 盯住四件事（用户口径：漏放 ≫ 多看一眼，不许不可逆淘汰）：
   · 命中兜底 → manual_review，且 conflicts 里留下 clean_but_thin 痕迹；
   · 兜底关（默认）→ 保持 auto_reject（不是悄悄改行为）；
   · 空话率高于阈值（注水型）→ 不救；基础项低于 min_base（证据本就稀薄）→ 不救；
   · 阈值是**生效值**：改配置立刻改结果（设定值=生效值）。
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # noqa: E402

from app.contracts import JobConfig                      # noqa: E402
from app.contracts.job_config import Scoring             # noqa: E402
from app.pipeline.rules import decide                    # noqa: E402
from app.pipeline.weighted_total import bonus_raw_v2, skill_words, span_quality  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

QUANT = "录入与分析数据3000余条，故障响应缩短至30分钟内"      # 数字 + 成果词
ACTION_ONLY = "负责餐厅饮食原料、物料的市场询价与采购，控制成本"  # 有动作词、无数字（旧口径罚它）
FLUFF = "沟通协调能力强，做事细致有耐心，具有良好团队精神"        # 空话模板（含动作词"协调"）
FAN_PURE = "具备良好的沟通表达能力与团队协作精神"               # 纯形容词 + 无对象 + 无动作词


def _cfg(**scoring):
    """按**契约校验**改配置：model_copy 不校验，直接塞 dict 会让 guard_review 变 dict。

    （实测踩过：model_copy(update={"guard_review": {...}}) → sc.guard_review 是 dict →
      decide() 里 AttributeError。改配置必须走 model_validate。）
    """
    cfg = JobConfig.model_validate(
        json.load(open(os.path.join(ROOT, "config", "job_hr_admin.json"), encoding="utf-8")))
    sc = Scoring.model_validate({**cfg.scoring.model_dump(mode="json"), **scoring})
    return cfg.model_copy(update={"scoring": sc})


def _cmp(verdict: str = "不达标", conf: float = 0.9):
    from app.contracts.llm_outputs import LlmCompareOutput
    return LlmCompareOutput(verdict=verdict, closest_anchor="不达标", confidence=conf,
                            reason="证据与不达标锚点一致", strengths=[], gaps=[],
                            veto_triggered=None, veto_filter_ids=[])


def _decide(cfg, score: float, fluff: float | None, base: float | None = None):
    """base = 基础项（score_parts.base）；不传 = 与总分同值（多数测试两值相近，够用）。"""
    return decide(cfg, score, _cmp(), None,
                  {"label": "ok", "compare": "ok", "fit": "ok"}, set(),
                  [], dim_points={}, bonus_names=set(), fluff_rate=fluff,
                  score_base=(score if base is None else base))


# ---------------------------------------------------------------- ① 加分项原始量 G2
def test_量化证据的质量分远高于空话() -> None:
    skills = skill_words(_cfg())
    assert span_quality(QUANT, skills) >= 0.6          # 数字 + 成果 → 0.6 权重吃满
    assert span_quality(FAN_PURE, skills) == 0.0       # 纯形容词 + 无对象 + 无动作 → 0
    assert span_quality(QUANT, skills) > span_quality(ACTION_ONLY, skills) > span_quality(FAN_PURE, skills)


def test_无数字但有动作词不再被归零() -> None:
    """设计缺陷的定点修复：旧 c_item 口径下「有动作词但没数字」拿 0.4（当不具体罚），

    新口径里这类条目的质量分必须 > 0（有宾语、可核实），只有纯形容词才归零。
    """
    skills = skill_words(_cfg())
    assert span_quality(ACTION_ONLY, skills) > 0.0
    assert span_quality(FAN_PURE, skills) == 0.0


def test_原始量对条数亚线性() -> None:
    skills = skill_words(_cfg())
    one = bonus_raw_v2([QUANT], skills)
    four = bonus_raw_v2([QUANT] * 4, skills)
    assert four > one                      # 条数多仍然加分
    assert four < 4 * one - 1e-9           # 但亚线性（除 √n）：4 条 ≈ 2 倍，不是 4 倍


def test_换掉证据文本必须改变原始量() -> None:
    skills = skill_words(_cfg())
    assert bonus_raw_v2([QUANT], skills) != bonus_raw_v2([FLUFF], skills)
    assert bonus_raw_v2([], skills) == 0.0
    assert bonus_raw_v2(["短"], skills) == 0.0      # 过短片段不参与（与 honesty 同口径）


# ---------------------------------------------------------------- ② decide() 兜底
def test_兜底命中转人工且留痕() -> None:
    cfg = _cfg(guard_review={"enabled": True, "max_fluff_rate": 0.35, "min_base": 45})
    out = _decide(cfg, 50.0, 0.12, base=50.0)       # 基础项 50 ≥45、总分 50 <55、空话率 0.12
    assert out.decision == "manual_review" and out.verdict == "边缘"
    assert "clean_but_thin" in [c.type for c in out.conflicts]
    assert "空话率 0.12" in out.reason and "基础项 50.0" in out.reason


def test_兜底比的是基础项不是总分() -> None:
    """口径错位的定点回归：min_base=45 说的是**基础项**，拿总分冒充会让界面上对不上。

    实测踩过：总分 52.15（≥45）而基础项只有 37.95，reason 却写"基础项 52.15"。
    """
    cfg = _cfg(guard_review={"enabled": True, "max_fluff_rate": 0.35, "min_base": 45})
    assert _decide(cfg, 52.0, 0.14, base=38.0).decision == "auto_reject"     # 基础项 38 < 45 → 不救
    assert _decide(cfg, 52.0, 0.14, base=50.0).decision == "manual_review"   # 基础项 50 ≥ 45 → 救


def test_兜底默认关时行为不变() -> None:
    cfg = _cfg(guard_review={"enabled": False, "max_fluff_rate": 0.35, "min_base": 45})
    out = _decide(cfg, 50.0, 0.12)
    assert out.decision == "auto_reject"


def test_注水型不被兜底救() -> None:
    cfg = _cfg(guard_review={"enabled": True, "max_fluff_rate": 0.35, "min_base": 45})
    assert _decide(cfg, 50.0, 0.78).decision == "auto_reject"      # 空话率超阈值
    assert _decide(cfg, 30.0, 0.00).decision == "auto_reject"      # 基础项低于 min_base


def test_兜底阈值是生效值() -> None:
    cfg = _cfg(guard_review={"enabled": True, "max_fluff_rate": 0.35, "min_base": 45})
    assert _decide(cfg, 50.0, 0.40).decision == "auto_reject"
    cfg2 = _cfg(guard_review={"enabled": True, "max_fluff_rate": 0.60, "min_base": 45})
    assert _decide(cfg2, 50.0, 0.40).decision == "manual_review"   # 把阈值调宽 → 立刻生效


def test_兜底只拦淘汰不拦放行() -> None:
    """分数在线内（≥线−band）本来就转人工，兜底不该把「达标 + 过线」的件降级。"""
    cfg = _cfg(guard_review={"enabled": True, "max_fluff_rate": 0.35, "min_base": 45})
    out = decide(cfg, 80.0, _cmp("达标"), None,
                 {"label": "ok", "compare": "ok", "fit": "ok"}, set(),
                 [], dim_points={}, bonus_names=set(), fluff_rate=0.05)
    assert out.decision == "auto_pass"
    assert "clean_but_thin" not in [c.type for c in out.conflicts]


# ---------------------------------------------------------------- ③ 纪律：口径变更必须冻结快照
def test_口径变更必须同批次冻结配置快照() -> None:
    """纪律判据（用户口径）：任一生产口径变更，必须同批次冻结 config 快照，否则视为未完成。

    这条测试同时钉三件事：
      · 配置里必须写 `scoring_semantics`（公式版本）——`config_version` 只管配置参数，复现不了公式；
      · 快照文件必须存在，且内嵌的 config 与当前配置**逐字一致**；
      · 快照里的**代码指纹**必须与当前源码一致 —— 改了算分代码而没重新冻结，这条就红。
    """
    import hashlib
    from tools.freeze_config_snapshot import latest_snapshot
    raw = json.load(open(os.path.join(ROOT, "config", "job_hr_admin.json"), encoding="utf-8"))
    cfg = JobConfig.model_validate(raw)
    sem = cfg.scoring.scoring_semantics
    assert sem, "配置里没写 scoring_semantics：公式版本不可复现"
    snap_dir = os.path.join(ROOT, "config", "snapshots")
    snap_path = latest_snapshot(cfg.job_id, cfg.config_version, sem, directory=snap_dir)
    assert snap_path, (f"缺配置快照（config/snapshots/{cfg.job_id}_v{cfg.config_version}"
                       f"[-cv*]-*.json） → 跑 tools/freeze_config_snapshot.py")
    snap = json.load(open(snap_path, encoding="utf-8"))
    assert snap["scoring_semantics"] == sem and snap["config"] == raw, "快照与当前配置不一致"
    # n 可以为 0 —— 但**必须显式说明**「该版本尚无入库结果」（绝不允许把"没跑"伪装成"跑出来是 0"）
    has_results = snap["metrics"]["n"] > 0
    note_ok = "尚无入库结果" in (snap.get("note") or "")
    assert snap["batch_fingerprint"] and (has_results or note_ok), \
        "快照既没绑定批次结果，也没声明『该版本尚无入库结果』"
    for rel, fp in (snap.get("code_fingerprint") or {}).items():
        cur = hashlib.sha256(open(os.path.join(ROOT, rel), "rb").read()).hexdigest()[:16]
        assert cur == fp, f"{rel} 的代码指纹变了（公式改了没重新冻结快照）"


def test_取最新快照按cv数值而非字典序(tmp_path) -> None:
    """多轮校准：cv1 / cv2 / cv10 三份并存，必须取到 cv10（字典序会把 cv10 排到 cv2 前面）。"""
    import time
    from tools.freeze_config_snapshot import latest_snapshot, snapshot_key
    job, sem = "job_x", "g2_scale25_budget25"
    names = [f"{job}_v2-g2_scale25_budget25.json",          # cv=0：老的、无 cv 段
             f"{job}_v2-cv1-{sem}.json",
             f"{job}_v2-cv2-{sem}.json",
             f"{job}_v2-cv10-{sem}.json"]
    for i, n in enumerate(names):
        (tmp_path / n).write_text(json.dumps({"scoring_semantics": sem, "metrics": {"n": 1}}),
                                  encoding="utf-8")
        os.utime(tmp_path / n, (time.time() - 100 + i, time.time() - 100 + i))
    assert snapshot_key(names[0]) == ("2", 0), "无 cv 段按 0 解释"
    assert snapshot_key(names[3]) == ("2", 10)
    assert sorted(names[1:])[-1].endswith("cv2-g2_scale25_budget25.json"), \
        "前提校验：字典序确实会选错（'cv2' > 'cv10' > 'cv1'）"
    picked = latest_snapshot(job, "2", sem, directory=str(tmp_path))
    assert picked is not None and picked.endswith(f"_v2-cv10-{sem}.json"), f"取到的是 {picked}"


def test_n0快照不红(tmp_path) -> None:
    """apply 后新冻结的 n=0 快照：有显式声明就不该让纪律测试红。"""
    from tools.freeze_config_snapshot import latest_snapshot
    job, sem = "job_y", "g2"
    snap = {"scoring_semantics": sem, "batch_fingerprint": "abc", "metrics": {"n": 0},
            "note": "自动校准 2026-09-15：⚠️ 该版本（2）尚无入库结果：批次指纹是空表指纹，指标全部为空"}
    (tmp_path / f"{job}_v2-cv1-{sem}.json").write_text(json.dumps(snap, ensure_ascii=False), encoding="utf-8")
    p = latest_snapshot(job, "2", sem, directory=str(tmp_path))
    got = json.load(open(p, encoding="utf-8"))
    has_results = got["metrics"]["n"] > 0
    assert got["batch_fingerprint"] and (has_results or "尚无入库结果" in got["note"]), \
        "n=0 但有显式声明 → 判据应通过";
