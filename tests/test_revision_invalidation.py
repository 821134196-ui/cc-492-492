"""版次失效：稿件版次变更使旧首箱结论失效、禁止补批、旧留样封档。"""
from __future__ import annotations

import os
import stat

from app.errors import RevisionStale, SampleLocked
from tests.base import BaseCase


class RevisionInvalidationTests(BaseCase):
    def _released_order_with_sample(self):
        oid = self.make_order()
        rid = self.make_rev(oid)
        fa = self.svc.submit_first_article(rid, self.good_capture())
        self.svc.decide(fa, self.qc1, "release", note="A 版放行")
        self.svc.retain_sample(fa, "A-01")
        return oid, rid, fa

    def test_new_revision_supersedes_old_and_invalidates_conclusion(self):
        oid, rid, fa = self._released_order_with_sample()
        self.assertEqual(self.svc.get_revision(rid)["status"], "active")
        self.assertEqual(self.svc.get_fa(fa)["status"], "approved")
        self.assertEqual(self.svc.get_fa(fa)["conclusion_valid"], 1)

        new_rid = self.make_rev(oid, "B", die="DM-2")

        self.assertEqual(self.svc.get_revision(rid)["status"], "superseded")
        self.assertEqual(self.svc.get_revision(new_rid)["status"], "active")
        # 旧首箱结论失效，但历史记录仍可读。
        self.assertEqual(self.svc.get_fa(fa)["status"], "approved")
        self.assertEqual(self.svc.get_fa(fa)["conclusion_valid"], 0)

    def test_only_one_active_revision_per_order(self):
        oid = self.make_order()
        self.make_rev(oid, "A")
        self.make_rev(oid, "B")
        self.make_rev(oid, "C")
        actives = [r["rev"] for r in self.svc.revision_chain(oid) if r["status"] == "active"]
        self.assertEqual(actives, ["C"])

    def test_old_fa_cannot_be_approved_after_revision_change(self):
        oid, rid, fa = self._released_order_with_sample()
        # 构造一个尚未裁决、随后版次变更的首箱。
        pending = self.svc.submit_first_article(rid, self.good_capture(
            barcode_payload="PO-T1-BOX-2"))
        self.make_rev(oid, "B", die="DM-2")
        with self.assertRaises(RevisionStale):
            self.svc.decide(pending, self.qc2, "release")

    def test_cannot_capture_on_superseded_revision(self):
        oid, rid, _ = self._released_order_with_sample()
        self.make_rev(oid, "B", die="DM-2")
        from app.errors import RevisionStale as RS
        with self.assertRaises(RS):
            self.svc.submit_first_article(rid, self.good_capture())

    def test_old_sample_archived_and_photo_folder_readonly(self):
        oid, rid, fa = self._released_order_with_sample()
        sample = self.svc._fetch(
            "SELECT * FROM retained_samples WHERE fa_id=?", (fa,))[0]
        photo = self.svc.photos_of(fa)[0]["path"]
        from app import storage
        photo_dir = storage.abs_path(photo).parent
        self.assertTrue(photo_dir.exists())

        self.make_rev(oid, "B", die="DM-2")

        sample_after = self.svc._fetch(
            "SELECT * FROM retained_samples WHERE id=?", (sample["id"],))[0]
        self.assertEqual(sample_after["state"], "archived")
        # 归档留样禁止任何写操作。
        with self.assertRaises(SampleLocked):
            self.svc.assert_sample_writable(sample["id"])
        # 照片目录已被封档为只读。
        mode = os.stat(photo_dir).st_mode
        self.assertFalse(mode & stat.S_IWUSR)

    def test_new_revision_can_have_its_own_sample_and_released_fa(self):
        oid, _, old_fa = self._released_order_with_sample()
        rid_b = self.make_rev(oid, "B", die="DM-2")
        fa_b = self.svc.submit_first_article(rid_b, self.good_capture(
            die_scanned="DM-2", barcode_payload="PO-T1-BOX-B1"))
        self.svc.decide(fa_b, self.qc2, "release", note="B 版放行")
        sid = self.svc.retain_sample(fa_b, "B-01")
        row = self.svc._fetch("SELECT * FROM retained_samples WHERE id=?", (sid,))[0]
        self.assertEqual(row["state"], "active")
        # 同订单现只有一份生效留样，旧的仍是归档。
        states = {(r["revision_id"], r["state"]) for r in
                  self.svc._fetch("SELECT revision_id,state FROM retained_samples")}
        self.assertEqual(len([s for s in states if s[1] == "active"]), 1)
