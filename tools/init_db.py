#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""建表 / 查状态：存储层运维入口。

用法：
  python tools/init_db.py                      # 建表（幂等）
  python tools/init_db.py --show               # 建表并打印各表行数
  python tools/init_db.py --progress job_secops
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.contracts import JobConfig  # noqa: E402
from app.store import Repository, db  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TABLES = ["job_config", "resume", "application", "resume_parse", "evidence_span", "evaluation", "audit_log",
          "resume_chunk"]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--show", action="store_true")
    ap.add_argument("--progress", default=None, help="查看某岗位的状态机分布（config/job_xxx.json）")
    a = ap.parse_args()

    print(f"DSN：{db.dsn().split('@')[-1]}（密码不打印）")
    print("建表中…")
    stmts = db.init_schema()
    print(f"完成：执行 {len(stmts)} 条 DDL")

    if a.show:
        print("\n各表行数：")
        for t in TABLES:
            row = db.fetch_one(f"SELECT count(*) AS n FROM {t}")   # 表名来自白名单常量
            print(f"  {t:<16} {row['n'] if row else '-'}")

    if a.progress:
        path = a.progress if os.path.exists(a.progress) else os.path.join(ROOT, "config", f"{a.progress}.json")
        cfg = JobConfig.model_validate(json.load(open(path, encoding="utf-8")))
        repo = Repository()
        print(f"\n{cfg.job_id}@{cfg.config_version} 状态机分布：{repo.progress(cfg) or '（暂无应聘记录）'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
