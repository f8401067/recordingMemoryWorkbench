"""修正历史纪要的 date_from/date_to（UTC 日期 → 本地日期）。

背景：旧的 local_date() 对带 Z 的时间戳没有 astimezone()，
凌晨 0-8 点的录音会被算成前一天，录音级纪要的日期也跟着错一天。

用法：
    cd server-py && .venv/bin/python ../scripts/fix_summary_dates.py            # 干跑
    cd server-py && .venv/bin/python ../scripts/fix_summary_dates.py --apply    # 落库
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.getcwd())  # 需在 server-py 目录下运行

from app.db import all_, execute
from app.services.summarize import local_date

APPLY = "--apply" in sys.argv

rows = all_(
    """SELECT s.id, s.date_from, s.date_to, r.started_at
       FROM summaries s JOIN recordings r ON r.id = s.recording_id
       WHERE s.scope = 'recording'"""
)

fixed = 0
for r in rows:
    want = local_date(r["started_at"])
    if want == r["date_from"] and want == r["date_to"]:
        continue
    fixed += 1
    print(f"{r['id']}: {r['date_from']} -> {want}")
    if APPLY:
        execute(
            "UPDATE summaries SET date_from = ?, date_to = ? WHERE id = ?",
            (want, want, r["id"]),
        )

print(f"{'已修正' if APPLY else '待修正'} {fixed} 条（模式：{'apply' if APPLY else 'dry-run'}）")
