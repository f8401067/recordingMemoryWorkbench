"""initial schema —— 建表 + 老库补列

Revision ID: 0001_initial
Revises:
Create Date: 2026-10-30

设计要点：
- DDL 只用 SQLite 与 PostgreSQL 都支持的语法（布尔值一律 INTEGER 0/1）；
- 全部操作幂等：新库从零建表，老库（已经跑过 schema.sql 的）只会被补齐缺的列/索引；
- 这样不需要区分「新库 baseline」和「老库升级」，直接 upgrade head 就行。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


TABLES: list[tuple[str, str]] = [
    (
        "app_meta",
        """CREATE TABLE IF NOT EXISTS app_meta (
             key   TEXT PRIMARY KEY,
             value TEXT
           )""",
    ),
    (
        "users",
        """CREATE TABLE IF NOT EXISTS users (
             id            TEXT PRIMARY KEY,
             username      TEXT NOT NULL UNIQUE,
             password_hash TEXT NOT NULL,
             display_name  TEXT,
             created_at    TEXT NOT NULL
           )""",
    ),
    (
        "api_keys",
        """CREATE TABLE IF NOT EXISTS api_keys (
             id           TEXT PRIMARY KEY,
             user_id      TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
             name         TEXT NOT NULL,
             key_value    TEXT NOT NULL UNIQUE,
             created_at   TEXT NOT NULL,
             last_used_at TEXT
           )""",
    ),
    (
        "providers",
        """CREATE TABLE IF NOT EXISTS providers (
             id         TEXT PRIMARY KEY,
             user_id    TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
             category   TEXT NOT NULL,
             kind       TEXT NOT NULL,
             name       TEXT NOT NULL,
             base_url   TEXT,
             api_key    TEXT,
             model      TEXT,
             params     TEXT,
             is_default INTEGER NOT NULL DEFAULT 0,
             enabled    INTEGER NOT NULL DEFAULT 1,
             created_at TEXT NOT NULL
           )""",
    ),
    (
        "recordings",
        """CREATE TABLE IF NOT EXISTS recordings (
             id              TEXT PRIMARY KEY,
             user_id         TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
             title           TEXT NOT NULL,
             status          TEXT NOT NULL DEFAULT 'recording',
             source          TEXT NOT NULL DEFAULT 'web',
             language        TEXT NOT NULL DEFAULT 'zh',
             started_at      TEXT NOT NULL,
             ended_at        TEXT,
             duration_ms     INTEGER NOT NULL DEFAULT 0,
             asr_provider_id TEXT,
             summary_status  TEXT NOT NULL DEFAULT 'none',
             memo            TEXT,
             created_at      TEXT NOT NULL
           )""",
    ),
    (
        "chunks",
        """CREATE TABLE IF NOT EXISTS chunks (
             id              TEXT PRIMARY KEY,
             recording_id    TEXT NOT NULL REFERENCES recordings(id) ON DELETE CASCADE,
             seq             INTEGER NOT NULL,
             filename        TEXT NOT NULL,
             path            TEXT NOT NULL,
             mime            TEXT,
             size            INTEGER NOT NULL DEFAULT 0,
             duration_ms     INTEGER NOT NULL DEFAULT 0,
             start_offset_ms INTEGER NOT NULL DEFAULT 0,
             status          TEXT NOT NULL DEFAULT 'pending',
             transcript      TEXT,
             segments        TEXT,
             error           TEXT,
             retries         INTEGER NOT NULL DEFAULT 0,
             created_at      TEXT NOT NULL,
             updated_at      TEXT NOT NULL,
             UNIQUE(recording_id, seq)
           )""",
    ),
    (
        "summaries",
        """CREATE TABLE IF NOT EXISTS summaries (
             id           TEXT PRIMARY KEY,
             user_id      TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
             recording_id TEXT REFERENCES recordings(id) ON DELETE CASCADE,
             scope        TEXT NOT NULL,
             date_from    TEXT NOT NULL,
             date_to      TEXT NOT NULL,
             title        TEXT NOT NULL,
             template     TEXT,
             markdown     TEXT,
             mindmap      TEXT,
             provider_id  TEXT,
             model        TEXT,
             status       TEXT NOT NULL DEFAULT 'queued',
             error        TEXT,
             stats        TEXT,
             created_at   TEXT NOT NULL,
             updated_at   TEXT NOT NULL
           )""",
    ),
    (
        "jobs",
        """CREATE TABLE IF NOT EXISTS jobs (
             id           TEXT PRIMARY KEY,
             type         TEXT NOT NULL,
             payload      TEXT NOT NULL,
             status       TEXT NOT NULL DEFAULT 'pending',
             attempts     INTEGER NOT NULL DEFAULT 0,
             max_attempts INTEGER NOT NULL DEFAULT 3,
             run_at       TEXT NOT NULL,
             started_at   TEXT,
             finished_at  TEXT,
             error        TEXT,
             created_at   TEXT NOT NULL
           )""",
    ),
    (
        "settings",
        """CREATE TABLE IF NOT EXISTS settings (
             user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
             key     TEXT NOT NULL,
             value   TEXT,
             PRIMARY KEY (user_id, key)
           )""",
    ),
    (
        "devices",
        """CREATE TABLE IF NOT EXISTS devices (
             id                TEXT PRIMARY KEY,
             user_id           TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
             name              TEXT NOT NULL,
             code              TEXT,
             token             TEXT,
             gap_sec           INTEGER NOT NULL DEFAULT 90,
             idle_sec          INTEGER NOT NULL DEFAULT 180,
             open_recording_id TEXT REFERENCES recordings(id) ON DELETE SET NULL,
             last_chunk_at     TEXT,
             last_seen_at      TEXT,
             session_chunks    INTEGER NOT NULL DEFAULT 0,
             bound_at          TEXT,
             last_poll_at      TEXT,
             created_at        TEXT NOT NULL,
             UNIQUE(user_id, name)
           )""",
    ),
    (
        "device_claims",
        """CREATE TABLE IF NOT EXISTS device_claims (
             code          TEXT PRIMARY KEY,
             name          TEXT,
             first_seen_at TEXT NOT NULL,
             last_poll_at  TEXT NOT NULL,
             poll_count    INTEGER NOT NULL DEFAULT 1,
             remote_ip     TEXT,
             firmware      TEXT
           )""",
    ),
]

# 先建表后建索引，索引全部 IF NOT EXISTS
INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_rec_user_start ON recordings(user_id, started_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_chunk_rec ON chunks(recording_id, seq)",
    "CREATE INDEX IF NOT EXISTS idx_sum_user_date ON summaries(user_id, date_from DESC)",
    "CREATE INDEX IF NOT EXISTS idx_job_poll ON jobs(status, run_at)",
    "CREATE INDEX IF NOT EXISTS idx_device_open ON devices(open_recording_id)",
    # device.code / token 是部分唯一索引：历史数据里可以有大量 NULL
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_device_code ON devices(code) WHERE code IS NOT NULL",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_device_token ON devices(token) WHERE token IS NOT NULL",
]

# 老库补列（CREATE TABLE IF NOT EXISTS 不会给已存在的表加字段）
LEGACY_COLUMNS: dict[str, list[tuple[str, str]]] = {
    "devices": [
        ("code", "ALTER TABLE devices ADD COLUMN code TEXT"),
        ("token", "ALTER TABLE devices ADD COLUMN token TEXT"),
        ("bound_at", "ALTER TABLE devices ADD COLUMN bound_at TEXT"),
        ("last_poll_at", "ALTER TABLE devices ADD COLUMN last_poll_at TEXT"),
    ],
    "summaries": [
        ("template", "ALTER TABLE summaries ADD COLUMN template TEXT"),
    ],
}


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)

    for name, ddl in TABLES:
        if not insp.has_table(name):
            op.execute(ddl)

    # 老库补列：必须排在建索引之前（idx_device_code 依赖 devices.code）
    insp = sa.inspect(bind)  # 新建过表之后重新拿一次快照
    for table, columns in LEGACY_COLUMNS.items():
        if not insp.has_table(table):
            continue
        existing = {c["name"] for c in insp.get_columns(table)}
        for column, ddl in columns:
            if column not in existing:
                op.execute(ddl)

    # 索引名在 SQLite 与 PostgreSQL 里都是库级唯一，重复执行安全
    for ddl in INDEXES:
        op.execute(ddl)


def downgrade() -> None:
    # 不提供破坏性回滚：库里的数据比表结构值钱
    pass
