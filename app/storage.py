"""本地文件存储：稿件与六面照片。

- 稿件：登记版次时落盘（支持上传内容，或由种子生成 SVG 占位稿）。
- 照片：首箱采集时按面落盘，返回 (相对路径, sha256)。
归档（旧版次）留样的照片通过 :func:`assert_writable` 拦截写/删操作。
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from . import config


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def save_artwork(order_code: str, rev: str, filename: str, data: bytes) -> tuple[str, str]:
    suffix = Path(filename).suffix or ".svg"
    safe = f"{order_code}_{rev}{suffix}"
    dest = config.ARTWORK_DIR / safe
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    return str(dest.relative_to(config.DATA_DIR)), _sha256(data)


def save_photo(fa_id: int, face: str, data: bytes, suffix: str = ".svg") -> tuple[str, str]:
    folder = config.PHOTO_DIR / f"fa_{fa_id}"
    folder.mkdir(parents=True, exist_ok=True)
    dest = folder / f"{face}{suffix}"
    dest.write_bytes(data)
    return str(dest.relative_to(config.DATA_DIR)), _sha256(data)


def abs_path(rel: str) -> Path:
    return config.DATA_DIR / rel


def assert_writable(rel_path: str) -> None:
    """旧留样只读：归档后其照片文件拒绝覆盖/删除。"""
    p = abs_path(rel_path)
    if p.exists():
        # 目录去掉写位即视为只读柜位；文件继承目录语义由服务层先行判定，
        # 这里是最后一道文件级防线。
        import os, stat
        st = os.stat(p.parent if p.is_file() else p)
        if not (st.st_mode & stat.S_IWUSR):
            raise PermissionError(f"归档留样目录只读: {rel_path}")


def lock_readonly(rel_paths: list[str]) -> None:
    """版次失效时，把旧留样照片目录置为只读（柜位封档）。"""
    import os, stat
    for rel in rel_paths:
        p = abs_path(rel)
        folder = p.parent if p.is_file() else p
        if folder.exists():
            os.chmod(folder, stat.S_IRUSR | stat.S_IXUSR | stat.S_IRGRP | stat.S_IXGRP)


# ---- 演练用占位图（SVG）生成 -------------------------------------------------

def artwork_svg(order_code: str, rev: str, product: str, colors: list[str]) -> bytes:
    swatches = "".join(
        f'<rect x="{40+i*70}" y="150" width="56" height="28" fill="{c}"'
        f' stroke="#1a1a1a" stroke-width="1"/>'
        for i, c in enumerate(colors[:6])
    )
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="640" height="400" viewBox="0 0 640 400">
<style>text{{font-family:'Noto Sans CJK SC',sans-serif;fill:#1a1a1a}}</style>
<rect width="640" height="400" fill="#f7f4ec"/>
<rect x="16" y="16" width="608" height="368" fill="none" stroke="#1a1a1a" stroke-width="2"/>
<text x="40" y="70" font-size="34" font-weight="700">{order_code} 客户稿件</text>
<text x="40" y="112" font-size="22">品名：{product}　版次：{rev}</text>
<text x="40" y="220" font-size="16">印色：</text>
{swatches}
<text x="40" y="340" font-size="14" fill="#555">客户来稿登记件 · 变更版次须重新登记并重做首箱</text>
</svg>""".encode("utf-8")


def face_photo_svg(face_label: str, order_code: str, rev: str, seq: int,
                   dims: str, damaged: bool = False) -> bytes:
    warn = ('<text x="320" y="300" font-size="20" fill="#b3261e" '
            'text-anchor="middle">样箱破损（折痕开裂）</text>') if damaged else ""
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="640" height="400" viewBox="0 0 640 400">
<style>text{{font-family:'Noto Sans CJK SC',sans-serif;fill:#1a1a1a}}</style>
<rect width="640" height="400" fill="#e9e4d6"/>
<rect x="90" y="70" width="460" height="250" fill="#d8cfb8" stroke="#1a1a1a" stroke-width="3"/>
<line x1="90" y1="120" x2="550" y2="120" stroke="#1a1a1a" stroke-width="1" stroke-dasharray="6 6"/>
<line x1="320" y1="70" x2="320" y2="320" stroke="#1a1a1a" stroke-width="1" stroke-dasharray="6 6"/>
<text x="320" y="52" font-size="26" font-weight="700" text-anchor="middle">{face_label} 面照片</text>
<text x="320" y="356" font-size="16" text-anchor="middle">{order_code} / 版次 {rev} / 第 {seq} 轮 · {dims}</text>
{warn}
</svg>""".encode("utf-8")
