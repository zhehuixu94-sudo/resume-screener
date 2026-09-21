# -*- coding: utf-8 -*-
"""cv 方案端到端演练（**沙箱纪律版**：全程落在独立库 `resume_screener_sandbox`，生产库零写入）。

⚠️ 2026-09-15 事故后重写。旧版用「生产库 + 生产 job_id」，收尾拿"当前配置文件 re-upsert"还原——
   事后无法证明生产行没被改过（re-upsert 与"当前配置"逐对象相等，两种还原方式不可区分）。
   新版按 `tools/sandbox_guard.py` 的三条硬纪律执行：

   纪律 1  进程 DSN 切到独立库（`<生产库>_sandbox`，每次用 TEMPLATE 从生产库拷一份全新副本）；
           本脚本之后**连都不连**生产库，子进程（回灌、batch status）继承 `PG_DSN` 同样落沙箱。
   纪律 2  沙箱内收尾**逐字回写** job_config 行（禁止 re-upsert），打印操作前/回写后 sha256，不等即非零退出。
   纪律 3  生产侧起点/终点指纹（job_config 行原文 sha256 + config_hash、配置文件 sha256、快照目录）必须相等。

演练内容（与旧版一致，只是换了落地库）：
   1) 灌 3 条「确认漏放」+ 3 条「确认该淘汰」的合成复核结论 → 回灌 → 3 条 eligible；
   2) 拿**配置副本**（job_id 不变、路径在 out/）跑一次真 apply → 看见「cv0 旧名 + cv1 新名」并存；
   3) 沙箱内还原（逐字回写 + 删沙箱快照 + 复位抽检行 + 清 calibration 行）；
   4) 生产指纹前后比对；5) batch status 仍按 v2 过滤 = 310 行。

用法：
    .venv/Scripts/python.exe tools/cv_sandbox_demo.py            # 演练（默认从生产模板重建沙箱库，跑完即删沙箱库）
    .venv/Scripts/python.exe tools/cv_sandbox_demo.py --keep-db  # 保留沙箱库，便于反复调试
    .venv/Scripts/python.exe tools/cv_sandbox_demo.py --no-recreate --keep-db   # 复用已有沙箱库（最快）
沙箱库跑完就删（默认）：它是生产数据的第二份副本（含简历 PII），不留盘。
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import subprocess
import sys

sys.path.insert(0, ".")
try:
    sys.stdout.reconfigure(line_buffering=True)
except Exception:  # noqa: BLE001
    pass

from app import calibrate                                       # noqa: E402
from app.contracts import JobConfig                             # noqa: E402
from app.store import db                                        # noqa: E402
from tools import sandbox_guard as sg                           # noqa: E402
import tools.freeze_config_snapshot as fcs                      # noqa: E402

PY = sys.executable
JOB_FILE = "config/job_hr_admin.json"
PROD_SNAP_DIR = "config/snapshots"
SANDBOX_CFG = "out/sandbox_job_hr_admin.json"
SANDBOX_SNAP_DIR = "out/sandbox_snapshots"        # 沙箱快照不落生产证据目录
SNAP_SEED = "config/snapshots/hr_admin_v2-g2_scale25_budget25.json"   # 拷进沙箱目录当"旧名"，看并存
JOB_ID, VER = "hr_admin", "2"


def sh(*args: str) -> None:
    print(f"\n$ {' '.join(args)}")
    subprocess.run(list(args), check=False)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="cv 方案端到端演练（沙箱纪律版）")
    ap.add_argument("--no-recreate", action="store_true", help="复用已有沙箱库（默认每次从生产模板重建）")
    ap.add_argument("--keep-db", action="store_true", help="跑完保留沙箱库（默认删除：不留生产数据的副本）")
    a = ap.parse_args(argv)

    print("=" * 96)
    print("cv 方案端到端演练 · 沙箱纪律版（独立库 + 逐字回写 + 生产指纹前后比对）")
    print("=" * 96)

    # 0) 生产侧起点指纹（此刻进程还没切库，读的是生产）
    prod_dsn = db.dsn()
    prod_before = sg.prod_fingerprint(prod_dsn, JOB_FILE, PROD_SNAP_DIR, JOB_ID, VER)
    print(f"[起点·生产] 库 {sg._parse(prod_dsn)['db']} · job_config 行 sha256 {prod_before['row_sha'][:16]} "
          f"· config_hash {prod_before['row_hash']} · 配置文件 sha256 {prod_before['cfg_sha'][:16]}")
    print(f"[起点·生产] 快照目录 {list(prod_before['snaps'])}")

    # 纪律 1：切到独立库
    sb_dsn = sg.use_sandbox_db(recreate=not a.no_recreate)

    # 快照目录沙箱化：apply 写的快照落在 out/sandbox_snapshots，不动 config/snapshots
    os.makedirs(SANDBOX_SNAP_DIR, exist_ok=True)
    if os.path.exists(SNAP_SEED) and not os.path.exists(
            os.path.join(SANDBOX_SNAP_DIR, os.path.basename(SNAP_SEED))):
        shutil.copy(SNAP_SEED, SANDBOX_SNAP_DIR)
    fcs.SNAPSHOT_DIR = SANDBOX_SNAP_DIR
    print(f"[沙箱] 快照目录 → {SANDBOX_SNAP_DIR}（生产证据目录 config/snapshots 不写）")

    # 沙箱内回写基准：apply 前的 job_config 行原文
    cap = sg.capture_job_config(sb_dsn, JOB_ID, VER)
    print(f"[沙箱] 回写基准 job_config({JOB_ID}@{VER}) 原文 sha256 = {cap['sha'][:16]}…")

    # 复位抽检行（沙箱库）
    db.execute("""UPDATE spot_check_item SET review_verdict = '', reviewer = '', review_ts = NULL,
                         calibration_eligible = false, state = 'pending' WHERE job_id = %s""", (JOB_ID,))

    # 1) 3 条确认漏放（auto_reject 里分最高的）+ 3 条确认淘汰（分最低的）
    rows = db.fetch_all("""SELECT i.application_id, r.file_name, i.score FROM spot_check_item i
                             JOIN resume r USING(resume_id)
                            WHERE i.job_id = %s AND i.source = 'auto_reject'
                            ORDER BY i.score DESC""", (JOB_ID,))
    pos, neg = rows[:3], rows[-3:]
    with open("out/calibration_reviews_demo2.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["application_id", "review_verdict", "reviewer", "review_ts"])
        w.writeheader()
        for r in pos:
            w.writerow({"application_id": r["application_id"], "review_verdict": "keep",
                        "reviewer": "hr-演练", "review_ts": "2026-09-15T18:00:00"})
        for r in neg:
            w.writerow({"application_id": r["application_id"], "review_verdict": "reject",
                        "reviewer": "hr-演练", "review_ts": "2026-09-15T18:00:00"})
    print(f"\n[演练样本] 确认漏放 {[(r['file_name'], round(float(r['score']), 2)) for r in pos]}")
    print(f"           确认淘汰 {[(r['file_name'], round(float(r['score']), 2)) for r in neg]}")
    for r in pos + neg:
        keep = r in pos
        db.execute("""UPDATE spot_check_item SET review_verdict = %s, reviewer = 'hr-演练',
                             review_ts = '2026-09-15T18:00:00', state = %s
                       WHERE job_id = %s AND application_id = %s""",
                   ("keep" if keep else "reject", "passed" if keep else "rejected", JOB_ID, r["application_id"]))
    sh(PY, "tools/calibration_backfill.py", "--job", JOB_FILE, "--apply")

    # 2) 沙箱 apply：配置副本 + 真 apply（快照落沙箱目录）
    shutil.copyfile(JOB_FILE, SANDBOX_CFG)
    sandbox_cfg = JobConfig.model_validate(json.load(open(SANDBOX_CFG, encoding="utf-8")))
    print(f"\n[沙箱] 配置副本 {SANDBOX_CFG}（job_id={sandbox_cfg.job_id}@{sandbox_cfg.config_version} · "
          f"cv{sandbox_cfg.scoring.calibration_version}）")
    print(f"[沙箱] 起点快照名（cv=0 无 cv 段）：{fcs.snapshot_path(sandbox_cfg)}")
    res = calibrate.run(SANDBOX_CFG, trigger="manual", leak_budget=0.7)
    print(f"\n[沙箱 apply] status={res['status']} · 配置世代 v{res.get('config_version')} 不变 · "
          f"校准线版本 cv{res.get('from_calibration_version')} → cv{res.get('calibration_version')}")
    print(f"[沙箱 apply] 达标线 {res['before_line']} → {res['after_line']}（副本文件里的线，生产文件没动）")
    print(f"[沙箱 apply] 快照：{res.get('snapshot_path')}"
          f"（绑定 {res.get('snapshot_n')} 行结果 · 批次指纹 {res.get('batch_fingerprint')}）")
    after_apply = json.load(open(SANDBOX_CFG, encoding="utf-8"))
    print(f"[沙箱 apply] 副本 config_version={after_apply['config_version']} · "
          f"calibration_version={after_apply['scoring']['calibration_version']} · "
          f"pass_line={after_apply['scoring']['pass_line']}")
    print(f"\n[并存] 沙箱快照目录：{sorted(os.listdir(SANDBOX_SNAP_DIR))}")
    sn = res.get("snapshot_path")
    if sn and os.path.exists(sn):
        d = json.load(open(sn, encoding="utf-8"))
        print(f"[并存] cv1 快照 note：{d['note'][:150]}…")
        print(f"[并存] cv1 快照 result_pass_lines={d['metrics']['result_pass_lines']} · "
              f"当前配置线={d['config']['scoring']['pass_line']}")
        # 快照本身要落在沙箱目录（不污染生产证据目录）
        assert os.path.abspath(sn).startswith(os.path.abspath(SANDBOX_SNAP_DIR)), sn

    # 3) 沙箱内还原
    print("\n" + "-" * 96)
    sg.verbatim_restore_job_config(cap)                      # 纪律 2：逐字回写 + sha256 断言
    if sn and os.path.exists(sn):
        os.remove(sn)                                        # 沙箱自己产出的快照（可再生产物）
    if os.path.exists(SANDBOX_CFG):
        os.remove(SANDBOX_CFG)
    sg_rmtree_history = os.path.join("out", "history")   # config_patch 的备份目录（沙箱副本旁边，可再生产物）
    shutil.rmtree(sg_rmtree_history, ignore_errors=True)
    db.execute("""UPDATE spot_check_item SET review_verdict = '', reviewer = '', review_ts = NULL,
                         calibration_eligible = false, state = 'pending' WHERE job_id = %s""", (JOB_ID,))
    db.execute("DELETE FROM calibration WHERE job_id = %s", (JOB_ID,))
    back = sg.capture_job_config(sb_dsn, JOB_ID, VER)
    print(f"[沙箱还原] 抽检行复位 · calibration 行清空 · 快照目录 {sorted(os.listdir(SANDBOX_SNAP_DIR))}")

    # 4) 纪律 3：生产指纹前后比对
    prod_after = sg.prod_fingerprint(prod_dsn, JOB_FILE, PROD_SNAP_DIR, JOB_ID, VER)
    sg.assert_unchanged(prod_before, prod_after, label="生产状态（job_config 行/配置文件/快照目录）")

    # 5) 批次可见性：沙箱库里 batch status 仍按 v2 过滤
    sh(PY, "-m", "app.batch", "status", "--job", JOB_FILE)

    # 6) 沙箱库跑完即弃（不留生产数据的第二份副本）
    if not a.keep_db:
        sg.drop_sandbox_db(sb_dsn)

    print("\n" + "=" * 96)
    print(f"结论：沙箱纪律三条全部通过 —— 独立库 {sg._parse(sb_dsn)['db']} · 逐字回写 "
          f"{back['sha'][:16]} == {cap['sha'][:16]}：{back['sha'] == cap['sha']} · 生产零副作用")
    print("=" * 96)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
