# -*- coding: utf-8 -*-
"""快速体检：把样例简历按"硬条件关键词"过一遍，看这批样例里到底有几个够格当合格锚点。"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.text.extract import extract

PATTERNS = {
    "学历": r"(本科|专科|大专|硕士|研究生|博士)",
    "专业": r"(专业|学院|系)[：:\s]*([^\n，,;；]{2,20})",
    "四级": r"(四级|CET-?4|CET4|英语四级|六级|CET-?6)",
    "竞赛": r"(竞赛|比赛|大赛|CTF|ACM|NOIP|蓝桥杯|程序设计|信息学|挑战赛|三等奖|二等奖|一等奖|获奖|奖项)",
    "教培": r"(培训|教师|老师|助教|家教|教学|授课|辅导|班主任)",
}


def scan(path: str) -> dict:
    ex = extract(path)
    text = ex.text
    out = {"file": os.path.basename(path), "chars": len(text)}
    for name, pat in PATTERNS.items():
        hits = re.findall(pat, text)
        flat = []
        for h in hits[:6]:
            flat.append(" ".join(h) if isinstance(h, tuple) else h)
        out[name] = sorted(set(flat))
    m = re.search(r"(20\d\d[.\-/年]?\d{0,2}\s*[-—~至]\s*(20\d\d|至今|现在))", text)
    out["时间样例"] = m.group(0) if m else ""
    return out


SAMPLES = [
    r"C:\Users\user\Desktop\毕设及简历\示例候选人简历3.0.docx",
    r"C:\Users\user\Desktop\毕设及简历\resume_a.pdf",
    r"C:\Users\user\Desktop\毕设及简历\resume_b.pdf",
    r"C:\Users\user\Desktop\毕设及简历\个人简历.docx",
] + [f"local/samples/resume_{i:03d}.txt" for i in range(1, 7)]

for p in SAMPLES:
    if not os.path.exists(p):
        print(f"{os.path.basename(p)}: 文件不存在")
        continue
    r = scan(p)
    print(f"\n=== {r['file']}（{r['chars']} 字）===")
    for k in PATTERNS:
        print(f"  {k}: {r[k] or '—'}")
