"""Laya (open-source Jev-alike) probe on real Chinese resumes.

Privacy: never prints resume text. Only decisions + aggregate stats.
Ground truth for the literal questions comes from regex over the source file.
"""
import json
import re
import sys
import time
from pathlib import Path

# --- 路径解析：可移植，不写死本机绝对路径（原脚本为 D:\... 硬编码）---
import os
_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parents[1]                       # experiments/jev/ → 仓库根
SAMPLES = Path(os.environ.get("SAMPLES_DIR", _REPO / "local" / "samples_all"))
OUT = Path(os.environ.get("PROBE_OUT", _HERE / "results"))
OUT.mkdir(parents=True, exist_ok=True)
MODEL_PATH = os.environ.get("LAYA_MODEL_DIR", str(_HERE / "model"))   # Laya 权重目录（不入库，见 README）
SUBFOLDER = sys.argv[1] if len(sys.argv) > 1 else None  # None=english, 'multilingual'
if SUBFOLDER == "english":
    SUBFOLDER = None

import laya  # noqa: E402

QUESTIONS = {
    "has_internship": {
        "type": "noul",
        "instructions": "这份简历是否包含正式实习经历（在公司实习过，不是课程作业或校园项目）",
    },
    "has_security": {
        "type": "noul",
        "instructions": "这份简历是否包含网络安全相关的经历（渗透测试/漏洞挖掘/CTF/安全运维/等级保护/安全服务）",
    },
    "track": {
        "type": "choice",
        "instructions": "这位候选人最匹配哪条招聘方向",
        "criteria": {
            "security": "安全服务、渗透测试、安全运维、等保",
            "ops_dev": "运维开发、SRE、后端运维、自动化脚本",
            "app_dev": "应用开发、前后端业务开发",
            "other": "都不像，或信息不足以判断",
        },
    },
    "project_depth": {
        "type": "score",
        "instructions": "简历中项目经历的工程深度",
        "criteria": [
            "只有课程作业或跟着教程做的练习",
            "能独立完成一个有实际用途的项目",
            "有可衡量的产出（上线、用户数、性能数据、开源仓库）",
        ],
    },
}

# regex ground truth for the two noul questions
GT = {
    "has_internship": re.compile(r"实习|intern", re.I),
    "has_security": re.compile(
        r"渗透|漏洞|CTF|ctf|网络安全|信息安全|安全服务|等保|SOC|WAF|内网|免杀|逆向|抓包|漏洞挖掘",
        re.I,
    ),
}


def main():
    files = sorted(SAMPLES.glob("resume_*.txt"))
    print(f"samples: {len(files)}  subfolder={SUBFOLDER or 'english'}")
    import torch

    print("cuda available:", torch.cuda.is_available(), torch.cuda.get_device_name(0) if torch.cuda.is_available() else "")
    device = "cuda" if torch.cuda.is_available() else "cpu"
    t0 = time.time()
    agent = laya.load(MODEL_PATH, device=device, subfolder=SUBFOLDER)
    print(f"model loaded in {time.time() - t0:.1f}s on {agent.device}")
    print("cfg max_len:", agent.cfg.get("max_len"), " head_max_len:", agent.cfg.get("head_max_len"))

    # warmup + latency
    first = files[0].read_text(encoding="utf-8", errors="ignore")
    t0 = time.time()
    agent.system_one(first[:4000], {"track": QUESTIONS["track"]})
    print(f"single-question latency (cold): {1000 * (time.time() - t0):.0f} ms")

    rows = []
    t0 = time.time()
    for i, f in enumerate(files):
        text = f.read_text(encoding="utf-8", errors="ignore")[:6000]
        ts = time.time()
        res = agent.system_one(text, QUESTIONS)
        dt = time.time() - ts
        ans = res.get("answers", res)
        gt_intern = bool(GT["has_internship"].search(text))
        gt_sec = bool(GT["has_security"].search(text))
        row = {
            "file": f.name,
            "ms": round(dt * 1000),
            "intern_pred": round(float(ans["has_internship"]["noul"]), 3),
            "intern_gt": gt_intern,
            "sec_pred": round(float(ans["has_security"]["noul"]), 3),
            "sec_gt": gt_sec,
            "track": ans["track"]["choice"],
            "track_conf": round(float(ans["track"]["confidence"]), 3),
            "depth": round(float(ans["project_depth"]["score"]), 3),
            "depth_conf": round(float(ans["project_depth"]["confidence"]), 3),
        }
        rows.append(row)
        if (i + 1) % 10 == 0:
            print(f"  {i + 1}/{len(files)} done  ({time.time() - t0:.0f}s elapsed)")

    json.dump(rows, open(OUT / f"laya_{SUBFOLDER or 'english'}.json", "w"), ensure_ascii=False, indent=1)

    # ---- scoring ----
    def acc(pred_key, gt_key, thr=0.5):
        correct = sum(1 for r in rows if (r[pred_key] >= thr) == r[gt_key])
        return correct / len(rows)

    def allhigh(r):
        return 1.0

    print("\n=== literal-question accuracy (thr=0.5) ===")
    print(f"has_internship : {acc('intern_pred', 'intern_gt'):.3f}")
    print(f"has_security   : {acc('sec_pred', 'sec_gt'):.3f}")
    for thr in (0.5, 0.8, 0.9):
        cov = sum(1 for r in rows if r["sec_pred"] >= thr or r["sec_pred"] <= 1 - thr)
        cor = sum(
            1
            for r in rows
            if (r["sec_pred"] >= thr or r["sec_pred"] <= 1 - thr) and (r["sec_pred"] >= thr) == r["sec_gt"]
        )
        print(f"  has_security @conf>={thr}: coverage {cov}/{len(rows)}, acc {(cor / cov if cov else 0):.3f}")

    print("\n=== confident-but-wrong cases (sec_pred>=0.9 & wrong) ===")
    bad = [r for r in rows if r["sec_pred"] >= 0.9 and not r["sec_gt"]]
    print(f"count: {len(bad)}")
    for r in bad[:5]:
        print(f"  {r['file']} pred={r['sec_pred']} gt={r['sec_gt']}")

    print("\n=== track distribution ===")
    dist = {}
    for r in rows:
        dist[r["track"]] = dist.get(r["track"], 0) + 1
    print(dist)
    print(f"mean track confidence: {sum(r['track_conf'] for r in rows) / len(rows):.3f}")
    print(f"mean depth score     : {sum(r['depth'] for r in rows) / len(rows):.3f}")
    print(f"mean latency per resume (4 questions): {sum(r['ms'] for r in rows) / len(rows):.0f} ms")


if __name__ == "__main__":
    main()
