"""HTML 视图：纸白 / 墨黑 / 印刷蓝，面向车间大屏。"""
from __future__ import annotations

import html
from typing import Iterable

from . import config
from .services import BLOCK_CODES, DimCheck, Service

esc = html.escape


def layout(title: str, body: str, svc: Service | None = None, uid: int | None = None,
           flash: str = "") -> str:
    users = svc._fetch("SELECT * FROM users ORDER BY role,id") if svc else []
    opts = "".join(
        f'<option value="{u["id"]}" {"selected" if u["id"]==uid else ""}>'
        f'{esc(u["name"])}（{ {"operator":"操作员","qc":"质检员","admin":"管理员"}[u["role"]] }）'
        f'</option>' for u in users)
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(title)} · 首箱放行</title>
<style>
:root{{--ink:{config.INK};--paper:{config.PAPER};--blue:{config.BLUE};
--blue-soft:{config.BLUE_SOFT};--red:{config.RED};--green:{config.GREEN};--amber:{config.AMBER};}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--paper);color:var(--ink);
font-family:"Noto Sans CJK SC","Microsoft YaHei",sans-serif;font-size:16px;line-height:1.5}}
a{{color:var(--blue);text-decoration:none}}
a:hover{{text-decoration:underline}}
header{{background:var(--ink);color:var(--paper);padding:10px 22px;
display:flex;align-items:center;gap:18px;flex-wrap:wrap}}
header a.brand{{color:var(--paper);font-weight:700;font-size:20px;letter-spacing:1px}}
header nav a{{color:var(--paper);margin-right:14px}}
header form{{margin-left:auto;display:flex;align-items:center;gap:8px}}
header select{{font-size:15px;padding:4px 8px}}
main{{max-width:1240px;margin:0 auto;padding:22px}}
h1{{font-size:26px;margin:0 0 16px}}
h2{{font-size:20px;margin:26px 0 10px;border-left:5px solid var(--blue);padding-left:10px}}
.grid{{display:grid;gap:16px}}
.cards{{grid-template-columns:repeat(auto-fill,minmax(330px,1fr))}}
.card{{background:#fff;border:1px solid #d8d2c2;border-radius:6px;padding:16px}}
.card h3{{margin:0 0 8px;font-size:18px}}
table{{border-collapse:collapse;width:100%;background:#fff}}
th,td{{border:1px solid #cfc8b6;padding:8px 10px;text-align:left;vertical-align:top}}
th{{background:var(--blue-soft);font-weight:700;white-space:nowrap}}
.badge{{display:inline-block;padding:2px 10px;border-radius:99px;font-size:13px;font-weight:700;
border:1px solid currentColor}}
.b-ok{{color:var(--green)}} .b-bad{{color:var(--red)}} .b-wait{{color:var(--amber)}}
.b-off{{color:#6b6657}} .b-blue{{color:var(--blue)}}
.btn{{display:inline-block;background:var(--blue);color:#fff;border:0;border-radius:4px;
padding:8px 18px;font-size:15px;cursor:pointer;font-weight:700}}
.btn.gray{{background:#5c5648}} .btn.red{{background:var(--red)}} .btn.green{{background:var(--green)}}
.btn[disabled]{{background:#9a948a;cursor:not-allowed}}
input,select,textarea{{font:inherit;padding:6px 9px;border:1px solid #9d9684;border-radius:4px;background:#fff}}
label.fl{{display:block;margin:8px 0}}
label.fl span{{display:inline-block;width:130px;font-weight:700}}
.flash{{background:#fdecea;border:1px solid var(--red);color:var(--red);
padding:10px 14px;border-radius:4px;margin-bottom:14px;font-weight:700}}
.okbox{{background:#e8f4ec;border-color:var(--green);color:var(--green)}}
.muted{{color:#6b6657}}
.locked{{background:#efece4;border:1px dashed #8a8472;color:#5c5648;padding:10px 14px;border-radius:4px}}
/* ---- 六面展开图 ---- */
.unfold{{display:grid;grid-template-columns:repeat(4,1fr);grid-template-rows:170px 170px 170px;gap:10px;margin:10px 0}}
.face{{border:2px solid var(--ink);border-radius:4px;background:#fff;position:relative;
display:flex;flex-direction:column;align-items:center;justify-content:center;overflow:hidden}}
.face img{{width:100%;height:132px;object-fit:cover;border-bottom:1px solid #cfc8b6}}
.face .cap{{width:100%;text-align:center;font-weight:700;padding:4px;background:var(--blue-soft)}}
.face.missing{{border-style:dashed;border-color:var(--red);background:#fbeae8}}
.face.missing .cap{{background:#f6d9d5;color:var(--red)}}
.face.stale{{opacity:.65}}
.f-top{{grid-column:2;grid-row:1}} .f-left{{grid-column:1;grid-row:2}}
.f-front{{grid-column:2;grid-row:2;outline:3px solid var(--blue);outline-offset:-3px}}
.f-right{{grid-column:3;grid-row:2}} .f-back{{grid-column:4;grid-row:2}}
.f-bottom{{grid-column:2;grid-row:3}}
.dim-good td:nth-child(4){{color:var(--green);font-weight:700}}
.dim-bad{{background:#fdecea!important}}
.dim-bad td{{font-weight:700}}
.dim-bad td:nth-child(4){{color:var(--red)}}
.findings li{{margin-bottom:6px}}
.kpi{{font-size:30px;font-weight:800}}
@media(max-width:900px){{.unfold{{grid-template-columns:repeat(2,1fr);grid-template-rows:none}}
.face{{grid-column:auto!important;grid-row:auto!important;min-height:170px}}}}
</style></head>
<body>
<header>
  <a class="brand" href="/">📦 首箱放行看板</a>
  <nav><a href="/">看板</a><a href="/orders/new">投产登记</a><a href="/samples">留样柜</a></nav>
  <form method="post" action="/auth">
    <label style="color:#ddd">当前身份</label>
    <select name="uid" onchange="this.form.submit()">{opts}</select>
    <noscript><button class="btn" type="submit">切换</button></noscript>
  </form>
</header>
<main>
{('<div class="flash">'+esc(flash)+'</div>') if flash and not flash.startswith('__ok__') else ''}
{('<div class="flash okbox">'+esc(flash[5:])+'</div>') if flash.startswith('__ok__') else ''}
{body}
</main></body></html>"""


STATUS_BADGE = {
    "pending": ("b-wait", "待质检员裁决"),
    "blocked": ("b-bad", "阻断：须返工"),
    "rejected": ("b-bad", "已停机返工"),
    "approved": ("b-ok", "已放行"),
}


def status_badge(status: str, valid: int = 1) -> str:
    cls, txt = STATUS_BADGE.get(status, ("b-off", status))
    html_txt = f'<span class="badge {cls}">{txt}</span>'
    if not valid:
        html_txt += ' <span class="badge b-off">版次变更·结论失效</span>'
    return html_txt


# ---------------- 看板 ----------------

def dashboard(data: dict) -> str:
    cards = []
    for r in data["rows"]:
        o, rev, latest = r["order"], r["revision"], r["latest"]
        if rev is None:
            state = '<span class="badge b-off">无生效版次</span>'
        elif latest is None:
            state = f'<span class="badge b-blue">版次 {esc(rev["rev"])} 待首箱</span>'
        else:
            state = (f'版次 <b>{esc(rev["rev"])}</b> · 第 {latest["sequence"]} 轮 '
                     + status_badge(latest["status"], latest["conclusion_valid"]))
        cards.append(f"""<div class="card"><h3>{esc(o["code"])} · {esc(o["product"])}</h3>
<p>客户：{esc(o["customer"])}　箱型：{esc(o["box_type"])}　产线：{esc(o["line"])}</p>
<p>刀模 {esc(o["die_no"])}　纸批 {esc(o["paper_batch"])}　印色 {esc(o["colors"])}</p>
<p>{state}</p>
<p><a class="btn" href="/orders/{o["id"]}">订单/版次</a>
<a class="btn gray" href="/revisions/{rev["id"]}">首箱采集</a></p></div>""")
    pend = "".join(
        f'<tr><td><a href="/fa/{p["id"]}">{esc(p["order_code"])} / {esc(p["rev"])}'
        f' 第{p["sequence"]}轮</a></td><td>{esc(p["operator_name"])}</td>'
        f'<td>{esc(p["created_at"])}</td></tr>' for p in data["pending"]) or \
        '<tr><td colspan="3" class="muted">无待裁决赛项</td></tr>'
    stops = "".join(
        f'<tr><td>{esc(s["order_code"])} / {esc(s["rev"])} 第{s["sequence"]}轮</td>'
        f'<td>{esc(s["reason"])}</td><td>{esc(s["created_at"])}</td></tr>'
        for s in data["stoppages"]) or '<tr><td colspan="3" class="muted">暂无停机记录</td></tr>'
    body = f"""
<h1>车间看板</h1>
<div class="grid cards">{''.join(cards) or '<p class="muted">暂无订单</p>'}</div>
<h2>待质检员裁决（操作员不可自批）</h2>
<table><tr><th>首箱</th><th>操作员</th><th>提交时间</th></tr>{pend}</table>
<h2>停机返工记录</h2>
<table><tr><th>对象</th><th>停机原因</th><th>时间</th></tr>{stops}</table>
"""
    return layout("看板", body)


# ---------------- 订单 / 版次 ----------------

def new_order_form(colors_palette: list[str]) -> str:
    checks = "".join(
        f'<label style="margin-right:12px"><input type="checkbox" name="colors" value="{c}" '
        f'{"checked" if c in ("黑","蓝") else ""}> {c}</label>' for c in colors_palette)
    body = f"""
<h1>投产前登记</h1>
<form method="post" action="/orders" class="card" style="max-width:760px">
<label class="fl"><span>订单号</span><input name="code" required placeholder="如 PO-2026-005"></label>
<label class="fl"><span>客户</span><input name="customer" required></label>
<label class="fl"><span>品名</span><input name="product" required></label>
<label class="fl"><span>箱型</span>
  <select name="box_type"><option>标准对口箱</option><option>天地盖</option>
  <option>插底自锁盒</option><option>全开模切盒</option></select></label>
<label class="fl"><span>产线</span>
  <select name="line"><option>1号线</option><option>2号线</option><option>3号线</option></select></label>
<label class="fl"><span>刀模编号</span><input name="die_no" required placeholder="如 DM-301-A"></label>
<label class="fl"><span>纸张批次</span><input name="paper_batch" required placeholder="如 PAP-1008"></label>
<label class="fl"><span>目标尺寸(mm)</span>
  长<input name="l" size="6" value="300"> ×
  宽<input name="w" size="6" value="200"> ×
  高<input name="h" size="6" value="150"></label>
<label class="fl"><span>印色</span>{checks}</label>
<button class="btn" type="submit">登记订单并生成 A 版稿件</button>
<p class="muted">提交后系统在本地 files/artwork 生成客户稿件文件（SVG），并建立 A 版次。</p>
</form>"""
    return layout("投产登记", body)


def order_detail(svc: Service, order, users: list) -> str:
    revs = svc.revision_chain(order["id"])
    rows = []
    for r in revs:
        chain = svc.fa_chain(r["id"])
        fa_txt = "；".join(
            f'第{fa["sequence"]}轮 {esc(STATUS_BADGE[fa["status"]][1])}'
            + ("（失效）" if not fa["conclusion_valid"] else "")
            + f' <a href="/fa/{fa["id"]}">查看</a>'
            for fa in chain) or "未采集"
        cls = "b-ok" if r["status"] == "active" else "b-off"
        state = "生效" if r["status"] == "active" else "已被取代"
        rows.append(f"""<tr>
<td><b>{esc(r["rev"])}</b><br><span class="badge {cls}">{state}</span></td>
<td><a href="/file?path={esc(r["artwork_path"])}" target="_blank">稿件文件 ↗</a><br>
<span class="muted">{esc(r["artwork_sha256"][:12])}…</span></td>
<td>{esc(r["die_no"])}</td><td>{esc(r["paper_batch"])}</td><td>{esc(r["colors"])}</td>
<td>{fa_txt}</td><td>{esc(r["created_at"])}</td></tr>""")
    uopts = "".join(f'<option value="{u["id"]}">{esc(u["name"])}（操作员）</option>'
                    for u in users if u["role"] == "operator")
    body = f"""
<h1>订单 {esc(order["code"])} · {esc(order["product"])}</h1>
<p>客户 {esc(order["customer"])} ｜ 箱型 {esc(order["box_type"])} ｜ 产线 {esc(order["line"])} ｜
目标尺寸 {order["target_l"]:g}×{order["target_w"]:g}×{order["target_h"]:g} mm</p>
<h2>稿件版次链</h2>
<table><tr><th>版次</th><th>客户稿件</th><th>刀模</th><th>纸批</th><th>印色</th>
<th>首箱轮次与结论</th><th>登记时间</th></tr>{''.join(rows)}</table>
<p class="locked">⚠ 登记新版次后，旧版次首箱结论立即失效，旧留样封档只读；
同一订单同一版次只保留一份生效留样。</p>
<h2>登记新版次（客户稿件变更）</h2>
<form class="card" method="post" action="/orders/{order["id"]}/revisions"
 enctype="multipart/form-data" style="max-width:760px">
<label class="fl"><span>新版次</span><input name="rev" required placeholder="如 B" size="4"></label>
<label class="fl"><span>刀模编号</span><input name="die_no" value="{esc(order["die_no"])}" required></label>
<label class="fl"><span>纸张批次</span><input name="paper_batch" value="{esc(order["paper_batch"])}" required></label>
<label class="fl"><span>印色(逗号)</span><input name="colors" value="{esc(order["colors"])}" size="24"></label>
<label class="fl"><span>操作员</span><select name="operator_id">{uopts}</select></label>
<label class="fl"><span>客户稿件</span><input type="file" name="artwork">
<span class="muted">未选文件时按版次信息生成 SVG 登记稿</span></label>
<button class="btn red" type="submit">登记新版次（旧结论失效）</button>
</form>"""
    return layout(f"订单 {order['code']}", body)


# ---------------- 首箱采集 ----------------

def capture_form(svc: Service, revision, order, users: list, prev_fa_id: int | None = None) -> str:
    seq = svc.next_sequence(revision["id"])
    uopts = "".join(f'<option value="{u["id"]}" {"selected" if u["login"]=="op1" else ""}>'
                    f'{esc(u["name"])}</option>' for u in users if u["role"] == "operator")
    face_checks = "".join(
        f'<label style="margin-right:14px"><input type="checkbox" name="face_{f}" checked> '
        f'{config.FACE_LABELS[f]}</label>' for f in config.FACES)
    pre = f'<p class="locked">复测轮次：前序第 {seq-1} 轮已判停机返工（FA #{prev_fa_id}）。</p>' \
        if prev_fa_id else ""
    body = f"""
<h1>首箱采集 · {esc(order["code"])} / 版次 {esc(revision["rev"])} 第 {seq} 轮</h1>
{pre}
<form class="card" method="post" action="/revisions/{revision["id"]}/capture" style="max-width:860px">
<input type="hidden" name="prev_fa_id" value="{prev_fa_id or ''}">
<label class="fl"><span>操作员</span><select name="operator_id">{uopts}</select></label>
<h2 style="margin-top:14px">实测尺寸（mm，与目标并排核对）</h2>
<label class="fl"><span>长 目标 {order["target_l"]:g}</span>
  <input name="l" size="7" value="{order["target_l"]:g}"></label>
<label class="fl"><span>宽 目标 {order["target_w"]:g}</span>
  <input name="w" size="7" value="{order["target_w"]:g}"></label>
<label class="fl"><span>高 目标 {order["target_h"]:g}</span>
  <input name="h" size="7" value="{order["target_h"]:g}"></label>
<label class="fl"><span>条码扫描</span>
  <select name="barcode_mode">
    <option value="readable">模拟扫码：可读且匹配</option>
    <option value="no_read">模拟扫码：不可读（NO_READ）</option>
    <option value="wrong_code">模拟扫码：扫到他单条码</option>
  </select></label>
<label class="fl"><span>上机刀模</span>
  <input name="die_scanned" value="{esc(revision["die_no"])}" size="16">
  <span class="muted">改成 {esc(revision["die_no"] + "-OLD")} 可模拟混用旧刀模</span></label>
<label class="fl"><span>样箱状态</span>
  <label><input type="checkbox" name="sample_intact" checked> 样箱完好（取消=破损）</label></label>
<h2>六面照片</h2>
<p>{face_checks}</p>
<p class="muted">勾选即由模拟相机在 files/photos 生成该面照片；故意取消一面可验证“照片缺面阻断”。</p>
<button class="btn" type="submit">提交首箱并自动核对</button>
</form>"""
    return layout("首箱采集", body)


# ---------------- 首箱核对页（六面展开图） ----------------

def fa_detail(svc: Service, fa, revision, order, current_uid: int | None) -> str:
    photos = {p["face"]: p for p in svc.photos_of(fa["id"])}
    stale = (not fa["conclusion_valid"]) or revision["status"] != "active"

    cells = []
    grid_pos = {"top": "f-top", "left": "f-left", "front": "f-front",
                "right": "f-right", "back": "f-back", "bottom": "f-bottom"}
    for f in config.FACES:
        p = photos.get(f)
        if p:
            inner = (f'<a href="/file?path={esc(p["path"])}" target="_blank">'
                     f'<img src="/file?path={esc(p["path"])}" alt="{config.FACE_LABELS[f]}"></a>'
                     f'<div class="cap">{config.FACE_LABELS[f]}</div>')
            cls = "face " + grid_pos[f] + (" stale" if stale else "")
        else:
            inner = (f'<div style="font-size:30px">🚫</div>'
                     f'<div class="cap">{config.FACE_LABELS[f]} · 缺面</div>')
            cls = "face missing " + grid_pos[f]
        cells.append(f'<div class="{cls}">{inner}</div>')

    # 测量值与目标值并排；超差行显示具体数值。
    dim_rows = []
    for axis, label in (("l", "长"), ("w", "宽"), ("h", "高")):
        target = fa[f"target_{axis}"]
        measured = fa[f"measured_{axis}"]
        chk = DimCheck(axis, label, target, measured)
        if measured is None:
            m_txt, d_txt = "—", "未测量"
            over = True
        else:
            sign = "+" if (chk.delta or 0) > 0 else ""
            m_txt = f"{measured:g}"
            d_txt = f"{sign}{chk.delta:g} mm"
            over = chk.over
        dim_rows.append(
            f'<tr class="{"dim-bad" if over else "dim-good"}"><td>{label}</td>'
            f'<td>{target:g} mm</td><td>{m_txt} mm</td>'
            f'<td>{d_txt}　{("❌ 超差（±" + str(config.TOL_MM).rstrip("0").rstrip(".") + " mm）") if over else "✓ 在公差内"}</td></tr>')

    findings = svc.list_findings(fa["id"])
    f_html = "".join(
        f'<li><span class="badge b-bad">{esc(BLOCK_CODES.get(f["code"], f["code"]))}</span> '
        f'{esc(f["message"])}</li>' for f in findings) or \
        '<li class="b-ok" style="color:var(--green);list-style:none;margin-left:-22px">✓ 五项核对全部通过</li>'

    approval = svc.approval_of(fa["id"])
    barcode_cls = "b-ok" if fa["barcode_ok"] else "b-bad"
    barcode_txt = "可读：" + esc(fa["barcode_scan"] or "") if fa["barcode_ok"] else "不可读/不符"
    die_ok = fa["die_scanned"] == revision["die_no"]
    die_txt = (f'{esc(fa["die_scanned"])} ✓' if die_ok
               else f'{esc(fa["die_scanned"])} ❌ 要求 {esc(revision["die_no"])}（混用旧刀模）')

    # 裁决区
    is_own = current_uid == fa["operator_id"]
    op = svc._fetch("SELECT name FROM users WHERE id=?", (fa["operator_id"],))[0]["name"]
    if stale:
        action = ('<div class="locked">🔒 该首箱结论已随旧版次失效，仅可只读查阅，'
                  '不能放行、返工登记或留样。</div>')
    elif approval:
        cls = "b-ok" if approval["decision"] == "release" else "b-bad"
        verdict = "放行，继续生产" if approval["decision"] == "release" else "停机返工"
        action = (f'<div class="locked">质检员 <b>{esc(approval["qc_name"])}</b> 已裁决：'
                  f'<span class="badge {cls}">{verdict}</span>　{esc(approval["note"])}'
                  f'<div class="muted" style="margin-top:6px">{esc(approval["created_at"])}</div></div>')
    elif fa["status"] == "blocked":
        action = ('<div class="locked">存在阻断项，不能放行；请质检员判停机返工，'
                  '整改后发起复测。</div>')
    elif is_own:
        action = ('<div class="locked">🚫 操作员不能批准自己的首箱。'
                  '请在右上角切换为质检员（王检/陈检）后裁决。</div>')
    else:
        can_release = "disabled" if findings else ""
        action = f"""<form method="post" action="/fa/{fa["id"]}/decide" style="display:flex;gap:10px;flex-wrap:wrap">
<button class="btn green" name="decision" value="release" {can_release}>✓ 放行·继续生产</button>
<button class="btn red" name="decision" value="rework">⛔ 停机返工</button>
<input name="reason" size="42" placeholder="停机原因（判返工时填写）"></form>"""

    # 留样 / 复测
    extra = ""
    sample = svc._fetch(
        "SELECT * FROM retained_samples WHERE fa_id=?", (fa["id"],))
    sample = sample[0] if sample else None
    if sample is not None:
        s_cls = "b-ok" if sample["state"] == "active" else "b-off"
        s_txt = "生效留样" if sample["state"] == "active" else "已封档·只读"
        extra += (f'<p>留样：柜位 <b>{esc(sample["cabinet"])}</b> '
                  f'<span class="badge {s_cls}">{s_txt}</span></p>')
    if approval and approval["decision"] == "release" and sample is None and not stale:
        extra += f"""<form method="post" action="/fa/{fa["id"]}/retain" style="margin-top:8px">
<label>留样柜位 <input name="cabinet" required value="A-{(fa['id']%12)+1:02d}" size="8">
<button class="btn" type="submit">登记留样入柜</button></label></form>"""
    if approval and approval["decision"] == "rework" and not stale:
        extra += (f'<p style="margin-top:10px"><a class="btn gray" '
                  f'href="/revisions/{revision["id"]}/capture?prev={fa["id"]}">'
                  f'返工整改后发起复测 →</a></p>')
    if fa["prev_fa_id"]:
        extra += (f'<p class="muted">本轮为复测，前序首箱：'
                  f'<a href="/fa/{fa["prev_fa_id"]}">#{fa["prev_fa_id"]}</a></p>')

    body = f"""
<h1>首箱核对 · {esc(order["code"])} / 版次 {esc(revision["rev"])} 第 {fa["sequence"]} 轮</h1>
<p>{status_badge(fa["status"], fa["conclusion_valid"])}
操作员 <b>{esc(op)}</b>　采集时间 {esc(fa["created_at"])}</p>
{'<div class="locked">🔒 <b>版次已变更</b>：客户稿件更新到新版次后，本首箱的放行结论与留样已失效/封档。</div>' if stale else ''}
<h2>六面展开核对图</h2>
<div class="unfold">{''.join(cells)}</div>
<h2>测量值 / 目标值（公差 ±{config.TOL_MM:g} mm）</h2>
<table><tr><th>项目</th><th>目标值</th><th>实测值</th><th>偏差与判定</th></tr>{''.join(dim_rows)}</table>
<h2>条码 · 刀模 · 样箱</h2>
<table>
<tr><th>条码扫描</th><td><span class="badge {barcode_cls}">{barcode_txt}</span></td></tr>
<tr><th>上机刀模</th><td>{die_txt}</td></tr>
<tr><th>样箱状态</th><td>{'完好 ✓' if fa['sample_intact'] else '<span class="badge b-bad">破损 ❌</span>'}</td></tr>
</table>
<h2>差异标注（显示具体数值）</h2>
<ul class="findings">{f_html}</ul>
<h2>质检员裁决</h2>
{action}
{extra}
<p style="margin-top:18px"><a href="/revisions/{revision["id"]}">← 版次页</a>
<a href="/orders/{order["id"]}">订单版次链</a></p>"""
    return layout("首箱核对", body)


# ---------------- 留样柜 ----------------

def samples_board(svc: Service) -> str:
    data = svc.dashboard()
    rows = []
    for s in data["samples"]:
        locked = s["state"] != "active"
        badge = ('<span class="badge b-off">已封档·只读</span>' if locked
                 else '<span class="badge b-ok">生效留样</span>')
        move = ("—" if locked else f"""<form method="post" action="/samples/{s["id"]}/move"
 style="display:flex;gap:6px"><input name="cabinet" size="7" value="{esc(s["cabinet"])}">
<button class="btn gray" type="submit">移位</button></form>""")
        rows.append(f"""<tr>
<td>{esc(s["order_code"])}</td><td>{esc(s["rev"])}</td><td>第{s["sequence"]}轮</td>
<td><b>{esc(s["cabinet"])}</b></td><td>{esc(s["qc_name"])}</td><td>{badge}</td>
<td>{esc(s["created_at"])}</td><td>{move}</td>
<td><a href="/fa/{s["fa_id"]}">首箱记录</a></td></tr>""")
    body = f"""
<h1>留样柜位</h1>
<p class="muted">同一订单同一版次仅一份生效留样；版次变更后旧留样封档，照片目录转只读，禁止移位或覆盖。</p>
<table><tr><th>订单</th><th>版次</th><th>轮次</th><th>柜位</th><th>放行质检</th>
<th>状态</th><th>入柜时间</th><th>操作</th><th></th></tr>
{''.join(rows) or '<tr><td colspan="9" class="muted">暂无留样</td></tr>'}</table>"""
    return layout("留样柜", body)
