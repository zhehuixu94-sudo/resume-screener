# -*- coding: utf-8 -*-
"""存储层：连接 + 建表 + SQL 执行的小封装。

DSN 来源优先级：环境变量 `PG_DSN` → 由 `PGUSER/PGPASSWORD/PGHOST/PGPORT/PGDATABASE` 拼 →
本机密钥文件（开发期，见 app/llm/client.py 的同一套做法）。
"""
from __future__ import annotations

import os
import re
from contextlib import contextmanager
from typing import Any, Iterator, Sequence

SCHEMA_SQL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "schema.sql")
KEY_FILE = os.environ.get("SILICONFLOW_ENV_FILE", r"D:\Hermes\secrets\openclaw.env")

DEFAULTS = {"host": "127.0.0.1", "port": "5432", "user": "resume_app", "dbname": "resume_screener"}


def _from_key_file(name: str) -> str | None:
    if not os.path.exists(KEY_FILE):
        return None
    m = re.search(rf"^{name}\s*=\s*([^\s#]+)", open(KEY_FILE, encoding="utf-8", errors="replace").read(), re.M)
    return m.group(1).strip().strip('"') if m else None


def dsn() -> str:
    """DSN 来源：PG_DSN 环境变量 → PGHOST/PGPORT/PGUSER/PGPASSWORD/PGDATABASE（env 或本机密钥文件）。"""
    if os.environ.get("PG_DSN"):
        return os.environ["PG_DSN"]

    def pick(name: str, default: str) -> str:
        return os.environ.get(name) or _from_key_file(name) or default

    pw = os.environ.get("PGPASSWORD") or _from_key_file("PG_PASSWORD") or ""
    host = pick("PGHOST", DEFAULTS["host"])
    port = pick("PGPORT", DEFAULTS["port"])
    user = pick("PGUSER", DEFAULTS["user"])
    db = pick("PGDATABASE", DEFAULTS["dbname"])
    auth = f"{user}:{pw}@" if pw else f"{user}@"
    return f"postgresql://{auth}{host}:{port}/{db}"


def connect(autocommit: bool = False):
    """psycopg3 连接（延迟 import，没装驱动时报错更直白）。"""
    import psycopg

    return psycopg.connect(dsn(), autocommit=autocommit)


def init_schema(verbose: bool = True) -> list[str]:
    """建表（幂等）。整脚本一次执行——psycopg 支持多语句，这样 DO $$…$$ 块里的分号不会被拆坏。"""
    sql = open(SCHEMA_SQL, encoding="utf-8").read()
    with connect(autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(sql)
    def _head(stmt: str) -> str:
        body = "\n".join(l for l in stmt.splitlines() if not l.strip().startswith("--")).strip()
        return " ".join(body.split())[:70]

    heads = [h for h in (_head(stmt) for stmt in sql.split(";")) if h and "$$" not in h]
    if verbose:
        for h in heads:
            print(f"  ok: {h}" if h else "")
    return heads


@contextmanager
def cursor(autocommit: bool = False) -> Iterator[Any]:
    with connect(autocommit=autocommit) as conn:
        with conn.cursor() as cur:
            yield cur


def fetch_all(sql: str, params: Sequence[Any] = ()) -> list[dict]:
    with cursor() as cur:
        cur.execute(sql, params)
        cols = [d.name for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def fetch_one(sql: str, params: Sequence[Any] = ()) -> dict | None:
    rows = fetch_all(sql, params)
    return rows[0] if rows else None


def execute(sql: str, params: Sequence[Any] = (), *, returning: bool = False) -> dict | None:
    with cursor() as cur:
        cur.execute(sql, params)
        if returning:
            cols = [d.name for d in cur.description]
            row = cur.fetchone()
            return dict(zip(cols, row)) if row else None
    return None
