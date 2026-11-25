from __future__ import annotations

import io
import re
import secrets
import threading
import time
import traceback
import wave
from datetime import datetime, timedelta, timezone
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
# 单账号服务器自动认领设备时，最多自动挂这么多台
AUTO_CLAIM_MAX_DEVICES = 8

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


def _bound_payload(dev: dict) -> dict:
    return {
        "status": "bound",
        "token": dev["token"],
        "deviceId": dev["id"],
        "name": dev["name"],
        "gapSec": dev.get("gap_sec") or GAP_DEFAULT,
        "idleSec": dev.get("idle_sec") or IDLE_DEFAULT,
    }


def _auto_claim_user() -> dict | None:
    """单账号的自托管服务器：没见过的编号直接认领，不需要人工绑定。

    设备恢复出厂 / 服务端换过库之后，token 在设备侧就没了。以前这种时候得去
    后台"输编号绑定"甚至"重置 token"，其实是多余的：库里只有一个账号时，
    这台服务器上不存在"设备该归谁"的问题，直接建条下发即可。

    多账号部署保持原样（返回 None，走人工认领），否则谁都能把自己的设备
    塞进别人的账号里。
    """
    users = all_("SELECT * FROM users")
    if len(users) != 1:
        return None
    user = users[0]
    row = one("SELECT COUNT(*) AS n FROM devices WHERE user_id = ?", (user["id"],))
    if row and int(row.get("n") or 0) >= AUTO_CLAIM_MAX_DEVICES:
        return None
    return user


def poll_pair(code: str, name: str | None, firmware: str | None, ip: str | None) -> dict:
    """设备开机后带着自己的编号轮询这个接口。

    未绑定 → pending；已被后台认领 → 直接下发 token（NVS 掉了也能自愈）；
    单账号服务器上遇到全新编号 → 自动建档下发 token（设备侧重置后自愈）。
    """
    c = _clean_code(code)

    dev = one("SELECT * FROM devices WHERE code = ?", (c,))
    if dev and dev.get("token"):
        execute(
            "UPDATE devices SET last_poll_at = ?, last_seen_at = ? WHERE id = ?",
            (now_iso(), now_iso(), dev["id"]),
        )
        execute("DELETE FROM device_claims WHERE code = ?", (c,))
        return _bound_payload(dev)

    if dev is None:
        owner = _auto_claim_user()
        if owner is not None:
            _insert_device(owner["id"], c, name)
            execute("DELETE FROM device_claims WHERE code = ?", (c,))
            fresh = one("SELECT * FROM devices WHERE code = ?", (c,))
            if fresh is not None:
                execute(
                    "UPDATE devices SET last_poll_at = ?, last_seen_at = ? WHERE id = ?",
                    (now_iso(), now_iso(), fresh["id"]),
                )
                dev = one("SELECT * FROM devices WHERE id = ?", (fresh["id"],)) or fresh
                return _bound_payload(dev)
    elif not dev.get("token"):
        # 有记录但没 token（老库补列、或某次重置只擦了 token）：直接补发一个，
        # 别让设备卡在"等待后台绑定"上
        owner = _auto_claim_user()
        if owner is not None and dev["user_id"] == owner["id"]:
            execute(
                """UPDATE devices SET token = ?, bound_at = COALESCE(bound_at, ?),
                   last_poll_at = ?, last_seen_at = ? WHERE id = ?""",
                (new_token(), now_iso(), now_iso(), now_iso(), dev["id"]),
            )
            fresh = one("SELECT * FROM devices WHERE id = ?", (dev["id"],))
            if fresh is not None:
                return _bound_payload(fresh)

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


def _insert_device(user_id: str, code: str, name: str | None) -> str:
    """新建一台设备并下发 token，返回 token。名字冲突时自动加 -2/-3 后缀。"""
    token = new_token()
    execute(
        """INSERT INTO devices
           (id, user_id, name, code, token, gap_sec, idle_sec, session_chunks,
            bound_at, last_poll_at, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)""",
        (
            uid("dev_"),
            user_id,
            _unique_name(user_id, name or code),
            code,
            token,
            GAP_DEFAULT,
            IDLE_DEFAULT,
            now_iso(),
            now_iso(),
            now_iso(),
        ),
    )
    return token


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
        token = _insert_device(user_id, c, guessed)

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
    # 标题里的时间跟着"录制时间"走：补传昨天的录音不该标成今天的 19:30
    base = _parse(ts) or datetime.now(timezone.utc)
    local = base.astimezone()
    auto_title = f"{name} {local.month}/{local.day} {local:%H:%M}"
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
        """UPDATE devices SET open_recording_id = NULL, last_chunk_at = NULL,
           last_rec_end = NULL, session_chunks = 0
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
def _chunk_gap(
    rec_at: datetime | None,
    prev_rec_end: datetime | None,
    last_chunk_at: datetime | None,
    now: datetime,
) -> float | None:
    """本片和上一片之间隔了多久（秒）。None = 没有参照，只能算新会话。

    为什么不能只看到达时间：设备出门攒了一整天的录音，回家连上 WiFi 后
    几十个分片几秒钟内全部推完，到达间隔永远是 1~2 秒，远小于 gapSec，
    一整天会被并成一条记录（时间轴、纪要、日报全都错位）。

    所以设备带了 recordedAt 就用"本片开始录制 − 上一片结束录制"，
    没带才退回服务器到达时间（老固件行为不变）。
    """
    if rec_at is not None:
        if prev_rec_end is not None:
            return (rec_at - prev_rec_end).total_seconds()
        return None
    if last_chunk_at is None:
        return None
    return (now - last_chunk_at).total_seconds()


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
    chunk_key: str | None = None,
) -> dict:
    """设备推一段音频：服务端判断是接着上一段，还是新的一次会话。

    认了 token 的设备直接传 device 行，不再按名字查找。
    """
    if device is not None and device.get("user_id") != user_id:
        raise PermissionError("设备不属于该账号")
    device = device or get_or_create(user_id, device_name or "default")

    # 幂等：同一个分片重传（上次响应丢了、超时重试）直接返回原来的结果，
    # 既不重复插入，也不把音频写第二遍。
    if chunk_key:
        dup = one(
            """SELECT c.* FROM chunks c JOIN recordings r ON r.id = c.recording_id
               WHERE r.user_id = ? AND c.chunk_key = ? LIMIT 1""",
            (user_id, chunk_key),
        )
        if dup:
            rec = one("SELECT * FROM recordings WHERE id = ?", (dup["recording_id"],))
            again = all_(
                "SELECT seq, duration_ms, start_offset_ms FROM chunks WHERE recording_id = ?",
                (dup["recording_id"],),
            )
            total = max(
                ((int(c.get("start_offset_ms") or 0) + int(c.get("duration_ms") or 0)) for c in again),
                default=int(dup.get("duration_ms") or 0),
            )
            return {
                "ok": True,
                "recordingId": dup["recording_id"],
                "title": (rec or {}).get("title") or "",
                "chunkId": dup["id"],
                "chunkSeq": dup["seq"],
                "newSession": False,
                "gapSec": 0,
                "sessionChunks": len(again),
                "sessionDurationMs": total,
                "durationMs": int(dup.get("duration_ms") or 0),
                "duplicate": True,
            }

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
                   last_rec_end = NULL, session_chunks = 0 WHERE id = ?""",
                (device["id"],),
            )

    # 设备端录制时间。设备攒了一整天再补传时，几十个分片几秒钟内全部到达，
    # 按到达时间算间隔永远是 1~2 秒，会把全天并成一条记录 —— 所以有 recordedAt
    # 就按真实录制间隔切会话，没有才退回到达时间（老固件行为不变）。
    # 时钟偏差超过 30 天视为没校准，忽略，避免把会话切得乱七八糟。
    rec_at = _parse(recorded_at)
    if rec_at is not None and abs((now - rec_at).total_seconds()) > 30 * 86400:
        rec_at = None

    # 判断是不是新的一次「聊天」：距上一段太久就断开
    new_session = False
    gap_seconds = 0.0
    span = _chunk_gap(
        rec_at,
        _parse(device.get("last_rec_end")),
        _parse(device.get("last_chunk_at")),
        now,
    )
    if rec is None:
        new_session = True
    elif span is None:
        new_session = True
    else:
        gap_seconds = max(0.0, span)
        if gap_seconds > gap:
            close_session(device, reason="gap")
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
    # 本片"录制结束时刻"：有录制时间就用它，否则退回服务器到达时间。
    # 记录时间一律用录制时间，补传一整天的音频也能对上正确的日期。
    chunk_end = rec_at + timedelta(milliseconds=real_duration) if rec_at else now
    chunk_end_iso = chunk_end.isoformat()
    chunk_id = uid("chk_")
    execute(
        """INSERT INTO chunks
           (id, recording_id, seq, filename, path, mime, size, duration_ms,
            start_offset_ms, status, created_at, updated_at, chunk_key)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?, ?)""",
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
            chunk_key,
        ),
    )

    total_ms = offset + real_duration
    execute(
        """UPDATE recordings SET status = 'processing', duration_ms = ?,
           ended_at = ? WHERE id = ?""",
        (total_ms, chunk_end_iso, rec["id"]),
    )
    execute(
        """UPDATE devices SET open_recording_id = ?, last_chunk_at = ?, last_seen_at = ?,
           last_rec_end = ?, session_chunks = COALESCE(session_chunks, 0) + 1 WHERE id = ?""",
        (rec["id"], t, t, chunk_end_iso if rec_at else None, device["id"]),
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
