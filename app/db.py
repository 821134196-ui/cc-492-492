"""SQLite 连接与建表脚本。

版次与审批全部落库；稿件与照片以文件形式落盘，库里只存路径与哈希。
所有写操作开启外键约束，并通过事务 + 行级守卫处理审批竞态。
"""
from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Optional

from . import config

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS users (
    id          INTEGER PRIMARY KEY,
    login       TEXT NOT NULL UNIQUE,
    name        TEXT NOT NULL,
    role        TEXT NOT NULL CHECK (role IN ('operator','qc','admin'))
);

-- 包装订单 + 投产前登记要素（刀模编号/纸张批次/印色/箱型/产线随版次登记）。
CREATE TABLE IF NOT EXISTS orders (
    id          INTEGER PRIMARY KEY,
    code        TEXT NOT NULL UNIQUE,          -- 订单号
    customer    TEXT NOT NULL,
    product     TEXT NOT NULL,
    box_type    TEXT NOT NULL,                 -- 箱型
    line        TEXT NOT NULL,                 -- 产线
    die_no      TEXT NOT NULL,                 -- 当前生效刀模编号
    paper_batch TEXT NOT NULL,                 -- 纸张批次
    colors      TEXT NOT NULL,                 -- 印色，逗号分隔
    target_l    REAL NOT NULL,                 -- 目标尺寸 长 mm
    target_w    REAL NOT NULL,
    target_h    REAL NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- 客户稿件版次。同一订单可有多个版次；最多一个 active=1。
-- active 版次被新版本取代（superseded）后，其首箱结论全部失效。
CREATE TABLE IF NOT EXISTS revisions (
    id            INTEGER PRIMARY KEY,
    order_id      INTEGER NOT NULL REFERENCES orders(id),
    rev           TEXT NOT NULL,               -- 版次，如 A / B / C
    artwork_path  TEXT NOT NULL,               -- 本地稿件文件
    artwork_sha256 TEXT NOT NULL,
    die_no        TEXT NOT NULL,               -- 本版次投产刀模（旧刀模混用以此比对）
    paper_batch   TEXT NOT NULL,
    colors        TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'active'
                  CHECK (status IN ('active','superseded')),
    created_at    TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    UNIQUE (order_id, rev)
);

-- 每个订单只允许一个 active 版次（部分唯一索引）。
CREATE UNIQUE INDEX IF NOT EXISTS idx_rev_one_active
    ON revisions(order_id) WHERE status='active';

-- 首箱采集（含首箱与返工复测，sequence=1 为首箱，之后为复测轮次）。
CREATE TABLE IF NOT EXISTS first_articles (
    id            INTEGER PRIMARY KEY,
    revision_id   INTEGER NOT NULL REFERENCES revisions(id),
    sequence      INTEGER NOT NULL,            -- 1=首箱 2..=返工复测
    operator_id   INTEGER NOT NULL REFERENCES users(id),
    measured_l    REAL, measured_w REAL, measured_h REAL,   -- 实测长宽高 mm
    target_l      REAL NOT NULL, target_w REAL NOT NULL, target_h REAL NOT NULL,
    barcode_scan  TEXT,                        -- 模拟扫码结果
    barcode_ok    INTEGER NOT NULL DEFAULT 0,
    die_scanned   TEXT,                        -- 上机刀模扫描值
    sample_intact INTEGER NOT NULL DEFAULT 1,  -- 样箱是否完好
    conclusion_valid INTEGER NOT NULL DEFAULT 1, -- 版次变更后置 0：旧首箱结论失效
    prev_fa_id    INTEGER REFERENCES first_articles(id), -- 返工复测链
    status        TEXT NOT NULL DEFAULT 'pending'
                  CHECK (status IN ('pending','blocked','rejected','approved')),
    created_at    TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    UNIQUE (revision_id, sequence)
);

-- 六面照片：一面一行，缺面即阻断。
CREATE TABLE IF NOT EXISTS photos (
    id        INTEGER PRIMARY KEY,
    fa_id     INTEGER NOT NULL REFERENCES first_articles(id) ON DELETE CASCADE,
    face      TEXT NOT NULL CHECK (face IN
              ('front','back','left','right','top','bottom')),
    path      TEXT NOT NULL,
    sha256    TEXT NOT NULL,
    UNIQUE (fa_id, face)
);

-- 差异标注（测量/条码/刀模/照片/样箱等），保留具体数值。
CREATE TABLE IF NOT EXISTS findings (
    id        INTEGER PRIMARY KEY,
    fa_id     INTEGER NOT NULL REFERENCES first_articles(id) ON DELETE CASCADE,
    code      TEXT NOT NULL,                   -- barcode_unreadable / dim_over ...
    face      TEXT,
    message   TEXT NOT NULL,                   -- 含目标值/实测值/偏差
    blocking  INTEGER NOT NULL DEFAULT 1
);

-- 停机原因（质检员判停机返工时登记）。
CREATE TABLE IF NOT EXISTS stoppages (
    id          INTEGER PRIMARY KEY,
    fa_id       INTEGER NOT NULL REFERENCES first_articles(id),
    reason      TEXT NOT NULL,
    qc_id       INTEGER NOT NULL REFERENCES users(id),
    created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- 审批（放行）。first_article_id 上的唯一索引保证同一首箱只有一次成功放行，
-- 两名质检员并发提交时只有一人成功；旧版次首箱禁止补批。
CREATE TABLE IF NOT EXISTS approvals (
    id          INTEGER PRIMARY KEY,
    fa_id       INTEGER NOT NULL UNIQUE REFERENCES first_articles(id),
    qc_id       INTEGER NOT NULL REFERENCES users(id),
    decision    TEXT NOT NULL CHECK (decision IN ('release','rework')),
    note        TEXT DEFAULT '',
    created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- 留样：同一订单同一版次只能有一份“生效留样”。
-- 版次失效后留样转为 archived，文件只读（UI 拒绝一切写操作）。
CREATE TABLE IF NOT EXISTS retained_samples (
    id          INTEGER PRIMARY KEY,
    order_id    INTEGER NOT NULL REFERENCES orders(id),
    revision_id INTEGER NOT NULL REFERENCES revisions(id),
    fa_id       INTEGER NOT NULL REFERENCES first_articles(id),
    cabinet     TEXT NOT NULL,                 -- 留样柜位，如 A-03
    state       TEXT NOT NULL DEFAULT 'active'
                CHECK (state IN ('active','archived')),
    created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_retain_one_active
    ON retained_samples(order_id, revision_id) WHERE state='active';
-- 生效留样的柜位互斥：一个柜位同一时刻只能放一份生效留样。
CREATE UNIQUE INDEX IF NOT EXISTS idx_retain_cabinet
    ON retained_samples(cabinet) WHERE state='active';
"""


def connect(db_path: Optional[Path] = None) -> sqlite3.Connection:
    path = Path(db_path or config.DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Web 端是多线程服务器，连接由 WAL + BEGIN IMMEDIATE 串行化写事务。
    conn = sqlite3.connect(str(path), timeout=5, isolation_level=None,
                           check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


class Hub:
    """每线程一个连接（ThreadingHTTPServer 下并发请求互不串扰）。

    写事务统一用 BEGIN IMMEDIATE 串行化，配合 WAL 满足
    “两名质检员并发放行仅一人成功”。
    """

    def __init__(self, db_path: Optional[Path] = None):
        self.path = Path(db_path or config.DB_PATH)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()

    @property
    def conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "conn", None)
        if c is None:
            c = connect(self.path)
            self._local.conn = c
        return c

    def close(self) -> None:
        c = getattr(self._local, "conn", None)
        if c is not None:
            c.close()
            self._local.conn = None
