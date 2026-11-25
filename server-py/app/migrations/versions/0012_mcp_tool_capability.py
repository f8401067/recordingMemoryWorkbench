"""缓存「这个工具能不能接收 AI 纪要」的判定结果。

添加 / 刷新 MCP 服务时就分析工具的 inputSchema，把结论写进
``mcp_tools.capability``（JSON）。纪要详情页据此直接渲染一键按钮，
不用每次打开页面都重新连 MCP 服务。

Revision ID: 0012_mcp_tool_capability
Revises: 0011_mcp_push_hint
Create Date: 2026-11-05
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0012_mcp_tool_capability"
down_revision = "0011_mcp_push_hint"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if insp.has_table("mcp_tools"):
        cols = {c["name"] for c in insp.get_columns("mcp_tools")}
        if "capability" not in cols:
            op.execute("ALTER TABLE mcp_tools ADD COLUMN capability TEXT")


def downgrade() -> None:
    pass
