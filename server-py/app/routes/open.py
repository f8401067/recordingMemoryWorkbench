from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from ..audio import probe_duration_ms
from ..config import UPLOADS_DIR, uid
from ..db import all_, execute, now_iso, one
from ..jobs import drain, enqueue
from ..services.devices import close_session, ingest, poll_pair, public_device
from ..services.transcribe import finalize_recording, transcript_for

router = APIRouter(prefix="/api/open", tags=["open"])


def _local_clock() -> str:
    from datetime import datetime

    return datetime.now().strftime("%H:%M")


def _default_title(prefix: str = "外部录音") -> str:
    from datetime import datetime

    now = datetime.now()
    return f"{prefix} {now.month}/{now.day} {now:%H:%M}"


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
    return ".bin"


def open_user(request: Request) -> dict:
    """开放接口鉴权：X-API-Key / Authorization: Bearer rk_xxx / ?key=rk_xxx

    最后一种是给单片机留的：ESP32 上拼 Header 也行，但塞进 URL 更省事。
    """
    key = request.headers.get("x-api-key") or request.query_params.get("key")
    if not key:
        auth = request.headers.get("authorization") or ""
        if auth.startswith("Bearer rk_"):
            key = auth[7:]
    if not key:
        raise HTTPException(401, "缺少 X-API-Key")

    row = one("SELECT * FROM api_keys WHERE key_value = ?", (key,))
    if not row:
        raise HTTPException(401, "API Key 无效")
    user = one("SELECT * FROM users WHERE id = ?", (row["user_id"],))
    if not user:
        raise HTTPException(401, "账号不存在")
    execute("UPDATE api_keys SET last_used_at = ? WHERE id = ?", (now_iso(), row["id"]))
    return user


def ingest_auth(request: Request) -> tuple[dict, dict | None]:
    """推流鉴权：优先设备 token（绑定后每台一密），没有就退回账号级 API Key。

    返回 (user, device)。用 token 时 device 就是那一行，不再按名字找。
    """
    token = request.headers.get("x-device-token")
    if not token:
        auth = request.headers.get("authorization") or ""
        if auth.startswith("Bearer dv_"):
            token = auth[7:]
    if token:
        dev = one("SELECT * FROM devices WHERE token = ?", (token,))
        if not dev:
            raise HTTPException(401, "设备 token 无效，请重新配对")
        user = one("SELECT * FROM users WHERE id = ?", (dev["user_id"],))
        if not user:
            raise HTTPException(401, "账号不存在")
        execute("UPDATE devices SET last_seen_at = ? WHERE id = ?", (now_iso(), dev["id"]))
        return user, dev

    return open_user(request), None


def _get_owned(recording_id: str, user_id: str) -> dict:
    rec = one(
        "SELECT * FROM recordings WHERE id = ? AND user_id = ?",
        (recording_id, user_id),
    )
    if not rec:
        raise HTTPException(404, "录音不存在")
    return rec


async def _read_audio(request: Request) -> tuple[bytes, str, str]:
    """返回 (音频字节, mime, 原始文件名)。支持 multipart 与原始二进制两种提交方式。"""
    ctype = request.headers.get("content-type") or ""
    if "multipart/form-data" in ctype:
        form = await request.form()
        upload = form.get("file")
        if upload is None or not hasattr(upload, "read"):
            raise HTTPException(400, "缺少音频文件")
        return (
            await upload.read(),
            getattr(upload, "content_type", None) or "audio/webm",
            getattr(upload, "filename", None) or "audio.bin",
        )

    data = await request.body()
    mime = ctype.split(";")[0].strip() or "audio/webm"
    return data, mime, request.headers.get("x-filename") or "audio.bin"


@router.get("/me")
def whoami(user: dict = Depends(open_user)) -> dict:
    return {
        "id": user["id"],
        "username": user["username"],
        "displayName": user.get("display_name"),
    }


# ------------------------------------------------------------------ #
# 配对：设备没有键盘，靠"设备播报/显示编号 + 后台输编号"完成绑定
# ------------------------------------------------------------------ #
_pair_hits: dict[str, list[float]] = {}


def _pair_rate_ok(ip: str) -> bool:
    now = time.time()
    hits = [t for t in _pair_hits.get(ip, []) if now - t < 60]
    if len(hits) >= 60:
        _pair_hits[ip] = hits
        return False
    hits.append(now)
    _pair_hits[ip] = hits
    return True


@router.get("/pair")
@router.post("/pair")
def pair_device(
    request: Request,
    code: str = Query(..., description="设备开机播报/显示的 6 位数字编号，由 MAC 换算"),
    name: str | None = Query(None, description="设备自报的名字"),
    firmware: str | None = Query(None, description="固件版本，方便后台识别"),
) -> dict:
    """设备轮询用。未绑定返回 pending；后台绑定后返回 token，设备存进 NVS 即可。

    后台绑定与设备轮询是同一时间窗内的双边动作：设备正显示编号、后台正开着绑定界面。
    """
    ip = request.client.host if request.client else "unknown"
    if not _pair_rate_ok(ip):
        raise HTTPException(429, "轮询太频繁，稍后再试")
    try:
        return poll_pair(code, name, firmware, ip)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


# ------------------------------------------------------------------ #
# 硬件设备推流：会话边界由服务端判断
# ------------------------------------------------------------------ #
@router.post("/ingest")
async def ingest_audio(
    request: Request,
    device: str = Query("default", description="设备名，未绑定 token 时用它区分设备"),
    duration_ms: int = Query(0, alias="durationMs", description="本段音频时长，不填则服务端探测"),
    title: str | None = Query(None, description="新会话的标题，不填则自动生成"),
    recorded_at: str | None = Query(None, alias="recordedAt", description="设备端录制时间（ISO），时钟不准可不传"),
    gap_sec: int | None = Query(None, alias="gapSec", description="临时覆盖：静默多久算新会话"),
    idle_sec: int | None = Query(None, alias="idleSec", description="临时覆盖：静默多久自动收尾"),
    rate: int = Query(16000, description="裸 PCM 采样率"),
    channels: int = Query(1, description="裸 PCM 声道数"),
    auth: tuple[dict, dict | None] = Depends(ingest_auth),
) -> dict:
    """设备推一段音频。服务端按时间间隔自动判断这段是接着上一段，还是新的一次会话。"""
    user, dev_row = auth

    data, mime, filename = await _read_audio(request)
    if not data:
        raise HTTPException(400, "请求体需要是音频二进制")

    result = await asyncio.to_thread(
        ingest,
        user["id"],
        None if dev_row else device,
        data=data,
        mime=mime or "audio/wav",
        filename=filename,
        duration_ms=duration_ms,
        title=title,
        recorded_at=recorded_at,
        gap_sec=gap_sec,
        idle_sec=idle_sec,
        rate=rate,
        channels=channels,
        device=dev_row,
    )
    drain()
    return result


@router.get("/ingest")
def ingest_status(
    device: str = Query("default"),
    auth: tuple[dict, dict | None] = Depends(ingest_auth),
) -> dict:
    """设备探活 + 查询当前会话。设备启动自检时调这个最合适。"""
    user, dev_row = auth
    row = dev_row or one(
        "SELECT * FROM devices WHERE user_id = ? AND name = ?",
        (user["id"], (device or "default")[:40]),
    )
    if not row:
        return {"ok": True, "device": device, "sessionOpen": False, "exists": False}
    info = public_device(row)
    return {
        "ok": True,
        "device": info["name"],
        "exists": True,
        "sessionOpen": bool(info["openRecordingId"]),
        "recordingId": info["openRecordingId"],
        "title": info["openRecordingTitle"],
        "sessionChunks": info["openChunks"],
        "sessionDurationMs": info["openDurationMs"],
        "gapSec": info["gapSec"],
        "idleSec": info["idleSec"],
    }


@router.post("/ingest/close")
def ingest_close(
    device: str = Query("default"),
    auth: tuple[dict, dict | None] = Depends(ingest_auth),
) -> dict:
    """设备休眠/关机前调一次，立刻收尾并触发纪要，不用等自动超时。"""
    user, dev_row = auth
    row = dev_row or one(
        "SELECT * FROM devices WHERE user_id = ? AND name = ?",
        (user["id"], (device or "default")[:40]),
    )
    if not row:
        return {"ok": True, "closed": False, "reason": "设备不存在"}
    if not row.get("open_recording_id"):
        return {"ok": True, "closed": False, "reason": "没有进行中的会话"}
    rec_id = close_session(row, reason="device")
    drain()
    rec = one("SELECT status, duration_ms FROM recordings WHERE id = ?", (rec_id,)) if rec_id else None
    return {
        "ok": True,
        "closed": True,
        "recordingId": rec_id,
        "status": (rec or {}).get("status"),
        "durationMs": (rec or {}).get("duration_ms") or 0,
    }


@router.post("/recordings", status_code=201)
def create_recording(
    body: dict[str, Any] = Body(default_factory=dict),
    user: dict = Depends(open_user),
) -> dict:
    rid = uid("rec_")
    title = str(body.get("title") or "").strip() or _default_title()
    execute(
        """INSERT INTO recordings
           (id, user_id, title, status, source, language, started_at, asr_provider_id, created_at)
           VALUES (?, ?, ?, 'recording', 'api', ?, ?, ?, ?)""",
        (
            rid,
            user["id"],
            title[:100],
            body.get("language") or "zh",
            body.get("startedAt") or now_iso(),
            body.get("asrProviderId"),
            now_iso(),
        ),
    )
    return {"id": rid, "title": title, "status": "recording"}


@router.post("/recordings/{recording_id}/chunks", status_code=201)
async def upload_chunk(
    recording_id: str,
    request: Request,
    seq: int = Query(0),
    duration_ms: int = Query(0, alias="durationMs"),
    start_offset_ms: int | None = Query(None, alias="startOffsetMs"),
    user: dict = Depends(open_user),
) -> dict:
    _get_owned(recording_id, user["id"])

    data, mime, filename = await _read_audio(request)
    if not data:
        raise HTTPException(400, "请求体需要是音频二进制")

    if start_offset_ms is None:
        agg = one(
            "SELECT COALESCE(SUM(duration_ms),0) AS s FROM chunks WHERE recording_id = ?",
            (recording_id,),
        ) or {"s": 0}
        offset = int(agg["s"] or 0)
    else:
        offset = int(start_offset_ms)

    folder = UPLOADS_DIR / recording_id
    folder.mkdir(parents=True, exist_ok=True)
    ext = Path(filename).suffix or ext_from_mime(mime)
    name = f"{seq:05d}{ext}"
    full = folder / name
    full.write_bytes(data)

    duration = int(duration_ms) or probe_duration_ms(full) or 0
    t = now_iso()
    chunk_id = uid("chk_")
    execute(
        """INSERT INTO chunks
           (id, recording_id, seq, filename, path, mime, size, duration_ms,
            start_offset_ms, status, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?)
           ON CONFLICT(recording_id, seq) DO UPDATE SET
             path = excluded.path, size = excluded.size, duration_ms = excluded.duration_ms,
             status = 'queued', transcript = NULL, segments = NULL,
             updated_at = excluded.updated_at""",
        (
            chunk_id,
            recording_id,
            seq,
            name,
            str(full),
            mime,
            len(data),
            duration,
            offset,
            t,
            t,
        ),
    )

    total = one(
        "SELECT COALESCE(MAX(start_offset_ms + duration_ms),0) AS s FROM chunks WHERE recording_id = ?",
        (recording_id,),
    ) or {"s": 0}
    execute(
        "UPDATE recordings SET status = 'processing', duration_ms = ? WHERE id = ?",
        (int(total["s"] or 0), recording_id),
    )
    enqueue("transcribe", {"chunkId": chunk_id}, max_attempts=3)

    return {
        "chunkId": chunk_id,
        "seq": seq,
        "status": "queued",
        "durationMs": duration,
    }


@router.post("/recordings/{recording_id}/finish")
def finish_recording(recording_id: str, user: dict = Depends(open_user)) -> dict:
    _get_owned(recording_id, user["id"])
    execute(
        """UPDATE recordings SET ended_at = COALESCE(ended_at, ?), status = 'processing'
           WHERE id = ?""",
        (now_iso(), recording_id),
    )
    finalize_recording(recording_id)
    drain()
    rec = one("SELECT * FROM recordings WHERE id = ?", (recording_id,))
    assert rec is not None
    return {"id": recording_id, "status": rec["status"]}


@router.get("/recordings/{recording_id}")
def get_recording(recording_id: str, user: dict = Depends(open_user)) -> dict:
    rec = _get_owned(recording_id, user["id"])
    t = transcript_for(recording_id)
    return {
        "id": rec["id"],
        "title": rec["title"],
        "status": rec["status"],
        "durationMs": rec.get("duration_ms") or 0,
        "startedAt": rec["started_at"],
        "endedAt": rec.get("ended_at"),
        "summaryStatus": rec["summary_status"],
        "chunks": {
            "total": t["chunkCount"],
            "done": t["doneChunks"],
            "failed": t["failedChunks"],
        },
        "text": t["text"],
        "segments": t["segments"],
    }


@router.post("/transcribe")
async def transcribe_once(
    request: Request,
    title: str | None = Query(None),
    language: str = Query("zh"),
    wait: str | None = Query(None),
    timeout: int = Query(120_000),
    user: dict = Depends(open_user),
) -> Any:
    """一步到位：上传完整音频 → 建会话 → 转写。默认异步返回 id，?wait=1 时等结果。"""
    data, mime, filename = await _read_audio(request)
    if not data:
        raise HTTPException(400, "请求体需要是音频二进制")

    rid = uid("rec_")
    rec_title = (title or "").strip() or _default_title()
    execute(
        """INSERT INTO recordings
           (id, user_id, title, status, source, language, started_at, created_at)
           VALUES (?, ?, ?, 'processing', 'api', ?, ?, ?)""",
        (rid, user["id"], rec_title[:100], language or "zh", now_iso(), now_iso()),
    )

    folder = UPLOADS_DIR / rid
    folder.mkdir(parents=True, exist_ok=True)
    ext = Path(filename).suffix or ext_from_mime(mime)
    full = folder / f"00000{ext}"
    full.write_bytes(data)

    duration = probe_duration_ms(full) or 0
    t = now_iso()
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
        (duration, now_iso(), rid),
    )
    enqueue("transcribe", {"chunkId": chunk_id}, max_attempts=3)
    drain()

    if wait in ("1", "true"):
        deadline = time.monotonic() + min(300_000, max(1000, int(timeout or 120_000))) / 1000
        while time.monotonic() < deadline:
            c = one("SELECT status, error FROM chunks WHERE id = ?", (chunk_id,))
            if c and c["status"] in ("done", "failed"):
                break
            await asyncio.sleep(0.5)
        fresh = one("SELECT * FROM recordings WHERE id = ?", (rid,))
        assert fresh is not None
        result = transcript_for(rid)
        return {
            "id": rid,
            "title": rec_title,
            "status": fresh["status"],
            "durationMs": fresh.get("duration_ms") or 0,
            "text": result["text"],
            "segments": result["segments"],
            "summaryStatus": fresh["summary_status"],
        }

    return JSONResponse(
        status_code=202,
        content={
            "id": rid,
            "title": rec_title,
            "status": "processing",
            "durationMs": duration,
        },
    )
