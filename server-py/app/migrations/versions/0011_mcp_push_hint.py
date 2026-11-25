"""MCP 推送改成由大模型决策，配置里只留一句自然语言偏好。

``push_tool`` / ``push_args`` 这两列不再由界面写入（保留兼容旧数据），
新增 ``push_hint``：用户可以写「只把含待办的内容发给笔记」这类要求，
调度器把它连同工具清单一起交给模型判断。

Revision ID: 0011_mcp_push_hint
Revises: 0010_mcp_servers
Create Date: 2026-11-05
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0011_mcp_push_hint"
down_revision = "0010_mcp_servers"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if insp.has_table("mcp_servers"):
        cols = {c["name"] for c in insp.get_columns("mcp_servers")}
        if "push_hint" not in cols:
            op.execute("ALTER TABLE mcp_servers ADD COLUMN push_hint TEXT")


def downgrade() -> None:
    pass
