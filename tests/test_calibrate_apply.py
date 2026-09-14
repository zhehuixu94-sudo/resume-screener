# -*- coding: utf-8 -*-
"""校准「生效 + 回滚」的集成测试（要 Postgres；容器没起就跳过）。

用真实评估结果的结构 + 合成标注（标注本来就是人给的，测试里只能合成）验证：
  · 样本够且两组分得开 → 生效：达标线改掉、config_version +1、旧配置备份、calibration 落库；
  · 生效后 rollback → 配置文件与库里的 job_config 都回到改前，且 calibration 标记 reverted；
  · 样本不足 → 只记录不动线（正在跑的岗位就该是这个结果，别偷偷改线）。
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import uuid

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import calibrate                              # noqa: E402
from app.contracts import JobConfig                    # noqa: E402
from app.store import db                               # noqa: E402

try:
    from tests.test_store import _db_up
except Exception:                                      # noqa: BLE001
    def _db_up() -> bool:                              # type: ignore[misc]
        return False

pytestmark = pytest.mark.skipif(not _db_up(), reason="Postgres 不可用（容器没起或端口不通）")

JOB_ID = "test_calibrate_apply"
SRC_JOB = "game_dev"


@pytest.fixture()
def env():
    """用真实评估结果的 payload 造 6 行：3 行高分（标为该留）+ 3 行低分（标为该淘汰）。"""
    tmp = tempfile.mkdtemp(prefix="calib-apply-")
    src = db.fetch_all(
        """SELECT e.payload, e.base_total FROM evaluation e JOIN application a USING(application_id)
            WHERE a.job_id = %s AND a.config_version = '3' ORDER BY e.base_total DESC""", (SRC_JOB,))
    if len(src) < 8:
        pytest.skip("库里没有足够的真实评估结果做夹具")
    highs, lows = src[:3], src[-3:]
    parse_src = db.fetch_one(
        """SELECT payload FROM resume_parse p JOIN application a ON a.resume_id = p.resume_id
            WHERE a.job_id = %s AND a.config_version = '3' LIMIT 1""", (SRC_JOB,))
    if not parse_src:
        pytest.skip("库里没有可复用的解析结果做夹具")

    cfg_path = os.path.join(tmp, "job_test.json")
    raw = {**json.load(open("config/job_game_dev.json", encoding="utf-8")),
           "job_id": JOB_ID, "config_version": "1"}
    json.dump(raw, open(cfg_path, "w", encoding="utf-8"), ensure_ascii=False)
    cfg = JobConfig.model_validate(raw)

    db.execute("DELETE FROM application WHERE job_id = %s", (JOB_ID,))
    db.execute("DELETE FROM calibration WHERE job_id = %s", (JOB_ID,))
    apps = []
    for i, r in enumerate(highs + lows):
        payload, score = r["payload"], r["base_total"]
        rid = f"c{uuid.uuid4().hex[:14]}"
        db.execute(
            """INSERT INTO resume (resume_id, company_id, file_name, file_sha256, file_format)
               VALUES (%s, 'local', %s, %s, 'txt')""", (rid, f"c_{i}.txt", rid))
        db.execute(
            """INSERT INTO resume_parse (resume_id, payload, model_id, prompt_version, prompt_hash,
                                         validation_state, gate_passed, text)
               VALUES (%s, %s, 'fake', 'p1', 'h1', 'ok', true, '')""",
            (rid, json.dumps(parse_src["payload"], ensure_ascii=False)))
        row = db.execute(
            """INSERT INTO application (company_id, resume_id, job_id, config_version, state)
               VALUES ('local', %s, %s, '1', 'evaluated') RETURNING application_id""",
            (rid, JOB_ID), returning=True)
        app_id = int(row["application_id"])
        db.execute(
            """INSERT INTO evaluation (application_id, resume_id, job_id, verdict, decision, base_total,
                                       confidence, config_version, model_id, prompt_version, prompt_hash,
                                       aggregation, pass_line, payload)
               VALUES (%s, %s, %s, '边缘', 'manual_review', %s, 0.8, '1', 'fake', 'p1', 'h1', 'best', 60, %s)""",
            (app_id, rid, JOB_ID, float(score), json.dumps(payload, ensure_ascii=False)))
        apps.append({"application_id": app_id, "resume_id": rid, "label": "pass" if i < 3 else "reject"})

    from app.pipeline import stability                  # 校准生效的前提：稳定性已达标
    db.execute("DELETE FROM stability_check WHERE job_id = %s", (JOB_ID,))
    stability.record(JOB_ID, "1", n_samples=4, agreement=0.9, score_range=2.0)

    for a in apps:                                     # 合成标注：高分该留 / 低分该淘汰
        db.execute(
            """INSERT INTO review_decision (company_id, application_id, resume_id, job_id, config_version,
                                            action, source, note)
               VALUES ('local', %s, %s, %s, '1', %s, 'manual', '测试标注')""",
            (a["application_id"], a["resume_id"], JOB_ID, a["label"]))

    yield cfg, apps, cfg_path, tmp
    db.execute("DELETE FROM application WHERE job_id = %s", (JOB_ID,))
    db.execute("DELETE FROM calibration WHERE job_id = %s", (JOB_ID,))
    db.execute("DELETE FROM stability_check WHERE job_id = %s", (JOB_ID,))
    for a in apps:
        db.execute("DELETE FROM resume WHERE resume_id = %s", (a["resume_id"],))
    shutil.rmtree(tmp, ignore_errors=True)


def test_apply_then_rollback(env) -> None:
    cfg, apps, cfg_path, tmp = env
    # 夹具只有 3 条「该留」样本：0 漏放的置信上界也有 63%，所以这里把漏放预算放宽到 0.7，
    # 专测"生效 + 回滚"这条链路；"样本不足时拒绝动线"由 test_leak_budget.py 单独盯着。
    res = calibrate.run(cfg_path, trigger="manual", leak_budget=0.7)
    assert res["status"] == "applied", res
    assert res["samples"]["separated"] is True

    after = json.load(open(cfg_path, encoding="utf-8"))
    assert after["config_version"] == "2", "生效要 bump 版本（历史结果仍可复现）"
    assert after["scoring"]["pass_line"] == res["after_line"]
    assert os.path.exists(res["backup"]), "旧配置要备份"
    row = db.fetch_one("SELECT status, reverted_at FROM calibration WHERE job_id = %s", (JOB_ID,))
    assert row["status"] == "applied" and row["reverted_at"] is None

    rb = calibrate.rollback(cfg_path)
    assert rb["status"] == "reverted" and rb["back_to"] == "1"
    back = json.load(open(cfg_path, encoding="utf-8"))
    assert back["config_version"] == "1" and back["scoring"]["pass_line"] == 60
    row = db.fetch_one("SELECT reverted_at FROM calibration WHERE job_id = %s", (JOB_ID,))
    assert row["reverted_at"] is not None, "回滚要留痕"
    assert calibrate.rollback(cfg_path)["status"] == "nothing_to_rollback", "不能重复回滚同一次校准"


def test_insufficient_samples_never_moves_the_line(env) -> None:
    cfg, apps, cfg_path, tmp = env
    db.execute("DELETE FROM review_decision WHERE job_id = %s", (JOB_ID,))     # 把标注清空
    res = calibrate.run(cfg_path, trigger="manual", leak_budget=0.7)
    assert res["status"] == "skipped" and "样本不足" in res["reason"]
    assert json.load(open(cfg_path, encoding="utf-8"))["scoring"]["pass_line"] == 60, "样本不足不许动线"
    assert calibrate.rollback(cfg_path)["status"] == "nothing_to_rollback"


def test_stability_gate_blocks_calibration(env) -> None:
    """稳定性没达标时，即便标注充足也不许改线（否则等于拿会漂的分数去改政策）。"""
    from app.pipeline import stability
    cfg, apps, cfg_path, tmp = env
    db.execute("DELETE FROM stability_check WHERE job_id = %s", (JOB_ID,))
    res = calibrate.run(cfg_path, trigger="manual", leak_budget=0.7)
    assert res["status"] == "skipped" and "稳定性未达标" in res["reason"], res
    assert json.load(open(cfg_path, encoding="utf-8"))["scoring"]["pass_line"] == 60
    assert calibrate.rollback(cfg_path)["status"] == "nothing_to_rollback"
