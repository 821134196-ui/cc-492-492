#!/usr/bin/env python3
"""本地单命令启动：初始化 SQLite → 播种错版/复测样例 → 启动 Web。

用法：
    python3 run.py              # 首次启动自动播种；端口默认 8000
    python3 run.py --fresh      # 删除 data/ 后重建（重新播种）
    python3 run.py --no-seed    # 只启动不播种
    FCA_PORT=8080 python3 run.py
"""
from __future__ import annotations

import argparse
import shutil
import sys

from app import config
from app.db import connect
from app.seed import ensure_users, seed
from app.web import make_server


def main() -> int:
    ap = argparse.ArgumentParser(description="首箱放行系统 · 本地单命令启动")
    ap.add_argument("--fresh", action="store_true", help="清空 data/ 后重建并播种")
    ap.add_argument("--no-seed", action="store_true", help="不写入演示数据")
    ap.add_argument("--host", default=config.HOST)
    ap.add_argument("--port", type=int, default=config.PORT)
    args = ap.parse_args()

    if args.fresh and config.DATA_DIR.exists():
        shutil.rmtree(config.DATA_DIR)
        print(f"[run] 已清空 {config.DATA_DIR}")
    config.ensure_dirs()

    conn = connect()
    if not args.no_seed:
        seed(conn)
    else:
        ensure_users(conn)
    conn.close()

    httpd = make_server(args.host, args.port)
    print(f"\n[run] 首箱放行系统已启动：http://{args.host}:{args.port}/")
    print("[run] 按 Ctrl+C 停止。演示身份：张操作/李操作（操作员）、王检/陈检（质检员）")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[run] 已停止。")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
