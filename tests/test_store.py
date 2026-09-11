# -*- coding: utf-8 -*-
"""存储层回归测试（需要容器里的 Postgres；连不上就整体跳过）。

跑法：.venv\\Scripts\\python -m pytest tests -q
用临时 job_id/config_version，测试数据自清理，不污染真岗位的数据。
"""
from __future__ import annotations

import json
import os
import sys
import uuid

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.contracts import JobConfig  # noqa: E402
from app.store import Repository, db  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _db_up() -> bool:
    try:
        db.fetch_one("SELECT 1 AS ok")
        return True
    except Exception:                                    # noqa: BLE001
        return False


pytestmark = pytest.mark.skipif(not _db_up(), reason="Postgres 不可用（容器没起或端口不通）")


@pytest.fixture()
def cfg():
    """临时岗位配置：独立 job_id，测试完删掉自己的数据。"""
    base = json.load(open(os.path.join(ROOT, "config", "job_secops.json"), encoding="utf-8"))
    base["job_id"] = "test_job"
    base["config_version"] = "t-" + uuid.uuid4().hex[:8]
    return JobConfig.model_validate(base)


@pytest.fixture()
def repo():
    return Repository()


def _mk_apps(repo: Repository, cfg: JobConfig, n: int = 2) -> list[str]:
    ids = []
    for i in range(n):
        rid = uuid.uuid4().hex[:16]
        db.execute(
            """INSERT INTO resume (resume_id, company_id, file_name, file_sha256, file_format)
               VALUES (%s, 'local', %s, %s, 'txt')""", (rid, f"t{i}.txt", uuid.uuid4().hex))
        repo.ensure_application(rid, cfg)
        ids.append(rid)
    return ids


def _cleanup(cfg: JobConfig, resume_ids: list[str]) -> None:
    db.execute("DELETE FROM application WHERE job_id = %s AND config_version = %s", (cfg.job_id, cfg.config_version))
    for rid in resume_ids:
        db.execute("DELETE FROM resume WHERE resume_id = %s", (rid,))
    db.execute("DELETE FROM job_config WHERE job_id = %s AND config_version = %s", (cfg.job_id, cfg.config_version))


def test_建表幂等():
    db.init_schema(verbose=False)
    stmts = db.init_schema(verbose=False)                     # 再跑一次：幂等，不报错
    assert stmts
    for t in ("job_config", "resume", "application", "resume_parse", "evidence_span", "evaluation", "audit_log"):
        assert db.fetch_one("SELECT to_regclass(%s) AS t", (t,))["t"] == t


def test_重复入队不产生第二行(repo, cfg):
    rids = _mk_apps(repo, cfg, 1)
    rid = rids[0]
    a1 = repo.ensure_application(rid, cfg)
    a2 = repo.ensure_application(rid, cfg)
    assert a1 == a2
    n = db.fetch_one("SELECT count(*) AS n FROM application WHERE resume_id = %s AND job_id = %s",
                     (rid, cfg.job_id))["n"]
    assert n == 1
    _cleanup(cfg, rids)


def test_岗位配置版本化(repo, cfg):
    h1 = repo.upsert_job_config(cfg)
    h2 = repo.upsert_job_config(cfg)
    assert h1 == h2 and len(h1) == 16
    n = db.fetch_one("SELECT count(*) AS n FROM job_config WHERE job_id = %s AND config_version = %s",
                     (cfg.job_id, cfg.config_version))["n"]
    assert n == 1
    _cleanup(cfg, [])


def test_抢占是互斥的(repo, cfg):
    rids = _mk_apps(repo, cfg, 2)
    first = repo.claim(cfg, limit=2)
    assert len(first) == 2
    # 锁还新鲜 → 别人抢不到
    assert repo.claim(cfg, limit=2) == []
    # 锁超时设 0 表示"原 worker 视为已死"，可回收（kill 后立刻续跑的语义）
    again = repo.claim(cfg, limit=2, lock_timeout_min=0)
    assert len(again) == 2
    _cleanup(cfg, rids)


def test_状态流转与失败计数(repo, cfg):
    rids = _mk_apps(repo, cfg, 1)
    app = repo.claim(cfg, limit=1)[0]
    app_id = int(app["application_id"])
    assert app["state"] == "pending_parse"

    repo.set_state(app_id, "parsing", release=False)
    row = db.fetch_one("SELECT state, worker, locked_at IS NOT NULL AS locked FROM application WHERE application_id = %s",
                       (app_id,))
    assert row["state"] == "parsing" and row["locked"] and row["worker"]      # 处理中保持锁

    repo.mark_failed(app_id, "parse", "模拟解析失败")
    row = db.fetch_one("SELECT state, attempts, worker FROM application WHERE application_id = %s", (app_id,))
    assert row["state"] == "parse_failed" and row["attempts"] == 1 and row["worker"] == ""

    repo.set_state(app_id, "evaluated")
    assert db.fetch_one("SELECT state FROM application WHERE application_id = %s", (app_id,))["state"] == "evaluated"
    _cleanup(cfg, rids)


def test_失败到上限后不再被抢占(repo, cfg):
    rids = _mk_apps(repo, cfg, 1)
    app = repo.claim(cfg, limit=1)[0]
    app_id = int(app["application_id"])
    for _ in range(3):
        repo.mark_failed(app_id, "parse", "反复失败", max_attempts=3)
    assert repo.claim(cfg, limit=1) == []                     # attempts 已到上限
    n = repo.reset_stuck(cfg, include_failed=True)
    assert n >= 1
    assert len(repo.claim(cfg, limit=1)) == 1                  # 重置后可重跑
    _cleanup(cfg, rids)


def test_审计日志只增不改(repo, cfg):
    before = db.fetch_one("SELECT count(*) AS n FROM audit_log")["n"]
    repo.audit("test_action", "application", "0", {"k": "v"})
    after = db.fetch_one("SELECT count(*) AS n FROM audit_log")["n"]
    assert after == before + 1
    row = db.fetch_one("SELECT detail FROM audit_log ORDER BY id DESC LIMIT 1")
    assert row["detail"]["k"] == "v"
