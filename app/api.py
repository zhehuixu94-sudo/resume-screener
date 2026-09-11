# -*- coding: utf-8 -*-
"""Web API：把内核（解析/评估/规则层/报告）包成 HTTP 接口，给前端与 exe 用。

设计取向：
- 不做用户体系（v1 单公司单部署）：身份由 company_id 固定，接口不做鉴权——这是内网单机形态；
  要对外暴露必须先加鉴权（见 README「部署」）。
- 长任务（批量评估）跑在后台线程里，前端轮询进度；进度真相在数据库的状态机上，不在内存里。
- 报告直接返回已渲染好的 HTML（证据可点开），前端用 iframe 嵌。
"""
from __future__ import annotations

import glob
import json
import os
import shutil
import threading
import time
from typing import Any

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import __doc__ as _pkg_doc  # noqa: F401
from .contracts import Evaluation, JobConfig
from .contracts.resume_parse import ResumeParse
from .llm.client import SiliconFlowClient
from .report import render_matrix, render_report
from .store import Repository, db

from . import paths

ROOT = paths.ROOT
WEB_DIR = paths.WEB_DIR
UPLOAD_DIR = os.path.join(paths.data_dir(), "uploads")


def config_dir() -> str:
    """每次调用都现取：exe 旁边新增岗位配置后不用重启。"""
    return paths.config_dir()


CONFIG_DIR = config_dir()
SUPPORTED = (".pdf", ".docx", ".txt")

# 后台任务登记表（内存态仅用于"是否在跑"，进度真相在库里）
RUNS: dict[str, dict[str, Any]] = {}
_lock = threading.Lock()


# ---------------------------------------------------------------- 工具
def load_job(job_id: str) -> JobConfig:
    path = os.path.join(config_dir(), f"job_{job_id}.json")
    if not os.path.exists(path):
        raise HTTPException(404, f"岗位配置不存在：job_{job_id}.json")
    return JobConfig.model_validate(json.load(open(path, encoding="utf-8")))


def list_jobs() -> list[dict]:
    out = []
    for path in sorted(glob.glob(os.path.join(config_dir(), "job_*.json"))):
        try:
            cfg = JobConfig.model_validate(json.load(open(path, encoding="utf-8")))
        except Exception as e:                                     # noqa: BLE001
            out.append({"job_id": os.path.basename(path)[4:-5], "error": str(e)[:200]})
            continue
        repo = Repository(cfg.company_id)
        out.append({
            "job_id": cfg.job_id, "job_title": cfg.job_title, "config_version": cfg.config_version,
            "dimensions": [{"id": d.id, "name": d.name, "weight": d.weight} for d in cfg.dimensions],
            "hard_filters": [{"id": f.id, "name": f.name, "rule": f.rule} for f in cfg.hard_filters],
            "overrides": [{"id": o.id, "when_dimension": o.when_dimension, "min_points": o.min_points} for o in cfg.overrides],
            "pass_line": cfg.scoring.pass_line, "pass_line_source": cfg.scoring.pass_line_source,
            "band_width": cfg.scoring.band_width, "aggregation": cfg.scoring.dimension_aggregation,
            "labeling_repeats": cfg.scoring.labeling_repeats,
            "calibration": cfg.calibration.status,
            "progress": repo.progress(cfg),
        })
    return out


def _client() -> SiliconFlowClient:
    model = os.environ.get("RESUME_MODEL") or "Qwen/Qwen3.5-35B-A3B"
    c = SiliconFlowClient(model=model)
    return c


# ---------------------------------------------------------------- 应用
def create_app() -> FastAPI:
    app = FastAPI(title="简历初筛系统 API", version="0.2.0")

    @app.get("/api/health")
    def health() -> dict:
        try:
            row = db.fetch_one("SELECT current_database() AS db, version() AS v")
            counts = {t: db.fetch_one(f"SELECT count(*) AS n FROM {t}")["n"]
                      for t in ("resume", "application", "evaluation")}
            dbv = row["v"].split()[1] if row else "?"
            ok, err = True, ""
        except Exception as e:                                     # noqa: BLE001
            ok, err, counts, dbv = False, f"{type(e).__name__}: {e}", {}, ""

        client = None
        try:
            client = _client()
            model = client.model
        except Exception:                                          # noqa: BLE001
            model = os.environ.get("RESUME_MODEL", "Qwen/Qwen3.5-35B-A3B")
        return {"ok": ok, "db": {"reachable": ok, "version": dbv, "counts": counts, "error": err},
                "model": model, "config_dir": config_dir(), "runs": {k: v.get("status") for k, v in RUNS.items()}}

    @app.get("/api/jobs")
    def jobs() -> list[dict]:
        return list_jobs()

    @app.get("/api/jobs/{job_id}")
    def job_detail(job_id: str) -> dict:
        return next((j for j in list_jobs() if j["job_id"] == job_id), {"error": "not found"})

    @app.get("/api/jobs/{job_id}/evaluations")
    def evaluations(job_id: str, decision: str | None = None, limit: int = 500) -> dict:
        cfg = load_job(job_id)
        repo = Repository(cfg.company_id)
        rows = repo.list_evaluations(cfg, decision=decision)[:limit]
        out = []
        for r in rows:
            payload = r.get("payload") or {}
            ev = payload if isinstance(payload, dict) else {}
            dims = {d["id"]: d["points"] for d in (ev.get("dimensions") or [])}
            # 定向取证结论：哪些维度查过了、哪些确实没证据（前端要显式展示，不让 0 分默默过去）
            cov = {c["id"]: {"found": c.get("found"), "n_items": c.get("n_items", 0), "note": c.get("note", "")}
                   for c in (ev.get("coverage") or [])}
            out.append({
                "application_id": r["application_id"], "file_name": r["file_name"], "resume_id": r["resume_id"],
                "state": r["state"], "verdict": r.get("verdict"), "decision": r.get("decision"),
                "base_total": r.get("base_total"), "confidence": r.get("confidence"),
                "attempts": r.get("attempts"), "last_error": r.get("last_error") or "",
                "decision_reason": ev.get("decision_reason", ""),
                "conflicts": [c.get("type") for c in (ev.get("conflicts") or [])],
                "hard_filters": {h["id"]: h["result"] for h in (ev.get("hard_filters") or [])},
                "fit": (ev.get("fit") or {}).get("verdict"),
                "fit_ratio": (ev.get("fit") or {}).get("fit_ratio"),
                "dim_points": dims,
                "coverage": cov,
                "labeling": ev.get("labeling"),
                "questions": len(ev.get("questions") or []),
                "quality": ev.get("quality"),
            })
            if payload:
                parse_row = db.fetch_one("SELECT payload FROM resume_parse WHERE resume_id = %s", (r["resume_id"],))
                if parse_row:
                    p = parse_row["payload"]
                    out[-1]["timeline"] = p.get("quality", {}).get("timeline", {}).get("status")
                    out[-1]["field_anomalies"] = len(p.get("quality", {}).get("field_anomalies") or [])
                    out[-1]["evidence_coverage"] = p.get("stats", {}).get("evidence_coverage")
        return {"job_id": job_id, "config_version": cfg.config_version, "count": len(out), "rows": out,
                "progress": repo.progress(cfg)}

    @app.get("/api/applications/{application_id}/report", response_class=HTMLResponse)
    def report(application_id: int) -> HTMLResponse:
        row = db.fetch_one(
            """SELECT e.payload AS ev, a.resume_id, r.file_name
                 FROM application a JOIN evaluation e ON e.application_id = a.application_id
                 JOIN resume r ON r.resume_id = a.resume_id WHERE a.application_id = %s""",
            (application_id,))
        if not row:
            raise HTTPException(404, "没有这份评估结果")
        parse_row = db.fetch_one("SELECT payload FROM resume_parse WHERE resume_id = %s", (row["resume_id"],))
        ev = Evaluation.model_validate(row["ev"])
        parse = ResumeParse.model_validate(parse_row["payload"])
        return HTMLResponse(render_report(parse, ev, title=f"{row['file_name']} · 初筛报告"))

    @app.get("/api/jobs/{job_id}/matrix", response_class=HTMLResponse)
    def matrix(job_id: str) -> HTMLResponse:
        cfg = load_job(job_id)
        rows = Repository(cfg.company_id).list_evaluations(cfg)
        return HTMLResponse(render_matrix(rows, job_title=cfg.job_title))

    @app.post("/api/jobs/{job_id}/upload")
    async def upload(job_id: str, files: list[UploadFile] = File(...)) -> dict:
        cfg = load_job(job_id)
        os.makedirs(UPLOAD_DIR, exist_ok=True)
        saved, skipped = [], []
        for f in files:
            ext = os.path.splitext(f.filename or "")[1].lower()
            if ext not in SUPPORTED:
                skipped.append({"file": f.filename, "why": f"不支持的格式 {ext}（支持 pdf/docx/txt）"})
                continue
            dst = os.path.join(UPLOAD_DIR, os.path.basename(f.filename or f"upload{ext}"))
            with open(dst, "wb") as fh:
                shutil.copyfileobj(f.file, fh)
            saved.append(dst)
        from .batch import enqueue
        repo = Repository(cfg.company_id)
        repo.upsert_job_config(cfg)
        n = enqueue(cfg, saved, repo) if saved else 0
        return {"saved": len(saved), "enqueued": n, "skipped": skipped, "progress": repo.progress(cfg)}

    @app.post("/api/jobs/{job_id}/run")
    def run(job_id: str, workers: int = 3, limit: int | None = None, force: bool = False) -> dict:
        cfg = load_job(job_id)
        key = f"{cfg.job_id}@{cfg.config_version}"
        with _lock:
            if RUNS.get(key, {}).get("status") == "running":
                return {"started": False, "why": "已经在跑了", "run": RUNS[key]}
            RUNS[key] = {"status": "running", "started_at": time.strftime("%H:%M:%S"), "workers": workers,
                         "processed": 0, "log": []}

        def _work() -> None:
            from .batch import run as batch_run
            try:
                repo = Repository(cfg.company_id)
                repo.upsert_job_config(cfg)
                client = _client()
                stats = batch_run(cfg, repo, client, workers=workers, limit=limit,
                                  model=client.model, force=force, lock_timeout_min=15)
                RUNS[key].update(status="done", stats=stats, api_calls=client.calls,
                                 finished_at=time.strftime("%H:%M:%S"))
            except Exception as e:                                  # noqa: BLE001
                RUNS[key].update(status="failed", error=f"{type(e).__name__}: {e}")

        threading.Thread(target=_work, daemon=True).start()
        return {"started": True, "run": RUNS[key]}

    @app.get("/api/jobs/{job_id}/run-status")
    def run_status(job_id: str) -> dict:
        cfg = load_job(job_id)
        repo = Repository(cfg.company_id)
        key = f"{cfg.job_id}@{cfg.config_version}"
        return {"run": RUNS.get(key), "progress": repo.progress(cfg)}

    @app.post("/api/jobs/{job_id}/retry")
    def retry(job_id: str) -> dict:
        cfg = load_job(job_id)
        repo = Repository(cfg.company_id)
        n = repo.reset_stuck(cfg, include_failed=True)
        return {"reset": n, "progress": repo.progress(cfg)}

    if os.path.isdir(WEB_DIR):
        app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
    else:
        @app.get("/")
        def no_web() -> dict:
            return {"error": "前端资源缺失（web/ 没打进包）"}

    @app.exception_handler(Exception)
    async def on_error(request, exc):                              # noqa: ANN001
        return JSONResponse({"error": f"{type(exc).__name__}: {exc}"}, status_code=500)

    return app


app = create_app()
