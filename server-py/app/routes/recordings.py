from __future__ import annotations

import asyncio
import json
import mimetypes
import queue as queue_mod
import shutil
import time
import urllib.parse
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response, StreamingResponse

from ..audio import probe_duration_ms, split_audio
from ..bus import bus, emit_recording
from ..config import MAX_CHUNK_BYTES, UPLOADS_DIR, uid
from ..db import all_, execute, now_iso, one
from ..jobs import drain, enqueue
from ..security import current_user, media_user
from ..services.summarize import create_summary, local_date, public_summary
from ..services.transcribe import finalize_recording, fmt_clock, parse_segments, transcript_for

router = APIRouter(prefix="/api/recordings", tags=["recordings"])


def _local_clock() -> str:
    from datetime import datetime

    return datetime.now().strftime("%H:%M")


def _squash(text: str) -> str:
    return " ".join(str(text or "").split())


def _excerpt(text: str, limit: int = 160) -> str:
    """转写正文摘要：列表里用来替代"约 xx 千字"这种没有信息量的描述。"""
    clean = _squash(text)
    if len(clean) <= limit:
        return clean
    return clean[:limit].rstrip("，。,.、；; ") + "…"


def _transcript_text(recording_id: str, limit: int = 4000) -> str:
    rows = all_(
        """SELECT transcript FROM chunks
           WHERE recording_id = ? AND COALESCE(transcript, '') <> ''
           ORDER BY seq ASC""",
        (recording_id,),
    )
    buf: list[str] = []
    size = 0
    for r in rows:
        piece = str(r.get("transcript") or "").strip()
        if not piece:
            continue
        buf.append(piece)
        size += len(piece)
        if size >= limit:
            break
    return "\n".join(buf)[:limit]


def _source_audio(recording_id: str) -> Path | None:
    """上传整段音频时保留的原始文件（source.xxx）；网页录音只有分片。"""
    folder = UPLOADS_DIR / recording_id
    if not folder.is_dir():
        return None
    for p in sorted(folder.glob("source.*")):
        try:
            if p.is_file() and p.stat().st_size > 0:
                return p
        except OSError:
            continue
    return None


_AUDIO_MIME = {
    ".m4a": "audio/mp4",
    ".mp4": "audio/mp4",
    ".aac": "audio/aac",
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".webm": "audio/webm",
    ".ogg": "audio/ogg",
    ".opus": "audio/ogg",
    ".flac": "audio/flac",
    ".amr": "audio/amr",
    ".3gp": "audio/3gpp",
}


def _guess_mime(path: Path | None, fallback: str = "audio/mpeg") -> str:
    """浏览器对 m4a 的 MIME 很挑，mimetypes 给的 audio/mp4a-latm 播不了，这里自己兜一层。"""
    if path is None:
        return fallback
    ext = path.suffix.lower()
    if ext in _AUDIO_MIME:
        return _AUDIO_MIME[ext]
    guess, _ = mimetypes.guess_type(path.name)
    return guess or fallback


def recording_card(r: dict, excerpt: str | None = None) -> dict:
    agg = one(
        """SELECT COUNT(*) AS n,
                  SUM(CASE WHEN status = 'done' THEN 1 ELSE 0 END) AS done,
                  SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed,
                  SUM(LENGTH(COALESCE(transcript, ''))) AS chars
           FROM chunks WHERE recording_id = ?""",
        (r["id"],),
    ) or {}

    summary = one(
        """SELECT id, status, title FROM summaries WHERE recording_id = ?
           ORDER BY created_at DESC LIMIT 1""",
        (r["id"],),
    )

    if excerpt is None:
        excerpt = _excerpt(_transcript_text(r["id"]))

    return {
        "id": r["id"],
        "title": r["title"],
        "status": r["status"],
        "source": r["source"],
        "language": r["language"],
        "startedAt": r["started_at"],
        "endedAt": r.get("ended_at"),
        "date": local_date(r["started_at"]),
        "durationMs": r.get("duration_ms") or 0,
        "chunkCount": agg.get("n") or 0,
        "doneChunks": agg.get("done") or 0,
        "failedChunks": agg.get("failed") or 0,
        "charCount": agg.get("chars") or 0,
        "excerpt": excerpt,
        "summaryStatus": r["summary_status"],
        "summaryId": summary["id"] if summary else None,
        "summaryTitle": summary["title"] if summary else None,
        "memo": r.get("memo"),
    }


def _get_recording(recording_id: str, user_id: str) -> dict:
    rec = one(
        "SELECT * FROM recordings WHERE id = ? AND user_id = ?",
        (recording_id, user_id),
    )
    if not rec:
        raise HTTPException(404, "录音不存在")
    return rec


def _day_bounds(date: str) -> tuple[str, str]:
    """本地日期 → UTC 时间边界（与 started_at 的存储格式保持一致）。"""
    from datetime import datetime, timezone

    try:
        start = datetime.fromisoformat(f"{date}T00:00:00").astimezone()
        end = datetime.fromisoformat(f"{date}T23:59:59.999").astimezone()
    except ValueError as e:
        raise HTTPException(400, "日期格式应为 YYYY-MM-DD") from e

    def z(dt: datetime) -> str:
        u = dt.astimezone(timezone.utc)
        return u.strftime("%Y-%m-%dT%H:%M:%S.") + f"{u.microsecond // 1000:03d}Z"

    return z(start), z(end)


def _store_upload(recording_id: str, seq: int, filename: str | None, data: bytes) -> dict:
    folder = UPLOADS_DIR / recording_id
    folder.mkdir(parents=True, exist_ok=True)
    ext = Path(filename or "").suffix or ".webm"
    name = f"{seq:05d}{ext}"
    full = folder / name
    full.write_bytes(data)
    return {"filename": name, "path": str(full), "size": len(data)}


# ---------------- 列表 ----------------
@router.get("")
def list_recordings(
    date: str | None = None,
    q: str | None = None,
    status: str | None = None,
    limit: int = 50,
    user: dict = Depends(current_user),
) -> list[dict]:
    where = ["user_id = ?"]
    params: list[Any] = [user["id"]]

    if date:
        frm, to = _day_bounds(date)
        where.append("started_at >= ? AND started_at <= ?")
        params.extend([frm, to])
    if q:
        where.append(
            "(title LIKE ? OR id IN (SELECT recording_id FROM chunks WHERE transcript LIKE ?))"
        )
        params.extend([f"%{q}%", f"%{q}%"])
    if status:
        where.append("status = ?")
        params.append(status)

    lim = max(1, min(200, limit or 50))
    rows = all_(
        f"SELECT * FROM recordings WHERE {' AND '.join(where)} ORDER BY started_at DESC LIMIT ?",
        (*params, lim),
    )
    if not rows:
        return []

    # 一次把这一页的转写都取出来做摘要，避免每行再查一遍
    ids = [r["id"] for r in rows]
    marks = ", ".join("?" for _ in ids)
    pieces = all_(
        f"""SELECT recording_id, transcript FROM chunks
            WHERE recording_id IN ({marks}) AND COALESCE(transcript, '') <> ''
            ORDER BY recording_id, seq ASC""",
        tuple(ids),
    )
    bucket: dict[str, list[str]] = {}
    for p in pieces:
        bucket.setdefault(str(p["recording_id"]), []).append(str(p.get("transcript") or ""))

    return [recording_card(r, excerpt=_excerpt("\n".join(bucket.get(r["id"], [])))) for r in rows]


# ---------------- 创建 ----------------
@router.post("", status_code=201)
def create_recording(
    body: dict[str, Any] = Body(default_factory=dict),
    user: dict = Depends(current_user),
) -> dict:
    clock = _local_clock()
    rid = uid("rec_")
    title = str(body.get("title") or "").strip() or f"录音 {clock}"
    execute(
        """INSERT INTO recordings
           (id, user_id, title, status, source, language, started_at, asr_provider_id, created_at)
           VALUES (?, ?, ?, 'recording', ?, ?, ?, ?, ?)""",
        (
            rid,
            user["id"],
            title[:100],
            body.get("source") or "web",
            body.get("language") or "zh",
            body.get("startedAt") or now_iso(),
            body.get("asrProviderId"),
            now_iso(),
        ),
    )
    rec = one("SELECT * FROM recordings WHERE id = ?", (rid,))
    assert rec is not None
    return recording_card(rec)


# ---------------- 详情 ----------------
@router.get("/{recording_id}")
def get_recording(recording_id: str, user: dict = Depends(current_user)) -> dict:
    rec = _get_recording(recording_id, user["id"])
    chunks = all_("SELECT * FROM chunks WHERE recording_id = ? ORDER BY seq ASC", (recording_id,))
    t = transcript_for(recording_id)
    summary = one(
        """SELECT * FROM summaries WHERE recording_id = ? ORDER BY created_at DESC LIMIT 1""",
        (recording_id,),
    )

    # 有原始上传文件就整段播；网页/设备录音只有分片，前端按 startOffsetMs 顺序拼着播
    source = _source_audio(recording_id)
    audio = {
        "mode": "source" if source else ("chunks" if chunks else "none"),
        "url": f"/api/recordings/{recording_id}/audio/source" if source else None,
        "mime": _guess_mime(source) if source else None,
        "size": source.stat().st_size if source else 0,
        "filename": f"{rec['title']}{source.suffix}" if source else None,
        "durationMs": rec.get("duration_ms") or 0,
    }

    return {
        **recording_card(rec),
        "memo": rec.get("memo"),
        "audio": audio,
        "chunks": [
            {
                "id": c["id"],
                "seq": c["seq"],
                "status": c["status"],
                "durationMs": c.get("duration_ms") or 0,
                "startOffsetMs": c.get("start_offset_ms") or 0,
                "size": c.get("size") or 0,
                "error": c.get("error"),
                "text": c.get("transcript") or "",
                "segmentCount": len(parse_segments(c)),
            }
            for c in chunks
        ],
        "transcript": {
            "text": t["text"],
            "segments": t["segments"],
            "chunkCount": t["chunkCount"],
            "doneChunks": t["doneChunks"],
            "failedChunks": t["failedChunks"],
        },
        "summary": public_summary(summary) if summary else None,
    }


@router.patch("/{recording_id}")
def update_recording(
    recording_id: str,
    body: dict[str, Any] = Body(default_factory=dict),
    user: dict = Depends(current_user),
) -> dict:
    _get_recording(recording_id, user["id"])
    if body.get("title") is not None:
        execute(
            "UPDATE recordings SET title = ? WHERE id = ?",
            (str(body["title"])[:100], recording_id),
        )
    if body.get("memo") is not None:
        execute(
            "UPDATE recordings SET memo = ? WHERE id = ?",
            (str(body["memo"])[:2000], recording_id),
        )
    rec = one("SELECT * FROM recordings WHERE id = ?", (recording_id,))
    assert rec is not None
    return recording_card(rec)


@router.delete("/{recording_id}")
def delete_recording(recording_id: str, user: dict = Depends(current_user)) -> dict:
    rec = one(
        "SELECT * FROM recordings WHERE id = ? AND user_id = ?",
        (recording_id, user["id"]),
    )
    if rec:
        execute("DELETE FROM recordings WHERE id = ?", (recording_id,))
        shutil.rmtree(UPLOADS_DIR / recording_id, ignore_errors=True)
    return {"ok": True}


# ---------------- 上传分片 ----------------
@router.post("/{recording_id}/chunks", status_code=201)
async def upload_chunk(
    recording_id: str,
    request: Request,
    seq: int = Query(0),
    duration_ms: int = Query(0, alias="durationMs"),
    start_offset_ms: int | None = Query(None, alias="startOffsetMs"),
    user: dict = Depends(current_user),
) -> dict:
    _get_recording(recording_id, user["id"])

    form = await request.form()
    upload = form.get("file")
    if upload is None or not hasattr(upload, "read"):
        raise HTTPException(400, "缺少音频文件")
    data = await upload.read()
    if len(data) > MAX_CHUNK_BYTES:
        raise HTTPException(413, "单个分片超过大小限制")

    if start_offset_ms is None:
        agg = one(
            "SELECT COALESCE(SUM(duration_ms),0) AS s FROM chunks WHERE recording_id = ?",
            (recording_id,),
        ) or {"s": 0}
        offset = int(agg["s"] or 0)
    else:
        offset = int(start_offset_ms)

    saved = _store_upload(recording_id, seq, getattr(upload, "filename", None), data)
    real_duration = int(duration_ms) or probe_duration_ms(saved["path"]) or 0
    mime = getattr(upload, "content_type", None) or "audio/webm"
    t = now_iso()

    existing = one(
        "SELECT * FROM chunks WHERE recording_id = ? AND seq = ?",
        (recording_id, seq),
    )
    if existing:
        chunk_id = existing["id"]
        execute(
            """UPDATE chunks SET filename = ?, path = ?, mime = ?, size = ?, duration_ms = ?,
               start_offset_ms = ?, status = 'queued', error = NULL, transcript = NULL,
               segments = NULL, updated_at = ? WHERE id = ?""",
            (
                saved["filename"],
                saved["path"],
                mime,
                saved["size"],
                real_duration,
                offset,
                t,
                chunk_id,
            ),
        )
    else:
        chunk_id = uid("chk_")
        execute(
            """INSERT INTO chunks
               (id, recording_id, seq, filename, path, mime, size, duration_ms,
                start_offset_ms, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?)""",
            (
                chunk_id,
                recording_id,
                seq,
                saved["filename"],
                saved["path"],
                mime,
                saved["size"],
                real_duration,
                offset,
                t,
                t,
            ),
        )

    total = one(
        """SELECT COALESCE(MAX(start_offset_ms + duration_ms),0) AS s, COUNT(*) AS n
           FROM chunks WHERE recording_id = ?""",
        (recording_id,),
    ) or {"s": 0, "n": 0}
    execute(
        "UPDATE recordings SET status = 'processing', duration_ms = ? WHERE id = ?",
        (int(total["s"] or 0), recording_id),
    )

    enqueue("transcribe", {"chunkId": chunk_id}, max_attempts=3)
    emit_recording(recording_id, "chunk", {"chunkId": chunk_id, "seq": seq, "status": "queued"})

    return {
        "chunkId": chunk_id,
        "seq": seq,
        "status": "queued",
        "durationMs": real_duration,
        "startOffsetMs": offset,
        "totalDurationMs": int(total["s"] or 0),
        "chunkCount": int(total["n"] or 1),
    }


# ---------------- 上传整段音频（后端自动切片） ----------------
@router.post("/{recording_id}/upload-file", status_code=201)
async def upload_file(
    recording_id: str,
    request: Request,
    segment_sec: int = Query(60, alias="segmentSec"),
    user: dict = Depends(current_user),
) -> dict:
    _get_recording(recording_id, user["id"])

    form = await request.form()
    upload = form.get("file")
    if upload is None or not hasattr(upload, "read"):
        raise HTTPException(400, "缺少音频文件")

    seg = max(30, min(300, int(segment_sec or 60)))
    folder = UPLOADS_DIR / recording_id
    folder.mkdir(parents=True, exist_ok=True)
    ext = Path(getattr(upload, "filename", "") or "").suffix or ".webm"
    source = folder / f"source{ext}"
    source.write_bytes(await upload.read())

    total_ms = probe_duration_ms(source)
    if not total_ms:
        raise HTTPException(400, "无法解析该音频文件，请确认格式是否受支持")

    parts = split_audio(source, folder / "parts", seg)
    if not parts:
        raise HTTPException(400, "音频切片失败")

    offset = 0
    for seq, part in enumerate(parts):
        chunk_id = uid("chk_")
        t = now_iso()
        part_path = Path(part["file"])
        execute(
            """INSERT INTO chunks
               (id, recording_id, seq, filename, path, mime, size, duration_ms,
                start_offset_ms, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?)
               ON CONFLICT(recording_id, seq) DO UPDATE SET
                 path = excluded.path, duration_ms = excluded.duration_ms,
                 start_offset_ms = excluded.start_offset_ms, status = 'queued',
                 transcript = NULL, segments = NULL, updated_at = excluded.updated_at""",
            (
                chunk_id,
                recording_id,
                seq,
                part_path.name,
                part["file"],
                _guess_mime(part_path),
                part_path.stat().st_size,
                part["durationMs"],
                offset,
                t,
                t,
            ),
        )
        enqueue("transcribe", {"chunkId": chunk_id}, max_attempts=3)
        offset += part["durationMs"]

    execute(
        """UPDATE recordings SET status = 'processing', duration_ms = ?,
           ended_at = COALESCE(ended_at, ?), started_at = COALESCE(started_at, ?) WHERE id = ?""",
        (total_ms, now_iso(), now_iso(), recording_id),
    )

    return {"chunks": len(parts), "durationMs": total_ms, "segmentSec": seg}


# ---------------- 结束录音 ----------------
@router.post("/{recording_id}/finish")
def finish_recording(
    recording_id: str,
    body: dict[str, Any] = Body(default_factory=dict),
    user: dict = Depends(current_user),
) -> dict:
    _get_recording(recording_id, user["id"])

    duration = int(body.get("durationMs") or 0)
    cnt = one(
        "SELECT COUNT(*) AS n FROM chunks WHERE recording_id = ?",
        (recording_id,),
    ) or {"n": 0}
    if not cnt["n"]:
        raise HTTPException(400, "还没有上传任何音频分片")

    execute(
        """UPDATE recordings SET ended_at = ?, status = 'processing',
           duration_ms = CASE WHEN ? > 0 THEN ? ELSE duration_ms END WHERE id = ?""",
        (now_iso(), duration, duration, recording_id),
    )
    finalize_recording(recording_id)
    drain()
    rec = one("SELECT * FROM recordings WHERE id = ?", (recording_id,))
    assert rec is not None
    return recording_card(rec)


# ---------------- 重试 ----------------
@router.post("/{recording_id}/retry")
def retry_recording(recording_id: str, user: dict = Depends(current_user)) -> dict:
    _get_recording(recording_id, user["id"])
    failed = all_(
        """SELECT * FROM chunks WHERE recording_id = ?
           AND status IN ('failed','pending','queued')""",
        (recording_id,),
    )
    if not failed:
        # 没有需要重试的分片就不要把录音改成 processing，否则会一直卡在“转写中”
        return {"requeued": 0}

    for c in failed:
        execute(
            "UPDATE chunks SET status = 'queued', error = NULL, updated_at = ? WHERE id = ?",
            (now_iso(), c["id"]),
        )
        enqueue("transcribe", {"chunkId": c["id"]}, max_attempts=3)
    execute("UPDATE recordings SET status = 'processing' WHERE id = ?", (recording_id,))
    drain()
    return {"requeued": len(failed)}


# ---------------- 手动触发纪要 ----------------
@router.post("/{recording_id}/summarize")
def summarize_recording(
    recording_id: str,
    body: dict[str, Any] = Body(default_factory=dict),
    user: dict = Depends(current_user),
) -> dict:
    _get_recording(recording_id, user["id"])
    try:
        s = create_summary(
            userId=user["id"],
            scope="recording",
            recordingId=recording_id,
            providerId=body.get("providerId"),
            template=body.get("template"),
            force=body.get("force", True),
        )
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return public_summary(s)


# ---------------- 实时事件（SSE） ----------------
@router.get("/{recording_id}/stream")
async def stream_recording(recording_id: str, user: dict = Depends(current_user)):
    q = bus.subscribe(f"rec:{recording_id}")

    async def gen():
        try:
            yield "event: ping\ndata: {}\n\n"
            last_ping = time.monotonic()
            while True:
                got = False
                while True:
                    try:
                        msg = q.get_nowait()
                    except queue_mod.Empty:
                        break
                    got = True
                    data = json.dumps(msg["data"], ensure_ascii=False)
                    yield f"event: {msg['event']}\ndata: {data}\n\n"
                if time.monotonic() - last_ping > 20:
                    last_ping = time.monotonic()
                    yield "event: ping\ndata: {}\n\n"
                if not got:
                    await asyncio.sleep(0.25)
        finally:
            bus.unsubscribe(f"rec:{recording_id}", q)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ---------------- 导出 ----------------
@router.get("/{recording_id}/export")
def export_recording(
    recording_id: str,
    format: str = Query("md"),
    user: dict = Depends(current_user),
) -> Response:
    rec = _get_recording(recording_id, user["id"])
    t = transcript_for(recording_id)
    summary = one(
        """SELECT * FROM summaries WHERE recording_id = ? ORDER BY created_at DESC LIMIT 1""",
        (recording_id,),
    )
    filename = urllib.parse.quote(rec["title"])

    if format == "txt":
        body = "\n".join(
            f"[{fmt_clock(s.get('startMs') or 0)}] {s.get('text') or ''}" for s in t["segments"]
        )
        return Response(
            content=body,
            media_type="text/plain; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{filename}.txt"'},
        )

    md = "\n".join(
        [
            f"# {rec['title']}",
            "",
            f"> {local_date(rec['started_at'])} · 时长 {fmt_clock(rec.get('duration_ms') or 0)} · "
            f"{t['chunkCount']} 个片段",
            "",
            (summary or {}).get("markdown") or "",
            "",
            "---",
            "",
            "## 完整转写",
            "",
            "\n\n".join(
                f"**{fmt_clock(s.get('startMs') or 0)}** {s.get('text') or ''}"
                for s in t["segments"]
            ),
        ]
    )
    return Response(
        content=md,
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}.md"'},
    )


# ---------------- 音频回放 ----------------
@router.get("/{recording_id}/audio/source")
def get_source_audio(
    recording_id: str,
    download: bool = Query(False),
    user: dict = Depends(media_user),
):
    """原始音频文件（上传整段音频时保留的那份），支持 Range 因此可以边下边播。"""
    rec = _get_recording(recording_id, user["id"])
    path = _source_audio(recording_id)
    if path is None:
        raise HTTPException(404, "该录音没有原始音频文件")
    headers = {"Cache-Control": "private, max-age=3600"}
    if download:
        return FileResponse(
            path,
            media_type=_guess_mime(path),
            filename=f"{rec['title']}{path.suffix}",
            headers=headers,
        )
    return FileResponse(path, media_type=_guess_mime(path), headers=headers)


@router.get("/{recording_id}/audio/{seq}")
def get_audio(
    recording_id: str,
    seq: int,
    download: bool = Query(False),
    user: dict = Depends(media_user),
):
    rec = _get_recording(recording_id, user["id"])
    chunk = one(
        "SELECT * FROM chunks WHERE recording_id = ? AND seq = ?",
        (recording_id, seq),
    )
    if not chunk or not Path(chunk["path"]).is_file():
        raise HTTPException(404, "音频不存在")
    path = Path(chunk["path"])
    headers = {"Cache-Control": "private, max-age=3600"}
    mime = _guess_mime(path, chunk.get("mime") or "audio/webm")
    if download:
        suffix = path.suffix or ".webm"
        return FileResponse(
            path,
            media_type=mime,
            filename=f"{rec['title']}-{seq + 1}{suffix}",
            headers=headers,
        )
    return FileResponse(path, media_type=mime, headers=headers)
