#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把原型 v0 的岗位配置（config/job_*.json）迁移到岗位配置契约 v1。

迁移只做"结构搬家"，不替 HR 编内容：
- pass_line 搬进 scoring.pass_line，并标 pass_line_source=manual_default（承认这是拍的，不是标定的）；
- 维度 keywords 留空（原型把兜底关键词硬编码在 score_resume.py 里，那属于代码，不能算 HR 配置）；
  留空的维度不参与兜底归类，条目会进"未归类"单列——这是刻意的：宁可留空，不硬塞。
- 加 calibration（未标定）、schema_version、config_version、company_id。

用法：
  python tools/migrate_job_config.py --src D:/resume-demo/config --dst config
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.contracts import SCHEMA_VERSION  # noqa: E402

WARN = "维度 keywords 为空（不参与兜底归类）：{ids}"


def migrate(old: dict) -> dict:
    dims = [{"id": d["id"], "name": d["name"], "weight": d["weight"],
             "anchors": d["anchors"], "keywords": []} for d in old["dimensions"]]
    hard = [{"id": f["id"], "name": f["name"], "rule": f["rule"], "veto": bool(f.get("veto", True)),
             "require_evidence": True, "keywords": []} for f in old.get("hard_filters", [])]
    bonus = [{"name": b["name"], "points": b["points"], "keywords": b.get("keywords", [b["name"]])}
             for b in old.get("bonus", [])]
    return {
        "schema_version": SCHEMA_VERSION,
        "job_id": old["job_id"],
        "config_version": "1",
        "company_id": "local",
        "job_title": old["job_title"],
        "jd": old["jd"],
        "dimensions": dims,
        "hard_filters": hard,
        "bonus": bonus,
        "scoring": {
            "pass_line": old.get("pass_line", 60),
            "pass_line_source": "manual_default",
            "band_width": 5,
            "dimension_aggregation": "best",
            "fallback_keyword_mapping": True,
            "confidence_floor": 0.6,
        },
        "calibration": {"status": "uncalibrated", "anchors": []},
        "interview_focus": old.get("interview_focus", ""),
        "notes": "由原型 v0 配置迁移（结构搬家，未替 HR 编内容）",
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default=r"D:\resume-demo\config")
    ap.add_argument("--dst", default="config")
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()

    os.makedirs(a.dst, exist_ok=True)
    for src in sorted(glob.glob(os.path.join(a.src, "job_*.json"))):
        old = json.load(open(src, encoding="utf-8"))
        dst = os.path.join(a.dst, os.path.basename(src))
        if os.path.exists(dst) and not a.force:
            print(f"跳过（已存在，加 --force 覆盖）：{dst}")
            continue
        new = migrate(old)
        json.dump(new, open(dst, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        print(f"迁移 {os.path.basename(src)} → {dst}")
        print("  " + WARN.format(ids=[d["id"] for d in new["dimensions"]]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
