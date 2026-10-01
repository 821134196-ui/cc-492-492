"""留样规则：同订单同版次唯一生效留样、旧留样只读；返工复测链路。"""
from __future__ import annotations

import os
import stat

from app import storage
from app.errors import (
    DuplicateActiveSample,
    IllegalTransition,
    RevisionStale,
    SampleLocked,
)
from tests.base import BaseCase


class SampleTests(BaseCase):
    def _released(self, die="DM-1", payload="PO-T1-BOX-1"):
        oid = self.make_order(die=die)
        rid = self.make_rev(oid, die=die)
        fa = self.svc.submit_first_article(rid, self.good_capture(
            die_scanned=die, barcode_payload=payload))
        self.svc.decide(fa, self.qc1, "release")
        return oid, rid, fa

    def test_one_active_sample_per_order_revision(self):
        _oid, rid, fa = self._released()
        self.svc.retain_sample(fa, "A-01")
        # 同一首箱/版次再留一份 → 拒绝。
        with self.assertRaises(DuplicateActiveSample):
            self.svc.retain_sample(fa, "A-02")
        # 同版次第二轮合格后仍不能再留生效样。
        fa2 = self.svc.submit_first_article(rid, self.good_capture(
            barcode_payload="PO-T1-BOX-2"))
        self.svc.decide(fa2, self.qc2, "release")
        with self.assertRaises(DuplicateActiveSample):
            self.svc.retain_sample(fa2, "A-03")

    def test_cabinet_slot_is_exclusive(self):
        _oid_a, rid_a, fa_a = self._released(payload="PO-T1-BOX-a")
        self.svc.retain_sample(fa_a, "C-09")
        # 第二个订单也要 C-09 → 柜位互斥拒绝。
        oid2 = self.make_order("PO-T2", die="DM-9")
        rid_b = self.make_rev(oid2, die="DM-9")
        fa_b = self.svc.submit_first_article(rid_b, self.good_capture(
            die_scanned="DM-9", barcode_payload="PO-T2-BOX-1",
            photos=self.six_photos(code="PO-T2")))
        self.svc.decide(fa_b, self.qc2, "release")
        with self.assertRaises(DuplicateActiveSample):
            self.svc.retain_sample(fa_b, "C-09")
        self.svc.retain_sample(fa_b, "C-10")  # 换柜位即可

    def test_retain_requires_release(self):
        rid = self.make_rev(self.make_order())
        fa = self.svc.submit_first_article(rid, self.good_capture())
        with self.assertRaises(IllegalTransition):
            self.svc.retain_sample(fa, "A-05")

    def test_old_sample_is_readonly_after_new_revision(self):
        oid, rid, fa = self._released()
        sid = self.svc.retain_sample(fa, "A-01")
        photo_path = self.svc.photos_of(fa)[0]["path"]
        photo_dir = storage.abs_path(photo_path).parent

        self.make_rev(oid, "B", die="DM-2")

        # 服务层：归档留样不可写、不可移位。
        with self.assertRaises(SampleLocked):
            self.svc.assert_sample_writable(sid)
        # 文件层：照片目录已转只读，直接写入被系统拒绝。
        self.assertFalse(os.stat(photo_dir).st_mode & stat.S_IWUSR)
        probe = photo_dir / "overwrite_probe.txt"
        with self.assertRaises(PermissionError):
            probe.write_text("should be denied")

    def test_archived_sample_still_readable(self):
        oid, rid, fa = self._released()
        self.svc.retain_sample(fa, "A-01")
        self.make_rev(oid, "B", die="DM-2")
        # 归档记录与照片仍可只读访问（页面历史溯源）。
        row = self.svc._fetch(
            "SELECT * FROM retained_samples WHERE fa_id=?", (fa,))[0]
        self.assertEqual(row["state"], "archived")
        self.assertTrue(storage.abs_path(self.svc.photos_of(fa)[0]["path"]).exists())


class ReworkRetestTests(BaseCase):
    def test_retest_requires_prior_rework_decision(self):
        rid = self.make_rev(self.make_order())
        fa = self.svc.submit_first_article(rid, self.good_capture())
        with self.assertRaises(IllegalTransition):
            self.svc.rework_retest(fa, self.good_capture(barcode_payload="X-2"))

    def test_rework_then_retest_pass_chain(self):
        rid = self.make_rev(self.make_order())
        bad = self.svc.submit_first_article(rid, self.good_capture(
            measured={"l": 305.0, "w": 200.0, "h": 150.0}))  # 长 +5 超差
        self.svc.decide(bad, self.qc1, "rework", reason="长度超差+5，调模")
        self.assertEqual(self.svc.get_fa(bad)["status"], "rejected")

        good = self.svc.rework_retest(bad, self.good_capture(
            measured={"l": 300.5, "w": 200.0, "h": 150.0},
            barcode_payload="PO-T1-BOX-2"))
        self.assertEqual(self.svc.get_fa(good)["sequence"], 2)
        self.assertEqual(self.svc.get_fa(good)["prev_fa_id"], bad)
        self.assertEqual(self.svc.list_findings(good), [])
        # 复测放行人与首人不同也允许，操作员本人仍不可自批。
        self.svc.decide(good, self.qc2, "release", note="复测合格")
        self.svc.retain_sample(good, "A-07")

    def test_retest_can_fail_again_and_chain_continues(self):
        rid = self.make_rev(self.make_order())
        bad1 = self.svc.submit_first_article(rid, self.good_capture(
            measured={"l": 310.0, "w": 200.0, "h": 150.0}))
        self.svc.decide(bad1, self.qc1, "rework", reason="第一次停机")
        bad2 = self.svc.rework_retest(bad1, self.good_capture(
            measured={"l": 308.0, "w": 200.0, "h": 150.0},
            barcode_payload="PO-T1-BOX-2"))
        self.svc.decide(bad2, self.qc1, "rework", reason="第二次停机")
        self.assertEqual(self.svc.get_fa(bad2)["sequence"], 2)
        good = self.svc.rework_retest(bad2, self.good_capture(
            measured={"l": 300.0, "w": 200.0, "h": 150.0},
            barcode_payload="PO-T1-BOX-3"))
        self.assertEqual(self.svc.get_fa(good)["sequence"], 3)
        self.assertEqual(self.svc.get_fa(good)["prev_fa_id"], bad2)
        self.svc.decide(good, self.qc2, "release")

    def test_retest_on_stale_revision_rejected(self):
        rid = self.make_rev(self.make_order())
        bad = self.svc.submit_first_article(rid, self.good_capture(
            measured={"l": 310.0, "w": 200.0, "h": 150.0}))
        self.svc.decide(bad, self.qc1, "rework", reason="停机")
        oid = self.svc.get_revision(rid)["order_id"]
        self.make_rev(oid, "B", die="DM-2")
        with self.assertRaises(RevisionStale):
            self.svc.rework_retest(bad, self.good_capture())
