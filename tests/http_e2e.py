"""端到端 HTTP 冒烟脚本：投产→采集阻断→自批拒绝→停机→复测→放行→留样→改版封档。

用法：FCA_DATA_DIR=... python3 tests/http_e2e.py http://127.0.0.1:8022
"""
from __future__ import annotations

import os
import sqlite3
import sys
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar

from app import config
from app.db import connect

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8022"
PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"  {'✓' if cond else '✗'} {name}" + (f"  ({detail})" if detail else ""))


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None  # 保留 303，由测试读取 Location


class Client:
    def __init__(self):
        self.opener = urllib.request.build_opener(
            _NoRedirect,
            urllib.request.HTTPCookieProcessor(CookieJar()))

    def get(self, path: str) -> str:
        with self.opener.open(BASE + path) as r:
            return r.read().decode()

    def post(self, path: str, data: dict | str, follow=False) -> tuple[int, str]:
        body = data if isinstance(data, str) else urllib.parse.urlencode(data, doseq=True)
        req = urllib.request.Request(BASE + path, data=body.encode(), method="POST")
        try:
            r = self.opener.open(req)
            return r.status, r.headers.get("Location", r.url)
        except urllib.error.HTTPError as e:
            return e.code, e.headers.get("Location", e.url)


def flash(url: str) -> str:
    q = urllib.parse.urlparse(url).query
    return urllib.parse.unquote(urllib.parse.parse_qs(q).get("flash", [""])[0])


def db_one(sql: str):
    c = connect(config.DB_PATH)
    row = c.execute(sql).fetchone()
    return row[0] if row else None


def main() -> int:
    c = Client()
    # 用角色固定的身份：张操作=op1、王检=qc1、陈检=qc2（种子用户顺序固定）。
    conn = connect(config.DB_PATH)
    uid = {r["login"]: r["id"] for r in conn.execute("SELECT * FROM users")}

    print("1) 投产登记")
    st, url = c.post("/orders", {
        "code": "PO-E2E", "customer": "测试客户", "product": "闭环箱",
        "box_type": "天地盖", "line": "2号线", "die_no": "DM-E-1",
        "paper_batch": "PAP-E", "l": 300, "w": 200, "h": 150,
        "colors": ["黑", "蓝"]})
    check("登记 303", st == 303, flash(url))
    rid = db_one("SELECT id FROM revisions WHERE order_id="
                 "(SELECT id FROM orders WHERE code='PO-E2E')")
    oid = db_one("SELECT id FROM orders WHERE code='PO-E2E'")

    print("2) 首箱：高 +5 超差 + 缺底面照片")
    faces = {f"face_{f}": "on" for f in ("front", "back", "left", "right", "top")}
    st, url = c.post(f"/revisions/{rid}/capture", {
        "operator_id": uid["op1"], "l": 300, "w": 200, "h": 155,
        "barcode_mode": "readable", "die_scanned": "DM-E-1",
        "sample_intact": "on", **faces})
    check("采集 303", st == 303)
    fa1 = db_one(f"SELECT id FROM first_articles WHERE revision_id={rid} AND sequence=1")
    page = c.get(f"/fa/{fa1}")
    check("高超差显示具体数值", "155" in page and "+5" in page and "目标 150" in page)
    check("缺底面红色占位", "缺少底面照片" in page and page.count("face missing") == 1)
    check("放行按钮被禁用", "disabled" in page)

    print("3) 操作员不能批准自己的首箱")
    c.post("/auth", {"uid": uid["op1"]})
    st, url = c.post(f"/fa/{fa1}/decide", {"decision": "release"})
    check("自批被拒", "self_approval" in flash(url), flash(url))

    print("4) 质检员强放被阻断，只能判返工")
    c.post("/auth", {"uid": uid["qc1"]})
    st, url = c.post(f"/fa/{fa1}/decide", {"decision": "release"})
    check("阻断强放", "blocking_finding" in flash(url), flash(url))
    st, url = c.post(f"/fa/{fa1}/decide",
                     {"decision": "rework", "reason": "高超差5mm且缺底面"})
    check("判返工", "停机返工" in flash(url), flash(url))
    check("状态 rejected", db_one(f"SELECT status FROM first_articles WHERE id={fa1}")
          == "rejected")
    check("停机原因入库", db_one(f"SELECT reason FROM stoppages WHERE fa_id={fa1}")
          == "高超差5mm且缺底面")

    print("5) 返工复测合格（六面齐全）")
    faces6 = {f"face_{f}": "on" for f in
              ("front", "back", "left", "right", "top", "bottom")}
    st, url = c.post(f"/revisions/{rid}/capture", {
        "operator_id": uid["op1"], "prev_fa_id": fa1,
        "l": 300.5, "w": 200, "h": 150, "barcode_mode": "readable",
        "die_scanned": "DM-E-1", "sample_intact": "on", **faces6})
    check("复测采集 303", st == 303)
    fa2 = db_one(f"SELECT id FROM first_articles WHERE revision_id={rid} AND sequence=2")
    check("复测链 prev_fa_id", db_one(f"SELECT prev_fa_id FROM first_articles WHERE id={fa2}")
          == fa1)

    print("6) 复测自批拒绝，另一名质检员放行")
    c.post("/auth", {"uid": uid["op1"]})
    st, url = c.post(f"/fa/{fa2}/decide", {"decision": "release"})
    check("复测自批被拒", "self_approval" in flash(url), flash(url))
    c.post("/auth", {"uid": uid["qc2"]})
    st, url = c.post(f"/fa/{fa2}/decide", {"decision": "release"})
    check("陈检放行", "已放行" in flash(url), flash(url))

    print("7) 留样入柜与唯一柜位")
    st, url = c.post(f"/fa/{fa2}/retain", {"cabinet": "B-07"})
    check("留样 B-07", "B-07" in flash(url), flash(url))
    st2, url2 = c.post(f"/fa/{fa2}/retain", {"cabinet": "B-08"})
    check("同版次重复留样拒绝", "duplicate_active_sample" in flash(url2), flash(url2))

    print("8) 登记 B 版：旧结论失效、旧留样封档只读")
    import io, uuid
    boundary = "----b" + uuid.uuid4().hex
    body = b"".join(
        (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n"
         f"{v}\r\n").encode() for k, v in {
            "rev": "B", "die_no": "DM-E-2", "paper_batch": "PAP-E",
            "colors": "黑,蓝", "operator_id": uid["op1"]}.items()) + \
        f"--{boundary}--\r\n".encode()
    req = urllib.request.Request(
        BASE + f"/orders/{oid}/revisions", data=body, method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    try:
        c.opener.open(req)
    except urllib.error.HTTPError:
        pass  # 303 See Other
    check("A 版 superseded", db_one(
        f"SELECT status FROM revisions WHERE id={rid}") == "superseded")
    check("旧首箱结论失效", db_one(
        f"SELECT conclusion_valid FROM first_articles WHERE id={fa2}") == 0)
    check("旧留样 archived", db_one(
        f"SELECT state FROM retained_samples WHERE fa_id={fa2}") == "archived")
    sid = db_one(f"SELECT id FROM retained_samples WHERE fa_id={fa2}")

    print("9) 封档只读：移位、补批均被拒")
    st, url = c.post(f"/samples/{sid}/move", {"cabinet": "Z-99"})
    check("封档留样移位拒绝", "sample_locked" in flash(url), flash(url))
    st, url = c.post(f"/fa/{fa2}/decide", {"decision": "rework"})
    check("旧首箱补批拒绝", "revision_stale" in flash(url), flash(url))
    page = c.get(f"/fa/{fa2}")
    check("页面只读提示", "不能放行、返工登记或留样" in page)
    check("柜位仍为 B-07", db_one(
        f"SELECT cabinet FROM retained_samples WHERE id={sid}") == "B-07")

    print(f"\n结果：{len(PASS)} 通过，{len(FAIL)} 失败")
    if FAIL:
        print("失败项：", FAIL)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
