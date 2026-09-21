# -*- coding: utf-8 -*-
"""校准样本入口（选项 A）的集成测试：只认 spot_check_item 的标记位 + 复核结论。

盯住四条（本轮定夺的硬边界）：
  ① 负样本只来自 `auto_reject` + `calibration_eligible=true` + `review_verdict=reject`；
  ② 人工复核件（borderline / model_conflict / manual_review / hard_gate_unknown）**即使有 reject/keep 结论
     也不进 pos/neg**，但必须逐条留 reason（被误读要看得见，不许静默）；
  ③ 正样本只来自 `auto_reject` + `keep`（确认漏放），`leak_signal=True`，单列统计；
  ④ 入口不再读 `review_decision.action` —— 所以"转人工件的结论污染 pos/neg"这条口子在入口处就堵死了。
"""
from __future__ import annotations

import json
import os
import sys
import uuid

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # noqa: E402

from app import calibrate                                     # noqa: E402
from app.contracts import JobConfig                           # noqa: E402
from app.store import db                                      # noqa: E402

try:
    from tests.test_store import _db_up
except Exception:                                             # noqa: BLE001
    def _db_up() -> bool:                                     # type: ignore[misc]
        return False

pytestmark = pytest.mark.skipif(not _db_up(), reason="Postgres 不可用（容器没起或端口不通）")

JOB_ID = "test_calib_entry"
VERSION = "c1"


def _cfg():
    cfg = JobConfig.model_validate(json.load(open("config/job_hr_admin.json", encoding="utf-8")))
    return cfg.model_copy(update={"job_id": JOB_ID, "config_version": VERSION})


@pytest.fixture()
def env():
    cfg = _cfg()
    db.execute("DELETE FROM application WHERE job_id = %s", (JOB_ID,))
    db.execute("DELETE FROM spot_check_batch WHERE job_id = %s", (JOB_ID,))
    args = []
    rids: list[str] = []
    specs = [
        # (source, verdict, reviewer, ts, eligible, decision, score)
        ("auto_reject", "reject", "hr", "2026-09-15T10:00:00", True, "auto_reject", 54.0),
        ("auto_reject", "reject", "hr", "2026-09-15T10:00:00", True, "auto_reject", 53.0),
        ("auto_reject", "reject", "hr", "2026-09-15T10:00:00", True, "auto_reject", 52.0),
        ("auto_reject", "keep", "hr", "2026-09-15T10:00:00", False, "auto_reject", 51.0),
        ("borderline", "reject", "hr", "2026-09-15T10:00:00", False, "manual_review", 69.0),
        ("model_conflict", "reject", "hr", "2026-09-15T10:00:00", False, "manual_review", 62.0),
        ("manual_review", "keep", "hr", "2026-09-15T10:00:00", False, "manual_review", 58.0),
        ("hard_gate_unknown", "keep", "hr", "2026-09-15T10:00:00", False, "manual_review", 57.0),
        ("auto_reject", "", "", None, False, "auto_reject", 40.0),          # 未复核
        ("borderline", "", "", None, False, "manual_review", 68.0),         # 未复核
    ]
    for i, (src, verdict, reviewer, ts, elig, decision, score) in enumerate(specs):
        rid = f"c{uuid.uuid4().hex[:14]}"
        rids.append(rid)
        db.execute("""INSERT INTO resume (resume_id, company_id, file_name, file_sha256, file_format)
                      VALUES (%s, 'local', %s, %s, 'txt')""", (rid, f"c_{i}.txt", rid))
        app = db.execute("""INSERT INTO application (company_id, resume_id, job_id, config_version, state)
                            VALUES ('local', %s, %s, %s, 'evaluated') RETURNING application_id""",
                         (rid, JOB_ID, VERSION), returning=True)
        aid = int(app["application_id"])
        db.execute("""INSERT INTO evaluation (application_id, resume_id, job_id, verdict, decision, base_total,
                                              gate_score, confidence, config_version, model_id, prompt_version,
                                              prompt_hash, aggregation, pass_line, payload)
                      VALUES (%s, %s, %s, '不达标', %s, %s, %s, 0.9, %s, 'fake', 'p1', 'h1', 'best', 60, '{}'::jsonb)""",
                   (aid, rid, JOB_ID, decision, score, score, VERSION))
        db.execute("""INSERT INTO spot_check_item (batch_id, application_id, resume_id, job_id, config_version,
                                                   check_date, score, state, source, priority_class,
                                                   calibration_eligible, review_verdict, reviewer, review_ts)
                      VALUES (%s, %s, %s, %s, %s, '2026-09-15', %s, 'pending', %s, %s, %s, %s, %s, %s)""",
                   (db.execute("""INSERT INTO spot_check_batch (company_id, job_id, config_version, check_date,
                                                                 drawn, want, pool_size, note)
                                  VALUES ('local', %s, %s, '2026-09-15', 10, 10, 10, 'test')
                                  ON CONFLICT (company_id, job_id, config_version, check_date)
                                  DO UPDATE SET drawn = 10 RETURNING batch_id""",
                               (JOB_ID, VERSION), returning=True)["batch_id"],
                    aid, rid, JOB_ID, VERSION, score, src, "descending", elig, verdict, reviewer, ts))
        args.append(aid)
    yield cfg, args
    db.execute("DELETE FROM application WHERE job_id = %s", (JOB_ID,))
    db.execute("DELETE FROM spot_check_batch WHERE job_id = %s", (JOB_ID,))
    db.execute("DELETE FROM resume WHERE resume_id = ANY(%s)", (rids,))


def test_负样本只认auto_reject且eligible且reject(env) -> None:
    cfg, _ = env
    pool = calibrate.calibration_pool(cfg)
    assert pool["counts"]["neg"] == 3, pool["counts"]
    assert all(n["source"] == "auto_reject" and n["leak_signal"] is False for n in pool["neg"])


def test_人工复核件的结论不进pos_neg但留reason(env) -> None:
    cfg, _ = env
    pool = calibrate.calibration_pool(cfg)
    sources = {r["source"] for r in pool["pos"] + pool["neg"]}
    assert sources == {"auto_reject"}, f"只有 auto_reject 能进 pos/neg，实际 {sources}"
    # 四条人工复核件的结论（3 reject/keep + 2 未复核）都要能在 reason 里看见
    manual = [d for d in pool["dropped"] if "人工复核件" in d["drop_reason"]]
    assert len(manual) == 4, [d["drop_reason"] for d in pool["dropped"]]
    assert pool["counts"]["manual_review_verdicts_dropped"] == 4
    assert pool["counts"]["reviewed_rows"] == 8


def test_正样本是确认漏放且单列标记(env) -> None:
    cfg, _ = env
    pool = calibrate.calibration_pool(cfg)
    assert pool["counts"]["pos"] == 1 and pool["counts"]["pos_leak_signal"] == 1
    assert pool["pos"][0]["leak_signal"] is True and pool["pos"][0]["review_verdict"] == "keep"


def test_违规eligible行计数为0且自愈可复位(env) -> None:
    cfg, args = env
    # 人为制造违规：把一条 borderline 行的 eligible 置真
    db.execute("""UPDATE spot_check_item SET calibration_eligible = true
                   WHERE job_id = %s AND source = 'borderline' AND review_verdict <> ''""", (JOB_ID,))
    pool = calibrate.calibration_pool(cfg)
    assert pool["counts"]["neg_eligible_non_auto_reject"] == 1
    assert all(r["source"] == "auto_reject" for r in pool["neg"]), "违规行不许混进 neg"
    h = calibrate.sanitize_calibration_flags(cfg, apply=False)
    assert h["violations"] == 1 and h["applied"] is False
    h = calibrate.sanitize_calibration_flags(cfg, apply=True)
    assert h["applied"] is True
    left = db.fetch_one("""SELECT count(*) AS n FROM spot_check_item
                            WHERE job_id = %s AND calibration_eligible""", (JOB_ID,))
    assert left["n"] == 3, "只该剩下 3 条合法的 auto_reject+reject"


def test_正样本不足时显式报校准未启用(env, tmp_path) -> None:
    """用户口径：正样本 <3 时**显式**说自己没启用，不静默报『样本不足』。"""
    cfg, _ = env
    path = str(tmp_path / "job.json")
    data = json.load(open("config/job_hr_admin.json", encoding="utf-8"))
    data["job_id"], data["config_version"] = JOB_ID, VERSION
    json.dump(data, open(path, "w", encoding="utf-8"), ensure_ascii=False)
    # 重放阶段需要完整评估载荷，不是本测试的靶子 → 让它返回空表，只验"启用前提"这条分支
    import app.calibrate as cal
    orig = cal.load_rows
    cal.load_rows = lambda cfg_: []
    try:
        r = cal.run(path, trigger="manual", dry_run=True)
    finally:
        cal.load_rows = orig
    assert r["status"] == "skipped" and r["calibration_enabled"] is False
    assert "校准未启用" in r["reason"]
    assert r["counts"]["neg"] == 3 and r["counts"]["pos"] == 1
    assert r["before_line"] == r["after_line"] == cfg.scoring.pass_line, "未动线"
    assert "auto_reject+keep" in r["reason"]


def test_启用前提是纯函数可单测() -> None:
    ok, why = calibrate.preconditions([{}] * 3, [{}] * 3, {"neg": 3})
    assert ok is True and why == ""
    ok, why = calibrate.preconditions([{}], [{}] * 5, {"neg": 5})
    assert ok is False and "校准未启用" in why and "auto_reject+keep" in why
    ok, why = calibrate.preconditions([{}] * 5, [{}], {"neg": 1})
    assert ok is False and "负样本不足" in why
