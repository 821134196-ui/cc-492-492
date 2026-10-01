"""SQLite 数据层：连接管理与建表。

所有审批/留样的"唯一生效"约束都落在数据库的部分唯一索引上，
即使两个线程同时放行，数据库也只允许一个成功（见 unique_active_approval）。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

-- 操作人员（操作员 / 质检员）
CREATE TABLE IF NOT EXISTS users (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    role        TEXT NOT NULL CHECK (role IN ('operator', 'qc'))
);

-- 客户订单
CREATE TABLE IF NOT EXISTS orders (
    id          INTEGER PRIMARY KEY,
    order_no    TEXT NOT NULL UNIQUE,
    customer    TEXT NOT NULL,
    box_type    TEXT NOT NULL,                 -- 箱型
    line        TEXT NOT NULL,                 -- 产线
    colors      TEXT NOT NULL,                 -- 印色，如 四色+专蓝
    die_no      TEXT NOT NULL,                 -- 目标刀模编号
    target_l    REAL NOT NULL,                 -- 目标尺寸 长 mm
    target_w    REAL NOT NULL,                 -- 宽
    target_h    REAL NOT NULL,                 -- 高
    tol_mm      REAL NOT NULL DEFAULT 2.0,     -- 允许偏差 ±mm
    barcode     TEXT NOT NULL                  -- 目标条码内容
);

-- 订单的稿件版次。同一订单只能有一个 active 版次。
CREATE TABLE IF NOT EXISTS revisions (
    id          INTEGER PRIMARY KEY,
    order_id    INTEGER NOT NULL REFERENCES orders(id),
    rev_code    TEXT NOT NULL,                 -- 如 V1 / V2
    art_file    TEXT NOT NULL,                 -- 本地稿件相对路径
    created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    active      INTEGER NOT NULL DEFAULT 1,
    UNIQUE (order_id, rev_code)
);
-- 同一订单仅一个生效版次
CREATE UNIQUE INDEX IF NOT EXISTS unique_active_revision
    ON revisions(order_id) WHERE active = 1;

-- 投产登记（每次换版重新登记）
CREATE TABLE IF NOT EXISTS productions (
    id              INTEGER PRIMARY KEY,
    order_id        INTEGER NOT NULL REFERENCES orders(id),
    revision_id     INTEGER NOT NULL REFERENCES revisions(id),
    paper_batch     TEXT NOT NULL,             -- 纸张批次
    die_no_actual   TEXT NOT NULL,             -- 实际上机刀模编号
    colors_actual   TEXT NOT NULL,             -- 实际印色
    box_type_actual TEXT NOT NULL,
    line_actual     TEXT NOT NULL,
    registered_by   INTEGER NOT NULL REFERENCES users(id),
    created_at      TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- 首箱采集。一个投产登记可有多条（返工复测时新增一条）。
CREATE TABLE IF NOT EXISTS first_articles (
    id              INTEGER PRIMARY KEY,
    production_id   INTEGER NOT NULL REFERENCES productions(id),
    seq             INTEGER NOT NULL,          -- 1=首箱, 2=返工复测...
    measured_l      REAL,
    measured_w      REAL,
    measured_h      REAL,
    barcode_scan    TEXT,                      -- 扫码枪实测内容
    barcode_ok      INTEGER,                   -- 扫码枪可读性
    cabinet_slot    TEXT,                      -- 留样柜位
    sample_intact   INTEGER,                   -- 样箱是否完好
    submitted_by    INTEGER NOT NULL REFERENCES users(id),
    created_at      TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- 六面照片：front/back/left/right/top/bottom
CREATE TABLE IF NOT EXISTS photos (
    id                INTEGER PRIMARY KEY,
    first_article_id  INTEGER NOT NULL REFERENCES first_articles(id) ON DELETE CASCADE,
    face              TEXT NOT NULL CHECK (face IN
                        ('front','back','left','right','top','bottom')),
    file_path         TEXT NOT NULL,
    UNIQUE (first_article_id, face)
);

-- 质检员对首箱的处置：released（放行）/ held（停机返工）
CREATE TABLE IF NOT EXISTS approvals (
    id                INTEGER PRIMARY KEY,
    first_article_id  INTEGER NOT NULL REFERENCES first_articles(id),
    qc_user_id        INTEGER NOT NULL REFERENCES users(id),
    decision          TEXT NOT NULL CHECK (decision IN ('released','held')),
    reason            TEXT,                    -- 停机原因 / 差异标注
    created_at         TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    UNIQUE (first_article_id, qc_user_id)
);
-- 关键竞态约束：同一首箱至多一份"放行"，两名质检员同时放行仅一人成功
CREATE UNIQUE INDEX IF NOT EXISTS unique_release_per_article
    ON approvals(first_article_id) WHERE decision = 'released';

-- 留样柜台账：放行审批与留样行在同一事务内写入。
-- revision_id 为主键 ⇒ 同一订单同一版次只能有一份留样；
-- 两名质检员即使放行不同首箱，也只有一人能成功。
-- 版次失效后行仍保留，即"旧留样"：仅可查看，不可修改/删除/重新审批。
CREATE TABLE IF NOT EXISTS samples (
    revision_id      INTEGER PRIMARY KEY REFERENCES revisions(id),
    production_id    INTEGER NOT NULL REFERENCES productions(id),
    first_article_id INTEGER NOT NULL UNIQUE REFERENCES first_articles(id),
    cabinet_slot     TEXT NOT NULL,
    created_at       TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- 旧留样只读：台账与已入柜首箱/照片在数据库层禁止 UPDATE/DELETE
CREATE TRIGGER IF NOT EXISTS samples_no_update
BEFORE UPDATE ON samples BEGIN SELECT RAISE(ABORT, '留样台账只读，禁止修改'); END;
CREATE TRIGGER IF NOT EXISTS samples_no_delete
BEFORE DELETE ON samples BEGIN SELECT RAISE(ABORT, '留样台账只读，禁止删除'); END;
CREATE TRIGGER IF NOT EXISTS locked_article_no_update
BEFORE UPDATE ON first_articles
WHEN EXISTS (SELECT 1 FROM samples s WHERE s.first_article_id = OLD.id)
BEGIN SELECT RAISE(ABORT, '已入柜首箱只读，禁止修改'); END;
CREATE TRIGGER IF NOT EXISTS locked_article_no_delete
BEFORE DELETE ON first_articles
WHEN EXISTS (SELECT 1 FROM samples s WHERE s.first_article_id = OLD.id)
BEGIN SELECT RAISE(ABORT, '已入柜首箱只读，禁止删除'); END;
CREATE TRIGGER IF NOT EXISTS locked_photo_no_update
BEFORE UPDATE ON photos
WHEN EXISTS (SELECT 1 FROM samples s WHERE s.first_article_id = OLD.first_article_id)
BEGIN SELECT RAISE(ABORT, '已入柜六面照片只读，禁止修改'); END;
CREATE TRIGGER IF NOT EXISTS locked_photo_no_delete
BEFORE DELETE ON photos
WHEN EXISTS (SELECT 1 FROM samples s WHERE s.first_article_id = OLD.first_article_id)
BEGIN SELECT RAISE(ABORT, '已入柜六面照片只读，禁止删除'); END;
"""

FACES = ["front", "back", "left", "right", "top", "bottom"]
FACE_CN = {
    "front": "正面", "back": "背面", "left": "左面",
    "right": "右面", "top": "上面", "bottom": "下面",
}


def connect(db_path: str | Path) -> sqlite3.Connection:
    # isolation_level=None：完全显式事务（服务层用 BEGIN IMMEDIATE 串行化写操作）
    conn = sqlite3.connect(str(db_path), timeout=10, check_same_thread=False,
                           isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()
