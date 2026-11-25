"""相邻录音合并 —— 前后相隔不超过 5 分钟的记录合成一条。

设备/网页录音常因为一次短暂静默被拆成好几条，回看起来很碎。这里做**真合并**：

- 保留最早那条作为主记录，把后面那条的分片整段搬过去，时间轴首尾相接；
- **音频文件本身不动**，仍是各自独立的一小段，详情页照旧分片播放；
- 合并后重新 finalize（重算时长 / 内容判定），旧纪要随 recording_id 级联删除，
  再由自动纪要为这一组合成生成一份，而不是每段一份。
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..bus import emit_recording
from ..config import UPLOADS_DIR
from ..db import all_, execute, now_iso, one

MERGE_WINDOW_MS = 5 * 60 * 1000


def _ms(iso: str | None) -> int:
    """ISO 时间戳 → 毫秒；空值返回 0。"""
    if not iso:
        return 0
    s = str(iso).replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return 0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def _end_ms(rec: dict) -> int:
    """录音最后的声音落在哪个时刻：有 ended_at 优先，否则开始时间 + 时长。"""
    ended = _ms(rec.get("ended_at"))
    if ended:
        return ended
    return _ms(rec.get("started_at")) + int(rec.get("duration_ms") or 0)


def gap_ms(older: dict, newer: dict) -> int:
    """两条之间的静默时长（毫秒），负数是时间重叠。"""
    return _ms(newer.get("started_at")) - _end_ms(older)


def find_merge_partner(recording_id: str, window_ms: int = MERGE_WINDOW_MS) -> tuple[str, str] | None:
    """找出和这条该合并的邻居，返回 (保留的那条, 被吸收的那条)。

    条件：同一用户、都已经是终态（ready/failed）、中间隔着不到 window_ms。
    """
    cur = one("SELECT * FROM recordings WHERE id = ?", (recording_id,))
    if not cur:
        return None

    rows = all_(
        """SELECT * FROM recordings
           WHERE user_id = ? AND status IN ('ready', 'failed') AND id != ?
           ORDER BY started_at ASC""",
        (cur["user_id"], recording_id),
    )
    rows = list(rows or [])
    rows.append(cur)
    rows.sort(key=lambda r: (_ms(r["started_at"]), str(r["id"])))

    idx = next((i for i, r in enumerate(rows) if r["id"] == recording_id), -1)
    if idx < 0:
        return None

    for cand in (idx - 1, idx + 1):
        if cand < 0 or cand >= len(rows):
            continue
        pair = sorted([rows[idx], rows[cand]], key=_end_ms)
        older, newer = pair[0], pair[1]
        if gap_ms(older, newer) <= window_ms:
            return str(older["id"]), str(newer["id"])
    return None


def _move_chunks(keep_id: str, drop_id: str) -> int:
    """把 drop 的分片整段搬到 keep 名下：seq 顺延、start_offset_ms 相接、句级时间戳整体平移。"""
    keep_chunks = all_("SELECT * FROM chunks WHERE recording_id = ? ORDER BY seq ASC", (keep_id,)) or []
    drop_chunks = all_("SELECT * FROM chunks WHERE recording_id = ? ORDER BY seq ASC", (drop_id,)) or []

    base = 0
    for c in keep_chunks:
        base = max(base, int(c.get("start_offset_ms") or 0) + int(c.get("duration_ms") or 0))

    count = len(keep_chunks)
    keep_dir = UPLOADS_DIR / keep_id
    keep_dir.mkdir(parents=True, exist_ok=True)

    moved = 0
    for i, c in enumerate(drop_chunks):
        old_off = int(c.get("start_offset_ms") or 0)
        new_off = base + old_off
        delta = new_off - old_off
        new_seq = count + i

        src = Path(c["path"])
        ext = src.suffix or ".webm"
        dst = keep_dir / f"m{new_seq:05d}{ext}"
        if src.is_file():
            try:
                shutil.move(str(src), str(dst))
            except OSError:
                dst = src  # 搬不动就先保持原路径，至少别断链

        segments: list[dict[str, Any]] = []
        try:
            arr = json.loads(c.get("segments") or "[]")
        except (TypeError, ValueError):
            arr = []
        if isinstance(arr, list):
            for s in arr:
                if not isinstance(s, dict):
                    continue
                line = dict(s)
                start = int(line.get("startMs") or 0)
                end = int(line.get("endMs") or start)
                line["startMs"] = start + delta
                line["endMs"] = end + delta
                segments.append(line)

        execute(
            """UPDATE chunks SET recording_id = ?, seq = ?, filename = ?, path = ?,
               start_offset_ms = ?, segments = ?, updated_at = ?
               WHERE id = ?""",
            (
                keep_id,
                new_seq,
                dst.name,
                str(dst),
                new_off,
                json.dumps(segments, ensure_ascii=False),
                now_iso(),
                c["id"],
            ),
        )
        moved += 1

    return moved


def merge_recordings(keep_id: str, drop_id: str) -> str:
    """把 drop 合并进 keep，返回保留下来的 recording id。"""
    drop = one("SELECT * FROM recordings WHERE id = ?", (drop_id,))
    if not drop:
        return keep_id

    _move_chunks(keep_id, drop_id)

    # 旧的纪要只覆盖合并前的一部分内容，删掉让合并后的整组重新生成一份
    execute("DELETE FROM summaries WHERE recording_id = ?", (keep_id,))

    keep = one("SELECT * FROM recordings WHERE id = ?", (keep_id,))
    keep_end = _end_ms(keep or {})
    drop_end = _end_ms(drop)
    ended_at = drop.get("ended_at") if drop_end > keep_end else (keep or {}).get("ended_at")

    execute(
        """UPDATE recordings
           SET ended_at = ?, merged_count = COALESCE(merged_count, 0) + COALESCE(?, 0) + 1,
               summary_status = 'none'
           WHERE id = ?""",
        (ended_at or now_iso(), int(drop.get("merged_count") or 0), keep_id),
    )

    # 剩下的零碎文件（源音频、临时目录）也跟着搬过去
    src_dir = UPLOADS_DIR / drop_id
    if src_dir.is_dir():
        dst_dir = UPLOADS_DIR / keep_id
        dst_dir.mkdir(parents=True, exist_ok=True)
        for leftover in src_dir.iterdir():
            target = dst_dir / leftover.name
            if target.exists():
                target = dst_dir / f"merged-{drop_id}-{leftover.name}"
            try:
                shutil.move(str(leftover), str(target))
            except (OSError, ValueError):
                continue
        shutil.rmtree(src_dir, ignore_errors=True)

    # summaries / chunks 由外键级联清理（chunks 已经改了主，不会连带删）
    execute("DELETE FROM recordings WHERE id = ?", (drop_id,))
    emit_recording(drop_id, "merged", {"id": keep_id, "mergedFrom": drop_id})
    return keep_id


def reconcile_after_finalize(recording_id: str, depth: int = 3) -> str:
    """转写完成后判断是否要和邻居合并，必要时链式合并，返回最终那条 id。"""
    current = recording_id
    while depth > 0:
        partner = find_merge_partner(current)
        if not partner:
            break
        keep_id, drop_id = partner
        from .transcribe import finalize_recording  # 延迟导入：transcribe 也会用到这里
        merge_recordings(keep_id, drop_id)
        finalize_recording(keep_id, allow_merge=False)
        current = keep_id
        depth -= 1
    return current
