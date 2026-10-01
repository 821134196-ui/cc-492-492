"""Web 看板与首箱核对页（仅依赖标准库）。

配色：纸白 #fbfaf6、墨黑 #1a1a1a、印刷蓝 #1f4e9c；缺陷用克制的朱红标注具体数值。
"""
from __future__ import annotations

import contextlib
import html
import json
import mimetypes
import re
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

from . import services
from .db import FACE_CN, FACES, connect, init_db
from .devices import ScannerError, scanner
from .media import abspath, save_artwork, save_six_photos

INK = "#1a1a1a"
BLUE = "#1f4e9c"
PAPER = "#fbfaf6"
RED = "#a51f1f"

CSS = """
:root{--ink:%(ink)s;--blue:%(blue)s;--paper:%(paper)s;--red:%(red)s;}
*{box-sizing:border-box}
body{margin:0;background:var(--paper);color:var(--ink);
 font-family:"PingFang SC","Microsoft YaHei",sans-serif;font-size:15px;line-height:1.55}
header{border-bottom:3px solid var(--ink);padding:14px 24px;
 display:flex;align-items:baseline;gap:18px;flex-wrap:wrap}
header h1{font-size:20px;margin:0;letter-spacing:2px}
header .sub{color:var(--blue);font-size:13px}
nav a{color:var(--blue);text-decoration:none;margin-right:14px;font-size:14px}
main{padding:22px 24px;max-width:1180px;margin:0 auto}
h2{font-size:17px;border-left:5px solid var(--blue);padding-left:9px;margin:26px 0 12px}
table{border-collapse:collapse;width:100%%;background:#fff;
 border:1px solid var(--ink);margin:8px 0 18px}
th,td{border:1px solid #cfc9bd;padding:7px 10px;text-align:left;vertical-align:top}
th{background:#efece3;font-weight:700}
tr.bad td{background:#fbeeee}
tr.done td{background:#eef3fb}
.badge{display:inline-block;padding:1px 9px;border:1.5px solid var(--ink);
 font-size:12.5px;white-space:nowrap}
.badge.hold{border-color:var(--red);color:var(--red)}
.badge.ok{border-color:var(--blue);color:var(--blue)}
.badge.dead{border-color:#777;color:#777;text-decoration:line-through}
a.btn,button{background:var(--ink);color:#fff;border:none;padding:6px 14px;
 font-size:14px;cursor:pointer;text-decoration:none;display:inline-block}
a.btn.blue,button.blue{background:var(--blue)}
button.ghost{background:#fff;color:var(--ink);border:1.5px solid var(--ink)}
input,select,textarea{border:1.5px solid var(--ink);padding:5px 8px;
 font:inherit;background:#fff}
label{display:block;margin:7px 0;font-size:14px}
.flash{border-left:6px solid var(--red);background:#fbeeee;padding:9px 14px;
 margin:10px 0;font-weight:600}
.note{border-left:6px solid var(--blue);background:#eef3fb;padding:9px 14px;margin:10px 0}
.readonly{background:#f2efe7;border:2px dashed #8a8475;padding:10px 16px;margin:12px 0;
 font-weight:700;color:#5d5748}
/* 六面展开图 net */
.net{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin:14px 0}
.net .cell{border:2px solid var(--ink);background:#fff;min-height:150px;
 display:flex;flex-direction:column}
.net .cell.miss{border-style:dashed;border-color:var(--red);background:#fbeeee;
 align-items:center;justify-content:center;color:var(--red);font-weight:700}
.net .cell img{width:100%%;height:118px;object-fit:cover;border-bottom:1.5px solid var(--ink)}
.net .cap{padding:5px 8px;font-size:13px;display:flex;justify-content:space-between}
.net .t{grid-column:2;grid-row:1}.net .l{grid-column:1;grid-row:2}
.net .f{grid-column:2;grid-row:2;border-width:3px}
.net .r{grid-column:3;grid-row:2}.net .b{grid-column:4;grid-row:2}
.net .bo{grid-column:2;grid-row:3}
.meas{width:100%%}
.meas .over{color:var(--red);font-weight:700}
.meas td.num{text-align:right;font-variant-numeric:tabular-nums}
.small{font-size:12.5px;color:#5d5748}
.formgrid{display:grid;grid-template-columns:1fr 1fr;gap:0 26px;max-width:860px}
""" % {"ink": INK, "blue": BLUE, "paper": PAPER, "red": RED}


def h(s) -> str:
    return html.escape("" if s is None else str(s))


class App:
    def __init__(self, db_path: str | Path, media_root: str | Path):
        self.db_path = str(db_path)
        self.media_root = str(media_root)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        conn = connect(self.db_path)
        init_db(conn)
        conn.close()
        self.lock = threading.Lock()

    def conn(self):
        return connect(self.db_path)

    @contextlib.contextmanager
    def session(self):
        conn = connect(self.db_path)
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


def _dim_row(axis: str, measured, target, tol):
    if measured is None:
        return (f"<tr><td>{axis}</td><td class='num'>—</td>"
                f"<td class='num'>{target:g}</td><td class='num'>—</td>"
                f"<td class='num'>±{tol:g}</td><td>缺项</td></tr>")
    diff = round(measured - target, 2)
    over = abs(diff) > tol
    cls = "over" if over else ""
    row_cls = " class='bad'" if over else ""
    verdict = f"超差 {diff:+g}mm" if over else f"合格 {diff:+g}mm"
    return (f"<tr{row_cls}><td>{axis}</td>"
            f"<td class='num {cls}'>{measured:g}</td>"
            f"<td class='num'>{target:g}</td>"
            f"<td class='num {cls}'>{diff:+g}</td>"
            f"<td class='num'>±{tol:g}</td>"
            f"<td class='{cls}'>{verdict}</td></tr>")


def _status_for_order(conn, o):
    """聚合订单当前状态。"""
    rev = conn.execute(
        "SELECT * FROM revisions WHERE order_id=? AND active=1", (o["id"],)
    ).fetchone()
    if rev is None:
        return "无生效版次", "dead", None, None
    prod = conn.execute(
        "SELECT * FROM productions WHERE revision_id=? ORDER BY id DESC LIMIT 1",
        (rev["id"],),
    ).fetchone()
    if prod is None:
        return "待投产登记", "", rev, None
    fa = conn.execute(
        "SELECT * FROM first_articles WHERE production_id=? ORDER BY id DESC LIMIT 1",
        (prod["id"],),
    ).fetchone()
    if fa is None:
        return "待首箱采集", "", rev, prod
    s = conn.execute(
        "SELECT decision FROM approvals WHERE first_article_id=? ORDER BY id DESC LIMIT 1",
        (fa["id"],),
    ).fetchone()
    sample = conn.execute(
        "SELECT cabinet_slot FROM samples WHERE revision_id=?", (rev["id"],)
    ).fetchone()
    if sample:
        return f"已放行 · 留样柜 {sample['cabinet_slot']}", "ok", rev, prod
    if s and s["decision"] == "held":
        return "停机返工 · 待复测", "hold", rev, prod
    return "首箱待质检", "", rev, prod


def page(title, body, nav_extra=""):
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{h(title)}</title><style>{CSS}</style></head><body>
<header><h1>首箱留样看板</h1><span class="sub">包装订单投产质量闸口</span>
<nav><a href="/">看板</a><a href="/scanner">扫码枪（模拟）</a>{nav_extra}</nav></header>
<main>{body}</main></body></html>"""


def make_handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def _send(self, code: int, body: bytes, ctype="text/html; charset=utf-8"):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _html(self, content, code=200):
            self._send(code, content.encode("utf-8"))

        def _redirect(self, path, flash=None):
            loc = path + ("?flash=" + quote(flash) if flash else "")
            self.send_response(303)
            self.send_header("Location", loc)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _form(self):
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length).decode("utf-8") if length else ""
            return {k: v[0] for k, v in parse_qs(raw, keep_blank_values=True).items()}

        # ---------- GET ----------
        def do_GET(self):
            u = urlparse(self.path)
            p, q = u.path, parse_qs(u.query)
            try:
                if p == "/":
                    return self.board(q.get("flash", [""])[0])
                if p == "/scanner":
                    return self.scanner_page(q.get("flash", [""])[0])
                if p == "/api/scan":
                    return self.api_scan()
                m = re.fullmatch(r"/orders/(\d+)", p)
                if m:
                    return self.order_page(int(m.group(1)), q.get("flash", [""])[0])
                m = re.fullmatch(r"/productions/(\d+)", p)
                if m:
                    return self.production_page(int(m.group(1)), q.get("flash", [""])[0])
                m = re.fullmatch(r"/articles/(\d+)", p)
                if m:
                    return self.article_page(int(m.group(1)), q.get("flash", [""])[0])
                if p.startswith("/media/"):
                    return self.serve_media(p[len("/media/"):])
                self._html(page("未找到", "<h2>404</h2>"), 404)
            except services.DomainError as e:
                self._html(page("业务拒绝",
                    f"<div class='flash'>{h(e.code)}：{h(e.message)}</div>"
                    "<p><a class='btn' href='/'>返回看板</a></p>"), 409)

        # ---------- POST ----------
        def do_POST(self):
            p = urlparse(self.path).path
            f = self._form()
            try:
                if p.startswith("/orders/"):
                    oid = int(p.split("/")[2])
                    if p.endswith("/register"):
                        return self.register_production(oid, f)
                    if p.endswith("/new-revision"):
                        return self.new_revision(oid, f)
                m = re.fullmatch(r"/productions/(\d+)/articles", p)
                if m:
                    return self.submit_article(int(m.group(1)), f)
                m = re.fullmatch(r"/articles/(\d+)/decision", p)
                if m:
                    return self.decide(int(m.group(1)), f)
                if p == "/scanner/enqueue":
                    kind = f.get("kind", "readable")
                    with app.lock:
                        if kind == "unreadable":
                            scanner.load_unreadable()
                        elif kind == "offline":
                            scanner.load_offline()
                        else:
                            scanner.load_readable(f.get("content", ""))
                    return self._redirect("/scanner", f"已排程一次扫描（{kind}）")
                self._html(page("未找到", "<h2>404</h2>"), 404)
            except services.DomainError as e:
                back = self.headers.get("Referer", "/")
                self._html(page("操作被拒绝",
                    f"<div class='flash'>{h(e.code)}：{h(e.message)}</div>"
                    f"<p><a class='btn' href='{h(back)}'>返回上一页</a></p>"), 409)
            except ScannerError as e:
                back = self.headers.get("Referer", "/")
                self._html(page("设备错误",
                    f"<div class='flash'>{h(e.code)}：{h(e.message)}</div>"
                    f"<p><a class='btn' href='{h(back)}'>返回</a></p>"), 503)

        # ---------- 媒体文件 ----------
        def serve_media(self, rel: str):
            fp = abspath(app.media_root, rel).resolve()
            root = Path(app.media_root).resolve()
            if root not in fp.parents or not fp.is_file():
                return self._html(page("未找到", "<h2>文件不存在</h2>"), 404)
            ctype = mimetypes.guess_type(str(fp))[0] or "application/octet-stream"
            self._send(200, fp.read_bytes(), ctype)

        # ---------- 看板 ----------
        def board(self, flash):
            with app.session() as conn:
                orders = conn.execute("SELECT * FROM orders ORDER BY id").fetchall()
                rows, cards = [], ""
                for o in orders:
                    label, cls, rev, prod = _status_for_order(conn, o)
                    row_cls = "bad" if cls == "hold" else ("done" if cls == "ok" else "")
                    rows.append(
                        f"<tr class='{row_cls}'><td><a href='/orders/{o['id']}'><b>{h(o['order_no'])}</b></a>"
                        f"<div class='small'>{h(o['customer'])}</div></td>"
                        f"<td>{h(rev['rev_code']) if rev else '—'}</td>"
                        f"<td>{h(o['line'])}<div class='small'>{h(o['box_type'])} · {h(o['colors'])}</div></td>"
                        f"<td>{h(o['die_no'])}</td>"
                        f"<td><span class='badge {cls}'>{h(label)}</span></td></tr>"
                    )
                body = (
                    (f"<div class='note'>{h(flash)}</div>" if flash else "")
                    + "<h2>在制订单</h2><table><thead><tr>"
                    "<th>订单 / 客户</th><th>生效版次</th><th>产线 / 箱型 / 印色</th>"
                    "<th>目标刀模</th><th>状态</th></tr></thead><tbody>"
                    + "".join(rows) + "</tbody></table>"
                    "<p class='small'>闸口规则：条码不可读、尺寸超差、混用旧刀模、照片缺面、"
                    "样箱破损即阻断放行；操作员不能批准自己的首箱；"
                    "换版后旧首箱结论失效；同订单同版次仅一份留样（只读）。</p>"
                )
            self._html(page("首箱留样看板", body))

        # ---------- 订单详情 ----------
        def order_page(self, oid, flash):
            with app.session() as conn:
                o = conn.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
                if not o:
                    raise services.DomainError("NOT_FOUND", "订单不存在")
                revs = conn.execute(
                    "SELECT * FROM revisions WHERE order_id=? ORDER BY id", (oid,)).fetchall()
                users = conn.execute("SELECT * FROM users WHERE role='operator'").fetchall()
                rev_rows, art_links = "", ""
                for r in revs:
                    badge = "<span class='badge ok'>生效中</span>" if r["active"] \
                            else "<span class='badge dead'>已失效</span>"
                    sample = conn.execute(
                        "SELECT * FROM samples WHERE revision_id=?", (r["id"],)).fetchone()
                    lock = (f" · 留样柜 <b>{h(sample['cabinet_slot'])}</b>（只读归档）"
                            if sample else "")
                    row_cls = "done" if r["active"] else ""
                    rev_rows += (f"<tr class='{row_cls}'><td>{h(r['rev_code'])}</td>"
                                 f"<td>{badge}{lock}</td><td>{h(r['created_at'])}</td>"
                                 f"<td><a href='/media/{h(r['art_file'])}'>查看稿件文件</a></td></tr>")
                active = next((r for r in revs if r["active"]), None)
                prods = conn.execute(
                    "SELECT p.*, u.name FROM productions p JOIN users u ON u.id=p.registered_by "
                    "WHERE p.order_id=? ORDER BY p.id", (oid,)).fetchall()
                prod_rows = ""
                for p in prods:
                    fas = conn.execute(
                        "SELECT * FROM first_articles WHERE production_id=? ORDER BY seq",
                        (p["id"],)).fetchall()
                    fa_txt = ""
                    for fa in fas:
                        dec = conn.execute(
                            "SELECT decision,reason FROM approvals WHERE first_article_id=? "
                            "ORDER BY id DESC LIMIT 1", (fa["id"],)).fetchone()
                        dmap = {"released": ("ok", "已放行"), "held": ("hold", "停机返工")}
                        if dec:
                            dcls, dlabel = dmap[dec["decision"]]
                            reason = f"：{h(dec['reason'])}" if dec["reason"] else ""
                            tag = f"<span class='badge {dcls}'>第{fa['seq']}次 {dlabel}{reason}</span> "
                        else:
                            tag = f"<span class='badge'>第{fa['seq']}次 待质检</span> "
                        fa_txt += f"<a href='/articles/{fa['id']}'>{tag}</a>"
                    prod_rows += (
                        f"<tr><td><a href='/productions/{p['id']}'>#{p['id']} 投产登记</a></td>"
                        f"<td>{h(p['paper_batch'])}</td><td>{h(p['die_no_actual'])}</td>"
                        f"<td>{h(p['colors_actual'])}</td><td>{h(p['line_actual'])}</td>"
                        f"<td>{h(p['name'])}</td><td>{fa_txt or '—'}</td></tr>")

                uopts = "".join(f"<option value='{u['id']}'>{h(u['name'])}</option>" for u in users)
                body = f"""
{("<div class='note'>"+h(flash)+"</div>") if flash else ""}
<p><nav><a href="/">← 看板</a></nav></p>
<h2>订单 {h(o['order_no'])} · {h(o['customer'])}</h2>
<table><tr><th>箱型</th><td>{h(o['box_type'])}</td><th>产线</th><td>{h(o['line'])}</td>
<th>印色</th><td>{h(o['colors'])}</td></tr>
<tr><th>目标刀模</th><td>{h(o['die_no'])}</td><th>目标条码</th><td>{h(o['barcode'])}</td>
<th>尺寸公差</th><td>{o['target_l']:g}×{o['target_w']:g}×{o['target_h']:g} mm ±{o['tol_mm']:g}</td></tr></table>

<h2>稿件版次（换版即作废旧首箱结论）</h2>
<table><thead><tr><th>版次</th><th>状态 / 留样</th><th>登记时间</th><th>稿件文件</th></tr></thead>
<tbody>{rev_rows}</tbody></table>
<form method="post" action="/orders/{oid}/new-revision" class='formgrid' style="max-width:520px">
<label>新版次号 <input name="rev_code" required placeholder="如 V3"></label>
<label style="align-self:end"><button class="blue" type="submit">登记换版（旧结论失效）</button></label>
</form>

<h2>投产登记</h2>
<table><thead><tr><th>#</th><th>纸张批次</th><th>上机刀模</th><th>印色</th><th>产线</th>
<th>登记操作员</th><th>首箱记录</th></tr></thead><tbody>{prod_rows or '<tr><td colspan=7>尚无登记</td></tr>'}</tbody></table>
"""
                if active:
                    body += f"""
<form method="post" action="/orders/{oid}/register" class='formgrid'>
<label>操作员
<select name="registered_by">{uopts}</select></label>
<label>纸张批次 <input name="paper_batch" required value="P-20261001-A"></label>
<label>上机刀模编号 <input name="die_no_actual" required value="{h(o['die_no'])}"></label>
<label>实际印色 <input name="colors_actual" required value="{h(o['colors'])}"></label>
<label>箱型 <input name="box_type_actual" required value="{h(o['box_type'])}"></label>
<label>产线 <input name="line_actual" required value="{h(o['line'])}"></label>
<div style="grid-column:1/3"><button type="submit">登记投产（版次 {h(active['rev_code'])}）</button></div>
<p class="small" style="grid-column:1/3">提示：把"上机刀模编号"改成旧编号即可演示<b>混用旧刀模阻断</b>。</p>
</form>"""
                else:
                    body += "<div class='flash'>无生效版次，请先联系制版登记新版稿件。</div>"
            self._html(page(f"订单 {o['order_no']}", body,
                            f"<a href='/orders/{oid}'>订单</a>"))

        # ---------- 投产 / 首箱采集 ----------
        def production_page(self, pid, flash):
            with app.session() as conn:
                p = conn.execute(
                    "SELECT p.*, o.order_no, o.customer, o.die_no, o.barcode, o.target_l,"
                    " o.target_w, o.target_h, o.tol_mm, r.rev_code, r.active AS rev_active "
                    "FROM productions p JOIN orders o ON o.id=p.order_id "
                    "JOIN revisions r ON r.id=p.revision_id WHERE p.id=?", (pid,)).fetchone()
                if not p:
                    raise services.DomainError("NOT_FOUND", "投产登记不存在")
                fas = conn.execute(
                    "SELECT * FROM first_articles WHERE production_id=? ORDER BY seq",
                    (pid,)).fetchall()
                users = conn.execute("SELECT * FROM users WHERE role='operator'").fetchall()
                next_seq = len(fas) + 1
                fa_links = "".join(
                    f"<li><a href='/articles/{fa['id']}'>第 {fa['seq']} 次采集</a></li>"
                    for fa in fas)
                face_checks = "".join(
                    f"<label style='display:inline-block;margin-right:14px'>"
                    f"<input type='checkbox' name='face_{f}' checked> {FACE_CN[f]}</label>"
                    for f in FACES)
                uopts = "".join(f"<option value='{u['id']}'>{h(u['name'])}</option>" for u in users)
                rev_note = "" if p["rev_active"] else (
                    "<div class='flash'>稿件版次已变更：本登记下的旧首箱结论均已失效，"
                    "留样柜内旧样箱仅作只读归档。须按新版次重新登记投产。</div>")
                body = f"""
{("<div class='note'>"+h(flash)+"</div>") if flash else ""}
<p><a href="/orders/{p['order_id']}">← 返回订单 {h(p['order_no'])}</a></p>
<h2>投产 #{pid} · 版次 {h(p['rev_code'])}</h2>
{rev_note}
<table><tr><th>纸张批次</th><td>{h(p['paper_batch'])}</td><th>上机刀模</th><td>{h(p['die_no_actual'])}</td>
<th>产线</th><td>{h(p['line_actual'])}</td></tr></table>
<h2>首箱记录</h2><ul>{fa_links or "<li>暂无，首箱完成后在下方采集</li>"}</ul>

<h2>{'返工复测采集' if next_seq>1 else '首箱采集'}（第 {next_seq} 次）</h2>
<div class="note">扫码枪为模拟设备：先到 <a href="/scanner">扫码枪页</a> 排程扫描结果，
再点本页“触发扫码”。当前队列 <b id="q">{scanner.pending()}</b> 条。
<button type="button" class="ghost" onclick="doScan()">触发扫码</button>
<span id="scanout" class="small"></span></div>
<form method="post" action="/productions/{pid}/articles" class='formgrid'>
<label>操作员 <select name="submitted_by">{uopts}</select></label>
<label>留样柜位 <input name="cabinet_slot" required value="A-{pid:02d}-{next_seq}"></label>
<label>实测长 mm <input name="measured_l" type="number" step="0.1" value="{p['target_l']:g}"></label>
<label>实测宽 mm <input name="measured_w" type="number" step="0.1" value="{p['target_w']:g}"></label>
<label>实测高 mm <input name="measured_h" type="number" step="0.1" value="{p['target_h']:g}"></label>
<label>样箱状态
<select name="sample_intact"><option value="1">完好</option><option value="0">破损</option></select></label>
<div style="grid-column:1/3">
<label>六面照片（车间相机；取消勾选模拟“照片缺面”）</label>{face_checks}
<label>模拟破损面 <select name="damaged_face"><option value="">无</option>
{''.join(f"<option value='{f}'>{FACE_CN[f]}</option>" for f in FACES)}</select></label>
</div>
<input type="hidden" name="barcode_scan" id="barcode_scan">
<input type="hidden" name="barcode_ok" id="barcode_ok">
<div style="grid-column:1/3"><button type="submit">提交首箱核对</button>
<span class="small">目标条码：{h(p['barcode'])}</span></div>
</form>
<script>
async function doScan(){{
  const out=document.getElementById('scanout');
  try{{
    const r=await fetch('/api/scan'); const d=await r.json();
    if(!r.ok){{out.textContent='设备错误：'+(d.error||r.status);return;}}
    document.getElementById('barcode_scan').value=d.content||'';
    document.getElementById('barcode_ok').value=d.ok?1:0;
    out.textContent=d.ok?('扫读成功：'+d.content):'条码不可读（NOREAD）';
    document.getElementById('q').textContent=d.pending;
  }}catch(e){{out.textContent='设备无响应';}}
}}
</script>"""
            self._html(page(f"投产 #{pid}", body))

        def submit_article(self, pid, f):
            with app.session() as conn:
                p = conn.execute(
                    "SELECT revision_id FROM productions WHERE id=?", (pid,)).fetchone()
                if not p:
                    raise services.DomainError("NOT_FOUND", "投产登记不存在")

                def num(k):
                    try:
                        return float(f[k]) if f.get(k, "").strip() != "" else None
                    except ValueError:
                        raise services.DomainError("BAD_NUMBER", f"{k} 需为数值")

                # 扫码：优先取页面 JS 已读到的值；否则当场从模拟设备弹一条
                if f.get("barcode_scan") or f.get("barcode_ok"):
                    scan_content = f.get("barcode_scan") or None
                    barcode_ok = int(f.get("barcode_ok") or 0)
                else:
                    res = scanner.scan()
                    scan_content, barcode_ok = res.content, 1 if res.ok else 0

                checked = [face for face in FACES if f.get(f"face_{face}")]
                if len(checked) < 6:
                    missing = [FACE_CN[x] for x in FACES if x not in checked]
                    raise services.DomainError(
                        "INCOMPLETE_COLLECTION",
                        "首箱采集缺项，阻断提交：照片缺面：" + "、".join(missing))

                token = f"p{pid}_{uuid.uuid4().hex[:8]}"
                photos_paths = save_six_photos(
                    app.media_root, token,
                    damaged_face=f.get("damaged_face") or None)
                fa_id = services.submit_first_article(
                    conn, production_id=pid,
                    measured_l=num("measured_l"), measured_w=num("measured_w"),
                    measured_h=num("measured_h"),
                    barcode_scan=scan_content, barcode_ok=barcode_ok,
                    cabinet_slot=f.get("cabinet_slot"),
                    sample_intact=int(f["sample_intact"]) if f.get("sample_intact") in ("0", "1") else None,
                    submitted_by=int(f["submitted_by"]),
                    photos=photos_paths,
                )
            self._redirect(f"/articles/{fa_id}", "首箱已提交，等待质检员判定")

        # ---------- 首箱核对页（六面展开图）----------
        def article_page(self, fa_id, flash):
            with app.session() as conn:
                fa = conn.execute(
                    """SELECT fa.*, o.*, p.id AS pid, p.die_no_actual, p.paper_batch,
                       r.rev_code, r.active AS rev_active, r.art_file,
                       u.name AS submitter
                       FROM first_articles fa
                       JOIN productions p ON p.id=fa.production_id
                       JOIN orders o ON o.id=p.order_id
                       JOIN revisions r ON r.id=p.revision_id
                       JOIN users u ON u.id=fa.submitted_by
                       WHERE fa.id=?""", (fa_id,)).fetchone()
                if not fa:
                    raise services.DomainError("NOT_FOUND", "首箱不存在")
                photos = {r["face"]: r["file_path"] for r in conn.execute(
                    "SELECT face,file_path FROM photos WHERE first_article_id=?", (fa_id,))}
                chk = services.evaluate_article(conn, fa_id)
                decision = services.article_status(conn, fa_id)
                locked = services.is_locked_sample(conn, fa_id)
                approvals = conn.execute(
                    "SELECT a.*, u.name FROM approvals a JOIN users u ON u.id=a.qc_user_id "
                    "WHERE first_article_id=? ORDER BY a.id", (fa_id,)).fetchall()
                qcs = conn.execute("SELECT * FROM users WHERE role='qc'").fetchall()

                # 六面展开图
                pos = {"top": "t", "left": "l", "front": "f",
                       "right": "r", "back": "b", "bottom": "bo"}
                cells = ""
                for face in ["top", "left", "front", "right", "back", "bottom"]:
                    path = photos.get(face)
                    if path:
                        inner = (f"<img src='/media/{h(path)}' alt='{FACE_CN[face]}'>"
                                 f"<div class='cap'><b>{FACE_CN[face]}</b>"
                                 f"<span class='small'>{face}</span></div>")
                    else:
                        inner = f"缺照片：{FACE_CN[face]}"
                    miss_cls = " miss" if not path else ""
                    cells += f"<div class='cell {pos[face]}{miss_cls}'>{inner}</div>"

                meas = "".join(_dim_row(a, m, t, fa["tol_mm"]) for a, m, t in (
                    ("长", fa["measured_l"], fa["target_l"]),
                    ("宽", fa["measured_w"], fa["target_w"]),
                    ("高", fa["measured_h"], fa["target_h"])))
                bc = fa["barcode_scan"]
                if fa["barcode_ok"] is None:
                    bc_html = "<tr class='bad'><td>条码</td><td colspan='3'>缺项：未扫描</td></tr>"
                elif fa["barcode_ok"] == 0:
                    bc_html = "<tr class='bad'><td>条码</td><td colspan='3'>不可读（NOREAD）— 阻断</td></tr>"
                else:
                    ok = bc == fa["barcode"]
                    row_cls = "bad" if not ok else ""
                    over_cls = "over" if not ok else ""
                    verdict = "一致" if ok else "内容不符 — 阻断"
                    bc_html = (f"<tr class='{row_cls}'><td>条码</td>"
                               f"<td>实测 <b>{h(bc)}</b></td><td>目标 {h(fa['barcode'])}</td>"
                               f"<td class='{over_cls}'>{verdict}</td></tr>")
                die_ok = fa["die_no_actual"] == fa["die_no"]
                die_cls = "bad" if not die_ok else ""
                die_over = "over" if not die_ok else ""
                die_verdict = "一致" if die_ok else "混用旧刀模 — 阻断"
                die_html = (f"<tr class='{die_cls}'><td>刀模</td>"
                            f"<td>上机 <b>{h(fa['die_no_actual'])}</b></td>"
                            f"<td>要求 {h(fa['die_no'])}</td>"
                            f"<td class='{die_over}'>{die_verdict}</td></tr>")
                intact = fa["sample_intact"]
                int_cls = "bad" if intact == 0 else ""
                int_state = ("破损 — 阻断" if intact == 0
                             else "完好" if intact == 1 else "未确认")
                int_html = (f"<tr class='{int_cls}'><td>样箱</td>"
                            f"<td colspan='2'>{int_state}</td><td></td></tr>")

                defects = chk.missing + [d.message for d in chk.defects]
                if defects:
                    defect_html = "<div class='flash'><b>核对结论：阻断放行</b><ul>" + \
                        "".join(f"<li>{h(x)}</li>" for x in defects) + "</ul></div>"
                else:
                    defect_html = "<div class='note'><b>核对结论：全部项合格，可由质检员放行。</b></div>"

                banner = ""
                if locked:
                    banner = ("<div class='readonly'>🔒 留样只读：该首箱已放行入柜，"
                              "照片、测量值与审批记录不可修改、不可删除。</div>")
                elif not fa["rev_active"]:
                    banner = ("<div class='flash'>⚠ 版次已失效：稿件已换版，"
                              "本首箱结论自动作废；如已留样则转入只读归档，须按新版次重新走首箱。</div>")

                hist = "".join(
                    f"<li>{h(a['created_at'])} 质检员 <b>{h(a['name'])}</b> "
                    f"{'放行' if a['decision']=='released' else '停机返工'}"
                    f"{('：'+h(a['reason'])) if a['reason'] else ''}</li>"
                    for a in approvals)

                form = ""
                if not locked and fa["rev_active"] and decision != "held":
                    opts = "".join(
                        f"<option value='{q['id']}'{' disabled' if q['id']==fa['submitted_by'] else ''}>"
                        f"{h(q['name'])}{'（提交人，禁止自批）' if q['id']==fa['submitted_by'] else ''}</option>"
                        for q in qcs)
                    form = f"""
<h2>质检判定</h2>
<form method="post" action="/articles/{fa_id}/decision">
<label>质检员 <select name="qc_user_id" required>{opts}</select></label>
<label><input type="radio" name="decision" value="released" {'checked' if not defects else ''}>
继续生产（放行入留样柜）</label>
<label><input type="radio" name="decision" value="held"> 停机返工</label>
<label>停机原因 / 差异标注 <textarea name="reason" rows="2" cols="60"></textarea></label>
<button class="blue" type="submit">提交判定</button>
<p class="small">两名质检员同时放行时，仅一人能成功；
停机返工后回到投产页做“返工复测采集”。</p>
</form>"""
                elif decision == "held" and fa["rev_active"]:
                    form = (f"<div class='flash'>已判定停机返工："
                            f"<a class='btn' href='/productions/{fa['pid']}'>前往返工复测 →</a></div>")

                body = f"""
{("<div class='note'>"+h(flash)+"</div>") if flash else ""}
<p><a href="/productions/{fa['pid']}">← 投产 #{fa['pid']}</a>
<span class='small'>订单 {h(fa['order_no'])} · 版次 {h(fa['rev_code'])} ·
第 {fa['seq']} 次采集 · 操作员 {h(fa['submitter'])} · {h(fa['created_at'])}</span></p>
<h2>首箱核对（六面展开图）</h2>
{banner}
<div class="net">{cells}</div>
<h2>测量值 vs 目标值</h2>
<table class="meas"><thead><tr><th>项</th><th>实测</th><th>目标</th><th>偏差</th>
<th>允许</th><th>判定</th></tr></thead>
<tbody>
{meas}{bc_html}{die_html}{int_html}
</tbody></table>
{defect_html}
<h2>审批记录</h2><ul>{hist or "<li>暂无判定</li>"}</ul>
{form}"""
            self._html(page(f"首箱 #{fa_id} 核对", body))

        def decide(self, fa_id, f):
            with app.session() as conn:
                services.approve(
                    conn, first_article_id=fa_id,
                    qc_user_id=int(f["qc_user_id"]), decision=f["decision"],
                    reason=f.get("reason", ""),
                )
            label = "已放行，继续生产" if f["decision"] == "released" else "已停机返工"
            self._redirect(f"/articles/{fa_id}", label)

        def register_production(self, oid, f):
            with app.session() as conn:
                rev = conn.execute(
                    "SELECT id FROM revisions WHERE order_id=? AND active=1", (oid,)
                ).fetchone()
                if rev is None:
                    raise services.DomainError("NO_ACTIVE_REV", "该订单没有生效版次")
                pid = services.register_production(
                    conn, order_id=oid, revision_id=rev["id"],
                    paper_batch=f.get("paper_batch", ""),
                    die_no_actual=f.get("die_no_actual", ""),
                    colors_actual=f.get("colors_actual", ""),
                    box_type_actual=f.get("box_type_actual", ""),
                    line_actual=f.get("line_actual", ""),
                    registered_by=int(f["registered_by"]),
                )
            self._redirect(f"/productions/{pid}", "投产已登记，请完成首箱后采集")

        def new_revision(self, oid, f):
            with app.session() as conn:
                o = conn.execute("SELECT order_no FROM orders WHERE id=?", (oid,)).fetchone()
                if not o:
                    raise services.DomainError("NOT_FOUND", "订单不存在")
                rev_code = f.get("rev_code", "").strip()
                if not rev_code:
                    raise services.DomainError("MISSING_FIELD", "必须填写新版次号")
                rel = save_artwork(app.media_root, o["order_no"], rev_code)
                services.supersede_revision(
                    conn, order_id=oid, new_rev_code=rev_code, art_file=rel)
            self._redirect(f"/orders/{oid}", f"已登记换版 {rev_code}：旧首箱结论失效")

        # ---------- 模拟扫码枪 ----------
        def scanner_page(self, flash):
            body = f"""
{("<div class='note'>"+h(flash)+"</div>") if flash else ""}
<h2>模拟扫码枪</h2>
<p class="small">真实设备接入串口/USB HID 后替换 app/devices.py 即可；
此处把“下一次扫描结果”排进队列，首箱采集页触发扫码时弹出。</p>
<p>当前待弹队列：<b>{scanner.pending()}</b> 条；累计扫描 {scanner.scan_count} 次。</p>
<form method="post" action="/scanner/enqueue" style="display:inline-block;margin-right:20px">
<label>可读条码 <input name="content" value="BC-ORD-001"></label>
<input type="hidden" name="kind" value="readable">
<button type="submit">排程“可读”</button></form>
<form method="post" action="/scanner/enqueue" style="display:inline-block;margin-right:20px">
<input type="hidden" name="kind" value="unreadable">
<button class="ghost" type="submit">排程“不可读 NOREAD”</button></form>
<form method="post" action="/scanner/enqueue" style="display:inline-block">
<input type="hidden" name="kind" value="offline">
<button class="ghost" type="submit">排程“设备无响应”</button></form>
"""
            self._html(page("模拟扫码枪", body))

        def api_scan(self):
            try:
                res = scanner.scan()
                payload = {"ok": res.ok, "content": res.content,
                           "raw": res.raw, "pending": scanner.pending()}
            except ScannerError as e:
                payload = json.dumps({"ok": False, "error": e.message}).encode()
                self._send(503, payload, "application/json")
                return
            self._send(200, json.dumps(payload).encode(), "application/json")

    return Handler


def run(db_path="data/factory.db", media_root="data/media", host="0.0.0.0", port=8000):
    app = App(db_path, media_root)
    httpd = ThreadingHTTPServer((host, port), make_handler(app))
    print(f"首箱留样看板：http://{host}:{port}  （Ctrl+C 停止）")
    httpd.serve_forever()
