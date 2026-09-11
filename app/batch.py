#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""批处理（M0 最小闭环）：入队 → 解析 → 评估 → 落库 → 出报告，支持并发与断点续跑。

用法：
  # 入队（把简历挂到岗位下，生成待处理行）
  python -m app.batch enqueue --job config/job_secops.json --resumes a.pdf b.docx --workers 1
  # 跑（可重复执行：已 evaluated 的行会跳过；被 kill 后重跑即续跑）
  python -m app.batch run --job config/job_secops.json --workers 3 --limit 10
  # 出报告
  python -m app.batch report --job config/job_secops.json --outdir out/reports
  # 一步到底
  python -m app.batch all --job config/job_secops.json --resumes ... --workers 3

断点续跑的语义：
  state 就是进度（pending_parse → parsed → evaluating → evaluated）；worker 抢占用原子 UPDATE +
  FOR UPDATE SKIP LOCKED，同一行不会被两个 worker 同时拿到；进程被 kill 留下的锁超过 15 分钟自动失效。
  重复执行不会产生第二行评分（唯一键 + ON CONFLICT）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

if __name__ == "__main__" and __package__ in (None, ""):        # 允许 python -m app.batch
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from .contracts import Evaluation, JobConfig                 # noqa: E402
from .contracts.resume_parse import ResumeParse              # noqa: E402
from .llm.client import SiliconFlowClient                    # noqa: E402
from .pipeline.evaluate import evaluate                      # noqa: E402
from .pipeline.parse import parse_resume                     # noqa: E402
from .report import render_matrix, render_report             # noqa: E402
from .store import Repository, db                            # noqa: E402

SUPPORTED = (".pdf", ".docx", ".txt")


# ---------------------------------------------------------------- 入队
def enqueue(job: JobConfig, paths: list[str], repo: Repository) -> int:
    added = 0
    for path in paths:
        ext = os.path.splitext(path)[1].lower()
        if ext not in SUPPORTED:
            print(f"  跳过（不支持的格式 {ext}）：{path}")
            continue
        sha = hashlib.sha256(open(path, "rb").read()).hexdigest()
        resume_id = sha[:16]
        db.execute(
            """INSERT INTO resume (resume_id, company_id, file_name, file_path, file_sha256, file_format)
               VALUES (%s, %s, %s, %s, %s, %s)
               ON CONFLICT (resume_id) DO UPDATE SET file_path = EXCLUDED.file_path""",
            (resume_id, repo.company_id, os.path.basename(path), os.path.abspath(path), sha, ext.lstrip(".")))
        repo.ensure_application(resume_id, job)
        added += 1
    return added


# ---------------------------------------------------------------- 单个任务
def process_one(row: dict, job: JobConfig, repo: Repository, client: SiliconFlowClient, *,
                model: str | None, force: bool = False) -> dict:
    """处理一行：状态机在库里推进，任一步失败都记录错误并停在可重试状态。"""
    app_id, resume_id, state = int(row["application_id"]), row["resume_id"], row["state"]
    out = {"application_id": app_id, "resume_id": resume_id, "state": state, "action": ""}

    # 1) 解析：只要还没拿到解析结果（包括被 kill 在 parsing 中途的）就重新解析
    parse: ResumeParse | None = None
    has_parse = db.fetch_one("SELECT 1 AS ok FROM resume_parse WHERE resume_id = %s", (resume_id,)) is not None
    # 解析是"简历的属性"，与岗位/配置无关：已有解析结果就复用（换配置只重跑评估，省一次调用、分数也可比）。
    # 要重解析用 --force；被 kill 在 parsing 中途、或解析失败的行因为 has_parse 为假会自动重解析。
    need_parse = force or not has_parse
    if need_parse:
        meta = db.fetch_one("SELECT file_path FROM resume WHERE resume_id = %s", (resume_id,))
        path = (meta or {}).get("file_path") or ""
        if not path or not os.path.exists(path):
            repo.mark_failed(app_id, "parse", f"原始文件不可访问：{path}")
            out.update(state="parse_failed", action="解析失败(文件缺失)")
            return out
        repo.set_state(app_id, "parsing", release=False)   # 处理中不释放锁，避免被别的 worker 抢走
        try:
            outcome = parse_resume(path, job, client, model=model)
        except Exception as e:                                   # noqa: BLE001
            repo.mark_failed(app_id, "parse", f"{type(e).__name__}: {e}")
            out.update(state="parse_failed", action="解析异常")
            return out
        if outcome.parse is None:
            repo.mark_failed(app_id, "parse",
                             "解析段降级/失败：" + "; ".join(outcome.result.meta.validation.errors[:3]))
            out.update(state="parse_failed", action="解析不合契约")
            return out
        parse = outcome.parse
        repo.upsert_resume(parse, file_path=path)
        repo.save_parse(parse)
        repo.set_state(app_id, "parsed", release=False)   # 锁留到自己手上，接着进评估阶段
        out["action"] = "已解析"
        state = "parsed"
    else:
        row_payload = db.fetch_one("SELECT payload FROM resume_parse WHERE resume_id = %s", (resume_id,))
        if not row_payload:
            repo.mark_failed(app_id, "parse", "库里没有解析结果，重新入队解析")
            out.update(state="parse_failed", action="缺解析结果")
            return out
        parse = ResumeParse.model_validate(row_payload["payload"])

    # 2) 评估（已评估且非 force → 跳过；但**存的是降级结果时不算"已完成"**，要重算）
    existing = db.fetch_one(
        """SELECT evaluation_id,
                  EXISTS (SELECT 1 FROM jsonb_each(payload -> 'stages') AS kv
                           WHERE kv.value IS NOT NULL
                             AND kv.value -> 'validation' ->> 'status' IN ('degraded', 'failed')) AS degraded
             FROM evaluation WHERE application_id = %s""", (app_id,))
    if existing and not force and not existing["degraded"]:
        repo.set_state(app_id, "evaluated")
        out.update(state="evaluated", action="已评估（跳过）")
        return out

    repo.set_state(app_id, "evaluating", release=False)
    try:
        ev, _notes = evaluate(parse, job, client, model=model)
    except Exception as e:                                       # noqa: BLE001
        repo.mark_failed(app_id, "eval", f"{type(e).__name__}: {e}")
        out.update(state="eval_failed", action="评估异常")
        return out
    repo.save_evaluation(app_id, ev)
    repo.set_state(app_id, "evaluated")
    out.update(state="evaluated", action=f"已评估：{ev.verdict}/{ev.decision} 分 {ev.base_total}")
    return out


# ---------------------------------------------------------------- 主流程
def run(job: JobConfig, repo: Repository, client: SiliconFlowClient, *, workers: int, limit: int | None,
        model: str | None, force: bool = False, lock_timeout_min: int = 15) -> dict[str, int]:
    stats: dict[str, int] = {}
    processed = 0
    while True:
        batch = min(workers, (limit - processed) if limit else workers)
        if batch <= 0:
            break
        rows = repo.claim(job, limit=batch, lock_timeout_min=lock_timeout_min)
        if not rows:
            break
        with ThreadPoolExecutor(max_workers=len(rows)) as pool:
            futures = [pool.submit(process_one, r, job, repo, client, model=model, force=force) for r in rows]
            for f in as_completed(futures):
                res = f.result()
                processed += 1
                key = f"{res['state']}"
                stats[key] = stats.get(key, 0) + 1
                print(f"  [{processed}] {res['resume_id']} → {res['action'] or res['state']}", flush=True)
    return stats


def reports(job: JobConfig, repo: Repository, outdir: str) -> list[str]:
    os.makedirs(outdir, exist_ok=True)
    rows = repo.list_evaluations(job)
    written = []
    for r in rows:
        if not r.get("payload"):
            continue
        ev = Evaluation.model_validate(r["payload"])
        parse_row = db.fetch_one("SELECT payload FROM resume_parse WHERE resume_id = %s", (r["resume_id"],))
        if not parse_row:
            continue
        parse = ResumeParse.model_validate(parse_row["payload"])
        path = os.path.join(outdir, f"{r['file_name']}.html")
        open(path, "w", encoding="utf-8").write(render_report(parse, ev))
        written.append(path)
    matrix = os.path.join(outdir, "矩阵.html")
    open(matrix, "w", encoding="utf-8").write(render_matrix(rows, job_title=job.job_title))
    written.append(matrix)
    return written


def main() -> int:
    ap = argparse.ArgumentParser(description="简历初筛批处理（M0 闭环）")
    ap.add_argument("cmd", choices=["enqueue", "run", "report", "all", "status", "retry",
                                    "requeue-degraded"])
    ap.add_argument("--job", default="config/job_secops.json")
    ap.add_argument("--resumes", nargs="*", default=None, help="简历文件/目录（目录会递归收集 pdf/docx/txt）")
    ap.add_argument("--workers", type=int, default=2, help="并发 worker 数（LLM 侧注意限流）")
    ap.add_argument("--limit", type=int, default=None, help="最多处理多少份（调试用）")
    ap.add_argument("--model", default=os.environ.get("RESUME_MODEL") or None,
                    help="模型（默认读 RESUME_MODEL 或内置主干；平台抖动时可切降本档）")
    ap.add_argument("--timeout", type=int, default=180, help="单次 LLM 调用超时秒数（批处理可调小）")
    ap.add_argument("--fallback-model", default="Qwen/Qwen3.5-35B-A3B",
                    help="主干探活失败时自动切换的模型（实测主干端点会整段时间无响应）")
    ap.add_argument("--outdir", default="out/reports")
    ap.add_argument("--force", action="store_true", help="忽略已有结果，强制重跑")
    ap.add_argument("--lock-timeout", type=int, default=15,
                    help="锁超时分钟数：超过它没更新的锁视为原 worker 已死，可被回收（kill 后立刻续跑可设 0）")
    a = ap.parse_args()

    job = JobConfig.model_validate(json.load(open(a.job, encoding="utf-8")))
    repo = Repository(job.company_id)
    repo.upsert_job_config(job)
    client = SiliconFlowClient(model=a.model, timeout=a.timeout) if a.model else SiliconFlowClient(timeout=a.timeout)

    # 开跑前探活：主干不可用就切降本档（把"白等 3×180s"变成一次 20s 探测）
    if a.cmd in ("run", "all") and not client.probe():
        if a.fallback_model and a.fallback_model != client.model:
            print(f"⚠️  主干 {client.model} 探活失败 → 自动切到 {a.fallback_model}")
            client.model = a.fallback_model

    paths: list[str] = []
    for item in (a.resumes or []):
        if os.path.isdir(item):
            for root, _dirs, files in os.walk(item):
                paths += [os.path.join(root, f) for f in sorted(files)
                          if os.path.splitext(f)[1].lower() in SUPPORTED]
        else:
            paths.append(item)

    if a.cmd in ("enqueue", "all"):
        if not paths:
            print("未提供 --resumes（文件或目录）")
            return 2
        n = enqueue(job, paths, repo)
        print(f"入队 {n} 份 → 岗位 {job.job_id}@{job.config_version}")
        print(f"当前状态机分布：{repo.progress(job)}")

    if a.cmd in ("run", "all"):
        print(f"开跑：workers={a.workers} limit={a.limit or '不限'} model={a.model or '默认'}")
        stats = run(job, repo, client, workers=a.workers, limit=a.limit, model=a.model, force=a.force,
                    lock_timeout_min=a.lock_timeout)
        print(f"本轮结束：{stats}")
        print(f"状态机分布：{repo.progress(job)}")
        print(f"累计 API 调用：{client.calls}")

    if a.cmd in ("report", "all"):
        written = reports(job, repo, a.outdir)
        print(f"报告 {len(written)} 个 → {a.outdir}")
        for w in written[:5]:
            print("  -", w)

    if a.cmd == "requeue-degraded":
        n = repo.requeue_degraded(job)
        print(f"已把 {n} 行『有关键阶段降级/失败』的重新入队 → 状态机分布：{repo.progress(job)}")
        return 0

    if a.cmd == "retry":
        # 只回收锁已过期的行：正在跑的行不动（用 --lock-timeout 控制"多久算过期"）
        n = repo.reset_stuck(job, include_failed=True, lock_timeout_min=a.lock_timeout)
        print(f"已重置 {n} 行为可重跑 → 状态机分布：{repo.progress(job)}")

    if a.cmd == "status":
        print(f"状态机分布：{repo.progress(job)}")
        for r in repo.list_evaluations(job):
            print(f"  {r['file_name']:<28} {r.get('state'):<12} {r.get('verdict') or '-':<6} "
                  f"{r.get('decision') or '-':<14} {r.get('base_total') if r.get('base_total') is not None else '-'}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\n被中断（Ctrl-C）。已完成的进度都在库里，直接重跑同一条命令即续跑。")
        raise SystemExit(130)
    except Exception:                                            # noqa: BLE001
        traceback.print_exc()
        raise SystemExit(1)
