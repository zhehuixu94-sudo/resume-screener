"""Does Jev move when the question wording moves?

This is the foundation of the "outer loop" (optimising wording / thresholds
instead of weights). If predictions barely shift across wordings, the outer
loop has nothing to optimise on.

3 concepts x 4 wordings = 12 noul questions, asked in ONE call per resume.
Same 100 synthetic resumes, same corrected ground truth as before.
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

CONCEPTS = {
    "database": (
        r"MySQL|SQL|Redis",
        {
            "base": "简历中出现数据库相关技术（MySQL/SQL/Redis）",
            "wide": "这份简历提到过任何与数据库相关的内容",
            "strict": "简历作者在项目里实际使用过关系型数据库（MySQL/SQL）进行开发，而不只是在课程里学过",
            "en": "Does `resume` mention database technologies such as MySQL, SQL or Redis?",
        },
    ),
    "competition": (
        r"竞赛|大赛|比赛|获奖|奖项",
        {
            "base": "这份简历中提到过竞赛获奖记录",
            "wide": "这份简历里出现过任何奖项、荣誉或比赛的字样",
            "strict": "简历作者在竞赛或比赛中实际获得了名次（不是参与奖、不是班级荣誉）",
            "en": "Does `resume` mention winning a competition or receiving an award?",
        },
    ),
    "student_leader": (
        r"班长|学生会|部长|团支书|班委|学生干部",
        {
            "base": "简历作者担任过学生干部（班长/部长/学生会/团支书/班委）",
            "wide": "这份简历里出现过任何校内职务或组织的字样",
            "strict": "简历作者担任过需要管理他人的学生职务（部长及以上、班长、团支书）",
            "en": "Did the author hold a student leadership position?",
        },
    ),
}


def main():
    files = sorted(SAMPLES.glob("resume_*.txt"))
    client = TypeSafeClient()

    qs, meta = {}, []
    for cid, (_pat, wordings) in CONCEPTS.items():
        for wname, ins in wordings.items():
            qid = f"{cid}__{wname}"
            qs[qid] = Noul(instructions=ins)
            meta.append((qid, cid, wname))

    print(f"resumes={len(files)}  questions={len(qs)} (3 concepts x 4 wordings, one call each)", flush=True)

    rows, tin, tout = [], 0, 0
    t0 = time.time()
    for i, f in enumerate(files):
        text = f.read_text(encoding="utf-8", errors="ignore")[:6000]
        r = client.system_one(text, qs)
        d = r.model_dump()
        u = d.get("usage") or {}
        tin += int(u.get("input_tokens") or 0)
        tout += int(u.get("output_tokens") or 0)
        rec = {"file": f.name}
        for qid, _c, _w in meta:
            rec[qid] = round(float(d["answers"][qid]["noul"]), 4)
        rows.append(rec)
        if (i + 1) % 25 == 0:
            print(f"  {i+1}/{len(files)}  {time.time()-t0:.0f}s", flush=True)

    json.dump(rows, open(OUT / "jev_sensitivity.json", "w"), ensure_ascii=False, indent=1)
    n = len(rows)

    print()
    for cid, (pat, wordings) in CONCEPTS.items():
        gt = [bool(re.search(pat, (SAMPLES / r["file"]).read_text(encoding="utf-8", errors="ignore"), re.I)) for r in rows]
        base = sum(gt) / n
        print(f"=== {cid}  (true rate {base:.2f}) ===")
        print(f"  {'wording':8s} {'mean_p':>7s} {'yes%':>6s} {'acc':>6s} {'ECE':>6s} {'spread':>7s}")
        vals = []
        for wname in wordings:
            p = [r[f"{cid}__{wname}"] for r in rows]
            vals.append(p)
            yes = sum(1 for x in p if x >= 0.5) / n
            acc = sum(1 for x, y in zip(p, gt) if (x >= 0.5) == y) / n
            # ECE
            bins = [[] for _ in range(10)]
            for x, y in zip(p, gt):
                bins[min(9, int(x * 10))].append((x, y))
            e = sum(len(b) / n * abs(sum(y for _, y in b) / len(b) - sum(x for x, _ in b) / len(b)) for b in bins if b)
            print(f"  {wname:8s} {sum(p)/n:7.3f} {100*yes:5.0f}% {acc:6.3f} {e:6.3f} {max(p)-min(p):7.3f}")
        # how much do the 4 wordings disagree per resume?
        disagree = sum(1 for i in range(n) if len({1 if v[i] >= 0.5 else 0 for v in vals}) > 1) / n
        maxspread = sum(max(v[i] for v in vals) - min(v[i] for v in vals) for i in range(n)) / n
        print(f"  -> resumes where wordings disagree on the answer: {100*disagree:.0f}%   mean per-resume spread: {maxspread:.3f}")
        print()

    print(f"tokens in={tin} out={tout}  cost=${tin/1e6*0.042:.6f}  wall={time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
