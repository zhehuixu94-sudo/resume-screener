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
def env(monkeypatch):
    """用真实评估结果的 payload 造 6 行：3 行高分（标为该留）+ 3 行低分（标为该淘汰）。"""
    tmp = tempfile.mkdtemp(prefix="calib-apply-")
    # 快照目录指到临时目录：仍走**真实命名规则**，但不往仓库的 config/snapshots 里丢测试文件
    import tools.freeze_config_snapshot as _fz
    monkeypatch.setattr(_fz, "SNAPSHOT_DIR", os.path.join(tmp, "snapshots"))
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
    raw["scoring"] = {**raw["scoring"], "calibration_version": 0}
    json.dump(raw, open(cfg_path, "w", encoding="utf-8"), ensure_ascii=False)
    cfg = JobConfig.model_validate(raw)

    db.execute("DELETE FROM application WHERE job_id = %s", (JOB_ID,))
    db.execute("DELETE FROM calibration WHERE job_id = %s", (JOB_ID,))
    db.execute("DELETE FROM job_config WHERE job_id = %s", (JOB_ID,))   # 清掉上一轮遗留的世代行（含旧代码留下的 v2）
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
                                       gate_score, confidence, config_version, model_id, prompt_version,
                                       prompt_hash, aggregation, pass_line, payload)
               VALUES (%s, %s, %s, '边缘', 'manual_review', %s, %s, 0.8, '1', 'fake', 'p1', 'h1', 'best', 60, %s)""",
            (app_id, rid, JOB_ID, float(score), float(score), json.dumps(payload, ensure_ascii=False)))
        apps.append({"application_id": app_id, "resume_id": rid, "label": "pass" if i < 3 else "reject"})

    from app.pipeline import stability                  # 校准生效的前提：稳定性已达标
    db.execute("DELETE FROM stability_check WHERE job_id = %s", (JOB_ID,))
    stability.record(JOB_ID, "1", n_samples=4, agreement=0.9, score_range=2.0,
                     danger_flip_rate=0.0, safe_flip_rate=0.0)

    # 合成标注（新版入口：只认 spot_check_item 的标记位 + 复核结论）
    #   低分 3 条 → auto_reject + reject + calibration_eligible=true  → 负样本
    #   高分 3 条 → auto_reject + keep（确认漏放）                    → 正样本
    db.execute("DELETE FROM spot_check_batch WHERE job_id = %s", (JOB_ID,))
    batch = db.execute(
        """INSERT INTO spot_check_batch (company_id, job_id, config_version, check_date, drawn, want, pool_size, note)
           VALUES ('local', %s, '1', '2026-09-15', %s, %s, %s, '测试批次') RETURNING batch_id""",
        (JOB_ID, len(apps), len(apps), len(apps)), returning=True)
    for a in apps:                                     # 合成标注：高分该留 / 低分该淘汰
        keep = a["label"] == "pass"
        db.execute(
            """INSERT INTO spot_check_item (batch_id, application_id, resume_id, job_id, config_version,
                                            check_date, score, state, source, priority_class,
                                            calibration_eligible, review_verdict, reviewer, review_ts)
               VALUES (%s, %s, %s, %s, '1', '2026-09-15', %s, %s, 'auto_reject', 'descending', %s, %s, 'hr-test',
                       '2026-09-15T10:00:00')""",
            (int(batch["batch_id"]), a["application_id"], a["resume_id"], JOB_ID, 0.0,
             "passed" if keep else "rejected", (not keep), "keep" if keep else "reject"))

    yield cfg, apps, cfg_path, tmp
    db.execute("DELETE FROM application WHERE job_id = %s", (JOB_ID,))
    db.execute("DELETE FROM calibration WHERE job_id = %s", (JOB_ID,))
    db.execute("DELETE FROM stability_check WHERE job_id = %s", (JOB_ID,))
    db.execute("DELETE FROM spot_check_batch WHERE job_id = %s", (JOB_ID,))
    db.execute("DELETE FROM job_config WHERE job_id = %s", (JOB_ID,))
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
    assert after["config_version"] == "1", "配置世代不许被校准动（动它会让整批结果从操作视图消失）"
    assert after["scoring"]["calibration_version"] == 1, "动线只动 calibration_version"
    assert after["scoring"]["pass_line"] == res["after_line"]
    assert os.path.exists(res["backup"]), "旧配置要备份"
    row = db.fetch_one("SELECT status, reverted_at FROM calibration WHERE job_id = %s", (JOB_ID,))
    assert row["status"] == "applied" and row["reverted_at"] is None

    rb = calibrate.rollback(cfg_path)
    assert rb["status"] == "reverted" and rb["back_to_cv"] == "0" and rb["undone_cv"] == "1"
    back = json.load(open(cfg_path, encoding="utf-8"))
    assert back["config_version"] == "1" and back["scoring"]["pass_line"] == 60
    assert back["scoring"].get("calibration_version", 0) == 0, "回滚要连线版本一起退回去"
    row = db.fetch_one("SELECT reverted_at FROM calibration WHERE job_id = %s", (JOB_ID,))
    assert row["reverted_at"] is not None, "回滚要留痕"
    assert calibrate.rollback(cfg_path)["status"] == "nothing_to_rollback", "不能重复回滚同一次校准"


def test_insufficient_samples_never_moves_the_line(env) -> None:
    cfg, apps, cfg_path, tmp = env
    db.execute("DELETE FROM spot_check_item WHERE job_id = %s", (JOB_ID,))     # 把复核结论清空
    res = calibrate.run(cfg_path, trigger="manual", leak_budget=0.7)
    # 口径（选项 A）：先查正样本，不足就**显式**说"校准未启用"，不许静默报"样本不足"
    assert res["status"] == "skipped" and "校准未启用" in res["reason"], res
    assert res["calibration_enabled"] is False
    assert json.load(open(cfg_path, encoding="utf-8"))["scoring"]["pass_line"] == 60, "样本不足不许动线"
    assert calibrate.rollback(cfg_path)["status"] == "nothing_to_rollback"


def test_apply_freezes_snapshot_in_same_transaction(env) -> None:
    """动线必留快照：apply 成功 → 新快照存在、版本与配置逐字对得上、路径来自命名规则。"""
    cfg, apps, cfg_path, tmp = env
    res = calibrate.run(cfg_path, trigger="manual", leak_budget=0.7)
    assert res["status"] == "applied", res
    snap_path = res["snapshot_path"]
    assert os.path.exists(snap_path), "apply 必须同批次冻结快照"
    sem = cfg.scoring.scoring_semantics or "UNSET"          # 没写公式标记 → 文件名里就是 UNSET（不静默）
    assert os.path.normpath(snap_path) == os.path.normpath(
        os.path.join(tmp, "snapshots", f"{JOB_ID}_v{res['config_version']}-cv{res['calibration_version']}-{sem}.json")),         "快照名必须带 cv 段（配置世代不变）"
    import json as _j
    snap = _j.load(open(snap_path, encoding="utf-8"))
    assert snap["config_version"] == res["config_version"], "快照的配置世代必须与 apply 后一致"
    assert snap["config"]["scoring"]["calibration_version"] == res["calibration_version"], "快照内 cv 要对得上"
    assert snap["config"] == _j.load(open(cfg_path, encoding="utf-8")), "快照内嵌配置要逐字等于落盘配置"
    assert snap["config"]["scoring"]["pass_line"] == res["after_line"], "pass_line 变更可追溯"
    assert snap["batch_fingerprint"] == res["batch_fingerprint"]
    # 配置世代不变 → 快照绑定**本世代已有的 6 行**；但它们的判定线是 60、当前线是 52，
    # 所以必须在 note 里说明"指标来自旧线"（n=0 的"尚无入库结果"分支由 test_final_package 那条覆盖）
    assert snap["metrics"]["n"] == 6
    assert snap["metrics"]["result_pass_lines"] == [60.0]
    assert "旧线" in snap["note"] and "rescore" in snap["note"], snap["note"]
    row = db.fetch_one("""SELECT changed FROM calibration
                           WHERE job_id = %s AND status = 'applied' ORDER BY created_at DESC LIMIT 1""", (JOB_ID,))
    assert row["changed"]["snapshot"] == snap_path, "calibration.changed 里要留下快照路径"
    assert row["changed"]["pass_line"] == {"before": 60, "after": res["after_line"]}
    assert row["changed"]["config_version"] == "1", "线动了，配置世代记在 changed 里备查"
    assert row["changed"]["calibration_version"] == {"before": 0, "after": 1}


def test_freeze_failure_rolls_back_whole_apply(env, monkeypatch) -> None:
    """注入冻结失败 → 线不动、版本不动、无半写状态（配置/库/快照/calibration 行全还原）。"""
    cfg, apps, cfg_path, tmp = env
    before_text = open(cfg_path, encoding="utf-8").read()

    def boom(*a, **kw):
        raise RuntimeError("磁盘炸了（注入）")
    monkeypatch.setattr(calibrate, "freeze_snapshot", boom)

    res = calibrate.run(cfg_path, trigger="manual", leak_budget=0.7)
    assert res["status"] == "failed" and res["rolled_back"] is True, res
    assert open(cfg_path, encoding="utf-8").read() == before_text, "配置文件必须逐字还原"
    after = json.load(open(cfg_path, encoding="utf-8"))
    assert after["scoring"]["pass_line"] == 60 and after["config_version"] == "1", "线未动、配置世代未动"
    assert after["scoring"].get("calibration_version", 0) == 0, "cv 也不能动"
    assert not os.path.exists(res["snapshot_path"]), "不许留半写快照"
    kept = db.fetch_one("""SELECT count(*) AS n FROM job_config
                            WHERE job_id = %s AND config_version = '1'""", (JOB_ID,))
    assert kept["n"] == 1, "配置世代不变 → 当前世代那行必须还在（不能删）"
    assert db.fetch_one("""SELECT count(*) AS n FROM job_config
                            WHERE job_id = %s AND config_version = '2'""", (JOB_ID,))["n"] == 0
    assert db.fetch_one("""SELECT count(*) AS n FROM calibration
                            WHERE job_id = %s AND status = 'applied'""", (JOB_ID,))["n"] == 0, "不能留 applied 行"
    fail = db.fetch_one("""SELECT status, reason FROM calibration
                            WHERE job_id = %s ORDER BY created_at DESC LIMIT 1""", (JOB_ID,))
    assert fail["status"] == "failed" and "整体回滚" in fail["reason"]
    assert calibrate.rollback(cfg_path)["status"] == "nothing_to_rollback", "没生效就不该有东西可回滚"


def test_dry_run_does_not_freeze_and_path_matches_apply(env) -> None:
    """dry-run 不写快照，但打印/返回的"将冻结到"路径必须与真 apply 一致。"""
    cfg, apps, cfg_path, tmp = env
    dry = calibrate.run(cfg_path, trigger="manual", leak_budget=0.7, dry_run=True)
    assert dry["status"] == "dry_run"
    assert not os.path.exists(dry["snapshot_path"]), "dry-run 不许写快照"
    assert db.fetch_one("""SELECT count(*) AS n FROM calibration
                            WHERE job_id = %s""", (JOB_ID,))["n"] == 0
    real = calibrate.run(cfg_path, trigger="manual", leak_budget=0.7)
    assert real["status"] == "applied"
    assert dry["snapshot_path"] == real["snapshot_path"], "dry-run 报的路径必须就是 apply 实际写的路径"
    assert dry["after_line"] == real["after_line"]


def test_stability_gate_blocks_calibration(env) -> None:
    """稳定性没达标时，即便标注充足也不许改线（否则等于拿会漂的分数去改政策）。"""
    from app.pipeline import stability
    cfg, apps, cfg_path, tmp = env
    db.execute("DELETE FROM stability_check WHERE job_id = %s", (JOB_ID,))
    res = calibrate.run(cfg_path, trigger="manual", leak_budget=0.7)
    assert res["status"] == "skipped" and "稳定性未达标" in res["reason"], res
    assert json.load(open(cfg_path, encoding="utf-8"))["scoring"]["pass_line"] == 60
    assert calibrate.rollback(cfg_path)["status"] == "nothing_to_rollback"
