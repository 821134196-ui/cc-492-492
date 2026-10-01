"""HTTP 集成测试：页面渲染、六面展开图、具体超差数值、表单阻断与只读提示。"""
from __future__ import annotations

import http.client
import shutil
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlencode

from app.seed import seed
from app.web import make_handler, App


class WebCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.db = str(Path(cls.tmp) / "f.db")
        cls.media = str(Path(cls.tmp) / "media")
        seed(cls.db, cls.media)
        cls.app = App(cls.db, cls.media)
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(cls.app))
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def req(self, method, path, data=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        body = urlencode(data or {}, doseq=True) if data is not None else None
        headers = {"Content-Type": "application/x-www-form-urlencoded"} if body else {}
        conn.request(method, path, body=body, headers=headers)
        r = conn.getresponse()
        text = r.read().decode("utf-8")
        conn.close()
        return r.status, text

    def test_board_lists_orders(self):
        code, text = self.req("GET", "/")
        self.assertEqual(code, 200)
        self.assertIn("明远食品", text)
        self.assertIn("留样柜", text)
        # 订单1经历过停机返工，其详情页保留停机原因
        code, text = self.req("GET", "/orders/1")
        self.assertEqual(code, 200)
        self.assertIn("停机返工", text)

    def test_article_page_six_face_net_and_values(self):
        # 种子里 article 2 是高度超差被停机的首箱
        code, text = self.req("GET", "/articles/2")
        self.assertEqual(code, 200)
        self.assertIn("首箱核对（六面展开图）", text)
        for cn in ("正面", "背面", "左面", "右面", "上面", "下面"):
            self.assertIn(cn, text)
        # 并排测量与具体超差值
        self.assertIn("156.5", text)
        self.assertIn("+6.5", text)
        self.assertIn("停机返工", text)
        self.assertIn("调模后返工复测", text)  # 停机原因/差异标注保留

    def test_blocked_release_returns_409_with_number(self):
        # article 4 是长 +3.8 超差、待质检的首箱
        code, text = self.req("POST", "/articles/4/decision",
                              {"qc_user_id": 3, "decision": "released"})
        self.assertEqual(code, 409)
        self.assertIn("BLOCKED", text)
        self.assertIn("253.8", text)

    def test_operator_role_blocked(self):
        # 操作员账号不能作为质检员审批（SELF_APPROVAL 细粒度规则由单测覆盖）
        code, text = self.req("POST", "/articles/4/decision",
                              {"qc_user_id": 1, "decision": "released"})
        self.assertEqual(code, 409)
        self.assertIn("NOT_QC", text)

    def test_old_sample_readonly_banner(self):
        # article 1 是 V1 旧版留样，已入柜只读
        code, text = self.req("GET", "/articles/1")
        self.assertEqual(code, 200)
        self.assertIn("留样只读", text)
        code2, _ = self.req("POST", "/articles/1/decision",
                            {"qc_user_id": 4, "decision": "held", "reason": "x"})
        self.assertEqual(code2, 409)

    def test_missing_face_submit_blocked(self):
        code, text = self.req("POST", "/productions/3/articles", {
            "submitted_by": 1, "cabinet_slot": "Z", "measured_l": 250,
            "measured_w": 180, "measured_h": 120, "sample_intact": 1,
            "barcode_scan": "BC-ORD-2402", "barcode_ok": 1,
            "face_front": "on"})
        self.assertEqual(code, 409)
        self.assertIn("照片缺面", text)

    def test_media_file_served(self):
        code, text = self.req("GET", "/media/artwork/ORD-2401_V2.svg")
        self.assertEqual(code, 200)
        self.assertIn("<svg", text)

    def test_path_traversal_rejected(self):
        code, _ = self.req("GET", "/media/../factory.db")
        self.assertIn(code, (404, 400))


if __name__ == "__main__":
    unittest.main()
