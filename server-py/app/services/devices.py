from __future__ import annotations

import io
import re
import secrets
import threading
import time
import traceback
import wave
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..audio import probe_duration_ms
from ..bus import emit_recording
from ..config import UPLOADS_DIR, uid
from ..db import all_, execute, now_iso, one
from ..jobs import enqueue
from .transcribe import finalize_recording

GAP_DEFAULT = 90      # 秒：超过这个静默时长就认定是"另一次聊天"
IDLE_DEFAULT = 180    # 秒：这么久没新音频就自动收尾
DEVICE_SOURCE = "device"

CODE_RE = re.compile(r"^\d{6}$")          # 设备编号：6 位纯数字，方便播报和口述
LEGACY_CODE_RE = re.compile(r"^[0-9A-F]{8}$")  # 老版本 8 位十六进制，继续兼容
CLAIM_TTL_MIN = 30    # 设备轮询超过这么久没动静，就从"待配对"里消失

# 中文数字 / 口头念法，语音播报后口述输入也能认
_CN_DIGITS = {
    "零": "0", "〇": "0", "洞": "0",
    "一": "1", "幺": "1",
    "二": "2", "两": "2",
    "三": "3", "四": "4", "五": "5", "六": "6", "七": "7", "八": "8", "九": "9",
}
# 念错/看错时最常见的字母，统一纠正成数字
_LOOKALIKE = str.maketrans({"O": "0", "I": "1", "L": "1", "Z": "2", "S": "5", "B": "8"})

_stop = threading.Event()
_sweeper: threading.Thread | None = None


def normalize_code(raw: str | None) -> str:
    """设备编号统一成无分隔符的大写串；中文数字（九零三四七一）也转成阿拉伯数字。

    设备端和后台输入都走这里，所以「903-471」「903 471」「九零三四七一」都等价。
    """
    s = str(raw or "")
    s = "".join(_CN_DIGITS.get(ch, ch) for ch in s)
    return re.sub(r"[^0-9A-Za-z]", "", s).upper()


def new_token() -> str:
    return "dv_" + secrets.token_hex(20)


def mask_token(token: str | None) -> str:
    if not token:
        return ""
    if len(token) <= 12:
        return "****"
    return f"{token[:7]}...{token[-4:]}"


def _parse(iso: str | None) -> datetime | None:
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def wrap_pcm_wav(data: bytes, rate: int = 16000, channels: int = 1, width: int = 2) -> bytes:
    """把裸 PCM 包成 WAV。硬件端可以省掉自己拼 44 字节头。"""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(max(1, channels))
        w.setsampwidth(max(1, min(4, width)))
        w.setframerate(max(8000, rate))
        w.writeframes(data)
    return buf.getvalue()


# ------------------------------------------------------------------ #
# 设备记录
# ------------------------------------------------------------------ #
def get_device(user_id: str, device_id: str) -> dict | None:
    return one("SELECT * FROM devices WHERE id = ? AND user_id = ?", (device_id, user_id))


def get_or_create(user_id: str, name: str) -> dict:
    name = (name or "").strip()[:40] or "default"
    row = one("SELECT * FROM devices WHERE user_id = ? AND name = ?", (user_id, name))
    if row:
        return row
    did = uid("dev_")
    execute(
        """INSERT INTO devices (id, user_id, name, gap_sec, idle_sec, session_chunks, created_at)
           VALUES (?, ?, ?, ?, ?, 0, ?)""",
        (did, user_id, name, GAP_DEFAULT, IDLE_DEFAULT, now_iso()),
    )
    row = one("SELECT * FROM devices WHERE id = ?", (did,))
    assert row is not None
    return row


def public_device(d: dict) -> dict:
    rec = None
    if d.get("open_recording_id"):
        rec = one("SELECT * FROM recordings WHERE id = ?", (d["open_recording_id"],))
    return {
        "id": d["id"],
        "name": d["name"],
        "code": d.get("code"),
        "tokenMasked": mask_token(d.get("token")),
        "paired": bool(d.get("token")),
        "boundAt": d.get("bound_at"),
        "gapSec": d.get("gap_sec") or GAP_DEFAULT,
        "idleSec": d.get("idle_sec") or IDLE_DEFAULT,
        "openRecordingId": d.get("open_recording_id"),
        "openRecordingTitle": rec["title"] if rec else None,
        "openDurationMs": (rec.get("duration_ms") or 0) if rec else 0,
        "openChunks": len(
            all_("SELECT id FROM chunks WHERE recording_id = ?", (rec["id"],))
        ) if rec else 0,
        "sessionChunks": d.get("session_chunks") or 0,
        "lastChunkAt": d.get("last_chunk_at"),
        "lastSeenAt": d.get("last_seen_at"),
        "lastPollAt": d.get("last_poll_at"),
        "createdAt": d["created_at"],
    }


def list_devices(user_id: str) -> list[dict]:
    rows = all_("SELECT * FROM devices WHERE user_id = ? ORDER BY created_at ASC", (user_id,))
    return [public_device(r) for r in rows]


def update_device(user_id: str, device_id: str, patch: dict) -> dict:
    d = get_device(user_id, device_id)
    if not d:
        raise ValueError("设备不存在")

    sets: list[str] = []
    params: list[Any] = []
    if patch.get("name") is not None:
        sets.append("name = ?")
        params.append(str(patch["name"]).strip()[:40] or d["name"])
    if patch.get("gapSec") is not None:
        sets.append("gap_sec = ?")
        params.append(max(3, min(3600, int(patch["gapSec"]))))
    if patch.get("idleSec") is not None:
        sets.append("idle_sec = ?")
        params.append(max(10, min(7200, int(patch["idleSec"]))))
    if sets:
        params.extend([device_id, user_id])
        execute(f"UPDATE devices SET {', '.join(sets)} WHERE id = ? AND user_id = ?", params)
    fresh = get_device(user_id, device_id)
    assert fresh is not None
    return public_device(fresh)


def delete_device(user_id: str, device_id: str) -> None:
    d = get_device(user_id, device_id)
    if not d:
        return
    if d.get("open_recording_id"):
        # 先收尾，避免留下一段永远不会结束的录音
        close_session(d, reason="delete")
    execute(
        "DELETE FROM device_claims WHERE code = ?",
        (d.get("code") or "\x00",),
    )
    execute("DELETE FROM devices WHERE id = ? AND user_id = ?", (device_id, user_id))


# ------------------------------------------------------------------ #
# 配对：设备显示编号 → 后台输编号 → 设备轮询取回 token
# ------------------------------------------------------------------ #
def _clean_code(code: str | None) -> str:
    c = normalize_code(code)
    if CODE_RE.match(c):
        return c
    # 用户把数字念成字母了（0 打成 O、1 打成 I/L、5 打成 S…），纠正后再判断
    fixed = c.translate(_LOOKALIKE)
    if CODE_RE.match(fixed):
        return fixed
    # 老固件用 8 位十六进制编号，升级后仍能配对
    if LEGACY_CODE_RE.match(c):
        return c
    raise ValueError("设备编号是 6 位数字，例如 903471")


def poll_pair(code: str, name: str | None, firmware: str | None, ip: str | None) -> dict:
    """设备开机后带着自己的编号轮询这个接口。

    未绑定 → pending；已被后台认领 → 直接下发 token（NVS 掉了也能自愈）。
    """
    c = _clean_code(code)

    dev = one("SELECT * FROM devices WHERE code = ?", (c,))
    if dev and dev.get("token"):
        execute(
            "UPDATE devices SET last_poll_at = ?, last_seen_at = ? WHERE id = ?",
            (now_iso(), now_iso(), dev["id"]),
        )
        execute("DELETE FROM device_claims WHERE code = ?", (c,))
        return {
            "status": "bound",
            "token": dev["token"],
            "deviceId": dev["id"],
            "name": dev["name"],
            "gapSec": dev.get("gap_sec") or GAP_DEFAULT,
            "idleSec": dev.get("idle_sec") or IDLE_DEFAULT,
        }

    ts = now_iso()
    execute(
        """INSERT INTO device_claims (code, name, first_seen_at, last_poll_at, poll_count, remote_ip, firmware)
           VALUES (?, ?, ?, ?, 1, ?, ?)
           ON CONFLICT(code) DO UPDATE SET
             last_poll_at = excluded.last_poll_at,
             poll_count = device_claims.poll_count + 1,
             name = COALESCE(excluded.name, device_claims.name),
             remote_ip = excluded.remote_ip,
             firmware = COALESCE(excluded.firmware, device_claims.firmware)""",
        (c, (name or "")[:40] or None, ts, ts, ip, (firmware or "")[:40] or None),
    )
    return {"status": "pending", "code": c, "pollAfterMs": 3000}


def list_claims() -> list[dict]:
    """待配对设备。编号是设备物理显示的，本身不是机密，这里只列还没被认领的。"""
    cutoff = datetime.now(timezone.utc).timestamp() - CLAIM_TTL_MIN * 60
    out: list[dict] = []
    for row in all_("SELECT * FROM device_claims ORDER BY last_poll_at DESC"):
        last = _parse(row.get("last_poll_at"))
        if last is None or last.timestamp() < cutoff:
            continue
        if one("SELECT 1 AS x FROM devices WHERE code = ?", (row["code"],)):
            continue
        out.append(
            {
                "code": row["code"],
                "name": row.get("name"),
                "firmware": row.get("firmware"),
                "remoteIp": row.get("remote_ip"),
                "firstSeenAt": row.get("first_seen_at"),
                "lastPollAt": row.get("last_poll_at"),
                "pollCount": row.get("poll_count") or 0,
            }
        )
    return out


def _unique_name(user_id: str, base: str) -> str:
    base = (base or "").strip()[:40] or "设备"
    name = base
    i = 2
    while one("SELECT 1 AS x FROM devices WHERE user_id = ? AND name = ?", (user_id, name)):
        suffix = f"-{i}"
        name = base[: 40 - len(suffix)] + suffix
        i += 1
    return name


def bind_device(user_id: str, code: str, name: str | None = None) -> dict:
    """后台输入设备编号完成绑定，并下发 token。"""
    c = _clean_code(code)
    claim = one("SELECT * FROM device_claims WHERE code = ?", (c,))

    dev = one("SELECT * FROM devices WHERE code = ?", (c,))
    if dev:
        if dev["user_id"] != user_id:
            raise PermissionError("该设备已绑定到其他账号")
        token = dev.get("token") or new_token()
        execute(
            """UPDATE devices SET token = ?, name = COALESCE(?, name), bound_at = COALESCE(bound_at, ?)
               WHERE id = ?""",
            (token, _unique_name(user_id, name) if name else None, now_iso(), dev["id"]),
        )
    else:
        guessed = name or (claim or {}).get("name") or c
        dev_id = uid("dev_")
        token = new_token()
        execute(
            """INSERT INTO devices
               (id, user_id, name, code, token, gap_sec, idle_sec, session_chunks,
                bound_at, last_poll_at, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)""",
            (
                dev_id,
                user_id,
                _unique_name(user_id, guessed),
                c,
                token,
                GAP_DEFAULT,
                IDLE_DEFAULT,
                now_iso(),
                now_iso(),
                now_iso(),
            ),
        )

    execute("DELETE FROM device_claims WHERE code = ?", (c,))
    fresh = one("SELECT * FROM devices WHERE code = ?", (c,))
    assert fresh is not None
    return public_device(fresh)


def reset_token(user_id: str, device_id: str) -> dict:
    """换发 token：老 token 立即失效，设备下次轮询会自动拿到新的。"""
    d = get_device(user_id, device_id)
    if not d:
        raise ValueError("设备不存在")
    execute(
        "UPDATE devices SET token = ?, last_poll_at = NULL WHERE id = ?",
        (new_token(), device_id),
    )
    fresh = one("SELECT * FROM devices WHERE id = ?", (device_id,))
    assert fresh is not None
    return public_device(fresh)


# ------------------------------------------------------------------ #
# 会话边界
# ------------------------------------------------------------------ #
def _create_session(user_id: str, device: dict, title: str | None, started_at: str | None) -> dict:
    rid = uid("rec_")
    ts = started_at or now_iso()
    name = device["name"]
    now = datetime.now()
    auto_title = f"{name} {now.month}/{now.day} {now:%H:%M}"
    execute(
        """INSERT INTO recordings
           (id, user_id, title, status, source, language, started_at, created_at)
           VALUES (?, ?, ?, 'processing', ?, 'zh', ?, ?)""",
        (rid, user_id, (title or auto_title)[:100], DEVICE_SOURCE, ts, now_iso()),
    )
    execute(
        "UPDATE devices SET open_recording_id = ?, session_chunks = 0 WHERE id = ?",
        (rid, device["id"]),
    )
    rec = one("SELECT * FROM recordings WHERE id = ?", (rid,))
    assert rec is not None
    return rec


def close_session(device: dict, reason: str = "idle") -> str | None:
    """结束设备当前会话：清空指针后走正常的收尾流程（含自动纪要）。"""
    rec_id = device.get("open_recording_id")
    execute(
        """UPDATE devices SET open_recording_id = NULL, last_chunk_at = NULL, session_chunks = 0
           WHERE id = ?""",
        (device["id"],),
    )
    if not rec_id:
        return None
    emit_recording(rec_id, "session", {"event": "closed", "reason": reason})
    finalize_recording(rec_id)
    return rec_id


def close_idle_devices() -> int:
    """把静默太久的设备会话自动收尾。"""
    closed = 0
    for d in all_("SELECT * FROM devices WHERE open_recording_id IS NOT NULL"):
        last = _parse(d.get("last_chunk_at"))
        if last is None:
            # 指针还在但没有时间戳（异常残留），直接收尾
            close_session(d, reason="stale")
            closed += 1
            continue
        idle = int(d.get("idle_sec") or IDLE_DEFAULT)
        if (datetime.now(timezone.utc) - last).total_seconds() >= idle:
            close_session(d, reason="idle")
            closed += 1
    return closed


# ------------------------------------------------------------------ #
# 收音频
# ------------------------------------------------------------------ #
def ingest(
    user_id: str,
    device_name: str | None = None,
    *,
    data: bytes,
    mime: str,
    filename: str | None,
    duration_ms: int = 0,
    title: str | None = None,
    recorded_at: str | None = None,
    gap_sec: int | None = None,
    idle_sec: int | None = None,
    rate: int = 16000,
    channels: int = 1,
    device: dict | None = None,
) -> dict:
    """设备推一段音频：服务端判断是接着上一段，还是新的一次会话。

    认了 token 的设备直接传 device 行，不再按名字查找。
    """
    if device is not None and device.get("user_id") != user_id:
        raise PermissionError("设备不属于该账号")
    device = device or get_or_create(user_id, device_name or "default")

    # 设备可临时覆盖阈值
    overrides: list[str] = []
    params: list[Any] = []
    if gap_sec is not None:
        overrides.append("gap_sec = ?")
        params.append(max(3, min(3600, int(gap_sec))))
    if idle_sec is not None:
        overrides.append("idle_sec = ?")
        params.append(max(10, min(7200, int(idle_sec))))
    if overrides:
        params.append(device["id"])
        execute(f"UPDATE devices SET {', '.join(overrides)} WHERE id = ?", params)
        device = one("SELECT * FROM devices WHERE id = ?", (device["id"],)) or device

    gap = int(device.get("gap_sec") or GAP_DEFAULT)
    now = datetime.now(timezone.utc)

    rec = None
    if device.get("open_recording_id"):
        rec = one("SELECT * FROM recordings WHERE id = ?", (device["open_recording_id"],))
        if rec is None:
            execute(
                """UPDATE devices SET open_recording_id = NULL, last_chunk_at = NULL,
                   session_chunks = 0 WHERE id = ?""",
                (device["id"],),
            )

    # 判断是不是新的一次「聊天」：距上一段太久就断开
    new_session = False
    gap_seconds = 0.0
    if rec is None:
        new_session = True
    else:
        last = _parse(device.get("last_chunk_at"))
        if last is not None:
            gap_seconds = (now - last).total_seconds()
            if gap_seconds > gap:
                close_session(device, reason="gap")
                new_session = True
        else:
            new_session = True

    if new_session:
        device = one("SELECT * FROM devices WHERE id = ?", (device["id"],)) or device
        rec = _create_session(user_id, device, title, recorded_at)

    assert rec is not None

    # 裸 PCM 自动补 WAV 头，硬件端不用自己拼
    if mime.split(";")[0].strip().lower() in ("audio/pcm", "audio/l16", "audio/raw"):
        data = wrap_pcm_wav(data, rate=rate, channels=channels)
        mime = "audio/wav"
        filename = (filename or "chunk.wav")
        if not filename.lower().endswith(".wav"):
            filename = filename.rsplit(".", 1)[0] + ".wav"

    folder = UPLOADS_DIR / rec["id"]
    folder.mkdir(parents=True, exist_ok=True)

    existing = all_(
        "SELECT seq, duration_ms, start_offset_ms FROM chunks WHERE recording_id = ? ORDER BY seq ASC",
        (rec["id"],),
    )
    seq = (existing[-1]["seq"] + 1) if existing else 0
    offset = sum(int(c.get("duration_ms") or 0) for c in existing)

    ext = Path(filename or "").suffix or ".wav"
    fname = f"{seq:05d}{ext}"
    full = folder / fname
    full.write_bytes(data)

    real_duration = int(duration_ms) or probe_duration_ms(full) or 0
    t = now_iso()
    chunk_id = uid("chk_")
    execute(
        """INSERT INTO chunks
           (id, recording_id, seq, filename, path, mime, size, duration_ms,
            start_offset_ms, status, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?)""",
        (
            chunk_id,
            rec["id"],
            seq,
            fname,
            str(full),
            mime,
            len(data),
            real_duration,
            offset,
            t,
            t,
        ),
    )

    total_ms = offset + real_duration
    execute(
        """UPDATE recordings SET status = 'processing', duration_ms = ?,
           ended_at = COALESCE(ended_at, ?) WHERE id = ?""",
        (total_ms, t, rec["id"]),
    )
    execute(
        """UPDATE devices SET open_recording_id = ?, last_chunk_at = ?, last_seen_at = ?,
           session_chunks = COALESCE(session_chunks, 0) + 1 WHERE id = ?""",
        (rec["id"], t, t, device["id"]),
    )

    enqueue("transcribe", {"chunkId": chunk_id}, max_attempts=3)
    emit_recording(rec["id"], "chunk", {"chunkId": chunk_id, "seq": seq, "status": "queued"})

    session_chunks = int((device.get("session_chunks") or 0)) + 1
    return {
        "ok": True,
        "recordingId": rec["id"],
        "title": rec["title"],
        "chunkId": chunk_id,
        "chunkSeq": seq,
        "newSession": new_session,
        "gapSec": round(gap_seconds, 1) if gap_seconds else 0,
        "sessionChunks": session_chunks,
        "sessionDurationMs": total_ms,
        "durationMs": real_duration,
    }


# ------------------------------------------------------------------ #
# 后台收尾
# ------------------------------------------------------------------ #
def _loop(interval: float) -> None:
    while not _stop.is_set():
        try:
            close_idle_devices()
        except Exception:  # noqa: BLE001
            traceback.print_exc()
        _stop.wait(interval)


def start_sweeper(interval: float = 10.0) -> None:
    global _sweeper
    _stop.clear()
    _sweeper = threading.Thread(target=_loop, args=(interval,), daemon=True, name="device-sweeper")
    _sweeper.start()


def stop_sweeper() -> None:
    _stop.set()
    time.sleep(0)
