"""Jev (TypeSafe) on the same 100 synthetic resumes with the *identical*
question set and metrics used for Laya in probe2_literal.py, so the two
results are directly comparable.

Resume text: local/samples_all (synthetic corpus). No resume text is printed.
"""
import json
import re
import time
from pathlib import Path

from typesafe_sdk import Noul, TypeSafeClient


# --- 路径解析：可移植，不写死本机绝对路径（原脚本为 D:\... 硬编码）---
import os
_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parents[1]                       # experiments/jev/ → 仓库根
SAMPLES = Path(os.environ.get("SAMPLES_DIR", _REPO / "local" / "samples_all"))
OUT = Path(os.environ.get("PROBE_OUT", _HERE / "results"))
OUT.mkdir(parents=True, exist_ok=True)

SPEC = [
    ("scholarship", "这份简历中提到过奖学金", r"奖学金"),
    ("student_leader", "简历作者担任过学生干部（班长/部长/学生会/团支书/班委）", r"班长|学生会|部长|团支书|班委|学生干部"),
    ("competition", "这份简历中提到过竞赛获奖记录", r"竞赛|大赛|比赛|获奖|奖项"),
    ("java", "简历中出现了 Java 相关技术或项目", r"java"),
    ("ml_ai", "简历中出现机器学习或人工智能相关经历", r"机器学习|深度学习|人工智能|神经网络|算法工程师"),
    ("frontend", "简历中出现前端开发相关技术（HTML/CSS/Vue/React/小程序）", r"前端|HTML|CSS|Vue|React|小程序|JavaScript"),
    ("database", "简历中出现数据库相关技术（MySQL/SQL/Redis）", r"MySQL|mysql|SQL|sql|Redis|数据库"),
    ("english", "简历中标注了英语等级或英语考试成绩（四六级/雅思/托福）", r"四级|六级|CET|雅思|托福"),
]
SEC_WORDINGS = {
    "sec_a_literal": "这份简历中是否出现了网络安全的字面描述（安全服务、渗透测试、等保、安全运维、漏洞挖掘）",
    "sec_b_narrow": "简历作者是否有过安全方向的岗位实习经历",
    "sec_c_oud": "这份简历属于网络安全方向的候选人",
}
SEC_GT = re.compile(r"渗透|漏洞|CTF|ctf|网络安全|信息安全|安全服务|等保|SOC|WAF|免杀|逆向|抓包")


def ece(pairs, bins=10):
    if not pairs:
        return 0.0, []
    buckets = [[] for _ in range(bins)]
    for p, y in pairs:
        buckets[min(bins - 1, int(p * bins))].append((p, y))
    e, detail, n = 0.0, [], len(pairs)
    for i, b in enumerate(buckets):
        if not b:
            continue
        mp = sum(p for p, _ in b) / len(b)
        ac = sum(1 for _, y in b if y) / len(b)
        e += (len(b) / n) * abs(ac - mp)
        detail.append((i / bins, len(b), round(mp, 3), round(ac, 3), round(ac - mp, 3)))
    return e, detail


def main():
    files = sorted(SAMPLES.glob("resume_*.txt"))
    client = TypeSafeClient()
    qs = {qid: Noul(instructions=ins) for qid, ins, _ in SPEC}
    qs.update({qid: Noul(instructions=ins) for qid, ins in SEC_WORDINGS.items()})
    print(f"resumes={len(files)}  questions={len(qs)}  model=jev-latest", flush=True)

    rows, tin, tout, fails = [], 0, 0, 0
    t0 = time.time()
    for i, f in enumerate(files):
        text = f.read_text(encoding="utf-8", errors="ignore")[:6000]
        ts = time.time()
        try:
            r = client.system_one(text, qs)
        except Exception as e:
            fails += 1
            print(f"  FAIL {f.name}: {type(e).__name__} {str(e)[:120]}", flush=True)
            continue
        dt = (time.time() - ts) * 1000
        d = r.model_dump()
        ans = d["answers"]
        u = d.get("usage") or {}
        tin += int(u.get("input_tokens") or 0)
        tout += int(u.get("output_tokens") or 0)
        rec = {"file": f.name, "ms": round(dt)}
        for qid, _, pat in SPEC:
            rec[qid] = round(float(ans[qid]["noul"]), 4)
            rec[qid + "_gt"] = bool(re.search(pat, text, re.I))
        for qid in SEC_WORDINGS:
            rec[qid] = round(float(ans[qid]["noul"]), 4)
        rows.append(rec)
        if (i + 1) % 20 == 0:
            print(f"  {i+1}/{len(files)}  {time.time()-t0:.0f}s", flush=True)

    json.dump(rows, open(OUT / "jev_literal.json", "w"), ensure_ascii=False, indent=1)
    n = len(rows)
    if not n:
        print("no rows"); return

    print(f"\n=== per-question (same GT regex as Laya run) ===")
    print(f"{'question':14s} {'base':>6s} {'pred':>6s} {'acc':>6s} {'maj':>6s} {'hiErr':>6s} {'ECE':>6s}")
    all_pairs = []
    for qid, _, _ in SPEC:
        gt = [r[qid + "_gt"] for r in rows]
        p = [r[qid] for r in rows]
        base = sum(gt) / n
        predrate = sum(1 for x in p if x >= 0.5) / n
        acc = sum(1 for x, y in zip(p, gt) if (x >= 0.5) == y) / n
        maj = max(base, 1 - base)
        hi = sum(1 for x, y in zip(p, gt) if max(x, 1 - x) >= 0.9 and (x >= 0.5) != y) / n
        e, _ = ece(list(zip(p, gt)))
        all_pairs += list(zip(p, gt))
        print(f"{qid:14s} {base:6.2f} {predrate:6.2f} {acc:6.3f} {maj:6.3f} {hi:6.2f} {e:6.3f}")

    e_all, detail = ece(all_pairs)
    labs = [x for x, _ in all_pairs]
    ys = [y for _, y in all_pairs]
    acc_all = sum(1 for x, y in zip(labs, ys) if (x >= 0.5) == y) / len(all_pairs)
    print(f"\nOVERALL ({len(all_pairs)} decisions): accuracy={acc_all:.3f}  ECE={e_all:.3f}  mean_pred_rate={sum(labs)/len(labs):.3f}  true_rate={sum(1 for y in ys if y)/len(ys):.3f}")
    print(f"high-confidence (|p|>=0.9): ", end="")
    hc = [(x, y) for x, y in all_pairs if max(x, 1 - x) >= 0.9]
    hcw = sum(1 for x, y in hc if (x >= 0.5) != y)
    print(f"{len(hc)} decisions, wrong {hcw} ({hcw/max(1,len(hc)):.1%})")

    print("\n=== calibration table ===")
    print(f"{'bin':>5s} {'n':>5s} {'mean_p':>7s} {'actual':>7s} {'gap':>7s}")
    for lo, cnt, mp, ac, gap in detail:
        print(f"{lo:5.1f} {cnt:5d} {mp:7.3f} {ac:7.3f} {gap:+7.3f}")

    print("\n=== wording sensitivity for the security concept ===")
    for qid in SEC_WORDINGS:
        p = [r[qid] for r in rows]
        gt = [bool(SEC_GT.search(SAMPLES.joinpath(r["file"]).read_text(encoding="utf-8", errors="ignore"))) for r in rows]
        pos = sum(1 for x in p if x >= 0.5)
        acc = sum(1 for x, y in zip(p, gt) if (x >= 0.5) == y) / n
        print(f"  {qid:14s} positive={pos:3d}/100  mean_p={sum(p)/n:.3f}  acc_vs_regex={acc:.3f}")

    print(f"\nlatency per resume ({len(qs)} questions): mean {sum(r['ms'] for r in rows)/n:.0f} ms")
    print(f"tokens: input={tin} output={tout}  fails={fails}")
    print(f"cost: ${tin/1e6*0.042:.6f} input (+ output billed free)")


if __name__ == "__main__":
    main()
