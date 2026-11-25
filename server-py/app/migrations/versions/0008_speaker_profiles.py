"""声纹角色库

把已分离录音里的某个说话人固化成具名角色（「我」是特殊角色），以后每条录音
自动认人，并判断「我」有没有参与这段对话。

recordings 上额外保存：
- speaker_embeddings：本次分离得到的每个说话人声纹质心（JSON），用于事后补标记
- speaker_matches：质心匹配到的角色（JSON）
- user_present：1 我参与了 / 0 我未参与 / NULL 未判定
- user_similarity：与「我」的最高相似度，便于解释判定结果

Revision ID: 0008_speaker_profiles
Revises: 0007_recording_diarized
Create Date: 2026-11-03
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0008_speaker_profiles"
down_revision = "0007_recording_diarized"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)

    if not insp.has_table("speaker_profiles"):
        op.execute(
            """CREATE TABLE IF NOT EXISTS speaker_profiles (
                 id                  TEXT PRIMARY KEY,
                 user_id             TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                 name                TEXT NOT NULL,
                 embedding           TEXT NOT NULL,
                 sample_count        INTEGER NOT NULL DEFAULT 1,
                 is_self             INTEGER NOT NULL DEFAULT 0,
                 source_recording_id TEXT,
                 source_speaker      INTEGER,
                 created_at          TEXT NOT NULL,
                 updated_at          TEXT NOT NULL,
                 UNIQUE(user_id, name)
               )"""
        )
        op.execute(
            "CREATE INDEX IF NOT EXISTS idx_voice_user ON speaker_profiles(user_id, is_self DESC)"
        )

    if not insp.has_table("recordings"):
        return
    cols = {c["name"] for c in insp.get_columns("recordings")}
    additions = [
        ("speaker_embeddings", "ALTER TABLE recordings ADD COLUMN speaker_embeddings TEXT"),
        ("speaker_matches", "ALTER TABLE recordings ADD COLUMN speaker_matches TEXT"),
        ("user_present", "ALTER TABLE recordings ADD COLUMN user_present INTEGER"),
        ("user_similarity", "ALTER TABLE recordings ADD COLUMN user_similarity REAL"),
    ]
    for column, ddl in additions:
        if column not in cols:
            op.execute(ddl)


def downgrade() -> None:
    pass
