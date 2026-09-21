# -*- coding: utf-8 -*-
"""证据文本源（离线共用）：把 span_id 解析成**真原文**，供 c_item / 基础项重算使用。

## 为什么要有这个模块（一次实测确认的缺陷）

原实现（`tools/joint_grid.py` 的 `c_of`）只用 `resume_parse.evidence` 一张表取文本，
取不到就兜底 `0.4`（＝最差档）。实测全库 4522 条计分条目：

| 情况 | 条数 | 占比 |
|---|---|---|
| `resume_parse` 与 `evaluation.cited_evidence` 都有 | 2675 | 59.2% |
| 仅 `resume_parse` 有 | 0 | 0% |
| **仅 `cited_evidence` 有（现被兜底成 0.4）** | **1847** | **40.8%** |
| 两张表都没有 | 0 | 0% |

且两张表同时存在同一 span_id 时 **2675/2675 逐字一致、0 例冲突**（`out/diag_text_source_conflict.py`）。
所以：`cited_evidence` 不是「另一个口径」，而是同一个 parser 产出的另一半 span——
取证段的 `evc_*`（1847 条，前缀 100% 为 `evc`）只落在 cited_evidence 里。

结论：**兜底 0.4 是测量 bug，不是保守取值**——它把「文本查不到」当成了「证据不具体」，
而这 1847 条里有 648 条真原文含数字/成果词，全被压到最差档。
修复后 c_item 取值发生变化的条目 816/4522（18.0%）。

## 用法

    from tools.evidence_source import span_text_map, c_of
    tmap = span_text_map(eval_payload, parse_payload)
    c = c_of(tmap.get(span_id, ""))
"""
from __future__ import annotations

from app.pipeline.honesty import NUM, RESULT

FALLBACK_C = 0.4      # 两侧都取不到文本时的兜底（全库实测 0 条，仅为防御）


def span_text_map(eval_payload: dict, parse_payload: dict | None) -> dict[str, str]:
    """span_id → 真原文。优先 `cited_evidence`（覆盖更全），`resume_parse.evidence` 补齐。

    两者重叠部分实测逐字一致，故先后顺序不影响结果，只按覆盖率选主源。
    """
    tmap: dict[str, str] = {}
    for sp in ((parse_payload or {}).get("evidence") or []):
        sid = sp.get("span_id") or sp.get("id")
        if sid:
            tmap[sid] = sp.get("text") or sp.get("content") or sp.get("quote") or ""
    for c in ((eval_payload or {}).get("cited_evidence") or []):
        sid = c.get("span_id") or c.get("id")
        if sid:
            t = c.get("text") or c.get("content") or c.get("quote") or ""
            if t or sid not in tmap:      # cited 有文本就覆盖（实测与 parse 一致）
                tmap[sid] = t
    return tmap


def c_of(text: str) -> float:
    """证据具体度 c_item ∈ {1.0, 0.7, 0.4}：数字+成果都有 → 1.0；有一项 → 0.7；都没有 → 0.4。

    ⚠️ 语义边界：这是在判「这条证据本身写没写清」，**不是**在判简历真假。
    一旦某一档在真实简历上被系统性压到 0.4，说明测量出了问题，先查文本源再谈改语义。
    """
    t = (text or "").strip()
    if not t:
        return FALLBACK_C
    num, res = bool(NUM.search(t)), bool(RESULT.search(t))
    return 1.0 if (num and res) else (0.7 if (num or res) else FALLBACK_C)


def load_span_maps(job_id: str = "hr_admin", config_version: str = "2"):
    """便捷入口：直接按 job/config_version 拉全库 (file_name, resume_id, Evaluation, tmap)。只读。"""
    from app.contracts import Evaluation
    from app.store import db

    rows = db.fetch_all(
        """SELECT r.file_name, r.resume_id, e.payload FROM evaluation e
             JOIN application a USING(application_id) JOIN resume r ON r.resume_id = e.resume_id
            WHERE a.job_id = %s AND a.config_version = %s""", (job_id, config_version))
    out = []
    for r in rows:
        pay = r["payload"]
        pr = db.fetch_one("SELECT payload FROM resume_parse WHERE resume_id = %s", (r["resume_id"],))
        out.append((r["file_name"], r["resume_id"], Evaluation.model_validate(pay),
                    span_text_map(pay, (pr or {}).get("payload"))))
    return out
