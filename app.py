#!/usr/bin/env python3
"""单命令启动：python3 app.py

自动初始化 SQLite（data/factory.db）、生成本地稿件/照片目录并灌入演示种子，
然后启动看板 Web 服务。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.seed import seed  # noqa: E402
from app.web import run  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description="首箱留样看板")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--db", default="data/factory.db")
    ap.add_argument("--media", default="data/media")
    ap.add_argument("--no-seed", action="store_true", help="不重建演示数据")
    args = ap.parse_args()

    if not args.no_seed and not Path(args.db).exists():
        seed(args.db, args.media)
        print("已生成演示数据（错版归档 / 停机返工复测 / 超差待判）。")
    run(db_path=args.db, media_root=args.media, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
