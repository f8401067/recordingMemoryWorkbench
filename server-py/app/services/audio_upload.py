"""一次性上传整段音频：建录音 → 落盘 → 排队转写。

开放 API 的 ``/api/open/transcribe`` 和对外 MCP 的 ``upload_recording`` 共用这条链路，
保证「传一段音频进来」的效果完全一致（转写完成后按设置自动生成 AI 纪要）。
"""

from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path
from typing import Any

from ..audio import probe_duration_ms
from ..config import UPLOADS_DIR, uid
from ..db import execute, now_iso, one
from ..jobs import drain, enqueue
from .transcribe import transcript_for


def ext_from_mime(mime: str) -> str:
    m = (mime or "").lower()
    if "webm" in m:
        return ".webm"
    if "mp4" in m or "m4a" in m:
        return ".m4a"
    if "mpeg" in m or "mp3" in m:
        return ".mp3"
    if "wav" in m:
        return ".wav"
    if "ogg" in m:
        return ".ogg"
    if "aac" in m:
        return ".aac"
    if "flac" in m:
        return ".flac"
    return ".bin"


def default_title(prefix: str = "外部录音") -> str:
    now = datetime.now()
    return f"{prefix} {now.month}/{now.day} {now:%H:%M}"


def create_transcription_job(
    *,
    user_id: str,
    data: bytes,
    mime: str,
    filename: str = "audio.bin",
    title: str | None = None,
    language: str = "zh",
    started_at: str | None = None,
    source: str = "api",
) -> dict[str, Any]:
    rid = uid("rec_")
    rec_title = (title or "").strip() or default_title()
    t = now_iso()

    execute(
        """INSERT INTO recordings
           (id, user_id, title, status, source, language, started_at, created_at)
           VALUES (?, ?, ?, 'processing', ?, ?, ?, ?)""",
        (rid, user_id, rec_title[:100], source, language or "zh", started_at or t, t),
    )

    folder = UPLOADS_DIR / rid
    folder.mkdir(parents=True, exist_ok=True)
    ext = Path(filename).suffix or ext_from_mime(mime)
    full = folder / f"00000{ext}"
    full.write_bytes(data)

    duration = probe_duration_ms(full) or 0
    chunk_id = uid("chk_")
    execute(
        """INSERT INTO chunks
           (id, recording_id, seq, filename, path, mime, size, duration_ms,
            start_offset_ms, status, created_at, updated_at)
           VALUES (?, ?, 0, ?, ?, ?, ?, ?, 0, 'queued', ?, ?)""",
        (
            chunk_id,
            rid,
            full.name,
            str(full),
            mime,
            len(data),
            duration,
            t,
            t,
        ),
    )
    execute(
        "UPDATE recordings SET duration_ms = ?, ended_at = ? WHERE id = ?",
        (duration, t, rid),
    )
    enqueue("transcribe", {"chunkId": chunk_id}, max_attempts=3)
    drain()

    return {
        "id": rid,
        "chunkId": chunk_id,
        "title": rec_title,
        "status": "processing",
        "durationMs": duration,
        "size": len(data),
    }


def wait_transcription(job: dict[str, Any], timeout_ms: int = 120_000) -> dict[str, Any]:
    """同步 worker 里用的等待循环（在线程池里跑，不占事件循环）。"""
    deadline = time.monotonic() + min(300_000, max(1000, int(timeout_ms or 120_000))) / 1000
    while time.monotonic() < deadline:
        c = one("SELECT status FROM chunks WHERE id = ?", (job["chunkId"],))
        if c and c["status"] in ("done", "failed"):
            break
        time.sleep(0.5)

    fresh = one("SELECT * FROM recordings WHERE id = ?", (job["id"],)) or {}
    result = transcript_for(job["id"])
    return {
        "id": job["id"],
        "title": job["title"],
        "status": fresh.get("status"),
        "durationMs": fresh.get("duration_ms") or 0,
        "text": result["text"],
        "segments": result["segments"],
        "summaryStatus": fresh.get("summary_status"),
    }
