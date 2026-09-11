#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""启动器：双击 exe 就能用。

做的三件事：
  1. 在 127.0.0.1 上挑一个空闲端口起 API + 前端（不对外监听，避免把简历暴露到局域网）；
  2. 自动打开默认浏览器；
  3. 控制台里显示地址、数据库状态与退出方式（关窗口即停）。

数据库连不上也照样起服务：前端会显示"数据库不可用"和排查提示，而不是让人对着黑框猜。
"""
from __future__ import annotations

import argparse
import os
import socket
import sys
import threading
import time
import webbrowser

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Windows 控制台默认是 GBK，直接打印中文会乱码：切 UTF-8 代码页 + 重配 stdout
if sys.platform == "win32":
    try:
        os.system("chcp 65001 > nul")
    except Exception:                                             # noqa: BLE001
        pass
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")   # type: ignore[union-attr]
    except Exception:                                             # noqa: BLE001
        pass


def free_port(preferred: int = 8765) -> int:
    for p in [preferred] + list(range(preferred + 1, preferred + 20)):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(("127.0.0.1", p))
                return p
            except OSError:
                continue
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def main() -> int:
    ap = argparse.ArgumentParser(description="简历初筛系统（本地化 exe 启动器）")
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--model", default=None, help="覆盖模型（默认读 RESUME_MODEL 或内置降本档）")
    a = ap.parse_args()

    if a.model:
        os.environ["RESUME_MODEL"] = a.model

    from app import paths

    paths.ensure_user_config()          # 首次运行把岗位配置释放到 exe 旁边

    port = a.port or free_port()
    url = f"http://127.0.0.1:{port}/"

    print("=" * 62, flush=True)
    print("  简历初筛系统 · 本地服务", flush=True)
    print("=" * 62, flush=True)
    print(f"  数据目录   {paths.EXE_DIR}")
    print(f"  岗位配置   {paths.config_dir()}", flush=True)
    print(f"  访问地址   {url}", flush=True)
    print("  关闭本窗口即停止服务", flush=True)
    print("-" * 62, flush=True)

    # 起服务前先探一次数据库，连不上要说清楚怎么办
    try:
        from app.store import db

        row = db.fetch_one("SELECT current_database() AS d, version() AS v")
        print(f"  数据库     已连接：{row['d']}（{row['v'].split()[1]}）", flush=True)
    except Exception as e:                                        # noqa: BLE001
        print(f"  数据库     连不上：{type(e).__name__}: {str(e)[:120]}")
        print("             → 请先启动 PostgreSQL（本项目开发用容器：docker start resume_pg）")
        print("             → 或设置环境变量 PG_DSN / PGHOST/PGPORT/PGUSER/PGPASSWORD 指向你自己的库")
    try:
        # 与 API 用同一个口径：RESUME_MODEL 优先，否则内置降本档（主干 122B 实测会整段时间无响应）
        model = os.environ.get("RESUME_MODEL") or "Qwen/Qwen3.5-35B-A3B"
        os.environ.setdefault("RESUME_MODEL", model)
        print(f"  模型       {model}", flush=True)
    except Exception as e:                                        # noqa: BLE001
        print(f"  模型       未配置密钥：{str(e)[:100]}")
        print("             → 设置环境变量 SILICONFLOW_API_KEY 即可")
    print("-" * 62)

    import uvicorn

    from app.api import app

    if not a.no_browser:
        threading.Thread(target=lambda: (time.sleep(1.2), webbrowser.open(url)), daemon=True).start()

    print("  正在启动服务…（Ctrl-C 停止）", flush=True)
    try:
        uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
    except KeyboardInterrupt:
        print("\n  已停止。", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
