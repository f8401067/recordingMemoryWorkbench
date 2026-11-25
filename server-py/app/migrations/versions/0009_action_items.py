"""行动页正式待办表

「猜你想做」是 AI 从纪要里抽出来的候选，只有用户点了 + 之后才落成真正的待办。
落库后才有完成状态（``status = pending | done``）。

``origin_key`` 是去重键（正文去掉空白/标点后的小写形式），同一个人同一句话只会
有一条正式待办；纪要重跑导致索引变化也不会重复添加。

Revision ID: 0009_action_items
Revises: 0008_speaker_profiles
Create Date: 2026-11-04
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0009_action_items"
down_revision = "0008_speaker_profiles"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)

    if not insp.has_table("action_items"):
        op.execute(
            """CREATE TABLE IF NOT EXISTS action_items (
                 id           TEXT PRIMARY KEY,
                 user_id      TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                 origin_key   TEXT,
                 text         TEXT NOT NULL,
                 tag          TEXT,
                 owner        TEXT,
                 due          TEXT,
                 date         TEXT,
                 summary_id   TEXT REFERENCES summaries(id) ON DELETE SET NULL,
                 status       TEXT NOT NULL DEFAULT 'pending',
                 completed_at TEXT,
                 created_at   TEXT NOT NULL
               )"""
        )

    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_action_user_created "
        "ON action_items(user_id, created_at DESC)"
    )
    # 不用部分索引：PostgreSQL 里 NULL 互不冲突，而 ON CONFLICT 推断部分索引需要带谓词
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_action_origin "
        "ON action_items(user_id, origin_key)"
    )


def downgrade() -> None:
    # 不提供破坏性回滚：用户确认过的待办比表结构值钱
    pass
