# -*- coding: utf-8 -*-
"""把受控文件里的真实姓名换成 GitHub ID（纪律：真实姓名不公开，统一用 GitHub ID）。

## 为什么姓名不进代码

名单本身也是 PII：把 `OLD = "真实姓名"` 写在源码里，等于把要保护的东西连同保护工具一起提交了。
所以名单放仓库外的 `local/pii_names.txt`（`.gitignore` 覆盖，只留 `.example` 模板）。

## 用法

    python tools/scrub_names.py                    # 默认读 local/pii_names.txt
    python tools/scrub_names.py --names <路径>
    python tools/scrub_names.py --check            # 只查不改（CI 用）

名单格式（每行 `原名<TAB>替换名`，`#` 开头为注释）：

    # local/pii_names.txt（**不进仓库**）
    张三	zhangsan-id

## 为什么名单缺失时必须报错退出

「名单文件不存在」时如果静默跳过，工具会打印"已替换：（无命中）"看着像跑成功了，
而真实姓名原封不动留在仓库里——**假成功比失败危险**。所以这里直接非零退出。
"""
from __future__ import annotations

import argparse
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent      # 仓库根：tools/ 的上一级
DEFAULT_NAMES = ROOT / "local" / "pii_names.txt"


def load_pairs(path: pathlib.Path) -> list[tuple[str, str]]:
    if not path.exists():
        sys.exit(
            f"❌ 姓名名单不存在：{path}\n"
            f"   名单属 PII，不能进仓库。请在本机创建（已 gitignore）：\n"
            f"       每行「原名<TAB>替换名」，或参考 local/pii_names.txt.example\n"
            f"   拒绝静默跳过：名单缺失时继续跑会假装清理成功，真名会留在仓库里。")
    pairs: list[tuple[str, str]] = []
    for ln, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split("\t") if p.strip()]
        if len(parts) != 2:
            sys.exit(f"❌ {path}:{ln} 格式错误（需 `原名<TAB>替换名`）：{line!r}")
        pairs.append((parts[0], parts[1]))
    if not pairs:
        sys.exit(f"❌ 姓名名单为空：{path}")
    return pairs


def tracked_files() -> list[str]:
    """已跟踪文件列表。

    ⚠️ 必须关掉 git 的路径转义：非 ASCII 文件名默认会被引成 `"docs\\345\\274\\200..."`，
    直接当路径用会崩（实测 `OSError: Invalid argument`），或更糟——被 except 吞掉后**静默漏掉这些文件**，
    于是清理工具报"无命中"，而真名就压在这些文件里。
    """
    out = subprocess.run(["git", "-c", "core.quotePath=false", "ls-files", "-z"],
                         cwd=ROOT, capture_output=True, text=True, encoding="utf-8").stdout
    return [f for f in out.split("\0") if f]


def main() -> int:
    ap = argparse.ArgumentParser(description="把受控文件里的真实姓名换成 GitHub ID")
    ap.add_argument("--names", default=str(DEFAULT_NAMES), help="姓名名单路径（默认 local/pii_names.txt）")
    ap.add_argument("--check", action="store_true", help="只检查不修改（CI 用），有残留即非零退出")
    a = ap.parse_args()
    pairs = load_pairs(pathlib.Path(a.names))

    changed, left = [], []
    for f in tracked_files():
        p = ROOT / f
        try:
            s = p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, FileNotFoundError):
            continue
        hit = [(o, n) for o, n in pairs if o in s]
        if not hit:
            continue
        if a.check:
            left += [f"{f}: {o}" for o, _ in hit]
            continue
        for o, n in hit:
            s = s.replace(o, n)
        p.write_text(s, encoding="utf-8")
        changed.append((f, [f"{o}→{n}×{s.count(n)}" for o, n in hit]))

    if a.check:
        if left:
            print("❌ 发现未替换的真实姓名：")
            for x in left:
                print(f"   {x}")
            return 1
        print(f"✅ 已跟踪文件中无名单内的姓名（检查 {len(pairs)} 条映射）")
        return 0

    print("已替换：", changed or "（无命中）")
    return 0


if __name__ == "__main__":       # 工具模块必须「可导入不产生副作用」
    sys.exit(main())
