"""公共夹具：隔离的临时数据目录与内存/文件 SQLite。"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app import config, storage
from app.db import connect, init_db
from app.services import Service


class BaseCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        config.DATA_DIR = tmp
        config.DB_PATH = tmp / "fca.sqlite3"
        config.FILES_DIR = tmp / "files"
        config.ARTWORK_DIR = config.FILES_DIR / "artwork"
        config.PHOTO_DIR = config.FILES_DIR / "photos"
        config.ensure_dirs()
        self.conn = connect(config.DB_PATH)
        init_db(self.conn)
        self.svc = Service(self.conn)
        for login, name, role in [
            ("op1", "张操作", "operator"),
            ("op2", "李操作", "operator"),
            ("qc1", "王检", "qc"),
            ("qc2", "陈检", "qc"),
        ]:
            self.conn.execute(
                "INSERT INTO users(login,name,role) VALUES(?,?,?)", (login, name, role))
        ids = {r["login"]: r["id"] for r in self.svc._fetch("SELECT * FROM users")}
        self.op1, self.op2 = ids["op1"], ids["op2"]
        self.qc1, self.qc2 = ids["qc1"], ids["qc2"]

    def tearDown(self) -> None:
        # Windows 上只读目录会挡住清理，先恢复写位。
        for p in config.PHOTO_DIR.rglob("*"):
            if p.is_dir():
                import os, stat
                os.chmod(p, stat.S_IRWXU)
        self.conn.close()
        self._tmp.cleanup()

    # -- 工厂辅助 --

    def make_order(self, code: str | None = None, die: str = "DM-1",
                   targets=(300.0, 200.0, 150.0)) -> int:
        code = code or f"PO-{id(self) % 100000:05d}-{getattr(self, '_n', 0)}"
        self._n = getattr(self, "_n", 0) + 1
        return self.svc.create_order(
            code, "测试客户", "测试箱", "标准对口箱", "1号线",
            die, "PAP-1", ["黑"], targets)

    def make_rev(self, order_id: int, rev: str = "A", die: str = "DM-1") -> int:
        order = self.svc.get_order(order_id)
        return self.svc.register_revision(
            order_id, rev, die, "PAP-1", ["黑"],
            storage.artwork_svg(order["code"], rev, "测试箱", ["黑"]))

    def six_photos(self, code="PO-T1", rev="A", seq=1, dims="300×200×150mm",
                   skip=()):
        return {
            f: storage.face_photo_svg(config.FACE_LABELS[f], code, rev, seq, dims)
            for f in config.FACES if f not in set(skip)
        }

    def good_capture(self, **kw):
        from app.services import CaptureInput
        data = dict(
            operator_id=self.op1,
            measured={"l": 300.0, "w": 200.0, "h": 150.0},
            barcode_ok=True, barcode_payload="PO-T1-BOX-1",
            die_scanned="DM-1", sample_intact=True,
            photos=self.six_photos())
        data.update(kw)
        return CaptureInput(**data)
