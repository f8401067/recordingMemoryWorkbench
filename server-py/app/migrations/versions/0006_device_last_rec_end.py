"""devices.last_rec_end —— 用录制时间而不是到达时间切会话

设备离线攒了一整天，回家连上 WiFi 后几十个分片几秒钟内全部推完。
只看 `last_chunk_at`（到达时间）的话，间隔永远是 1~2 秒，远小于 gapSec，
一整天会被并成一条记录。加一列记住"上一片录制到什么时候结束"，
设备带了 recordedAt 就按真实录制间隔切，没带再退回到达时间。

Revision ID: 0006_device_last_rec_end
Revises: 0005_job_heartbeat
Create Date: 2026-11-01
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0006_device_last_rec_end"
down_revision = "0005_job_heartbeat"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if not insp.has_table("devices"):
        return

    cols = {c["name"] for c in insp.get_columns("devices")}
    if "last_rec_end" not in cols:
        op.execute("ALTER TABLE devices ADD COLUMN last_rec_end TEXT")


def downgrade() -> None:
    # 只是多一列，留着不影响旧版本
    pass
