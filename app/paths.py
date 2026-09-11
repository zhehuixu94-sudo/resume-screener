# -*- coding: utf-8 -*-
"""路径解析：源码运行与 PyInstaller 打包（exe）两种形态都要能找到资源。

exe 形态下：
- 只读资源（web/ 前端、schemas/）打进包里（sys._MEIPASS）；
- **可写/可改的东西放在 exe 旁边**（config/ 岗位配置、local/ 上传与运行产物），
  这样用户拿着 exe 就能加岗位、改权重，不用重新打包。
"""
from __future__ import annotations

import os
import sys


def is_frozen() -> bool:
    return getattr(sys, "frozen", False)


def _bundle_dir() -> str:
    """打包后的只读资源目录。"""
    if is_frozen():
        return getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(sys.executable)))
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _exe_dir() -> str:
    """exe 所在目录（可写）。源码运行时就是项目根目录。"""
    if is_frozen():
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


BUNDLE_DIR = _bundle_dir()
EXE_DIR = _exe_dir()
ROOT = EXE_DIR

WEB_DIR = os.path.join(BUNDLE_DIR, "web")
SCHEMA_DIR = os.path.join(BUNDLE_DIR, "schemas")


def config_dir() -> str:
    """优先用 exe 旁边的 config/（用户可改），没有就用包内的。"""
    local = os.path.join(EXE_DIR, "config")
    if os.path.isdir(local) and any(f.endswith(".json") for f in os.listdir(local)):
        return local
    bundled = os.path.join(BUNDLE_DIR, "config")
    return bundled if os.path.isdir(bundled) else local


def data_dir() -> str:
    """上传文件与本地数据目录（可写，始终在 exe 旁边）。"""
    return os.path.join(EXE_DIR, "local")


def ensure_user_config() -> str:
    """首次运行时把包内的岗位配置释放到 exe 旁边，用户就能直接改 JSON 加岗位。

    已存在同名文件则不覆盖（保护用户的改动）；exe 旁边没有 config 目录时才会释放。
    """
    import shutil

    target = os.path.join(EXE_DIR, "config")
    bundled = os.path.join(BUNDLE_DIR, "config")
    if is_frozen() and not os.path.isdir(target) and os.path.isdir(bundled):
        os.makedirs(target, exist_ok=True)
        for name in os.listdir(bundled):
            if name.endswith(".json"):
                shutil.copyfile(os.path.join(bundled, name), os.path.join(target, name))
    return target
