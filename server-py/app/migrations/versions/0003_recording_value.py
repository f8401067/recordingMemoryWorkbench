"""recordings 低价值标记 —— 空录音 / 无意义转写

给 recordings 加三个字段：
- `low_value`：0/1，1 = 这段既没有人声也几乎没有有效内容（列表里排最后并默认收起）
- `low_reason`：人话说明（"没有识别到人声" 等），前端直接展示
- `value_checked`：0/1，是否已经评估过。历史数据由启动后的后台补算线程刷成 1，
  转写完成时也会置 1。

Revision ID: 0003_recording_value
Revises: 0002_chunk_key
Create Date: 2026-10-31
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003_recording_value"
down_revision = "0002_chunk_key"
branch_labels = None
depends_on = None

COLUMNS = [
    ("low_value", "ALTER TABLE recordings ADD COLUMN low_value INTEGER NOT NULL DEFAULT 0"),
    ("low_reason", "ALTER TABLE recordings ADD COLUMN low_reason TEXT"),
    ("value_checked", "ALTER TABLE recordings ADD COLUMN value_checked INTEGER NOT NULL DEFAULT 0"),
]


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if not insp.has_table("recordings"):
        return

    cols = {c["name"] for c in insp.get_columns("recordings")}
    for name, ddl in COLUMNS:
        if name not in cols:
            op.execute(ddl)

    op.execute("CREATE INDEX IF NOT EXISTS idx_rec_low ON recordings(low_value)")


def downgrade() -> None:
    # 保留数据，不做破坏性回滚
    pass
