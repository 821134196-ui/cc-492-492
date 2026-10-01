"""种子数据：演示错版归档、停机返工复测、待判定阻断三类样例。

重复执行会重建 data/ 目录（仅演示用）。
"""
from __future__ import annotations

import shutil
from pathlib import Path

from . import services
from .db import connect, init_db
from .devices import scanner
from .media import ensure_dirs, save_artwork, save_six_photos


def seed(db_path: str = "data/factory.db", media_root: str = "data/media",
         reset: bool = True) -> None:
    if reset:
        for p in (db_path, str(db_path) + "-wal", str(db_path) + "-shm"):
            Path(p).unlink(missing_ok=True)
        shutil.rmtree(media_root, ignore_errors=True)
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    ensure_dirs(media_root)

    conn = connect(db_path)
    init_db(conn)

    def user(name, role):
        cur = conn.execute("INSERT INTO users(name,role) VALUES(?,?)", (name, role))
        return cur.lastrowid

    wang = user("王建国", "operator")
    li = user("李秀英", "operator")
    zhang = user("张质检", "qc")
    zhao = user("赵质检", "qc")

    def order(no, customer, **kw):
        d = dict(box_type="对口箱", line="A线", colors="四色+专蓝",
                 die_no=f"D-{no[-3:]}", target_l=300, target_w=200, target_h=150,
                 tol_mm=2.0, barcode=f"BC-{no}")
        d.update(kw)
        cur = conn.execute(
            """INSERT INTO orders(order_no,customer,box_type,line,colors,die_no,
               target_l,target_w,target_h,tol_mm,barcode)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (no, customer, d["box_type"], d["line"], d["colors"], d["die_no"],
             d["target_l"], d["target_w"], d["target_h"], d["tol_mm"], d["barcode"]))
        return cur.lastrowid, d

    def revision(oid, code):
        o = conn.execute("SELECT order_no FROM orders WHERE id=?", (oid,)).fetchone()
        rel = save_artwork(media_root, o["order_no"], code)
        cur = conn.execute(
            "INSERT INTO revisions(order_id,rev_code,art_file,active) VALUES(?,?,?,1)",
            (oid, code, rel))
        return cur.lastrowid

    def six(pid, seq, **overrides):
        token = f"p{pid}_fa{seq}"
        paths = save_six_photos(media_root, token,
                                damaged_face=overrides.get("damaged_face"))
        return paths

    def submit(pid, seq, paths, *, measured=(300, 200, 150), scan_ok=True,
               barcode=None, intact=1, slot=None, by=wang):
        o = conn.execute(
            "SELECT o.barcode FROM productions p JOIN orders o ON o.id=p.order_id "
            "WHERE p.id=?", (pid,)).fetchone()
        content = o["barcode"] if scan_ok else None
        if barcode is not None:
            content = barcode
        fa = services.submit_first_article(
            conn, production_id=pid,
            measured_l=measured[0], measured_w=measured[1], measured_h=measured[2],
            barcode_scan=content, barcode_ok=1 if scan_ok else 0,
            cabinet_slot=slot or f"A-{pid:02d}-{seq}",
            sample_intact=intact, submitted_by=by, photos=paths)
        return fa

    # ---- 订单1：错版 + 停机返工复测 完整链路 ----
    o1, d1 = order("ORD-2401", "明远食品")
    r1v1 = revision(o1, "V1")
    p1v1 = services.register_production(
        conn, order_id=o1, revision_id=r1v1, paper_batch="P-0928-A",
        die_no_actual=d1["die_no"], colors_actual=d1["colors"],
        box_type_actual=d1["box_type"], line_actual=d1["line"], registered_by=wang)
    fa_old = submit(p1v1, 1, six(p1v1, 1), slot="A-01-1")
    services.approve(conn, first_article_id=fa_old, qc_user_id=zhang,
                     decision="released")
    # 客户改稿 → V1 失效，旧留样转入只读归档（错版样例）
    r1v2 = services.supersede_revision(
        conn, order_id=o1, new_rev_code="V2",
        art_file=save_artwork(media_root, "ORD-2401", "V2"))
    p1v2 = services.register_production(
        conn, order_id=o1, revision_id=r1v2, paper_batch="P-1001-A",
        die_no_actual=d1["die_no"], colors_actual=d1["colors"],
        box_type_actual=d1["box_type"], line_actual=d1["line"], registered_by=wang)
    # 首箱高度超差 → 张质检停机返工（差异标注 + 停机原因）
    fa_bad = submit(p1v2, 1, six(p1v2, 1), measured=(300, 200, 156.5))
    services.approve(
        conn, first_article_id=fa_bad, qc_user_id=zhang, decision="held",
        reason="高度 156.5mm，超差 +6.5mm（允许 ±2mm）；调模后返工复测")
    # 返工复测合格 → 赵质检放行，V2 留样入柜
    fa_ok = submit(p1v2, 2, six(p1v2, 2), measured=(300.2, 199.5, 150.1),
                   slot="A-02-2", by=li)
    services.approve(conn, first_article_id=fa_ok, qc_user_id=zhao,
                     decision="released")

    # ---- 订单2：首箱已提交、待质检，存在超差（现场演示阻断放行）----
    o2, d2 = order("ORD-2402", "青禾日化", line="B线", die_no="D-2402",
                   colors="专蓝+黑", target_l=250, target_w=180, target_h=120)
    r2 = revision(o2, "V1")
    p2 = services.register_production(
        conn, order_id=o2, revision_id=r2, paper_batch="P-1001-B",
        die_no_actual=d2["die_no"], colors_actual=d2["colors"],
        box_type_actual=d2["box_type"], line_actual=d2["line"], registered_by=wang)
    submit(p2, 1, six(p2, 1), measured=(253.8, 180, 120),
           slot="B-01-1")  # 长 +3.8 超差，待质检员看到阻断

    # ---- 订单3：刚到产线，待登记（现场操作入口）----
    o3, d3 = order("ORD-2403", "山海茶业", line="C线", die_no="D-2403",
                   colors="单色黑", target_l=120, target_w=80, target_h=60)
    revision(o3, "V1")

    conn.commit()
    conn.close()

    # 给模拟扫码枪预置一条可读结果（订单2复测/订单3采集时用）
    scanner.load_readable("BC-ORD-2403")


if __name__ == "__main__":
    seed()
    print("种子数据已写入 data/")
