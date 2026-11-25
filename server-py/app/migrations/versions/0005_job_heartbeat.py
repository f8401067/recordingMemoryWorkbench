"""jobs.heartbeat_at —— 用心跳判断任务是不是还活着

以前只靠 `started_at` 判活：不管任务是在跑还是卡死了，只要超过阈值就放回队列，
长任务（一小时录音切成几十片、本地 ASR 慢慢推）会被误判成僵尸。
加一列心跳，worker 每 15 秒刷一次，reaper 只看心跳停没停。

Revision ID: 0005_job_heartbeat
Revises: 0004_merged_count
Create Date: 2026-10-31
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0005_job_heartbeat"
down_revision = "0004_merged_count"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if not insp.has_table("jobs"):
        return

    cols = {c["name"] for c in insp.get_columns("jobs")}
    if "heartbeat_at" not in cols:
        op.execute("ALTER TABLE jobs ADD COLUMN heartbeat_at TEXT")
        # 老数据用 started_at 兜底，免得刚升级就被 reaper 一把扫回去
        op.execute("UPDATE jobs SET heartbeat_at = started_at WHERE heartbeat_at IS NULL")


def downgrade() -> None:
    # 只多一列，留着不影响旧版本
    pass
