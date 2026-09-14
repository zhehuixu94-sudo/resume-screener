# -*- coding: utf-8 -*-
"""Repository 层：所有读写走这里，业务代码不拼 SQL。

状态机（挂在 application 上）：
    pending_parse → parsing → parsed → evaluating → evaluated → reviewed
    任一环节出错 → parse_failed / eval_failed（记录 last_error，attempts+1，可重试）

断点续跑的实现：state 就是进度；worker 抢占用 `claim()` 里的原子 UPDATE，
同一行不会被两个进程同时拿到（`WHERE state = ANY(...) AND (locked_at IS NULL OR locked_at < now() - interval)`）。
幂等：唯一键 + ON CONFLICT，重跑同一份简历不会产生第二行评分。
"""
from __future__ import annotations

import hashlib
import json
import socket
import os
from typing import Any, Iterable, Sequence

from ..contracts import Evaluation, JobConfig
from ..contracts.resume_parse import ResumeParse
from . import db

TERMINAL_OK = ("evaluated", "reviewed")
# 可被 worker 抢占的状态：含 parsing/evaluating —— 进程被 kill 时行会停在中间态，
# 靠 locked_at 超时判定"原 worker 已死"才能安全回收（活着的 worker 持有较新的锁）。
RETRYABLE = ("pending_parse", "parsing", "parse_failed", "parsed", "evaluating", "eval_failed")
LOCK_TIMEOUT_MIN = 15          # 超过这个时间没更新的锁视为死锁（被 kill 的进程留下的）
MAX_ATTEMPTS = 3


def worker_name() -> str:
    return f"{socket.gethostname()}-{os.getpid()}"


def payload_hash(obj: Any) -> str:
    raw = obj.model_dump(mode="json", exclude_none=True) if hasattr(obj, "model_dump") else obj
    return hashlib.sha256(json.dumps(raw, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:16]


class Repository:
    def __init__(self, company_id: str = "local"):
        self.company_id = company_id
        self.worker = worker_name()

    # ------------------------------------------------------------ 岗位配置
    def upsert_job_config(self, cfg: JobConfig) -> str:
        row = db.execute(
            """INSERT INTO job_config (company_id, job_id, config_version, config_hash, job_title, payload)
               VALUES (%s, %s, %s, %s, %s, %s)
               ON CONFLICT (company_id, job_id, config_version)
               DO UPDATE SET payload = EXCLUDED.payload, config_hash = EXCLUDED.config_hash,
                             job_title = EXCLUDED.job_title
               RETURNING config_hash""",
            (self.company_id, cfg.job_id, cfg.config_version, payload_hash(cfg), cfg.job_title,
             json.dumps(cfg.model_dump(mode="json"), ensure_ascii=False)), returning=True)
        self.audit("upsert_job_config", "job_config", f"{cfg.job_id}@{cfg.config_version}",
                   {"job_title": cfg.job_title})
        return row["config_hash"] if row else ""

    # ------------------------------------------------------------ 简历与应聘
    def upsert_resume(self, parse: ResumeParse, file_path: str = "") -> str:
        db.execute(
            """INSERT INTO resume (resume_id, company_id, file_name, file_path, file_sha256, file_format,
                                   pages, char_count, text_sha256)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (resume_id) DO UPDATE
                 SET file_name = EXCLUDED.file_name, pages = EXCLUDED.pages,
                     file_path = COALESCE(EXCLUDED.file_path, resume.file_path),
                     char_count = EXCLUDED.char_count, updated_at = now()""",
            (parse.resume_id, self.company_id, parse.source.file_name, file_path or None,
             parse.source.file_sha256, parse.source.file_format, parse.source.pages, len(parse.text),
             parse.normalization.norm_sha256))
        return parse.resume_id

    def ensure_application(self, resume_id: str, cfg: JobConfig) -> int:
        row = db.execute(
            """INSERT INTO application (company_id, resume_id, job_id, config_version)
               VALUES (%s, %s, %s, %s)
               ON CONFLICT (company_id, resume_id, job_id, config_version) DO UPDATE
                 SET updated_at = now()
               RETURNING application_id""",
            (self.company_id, resume_id, cfg.job_id, cfg.config_version), returning=True)
        return int(row["application_id"])

    def save_parse(self, parse: ResumeParse) -> None:
        """解析结果幂等落库：resume_parse 一行 + evidence_span 全量覆盖。"""
        with db.cursor() as cur:
            cur.execute(
                """INSERT INTO resume_parse (resume_id, payload, model_id, prompt_version, prompt_hash,
                                             validation_state, gate_passed, evidence_total, evidence_anchored,
                                             evidence_coverage, text)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (resume_id) DO UPDATE
                     SET payload = EXCLUDED.payload, model_id = EXCLUDED.model_id,
                         prompt_version = EXCLUDED.prompt_version, prompt_hash = EXCLUDED.prompt_hash,
                         validation_state = EXCLUDED.validation_state, gate_passed = EXCLUDED.gate_passed,
                         evidence_total = EXCLUDED.evidence_total, evidence_anchored = EXCLUDED.evidence_anchored,
                         evidence_coverage = EXCLUDED.evidence_coverage, text = EXCLUDED.text,
                         created_at = now()""",
                (parse.resume_id, json.dumps(parse.model_dump(mode="json"), ensure_ascii=False),
                 parse.llm.model_id, parse.llm.prompt_version, parse.llm.prompt_hash,
                 parse.llm.validation.status, parse.quality.gate_passed, parse.stats.evidence_total,
                 parse.stats.evidence_anchored, parse.stats.evidence_coverage, parse.text))
            cur.execute("DELETE FROM evidence_span WHERE resume_id = %s", (parse.resume_id,))
            for e in parse.evidence:
                cur.execute(
                    """INSERT INTO evidence_span (span_id, resume_id, item_id, block_title, text,
                                                  start_offset, end_offset, page, method, similarity, anchored)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                    (e.span_id, parse.resume_id, e.item_id, e.block_title, e.text,
                     e.span.start if e.span else None, e.span.end if e.span else None,
                     e.page, e.anchoring.method, e.anchoring.similarity, e.anchored))

    def save_evaluation(self, application_id: int, ev: Evaluation) -> None:
        db.execute(
            """INSERT INTO evaluation (application_id, resume_id, job_id, organization_id, verdict, decision,
                                       base_total, gate_score, confidence, config_version, model_id, prompt_version,
                                       prompt_hash, aggregation, pass_line, payload)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (application_id) DO UPDATE
                 SET verdict = EXCLUDED.verdict, decision = EXCLUDED.decision, base_total = EXCLUDED.base_total,
                     gate_score = EXCLUDED.gate_score,
                     confidence = EXCLUDED.confidence, model_id = EXCLUDED.model_id,
                     prompt_version = EXCLUDED.prompt_version, prompt_hash = EXCLUDED.prompt_hash,
                     aggregation = EXCLUDED.aggregation, pass_line = EXCLUDED.pass_line,
                     payload = EXCLUDED.payload, created_at = now()""",
            (application_id, ev.resume_id, ev.job_id, self.company_id, ev.verdict, ev.decision,
             ev.base_total, ev.gate_score, ev.confidence, ev.job_config_version, ev.model_id, ev.prompt_version,
             ev.stages.compare.prompt_hash, ev.scoring_snapshot.aggregation, ev.scoring_snapshot.pass_line,
             json.dumps(ev.model_dump(mode="json"), ensure_ascii=False)))
        self.audit("save_evaluation", "application", str(application_id),
                   {"verdict": ev.verdict, "decision": ev.decision, "gate_score": ev.gate_score,
                    "base_total": ev.base_total},
                   ev.stages.compare.trace_ids[-1:])

    # ------------------------------------------------------------ 状态机
    def claim(self, cfg: JobConfig, *, limit: int = 1, states: Sequence[str] = RETRYABLE,
              max_attempts: int = MAX_ATTEMPTS, lock_timeout_min: int = LOCK_TIMEOUT_MIN) -> list[dict]:
        """原子抢占：把一批可处理的行标成 processing，并把返回状态交给调用方。

        同一行只会被一个 worker 拿到；被 kill 的进程留下的锁超过 LOCK_TIMEOUT_MIN 分钟后自动可抢占。
        """
        rows = db.fetch_all(
            """UPDATE application
                  SET worker = %s, locked_at = now(), updated_at = now()
                WHERE application_id IN (
                    SELECT application_id FROM application
                     WHERE company_id = %s AND job_id = %s AND config_version = %s
                       AND state = ANY(%s) AND attempts < %s
                       AND (locked_at IS NULL OR locked_at < now() - make_interval(mins => %s))
                     ORDER BY application_id
                     FOR UPDATE SKIP LOCKED
                     LIMIT %s)
            RETURNING application_id, resume_id, state, attempts""",
            (self.worker, self.company_id, cfg.job_id, cfg.config_version, list(states), max_attempts,
             lock_timeout_min, limit))
        return rows

    def set_state(self, application_id: int, state: str, *, error: str = "", release: bool = True) -> None:
        db.execute(
            """UPDATE application SET state = %s, last_error = %s, updated_at = now(),
                   worker = CASE WHEN %s THEN '' ELSE worker END,
                   locked_at = CASE WHEN %s THEN NULL ELSE locked_at END
               WHERE application_id = %s""",
            (state, error[:2000], release, release, application_id))

    def mark_failed(self, application_id: int, stage: str, error: str, *, max_attempts: int = MAX_ATTEMPTS) -> str:
        """记一次失败：attempts+1；达到上限就停在失败态，等人工处理（不再被反复重试）。"""
        state = "parse_failed" if stage == "parse" else "eval_failed"
        row = db.execute(
            """UPDATE application
                  SET state = %s, last_error = %s, attempts = attempts + 1, updated_at = now(),
                      worker = '', locked_at = NULL
                WHERE application_id = %s
            RETURNING attempts""",
            (state, error[:2000], application_id), returning=True)
        attempts = int((row or {}).get("attempts", 0))
        self.audit("stage_failed", "application", str(application_id),
                   {"stage": stage, "error": error[:500], "attempts": attempts,
                    "give_up": attempts >= max_attempts})
        return state

    def requeue_degraded(self, cfg: JobConfig) -> int:
        """把"评估里有关键阶段降级/失败"的行重新入队。

        为什么需要它：跑批时模型偶尔返回截断/非法 JSON（实测 100 份里 5 份的 compare 失败），
        这类行仍会以"降级"状态完成（置信度 0、强制转人工），不会被 reset_stuck 当成失败态回收；
        要修得靠重跑，但重跑前得先能把它挑出来。
        """
        row = db.execute(
            """WITH upd AS (
                   UPDATE application a
                      SET state = 'pending_parse', attempts = 0, last_error = '', worker = '', locked_at = NULL,
                          updated_at = now()
                    WHERE a.company_id = %s AND a.job_id = %s AND a.config_version = %s
                      AND EXISTS (
                          SELECT 1 FROM evaluation e
                           WHERE e.application_id = a.application_id
                             AND EXISTS (
                                 SELECT 1 FROM jsonb_each(e.payload -> 'stages') AS kv
                                  WHERE kv.value IS NOT NULL
                                    AND kv.value -> 'validation' ->> 'status' IN ('degraded', 'failed')))
                RETURNING 1)
               SELECT count(*) AS n FROM upd""",
            (self.company_id, cfg.job_id, cfg.config_version), returning=True)
        n = int(row["n"]) if row else 0
        self.audit("requeue_degraded", "job", f"{cfg.job_id}@{cfg.config_version}", {"n": n})
        return n

    def requeue_by_model(self, cfg: JobConfig, model: str) -> int:
        """把「评估里用过指定模型」的行重新入队（评估重跑）。

        用途：做同口径对照时，最怕整批里混着两个模型跑出来的行（实测踩过：批处理启动时探活
        挑中了主干 122B，82/100 份用它跑，另 18 份是降本档，整批对照直接作废）。
        用它可以只挑出「模型不对」的那些行重跑，不必整批重来。
        """
        row = db.execute(
            """WITH upd AS (
                   UPDATE application a
                      SET state = 'pending_parse', attempts = 0, last_error = '', worker = '', locked_at = NULL,
                          updated_at = now()
                    WHERE a.company_id = %s AND a.job_id = %s AND a.config_version = %s
                      AND EXISTS (SELECT 1 FROM evaluation e
                                   WHERE e.application_id = a.application_id
                                     AND EXISTS (SELECT 1 FROM jsonb_each(e.payload -> 'stages') AS kv
                                                  WHERE kv.value ->> 'model_id' = %s))
                RETURNING 1)
               SELECT count(*) AS n FROM upd""",
            (self.company_id, cfg.job_id, cfg.config_version, model), returning=True)
        n = int((row or {}).get("n", 0))
        self.audit("requeue_by_model", "job", f"{cfg.job_id}@{cfg.config_version}",
                   {"model": model, "n": n})
        return n

    def reset_all(self, cfg: JobConfig) -> int:
        """把该岗位该配置版本的所有行重新入队（解析结果保留，评估重跑）。

        用途：换模型/换权重做**同口径对照**时，必须整批重跑；而 --force 会把解析也重跑
        （解析是简历属性，与配置/模型无关，重跑纯属烧钱）。所以单独给一个入口。
        """
        row = db.execute(
            """WITH upd AS (
                   UPDATE application
                      SET state = 'pending_parse', attempts = 0, last_error = '', worker = '', locked_at = NULL,
                          updated_at = now()
                    WHERE company_id = %s AND job_id = %s AND config_version = %s
                RETURNING 1)
               SELECT count(*) AS n FROM upd""",
            (self.company_id, cfg.job_id, cfg.config_version), returning=True)
        n = int((row or {}).get("n", 0))
        self.audit("reset_all", "job", f"{cfg.job_id}@{cfg.config_version}", {"n": n})
        return n

    def reset_stuck(self, cfg: JobConfig, *, states: Sequence[str] = ("parsing", "evaluating"),
                    include_failed: bool = False, lock_timeout_min: int = LOCK_TIMEOUT_MIN) -> int:
        """把卡住/失败的行恢复成可重跑（清 attempts 与锁）——运维入口，供 kill 后或人工重试用。

        只回收**锁已过期**的中间态行：正在被 worker 处理的行（锁新鲜）绝不碰，
        否则会把别人手上的活抢下来重算（实测踩过：一次 retry 让 10 行被重复评估）。
        """
        target = list(states) + (["parse_failed", "eval_failed"] if include_failed else [])
        row = db.execute(
            """WITH upd AS (
                   UPDATE application
                      SET state = 'pending_parse', attempts = 0, last_error = '', worker = '', locked_at = NULL,
                          updated_at = now()
                    WHERE company_id = %s AND job_id = %s AND config_version = %s AND state = ANY(%s)
                      AND (locked_at IS NULL OR locked_at < now() - make_interval(mins => %s))
                RETURNING 1)
               SELECT count(*) AS n FROM upd""",
            (self.company_id, cfg.job_id, cfg.config_version, target, lock_timeout_min), returning=True)
        n = int((row or {}).get("n", 0))
        self.audit("reset_stuck", "job", f"{cfg.job_id}@{cfg.config_version}", {"states": target, "n": n})
        return n

    # ------------------------------------------------------------ 查询
    def progress(self, cfg: JobConfig) -> dict[str, int]:
        rows = db.fetch_all(
            """SELECT state, count(*) AS n FROM application
                WHERE company_id = %s AND job_id = %s AND config_version = %s GROUP BY state""",
            (self.company_id, cfg.job_id, cfg.config_version))
        return {r["state"]: int(r["n"]) for r in rows}

    def list_evaluations(self, cfg: JobConfig, decision: str | None = None) -> list[dict]:
        sql = """SELECT a.application_id, a.state, e.verdict, e.decision, e.gate_score, e.base_total,
                        e.confidence, e.payload, r.file_name, r.resume_id, a.attempts, a.last_error
                   FROM application a
                   JOIN resume r ON r.resume_id = a.resume_id
                   LEFT JOIN evaluation e ON e.application_id = a.application_id
                  WHERE a.company_id = %s AND a.job_id = %s AND a.config_version = %s"""
        params: list[Any] = [self.company_id, cfg.job_id, cfg.config_version]
        if decision:
            sql += " AND e.decision = %s"
            params.append(decision)
        sql += " ORDER BY e.gate_score DESC NULLS LAST, r.file_name"
        return db.fetch_all(sql, params)

    # ------------------------------------------------------------ 审计
    def audit(self, action: str, entity: str, entity_id: str, detail: dict | None = None,
              trace_ids: Iterable[str] = ()) -> None:
        db.execute(
            """INSERT INTO audit_log (actor, action, entity, entity_id, detail, trace_ids)
               VALUES (%s, %s, %s, %s, %s, %s)""",
            (self.worker, action, entity, entity_id, json.dumps(detail or {}, ensure_ascii=False),
             list(trace_ids)))
