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
                                       gate_score, confidence, config_version, model_id, prompt_version,
                                       prompt_hash, aggregation, pass_line, payload)
               VALUES (%s, %s, %s, '不达标', 'auto_reject', %s, %s, 0.7, %s, 'fake', 'p1', 'h1', 'best', 60,
                       '{}'::jsonb)""",
            (app_id, rid, JOB_ID, 20.0 + i * 5, 20.0 + i * 5, VERSION))
        apps.append({"application_id": app_id, "resume_id": rid, "file_path": path})
    yield cfg, apps, tmp
    db.execute("DELETE FROM application WHERE job_id = %s", (JOB_ID,))
    db.execute("DELETE FROM spot_check_batch WHERE job_id = %s", (JOB_ID,))     # 批次没有外键，得自己清
    db.execute("DELETE FROM calibration WHERE job_id = %s", (JOB_ID,))
    for a in apps:
        db.execute("DELETE FROM resume WHERE resume_id = %s", (a["resume_id"],))
    shutil.rmtree(tmp, ignore_errors=True)


def test_pool_defaults_to_two_sources_with_labels(env) -> None:
    """路线 A：池子默认 = 自动淘汰件 + 转人工件，各自带 source / priority_class / 校准池准入标记。"""
    cfg, apps, _ = env
    rows = review.pool(cfg)
    assert {r["application_id"] for r in rows} == {a["application_id"] for a in apps}
    assert all(r["source"] == review.SOURCE_AUTO_REJECT for r in rows), "自动淘汰件必须标 auto_reject"
    assert all(r["calibration_eligible_candidate"] is True for r in rows)
    assert all(r["priority_class"] == "descending" for r in rows), "池子里的件距线很远 → 降序类"
    # 显式只要一类时也能收窄（抽检池不复制数据，靠标记位区分）
    only_mr = review.pool(cfg, sources=(review.SOURCE_MANUAL_REVIEW,))
    assert only_mr == [], "本测试环境里没有转人工件"


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


def test_quota_draw_and_markers(env) -> None:
    """路线 A 配额抽样：按 source 桶给名额（配额制，不是优先级列表），并把标记位落库。

    旧口径二（优先级填充）已被用户换成配额制：纯按优先级填，打架类吃掉 85% 名额
    （抽样率 31% vs 降序 0.6%），那是"优先级列表"，不能用来推断池子结构。
    """
    cfg, apps, tmp = env
    line = float(cfg.scoring.pass_threshold())
    db.execute("DELETE FROM application WHERE job_id = %s", (JOB_ID,))
    specs = ([("borderline", line - 0.5, "manual_review", [])] * 2
             + [("conflict", line - 20, "manual_review", ["fit_vs_verdict"])] * 2
             + [("unknown", line - 30, "manual_review", [])] * 2
             + [("reject", line - 40, "auto_reject", [])] * 2)
    made = []
    for i, (kind, sc, decision, ct) in enumerate(specs):
        rid = f"p{uuid.uuid4().hex[:14]}"
        db.execute("""INSERT INTO resume (resume_id, company_id, file_name, file_sha256, file_format)
                      VALUES (%s, 'local', %s, %s, 'txt')""", (rid, f"p_{i}_{kind}.txt", rid))
        row = db.execute("""INSERT INTO application (company_id, resume_id, job_id, config_version, state)
                            VALUES ('local', %s, %s, %s, 'evaluated') RETURNING application_id""",
                         (rid, JOB_ID, VERSION), returning=True)
        payload = {"conflicts": [{"type": c, "detail": ""} for c in ct],
                   "hard_filters": ([{"id": "edu", "name": "学历", "result": "unknown", "reason": "证据不足"}]
                                    if kind == "unknown" else [])}
        db.execute("""INSERT INTO evaluation (application_id, resume_id, job_id, verdict, decision, base_total,
                                              gate_score, confidence, config_version, model_id, prompt_version,
                                              prompt_hash, aggregation, pass_line, payload)
                      VALUES (%s, %s, %s, %s, %s, %s, %s, 0.7, %s, 'fake', 'p1', 'h1', 'best', %s, %s::jsonb)""",
                   (int(row["application_id"]), rid, JOB_ID,
                    "不达标" if decision == "auto_reject" else "边缘", decision,
                    float(sc), float(sc), VERSION, line, json.dumps(payload, ensure_ascii=False)))
        made.append((kind, int(row["application_id"])))

    out = review.draw(cfg, want=4, check_date="2026-02-01", archive_dir=tmp)
    assert out["drawn"] == 4
    got_sources = [it["source"] for it in out["items"]]
    assert set(got_sources) == {"auto_reject", "borderline", "model_conflict", "hard_gate_unknown"}, \
        f"want=4 且四类都非空 → 配额+保底应让四类各出 1 条，实际 {got_sources}"
    assert len(set(got_sources)) == len(got_sources), "同一类不许超配额重复占位"
    rows = db.fetch_all("""SELECT application_id, source, priority_class, calibration_eligible
                             FROM spot_check_item WHERE job_id = %s AND check_date = '2026-02-01'""",
                        (JOB_ID,))
    by_id = {int(r["application_id"]): r for r in rows}
    assert all(r["calibration_eligible"] is False for r in rows), "未复核 → 一律不许进校准池"
    for kind, aid in made:
        if aid not in by_id:
            continue
        r = by_id[aid]
        if kind == "reject":
            assert r["source"] == "auto_reject"
        elif kind == "conflict":
            assert r["source"] == "model_conflict" and r["priority_class"] == "model_conflict"
        elif kind == "unknown":
            assert r["source"] == "hard_gate_unknown" and r["priority_class"] == "hard_gate_unknown"
        else:
            assert r["source"] == "borderline" and r["priority_class"] == "borderline"


# ---------------------------------------------------------------- 步骤 4/5：复核结论落库 + 回灌
def _write_sample(path: str, rows: list[dict]) -> None:
    import csv as _c
    cols = ["application_id", "file_name", "gate_score", "system_decision", "source", "priority_class",
            "calibration_eligible_candidate", "calibration_eligible"]
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = _c.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in rows:
            w.writerow({**{c: "" for c in cols}, **r})


def test_record_verdict_writes_columns_and_forces_false(env) -> None:
    """步骤 4：三列落库；且**永远**不许由写入路径把 calibration_eligible 置真（哪怕库里已是 true）。"""
    cfg, apps, tmp = env
    sample = os.path.join(tmp, "sample.csv")
    _write_sample(sample, [{"application_id": a["application_id"], "file_name": "r.txt",
                            "gate_score": 30, "system_decision": "auto_reject",
                            "source": "auto_reject", "priority_class": "descending"} for a in apps])
    aid = apps[0]["application_id"]
    review.draw(cfg, want=5, check_date="2026-03-01", archive_dir=tmp, stratified=False)
    db.execute("UPDATE spot_check_item SET source='auto_reject', calibration_eligible=true WHERE application_id=%s",
               (aid,))                                    # 人为把它置真，看写入路径是否复位
    res = review.record_verdict(cfg, aid, "reject", "hr-徐", note="确实不符合", sample_file=sample,
                                archive_dir=tmp, day="2026-03-01")
    assert res["calibration_eligible"] is False and res["review_ts"]
    row = db.fetch_one("""SELECT review_verdict, reviewer, review_ts, state, calibration_eligible
                            FROM spot_check_item WHERE application_id=%s""", (aid,))
    assert row["review_verdict"] == "reject" and row["reviewer"] == "hr-徐"
    assert row["review_ts"] is not None and row["state"] == "rejected"
    assert row["calibration_eligible"] is False, "写入路径必须强制 false（只有回灌脚本能置真）"


def test_record_verdict_validations(env) -> None:
    """三条硬校验：枚举、署名、样本必须来自冻结文件。"""
    cfg, apps, tmp = env
    sample = os.path.join(tmp, "sample.csv")
    _write_sample(sample, [{"application_id": apps[0]["application_id"], "source": "auto_reject"}])
    review.draw(cfg, want=5, check_date="2026-03-02", archive_dir=tmp, stratified=False)
    with pytest.raises(SystemExit):
        review.record_verdict(cfg, apps[0]["application_id"], "maybe", "hr", sample_file=sample)
    with pytest.raises(SystemExit):
        review.record_verdict(cfg, apps[0]["application_id"], "reject", "  ", sample_file=sample)
    with pytest.raises(SystemExit):
        review.record_verdict(cfg, apps[1]["application_id"], "reject", "hr", sample_file=sample)


def test_record_verdict_keep_is_leak_signal(env) -> None:
    """auto_reject + keep = 确认漏放：留信号、不进校准池。"""
    cfg, apps, tmp = env
    sample = os.path.join(tmp, "sample.csv")
    _write_sample(sample, [{"application_id": apps[0]["application_id"], "source": "auto_reject"}])
    review.draw(cfg, want=5, check_date="2026-03-03", archive_dir=tmp, stratified=False)
    db.execute("UPDATE spot_check_item SET source='auto_reject' WHERE application_id=%s",
               (apps[0]["application_id"],))
    res = review.record_verdict(cfg, apps[0]["application_id"], "keep", "hr-徐", sample_file=sample,
                                archive_dir=tmp, day="2026-03-03")
    assert res["leak_signal"] is True and res["calibration_eligible"] is False


def test_backfill_tool_dry_run_counts(env, tmp_path) -> None:
    """步骤 5：回灌 dry-run 计数——只置真 auto_reject+reject，非 auto_reject 一律拒绝。"""
    import csv as _c
    import subprocess
    cfg, apps, tmp = env
    sample = str(tmp_path / "sample.csv")
    rows = [{"application_id": apps[0]["application_id"], "file_name": "a.txt", "gate_score": 40,
             "system_decision": "auto_reject", "source": "auto_reject"},
            {"application_id": apps[1]["application_id"], "file_name": "b.txt", "gate_score": 60,
             "system_decision": "manual_review", "source": "manual_review"}]
    _write_sample(sample, rows)
    reviews = str(tmp_path / "reviews.csv")
    with open(reviews, "w", encoding="utf-8-sig", newline="") as f:
        w = _c.DictWriter(f, fieldnames=["application_id", "review_verdict", "reviewer", "review_ts"])
        w.writeheader()
        w.writerow({"application_id": apps[0]["application_id"], "review_verdict": "reject",
                    "reviewer": "hr-徐", "review_ts": "2026-09-15T10:00:00"})
        w.writerow({"application_id": apps[1]["application_id"], "review_verdict": "reject",
                    "reviewer": "hr-徐", "review_ts": "2026-09-15T10:00:00"})   # 非 auto_reject
    out_csv = str(tmp_path / "pool.csv")
    # 环境无关：子进程 stdout 显式钉成 UTF-8（否则 Windows 下按 ANSI 代码页输出中文，
    # 父进程按 UTF-8 解码会 UnicodeDecodeError；2026-09-26 由 WSL 侧复现），
    # 父进程侧再加 errors="replace" 兜底 —— 本测试不该依赖环境里的 PYTHONIOENCODING。
    p = subprocess.run([sys.executable, "tools/calibration_backfill.py", "--sample", sample,
                        "--reviews", reviews, "--out", out_csv],
                       capture_output=True, text=True, encoding="utf-8", errors="replace",
                       env=dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1"))
    assert p.returncode == 0, p.stderr
    assert "应置真（进校准池）：1" in p.stdout, p.stdout
    assert "拒绝置真：1" in p.stdout, p.stdout
    got = list(_c.DictReader(open(out_csv, encoding="utf-8-sig")))
    ok = {r["file_name"]: r["calibration_eligible_now"] for r in got}
    assert ok["a.txt"] == "True" and ok["b.txt"] == "False"
    assert all(r["calibration_eligible_now"] != "True" or r["source"] == "auto_reject" for r in got)
