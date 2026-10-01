"""种子数据：正常放行、错版（旧结论失效/旧留样只读）、停机返工复测、
五项阻断各一例。幂等：按固定订单号判断，已存在则跳过。
"""
from __future__ import annotations

import sqlite3

from . import config, storage
from .db import connect, init_db
from .scanner import MockBarcodeScanner
from .services import CaptureInput, Service

USERS = [
    ("op1", "张操作", "operator"),
    ("op2", "李操作", "operator"),
    ("qc1", "王检", "qc"),
    ("qc2", "陈检", "qc"),
    ("admin", "车间主管", "admin"),
]


def _six_photos(order_code: str, rev: str, seq: int, dims: str,
                damaged: bool = False, skip: set[str] | None = None) -> dict[str, bytes]:
    skip = skip or set()
    return {
        f: storage.face_photo_svg(config.FACE_LABELS[f], order_code, rev, seq, dims, damaged)
        for f in config.FACES if f not in skip
    }


def ensure_users(conn: sqlite3.Connection) -> None:
    """任何启动方式都保证存在可切换的操作员/质检员账号。"""
    init_db(conn)
    for login, name, role in USERS:
        conn.execute(
            "INSERT OR IGNORE INTO users(login,name,role) VALUES(?,?,?)",
            (login, name, role))


def seed(conn: sqlite3.Connection, verbose: bool = True) -> None:
    init_db(conn)
    svc = Service(conn)

    def log(msg: str) -> None:
        if verbose:
            print("  " + msg)

    ensure_users(conn)
    u = {r["login"]: r["id"] for r in svc._fetch("SELECT * FROM users")}

    existed = svc._fetch("SELECT code FROM orders")
    if existed:
        if verbose:
            print("[seed] 数据库已有订单，跳过播种（如需重建：python3 run.py --fresh）")
        return

    # ---- 样例 1：正常投产 → 首箱合格 → 放行 → 留样入柜 --------------------
    print("[seed] 样例1 正常放行 PO-2401")
    o1 = svc.create_order("PO-2401", "晨光食品", "燕麦礼盒", "天地盖", "1号线",
                          "DM-301-A", "PAP-1008", ["黑", "专色金"], (300, 200, 150))
    order = svc.get_order(o1)
    r1 = svc.register_revision(o1, "A", "DM-301-A", "PAP-1008", ["黑", "专色金"],
                               storage.artwork_svg("PO-2401", "A", "燕麦礼盒", ["黑", "专色金"]))
    fa1 = svc.submit_first_article(r1, CaptureInput(
        operator_id=u["op1"], measured={"l": 300.0, "w": 200.5, "h": 149.5},
        barcode_ok=True, barcode_payload="PO-2401-BOX-100001",
        die_scanned="DM-301-A", sample_intact=True,
        photos=_six_photos("PO-2401", "A", 1, "300×200.5×149.5mm"),
        scanner=MockBarcodeScanner("readable")))
    svc.decide(fa1, u["qc1"], "release", note="首箱合格，同意放行")
    svc.retain_sample(fa1, "A-01")
    log(f"首箱 #{fa1} 王检放行，留样柜位 A-01")

    # ---- 样例 2：错版 —— A 版已放行留样，客户改版 B 后旧结论失效 ----------
    print("[seed] 样例2 错版失效 PO-2402（A→B，旧留样封档只读）")
    o2 = svc.create_order("PO-2402", "蓝海日化", "洗护套装箱", "标准对口箱", "2号线",
                          "DM-220-B", "PAP-1015", ["蓝", "黑"], (400, 280, 220))
    r2a = svc.register_revision(o2, "A", "DM-220-A", "PAP-1015", ["蓝", "黑"],
                                storage.artwork_svg("PO-2402", "A", "洗护套装箱", ["蓝", "黑"]))
    fa2a = svc.submit_first_article(r2a, CaptureInput(
        operator_id=u["op2"], measured={"l": 400.0, "w": 279.0, "h": 220.0},
        barcode_ok=True, barcode_payload="PO-2402-BOX-200001",
        die_scanned="DM-220-A", sample_intact=True,
        photos=_six_photos("PO-2402", "A", 1, "400×279×220mm")))
    svc.decide(fa2a, u["qc2"], "release", note="A 版首箱合格")
    svc.retain_sample(fa2a, "A-02")
    # 客户稿件改版：登记 B 版 → A 版结论失效、留样封档只读。
    r2b = svc.register_revision(o2, "B", "DM-220-B", "PAP-1015", ["蓝", "黑"],
                                storage.artwork_svg("PO-2402", "B", "洗护套装箱(新文案)",
                                                    ["蓝", "黑"]))
    log(f"A 版首箱 #{fa2a} 结论已失效、留样 A-02 已封档；B 版 #{r2b} 等待重做首箱")
    # B 版首箱已采集、等待质检员裁决（可在页面切换王检/陈检体验放行）。
    fa2b = svc.submit_first_article(r2b, CaptureInput(
        operator_id=u["op2"], measured={"l": 400.5, "w": 280.0, "h": 219.0},
        barcode_ok=True, barcode_payload="PO-2402-BOX-200077",
        die_scanned="DM-220-B", sample_intact=True,
        photos=_six_photos("PO-2402", "B", 1, "400.5×280×219mm")))
    log(f"B 版首箱 #{fa2b} 已提交，待质检员裁决（操作员李操作不能自批）")

    # ---- 样例 3：停机返工 → 复测合格 --------------------------------------
    print("[seed] 样例3 停机返工复测 PO-2403（长度超差 → 返工 → 复测放行）")
    o3 = svc.create_order("PO-2403", "山野茶业", "毛尖瓦楞箱", "插底自锁盒", "1号线",
                          "DM-118-A", "PAP-1020", ["黑"], (250, 180, 120))
    r3 = svc.register_revision(o3, "A", "DM-118-A", "PAP-1020", ["黑"],
                               storage.artwork_svg("PO-2403", "A", "毛尖瓦楞箱", ["黑"]))
    fa3a = svc.submit_first_article(r3, CaptureInput(
        operator_id=u["op1"], measured={"l": 256.0, "w": 180.0, "h": 120.0},  # 长 +6 超差
        barcode_ok=True, barcode_payload="PO-2403-BOX-300001",
        die_scanned="DM-118-A", sample_intact=True,
        photos=_six_photos("PO-2403", "A", 1, "256×180×120mm")))
    svc.decide(fa3a, u["qc1"], "rework", reason="长度方向实测 256 mm，超差 +6 mm，调机返工")
    # 返工复测轮：合格。
    fa3b = svc.rework_retest(fa3a, CaptureInput(
        operator_id=u["op1"], measured={"l": 250.5, "w": 180.0, "h": 119.5},
        barcode_ok=True, barcode_payload="PO-2403-BOX-300002",
        die_scanned="DM-118-A", sample_intact=True,
        photos=_six_photos("PO-2403", "A", 2, "250.5×180×119.5mm")))
    svc.decide(fa3b, u["qc2"], "release", note="复测合格，陈检放行")
    svc.retain_sample(fa3b, "A-04")
    log(f"首轮 #{fa3a} 判返工，复测 #{fa3b} 陈检放行，留样 A-04")

    # ---- 样例 4：多重阻断（条码不可读 + 旧刀模 + 缺顶面 + 样箱破损） -------
    print("[seed] 样例4 阻断合集 PO-2404（条码/刀模/缺面/破损同时出现）")
    o4 = svc.create_order("PO-2404", "恒泰五金", "配件周转箱", "全开模切盒", "3号线",
                          "DM-410-A", "PAP-1022", ["黑", "红"], (350, 250, 180))
    r4 = svc.register_revision(o4, "A", "DM-410-A", "PAP-1022", ["黑", "红"],
                               storage.artwork_svg("PO-2404", "A", "配件周转箱", ["黑", "红"]))
    fa4 = svc.submit_first_article(r4, CaptureInput(
        operator_id=u["op2"], measured={"l": 351.0, "w": None, "h": 181.0},  # 宽未测
        barcode_ok=False, barcode_payload="",
        die_scanned="DM-409-OLD", sample_intact=False,                       # 旧刀模+破损
        photos=_six_photos("PO-2404", "A", 1, "351×未测×181mm",
                           damaged=True, skip={"top"})))                     # 缺顶面
    log(f"首箱 #{fa4} 被阻断：条码不可读/宽未测/旧刀模/缺顶面/样箱破损，待判停机返工")

    if verbose:
        print("\n[seed] 演练入口：")
        print("  看板              http://%s:%d/" % (config.HOST, config.PORT))
        print("  错版样例          /orders/%d  （A 版首箱 #%d 已失效，留样 A-02 只读）"
              % (o2, fa2a))
        print("  B 版待裁决首箱    /fa/%d" % fa2b)
        print("  返工复测样例      /fa/%d  （前序 #%d 停机）" % (fa3b, fa3a))
        print("  五项阻断样例      /fa/%d" % fa4)
        print("  留样柜            /samples")
