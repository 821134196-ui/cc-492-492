"""业务规则测试：版次失效、缺项阻断、审批竞态、旧留样只读等。

运行：python3 -m unittest discover -s tests -v
"""
from __future__ import annotations

import shutil
import tempfile
import threading
import unittest
from pathlib import Path

from app import services
from app.db import FACES, connect, init_db
from app.media import save_artwork, save_six_photos


class CaseBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = str(Path(self.tmp) / "t.db")
        self.media = str(Path(self.tmp) / "media")
        self.conn = connect(self.db)
        init_db(self.conn)
        self.op = self._user("操作员甲", "operator")
        self.op2 = self._user("操作员乙", "operator")
        self.qc1 = self._user("质检一", "qc")
        self.qc2 = self._user("质检二", "qc")
        self.oid = self._order()
        self.rid = self._rev(self.oid, "V1")
        self.pid = services.register_production(
            self.conn, order_id=self.oid, revision_id=self.rid,
            paper_batch="P1", die_no_actual="D-001", colors_actual="四色",
            box_type_actual="对口箱", line_actual="A线", registered_by=self.op)

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _user(self, name, role):
        return self.conn.execute(
            "INSERT INTO users(name,role) VALUES(?,?)", (name, role)).lastrowid

    def _order(self, barcode="BC-1"):
        return self.conn.execute(
            """INSERT INTO orders(order_no,customer,box_type,line,colors,die_no,
               target_l,target_w,target_h,tol_mm,barcode)
               VALUES('O1','客户','对口箱','A线','四色','D-001',300,200,150,2.0,?)""",
            (barcode,)).lastrowid

    def _rev(self, oid, code):
        rel = save_artwork(self.media, "O1", code)
        return self.conn.execute(
            "INSERT INTO revisions(order_id,rev_code,art_file,active) VALUES(?,?,?,1)",
            (oid, code, rel)).lastrowid

    def _photos(self, token):
        return save_six_photos(self.media, token)

    def _submit(self, *, pid=None, measured=(300, 200, 150), scan="BC-1",
                scan_ok=1, intact=1, slot="A-1", by=None, photos=None,
                barcode_ok=None):
        pid = pid or self.pid
        return services.submit_first_article(
            self.conn, production_id=pid,
            measured_l=measured[0], measured_w=measured[1], measured_h=measured[2],
            barcode_scan=scan, barcode_ok=scan_ok if barcode_ok is None else barcode_ok,
            cabinet_slot=slot, sample_intact=intact,
            submitted_by=by or self.op,
            photos=photos if photos is not None else self._photos(f"p{pid}"))

    def _release(self, fa_id, qc=None):
        return services.approve(self.conn, first_article_id=fa_id,
                                qc_user_id=qc or self.qc1, decision="released")


class TestBlocking(CaseBase):
    def test_dim_out_of_tol_blocks(self):
        fa = self._submit(measured=(300, 200, 156.5))  # 高 +6.5
        with self.assertRaises(services.DomainError) as cm:
            self._release(fa)
        self.assertEqual(cm.exception.code, "BLOCKED")
        self.assertIn("156.5", cm.exception.message)  # 显示具体数值而非只标红

    def test_unreadable_barcode_blocks(self):
        fa = self._submit(scan=None, scan_ok=0)
        with self.assertRaises(services.DomainError) as cm:
            self._release(fa)
        self.assertEqual(cm.exception.code, "BLOCKED")
        self.assertIn("条码不可读", cm.exception.message)

    def test_wrong_barcode_content_blocks(self):
        fa = self._submit(scan="BC-OTHER")
        with self.assertRaises(services.DomainError) as cm:
            self._release(fa)
        self.assertIn("BC-OTHER", cm.exception.message)

    def test_old_die_blocks(self):
        pid2 = services.register_production(
            self.conn, order_id=self.oid, revision_id=self.rid,
            paper_batch="P2", die_no_actual="D-OLD", colors_actual="四色",
            box_type_actual="对口箱", line_actual="A线", registered_by=self.op)
        fa = self._submit(pid=pid2)
        with self.assertRaises(services.DomainError) as cm:
            self._release(fa)
        self.assertIn("混用旧刀模", cm.exception.message)

    def test_damaged_sample_blocks(self):
        fa = self._submit(intact=0)
        with self.assertRaises(services.DomainError) as cm:
            self._release(fa)
        self.assertIn("样箱破损", cm.exception.message)


class TestMissingCollection(CaseBase):
    def test_missing_face_blocks_submit(self):
        photos = {f: p for f, p in self._photos("x").items() if f != "top"}
        with self.assertRaises(services.DomainError) as cm:
            self._submit(photos=photos)
        self.assertEqual(cm.exception.code, "INCOMPLETE_COLLECTION")
        self.assertIn("照片缺面", cm.exception.message)

    def test_missing_dimension_blocks_submit(self):
        with self.assertRaises(services.DomainError) as cm:
            self._submit(measured=(300, 200, None))
        self.assertEqual(cm.exception.code, "INCOMPLETE_COLLECTION")

    def test_missing_slot_blocks_submit(self):
        with self.assertRaises(services.DomainError) as cm:
            self._submit(slot="  ")
        self.assertEqual(cm.exception.code, "INCOMPLETE_COLLECTION")

    def test_held_requires_reason(self):
        fa = self._submit()
        with self.assertRaises(services.DomainError) as cm:
            services.approve(self.conn, first_article_id=fa,
                             qc_user_id=self.qc1, decision="held", reason="  ")
        self.assertEqual(cm.exception.code, "REASON_REQUIRED")


class TestSelfApproval(CaseBase):
    def test_operator_cannot_approve(self):
        fa = self._submit()
        with self.assertRaises(services.DomainError) as cm:
            self._release(fa, qc=self.op)
        self.assertEqual(cm.exception.code, "NOT_QC")

    def test_submitter_qc_cannot_self_approve(self):
        # 质检员本人提交的首箱，不能自己批
        fa = self._submit(by=self.qc1)
        with self.assertRaises(services.DomainError) as cm:
            self._release(fa, qc=self.qc1)
        self.assertEqual(cm.exception.code, "SELF_APPROVAL")


class TestRevisionInvalidation(CaseBase):
    def test_supersede_blocks_new_submit(self):
        fa = self._submit()
        self._release(fa)
        services.supersede_revision(
            self.conn, order_id=self.oid, new_rev_code="V2",
            art_file=save_artwork(self.media, "O1", "V2"))
        # 旧版次下不能再提交首箱
        with self.assertRaises(services.DomainError) as cm:
            self._submit()
        self.assertEqual(cm.exception.code, "REVISION_SUPERSEDED")

    def test_old_conclusion_invalid_cannot_approve(self):
        fa = self._submit()
        services.supersede_revision(
            self.conn, order_id=self.oid, new_rev_code="V2",
            art_file=save_artwork(self.media, "O1", "V2"))
        # 换版前未审批的首箱，结论失效，不能补批
        with self.assertRaises(services.DomainError) as cm:
            self._release(fa)
        self.assertEqual(cm.exception.code, "REVISION_SUPERSEDED")

    def test_cannot_register_on_old_revision(self):
        services.supersede_revision(
            self.conn, order_id=self.oid, new_rev_code="V2",
            art_file=save_artwork(self.media, "O1", "V2"))
        with self.assertRaises(services.DomainError) as cm:
            services.register_production(
                self.conn, order_id=self.oid, revision_id=self.rid,
                paper_batch="P", die_no_actual="D-001", colors_actual="四色",
                box_type_actual="对口箱", line_actual="A线", registered_by=self.op)
        self.assertEqual(cm.exception.code, "REVISION_SUPERSEDED")

    def test_one_active_revision_only(self):
        services.supersede_revision(
            self.conn, order_id=self.oid, new_rev_code="V2",
            art_file=save_artwork(self.media, "O1", "V2"))
        actives = self.conn.execute(
            "SELECT COUNT(*) FROM revisions WHERE order_id=? AND active=1",
            (self.oid,)).fetchone()[0]
        self.assertEqual(actives, 1)


class TestApprovalRace(CaseBase):
    """两名质检员同时放行：仅一人成功（数据库唯一约束兜底）。"""

    def test_concurrent_release_same_article(self):
        fa = self._submit()
        results = []
        barrier = threading.Barrier(2)

        def release(qc):
            c = connect(self.db)
            try:
                barrier.wait()
                services.approve(c, first_article_id=fa, qc_user_id=qc,
                                 decision="released")
                results.append(("ok", qc))
            except services.DomainError as e:
                results.append((e.code, qc))
            finally:
                c.close()

        t1 = threading.Thread(target=release, args=(self.qc1,))
        t2 = threading.Thread(target=release, args=(self.qc2,))
        t1.start(); t2.start(); t1.join(); t2.join()

        oks = [r for r in results if r[0] == "ok"]
        fails = [r for r in results if r[0] in ("APPROVAL_RACE", "ALREADY_RELEASED")]
        self.assertEqual(len(oks), 1, results)
        self.assertEqual(len(fails), 1, results)

    def test_concurrent_release_different_articles_same_revision(self):
        """即使放行不同首箱，同一版次也只能有一份留样。"""
        fa1 = self._submit(slot="A-1")
        fa2 = self._submit(slot="A-2", by=self.op2)
        results = []
        barrier = threading.Barrier(2)

        def release(qc, fa):
            c = connect(self.db)
            try:
                barrier.wait()
                services.approve(c, first_article_id=fa, qc_user_id=qc,
                                 decision="released")
                results.append("ok")
            except services.DomainError as e:
                results.append(e.code)
            finally:
                c.close()

        t1 = threading.Thread(target=release, args=(self.qc1, fa1))
        t2 = threading.Thread(target=release, args=(self.qc2, fa2))
        t1.start(); t2.start(); t1.join(); t2.join()

        self.assertEqual(sorted(results), ["APPROVAL_RACE", "ok"], results)
        n = self.conn.execute("SELECT COUNT(*) FROM samples WHERE revision_id=?",
                              (self.rid,)).fetchone()[0]
        self.assertEqual(n, 1)


class TestSampleReadOnly(CaseBase):
    def test_released_article_locked(self):
        fa = self._submit()
        self._release(fa)
        self.assertTrue(services.is_locked_sample(self.conn, fa))
        # 重复放行拒绝
        with self.assertRaises(services.DomainError) as cm:
            self._release(fa, qc=self.qc2)
        self.assertEqual(cm.exception.code, "ALREADY_RELEASED")
        # 也不能改判停机
        with self.assertRaises(services.DomainError) as cm:
            services.approve(self.conn, first_article_id=fa,
                             qc_user_id=self.qc2, decision="held", reason="x")
        self.assertEqual(cm.exception.code, "ALREADY_RELEASED")

    def test_old_sample_remains_readonly_after_supersede(self):
        fa = self._submit(slot="柜-V1-01")
        self._release(fa)
        services.supersede_revision(
            self.conn, order_id=self.oid, new_rev_code="V2",
            art_file=save_artwork(self.media, "O1", "V2"))
        # 留样行仍在（归档只读），且仍标记为锁定
        self.assertTrue(services.is_locked_sample(self.conn, fa))
        row = self.conn.execute(
            "SELECT cabinet_slot FROM samples WHERE revision_id=?",
            (self.rid,)).fetchone()
        self.assertEqual(row["cabinet_slot"], "柜-V1-01")
        # 底层防篡改：直接改留样柜位之外的业务保护由 services 把关；
        # 这里确认旧留样照片仍可读
        n = self.conn.execute(
            "SELECT COUNT(*) FROM photos WHERE first_article_id=?", (fa,)).fetchone()[0]
        self.assertEqual(n, 6)

    def test_held_then_retest_flow(self):
        """停机原因 → 返工复测 → 复测放行；停机的首箱不能改判。"""
        bad = self._submit(measured=(300, 200, 158), slot="A-1")
        services.approve(self.conn, first_article_id=bad, qc_user_id=self.qc1,
                         decision="held", reason="高度超差 +8mm，调模返工")
        with self.assertRaises(services.DomainError) as cm:
            self._release(bad, qc=self.qc2)
        self.assertEqual(cm.exception.code, "ARTICLE_DECIDED")
        # 返工复测：第二条首箱合格
        good = self._submit(measured=(300, 200, 150), slot="A-2", by=self.op2)
        self._release(good, qc=self.qc2)
        self.assertTrue(services.is_locked_sample(self.conn, good))
        # 版次仍只有一份生效留样
        n = self.conn.execute("SELECT COUNT(*) FROM samples").fetchone()[0]
        self.assertEqual(n, 1)
        # 停机原因留存可查
        reason = self.conn.execute(
            "SELECT reason FROM approvals WHERE first_article_id=? AND decision='held'",
            (bad,)).fetchone()["reason"]
        self.assertIn("+8mm", reason)


    def test_old_sample_db_level_immutable(self):
        """旧留样在数据库层也无法篡改（触发器兜底，不只是页面隐藏入口）。"""
        import sqlite3
        fa = self._submit(slot="柜-V1-01")
        self._release(fa)
        services.supersede_revision(
            self.conn, order_id=self.oid, new_rev_code="V2",
            art_file=save_artwork(self.media, "O1", "V2"))
        for sql, params in (
            ("UPDATE samples SET cabinet_slot='X' WHERE first_article_id=?", (fa,)),
            ("DELETE FROM samples WHERE first_article_id=?", (fa,)),
            ("UPDATE first_articles SET measured_h=999 WHERE id=?", (fa,)),
            ("DELETE FROM first_articles WHERE id=?", (fa,)),
            ("DELETE FROM photos WHERE first_article_id=?", (fa,)),
        ):
            with self.assertRaises(sqlite3.IntegrityError):
                self.conn.execute(sql, params)


if __name__ == "__main__":
    unittest.main()
