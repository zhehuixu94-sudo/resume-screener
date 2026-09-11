#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""单份简历计时基准：把"评估一份简历要多久"变成可复现的数字，而不是估计。

做法：给 LLM 客户端打桩，记录**每次调用**的耗时与阶段（解析/标注/对照/匹配/面试题），
再跑一份简历的完整链路，输出阶段耗时与调用次数明细。

用法：
    python tools/time_one.py --resume local/samples_all/resume_001.txt --job config/job_game_dev.json
    python tools/time_one.py --resume ... --job ... --repeat 2     # 连跑两遍看抖动
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.contracts import JobConfig                     # noqa: E402
from app.llm import client as llm_client                # noqa: E402
from app.pipeline import evaluate as ev                 # noqa: E402
from app.pipeline import parse as ps                    # noqa: E402
from app.text import extract as ex                      # noqa: E402

CALLS: list[tuple[str, float]] = []


def instrument() -> None:
    """记录每次 LLM 调用的耗时；原样转发返回值，不改行为。"""
    orig = llm_client.SiliconFlowClient.chat

    def stage_of(system: str, user: str) -> str:
        """按提示词特征判断这次调用属于哪个阶段（调用方没传 tag，只能从内容认）。"""
        head = (system or "")[:400] + (user or "")[:200]
        # 按提示词里的角色定位认阶段（SYS_PARSE/SYS_LABEL/SYS_COMPARE/SYS_FIT/SYS_Q）
        for key, name in (("结构化解析器", "解析"), ("招聘评估助手", "标注"), ("资深招聘评审", "对照解释"),
                          ("匹配度分析员", "匹配度"), ("技术面试官", "面试题"),
                          ("标注", "标注"), ("面试", "面试题")):
            if key in head:
                return name
        return "其他"

    def timed(self, system, user, **kw):                # type: ignore[no-untyped-def]
        t0 = time.perf_counter()
        try:
            return orig(self, system, user, **kw)
        finally:
            CALLS.append((stage_of(system, user), time.perf_counter() - t0))

    llm_client.SiliconFlowClient.chat = timed              # type: ignore[method-assign]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--resume", required=True)
    ap.add_argument("--job", required=True)
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--model", default=os.environ.get("RESUME_MODEL", "Qwen/Qwen3.5-35B-A3B"))
    a = ap.parse_args()

    cfg = JobConfig.model_validate(json.load(open(a.job, encoding="utf-8")))
    instrument()
    client = llm_client.SiliconFlowClient(model=a.model)

    totals = []
    for i in range(a.repeat):
        CALLS.clear()
        t0 = time.perf_counter()
        ext = ex.extract(a.resume)
        t_ext = time.perf_counter() - t0
        t1 = time.perf_counter()
        parsed = ps.parse_resume(a.resume, cfg, client, model=a.model)
        t_parse = time.perf_counter() - t1
        t2 = time.perf_counter()
        evaluation, _notes = ev.evaluate(parsed.parse, cfg, client, model=a.model)
        t_eval = time.perf_counter() - t2
        wall = time.perf_counter() - t0
        totals.append(wall)

        lat = [d for _, d in CALLS]
        stages = {}
        for tag, d in CALLS:
            stages[tag] = stages.get(tag, 0.0) + d
        print(f"\n=== 第 {i + 1} 遍（{os.path.basename(a.resume)} × {cfg.job_id}，模型 {a.model}）===")
        print(f"  总耗时        {wall:6.1f}s   （读取 {t_ext:.2f}s + 解析 {t_parse:.1f}s + 评估 {t_eval:.1f}s）")
        print(f"  LLM 调用      {len(CALLS)} 次，纯调用合计 {sum(lat):.1f}s"
              f"（占 {sum(lat) / wall * 100:.0f}%）")
        if lat:
            print(f"  单次延迟      中位 {statistics.median(lat):.1f}s · 最快 {min(lat):.1f}s · 最慢 {max(lat):.1f}s")
        print("  分阶段调用    " + " · ".join(f"{k}×{sum(1 for t, _ in CALLS if t == k)}"
                                              f"({v:.1f}s)" for k, v in sorted(stages.items(), key=lambda x: -x[1])))
        print(f"  判定          {evaluation.verdict}/{evaluation.decision}"
              f"  规则分 {evaluation.base_total}"
              f"  证据 {len(evaluation.cited_evidence)} 条 · 未归位 {len(evaluation.unmapped_items)} 项")

    if a.repeat > 1:
        print(f"\n=== {a.repeat} 遍汇总：中位 {statistics.median(totals):.0f}s · "
              f"范围 {min(totals):.0f}~{max(totals):.0f}s ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
