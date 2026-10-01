"""业务规则层。

阻断放行的五类缺陷：条码不可读、尺寸超差、混用旧刀模、照片缺面、样箱破损；
另加采集缺项（尺寸/照片/柜位未填）阻断提交。
所有写操作走串行化事务（BEGIN IMMEDIATE），唯一约束兜底审批竞态。
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from .db import FACES


class DomainError(Exception):
    """业务规则拒绝，code 供页面与测试判别。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class Defect:
    code: str
    message: str


@dataclass
class ArticleCheck:
    defects: list[Defect] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.defects and not self.missing


def _revision_active(conn: sqlite3.Connection, revision_id: int) -> bool:
    row = conn.execute(
        "SELECT active FROM revisions WHERE id=?", (revision_id,)
    ).fetchone()
    return row is not None and row["active"] == 1


def evaluate_article(conn: sqlite3.Connection, fa_id: int) -> ArticleCheck:
    """对已保存的首箱做完整核对，返回缺陷与缺项清单。"""
    chk = ArticleCheck()
    fa = conn.execute(
        """SELECT fa.*, o.die_no, o.barcode, o.target_l, o.target_w, o.target_h,
                  o.tol_mm, p.die_no_actual
           FROM first_articles fa
           JOIN productions p ON p.id = fa.production_id
           JOIN orders o ON o.id = p.order_id
           WHERE fa.id=?""",
        (fa_id,),
    ).fetchone()
    if fa is None:
        raise DomainError("NOT_FOUND", "首箱记录不存在")

    # ---- 缺项（采集不完整）----
    for key, label in (
        ("measured_l", "实测长"), ("measured_w", "实测宽"), ("measured_h", "实测高"),
    ):
        if fa[key] is None:
            chk.missing.append(f"缺少{label}")
    if fa["barcode_ok"] is None:
        chk.missing.append("缺少条码扫描结果")
    if fa["cabinet_slot"] is None or not str(fa["cabinet_slot"]).strip():
        chk.missing.append("缺少留样柜位")
    if fa["sample_intact"] is None:
        chk.missing.append("未确认样箱完好状态")

    faces = {
        r["face"] for r in conn.execute(
            "SELECT face FROM photos WHERE first_article_id=?", (fa_id,)
        )
    }
    for f in FACES:
        if f not in faces:
            chk.missing.append("照片缺面")
            break

    # ---- 五类阻断缺陷（带具体数值，页面要显示）----
    if fa["barcode_ok"] is not None and fa["barcode_ok"] == 0:
        chk.defects.append(Defect("BARCODE_UNREADABLE", "条码不可读：扫码枪未能识别"))
    elif fa["barcode_scan"] is not None and fa["barcode_scan"] != fa["barcode"]:
        chk.defects.append(Defect(
            "BARCODE_MISMATCH",
            f"条码内容不符：实测 {fa['barcode_scan']!r}，应为 {fa['barcode']!r}",
        ))

    tol = fa["tol_mm"]
    for axis, measured, target in (
        ("长", fa["measured_l"], fa["target_l"]),
        ("宽", fa["measured_w"], fa["target_w"]),
        ("高", fa["measured_h"], fa["target_h"]),
    ):
        if measured is not None:
            diff = round(measured - target, 2)
            if abs(diff) > tol:
                chk.defects.append(Defect(
                    "DIM_OUT_OF_TOL",
                    f"{axis}尺寸超差：实测 {measured:g}mm，目标 {target:g}mm，"
                    f"偏差 {diff:+g}mm，允许 ±{tol:g}mm",
                ))

    if (fa["die_no_actual"] or "").strip() != (fa["die_no"] or "").strip():
        chk.defects.append(Defect(
            "DIE_MISMATCH",
            f"混用旧刀模：上机刀模 {fa['die_no_actual']!r}，"
            f"订单要求 {fa['die_no']!r}",
        ))

    if fa["sample_intact"] == 0:
        chk.defects.append(Defect("SAMPLE_DAMAGED", "样箱破损，不得作为留样"))

    return chk


def register_production(
    conn: sqlite3.Connection, *, order_id: int, revision_id: int,
    paper_batch: str, die_no_actual: str, colors_actual: str,
    box_type_actual: str, line_actual: str, registered_by: int,
) -> int:
    user = conn.execute("SELECT role FROM users WHERE id=?", (registered_by,)).fetchone()
    if user is None:
        raise DomainError("NO_USER", "登记人不存在")
    rev = conn.execute(
        "SELECT id, active, order_id FROM revisions WHERE id=?", (revision_id,)
    ).fetchone()
    if rev is None or rev["order_id"] != order_id:
        raise DomainError("BAD_REVISION", "版次不属于该订单")
    if rev["active"] != 1:
        raise DomainError("REVISION_SUPERSEDED", "该版次已被新版稿件替代，不能据此投产")
    for name, val in (("纸张批次", paper_batch), ("刀模编号", die_no_actual),
                      ("印色", colors_actual), ("箱型", box_type_actual), ("产线", line_actual)):
        if not str(val or "").strip():
            raise DomainError("MISSING_FIELD", f"投产登记缺项：{name}")
    conn.execute("BEGIN IMMEDIATE")
    try:
        cur = conn.execute(
            """INSERT INTO productions
               (order_id, revision_id, paper_batch, die_no_actual, colors_actual,
                box_type_actual, line_actual, registered_by)
               VALUES (?,?,?,?,?,?,?,?)""",
            (order_id, revision_id, paper_batch, die_no_actual, colors_actual,
             box_type_actual, line_actual, registered_by),
        )
        conn.commit()
        return cur.lastrowid
    except Exception:
        conn.rollback()
        raise


def submit_first_article(
    conn: sqlite3.Connection, *, production_id: int,
    measured_l: float | None, measured_w: float | None, measured_h: float | None,
    barcode_scan: str | None, barcode_ok: int | None,
    cabinet_slot: str | None, sample_intact: int | None,
    submitted_by: int, photos: dict[str, str],
) -> int:
    """保存首箱采集。照片 face->file_path 必须六面齐全，否则缺项阻断。"""
    user = conn.execute("SELECT role FROM users WHERE id=?", (submitted_by,)).fetchone()
    if user is None:
        raise DomainError("NO_USER", "提交人不存在")
    prod = conn.execute(
        "SELECT id, revision_id FROM productions WHERE id=?", (production_id,)
    ).fetchone()
    if prod is None:
        raise DomainError("NO_PRODUCTION", "投产登记不存在")
    if not _revision_active(conn, prod["revision_id"]):
        raise DomainError("REVISION_SUPERSEDED",
                          "稿件版次已变更，请按新版次重新登记投产与首箱")

    missing_faces = [f for f in FACES if not photos.get(f)]
    if missing_faces or measured_l is None or measured_w is None or measured_h is None \
            or barcode_ok is None or not str(cabinet_slot or "").strip() \
            or sample_intact is None:
        parts = []
        if measured_l is None or measured_w is None or measured_h is None:
            parts.append("实测尺寸不完整")
        if barcode_ok is None:
            parts.append("缺少条码扫描结果")
        if not str(cabinet_slot or "").strip():
            parts.append("缺少留样柜位")
        if sample_intact is None:
            parts.append("未确认样箱完好")
        if missing_faces:
            parts.append("照片缺面：" + "、".join(missing_faces))
        raise DomainError("INCOMPLETE_COLLECTION", "首箱采集缺项，阻断提交：" + "；".join(parts))

    conn.execute("BEGIN IMMEDIATE")
    try:
        seq = conn.execute(
            "SELECT COALESCE(MAX(seq),0)+1 FROM first_articles WHERE production_id=?",
            (production_id,),
        ).fetchone()[0]
        cur = conn.execute(
            """INSERT INTO first_articles
               (production_id, seq, measured_l, measured_w, measured_h,
                barcode_scan, barcode_ok, cabinet_slot, sample_intact, submitted_by)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (production_id, seq, measured_l, measured_w, measured_h,
             barcode_scan, barcode_ok, cabinet_slot.strip(), sample_intact, submitted_by),
        )
        fa_id = cur.lastrowid
        for face in FACES:
            conn.execute(
                "INSERT INTO photos(first_article_id, face, file_path) VALUES (?,?,?)",
                (fa_id, face, photos[face]),
            )
        conn.commit()
        return fa_id
    except Exception:
        conn.rollback()
        raise


def approve(
    conn: sqlite3.Connection, *, first_article_id: int, qc_user_id: int,
    decision: str, reason: str = "",
) -> int:
    """质检员放行/停机。返回 approval id；竞态失败者抛 APPROVAL_RACE。"""
    if decision not in ("released", "held"):
        raise DomainError("BAD_DECISION", "决策只能是 released / held")

    conn.execute("BEGIN IMMEDIATE")
    try:
        qc = conn.execute("SELECT * FROM users WHERE id=?", (qc_user_id,)).fetchone()
        if qc is None:
            raise DomainError("NO_USER", "质检员不存在")
        if qc["role"] != "qc":
            raise DomainError("NOT_QC", "只有质检员可以审批首箱")

        fa = conn.execute(
            """SELECT fa.*, p.revision_id FROM first_articles fa
               JOIN productions p ON p.id=fa.production_id
               WHERE fa.id=?""",
            (first_article_id,),
        ).fetchone()
        if fa is None:
            raise DomainError("NO_ARTICLE", "首箱不存在")
        if fa["submitted_by"] == qc_user_id:
            raise DomainError("SELF_APPROVAL", "操作员不能批准自己提交的首箱")
        if not _revision_active(conn, fa["revision_id"]):
            raise DomainError("REVISION_SUPERSEDED",
                              "稿件版次已变更，该首箱结论已失效，不能审批")

        existing = conn.execute(
            "SELECT decision FROM approvals WHERE first_article_id=?",
            (first_article_id,),
        ).fetchall()
        if any(r["decision"] == "released" for r in existing):
            raise DomainError("ALREADY_RELEASED", "该首箱已放行，留样只读，不可重复审批")
        if existing:
            raise DomainError("ARTICLE_DECIDED",
                              "该首箱已被判定停机返工，须返工后重新采集复测，不得改判")

        if decision == "held" and not reason.strip():
            raise DomainError("REASON_REQUIRED", "停机返工必须填写停机原因")

        chk = evaluate_article(conn, first_article_id)
        if decision == "released" and not chk.ok:
            msgs = [m for m in chk.missing] + [d.message for d in chk.defects]
            raise DomainError("BLOCKED", "阻断放行：" + "；".join(msgs))

        try:
            cur = conn.execute(
                """INSERT INTO approvals(first_article_id, qc_user_id, decision, reason)
                   VALUES (?,?,?,?)""",
                (first_article_id, qc_user_id, decision, reason.strip() or None),
            )
        except sqlite3.IntegrityError:
            # unique_release_per_article：两名质检员同时放行同一首箱
            raise DomainError("APPROVAL_RACE", "另一质检员已先行放行，仅一人可成功")
        approval_id = cur.lastrowid

        if decision == "released":
            try:
                conn.execute(
                    """INSERT INTO samples
                       (revision_id, production_id, first_article_id, cabinet_slot)
                       VALUES (?,?,?,?)""",
                    (fa["revision_id"], fa["production_id"],
                     first_article_id, fa["cabinet_slot"]),
                )
            except sqlite3.IntegrityError:
                # 同版次已有留样：并发放行不同首箱，也只允许一人成功
                raise DomainError("APPROVAL_RACE",
                                  "同一版次仅允许一份留样，另一质检员已放行")
        conn.commit()
        return approval_id
    except Exception:
        conn.rollback()
        raise


def supersede_revision(conn: sqlite3.Connection, *, order_id: int,
                       new_rev_code: str, art_file: str) -> int:
    """稿件换版：旧版置失效，旧首箱结论随之失效，旧留样转为只读归档。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        old = conn.execute(
            "SELECT id FROM revisions WHERE order_id=? AND active=1", (order_id,)
        ).fetchone()
        if old is None:
            raise DomainError("NO_ACTIVE_REV", "该订单没有生效中的版次")
        dup = conn.execute(
            "SELECT 1 FROM revisions WHERE order_id=? AND rev_code=?",
            (order_id, new_rev_code),
        ).fetchone()
        if dup:
            raise DomainError("DUP_REV", "版次号已存在")
        conn.execute("UPDATE revisions SET active=0 WHERE id=?", (old["id"],))
        cur = conn.execute(
            "INSERT INTO revisions(order_id, rev_code, art_file, active) VALUES (?,?,?,1)",
            (order_id, new_rev_code, art_file),
        )
        conn.commit()
        return cur.lastrowid
    except Exception:
        conn.rollback()
        raise


def article_status(conn: sqlite3.Connection, fa_id: int) -> str:
    row = conn.execute(
        """SELECT a.decision FROM approvals a WHERE a.first_article_id=?
           ORDER BY a.id DESC LIMIT 1""",
        (fa_id,),
    ).fetchone()
    return row["decision"] if row else "pending"


def is_locked_sample(conn: sqlite3.Connection, fa_id: int) -> bool:
    """已入柜留样（含旧版归档留样）只读：任何修改/删除都拒绝。"""
    row = conn.execute(
        "SELECT 1 FROM samples WHERE first_article_id=?", (fa_id,)
    ).fetchone()
    return row is not None


def revision_state(conn: sqlite3.Connection, revision_id: int) -> dict | None:
    """版次及其留样状态，用于看板/只读判定。"""
    row = conn.execute(
        """SELECT r.*, s.first_article_id AS sample_fa_id, s.cabinet_slot
           FROM revisions r
           LEFT JOIN samples s ON s.revision_id = r.id
           WHERE r.id=?""",
        (revision_id,),
    ).fetchone()
    return dict(row) if row else None
