# -*- coding: utf-8 -*-
"""路线 A · 抽检抽样与标记位的测试。

盯住四条（都是实测踩过的类型）：
  ① 「打架」不能把 `edge_band` 算进来——它只是"分数落在线内"的标记，算进来会让 155/236 件都变打架，
     优先级等于失效（一个标记 96% 命中 = 没有优先级）；
  ② source 是**互斥**的、有优先级：auto_reject 永远标 auto_reject（它是唯一能进校准池的一类，标记不能丢）；
  ③ calibration_eligible_candidate 只能出现在 source=auto_reject 上（用户口径：转人工件的复核结论不进校准池）；
  ④ 抽样优先级必须**每类非空保底 1 条**，否则纯按优先级排会让降序类永远抽不到，
     分布检查会误判成"本批没有这一类"。
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # noqa: E402

from app.contracts import JobConfig                                                # noqa: E402
from app.review import (PRIORITY_CLASSES, SOURCE_AUTO_PASS, SOURCE_AUTO_REJECT,     # noqa: E402
                        SOURCE_BORDERLINE, SOURCE_HARD_GATE_UNKNOWN, SOURCE_MANUAL_REVIEW,
                        SOURCE_MODEL_CONFLICT, _pick_by_priority, _pick_by_quota, _row_labels,
                        classify_source, priority_class)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _cfg():
    return JobConfig.model_validate(
        json.load(open(os.path.join(ROOT, "config", "job_hr_admin.json"), encoding="utf-8")))


def _row(name: str, score: float, decision: str, conflicts=(), unknown=False):
    return {"file_name": name, "gate_score": score, "decision": decision, "resume_id": name,
            "application_id": abs(hash(name)) % 100000,
            "payload": {"conflicts": [{"type": c, "detail": ""} for c in conflicts],
                        "hard_filters": ([{"id": "edu", "name": "学历", "result": "unknown",
                                           "reason": "证据不足"}] if unknown else
                                         [{"id": "edu", "name": "学历", "result": "pass",
                                           "reason": "命中本科"}])}}


# ---------------------------------------------------------------- ① 口径
def test_打架不认edge_band() -> None:
    """edge_band 只是"落在线内"的标记，不是两路矛盾——认它会让绝大多数件都变"打架"。"""
    cfg = _cfg()
    lab = _row_labels(_row("a.txt", 62.0, "manual_review", conflicts=["edge_band"]), cfg)
    assert lab["source"] == SOURCE_MANUAL_REVIEW and lab["priority_class"] == "descending"


def test_打架认两路矛盾() -> None:
    cfg = _cfg()
    for c in ("llm_vs_rule", "fit_vs_verdict"):
        lab = _row_labels(_row("b.txt", 62.0, "manual_review", conflicts=[c]), cfg)
        assert lab["source"] == SOURCE_MODEL_CONFLICT and lab["priority_class"] == "model_conflict"


# ---------------------------------------------------------------- ② source 互斥与优先级
def test_auto_reject_标记不被别的类抢走() -> None:
    """自动淘汰件即使同时"擦线/打架"，也必须标成 auto_reject——它是唯一能进校准池的一类。"""
    cfg = _cfg()
    lab = _row_labels(_row("c.txt", 55.5, "auto_reject", conflicts=["fit_vs_verdict"]), cfg)
    assert lab["source"] == SOURCE_AUTO_REJECT
    assert lab["priority_class"] == "model_conflict"          # 抽样优先级仍按打架走（两个维度解耦）
    assert lab["calibration_eligible_candidate"] is True


def test_六类source都能落到() -> None:
    line = 70.0
    assert classify_source("auto_reject", 30, line, set(), False) == SOURCE_AUTO_REJECT
    assert classify_source("manual_review", 69.5, line, set(), False) == SOURCE_BORDERLINE
    assert classify_source("manual_review", 62, line, {"llm_vs_rule"}, False) == SOURCE_MODEL_CONFLICT
    assert classify_source("manual_review", 62, line, set(), True) == SOURCE_HARD_GATE_UNKNOWN
    assert classify_source("auto_pass", 75, line, set(), False) == SOURCE_AUTO_PASS
    assert classify_source("manual_review", 40, line, set(), False) == SOURCE_MANUAL_REVIEW


# ---------------------------------------------------------------- ③ 校准池准入
def test_只有auto_reject能进校准池() -> None:
    cfg = _cfg()
    rows = [_row("ar.txt", 30, "auto_reject"), _row("mr.txt", 62, "manual_review"),
            _row("bl.txt", 69, "manual_review"), _row("ap.txt", 80, "auto_pass")]
    for r in rows:
        lab = _row_labels(r, cfg)
        assert lab["calibration_eligible_candidate"] == (lab["source"] == SOURCE_AUTO_REJECT)
        if lab["source"] != SOURCE_AUTO_REJECT:
            assert not lab["calibration_eligible_candidate"], "转人工件的复核结论不许进校准池"


# ---------------------------------------------------------------- ④ 抽样：每类保底
def test_抽样每类非空保底一条() -> None:
    cfg = _cfg()
    pool = [_row("bl1.txt", 69.0, "manual_review"),                       # 擦线 1
            _row("mc1.txt", 60.0, "manual_review", conflicts=["llm_vs_rule"]),   # 打架 1
            _row("un1.txt", 58.0, "manual_review", unknown=True)] + \
           [_row(f"ds{i}.txt", 50 - i, "manual_review") for i in range(8)]         # 降序 8
    assert sum(1 for r in pool if _row_labels(r, cfg)["priority_class"] == "descending") == 8
    picked, info = _pick_by_priority(pool, cfg, 4)
    got = {r["priority_class"] for r in picked}
    assert got == {"borderline", "model_conflict", "hard_gate_unknown", "descending"}, got
    assert info["class_size"] == {"borderline": 1, "model_conflict": 1,
                                  "hard_gate_unknown": 1, "descending": 8}
    # 保底之外的额度仍按优先级：擦线/打架/unknown 都已抽完，剩下给降序
    assert len(picked) == 4 and sum(1 for r in picked if r["priority_class"] == "descending") == 1


def test_抽样全量时不丢件() -> None:
    cfg = _cfg()
    pool = [_row(f"x{i}.txt", 60 - i, "manual_review") for i in range(5)]
    picked, info = _pick_by_priority(pool, cfg, None)
    assert len(picked) == 5 and info["pool"] == 5
    assert [r["priority_class"] for r in picked] == sorted(
        (r["priority_class"] for r in picked), key=PRIORITY_CLASSES.index)


# ---------------------------------------------------------------- ⑤ 配额制（本轮口径）
def test_配额按权重分配且不超池子() -> None:
    from app.review import SAMPLE_WEIGHTS, _allocate
    sizes = {"auto_reject": 33, "borderline": 5, "model_conflict": 48, "manual_review": 150,
             "hard_gate_unknown": 0, "auto_pass": 0}
    alloc = _allocate(40, SAMPLE_WEIGHTS, sizes)
    assert alloc == {"auto_reject": 10, "borderline": 5, "model_conflict": 10,
                     "manual_review": 15, "hard_gate_unknown": 0, "auto_pass": 0}, alloc
    # 池子不够时按实际封顶，剩下的名额让给别的类（不许超发）
    small = {"auto_reject": 33, "borderline": 2, "model_conflict": 48, "manual_review": 150,
             "hard_gate_unknown": 0, "auto_pass": 0}
    a2 = _allocate(40, SAMPLE_WEIGHTS, small)
    assert a2["borderline"] == 2 and sum(a2.values()) == 40, a2


def test_配额制非空类保底一条() -> None:
    """权重 0 但池子非空的类（如将来的 hard_gate_unknown）也要拿到 1 条——
    否则"本类为空"与"本类被抽取器排除"分不清。"""
    from app.review import SAMPLE_WEIGHTS, _allocate
    sizes = {"auto_reject": 33, "borderline": 5, "model_conflict": 48, "manual_review": 150,
             "hard_gate_unknown": 7, "auto_pass": 0}
    alloc = _allocate(40, SAMPLE_WEIGHTS, sizes)
    assert alloc["hard_gate_unknown"] == 1 and sum(alloc.values()) == 40, alloc


def test_配额抽样无一类超过四成() -> None:
    cfg = _cfg()
    pool = ([_row(f"ar{i}.txt", 40 - i, "auto_reject") for i in range(33)]
            + [_row(f"bl{i}.txt", 69 - i, "manual_review") for i in range(5)]
            + [_row(f"mc{i}.txt", 62 - i, "manual_review", conflicts=["llm_vs_rule"]) for i in range(48)]
            + [_row(f"mr{i}.txt", 50 - i, "manual_review") for i in range(150)])
    picked, info = _pick_by_quota(pool, cfg, 40)
    assert len(picked) == 40
    share = {k: v / 40 for k, v in info["class_picked"].items() if v}
    assert max(share.values()) <= 0.40, f"单一类占比超过 40% = 又变回优先级列表：{share}"
    assert set(share) == {"auto_reject", "borderline", "model_conflict", "manual_review"}, share
    # 抽样率不再差 50 倍（校准池 30% vs 降序 10%）
    rates = {k: v / info["class_size"][k] for k, v in info["class_picked"].items() if v}
    assert max(rates.values()) / min(rates.values()) <= 10, rates


def test_配额抽样只让AutoReject进校准池() -> None:
    cfg = _cfg()
    pool = ([_row(f"ar{i}.txt", 40 - i, "auto_reject") for i in range(5)]
            + [_row(f"bl{i}.txt", 69 - i, "manual_review") for i in range(5)]
            + [_row(f"mr{i}.txt", 50 - i, "manual_review") for i in range(20)])
    picked, info = _pick_by_quota(pool, cfg, 12)
    assert picked
    for r in picked:
        assert r["calibration_eligible_candidate"] == (r["source"] == SOURCE_AUTO_REJECT)
    # 配额口径（不是"全抽"）：池 5 件、权重 10/5/15、want 12 → auto_reject 拿 4 个名额
    n_ar = sum(1 for r in picked if r["calibration_eligible_candidate"])
    assert n_ar == info["class_picked"][SOURCE_AUTO_REJECT] == 4, info["class_picked"]
    assert sum(1 for r in picked if r["source"] == SOURCE_BORDERLINE) == 2   # 擦线全抽（池里就 2 条）
    assert all(not r["calibration_eligible_candidate"] for r in picked if r["source"] != SOURCE_AUTO_REJECT)


# ---------------------------------------------------------------- ⑥ 回灌判据（步骤 5）
def test_回灌判据只认auto_reject加reject() -> None:
    """唯一入口的逐分支：置真条件 = auto_reject + reject + reviewer/review_ts 非空，缺一不可。"""
    from app.review import calibration_decision
    ok, why = calibration_decision(SOURCE_AUTO_REJECT, "reject", "hr-x", "2026-09-15T10:00:00")
    assert ok is True and "进校准池" in why
    # 非 auto_reject：即使 reject 也拒绝
    for src in (SOURCE_MANUAL_REVIEW, SOURCE_BORDERLINE, SOURCE_MODEL_CONFLICT, SOURCE_HARD_GATE_UNKNOWN):
        ok, why = calibration_decision(src, "reject", "hr-x", "2026-09-15T10:00:00")
        assert ok is False and "不进校准池" in why
    # auto_reject 但结论不是 reject / 字段不全
    assert calibration_decision(SOURCE_AUTO_REJECT, "keep", "hr", "t")[0] is False
    assert calibration_decision(SOURCE_AUTO_REJECT, "uncertain", "hr", "t")[0] is False
    assert calibration_decision(SOURCE_AUTO_REJECT, "", "hr", "t")[0] is False
    assert calibration_decision(SOURCE_AUTO_REJECT, "reject", "", "t")[0] is False
    assert calibration_decision(SOURCE_AUTO_REJECT, "reject", "hr", None)[0] is False


def test_回灌判据把keep标成漏放而不是校准样本() -> None:
    from app.review import calibration_decision
    ok, why = calibration_decision(SOURCE_AUTO_REJECT, "keep", "hr", "t")
    assert ok is False and "漏放" in why
