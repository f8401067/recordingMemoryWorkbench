"""recordings.merged_count —— 记录这条是被合并出来的几条碎片

`reconcile_after_finalize()` 把前后相隔不到 5 分钟的录音真合并成一条之后，
前端要能在列表里标出「已合并 N 段」，所以留一个计数（合并前的原始条数之和）。

Revision ID: 0004_merged_count
Revises: 0003_recording_value
Create Date: 2026-10-31
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004_merged_count"
down_revision = "0003_recording_value"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if not insp.has_table("recordings"):
        return

    cols = {c["name"] for c in insp.get_columns("recordings")}
    if "merged_count" not in cols:
        op.execute("ALTER TABLE recordings ADD COLUMN merged_count INTEGER NOT NULL DEFAULT 0")


def downgrade() -> None:
    # 保留计数，不做破坏性回滚
    pass
