# -*- coding: utf-8 -*-
"""抽检/复核/归档的集成测试（要 Postgres；容器没起就跳过）。

用合成数据在一个隔离的 job_id 下验证（不碰真实岗位）：
  · 抽检只从「自动淘汰库」抽，池子不足如实记录；
  · 抽过不再抽：已复核的件不再进池子；
  · 不累加：旧批次的未复核项过期，第二天仍是 want 份；
  · 未复核不算被抽过：过期件仍可能被抽中；
  · 通过 → 记人工标注 + 简历归档到「简历/<日期>/」；淘汰 → 进人工淘汰库，可手动清理（软删除）。
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

from app import review                                  # noqa: E402
from app.contracts import JobConfig                     # noqa: E402
from app.store import db                                # noqa: E402

try:
    from tests.test_store import _db_up
except Exception:                                        # noqa: BLE001
    def _db_up() -> bool:                                # type: ignore[misc]
        return False

pytestmark = pytest.mark.skipif(not _db_up(), reason="Postgres 不可用（容器没起或端口不通）")

JOB_ID = "test_review_flow"
VERSION = "t1"


@pytest.fixture()
def env():
    """隔离环境：一个测试岗位 + 3 份「自动淘汰」件 + 各自的简历文件。"""
    tmp = tempfile.mkdtemp(prefix="review-flow-")
    cfg = JobConfig.model_validate({**json.load(open("config/job_game_dev.json", encoding="utf-8")),
                                    "job_id": JOB_ID, "config_version": VERSION})
    db.execute("DELETE FROM application WHERE job_id = %s", (JOB_ID,))
    apps = []
    for i in range(3):
        rid = f"t{uuid.uuid4().hex[:14]}"
        path = os.path.join(tmp, f"resume_{i}.txt")
        open(path, "w", encoding="utf-8").write(f"测试候选人 {i} 的简历")
        db.execute(
            """INSERT INTO resume (resume_id, company_id, file_name, file_path, file_sha256, file_format,
                                   pages, char_count)
               VALUES (%s, 'local', %s, %s, %s, 'txt', 1, 20)""",
            (rid, f"resume_{i}.txt", path, rid))
        row = db.execute(
            """INSERT INTO application (company_id, resume_id, job_id, config_version, state)
               VALUES ('local', %s, %s, %s, 'evaluated') RETURNING application_id""",
            (rid, JOB_ID, VERSION), returning=True)
        app_id = int(row["application_id"])
        db.execute(
            """INSERT INTO evaluation (application_id, resume_id, job_id, verdict, decision, base_total,
                                       confidence, config_version, model_id, prompt_version, prompt_hash,
                                       aggregation, pass_line, payload)
               VALUES (%s, %s, %s, '不达标', 'auto_reject', %s, 0.7, %s, 'fake', 'p1', 'h1', 'best', 60, '{}'::jsonb)""",
            (app_id, rid, JOB_ID, 20.0 + i * 5, VERSION))
        apps.append({"application_id": app_id, "resume_id": rid, "file_path": path})
    yield cfg, apps, tmp
    db.execute("DELETE FROM application WHERE job_id = %s", (JOB_ID,))
    db.execute("DELETE FROM spot_check_batch WHERE job_id = %s", (JOB_ID,))     # 批次没有外键，得自己清
    db.execute("DELETE FROM calibration WHERE job_id = %s", (JOB_ID,))
    for a in apps:
        db.execute("DELETE FROM resume WHERE resume_id = %s", (a["resume_id"],))
    shutil.rmtree(tmp, ignore_errors=True)


def test_pool_only_auto_reject(env) -> None:
    cfg, apps, _ = env
    assert {r["application_id"] for r in review.pool(cfg)} == {a["application_id"] for a in apps}


def test_draw_records_shortage_and_respects_want(env) -> None:
    cfg, apps, _ = env
    out = review.draw(cfg, want=5, check_date="2026-01-01", archive_dir=env[2], stratified=False)
    assert out["drawn"] == 3 and out["pool"] == 3, "池子只有 3 份就从实记录，不补假数据"
    assert len(review.listing(cfg, check_date="2026-01-01")) == 3


def test_pass_archives_and_reject_enters_library(env) -> None:
    cfg, apps, tmp = env
    review.draw(cfg, want=5, check_date="2026-01-02", archive_dir=tmp, stratified=False)
    pass_id, rej_id = apps[0]["application_id"], apps[1]["application_id"]

    res = review.review(cfg, pass_id, "pass", note="项目扎实", source="spot_check", archive_dir=tmp,
                        day="2026-01-02")
    assert res["error"] is True, "系统判淘汰、HR 判该留 = 一条误差"
    assert res["archived"] and os.path.exists(res["archived"]), "通过件要真的归档到磁盘"
    assert os.path.basename(os.path.dirname(res["archived"])) == "2026-01-02"
    assert os.path.basename(os.path.dirname(os.path.dirname(res["archived"]))) == review.ARCHIVE_TOP

    review.review(cfg, rej_id, "reject", source="spot_check", archive_dir=tmp)
    lib = review.manual_reject_library(cfg)
    assert [r["application_id"] for r in lib] == [rej_id], "淘汰件进人工淘汰库"
    rej_row = db.fetch_one("SELECT archived_path FROM review_decision WHERE application_id = %s", (rej_id,))
    assert rej_row["archived_path"] and os.path.exists(rej_row["archived_path"]), "淘汰件也要留证归档"
    assert os.path.basename(os.path.dirname(os.path.dirname(rej_row["archived_path"]))) == review.ARCHIVE_REJECT_TOP

    # 已复核的件不再回池子；未复核的（第三份）仍在
    assert [r["application_id"] for r in review.pool(cfg)] == [apps[2]["application_id"]]


def test_not_accumulating_and_unreviewed_can_be_redrawn(env) -> None:
    cfg, apps, tmp = env
    review.draw(cfg, want=2, check_date="2026-01-03", archive_dir=tmp, stratified=False)
    review.draw(cfg, want=2, check_date="2026-01-04", archive_dir=tmp, stratified=False)   # 第二天：旧清单过期
    old = db.fetch_all("""SELECT state FROM spot_check_item WHERE job_id=%s AND check_date='2026-01-03'""",
                       (JOB_ID,))
    assert all(r["state"] == "expired" for r in old), "不累加：旧批次未复核项要过期"
    today = review.listing(cfg, check_date="2026-01-04")
    assert len(today) == 2, "第二天仍然是 2 条，不是 4 条"
    assert all(r["state"] == "pending" for r in today)
    # 未复核 = 不算被抽过：过期件仍可能出现在新批次里
    drawn_twice = db.fetch_one(
        """SELECT count(*) AS n FROM spot_check_item WHERE job_id=%s AND application_id=%s""",
        (JOB_ID, apps[0]["application_id"]))
    assert drawn_twice["n"] >= 1


def test_stats_counts_errors(env) -> None:
    cfg, apps, tmp = env
    review.draw(cfg, want=5, check_date="2026-01-05", archive_dir=tmp, stratified=False)
    review.review(cfg, apps[0]["application_id"], "pass", source="spot_check", archive_dir=tmp, day="2026-01-05")
    s = review.stats(cfg, day="2026-01-05")
    assert s["today_errors"] == 1 and s["reviewed_today"] == 1
    assert s["manual_reject_library"] == 0


def test_clear_manual_reject_is_soft_delete(env) -> None:
    cfg, apps, tmp = env
    review.draw(cfg, want=5, check_date="2026-01-06", archive_dir=tmp, stratified=False)
    review.review(cfg, apps[0]["application_id"], "reject", source="spot_check", archive_dir=tmp)
    assert review.clear_manual_reject(cfg, apps[0]["application_id"]) == 1
    assert review.manual_reject_library(cfg) == [], "清理后不在库里"
    row = db.fetch_one("SELECT cleared_at FROM review_decision WHERE application_id = %s",
                       (apps[0]["application_id"],))
    assert row["cleared_at"] is not None, "软删除：记录仍在，可追溯"


def test_strata_of_classifies_by_distance_to_line(env) -> None:
    """擦线带 / 中间带 / 远端的划分：越靠近"线−band"，越要人工看。"""
    from app.review import strata_of
    line, band = 60.0, 5.0                     # 淘汰门槛 = 55
    assert strata_of(54.9, line, band) == "near"
    assert strata_of(45.0, line, band) == "near"      # 55-10 = 45 起算
    assert strata_of(44.9, line, band) == "mid"
    assert strata_of(25.1, line, band) == "mid"       # 55-30 = 25 起算
    assert strata_of(24.9, line, band) == "far"
    assert strata_of(10.0, line, band) == "far"


def test_stratified_draw_concentrates_on_near_band(env) -> None:
    """分层抽检要把人力压在擦线带：给 30 份不同分数的淘汰件，擦线带应被优先抽走。"""
    cfg, apps, tmp = env
    line, band = float(cfg.scoring.pass_line), float(cfg.scoring.band_width)
    scores = [line - band - 1] * 6 + [line - band - 15] * 6 + [10.0] * 6     # 6 擦线 / 6 中间 / 6 远端
    db.execute("DELETE FROM application WHERE job_id = %s", (JOB_ID,))
    for i, sc in enumerate(scores):
        rid = f"s{uuid.uuid4().hex[:14]}"
        db.execute("""INSERT INTO resume (resume_id, company_id, file_name, file_sha256, file_format)
                      VALUES (%s, 'local', %s, %s, 'txt')""", (rid, f"s_{i}.txt", rid))
        row = db.execute("""INSERT INTO application (company_id, resume_id, job_id, config_version, state)
                            VALUES ('local', %s, %s, %s, 'evaluated') RETURNING application_id""",
                         (rid, JOB_ID, VERSION), returning=True)
        db.execute("""INSERT INTO evaluation (application_id, resume_id, job_id, verdict, decision, base_total,
                                              confidence, config_version, model_id, prompt_version, prompt_hash,
                                              aggregation, pass_line, payload)
                      VALUES (%s, %s, %s, '不达标', 'auto_reject', %s, 0.7, %s, 'fake', 'p1', 'h1', 'best', %s, '{}'::jsonb)""",
                   (int(row["application_id"]), rid, JOB_ID, float(sc), VERSION, line))
        apps.append({"application_id": int(row["application_id"]), "resume_id": rid, "file_path": ""})
    out = review.draw(cfg, check_date="2026-02-01", archive_dir=tmp)
    assert out["strata_size"]["near"] == 6 and out["strata_size"]["far"] == 6
    near_drawn = sum(1 for it in out["items"] if it["stratum"] == "near")
    far_drawn = sum(1 for it in out["items"] if it["stratum"] == "far")
    assert near_drawn == 3 and far_drawn == 0, f"擦线带该抽 3 份、远端 0 份，实际 {out['items']}"
