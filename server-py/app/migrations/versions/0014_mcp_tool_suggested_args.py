"""工具的「大模型建议参数」落库。

添加 / 刷新 MCP 服务时，后台让大模型按工具 schema 给出一份参数建议，
写进 ``mcp_tools.suggested_args``（JSON 字符串）。调试面板与纪要一键发送
都拿它做默认值，用户改过之后以 ``suggested_at`` 判断是否回写。

Revision ID: 0014_mcp_tool_suggested_args
Revises: 0013_mcp_service
Create Date: 2026-11-07
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0014_mcp_tool_suggested_args"
down_revision = "0013_mcp_service"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if insp.has_table("mcp_tools"):
        cols = {c["name"] for c in insp.get_columns("mcp_tools")}
        if "suggested_args" not in cols:
            op.execute("ALTER TABLE mcp_tools ADD COLUMN suggested_args TEXT")
        if "suggested_at" not in cols:
            op.execute("ALTER TABLE mcp_tools ADD COLUMN suggested_at TEXT")


def downgrade() -> None:
    pass
