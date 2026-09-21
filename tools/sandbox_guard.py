# -*- coding: utf-8 -*-
"""**沙箱纪律守卫**：让「沙箱演练碰了生产库」这件事在代码层面不可能发生。

事故根因（2026-09-15 实测踩过）：
    沙箱演练（`tools/cv_sandbox_demo.py`）跑的是**生产库 + 生产 job_id**，
    `calibrate.apply` 会写 `job_config` 行（`app/calibrate.py:353`）；收尾"还原"用的是
    「拿当前配置文件 re-upsert」，于是事后**根本验不出**生产行有没有被改过——
    re-upsert 的结果与「当前配置」逐对象相等，两种还原方式在内容上不可区分
    （证据：`tools/closure_verify.py` 的 C2 会打印这行事实）。
    结论：纪律不能是"收尾还原得更仔细"，而必须是**沙箱压根不碰生产库**。

三条硬纪律（写进代码，不靠人记）：
    1. `use_sandbox_db()` —— 进程级把 DSN 切到**独立库**（库名必须以 `_sandbox` 结尾），
       缺库就用 `CREATE DATABASE <prod>_sandbox TEMPLATE <prod>` 拷一份（PG13+ / 本机 PG18 支持）。
       本进程之后**连都不会连**生产库；子进程（回灌、batch status…）继承 `PG_DSN`，同样落在沙箱。
    2. `verbatim_restore_job_config()` —— 收尾**逐字回写**（`UPDATE ... payload = <原文>::jsonb`），
       禁止 re-upsert；回写后打印「操作前 sha256 / 回写后 sha256」，不相等直接抛异常（脚本非零退出）。
    3. `assert_unchanged()` —— 生产侧起点/终点指纹（job_config 行、配置文件、快照目录）必须相等。

⚠️ 本模块必须"可导入不产生副作用"（同 `tools/freeze_config_snapshot.py` 的踩坑）：逻辑全在函数里。
"""
from __future__ import annotations

import hashlib
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # noqa: E402

from app.store.repo import payload_hash  # noqa: E402


class SandboxViolation(RuntimeError):
    """违反沙箱纪律（用生产库、回写不一致、生产指纹变化）——一律硬失败，不降级。"""


# --------------------------------------------------------------------- DSN
def _parse(dsn: str) -> dict:
    """把 DSN 拆成部分（只认 libpq URL；本项目的 DSN 一律是 postgresql://…）。"""
    import urllib.parse as up

    u = up.urlparse(dsn)
    if not u.scheme.startswith("postgres"):
        raise SandboxViolation(f"不是 PostgreSQL DSN：{dsn[:40]}…")
    return {"scheme": u.scheme, "user": up.unquote(u.username or ""), "pw": up.unquote(u.password or ""),
            "host": u.hostname or "127.0.0.1", "port": u.port or 5432,
            "db": (u.path or "/").lstrip("/") or "postgres"}


def _build(p: dict, db: str) -> str:
    import urllib.parse as up

    auth = f"{up.quote(p['user'])}:{up.quote(p['pw'])}@" if p["pw"] else (f"{up.quote(p['user'])}@" if p["user"] else "")
    return f"{p['scheme']}://{auth}{p['host']}:{p['port']}/{db}"


def sandbox_name(prod_db: str) -> str:
    return prod_db if prod_db.endswith("_sandbox") else f"{prod_db}_sandbox"


def is_sandbox_dsn(dsn: str) -> bool:
    return _parse(dsn)["db"].endswith("_sandbox")


def _connect(dsn: str, autocommit: bool = False):
    import psycopg

    return psycopg.connect(dsn, autocommit=autocommit)


def _rows(dsn: str, sql: str, params: tuple = ()) -> list[dict]:
    with _connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        cols = [d.name for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def _row(dsn: str, sql: str, params: tuple = ()) -> dict | None:
    r = _rows(dsn, sql, params)
    return r[0] if r else None


# --------------------------------------------------------------------- 纪律 1
def ensure_sandbox_db(*, recreate: bool = True, prod_dsn: str = "") -> str:
    """保证沙箱库存在（默认每次从生产库模板拷贝一份全新的），返回沙箱 DSN。**不连生产库改数据**。"""
    from app.store import db as appdb

    prod = prod_dsn or appdb.dsn()
    if is_sandbox_dsn(prod):
        raise SandboxViolation(f"当前 DSN 已经是沙箱库：{_parse(prod)['db']}（不要再套一层）")
    pp = _parse(prod)
    sb = _build(pp, sandbox_name(pp["db"]))
    admin = _build(pp, "postgres")                      # 维护库：建/删库在这里做，不碰生产数据
    sbdb, pdb = _parse(sb)["db"], pp["db"]
    if sbdb == pdb or not sbdb.endswith("_sandbox"):
        raise SandboxViolation(f"沙箱库名不合法：{sbdb}（必须以 _sandbox 结尾且 ≠ 生产库）")
    try:
        with _connect(admin, autocommit=True) as conn, conn.cursor() as cur:
            if recreate:
                cur.execute(f'DROP DATABASE IF EXISTS "{sbdb}" WITH (FORCE)')
            cur.execute(f"""SELECT 1 FROM pg_database WHERE datname = '{sbdb}'""")
            if not cur.fetchone():
                cur.execute(f'CREATE DATABASE "{sbdb}" TEMPLATE "{pdb}"')
            cur.execute(f"""SELECT count(*) FROM pg_database WHERE datname = '{sbdb}'""")
            assert cur.fetchone()[0] == 1
    except Exception as exc:                            # noqa: BLE001
        raise SandboxViolation(
            f"沙箱库 {sbdb} 准备失败（{type(exc).__name__}: {exc}）。"
            f"最常见原因：**有进程正连着生产库 {pdb}**，PostgreSQL 不许从有连接的模板拷贝；"
            f"关掉跑批/看门狗/其它工具后重跑，或先用命令行手工建库。") from exc
    return sb


def use_sandbox_db(*, recreate: bool = True) -> str:
    """切进程到沙箱库：设 `PG_DSN`（子进程继承；`app/store/db.dsn()` 优先读它）。返回沙箱 DSN。"""
    prod = None
    if os.environ.get("PG_DSN"):
        prod = os.environ["PG_DSN"]
    sb = ensure_sandbox_db(recreate=recreate, prod_dsn=prod or "")
    os.environ["PG_DSN"] = sb
    p = _parse(sb)
    print(f"[沙箱纪律] 进程 DSN → 沙箱库 {p['db']}（{p['host']}:{p['port']}）· 生产库本进程不再连接"
          + (f" · 来源 DSN 覆盖：{_parse(prod)['db']}" if prod else ""))
    return sb


def drop_sandbox_db(sandbox_dsn: str) -> None:
    """删掉沙箱库（跑完的默认动作）：不让**生产数据的第二份副本**（含简历 PII）留在盘上。

    只认 `_sandbox` 结尾的库名；生产库名一律拒绝（防手滑）。
    """
    p = _parse(sandbox_dsn)
    if not p["db"].endswith("_sandbox"):
        raise SandboxViolation(f"拒绝删除非沙箱库：{p['db']}")
    with _connect(_build(p, "postgres"), autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(f'DROP DATABASE IF EXISTS "{p["db"]}" WITH (FORCE)')
    print(f"[沙箱纪律] 已删除沙箱库 {p['db']}（生产数据的副本不留盘，跑完即弃）")


# --------------------------------------------------------------------- 纪律 2
def sha256_text(t: str) -> str:
    return hashlib.sha256(t.encode("utf-8")).hexdigest()


def sha256_file(path: str) -> str:
    return hashlib.sha256(open(path, "rb").read()).hexdigest() if os.path.exists(path) else "-"


def capture_job_config(dsn: str, job_id: str, config_version: str, company_id: str = "local") -> dict:
    """抓 job_config 行的**原文文本**（回写要比的就是它）。"""
    r = _row(dsn, """SELECT payload::text AS txt, config_hash FROM job_config
                      WHERE company_id = %s AND job_id = %s AND config_version = %s""",
             (company_id, job_id, config_version))
    if not r:
        raise SandboxViolation(f"job_config 无行：{job_id}@{config_version}（库 {_parse(dsn)['db']}）")
    return {"dsn": dsn, "job_id": job_id, "config_version": config_version, "company_id": company_id,
            "text": r["txt"], "sha": sha256_text(r["txt"]), "config_hash": r["config_hash"]}


def verbatim_restore_job_config(cap: dict) -> dict:
    """**逐字回写** job_config 行（禁止 re-upsert 当前配置文件），并断言前后 sha256 相等。

    为什么必须逐字：re-upsert 会拿"当前配置对象"重新序列化，可能带上本轮新增字段/丢掉历史字段，
    于是"演练前后生产状态未变"这件事永远验不出 True（实测踩过）。
    """
    dsn = cap["dsn"]
    before = sha256_text(cap["text"])
    with _connect(dsn, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("""UPDATE job_config SET payload = %s::jsonb, config_hash = %s
                        WHERE company_id = %s AND job_id = %s AND config_version = %s""",
                    (cap["text"], cap["config_hash"], cap["company_id"], cap["job_id"], cap["config_version"]))
        if cur.rowcount != 1:
            raise SandboxViolation(f"逐字回写影响了 {cur.rowcount} 行（应为 1）：{cap['job_id']}@{cap['config_version']}")
    after_txt = _row(dsn, """SELECT payload::text AS txt FROM job_config
                              WHERE company_id = %s AND job_id = %s AND config_version = %s""",
                     (cap["company_id"], cap["job_id"], cap["config_version"]))["txt"]
    after = sha256_text(after_txt)
    print(f"[沙箱纪律·逐字回写] job_config({cap['job_id']}@{cap['config_version']})")
    print(f"    操作前 payload sha256 = {before}")
    print(f"    回写后 payload sha256 = {after}")
    print(f"    相等：{before == after}"
          + ("  ✅" if before == after else "  ❌ 回写不一致——脚本必须以非零退出"))
    if before != after:
        raise SandboxViolation("逐字回写后 sha256 不相等：job_config 行没回到操作前的原文")
    # 额外一条：回写后行内 config_hash 必须与 payload 自洽（防止 hand-written SQL 写歪）
    from app.contracts import JobConfig
    import json as _j
    h = payload_hash(JobConfig.model_validate(_j.loads(after_txt)))
    if h != cap["config_hash"]:
        raise SandboxViolation(f"回写后 config_hash 与 payload 不自洽：{cap['config_hash']} ≠ {h}")
    return {"before": before, "after": after, "config_hash": cap["config_hash"]}


# --------------------------------------------------------------------- 纪律 3
def prod_fingerprint(dsn: str, config_path: str, snapshot_dir: str,
                     job_id: str, config_version: str) -> dict:
    """生产侧指纹：job_config 行原文 sha256 + config_hash、配置文件 sha256、快照目录（名→sha256）。"""
    r = _row(dsn, """SELECT payload::text AS txt, config_hash FROM job_config
                      WHERE job_id = %s AND config_version = %s""", (job_id, config_version)) or {}
    snaps = ({n: sha256_file(os.path.join(snapshot_dir, n)) for n in sorted(os.listdir(snapshot_dir))}
             if os.path.isdir(snapshot_dir) else {})
    return {"row_sha": sha256_text(r.get("txt") or ""), "row_hash": r.get("config_hash") or "",
            "cfg_sha": sha256_file(config_path), "snaps": snaps}


def assert_unchanged(before: dict, after: dict, label: str = "生产状态") -> None:
    """起点/终点指纹逐项比对；任何一项变化都抛异常（不打印"看起来没事"糊过去）。"""
    keys = sorted(set(before) | set(after))
    print(f"[沙箱纪律] {label} 前后比对：")
    bad = []
    for k in keys:
        same = before.get(k) == after.get(k)
        print(f"    {k:10s} {'一致' if same else '变了'}  {before.get(k)}"
              + ("" if same else f"  →  {after.get(k)}"))
        if not same:
            bad.append(k)
    if bad:
        raise SandboxViolation(f"{label}出现变化：{bad}（沙箱必须对生产零副作用）")
