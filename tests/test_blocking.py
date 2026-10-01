"""缺项与异常阻断：条码不可读、尺寸超差、混用旧刀模、照片缺面、样箱破损。"""
from __future__ import annotations

from app.errors import BlockingFinding
from app.scanner import MockBarcodeScanner
from tests.base import BaseCase


class BlockingTests(BaseCase):
    def setUp(self):
        super().setUp()
        self.rid = self.make_rev(self.make_order())

    def _codes(self, fa_id):
        return {f["code"] for f in self.svc.list_findings(fa_id)}

    def test_clean_capture_has_no_blocking_item(self):
        fa = self.svc.submit_first_article(self.rid, self.good_capture())
        self.assertEqual(self.svc.get_fa(fa)["status"], "pending")
        self.assertEqual(self._codes(fa), set())
        self.svc.decide(fa, self.qc1, "release")

    def test_barcode_unreadable_blocks(self):
        fa = self.svc.submit_first_article(self.rid, self.good_capture(
            barcode_ok=False, barcode_payload="",
            scanner=MockBarcodeScanner("no_read")))
        self.assertIn("barcode_unreadable", self._codes(fa))
        self.assertEqual(self.svc.get_fa(fa)["status"], "blocked")
        with self.assertRaises(BlockingFinding):
            self.svc.decide(fa, self.qc1, "release")

    def test_barcode_mismatch_blocks(self):
        fa = self.svc.submit_first_article(self.rid, self.good_capture(
            barcode_ok=False, scanner=MockBarcodeScanner("wrong_code")))
        self.assertIn("barcode_mismatch", self._codes(fa))
        with self.assertRaises(BlockingFinding):
            self.svc.decide(fa, self.qc1, "release")

    def test_dimension_over_tolerance_blocks_with_concrete_value(self):
        # 公差 ±2mm：高向 +3 超差。
        fa = self.svc.submit_first_article(self.rid, self.good_capture(
            measured={"l": 300.0, "w": 200.0, "h": 153.0}))
        msgs = [f["message"] for f in self.svc.list_findings(fa)]
        self.assertEqual(len(msgs), 1)
        self.assertIn("高", msgs[0])
        # 超差标注必须带目标值、实测值、偏差的具体数字，而不只是红色标记。
        self.assertIn("153", msgs[0]); self.assertIn("150", msgs[0]); self.assertIn("+3", msgs[0])
        with self.assertRaises(BlockingFinding):
            self.svc.decide(fa, self.qc1, "release")

    def test_dimension_boundary_and_missing_measure(self):
        # 恰在 ±2 边界内合格。
        fa_ok = self.svc.submit_first_article(self.rid, self.good_capture(
            measured={"l": 302.0, "w": 198.0, "h": 150.0},
            barcode_payload="PO-T1-BOX-b"))
        self.assertEqual(self._codes(fa_ok), set())
        # 未测量视为缺项阻断。
        fa_miss = self.svc.submit_first_article(self.rid, self.good_capture(
            measured={"l": 300.0, "w": None, "h": 150.0},
            barcode_payload="PO-T1-BOX-c"))
        self.assertIn("dim_over", self._codes(fa_miss))
        self.assertIn("宽未测量",
                      [f["message"] for f in self.svc.list_findings(fa_miss)][0])

    def test_old_die_blocks(self):
        fa = self.svc.submit_first_article(self.rid, self.good_capture(
            die_scanned="DM-1-OLD"))
        msgs = [f["message"] for f in self.svc.list_findings(fa)]
        self.assertTrue(any("混用旧刀模" in m for m in msgs))
        self.assertIn("DM-1-OLD", msgs[0]); self.assertIn("DM-1", msgs[0])

    def test_missing_photo_face_blocks(self):
        fa = self.svc.submit_first_article(self.rid, self.good_capture(
            photos=self.six_photos(skip=("top", "bottom"))))
        msgs = [f["message"] for f in self.svc.list_findings(fa)]
        faces = " ".join(msgs)
        self.assertIn("顶面", faces); self.assertIn("底面", faces)
        self.assertEqual(len(self.svc.photos_of(fa)), 4)

    def test_damaged_sample_blocks(self):
        fa = self.svc.submit_first_article(self.rid, self.good_capture(
            sample_intact=False))
        self.assertIn("sample_damaged", self._codes(fa))

    def test_all_five_blockers_together_force_rework_path(self):
        fa = self.svc.submit_first_article(self.rid, self.good_capture(
            measured={"l": 309.0, "w": 200.0, "h": None},
            barcode_ok=False, barcode_payload="",
            die_scanned="DM-0-OLD", sample_intact=False,
            photos=self.six_photos(skip={"left"}),
            scanner=MockBarcodeScanner("no_read")))
        codes = self._codes(fa)
        for c in ("barcode_unreadable", "dim_over", "die_mismatch",
                  "photo_missing", "sample_damaged"):
            self.assertIn(c, codes)
        # 放行被挡，只能判停机返工。
        with self.assertRaises(BlockingFinding):
            self.svc.decide(fa, self.qc1, "release")
        res = self.svc.decide(fa, self.qc1, "rework", reason="多项不合格")
        self.assertEqual(res["status"], "rejected")
        self.assertEqual(self.svc.get_fa(fa)["status"], "rejected")
