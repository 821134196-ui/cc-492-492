"""全局路径与常量。"""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 可用环境变量覆盖，默认全部落在仓库 data/ 下，便于本地单命令启动。
DATA_DIR = Path(os.environ.get("FCA_DATA_DIR", ROOT / "data"))
DB_PATH = Path(os.environ.get("FCA_DB", DATA_DIR / "fca.sqlite3"))
FILES_DIR = DATA_DIR / "files"          # 客户稿件（PDF/SVG）原件
ARTWORK_DIR = FILES_DIR / "artwork"     # 稿件渲染件
PHOTO_DIR = FILES_DIR / "photos"        # 六面照片

HOST = os.environ.get("FCA_HOST", "127.0.0.1")
PORT = int(os.environ.get("FCA_PORT", "8000"))

# 纸箱六个面（展开图布局顺序）。
FACES = ["front", "back", "left", "right", "top", "bottom"]
FACE_LABELS = {
    "front": "正面",
    "back": "背面",
    "left": "左面",
    "right": "右面",
    "top": "顶面",
    "bottom": "底面",
}

# 尺寸公差 ±mm（长宽高与压痕/摇盖）。
TOL_MM = 2.0

# 印刷配色：纸白、墨黑、少量印刷蓝。
INK = "#1a1a1a"
PAPER = "#f7f4ec"
BLUE = "#1558a8"
BLUE_SOFT = "#e4edf8"
RED = "#b3261e"
GREEN = "#1d6b3a"
AMBER = "#8a5a00"


def ensure_dirs() -> None:
    for d in (DATA_DIR, FILES_DIR, ARTWORK_DIR, PHOTO_DIR):
        d.mkdir(parents=True, exist_ok=True)
