"""用户自定义 MCP 服务器

三张表：

- ``mcp_servers``  每个用户自己的 MCP 服务（只支持远程 Streamable HTTP / SSE）
- ``mcp_tools``    连通后缓存的工具清单，避免每次对话都去握手列工具
- ``mcp_call_logs`` 调试与对话调用的结果留档，方便回看「发出去的内容长什么样」

Revision ID: 0010_mcp_servers
Revises: 0009_action_items
Create Date: 2026-11-05
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0010_mcp_servers"
down_revision = "0009_action_items"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)

    if not insp.has_table("mcp_servers"):
        op.execute(
            """CREATE TABLE IF NOT EXISTS mcp_servers (
                 id                TEXT PRIMARY KEY,
                 user_id           TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                 name              TEXT NOT NULL,
                 url               TEXT NOT NULL,
                 transport         TEXT NOT NULL DEFAULT 'auto',
                 headers           TEXT,
                 enabled           INTEGER NOT NULL DEFAULT 1,
                 use_in_chat       INTEGER NOT NULL DEFAULT 1,
                 push_mode         TEXT NOT NULL DEFAULT 'manual',
                 push_tool         TEXT,
                 push_args         TEXT,
                 last_status       TEXT,
                 last_error        TEXT,
                 last_connected_at TEXT,
                 created_at        TEXT NOT NULL,
                 updated_at        TEXT
               )"""
        )

    if not insp.has_table("mcp_tools"):
        op.execute(
            """CREATE TABLE IF NOT EXISTS mcp_tools (
                 id           TEXT PRIMARY KEY,
                 server_id    TEXT NOT NULL REFERENCES mcp_servers(id) ON DELETE CASCADE,
                 user_id      TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                 name         TEXT NOT NULL,
                 title        TEXT,
                 description  TEXT,
                 input_schema TEXT,
                 updated_at   TEXT NOT NULL
               )"""
        )
        op.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_mcp_tools_name "
            "ON mcp_tools(server_id, name)"
        )

    if not insp.has_table("mcp_call_logs"):
        op.execute(
            """CREATE TABLE IF NOT EXISTS mcp_call_logs (
                 id          TEXT PRIMARY KEY,
                 user_id     TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                 server_id   TEXT,
                 tool        TEXT,
                 args        TEXT,
                 ok          INTEGER NOT NULL DEFAULT 0,
                 result      TEXT,
                 error       TEXT,
                 duration_ms INTEGER NOT NULL DEFAULT 0,
                 source      TEXT NOT NULL DEFAULT 'debug',
                 created_at  TEXT NOT NULL
               )"""
        )

    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_mcp_servers_user "
        "ON mcp_servers(user_id, created_at ASC)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_mcp_tools_user "
        "ON mcp_tools(user_id, server_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_mcp_logs_user "
        "ON mcp_call_logs(user_id, created_at DESC)"
    )


def downgrade() -> None:
    # 配置属于用户数据，不做破坏性回滚
    pass
