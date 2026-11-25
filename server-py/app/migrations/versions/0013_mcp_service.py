"""对外提供 MCP 服务：Token、客户端会话与调用记录

三张表：

- ``mcp_service_tokens``   每个账号一把 MCP 访问令牌，自动生成、可重置
- ``mcp_service_sessions`` initialize 握手过的客户端，用来把后续调用归到具体客户端
- ``mcp_service_logs``     每次 JSON-RPC 调用留档（握手 / 列工具 / 调工具 / 鉴权失败）

Revision ID: 0013_mcp_service
Revises: 0012_mcp_tool_capability
Create Date: 2026-11-06
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0013_mcp_service"
down_revision = "0012_mcp_tool_capability"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)

    if not insp.has_table("mcp_service_tokens"):
        op.execute(
            """CREATE TABLE IF NOT EXISTS mcp_service_tokens (
                 id           TEXT PRIMARY KEY,
                 user_id      TEXT NOT NULL UNIQUE REFERENCES users(id) ON DELETE CASCADE,
                 token        TEXT NOT NULL UNIQUE,
                 enabled      INTEGER NOT NULL DEFAULT 1,
                 created_at   TEXT NOT NULL,
                 reset_at     TEXT,
                 last_used_at TEXT
               )"""
        )

    if not insp.has_table("mcp_service_sessions"):
        op.execute(
            """CREATE TABLE IF NOT EXISTS mcp_service_sessions (
                 id               TEXT PRIMARY KEY,
                 user_id          TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                 token_id         TEXT,
                 client_name      TEXT,
                 client_version   TEXT,
                 protocol_version TEXT,
                 ip               TEXT,
                 user_agent       TEXT,
                 first_seen_at    TEXT NOT NULL,
                 last_seen_at     TEXT NOT NULL,
                 call_count       INTEGER NOT NULL DEFAULT 0
               )"""
        )

    if not insp.has_table("mcp_service_logs"):
        op.execute(
            """CREATE TABLE IF NOT EXISTS mcp_service_logs (
                 id          TEXT PRIMARY KEY,
                 user_id     TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                 session_id  TEXT,
                 client_name TEXT,
                 method      TEXT,
                 tool        TEXT,
                 args        TEXT,
                 ok          INTEGER NOT NULL DEFAULT 1,
                 error       TEXT,
                 duration_ms INTEGER NOT NULL DEFAULT 0,
                 ip          TEXT,
                 created_at  TEXT NOT NULL
               )"""
        )

    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_mcp_service_sessions_user "
        "ON mcp_service_sessions(user_id, last_seen_at DESC)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_mcp_service_logs_user "
        "ON mcp_service_logs(user_id, created_at DESC)"
    )


def downgrade() -> None:
    # 令牌与调用记录属于用户数据，不做破坏性回滚
    pass
