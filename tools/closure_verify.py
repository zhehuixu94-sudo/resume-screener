# -*- coding: utf-8 -*-
"""**收口校验（只读）**：证明生产状态自洽、且本轮沙箱演练没有留下实际残留。

为什么单独做一个工具（而不是跑几条 SQL 看一眼）：
    沙箱演练期间 `calibrate.apply` 会写 `job_config` 行（`app/calibrate.py:353`），
    还原方式一旦用「拿当前配置文件 re-upsert」，就**验不出**生产状态是否变过——
    因为 re-upsert 的结果与「当前配置」逐对象相等，事后无法区分。
    所以收口必须有一组**可反复跑、只读、有明确判据**的断言，而不是靠人回忆。

⚠️ 本工具**只读**：不写配置、不写库、不写快照、不改口径/线。任何一条 FAIL 都只报告，不自动修复。

用法：
    .venv/Scripts/python.exe tools/closure_verify.py --job config/job_hr_admin.json
    （退出码 0 = 全过；1 = 有 FAIL。方便接进 CI 或跑批后自检。）

判据清单（对应本轮收口口径）：
    C1 job_config 行自洽        config_hash == payload_hash(payload)
    C2 行 == 生产配置文件        契约对象逐字段相等（逐字回写没写歪）
    C3 引用完整性                application/evaluation 的 (job_id, config_version) 全部有唯一 job_config 行
    C4 运行期参数一致            evaluation.scoring_snapshot 与 job_config 行的线/带宽/版本一致
    C5 批次可见性                该版本状态机 == {'evaluated': N}（N 缺省 310）
    C6 生产口径值                pass_line / auto_pass_line / band / semantics / guard / auto_reject
    C7 快照证据链                内嵌 config 逐字一致 + 代码指纹相符 + 批次指纹与库里重算相符
    C8 沙箱无残留                calibration 表空、抽检行未复核、快照目录只剩生产那一份、沙箱副本已清

关于「config_hash 一致性」的一句话事实（**先说清楚前提**）：
    `application` 与 `evaluation` 表**没有 config_hash 列**——外键键值是 `config_version`
    （见 `app/store/schema.sql`：`application.config_version` / `evaluation.config_version`）。
    所以「job_config 的 hash 与 application/evaluation 引用的 hash 是否一致」在库里不存在这样的比对对象；
    等价且更强的判据是 C2+C3+C4 三条：行的 hash 与自身 payload 自洽、按版本无悬空引用、
    运行期真正落库的参数（scoring_snapshot）与行/文件一致。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # noqa: E402

from app.contracts import JobConfig                                  # noqa: E402
from app.store import db                                             # noqa: E402
from app.store.repo import payload_hash                              # noqa: E402
from tools.freeze_config_snapshot import CODE_FILES, sha_file, snapshot_path  # noqa: E402

SNAPSHOT_DIR = "config/snapshots"
RESULTS: list[tuple[str, bool, str]] = []


def check(cid: str, ok: bool, msg: str) -> None:
    RESULTS.append((cid, bool(ok), msg))
    print(f"[{'PASS' if ok else 'FAIL'}] {cid} {msg}")


# ------------------------------------------------------------------ C1 / C2
def c1c2(job_path: str) -> JobConfig:
    raw = json.load(open(job_path, encoding="utf-8"))
    cfg = JobConfig.model_validate(raw)
    row = db.fetch_one(
        """SELECT payload::text AS txt, config_hash FROM job_config
            WHERE job_id = %s AND config_version = %s""", (cfg.job_id, cfg.config_version))
    if not row:
        check("C1", False, f"job_config 无行：{cfg.job_id}@{cfg.config_version}")
        return cfg
    payload = json.loads(row["txt"])
    h_row = payload_hash(JobConfig.model_validate(payload))
    check("C1", h_row == row["config_hash"],
          f"job_config({cfg.job_id}@{cfg.config_version}) 自洽：config_hash={row['config_hash']} · "
          f"payload_hash={h_row}{'' if h_row == row['config_hash'] else ' ← 行内 hash 与 payload 不符（被改过）'}")

    diff = _walk(JobConfig.model_validate(payload).model_dump(mode="json"), cfg.model_dump(mode="json"))
    check("C2", not diff,
          f"行 == 生产配置文件（逐字段）{'；差异 ' + str(diff[:3]) if diff else ''}"
          + f" · payload::text sha256[:16]={hashlib.sha256(row['txt'].encode()).hexdigest()[:16]}")

    # 事实留痕（不算判据）：内容是否与「拿当前文件 re-upsert」的结果相同。
    reup = json.loads(json.dumps(cfg.model_dump(mode="json"), ensure_ascii=False))
    same = payload == reup
    print(f"        · 事实：行内容与「re-upsert 当前配置文件」的结果{'相同' if same else '不同'}"
          + ("（= 这两种还原方式在当前内容上不可区分；结论只能靠 C2 的「等价于生产文件」来支撑）"
             if same else "（= 行是历史原文，未被当前配置覆盖）"))
    return cfg


def _walk(x, y, path: str = "") -> list:
    out = []
    if isinstance(x, dict) and isinstance(y, dict):
        for k in sorted(set(x) | set(y)):
            out += _walk(x.get(k), y.get(k), f"{path}/{k}")
    elif isinstance(x, list) and isinstance(y, list):
        for i, (a, b) in enumerate(zip(x, y)):
            out += _walk(a, b, f"{path}[{i}]")
        if len(x) != len(y):
            out.append((path, f"len={len(x)}", f"len={len(y)}"))
    elif x != y:
        out.append((path, str(x)[:80], str(y)[:80]))
    return out


# ------------------------------------------------------------------ C3
def c3() -> None:
    dangling = db.fetch_all(
        """SELECT u.job_id, u.config_version, sum(u.n_app) AS n_app, sum(u.n_eval) AS n_eval
             FROM (SELECT job_id, config_version, count(*) AS n_app, 0 AS n_eval FROM application
                   GROUP BY 1, 2
                   UNION ALL
                   SELECT job_id, config_version, 0, count(*) FROM evaluation GROUP BY 1, 2) u
             LEFT JOIN job_config j USING (job_id, config_version)
            WHERE j.config_id IS NULL
            GROUP BY 1, 2""")
    check("C3", not dangling,
          "引用完整性：application/evaluation 的 (job_id, config_version) 全部命中 job_config"
          + (f"；悬空 {[dict(r) for r in dangling]}" if dangling else "")
          + "（注释：这两张表按 config_version 引用，不按 config_hash）")


# ------------------------------------------------------------------ C4 / C5
def c4c5(cfg: JobConfig, want_n: int) -> None:
    rows = db.fetch_all(
        """SELECT payload -> 'scoring_snapshot' AS snap, count(*) AS n
             FROM evaluation WHERE job_id = %s AND config_version = %s
            GROUP BY 1""", (cfg.job_id, cfg.config_version))
    distinct = {(json.dumps(r["snap"], sort_keys=True, ensure_ascii=False) if r["snap"] else "null"): r["n"]
                for r in rows}
    s = cfg.scoring
    ok_all = True
    detail = []
    for k, n in distinct.items():
        sw = json.loads(k)
        if not isinstance(sw, dict):
            ok_all = False
            detail.append(f"scoring_snapshot 缺失（{n} 行）")
            continue
        bad = []
        for field, expect in (("pass_line", float(s.pass_line)), ("pass_threshold", float(s.auto_pass_line)),
                              ("band_width", float(s.band_width)), ("config_version", cfg.config_version),
                              ("aggregation", s.dimension_aggregation), ("confidence_floor", float(s.confidence_floor)),
                              ("pass_line_source", s.pass_line_source)):
            if sw.get(field) != expect:
                bad.append(f"{field}: 行里 {sw.get(field)!r} ≠ 配置 {expect!r}")
        if bad:
            ok_all = False
            detail.append(f"{n} 行：{'; '.join(bad)}")
    check("C4", ok_all,
          f"运行期参数一致（{sum(distinct.values())} 行 · {len(distinct)} 种 scoring_snapshot）"
          + (f"；{detail}" if detail else
             f" · 线 {s.pass_line}/自动达标 {s.auto_pass_line}/带宽 {s.band_width}/世代 v{cfg.config_version}"))

    st = db.fetch_all(
        """SELECT a.state, count(*) AS n FROM application a
            WHERE a.job_id = %s AND a.config_version = %s GROUP BY 1 ORDER BY 2 DESC""",
        (cfg.job_id, cfg.config_version))
    dist = {r["state"]: r["n"] for r in st}
    check("C5", dist == {"evaluated": want_n},
          f"批次可见性（按当前配置文件的版本过滤）：{dist}（期望 {{'evaluated': {want_n}}}）")


# ------------------------------------------------------------------ C6
def c6(cfg: JobConfig) -> None:
    s = cfg.scoring
    g = s.guard_review
    ok = (float(s.pass_line) == 60.0 and float(s.auto_pass_line) == 70.0 and float(s.band_width) == 5.0
          and s.scoring_semantics == "g2_scale25_budget25" and s.auto_reject_enabled is True
          and int(s.calibration_version) == 0 and cfg.config_version == "2"
          and g.enabled is True and float(g.max_fluff_rate) == 0.35 and float(g.min_base) == 45.0)
    check("C6", ok,
          f"生产口径值：pass_line={s.pass_line} · auto_pass_line={s.auto_pass_line} · band={s.band_width} · "
          f"semantics={s.scoring_semantics} · cv={s.calibration_version} · config_version={cfg.config_version} · "
          f"guard_review={{enabled:{g.enabled}, max_fluff_rate:{g.max_fluff_rate}, min_base:{g.min_base}}} · "
          f"auto_reject_enabled={s.auto_reject_enabled}")


# ------------------------------------------------------------------ C7
def c7(cfg: JobConfig, job_path: str) -> None:
    path = snapshot_path(cfg)
    if not os.path.exists(path):
        check("C7", False, f"快照不存在：{path}（口径变更必须同批次冻结）")
        return
    snap = json.load(open(path, encoding="utf-8"))
    probs = []
    if snap.get("config") != json.load(open(job_path, encoding="utf-8")):
        probs.append("内嵌 config 与生产文件不一致")
    if snap.get("job_id") != cfg.job_id or str(snap.get("config_version")) != str(cfg.config_version):
        probs.append("快照 job/版本与配置不符")
    if snap.get("scoring_semantics") != cfg.scoring.scoring_semantics:
        probs.append("公式标记不符")
    fps = snap.get("code_fingerprint") or {}
    files = [f for f in CODE_FILES if os.path.exists(f)]
    if len(files) != 5:
        probs.append(f"算分链路源码文件数 {len(files)} ≠ 5（CODE_FILES 有悬空项？）")
    for f in files:
        if fps.get(f) != sha_file(f):
            probs.append(f"代码指纹漂移：{f} 快照 {fps.get(f)} ≠ 当前 {sha_file(f)}")
    # 批次指纹重算
    rows = db.fetch_all(
        """SELECT r.file_name, r.resume_id, e.gate_score, e.decision, e.verdict
             FROM evaluation e JOIN application ap USING (application_id)
             JOIN resume r ON r.resume_id = e.resume_id
            WHERE ap.job_id = %s AND ap.config_version = %s ORDER BY r.file_name""",
        (cfg.job_id, cfg.config_version))
    per = [{"file": r["file_name"], "resume_id": r["resume_id"], "gate_score": float(r["gate_score"] or 0),
            "decision": r["decision"], "verdict": r["verdict"]} for r in rows]
    fp = hashlib.sha256(json.dumps(per, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:16]
    if fp != snap.get("batch_fingerprint"):
        probs.append(f"批次指纹不符：快照 {snap.get('batch_fingerprint')} ≠ 重算 {fp}")
    if (snap.get("metrics") or {}).get("n") != len(per):
        probs.append(f"快照 metrics.n={(snap.get('metrics') or {}).get('n')} ≠ 库里 {len(per)} 行")
    check("C7", not probs,
          f"快照证据链 {os.path.basename(path)}：内嵌 config 逐字一致 · 5 个代码指纹相符 · "
          f"批次指纹 {fp}（n={len(per)}）" + (f"；问题：{probs}" if probs else ""))


# ------------------------------------------------------------------ C8
def c8(job_id: str) -> None:
    probs = []
    n_cal = db.fetch_one("SELECT count(*) AS n FROM calibration WHERE job_id = %s", (job_id,))["n"]
    if n_cal:
        probs.append(f"calibration 表残留 {n_cal} 行")
    sc = db.fetch_one(
        """SELECT count(*) AS n, count(*) FILTER (WHERE review_verdict <> '') AS done,
                  count(*) FILTER (WHERE calibration_eligible) AS elig,
                  count(*) FILTER (WHERE state = 'pending') AS pend
             FROM spot_check_item WHERE job_id = %s""", (job_id,))
    if sc["done"] or sc["elig"]:
        probs.append(f"抽检行已被写入结论：已复核 {sc['done']} · eligible {sc['elig']}")
    snaps = sorted(os.listdir(SNAPSHOT_DIR)) if os.path.isdir(SNAPSHOT_DIR) else []
    extra = [x for x in snaps if "cv" in x]
    if extra:
        probs.append(f"生产证据目录还有沙箱 cv 快照：{extra}")
    check("C8", not probs,
          f"生产侧无沙箱残留：calibration 0 行 · 抽检 {sc['n']} 行（未复核 {sc['pend']} / eligible {sc['elig']}）· "
          f"生产快照目录 {snaps}（cv 快照 0 份）" + (f"；问题：{probs}" if probs else ""))
    # 信息行（不算判据）：沙箱纪律版演练的产物应当落在沙箱命名空间里，而不是生产证据目录
    sandbox_files = sorted(p for p in ("out/sandbox_snapshots", "out/sandbox_job_hr_admin.json",
                                       "out/calibration_reviews_demo2.csv") if os.path.exists(p))
    dbs = db.fetch_all("SELECT datname FROM pg_database WHERE datname LIKE %s ORDER BY 1", ("%\\_sandbox",))
    print(f"        · 沙箱命名空间（预期存在，非残留）：{sandbox_files} · 沙箱库 {[r['datname'] for r in dbs]}")


def c9_sandbox_discipline() -> None:
    """沙箱纪律必须写进工具（下一轮 sandbox 脚本按新纪律执行）——检查守卫模块与其关键断言在位。"""
    import inspect
    from tools import sandbox_guard as sgmod
    src = inspect.getsource(sgmod)
    need = {
        "require/use_sandbox_db": "def use_sandbox_db",
        "独立库后缀": "_sandbox",
        "TEMPLATE 拷贝": "TEMPLATE",
        "逐字回写": "def verbatim_restore_job_config",
        "禁止 re-upsert 的说明": "re-upsert",
        "sha256 前后断言": "逐字回写后 sha256 不相等",
        "生产指纹比对": "def assert_unchanged",
    }
    miss = [k for k, v in need.items() if v not in src]
    demo = open("tools/cv_sandbox_demo.py", encoding="utf-8").read()
    if "use_sandbox_db" not in demo or "verbatim_restore_job_config" not in demo:
        miss.append("cv_sandbox_demo 未按新纪律改（须调用 use_sandbox_db / verbatim_restore_job_config）")
    if "shutil.copyfile(JOB_FILE, SANDBOX_CFG)" not in demo:
        miss.append("sandbox 配置副本机制缺失")
    check("C9", not miss, "沙箱纪律已机器化（tools/sandbox_guard.py + cv_sandbox_demo.py）"
          + (f"；缺 {miss}" if miss else " · 三条纪律 + 逐字回写 sha256 断言 + 生产指纹比对全部在位"))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="收口校验（只读）")
    ap.add_argument("--job", default="config/job_hr_admin.json")
    ap.add_argument("--want-n", type=int, default=310, help="该版本应有的入库份数（缺省 310）")
    a = ap.parse_args(argv)
    print("=" * 96)
    print(f"收口校验（只读）· job={a.job} · 期望 {a.want_n} 行")
    print("=" * 96)
    cfg = c1c2(a.job)
    c3()
    c4c5(cfg, a.want_n)
    c6(cfg)
    c7(cfg, a.job)
    c8(cfg.job_id)
    c9_sandbox_discipline()
    bad = [r for r in RESULTS if not r[1]]
    print("-" * 96)
    print(f"结果：{len(RESULTS) - len(bad)}/{len(RESULTS)} 通过" + (f"；FAIL = {[r[0] for r in bad]}" if bad else " · 全过"))
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
