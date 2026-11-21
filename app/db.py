"""SQLite 存取层（标准库 sqlite3，按请求开连接）。"""
from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS catalogs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    version     INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS partitions (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    catalog_id       INTEGER NOT NULL REFERENCES catalogs(id),
    partition_hour   TEXT NOT NULL,   -- UTC 整点 ISO8601
    planned_deadline TEXT NOT NULL,
    submitted_at     TEXT NOT NULL,
    revision         INTEGER NOT NULL,
    created_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_partitions_catalog_hour
    ON partitions(catalog_id, partition_hour);

CREATE TABLE IF NOT EXISTS reports (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    catalog_id    INTEGER NOT NULL REFERENCES catalogs(id),
    name          TEXT NOT NULL,
    offset_days   INTEGER NOT NULL DEFAULT 1,
    grace_minutes INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL,
    deleted_at    TEXT                      -- 软删除：保留身份以归档历史核查
);

CREATE TABLE IF NOT EXISTS checks (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    catalog_id      INTEGER NOT NULL REFERENCES catalogs(id),
    catalog_version INTEGER NOT NULL,   -- 核查时的目录版本；与当前版本不一致即失效
    business_date   TEXT NOT NULL,      -- YYYY-MM-DD
    as_of           TEXT NOT NULL,
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_checks_catalog ON checks(catalog_id);
CREATE INDEX IF NOT EXISTS idx_checks_catalog_date
    ON checks(catalog_id, business_date);

CREATE TABLE IF NOT EXISTS check_results (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    check_id       INTEGER NOT NULL REFERENCES checks(id),
    report_id      INTEGER,             -- 日报身份快照；NULL = 无法确认身份的旧记录（未归属）
    report_name    TEXT NOT NULL,       -- 快照：报表定义可能随后变化
    offset_days    INTEGER NOT NULL,
    grace_minutes  INTEGER NOT NULL,
    window_start   TEXT NOT NULL,
    window_end     TEXT NOT NULL,
    watermark_hour TEXT,                -- 安全水位（窗口内最后一个连续可用分区）
    status         TEXT NOT NULL,       -- READY | BLOCKED
    block_hour     TEXT,
    block_reason   TEXT,                -- MISSING | PENDING | DUPLICATE | LATE
    block_message  TEXT,
    counts_json    TEXT NOT NULL,
    detail_json    TEXT NOT NULL        -- 24 个分区逐小时判定明细
);
CREATE INDEX IF NOT EXISTS idx_check_results_check ON check_results(check_id);
"""

# 旧库增量迁移：(表名, 列名, DDL)
COLUMN_MIGRATIONS = [
    ("reports", "deleted_at",
     "ALTER TABLE reports ADD COLUMN deleted_at TEXT"),
    ("check_results", "report_id",
     "ALTER TABLE check_results ADD COLUMN report_id INTEGER"),
]

# 依赖迁移后新列的索引，在 ALTER 补列之后再创建
POST_MIGRATION_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_check_results_report"
    " ON check_results(report_id)",
]


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _existing_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def init_db(path: str) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = connect(path)
    try:
        conn.executescript(SCHEMA)
        for table, column, ddl in COLUMN_MIGRATIONS:
            if column not in _existing_columns(conn, table):
                conn.execute(ddl)  # 旧库补列；既有核查结果 report_id 为 NULL -> 未归属
        for ddl in POST_MIGRATION_INDEXES:
            conn.execute(ddl)
        conn.commit()
    finally:
        conn.close()
