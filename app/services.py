"""领域服务：版次、首箱采集、阻断判定、审批竞态、停机返工复测、留样。

关键规则（全部在此层强制，UI 只负责呈现）：

1. 登记新版次会使同订单旧版次立即失效（superseded），旧首箱结论
   ``conclusion_valid=0``，旧留样归档只读——“稿件版本变更会使旧首箱结论失效”。
2. 同一订单同一版次最多一份生效留样（DB 部分唯一索引 + 状态守卫）。
3. 阻断放行五类：条码不可读/不符、尺寸超差、混用旧刀模、照片缺面、样箱破损。
4. 操作员不能批准自己的首箱；同一首箱的放行由唯一索引 + IMMEDIATE 事务保证
   两名质检员并发提交时仅一人成功。
5. 停机返工后新建复测轮次，沿 prev_fa_id 成链。
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from . import config, storage
from .db import Hub
from .errors import (
    AlreadyApproved,
    BlockingFinding,
    DuplicateActiveSample,
    IllegalTransition,
    MissingItem,
    RevisionStale,
    SampleLocked,
    SelfApproval,
    WrongRole,
)
from .scanner import MockBarcodeScanner

BLOCK_CODES = {
    "barcode_unreadable": "条码不可读",
    "barcode_mismatch": "条码与订单不符",
    "dim_over": "尺寸超差",
    "die_mismatch": "混用旧刀模",
    "photo_missing": "照片缺面",
    "sample_damaged": "样箱破损",
}


@dataclass
class DimCheck:
    axis: str
    label: str
    target: float
    measured: float | None

    @property
    def delta(self) -> float | None:
        return None if self.measured is None else round(self.measured - self.target, 1)

    @property
    def over(self) -> bool:
        return self.delta is None or abs(self.delta) > config.TOL_MM

    def message(self) -> str:
        if self.measured is None:
            return f"{self.label}未测量（目标 {self.target:g} mm，公差 ±{config.TOL_MM:g}）"
        sign = "+" if (self.delta or 0) > 0 else ""
        return (f"{self.label}超差：实测 {self.measured:g} mm / 目标 {self.target:g} mm，"
                f"偏差 {sign}{self.delta:g} mm（允许 ±{config.TOL_MM:g}）")


@dataclass
class CaptureInput:
    """首箱采集入参。photos 只给已拍到的面；缺面由服务层判定。"""

    operator_id: int
    measured: dict[str, float | None]          # keys: l/w/h
    barcode_ok: bool
    barcode_payload: str = ""
    barcode_detail: str = ""
    die_scanned: str = ""
    sample_intact: bool = True
    photos: dict[str, bytes] = field(default_factory=dict)  # face -> svg bytes
    prev_fa_id: int | None = None
    scanner: MockBarcodeScanner | None = None  # 给了就现场模拟扫码，覆盖手工字段


class Service:
    def __init__(self, hub_or_conn):
        """接受 :class:`~app.db.Hub`（多线程 Web/竞态场景）或单个连接（测试/脚本）。"""
        self._hub = hub_or_conn
        self._owns_conn = not isinstance(hub_or_conn, Hub)

    @property
    def conn(self) -> sqlite3.Connection:
        return self._hub.conn if isinstance(self._hub, Hub) else self._hub

    # ---------- 查询辅助 ----------

    def get_user(self, login: str) -> sqlite3.Row:
        row = self.conn.execute("SELECT * FROM users WHERE login=?", (login,)).fetchone()
        if row is None:
            raise LookupError(login)
        return row

    def get_order(self, order_id: int) -> sqlite3.Row:
        row = self.conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
        if row is None:
            raise LookupError(f"订单不存在: {order_id}")
        return row

    def get_revision(self, rev_id: int) -> sqlite3.Row:
        row = self.conn.execute("SELECT * FROM revisions WHERE id=?", (rev_id,)).fetchone()
        if row is None:
            raise LookupError(f"版次不存在: {rev_id}")
        return row

    def get_fa(self, fa_id: int) -> sqlite3.Row:
        row = self.conn.execute("SELECT * FROM first_articles WHERE id=?", (fa_id,)).fetchone()
        if row is None:
            raise LookupError(f"首箱不存在: {fa_id}")
        return row

    def _fetch(self, sql: str, args=()):
        return self.conn.execute(sql, args).fetchall()

    # ---------- 订单与版次 ----------

    def create_order(self, code: str, customer: str, product: str, box_type: str,
                     line: str, die_no: str, paper_batch: str, colors: list[str],
                     targets: tuple[float, float, float]) -> int:
        cur = self.conn.execute(
            """INSERT INTO orders(code,customer,product,box_type,line,die_no,
                                  paper_batch,colors,target_l,target_w,target_h)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (code, customer, product, box_type, line, die_no, paper_batch,
             ",".join(colors), *targets),
        )
        return cur.lastrowid

    def register_revision(self, order_id: int, rev: str, die_no: str,
                          paper_batch: str, colors: list[str],
                          artwork: bytes, artwork_name: str = "artwork.svg") -> int:
        """登记新版次：旧版次立即失效、旧首箱结论作废、旧留样封档只读。"""
        order = self.get_order(order_id)
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            old = self.conn.execute(
                "SELECT id FROM revisions WHERE order_id=? AND status='active'",
                (order_id,)).fetchone()
            if old is not None:
                old_id = old["id"]
                self.conn.execute(
                    "UPDATE revisions SET status='superseded' WHERE id=?", (old_id,))
                # 旧首箱结论全部失效（不论此前放行与否）。
                self.conn.execute(
                    "UPDATE first_articles SET conclusion_valid=0 WHERE revision_id=?",
                    (old_id,))
                # 旧留样归档，柜位释放，照片目录封档只读。
                rows = self.conn.execute(
                    """SELECT rs.id, p.path FROM retained_samples rs
                       JOIN photos p ON p.fa_id=rs.fa_id
                       WHERE rs.revision_id=? AND rs.state='active'""",
                    (old_id,)).fetchall()
                paths = sorted({r["path"] for r in rows})
                self.conn.execute(
                    "UPDATE retained_samples SET state='archived' WHERE revision_id=?",
                    (old_id,))
            rel, sha = storage.save_artwork(order["code"], rev, artwork_name, artwork)
            cur = self.conn.execute(
                """INSERT INTO revisions(order_id,rev,artwork_path,artwork_sha256,
                                         die_no,paper_batch,colors)
                   VALUES(?,?,?,?,?,?,?)""",
                (order_id, rev, rel, sha, die_no, paper_batch, ",".join(colors)),
            )
            new_id = cur.lastrowid
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        if old is not None and paths:
            storage.lock_readonly(paths)
        return new_id

    # ---------- 首箱采集与判定 ----------

    @staticmethod
    def evaluate(revision: sqlite3.Row, order: sqlite3.Row,
                 data: CaptureInput) -> list[dict]:
        """纯函数判定：返回 findings 列表（dict），不写库。"""
        findings: list[dict] = []

        # 1) 条码
        if data.scanner is not None:
            res = data.scanner.scan(expected=order["code"])
            data.barcode_ok, data.barcode_payload = res.ok, res.payload
            data.barcode_detail = res.detail
        if not data.barcode_ok:
            if not data.barcode_payload:
                findings.append({"code": "barcode_unreadable", "face": None,
                                 "message": "条码不可读：扫描枪返回 NO_READ，无法核验箱码"})
            else:
                findings.append({"code": "barcode_mismatch", "face": None,
                                 "message": data.barcode_detail
                                            or f"条码与订单不符：{data.barcode_payload}"})

        # 2) 尺寸（实测/目标并排，偏差超 ±TOL_MM 记录具体数值）
        for axis, label in (("l", "长"), ("w", "宽"), ("h", "高")):
            chk = DimCheck(axis, label, order[f"target_{axis}"],
                           data.measured.get(axis))
            if chk.over:
                findings.append({"code": "dim_over", "face": None,
                                 "message": chk.message()})

        # 3) 刀模：上机扫描值必须等于本版次登记刀模，否则视为混用旧刀模。
        if data.die_scanned and data.die_scanned != revision["die_no"]:
            findings.append({
                "code": "die_mismatch", "face": None,
                "message": (f"混用旧刀模：上机刀模 {data.die_scanned}，"
                            f"本版次要求 {revision['die_no']}")})

        # 4) 六面照片缺一不可
        for face in config.FACES:
            if face not in data.photos:
                findings.append({
                    "code": "photo_missing", "face": face,
                    "message": f"照片缺面：缺少{config.FACE_LABELS[face]}照片"})

        # 5) 样箱完好
        if not data.sample_intact:
            findings.append({"code": "sample_damaged", "face": None,
                             "message": "样箱破损：箱体存在开裂/压溃，不得留样放行"})
        return findings

    def submit_first_article(self, revision_id: int, data: CaptureInput) -> int:
        revision = self.get_revision(revision_id)
        order = self.get_order(revision["order_id"])
        if revision["status"] != "active":
            raise RevisionStale(f"版次 {order['code']}/{revision['rev']} 已失效，"
                                "请在新版次上重做首箱")
        findings = self.evaluate(revision, order, data)
        blocked = bool(findings)

        self.conn.execute("BEGIN IMMEDIATE")
        try:
            seq = self.conn.execute(
                "SELECT COALESCE(MAX(sequence),0)+1 AS n FROM first_articles WHERE revision_id=?",
                (revision_id,)).fetchone()["n"]
            m = data.measured
            cur = self.conn.execute(
                """INSERT INTO first_articles(revision_id,sequence,operator_id,
                    measured_l,measured_w,measured_h,target_l,target_w,target_h,
                    barcode_scan,barcode_ok,die_scanned,sample_intact,prev_fa_id,status)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (revision_id, seq, data.operator_id,
                 m.get("l"), m.get("w"), m.get("h"),
                 order["target_l"], order["target_w"], order["target_h"],
                 data.barcode_payload, int(data.barcode_ok), data.die_scanned,
                 int(data.sample_intact), data.prev_fa_id,
                 "blocked" if blocked else "pending"),
            )
            fa_id = cur.lastrowid
            for face, img in data.photos.items():
                rel, sha = storage.save_photo(fa_id, face, img)
                self.conn.execute(
                    "INSERT INTO photos(fa_id,face,path,sha256) VALUES(?,?,?,?)",
                    (fa_id, face, rel, sha))
            for f in findings:
                self.conn.execute(
                    "INSERT INTO findings(fa_id,code,face,message,blocking) VALUES(?,?,?,?,1)",
                    (fa_id, f["code"], f["face"], f["message"]))
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return fa_id

    def next_sequence(self, revision_id: int) -> int:
        return self.conn.execute(
            "SELECT COALESCE(MAX(sequence),0)+1 AS n FROM first_articles WHERE revision_id=?",
            (revision_id,)).fetchone()["n"]

    def list_findings(self, fa_id: int) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT * FROM findings WHERE fa_id=? ORDER BY id", (fa_id,)))

    # ---------- 审批（放行 / 停机返工） ----------

    def decide(self, fa_id: int, qc_id: int, decision: str,
               reason: str = "", note: str = "") -> dict:
        """质检员裁决。返回事务结果；并发时第二个提交者拿 AlreadyApproved。"""
        if decision not in ("release", "rework"):
            raise IllegalTransition("decision 必须是 release/rework")
        fa = self.get_fa(fa_id)
        revision = self.get_revision(fa["revision_id"])
        qc = self.conn.execute("SELECT * FROM users WHERE id=?", (qc_id,)).fetchone()
        if qc is None:
            raise WrongRole("只有质检员可以放行或判返工")
        # 先判“不能批准自己的首箱”，再判角色，给出更贴切的拒绝原因。
        if fa["operator_id"] == qc_id:
            raise SelfApproval("操作员不能批准自己的首箱")
        if qc["role"] != "qc":
            raise WrongRole("只有质检员可以放行或判返工")
        if not fa["conclusion_valid"] or revision["status"] != "active":
            raise RevisionStale("版次已变更，该首箱结论已失效，禁止补批")

        self.conn.execute("BEGIN IMMEDIATE")
        try:
            exists = self.conn.execute(
                "SELECT id FROM approvals WHERE fa_id=?", (fa_id,)).fetchone()
            if exists is not None:
                self.conn.execute("ROLLBACK")
                raise AlreadyApproved("该首箱已有裁决，两名质检员中仅一人可成功放行")

            findings = self.conn.execute(
                "SELECT COUNT(*) AS n FROM findings WHERE fa_id=? AND blocking=1",
                (fa_id,)).fetchone()["n"]
            if decision == "release" and findings:
                self.conn.execute("ROLLBACK")
                raise BlockingFinding(f"存在 {findings} 项阻断差异，必须停机返工后复测")

            self.conn.execute(
                "INSERT INTO approvals(fa_id,qc_id,decision,note) VALUES(?,?,?,?)",
                (fa_id, qc_id, decision, note))
            new_status = "approved" if decision == "release" else "rejected"
            self.conn.execute(
                "UPDATE first_articles SET status=? WHERE id=?", (new_status, fa_id))
            if decision == "rework":
                self.conn.execute(
                    "INSERT INTO stoppages(fa_id,reason,qc_id) VALUES(?,?,?)",
                    (fa_id, reason or "首箱检验不合格，停机返工", qc_id))
            self.conn.execute("COMMIT")
        except (AlreadyApproved, BlockingFinding, RevisionStale):
            raise
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return {"fa_id": fa_id, "status": new_status, "qc": qc["name"]}

    # ---------- 返工复测 ----------

    def rework_retest(self, failed_fa_id: int, data: CaptureInput) -> int:
        """以停机首箱为前序，提交一轮返工复测采集。"""
        fa = self.get_fa(failed_fa_id)
        approval = self.conn.execute(
            "SELECT * FROM approvals WHERE fa_id=?", (failed_fa_id,)).fetchone()
        if approval is None or approval["decision"] != "rework":
            raise IllegalTransition("只有已判停机返工的首箱才能发起复测")
        data.prev_fa_id = failed_fa_id
        return self.submit_first_article(fa["revision_id"], data)

    # ---------- 留样 ----------

    def retain_sample(self, fa_id: int, cabinet: str) -> int:
        fa = self.get_fa(fa_id)
        revision = self.get_revision(fa["revision_id"])
        approval = self.conn.execute(
            "SELECT * FROM approvals WHERE fa_id=?", (fa_id,)).fetchone()
        if approval is None or approval["decision"] != "release":
            raise IllegalTransition("只有放行首箱可以登记留样")
        if not fa["conclusion_valid"] or revision["status"] != "active":
            raise SampleLocked("版次已失效，旧留样仅供只读查阅，不能再登记")
        try:
            cur = self.conn.execute(
                """INSERT INTO retained_samples(order_id,revision_id,fa_id,cabinet,state)
                   VALUES(?,?,?,?,'active')""",
                (revision["order_id"], revision["id"], fa_id, cabinet))
        except sqlite3.IntegrityError as e:
            msg = str(e)
            if "idx_retain_one_active" in msg or \
                    ("order_id" in msg and "revision_id" in msg):
                raise DuplicateActiveSample("同一订单同一版次只能有一份生效留样")
            if "idx_retain_cabinet" in msg or "cabinet" in msg:
                raise DuplicateActiveSample(f"留样柜位 {cabinet} 已被其他生效留样占用")
            raise
        return cur.lastrowid

    def assert_sample_writable(self, sample_id: int) -> None:
        """旧留样只读：任何修改/移位/删除归档留样的入口都先过这里。"""
        row = self.conn.execute(
            "SELECT state FROM retained_samples WHERE id=?", (sample_id,)).fetchone()
        if row is None:
            raise LookupError(sample_id)
        if row["state"] != "active":
            raise SampleLocked("该留样随旧版次封档，柜位与照片只读")

    # ---------- 看板汇总 ----------

    def dashboard(self) -> dict:
        orders = self.conn.execute(
            """SELECT o.*,
                      (SELECT rev FROM revisions r WHERE r.order_id=o.id
                       AND r.status='active' LIMIT 1) AS active_rev,
                      (SELECT COUNT(*) FROM revisions r WHERE r.order_id=o.id) AS rev_count
               FROM orders o ORDER BY o.id""").fetchall()
        rows = []
        for o in orders:
            rev = self.conn.execute(
                "SELECT * FROM revisions WHERE order_id=? AND status='active'",
                (o["id"],)).fetchone()
            latest = None
            if rev:
                latest = self.conn.execute(
                    """SELECT fa.*, u.name AS operator_name FROM first_articles fa
                       JOIN users u ON u.id=fa.operator_id
                       WHERE fa.revision_id=? ORDER BY fa.sequence DESC LIMIT 1""",
                    (rev["id"],)).fetchone()
            rows.append({"order": o, "revision": rev, "latest": latest})
        pending = self.conn.execute(
            """SELECT fa.*, o.code AS order_code, r.rev, u.name AS operator_name
               FROM first_articles fa
               JOIN revisions r ON r.id=fa.revision_id
               JOIN orders o ON o.id=r.order_id
               JOIN users u ON u.id=fa.operator_id
               WHERE fa.status='pending' AND fa.conclusion_valid=1
               ORDER BY fa.id""").fetchall()
        stoppages = self.conn.execute(
            """SELECT s.*, o.code AS order_code, r.rev, fa.sequence
               FROM stoppages s
               JOIN first_articles fa ON fa.id=s.fa_id
               JOIN revisions r ON r.id=fa.revision_id
               JOIN orders o ON o.id=r.order_id
               ORDER BY s.id DESC""").fetchall()
        samples = self.conn.execute(
            """SELECT rs.*, o.code AS order_code, r.rev, fa.sequence,
                      u.name AS qc_name
               FROM retained_samples rs
               JOIN orders o ON o.id=rs.order_id
               JOIN revisions r ON r.id=rs.revision_id
               JOIN first_articles fa ON fa.id=rs.fa_id
               JOIN approvals a ON a.fa_id=fa.id
               JOIN users u ON u.id=a.qc_id
               ORDER BY rs.state, rs.id""").fetchall()
        return {"rows": rows, "pending": pending, "stoppages": stoppages,
                "samples": samples}

    def revision_chain(self, order_id: int) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT * FROM revisions WHERE order_id=? ORDER BY id", (order_id,)))

    def fa_chain(self, revision_id: int) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            """SELECT fa.*, u.name AS operator_name
               FROM first_articles fa JOIN users u ON u.id=fa.operator_id
               WHERE revision_id=? ORDER BY sequence""", (revision_id,)))

    def photos_of(self, fa_id: int) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT * FROM photos WHERE fa_id=? ORDER BY face", (fa_id,)))

    def approval_of(self, fa_id: int):
        return self.conn.execute(
            """SELECT a.*, u.name AS qc_name FROM approvals a
               JOIN users u ON u.id=a.qc_id WHERE a.fa_id=?""",
            (fa_id,)).fetchone()
