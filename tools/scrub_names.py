# -*- coding: utf-8 -*-
"""把受控文件里的真实姓名换成 GitHub ID（用户要求不公开姓名）。"""
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(r"D:\GithubProgram\resume-screener")
OLD, NEW = "示例候选人", "zhehuixu94-sudo"

files = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True,
                       encoding="utf-8").stdout.split()
changed = []
for f in files:
    p = ROOT / f
    try:
        s = p.read_text(encoding="utf-8")
    except (UnicodeDecodeError, FileNotFoundError):
        continue
    if OLD in s:
        p.write_text(s.replace(OLD, NEW), encoding="utf-8")
        changed.append((f, s.count(OLD)))

print("已替换：", changed or "（无命中）")
left = subprocess.run(["git", "grep", "-n", OLD, "--", "."], cwd=ROOT,
                      capture_output=True, text=True, encoding="utf-8")
print("残留：", left.stdout.strip() or "无 ✅")
sys.exit(0 if not left.stdout.strip() else 1)
