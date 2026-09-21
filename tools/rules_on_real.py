# -*- coding: utf-8 -*-
"""把两个规则层放到真实简历上跑一遍（不调模型），看误报率。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.text.extract import extract
from app.text.fields import detect_anomalies, summarize
from app.text.normalize import normalize
from app.text.timeline import check_timeline
from app.text.timeline import summarize as tsum
from tools.sample_paths import resume_extra_files

# 本机简历路径一律走环境变量（RESUME_EXTRA_FILES），不写死在代码里 —— 见 tools/sample_paths.py
FILES = resume_extra_files(
    "local/samples/resume_001.txt",
    "local/samples/resume_003.txt",
)

for f in FILES:
    if not os.path.exists(f):
        print(f"{os.path.basename(f)}: 不存在")
        continue
    n = normalize(extract(f).text)
    a = detect_anomalies(n.text)
    tl = check_timeline(n.text)
    err, warn = summarize(a)
    print(f"\n{os.path.basename(f)}（{len(n.text)} 字）")
    print(f"  字段级异常：{err} 严重 / {warn} 提醒 ｜ 时间线：{tsum(tl)}")
    for x in a:
        print(f"    - [{x.severity}] {x.field}：{x.detail[:70]}")
    for i in tl.issues:
        print(f"    - [{i.kind}] {i.detail[:80]}")
