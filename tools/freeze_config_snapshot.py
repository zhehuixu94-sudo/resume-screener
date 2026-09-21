# -*- coding: utf-8 -*-
"""**冻结配置快照**：任一生产口径变更，必须同批次冻结一份配置快照（否则视为未完成）。

为什么是纪律而不是可选步骤（实测踩过）：口径从「40 × 分位(Σ相关度×难度)」换成
「25 × 分位(Σq/√n)」时，`config_version` 没变——库里 310 行的 v2 分数是旧公式算的，
而配置里只有参数、没有公式版本，任何人拿 config_version=2 都复现不出历史分数。
version 号管**配置参数**，`scoring_semantics` 管**公式**，快照把两者 + 代码指纹 + 批次指纹钉在一起。

快照内容：
  · config              当前完整配置（原样，可直接 model_validate）
  · scoring_semantics   公式标记（contracts.scoring.scoring_semantics）
  · code_fingerprint    **算分链路源码的 sha256**（weighted_total/score/rules/honesty 等）——
                        公式再改一个字，指纹就变，想复现历史分数必须回到对应提交
  · batch_fingerprint   本批次入库结果的指纹：份数 + 每份 (file, gate_score, decision) 的 sha256
  · metrics             当批验收判据的实测值（放行/重叠/档位差/误放…）

两种用法（同一个函数，不重复实现）：
  · 命令行：python tools/freeze_config_snapshot.py --job config/job_hr_admin.json
  · 代码内：`from tools.freeze_config_snapshot import freeze, snapshot_path`
            —— `app.calibrate` 的 apply 就是这个用法：**线动了就必须同批次留下快照**，
            冻结失败由 apply 整体回滚（不允许「线动了快照没动」）。

⚠️ 本模块必须保持"可导入不产生副作用"：顶部不留任何裸语句，`freeze()` 只被显式调用时写文件。
（踩过：早期版本把逻辑写在模块顶层，`import tools.freeze_config_snapshot` 会直接跑一遍冻结。）
"""
from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
import re
import statistics as st
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # noqa: E402
from app.contracts import JobConfig                    # noqa: E402
from app.store import db                               # noqa: E402

CODE_FILES = ["app/pipeline/weighted_total.py", "app/pipeline/score.py", "app/pipeline/rules.py",
              "app/pipeline/honesty.py", "app/pipeline/rescore.py", "app/pipeline/decide_placeholder"]

SNAPSHOT_DIR = "config/snapshots"


def sha_file(path: str) -> str:
    if not os.path.exists(path):
        return ""
    return hashlib.sha256(open(path, "rb").read()).hexdigest()[:16]


def snapshot_path(cfg: JobConfig) -> str:
    """快照路径：按**现有命名规则**自动生成（不手工指定）。

    `<job>_v<config_version>-cv<calibration_version>-<scoring_semantics>.json`
    —— cv 段是**校准线版本**（`calibrate.apply` 每动一次线 +1；`config_version` 不动）。
    `calibration_version == 0`（从未校准）时**不写 cv 段**，沿用历史命名 `<job>_v<ver>-<sem>.json`，
    所以 v2 那份老快照的名字保持不动（历史证据不重命名）。
    """
    sem = cfg.scoring.scoring_semantics or "UNSET"
    cv = int(getattr(cfg.scoring, "calibration_version", 0) or 0)
    mid = f"-cv{cv}" if cv > 0 else ""
    return f"{SNAPSHOT_DIR}/{cfg.job_id}_v{cfg.config_version}{mid}-{sem}.json"


_NAME_RE = re.compile(r"^(?P<job>.+)_v(?P<ver>[^-]+)(?:-cv(?P<cv>\d+))?-(?P<sem>.+)\.json$")


def snapshot_key(path: str) -> tuple[str, int]:
    """从快照文件名解析 (config_version, calibration_version)；无 cv 段按 0 解释。

    给"取最新快照"用：**不要用字典序**（`cv10` 会排在 `cv2` 前面）。
    """
    m = _NAME_RE.match(os.path.basename(path))
    if not m:
        return ("", -1)
    return (m.group("ver") or "", int(m.group("cv") or 0))


def latest_snapshot(job_id: str, config_version: str, semantics: str = "",
                    directory: str = SNAPSHOT_DIR) -> str | None:
    """按 (config_version, calibration_version, mtime) 取最新的那份快照路径，没有则 None。

    semantics 非空时只认该公式标记的候选（换公式 ≠ 同一条证据链）。
    """
    best, best_key = None, None
    for p in glob.glob(os.path.join(directory, f"{job_id}_v{config_version}*.json")):
        try:
            snap = json.load(open(p, encoding="utf-8"))
        except Exception:                                       # noqa: BLE001
            continue
        if semantics and snap.get("scoring_semantics") != semantics:
            continue
        ver, cv = snapshot_key(p)
        if ver != config_version:
            continue
        key = (ver, cv, os.path.getmtime(p))
        if best_key is None or key > best_key:
            best, best_key = p, key
    return best


def build_snapshot(job_path: str, *, note: str = "") -> dict:
    """按当前配置文件 + 当批入库结果组装快照（不写文件）。"""
    cfg_raw = json.load(open(job_path, encoding="utf-8"))
    cfg = JobConfig.model_validate(cfg_raw)
    sem = cfg.scoring.scoring_semantics or "UNSET"

    rows = db.fetch_all(
        """SELECT r.file_name, r.resume_id, e.gate_score, e.decision, e.verdict, e.payload
             FROM evaluation e JOIN application ap USING(application_id)
             JOIN resume r ON r.resume_id = e.resume_id
            WHERE ap.job_id = %s AND ap.config_version = %s
            ORDER BY r.file_name""", (cfg.job_id, cfg.config_version))

    per = []
    for r in rows:
        per.append({"file": r["file_name"], "resume_id": r["resume_id"],
                    "gate_score": float(r["gate_score"] or 0), "decision": r["decision"],
                    "verdict": r["verdict"]})
    blob = json.dumps(per, ensure_ascii=False, sort_keys=True).encode("utf-8")
    batch_fp = hashlib.sha256(blob).hexdigest()[:16]

    # 这批结果的判定是用**哪条线**做的？动线（cv+1）之后冻结的同一批结果，指标反映的是**旧线**——
    # 必须写在明面上，否则快照会替新线背书一批旧线跑出来的数。
    line_rows = db.fetch_all(
        """SELECT DISTINCT e.pass_line FROM evaluation e JOIN application ap USING(application_id)
            WHERE ap.job_id = %s AND ap.config_version = %s""", (cfg.job_id, cfg.config_version))
    result_lines = sorted({float(r["pass_line"]) for r in line_rows if r["pass_line"] is not None})
    line_mismatch = bool(per) and (len(result_lines) != 1
                                   or abs((result_lines[0] if result_lines else 0)
                                          - float(cfg.scoring.pass_line)) > 1e-6)

    tier = {}
    mp = os.path.join("local", "samples_admin", "manifest.json")
    if os.path.exists(mp):
        tier = {m["file"]: m["tier"] for m in json.load(open(mp, encoding="utf-8"))}
    score = [x["gate_score"] for x in per]
    med = {t: st.median([x["gate_score"] for x in per if tier.get(x["file"]) == t]) if
           any(tier.get(x["file"]) == t for x in per) else None for t in (1, 2, 3, 4)}
    old = {}
    oldp = "out/回灌前_old_scores.csv"
    if os.path.exists(oldp):
        import csv as _c
        old = {r["file"]: float(r["old_total"]) for r in _c.DictReader(open(oldp, encoding="utf-8-sig"))}
    old_top = {f for f, _ in sorted(old.items(), key=lambda kv: -kv[1])[:max(1, len(old) // 5)]} if old else set()
    new_top = {x["file"] for x in sorted(per, key=lambda z: -z["gate_score"])[:max(1, len(per) // 5)]}
    metrics = {
        "n": len(per),
        "frozen_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "放行_线70": sum(1 for x in per if x["gate_score"] >= 70),
        "Top20%重叠": round(len(old_top & new_top) / len(old_top), 3) if old_top else None,
        "明显合格−边缘合格": round((med[1] or 0) - (med[2] or 0), 1) if med[1] and med[2] else None,
        "注水型中位": med[4], "明显不匹配中位": med[3],
        "新版误放_旧lt70_新ge70": (sum(1 for x in per if old.get(x["file"], 999) < 70 and x["gate_score"] >= 70)
                              if old else None),
        "处理分布": {d: sum(1 for x in per if x["decision"] == d)
                 for d in ("auto_pass", "manual_review", "auto_reject")},
        "score_median": round(st.median(score), 2) if score else None,
        "result_pass_lines": result_lines,
    }
    if not per:
        # 不许把"还没跑过结果"伪装成"跑出来是 0"：明确写进 note
        note = (note + "｜" if note else "") + (
            f"⚠️ 该版本（{cfg.config_version}）尚无入库结果：批次指纹是空表指纹，指标全部为空——"
            f"本快照冻结的是**配置与代码**，不是验收结论；该版本跑完批次后需再冻一次。")
    elif line_mismatch:
        # 动线之后冻结同一批结果：指标来自**旧线的判定**，必须说清楚
        note = (note + "｜" if note else "") + (
            f"⚠️ 这批结果的判定线是 {result_lines}，而当前配置线是 {cfg.scoring.pass_line}："
            f"下面的验收指标反映的是**旧线**的处置，动线后需重跑本批（app.batch rescore）指标才代表新线。")
    out = snapshot_path(cfg)
    snap = {
        "_what": "配置快照：算分口径变更的证据链（config + 公式标记 + 代码指纹 + 批次指纹 + 验收指标）",
        "_discipline": "任一生产口径变更必须同批次冻结本文件，否则视为未完成（tools/freeze_config_snapshot.py）",
        "job_id": cfg.job_id, "config_version": cfg.config_version, "scoring_semantics": sem,
        "note": note,
        "code_fingerprint": {f: sha_file(f) for f in CODE_FILES if os.path.exists(f)},
        "batch_fingerprint": batch_fp,
        "metrics": metrics,
        "config": cfg_raw,
    }
    return {"snapshot": snap, "path": out, "cfg": cfg, "batch_fingerprint": batch_fp, "n": len(per)}


def freeze(job_path: str, *, out: str = "", note: str = "", quiet: bool = False) -> dict:
    """冻结快照到磁盘。失败时**抛异常**（调用方负责回滚自己的变更）。"""
    built = build_snapshot(job_path, note=note)
    path = out or built["path"]
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    snap = built["snapshot"]
    with open(path, "w", encoding="utf-8") as f:
        json.dump(snap, f, ensure_ascii=False, indent=1)
    if not quiet:
        print(f"快照已冻结：{path}")
        print(f"  semantics={snap['scoring_semantics']} · config_version={snap['config_version']} · "
              f"批次指纹 {built['batch_fingerprint']} · {built['n']} 份")
        print("  代码指纹：" + " · ".join(f"{os.path.basename(k)}={v}" for k, v in snap["code_fingerprint"].items()))
        print("  验收指标：" + json.dumps(snap["metrics"], ensure_ascii=False))
    return {"path": path, "snapshot": snap, "batch_fingerprint": built["batch_fingerprint"],
            "n": built["n"], "config_version": snap["config_version"]}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="冻结配置快照（口径变更的证据链）")
    ap.add_argument("--job", default="config/job_hr_admin.json")
    ap.add_argument("--out", default="", help="默认按命名规则 config/snapshots/<job>_v<版本>-<语义>.json")
    ap.add_argument("--note", default="")
    a = ap.parse_args(argv)
    freeze(a.job, out=a.out, note=a.note)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
