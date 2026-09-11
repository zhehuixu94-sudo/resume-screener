#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""导出 JSON Schema：Pydantic 是单一真相来源，schemas/*.schema.json 是它的产物。

用法：
  python tools/export_schemas.py            # 写 schemas/
  python tools/export_schemas.py --check    # 只校验是否与代码一致（CI/提交前用，漂移则退出码 1）

产物：
  schemas/resume_parse.schema.json   ① 简历解析契约
  schemas/job_config.schema.json     ② 岗位配置契约
  schemas/evaluation.schema.json     ③ 评估契约（含 ③④⑤ 段模型输出的 $defs）
  schemas/llm/<stage>_output.schema.json  五段模型输出的独立 schema（可用于 response_format.json_schema）
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pydantic import BaseModel  # noqa: E402

from app.contracts import CONTRACTS, LLM_STAGE_CONTRACTS, SCHEMA_VERSION  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCHEMA_DIR = os.path.join(ROOT, "schemas")
DRAFT = "https://json-schema.org/draft/2020-12/schema"
TITLES = {
    "resume_parse": "① ResumeParse —— 简历解析契约",
    "job_config": "② JobConfig —— 岗位配置契约",
    "evaluation": "③ Evaluation —— 评估契约（判定/理由/证据引用/置信度/维度分数/硬门槛/匹配度）",
}
NOTES = {
    "resume_parse": "偏移由代码锚定，模型只给原文片段；未锚定的证据不可高亮、不可作自动淘汰依据。",
    "job_config": "HR 侧唯一配置入口：硬门槛 + 维度锚点 + 加分项关键词 + 达标线/三段带 + 锚点标定。",
    "evaluation": "判定由规则层产出；自动处置不得建立在未锚定证据或降级输出之上（契约层强制）。",
}


def build(model: type[BaseModel], *, name: str, sid: str, title: str, note: str = "") -> dict:
    js = model.model_json_schema(ref_template="#/$defs/{model}")
    js = {k: v for k, v in js.items() if k not in ("$schema", "$id")}
    out = {"$schema": DRAFT, "$id": sid, "title": title}
    if note:
        out["description"] = note
    out.update(js)
    out["x-contract-version"] = SCHEMA_VERSION
    out["x-model"] = f"{model.__module__}.{model.__name__}"
    return out


def targets() -> dict[str, tuple[type[BaseModel], dict]]:
    t: dict[str, tuple[type[BaseModel], dict]] = {}
    for key, model in CONTRACTS.items():
        t[os.path.join(SCHEMA_DIR, f"{key}.schema.json")] = (
            model, {"name": key, "sid": f"resume-screener/schemas/{key}.schema.json",
                    "title": TITLES[key], "note": NOTES[key]})
    for key, model in LLM_STAGE_CONTRACTS.items():
        t[os.path.join(SCHEMA_DIR, "llm", f"{key}.schema.json")] = (
            model, {"name": key, "sid": f"resume-screener/schemas/llm/{key}.schema.json",
                    "title": f"模型输出契约 · {key}", "note": "喂给模型/校验模型输出用；改了模型就重新导出。"})
    return t


def main() -> int:
    check = "--check" in sys.argv
    drift, written = [], []
    for path, (model, meta) in targets().items():
        content = json.dumps(build(model, **meta), ensure_ascii=False, indent=2) + "\n"
        if check:
            old = open(path, encoding="utf-8").read() if os.path.exists(path) else None
            if old != content:
                drift.append(path)
            continue
        os.makedirs(os.path.dirname(path), exist_ok=True)
        open(path, "w", encoding="utf-8").write(content)
        written.append(os.path.relpath(path, ROOT))

    if check:
        if drift:
            print("[FAIL] 以下 schema 与代码不一致（跑 python tools/export_schemas.py 重新导出）：")
            for d in drift:
                print("  -", os.path.relpath(d, ROOT))
            return 1
        print(f"[OK] {len(targets())} 个 schema 与代码一致（契约版本 {SCHEMA_VERSION}）")
        return 0

    print(f"已导出 {len(written)} 个 schema（契约版本 {SCHEMA_VERSION}）：")
    for w in written:
        print("  -", w)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
