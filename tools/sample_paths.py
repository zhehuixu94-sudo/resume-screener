# -*- coding: utf-8 -*-
"""样本路径解析（离线工具共用）：把「本机简历在哪」从代码里挪到环境变量。

## 为什么

这 4 个工具曾各自写死开发机的绝对路径（`C:\\Users\\<user>\\Desktop\\<个人目录>\\...`）。
后果有两层：
  1. **隐私**：路径字符串会随仓库公开，目录名 + 文件名本身就能定位到人；
  2. **可用性**：别人 clone 后那些路径不存在，工具要么崩、要么静默跳过全部样本报"无命中"。

## 约定

- 仓库内样本：`local/samples`、`local/samples_all`（已 gitignore，靠 `local/resumes.json.example` 说明结构）
- 本机额外目录：环境变量 `RESUME_SEARCH_DIRS`（`os.pathsep` 分隔）
- 本机额外单文件：环境变量 `RESUME_EXTRA_FILES`（`os.pathsep` 分隔）
- 显式传入的参数优先级最高

工具只负责**列出候选**，不负责判断存在性——是否跳过交回调用方（便于打印"跳过 N 个不存在的样本"）。
"""
from __future__ import annotations

import os
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent      # 仓库根：tools/ 的上一级

# 仓库内默认样本目录（相对仓库根；都不进版本库）
DEFAULT_SAMPLE_DIRS = ("local/samples", "local/samples_all")


def resume_search_dirs(*extra: str) -> list[str]:
    """按文件名找简历时要搜的目录，去重且保序。

    优先级：显式 `extra` 参数 → `RESUME_SEARCH_DIRS` 环境变量 → 仓库内默认目录。
    """
    out: list[str] = []
    for d in (*extra, *os.environ.get("RESUME_SEARCH_DIRS", "").split(os.pathsep), *DEFAULT_SAMPLE_DIRS):
        d = (d or "").strip()
        if d and d not in out:
            out.append(d)
    return out


def resume_extra_files(*extra: str) -> list[str]:
    """额外的样本文件绝对路径（本机简历），优先级同 `resume_search_dirs`。"""
    out: list[str] = []
    for f in (*extra, *os.environ.get("RESUME_EXTRA_FILES", "").split(os.pathsep)):
        f = (f or "").strip()
        if f and f not in out:
            out.append(f)
    return out


def repo_path(p: str) -> str:
    """把仓库相对路径解析成绝对路径；已是绝对路径则原样返回。"""
    return p if os.path.isabs(p) else str(ROOT / p)
