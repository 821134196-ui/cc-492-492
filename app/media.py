"""本地文件存储：客户稿件与六面照片以文件形式保存在 data/media 下。

车间相机/稿件系统接入点替换 save_photo / save_artwork 即可；
当前实现生成带面别与版次标注的 SVG 占位图，模拟实拍。
"""
from __future__ import annotations

import html
import shutil
from pathlib import Path

from .db import FACE_CN, FACES


def ensure_dirs(root: str | Path) -> tuple[Path, Path]:
    root = Path(root)
    art = root / "artwork"
    photo = root / "photos"
    art.mkdir(parents=True, exist_ok=True)
    photo.mkdir(parents=True, exist_ok=True)
    return art, photo


def _svg(title: str, subtitle: str, w: int = 480, h: int = 360) -> str:
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}"
 viewBox="0 0 {w} {h}" font-family="sans-serif">
  <rect width="100%" height="100%" fill="#f7f5ef" stroke="#1a1a1a" stroke-width="3"/>
  <text x="50%" y="46%" text-anchor="middle" font-size="30" fill="#1a1a1a"
        font-weight="bold">{html.escape(title)}</text>
  <text x="50%" y="60%" text-anchor="middle" font-size="16" fill="#1f4e9c">
        {html.escape(subtitle)}</text>
  <rect x="24" y="24" width="{w-48}" height="{h-48}" fill="none"
        stroke="#1f4e9c" stroke-width="1" stroke-dasharray="6 6"/>
</svg>
"""


def save_artwork(media_root: str | Path, order_no: str, rev_code: str,
                 src: str | Path | None = None) -> str:
    art_dir, _ = ensure_dirs(media_root)
    name = f"{order_no}_{rev_code}.svg"
    dest = art_dir / name
    if src is not None and Path(src).exists():
        shutil.copyfile(src, dest)
    else:
        dest.write_text(_svg(f"客户稿件 {order_no}", f"版次 {rev_code}"), encoding="utf-8")
    return f"artwork/{name}"


def save_photo(media_root: str | Path, fa_token: str, face: str,
               damaged: bool = False) -> str:
    _, photo_dir = ensure_dirs(media_root)
    assert face in FACES
    name = f"{fa_token}_{face}.svg"
    sub = "样箱破损（模拟）" if damaged else f"面别：{FACE_CN[face]}"
    (photo_dir / name).write_text(
        _svg(f"首箱六面照 · {FACE_CN[face]}", f"{fa_token} · {sub}"),
        encoding="utf-8",
    )
    return f"photos/{name}"


def save_six_photos(media_root: str | Path, fa_token: str,
                    damaged_face: str | None = None) -> dict[str, str]:
    return {
        face: save_photo(media_root, fa_token, face, damaged=(face == damaged_face))
        for face in FACES
    }


def abspath(media_root: str | Path, rel: str) -> Path:
    return Path(media_root) / rel
