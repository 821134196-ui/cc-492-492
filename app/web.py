"""基于标准库 http.server 的 Web 层与路由。

零第三方依赖；会话用最简单的 uid Cookie（演练用途）。
"""
from __future__ import annotations

import http.cookies
import io
import re
import sqlite3
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import config, storage, views
from .db import Hub, init_db
from .errors import DomainError
from .scanner import MockBarcodeScanner
from .services import CaptureInput, Service

COLORS_PALETTE = ["黑", "蓝", "红", "黄", "金", "专色绿"]


def parse_multipart(body: bytes, boundary: bytes) -> dict[str, tuple[str, bytes]]:
    """极简 multipart/form-data 解析：{字段名: (文件名或'', 字节)}。"""
    out: dict[str, tuple[str, bytes]] = {}
    delim = b"--" + boundary
    for part in body.split(delim):
        part = part.strip(b"\r\n")
        if not part or part == b"--":
            continue
        head, _, content = part.partition(b"\r\n\r\n")
        if not _:
            continue
        name_m = re.search(rb'name="([^"]+)"', head)
        if not name_m:
            continue
        name = name_m.group(1).decode()
        fn_m = re.search(rb'filename="([^"]*)"', head)
        filename = fn_m.group(1).decode() if fn_m else ""
        out[name] = (filename, content.rstrip(b"\r\n"))
    return out


class App(BaseHTTPRequestHandler):
    svc: Service  # 由 make_server 注入（类属性）

    server_version = "FCA/1.0"

    # ---- 基础工具 ----

    def _uid(self) -> int | None:
        raw = self.headers.get("Cookie")
        if not raw:
            return None
        ck = http.cookies.SimpleCookie(raw)
        if "uid" in ck:
            try:
                return int(ck["uid"].value)
            except ValueError:
                return None
        return None

    def _read_form(self) -> dict[str, str]:
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        ctype = self.headers.get("Content-Type", "")
        if ctype.startswith("multipart/form-data"):
            boundary = ctype.split("boundary=", 1)[1].encode()
            parsed = parse_multipart(body, boundary)
            self._files = {k: v for k, v in parsed.items() if v[0]}
            self._multi = {k: v[1].decode("utf-8", "replace") for k, v in parsed.items()}
            return {k: v[1].decode("utf-8", "replace") for k, v in parsed.items()}
        multi = urllib.parse.parse_qs(body.decode("utf-8", "replace"))
        self._multi = {k: ",".join(vals) for k, vals in multi.items()}
        return {k: v[0] for k, v in multi.items()}

    def _render(self, html_text: str, status: int = 200) -> None:
        data = html_text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _redirect(self, path: str, flash: str = "", ok: bool = False) -> None:
        if flash:
            sep = "&" if "?" in path else "?"
            path += sep + "flash=" + urllib.parse.quote(("__ok__" if ok else "") + flash)
        self.send_response(303)
        self.send_header("Location", path)
        self.end_headers()

    def _flash(self, qs: str) -> str:
        return urllib.parse.parse_qs(qs).get("flash", [""])[0]

    def _users(self):
        return self.svc._fetch("SELECT * FROM users ORDER BY role,id")

    def _page(self, title: str, body: str, flash: str = "") -> str:
        return views.layout(title, body, self.svc, self._uid(), flash)

    def log_message(self, fmt, *args):  # 简洁日志
        print(f"[web] {self.address_string()} {fmt % args}")

    # ---- 路由 ----

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path, qs = parsed.path, parsed.query
        flash = self._flash(qs)
        s = self.svc
        try:
            if path == "/":
                self._render(self._page("看板", views.dashboard(s.dashboard()), flash))
            elif path == "/orders/new":
                self._render(self._page("投产登记", views.new_order_form(COLORS_PALETTE), flash))
            elif m := re.fullmatch(r"/orders/(\d+)", path):
                o = s.get_order(int(m.group(1)))
                self._render(self._page(o["code"], views.order_detail(s, o, self._users()), flash))
            elif m := re.fullmatch(r"/revisions/(\d+)", path):
                rev = s.get_revision(int(m.group(1)))
                self._render(self._page("首箱采集",
                                        views.capture_form(s, rev, s.get_order(rev["order_id"]),
                                                           self._users()), flash))
            elif m := re.fullmatch(r"/revisions/(\d+)/capture", path):
                rev = s.get_revision(int(m.group(1)))
                prev = urllib.parse.parse_qs(qs).get("prev", [""])[0]
                self._render(self._page(
                    "返工复测", views.capture_form(
                        s, rev, s.get_order(rev["order_id"]), self._users(),
                        int(prev) if prev.isdigit() else None), flash))
            elif m := re.fullmatch(r"/fa/(\d+)", path):
                fa = s.get_fa(int(m.group(1)))
                rev = s.get_revision(fa["revision_id"])
                self._render(self._page(
                    "首箱核对", views.fa_detail(s, fa, rev, s.get_order(rev["order_id"]),
                                                self._uid()), flash))
            elif path == "/samples":
                self._render(self._page("留样柜", views.samples_board(s), flash))
            elif path == "/file":
                self._serve_file(urllib.parse.parse_qs(qs).get("path", [""])[0])
            else:
                self._not_found()
        except DomainError as e:
            self._render(self._page("操作被拒绝", f"<h1>操作被拒绝</h1><p>{e}</p>", str(e)), 409)
        except LookupError:
            self._not_found()

    def do_POST(self) -> None:
        path = urllib.parse.urlparse(self.path).path
        form = self._read_form()
        s = self.svc
        files = getattr(self, "_files", {})
        try:
            if path == "/auth":
                self.send_response(303)
                self.send_header("Location", self.headers.get("Referer", "/"))
                self.send_header("Set-Cookie", f"uid={form.get('uid')}; Path=/")
                self.end_headers()

            elif path == "/orders":
                colors = getattr(self, "_multi", {}).get("colors", "黑,蓝")
                color_list = [c for c in colors.split(",") if c]
                oid = s.create_order(
                    form["code"].strip(), form["customer"].strip(), form["product"].strip(),
                    form["box_type"], form["line"], form["die_no"].strip(),
                    form["paper_batch"].strip(),
                    color_list,
                    (float(form["l"]), float(form["w"]), float(form["h"])))
                order = s.get_order(oid)
                svg = storage.artwork_svg(order["code"], "A", order["product"], color_list)
                s.register_revision(oid, "A", order["die_no"], order["paper_batch"],
                                    color_list, svg)
                self._redirect(f"/orders/{oid}", f"订单 {order['code']} 已投产登记，A 版稿件已入库",
                               ok=True)

            elif m := re.fullmatch(r"/orders/(\d+)/revisions", path):
                oid = int(m.group(1))
                order = s.get_order(oid)
                colors = [c.strip() for c in form.get("colors", "").split(",") if c.strip()]
                if "artwork" in files and files["artwork"][1]:
                    data = files["artwork"][1]
                    name = files["artwork"][0]
                else:
                    data = storage.artwork_svg(order["code"], form["rev"], order["product"], colors)
                    name = "artwork.svg"
                s.register_revision(oid, form["rev"].strip(), form["die_no"].strip(),
                                    form["paper_batch"].strip(), colors, data, name)
                self._redirect(f"/orders/{oid}",
                               f"新版次 {form['rev']} 已生效，旧版次首箱结论与留样已失效/封档",
                               ok=True)

            elif m := re.fullmatch(r"/revisions/(\d+)/capture", path):
                rev_id = int(m.group(1))
                rev = s.get_revision(rev_id)
                order = s.get_order(rev["order_id"])

                def num(k: str):
                    v = form.get(k, "").strip()
                    return float(v) if v else None

                photos = {}
                for face in config.FACES:
                    if form.get(f"face_{face}"):
                        dims = f'{form.get("l","?")}×{form.get("w","?")}×{form.get("h","?")}mm'
                        photos[face] = storage.face_photo_svg(
                            config.FACE_LABELS[face], order["code"], rev["rev"],
                            s.next_sequence(rev_id), dims,
                            damaged="sample_intact" not in form)
                data = CaptureInput(
                    operator_id=int(form["operator_id"]),
                    measured={"l": num("l"), "w": num("w"), "h": num("h")},
                    barcode_ok=False,  # 由模拟扫码器现场判定
                    die_scanned=form.get("die_scanned", "").strip(),
                    sample_intact="sample_intact" in form,
                    photos=photos,
                    scanner=MockBarcodeScanner(form.get("barcode_mode", "readable")))
                prev = form.get("prev_fa_id", "").strip()
                if prev.isdigit():
                    fa_id = s.rework_retest(int(prev), data)
                    msg = f"返工复测（第 {s.get_fa(fa_id)['sequence']} 轮）已提交"
                else:
                    fa_id = s.submit_first_article(rev_id, data)
                    msg = "首箱已采集并完成五项自动核对"
                self._redirect(f"/fa/{fa_id}", msg, ok=True)

            elif m := re.fullmatch(r"/fa/(\d+)/decide", path):
                uid = self._uid()
                if uid is None:
                    raise DomainError("请先在右上角选择身份")
                fa_id = int(m.group(1))
                res = s.decide(fa_id, uid, form.get("decision"),
                               reason=form.get("reason", ""),
                               note="车间现场裁决")
                if res["status"] == "approved":
                    self._redirect(f"/fa/{fa_id}",
                                   f"质检员 {res['qc']} 已放行，可继续生产；请登记留样柜位", ok=True)
                else:
                    self._redirect(f"/fa/{fa_id}",
                                   f"质检员 {res['qc']} 判定停机返工：{form.get('reason','')}")

            elif m := re.fullmatch(r"/fa/(\d+)/retain", path):
                fa_id = int(m.group(1))
                s.retain_sample(fa_id, form["cabinet"].strip().upper())
                self._redirect(f"/fa/{fa_id}", f"留样已入柜 {form['cabinet'].strip().upper()}",
                               ok=True)

            elif m := re.fullmatch(r"/samples/(\d+)/move", path):
                sid = int(m.group(1))
                s.assert_sample_writable(sid)
                try:
                    s.conn.execute("UPDATE retained_samples SET cabinet=? WHERE id=?",
                                   (form["cabinet"].strip().upper(), sid))
                except Exception as e:
                    raise DomainError(f"移位失败：{e}")
                self._redirect("/samples", "柜位已更新", ok=True)

            else:
                self._not_found()
        except DomainError as e:
            back = self._post_fallback(path)
            self._redirect(back, f"[{e.code}] {e}")
        except LookupError as e:
            self._redirect(self._post_fallback(path), f"记录不存在：{e}")
        except sqlite3.Error as e:
            self._redirect(self._post_fallback(path), f"数据校验未通过：{e}")
        except (ValueError, KeyError) as e:
            self._redirect(self._post_fallback(path), f"输入有误：{e}")

    @staticmethod
    def _post_fallback(path: str) -> str:
        m = re.fullmatch(r"/fa/(\d+)/(?:decide|retain)", path)
        if m:
            return f"/fa/{m.group(1)}"
        if path.startswith("/samples"):
            return "/samples"
        return "/"

    # ---- 文件服务（稿件与照片，限定在 DATA_DIR 内） ----

    def _serve_file(self, rel: str) -> None:
        if not rel:
            self._not_found()
            return
        target = (config.DATA_DIR / rel).resolve()
        base = config.DATA_DIR.resolve()
        if base not in target.parents and target != base:
            self.send_error(403)
            return
        if not target.is_file():
            self._not_found()
            return
        ctype = "image/svg+xml" if target.suffix == ".svg" else "application/octet-stream"
        data = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _not_found(self) -> None:
        self._render(self._page("404", "<h1>页面不存在</h1><p><a href='/'>返回看板</a></p>"), 404)


def make_server(host: str | None = None, port: int | None = None,
                db_path: Path | None = None) -> ThreadingHTTPServer:
    config.ensure_dirs()
    hub = Hub(db_path)
    init_db(hub.conn)
    App.svc = Service(hub)
    httpd = ThreadingHTTPServer((host or config.HOST, port or config.PORT), App)
    return httpd
