"""chunks.chunk_key —— 设备端上传幂等

设备重传同一个分片时（上一次响应丢了、或者超时后又试了一次），
服务端按 (recording_id, chunk_key) 认出是同一段，直接返回原来的 chunk，
不再插一条新的、也不会把音频写两遍。

Revision ID: 0002_chunk_key
Revises: 0001_initial
Create Date: 2026-10-31
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002_chunk_key"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if not insp.has_table("chunks"):
        return

    cols = {c["name"] for c in insp.get_columns("chunks")}
    if "chunk_key" not in cols:
        op.execute("ALTER TABLE chunks ADD COLUMN chunk_key TEXT")

    # 幂等键按设备上传的序号生成，全局唯一性由设备保证；这里只建普通索引
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_chunk_key ON chunks(chunk_key)"
    )


def downgrade() -> None:
    # 不提供破坏性回滚（列留着不影响老代码）
    pass
