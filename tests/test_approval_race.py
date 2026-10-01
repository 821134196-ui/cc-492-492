"""审批规则：操作员不能自批、仅质检可裁决、两人并发仅一人成功。"""
from __future__ import annotations

import threading
import unittest

from app import config
from app.errors import AlreadyApproved, SelfApproval, WrongRole
from tests.base import BaseCase


class ApprovalRuleTests(BaseCase):
    def setUp(self):
        super().setUp()
        self.rid = self.make_rev(self.make_order())

    def _pending_fa(self):
        return self.svc.submit_first_article(self.rid, self.good_capture())

    def test_operator_cannot_approve_own_article(self):
        fa = self._pending_fa()
        with self.assertRaises(SelfApproval):
            self.svc.decide(fa, self.op1, "release")

    def test_another_operator_still_cannot_approve(self):
        fa = self._pending_fa()
        with self.assertRaises(WrongRole):
            self.svc.decide(fa, self.op2, "release")

    def test_qc_release_then_second_qc_gets_already_approved(self):
        fa = self._pending_fa()
        self.svc.decide(fa, self.qc1, "release", note="王检先放行")
        with self.assertRaises(AlreadyApproved):
            self.svc.decide(fa, self.qc2, "release", note="陈检晚到一步")
        approval = self.svc.approval_of(fa)
        self.assertEqual(approval["qc_id"], self.qc1)

    def test_concurrent_two_qcs_exactly_one_succeeds(self):
        """两名质检员几乎同时放行：唯一索引 + IMMEDIATE 事务下仅一人成功。"""
        from app.db import Hub
        from app.services import Service
        fa = self._pending_fa()  # 主线程连接 autocommit，对其它连接立即可见
        results: list[Exception | str] = []
        barrier = threading.Barrier(2)

        def approve(qc_id: int) -> None:
            svc = Service(Hub(config.DB_PATH))  # 每线程独立连接、同一数据库文件
            barrier.wait()
            try:
                svc.decide(fa, qc_id, "release", note="并发裁决")
                results.append("ok")
            except AlreadyApproved as e:
                results.append(e)
            except Exception as e:  # 不应出现其他异常
                results.append(e)

        t1 = threading.Thread(target=approve, args=(self.qc1,))
        t2 = threading.Thread(target=approve, args=(self.qc2,))
        t1.start(); t2.start(); t1.join(10); t2.join(10)

        self.assertEqual(len(results), 2)
        oks = [r for r in results if r == "ok"]
        rejects = [r for r in results if isinstance(r, AlreadyApproved)]
        self.assertEqual(len(oks), 1, results)
        self.assertEqual(len(rejects), 1, results)
        # 库里恰好一条审批。
        n = self.svc._fetch("SELECT COUNT(*) AS n FROM approvals WHERE fa_id=?", (fa,))[0]["n"]
        self.assertEqual(n, 1)

    def test_qc_can_rework_blocked_article(self):
        fa = self.svc.submit_first_article(self.rid, self.good_capture(
            sample_intact=False))
        res = self.svc.decide(fa, self.qc1, "rework", reason="样箱破损")
        self.assertEqual(res["status"], "rejected")
        stops = self.svc._fetch("SELECT * FROM stoppages WHERE fa_id=?", (fa,))
        self.assertEqual(len(stops), 1)
        self.assertIn("样箱破损", stops[0]["reason"])


if __name__ == "__main__":
    unittest.main()
