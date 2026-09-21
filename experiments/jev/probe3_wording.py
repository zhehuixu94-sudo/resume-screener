"""Fairness control: is the near-constant 'yes' caused by my Chinese statement
wording, or by the model itself?

Same 4 concepts, same 20 resumes, three wordings:
  A = Chinese statement   (as used in probe2)
  B = Chinese question
  C = English question with backticked field name (Laya's own preset style)
"""
import json
import re
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
import laya  # noqa: E402

CONCEPTS = [
    ("scholarship", r"奖学金"),
    ("ml_ai", r"机器学习|深度学习|人工智能|神经网络"),
    ("english", r"四级|六级|CET|雅思|托福"),
    ("competition", r"竞赛|大赛|比赛|获奖|奖项"),
]

Q_A = {  # Chinese statement (probe2 style)
    "scholarship": "这份简历中提到过奖学金",
    "ml_ai": "简历中出现机器学习或人工智能相关经历",
    "english": "简历中标注了英语等级或英语考试成绩（四六级/雅思/托福）",
    "competition": "这份简历中提到过竞赛获奖记录",
}
Q_B = {  # Chinese question
    "scholarship": "简历中是否提到过奖学金？",
    "ml_ai": "简历中是否出现机器学习或人工智能相关经历？",
    "english": "简历中是否标注了英语等级或考试成绩（四六级/雅思/托福）？",
    "competition": "简历中是否提到过竞赛获奖记录？",
}
Q_C = {  # English question, Laya preset style
    "scholarship": "Does `resume` mention a scholarship?",
    "ml_ai": "Does `resume` mention machine learning or AI experience?",
    "english": "Does `resume` state an English proficiency score such as CET-4/6, IELTS or TOEFL?",
    "competition": "Does `resume` mention winning a competition or receiving an award?",
}

VARIANTS = {"A_cn_statement": Q_A, "B_cn_question": Q_B, "C_en_question": Q_C}


def main():
    files = sorted(SAMPLES.glob("resume_*.txt"))[:20]
    agent = laya.load(MODEL_PATH, subfolder="multilingual")
    print("device:", agent.device, "| resumes:", len(files), flush=True)

    texts = [f.read_text(encoding="utf-8", errors="ignore")[:6000] for f in files]
    gt = {cid: [bool(re.search(pat, t, re.I)) for t in texts] for cid, pat in CONCEPTS}
    base = {cid: sum(v) / len(v) for cid, v in gt.items()}
    print("base rates: " + "  ".join(f"{c}={b:.2f}" for c, b in base.items()), flush=True)

    print(f"\n{'variant':16s} {'concept':12s} {'pos%':>6s} {'acc':>6s} {'expect_yes%':>11s}")
    out = {}
    for vname, qs in VARIANTS.items():
        payload = {cid: {"type": "noul", "instructions": ins} for cid, ins in qs.items()}
        preds = {cid: [] for cid, _ in CONCEPTS}
        for t in texts:
            a = agent.system_one(t, payload)["answers"]
            for cid, _pat in CONCEPTS:
                preds[cid].append(float(a[cid]["noul"]))
        for cid, _ in CONCEPTS:
            p = preds[cid]
            y = gt[cid]
            pos = sum(1 for x in p if x >= 0.5) / len(p)
            acc = sum(1 for x, yy in zip(p, y) if (x >= 0.5) == yy) / len(p)
            print(f"{vname:16s} {cid:12s} {100*pos:5.0f}% {acc:6.3f} {100*base[cid]:10.0f}%")
            out[f"{vname}|{cid}"] = {"positive_rate": round(pos, 3), "acc": round(acc, 3), "mean_p": round(sum(p) / len(p), 3)}
        print(flush=True)

    (OUT / "wording_control.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print("mean positive-rate per variant (should track base rate ~0.45):")
    for vname in VARIANTS:
        vals = [out[f"{vname}|{c}"]["positive_rate"] for c, _ in CONCEPTS]
        print(f"  {vname:16s} {sum(vals)/len(vals):.3f}")


if __name__ == "__main__":
    t0 = time.time()
    main()
    print(f"done in {time.time()-t0:.0f}s")
