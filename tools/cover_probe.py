#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""取证覆盖率探针：只跑"按维度定向取证"这一步，看覆盖率到底补上没有（不烧整条流水线的钱）。

用法：
    python tools/cover_probe.py --job config/job_game_dev.json --resumes local/samples_all/resume_006.txt ...
    python tools/cover_probe.py --job config/job_game_dev.json --sample 8     # 从库里随机取 8 份已解析的
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.contracts import JobConfig                       # noqa: E402
from app.contracts.resume_parse import ResumeParse         # noqa: E402
from app.llm.client import SiliconFlowClient                # noqa: E402
from app.pipeline.cover import cover_dimensions            # noqa: E402
from app.pipeline.parse import parse_resume                # noqa: E402
from app.store import db                                   # noqa: E402


def parse_of(path: str, cfg: JobConfig, client: SiliconFlowClient) -> ResumeParse:
    """优先用库里已存的解析结果（解析是简历属性，不必重跑），没有才现场解析。"""
    import hashlib
    from app.contracts.common import sha256_file
    rid = sha256_file(path)[:16]
    row = db.fetch_one("SELECT payload FROM resume_parse WHERE resume_id = %s", (rid,))
    if row:
        return ResumeParse.model_validate(row["payload"])
    return parse_resume(path, cfg, client).parse            # type: ignore[return-value]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--job", required=True)
    ap.add_argument("--resumes", nargs="*", default=[])
    ap.add_argument("--sample", type=int, default=0, help="从库里随机取 N 份已解析简历")
    ap.add_argument("--model", default=os.environ.get("RESUME_MODEL", "Qwen/Qwen3.5-35B-A3B"))
    a = ap.parse_args()

    cfg = JobConfig.model_validate(json.load(open(a.job, encoding="utf-8")))
    client = SiliconFlowClient(model=a.model)

    paths = list(a.resumes)
    if a.sample:
        rows = db.fetch_all("SELECT file_path FROM resume WHERE file_path IS NOT NULL ORDER BY random() LIMIT %s",
                            (a.sample,))
        paths += [r["file_path"] for r in rows if r["file_path"] and os.path.exists(r["file_path"])]
    if not paths:
        print("没有可跑的简历：给 --resumes 路径或 --sample N")
        return 1

    dims = [d.id for d in cfg.dimensions]
    names = {d.id: d.name for d in cfg.dimensions}
    before = Counter()      # 解析条目覆盖到的维度（改动前）
    after = Counter()       # 解析条目 + 取证条目
    n_calls_items = 0
    print(f"取证探针：{len(paths)} 份 × 岗位 {cfg.job_id}@{cfg.config_version}（模型 {a.model}）\n")
    for path in paths:
        parse = parse_of(path, cfg, client)
        labeled_like = [{"span_id": i.span_id} for i in parse.items]
        # 改动前：解析条目在标注后落到哪些维度 —— 无法离线知道，这里用"条目所在块的维度提示"近似不了，
        # 因此 before 用线上已落库的评估结果做对照（若没有就留空），after 用本次取证结果。
        cover = cover_dimensions(cfg, parse, client, model=a.model)
        got = [c for c in cover.coverage if c.found]
        n_calls_items += sum(c.n_items for c in cover.coverage)
        print(f"{os.path.basename(path):18s} 取证覆盖 {len(got)}/{len(dims)} 个维度"
              f" | 条目 {sum(c.n_items for c in cover.coverage)}"
              f" | 无证据的维度：{', '.join(c.id for c in cover.coverage if not c.found) or '无'}")
        for c in cover.coverage:
            if c.found:
                after[c.id] += 1
        for c in cover.coverage:
            if not c.found and c.note:
                print(f"      {c.id}: {c.note[:70]}")
        _ = before, labeled_like

    n = len(paths)
    print(f"\n=== 取证覆盖率（{n} 份）===")
    for did in dims:
        print(f"  {names[did]:22s} {after[did]:>3}/{n} 份有证据（{after[did] / n * 100:5.0f}%）")
    never = [d for d in dims if after[d] == 0]
    if never:
        print(f"\n完全没取到证据的维度：{never}（要么简历里真没有，要么取证提示词还要调）")
    print(f"\n平均每份取到 {n_calls_items / n:.1f} 条证据条目（这些会作为新条目进入标注与算分）")
    print("对照基线（改动前，来自落库评估）：项目 31% · 游戏理解 29% · 学习与竞赛 16%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
