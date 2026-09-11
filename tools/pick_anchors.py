# -*- coding: utf-8 -*-
"""在合成简历池里按硬条件筛"合格锚点候选"：本科（明写）+ 英语四级 + 计算机竞赛 + 对口专业。

只做关键词判定，和硬门槛规则层同一套口径，避免"筛样本"和"筛简历"两套标准。
用法：python tools/pick_anchors.py [--pool local/samples_all] [--need 8]
"""
from __future__ import annotations

import argparse
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.text.extract import extract  # noqa: E402

EDU = ["本科", "学士"]
EDU_FAIL = ["专科", "大专", "中专"]
CET = ["四级", "CET-4", "CET4", "英语四级", "六级", "CET-6", "雅思", "托福"]
CONTEST = ["程序设计竞赛", "程序设计大赛", "ACM", "ICPC", "蓝桥杯", "NOIP", "信息学奥赛", "算法竞赛",
           "编程竞赛", "竞赛", "大赛", "比赛"]
MAJOR = ["计算机科学与技术", "计算机科学", "软件工程", "网络工程", "网络空间安全", "信息安全",
         "数字媒体技术", "人工智能", "电子信息", "物联网", "数据科学"]


def hit(text: str, kws: list[str]) -> list[str]:
    low = text.lower()
    return [k for k in kws if k.lower() in low]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pool", default="local/samples_all")
    ap.add_argument("--need", type=int, default=8)
    a = ap.parse_args()

    rows = []
    for f in sorted(os.listdir(a.pool)):
        if not f.lower().endswith((".txt", ".pdf", ".docx")):
            continue
        path = os.path.join(a.pool, f)
        text = extract(path).text
        edu, cet, con, maj = hit(text, EDU), hit(text, CET), hit(text, CONTEST), hit(text, MAJOR)
        edu_bad = hit(text, EDU_FAIL)
        # 竞赛要有"获奖"信号才算数
        award = bool(re.search(r"(一等奖|二等奖|三等奖|优胜奖|金奖|银奖|省级|国赛|国家级|区域赛)", text))
        ok = bool(edu and cet and maj and con and award and not edu_bad)
        rows.append({"file": f, "ok": ok, "edu": edu[:2], "edu_fail": edu_bad[:2], "cet": cet[:2],
                     "major": maj[:2], "contest": con[:3], "award": award, "chars": len(text)})

    good = [r for r in rows if r["ok"]]
    print(f"池子：{len(rows)} 份 ｜ 同时满足【本科 + 四级 + 对口专业 + 竞赛获奖】的：{len(good)} 份")
    for r in good[:a.need]:
        print(f"  {r['file']:<16} 学历{r['edu']} 四级{r['cet']} 专业{r['major']} 竞赛{r['contest']} 有获奖={r['award']}")

    print("\n边缘样本（缺一项）：")
    for r in rows:
        if r["ok"]:
            continue
        missing = [k for k, v in (("学历", r["edu"]), ("四级", r["cet"]), ("专业", r["major"]),
                                  ("竞赛", r["contest"]), ("获奖", r["award"])) if not v]
        if len(missing) == 1 and not r["edu_fail"]:
            print(f"  {r['file']:<16} 缺：{missing} 专业{r['major']} 竞赛{r['contest']}")

    print("\n明确不合格样本（学历不符 / 专业不对口）：")
    bad = [r for r in rows if r["edu_fail"] or not r["major"]]
    for r in bad[:6]:
        print(f"  {r['file']:<16} 学历命中{r['edu_fail'] or r['edu']} 专业{r['major'] or '（无对口专业关键词）'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
