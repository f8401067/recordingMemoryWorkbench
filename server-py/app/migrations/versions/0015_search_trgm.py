"""给「找寻」的字面搜索补 pg_trgm 索引。

``search_keyword``（以及首页搜索、``search_memory`` 工具）用的是
``LOWER(col) LIKE LOWER('%q%')``，没有索引时是整表扫描。pg_trgm 的 GIN 索引
支持这种「前后都模糊」的匹配，且索引建在查询里用到的同一个表达式上，业务 SQL
一行都不用改就能走上索引。

注意：trigram 需要至少 3 个字符才能利用索引，一两个字的中文查询仍会退化成扫描；
这是 PostgreSQL 全文/模糊检索的固有限制，不是本迁移引入的问题。

Revision ID: 0015_search_trgm
Revises: 0014_mcp_tool_suggested_args
Create Date: 2026-11-08
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0015_search_trgm"
down_revision = "0014_mcp_tool_suggested_args"
branch_labels = None
depends_on = None


_INDEXES: tuple[tuple[str, str], ...] = (
    (
        "idx_recordings_title_trgm",
        "CREATE INDEX IF NOT EXISTS idx_recordings_title_trgm "
        "ON recordings USING gin (lower(title) gin_trgm_ops)",
    ),
    (
        "idx_recordings_memo_trgm",
        "CREATE INDEX IF NOT EXISTS idx_recordings_memo_trgm "
        "ON recordings USING gin (lower(COALESCE(memo, '')) gin_trgm_ops)",
    ),
    (
        "idx_chunks_transcript_trgm",
        "CREATE INDEX IF NOT EXISTS idx_chunks_transcript_trgm "
        "ON chunks USING gin (lower(COALESCE(transcript, '')) gin_trgm_ops)",
    ),
    (
        "idx_summaries_title_trgm",
        "CREATE INDEX IF NOT EXISTS idx_summaries_title_trgm "
        "ON summaries USING gin (lower(title) gin_trgm_ops)",
    ),
    (
        "idx_summaries_markdown_trgm",
        "CREATE INDEX IF NOT EXISTS idx_summaries_markdown_trgm "
        "ON summaries USING gin (lower(COALESCE(markdown, '')) gin_trgm_ops)",
    ),
)


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        # 迁移只维护 PostgreSQL 一套结构；别的方言直接跳过。
        return

    # 扩展建不起来（权限不够 / 镜像没带 contrib）时，用 savepoint 回滚掉这一段，
    # 只跳过索引优化，不让整个迁移失败、把服务卡在「数据库初始化失败」。
    try:
        with bind.begin_nested():
            op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    except Exception as exc:  # noqa: BLE001 - 索引是优化项，不是功能依赖
        print(f"[migrate] pg_trgm 不可用，跳过模糊搜索索引：{exc}", flush=True)
        return

    insp = sa.inspect(bind)
    if not insp.has_table("recordings"):
        return

    for name, ddl in _INDEXES:
        try:
            with bind.begin_nested():
                op.execute(ddl)
        except Exception as exc:  # noqa: BLE001 - 单个索引失败不影响其它
            print(f"[migrate] 建索引 {name} 失败，跳过：{exc}", flush=True)


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    for name, _ in _INDEXES:
        try:
            with bind.begin_nested():
                op.execute(f"DROP INDEX IF EXISTS {name}")
        except Exception as exc:  # noqa: BLE001 - 回滚尽力而为
            print(f"[migrate] 删索引 {name} 失败，跳过：{exc}", flush=True)
