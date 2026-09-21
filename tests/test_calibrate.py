# -*- coding: utf-8 -*-
"""口径校准的纯逻辑测试：挑线规则、版本号、配置文件就地更新与备份。

挑线规则是「安全优先」写死的：漏放优先 → 误放 → 人工负担 → 线更低者优先。
这条规则决定了系统会不会把该面的人自动淘汰，必须有测试盯着。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.calibrate import choose_line, config_patch                     # noqa: E402


def test_choose_line_prefers_no_leak() -> None:
    counts = [
        {"line": 60.0, "leak": 2, "wrong": 0, "manual": 10},
        {"line": 50.0, "leak": 0, "wrong": 3, "manual": 40},
        {"line": 45.0, "leak": 0, "wrong": 0, "manual": 60},
    ]
    assert choose_line(counts)["line"] == 45.0, "先把漏放压到 0，其次才是误放与人工负担"


def test_choose_line_then_fewer_wrong_then_less_manual() -> None:
    counts = [
        {"line": 40.0, "leak": 0, "wrong": 1, "manual": 5},
        {"line": 42.0, "leak": 0, "wrong": 0, "manual": 30},
        {"line": 44.0, "leak": 0, "wrong": 0, "manual": 20},
    ]
    assert choose_line(counts)["line"] == 44.0, "同为 0 漏放 0 误放时，选人工负担小的那条"


def test_choose_line_tie_prefers_higher_line() -> None:
    """三项全平时取更高的线（保守方向）：反过来会一路滑向"人人自动达标"。"""
    counts = [{"line": 12.0, "leak": 0, "wrong": 0, "manual": 8},
              {"line": 34.0, "leak": 0, "wrong": 0, "manual": 8},
              {"line": 26.0, "leak": 0, "wrong": 0, "manual": 8}]
    assert choose_line(counts)["line"] == 34.0


def test_choose_line_empty() -> None:
    assert choose_line([]) is None


def test_config_patch_只动线不改配置世代() -> None:
    """动线 = scoring.calibration_version +1；config_version 必须原样不动（否则整批从操作视图消失）。"""
    cfg = {"job_id": "j", "config_version": "2", "scoring": {"pass_line": 60, "calibration_version": 0}}
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "job_x.json")
        json.dump(cfg, open(path, "w", encoding="utf-8"), ensure_ascii=False)
        out = config_patch(path, pass_line=52.0, calibration_version=1, source="anchor_calibrated")
        now = json.load(open(path, encoding="utf-8"))
        assert now["config_version"] == "2", "配置世代不许被校准动"
        assert now["scoring"]["calibration_version"] == 1 and now["scoring"]["pass_line"] == 52.0
        assert out["before"]["scoring"]["calibration_version"] == 0


def test_config_patch_keeps_other_keys_and_backs_up() -> None:
    cfg = {"job_id": "j", "config_version": "1", "job_title": "标题",
           "scoring": {"pass_line": 60, "band_width": 5, "weights": {"a": 50, "b": 50}},
           "dimensions": [{"id": "d1"}]}
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "job_x.json")
        json.dump(cfg, open(path, "w", encoding="utf-8"), ensure_ascii=False)
        out = config_patch(path, pass_line=47.5, calibration_version=3, source="自动校准 测试")
        now = json.load(open(path, encoding="utf-8"))
        assert now["scoring"]["pass_line"] == 47.5 and now["scoring"]["calibration_version"] == 3
        assert now["config_version"] == "1", "配置世代保持不变"
        assert now["scoring"]["weights"] == {"a": 50, "b": 50}, "只动必要字段，别把配置写丢"
        assert now["dimensions"] == [{"id": "d1"}]
        assert out["before"]["config_version"] == "1", "快照要能还原到改前"
        assert os.path.exists(out["backup"]), "旧版本必须备份（回滚/对照都靠它）"


# ---------------------------------------------------------------------------
# 标定的「尺子」必须与判定一致 —— 这条曾经错得很隐蔽
# ---------------------------------------------------------------------------
def _hr_cfg():
    from app.contracts import JobConfig
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return JobConfig.model_validate(
        json.load(open(os.path.join(root, "config", "job_hr_admin.json"), encoding="utf-8")))


def test_标定读判定分而不是审计分(monkeypatch) -> None:
    """`scores_of` 必须用 decide() 同一把尺子（gate_score）。

    契约里 `base_total` 写的是「规则层维度分合计；用于报告与审计」，与判定分不是同一刻度。
    实测同一批锚点：用 base_total 标出 62.2、用 gate_score 标出 76.4，**差 14.2 分**；
    而标定结果写进 config 的 pass_line，随后被 decide() 拿去和 gate_score 比 → 线被系统性低估。
    旧口径下 bonus/penalty 恒为 0、两值恰好相等，所以潜伏很久，换新口径才咬人。
    """
    from tools import calibrate
    fake = {"file_name": "r.txt", "base_total": 88.4, "gate_score": 77.1,
            "verdict": "达标", "decision": "auto_pass"}
    monkeypatch.setattr(calibrate.db, "fetch_one", lambda *a, **k: fake)
    found, missing = calibrate.scores_of(_hr_cfg(), ["r.txt"], "2")
    assert found == [("r.txt", 77.1)], "标定必须读判定分 gate_score；读成 base_total 会把线压低估"
    assert missing == []


def test_标定缺判定分要报缺失不许当零分(monkeypatch) -> None:
    """gate_score 为空时归入 missing，不能静默当成 0 分——那会凭空造出一个'不合格锚点'。"""
    from tools import calibrate
    fake = {"file_name": "r.txt", "base_total": 50.0, "gate_score": None,
            "verdict": "边缘", "decision": "manual_review"}
    monkeypatch.setattr(calibrate.db, "fetch_one", lambda *a, **k: fake)
    found, missing = calibrate.scores_of(_hr_cfg(), ["r.txt"], "2")
    assert found == [] and missing == ["r.txt"]


def test_锚点数量硬条件不满足要拒绝写盘() -> None:
    """合格 ≥3 / 不合格 ≥2 是**硬条件**：不满足必须阻止写盘。

    实测踩过：jushi_teacher 只有 2 份合格 + 1 份不合格，工具照样输出「标定结果：达标线 41.1」，
    只有 `--dry-run` 才没落盘 —— docstring 写着"硬条件"，实现却只当警告，样本不足的线会冒充结论。
    """
    from tools import calibrate
    assert len(calibrate.undersized_blockers(2, 1)) == 2, "2 份合格 + 1 份不合格：两条硬条件都不满足"
    assert len(calibrate.undersized_blockers(3, 1)) == 1, "合格够、不合格不够 → 1 条"
    assert len(calibrate.undersized_blockers(2, 2)) == 1, "不合格够、合格不够 → 1 条"
    assert calibrate.undersized_blockers(3, 2) == [], "刚好达线 → 通过"
    assert calibrate.undersized_blockers(5, 3) == [], "充裕 → 通过"


def test_分不开只警告不阻止写盘() -> None:
    """「两组分不开」是有意只警告（docstring 明写"照样写线"），别被上一条顺手也变成硬拦。

    两者的区别是设计：数量不足 = 样本根本不够，线不可信 → 拦；
    分不开 = 可能只是维度设计区分不出来，留证据交人看 → 放行并记 notes。
    """
    from tools import calibrate
    # 数量充足时分不开，blockers 必须为空（分不开由 warnings 承载）
    assert calibrate.undersized_blockers(5, 3) == []

