"""recordings 说话人分离结果标记

记录这条录音是否已经跑过说话人分离，以及分出了几个人。
``diarized_at`` 为空表示还没跑过；重新转写某个分片时会被清空，等下一次收尾重跑。

Revision ID: 0007_recording_diarized
Revises: 0006_device_last_rec_end
Create Date: 2026-11-02
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0007_recording_diarized"
down_revision = "0006_device_last_rec_end"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if not insp.has_table("recordings"):
        return

    cols = {c["name"] for c in insp.get_columns("recordings")}
    if "diarized_at" not in cols:
        op.execute("ALTER TABLE recordings ADD COLUMN diarized_at TEXT")
    if "speaker_count" not in cols:
        op.execute("ALTER TABLE recordings ADD COLUMN speaker_count INTEGER")


def downgrade() -> None:
    pass
