from __future__ import annotations

import json
import shutil
from typing import Any, TypedDict

from ..bus import emit_recording
from ..config import UPLOADS_DIR
from ..db import all_, execute, now_iso, one
from ..jobs import enqueue
from .providers import build_asr, resolve_asr
from .settings import read_settings
from .transcript_value import evaluate_transcript, should_auto_discard


class Segment(TypedDict):
    startMs: int
    endMs: int
    text: str


def parse_segments(chunk: dict) -> list[dict]:
    raw = chunk.get("segments")
    if not raw:
        return []
    try:
        arr = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []
    return arr if isinstance(arr, list) else []


def transcript_for(recording_id: str) -> dict[str, Any]:
    chunks = all_("SELECT * FROM chunks WHERE recording_id = ? ORDER BY seq ASC", (recording_id,))
    segments: list[dict] = []
    for c in chunks:
        segments.extend(parse_segments(c))
    segments.sort(key=lambda s: s.get("startMs") or 0)
    text = "\n".join(str(s.get("text") or "") for s in segments)
    done = sum(1 for c in chunks if c["status"] == "done")
    failed = sum(1 for c in chunks if c["status"] == "failed")
    return {
        "segments": segments,
        "text": text,
        "chunkCount": len(chunks),
        "doneChunks": done,
        "failedChunks": failed,
    }


def transcript_with_timestamps(recording_id: str) -> str:
    """把转写渲染成带时间戳的纯文本，供 LLM 阅读。"""
    segs = transcript_for(recording_id)["segments"]
    return "\n".join(f"[{fmt_clock(s.get('startMs') or 0)}] {s.get('text') or ''}" for s in segs)


def fmt_clock(ms: float) -> str:
    total = max(0, int(ms // 1000))
    h = total // 3600
    m = (total % 3600) // 60
    s = total % 60
    mm = f"{m:02d}"
    ss = f"{s:02d}"
    return f"{h}:{mm}:{ss}" if h > 0 else f"{mm}:{ss}"


# ------------------------------------------------------------------ #
# 转写任务
# ------------------------------------------------------------------ #
def handle_transcribe_job(payload: dict) -> None:
    chunk_id = payload.get("chunkId")
    chunk = one("SELECT * FROM chunks WHERE id = ?", (chunk_id,))
    if not chunk:
        return  # 已被删除
    if chunk["status"] == "done":
        return

    rec = one("SELECT * FROM recordings WHERE id = ?", (chunk["recording_id"],))
    if not rec:
        return

    execute(
        "UPDATE chunks SET status = 'transcribing', updated_at = ? WHERE id = ?",
        (now_iso(), chunk["id"]),
    )
    emit_recording(rec["id"], "chunk", {"chunkId": chunk["id"], "seq": chunk["seq"], "status": "transcribing"})

    provider = resolve_asr(rec["user_id"], rec.get("asr_provider_id"))
    engine = build_asr(provider)

    try:
        res = engine.transcribe(
            chunk["path"],
            chunk.get("mime"),
            rec.get("language"),
            chunk.get("duration_ms") or None,
        )

        offset = chunk.get("start_offset_ms") or 0
        segments: list[dict] = []
        for s in res.get("segments") or []:
            start = int(s.get("startMs") or 0)
            end = int(s.get("endMs") or start)
            segments.append(
                {
                    "startMs": start + offset,
                    "endMs": end + offset,
                    "text": str(s.get("text") or "").strip(),
                }
            )

        execute(
            """UPDATE chunks SET status = 'done', transcript = ?, segments = ?, error = NULL,
               updated_at = ? WHERE id = ?""",
            (res.get("text") or "", json.dumps(segments, ensure_ascii=False), now_iso(), chunk["id"]),
        )
        emit_recording(
            rec["id"],
            "chunk",
            {
                "chunkId": chunk["id"],
                "seq": chunk["seq"],
                "status": "done",
                "preview": (res.get("text") or "")[:120],
            },
        )
    except Exception as e:  # noqa: BLE001
        msg = str(e)[:500]
        execute(
            "UPDATE chunks SET status = 'failed', error = ?, updated_at = ? WHERE id = ?",
            (msg, now_iso(), chunk["id"]),
        )
        emit_recording(
            rec["id"],
            "chunk",
            {"chunkId": chunk["id"], "seq": chunk["seq"], "status": "failed", "error": msg},
        )
        finalize_recording(rec["id"])
        raise

    finalize_recording(rec["id"])


def discard_recording(recording_id: str) -> None:
    """彻底删掉一条录音：分片、纪要、上传目录一起清。手动删除与自动丢弃共用。"""
    execute("DELETE FROM summaries WHERE recording_id = ?", (recording_id,))
    execute("DELETE FROM chunks WHERE recording_id = ?", (recording_id,))
    execute("DELETE FROM recordings WHERE id = ?", (recording_id,))
    shutil.rmtree(UPLOADS_DIR / recording_id, ignore_errors=True)
    emit_recording(recording_id, "discarded", {"id": recording_id})


def finalize_recording(recording_id: str, allow_merge: bool = True) -> None:
    """全部片子处理完 → 更新录音状态，必要时触发自动纪要。

    allow_merge=True 时，收尾后会检查有没有前后相隔不到 5 分钟的邻居，
    有就真合并成一条（见 services/merge.py），再由自动纪要产出一份覆盖整组的纪要。
    """
    rec = one("SELECT * FROM recordings WHERE id = ?", (recording_id,))
    if not rec:
        return

    chunks = all_("SELECT * FROM chunks WHERE recording_id = ?", (recording_id,))
    if not chunks:
        return

    # 设备会话还开着：分片之间可能有几秒到几分钟的间隔，不能因为"暂时没有待处理分片"
    # 就把录音判定为完成（否则会拆成多条记录，还会提前生成半截纪要）
    if one("SELECT 1 AS x FROM devices WHERE open_recording_id = ?", (recording_id,)):
        execute("UPDATE recordings SET status = 'processing' WHERE id = ?", (recording_id,))
        return

    busy = any(c["status"] in ("pending", "queued", "transcribing") for c in chunks)
    if busy:
        execute("UPDATE recordings SET status = 'processing' WHERE id = ?", (recording_id,))
        return

    any_done = any(c["status"] == "done" for c in chunks)
    status = "ready" if any_done else "failed"
    duration = 0
    for c in chunks:
        duration = max(duration, (c.get("start_offset_ms") or 0) + (c.get("duration_ms") or 0))

    ended_at = rec.get("ended_at") or now_iso()

    # 转写完了才谈得上"有没有内容"：每次 finalize 都重算一遍，重试/补充转写后会自动纠正
    segments = transcript_for(recording_id)["segments"] if status == "ready" else []
    low, reason = evaluate_transcript(segments) if status == "ready" else (False, None)

    # 设备常驻监听录到的纯杂音：转写确认没有人声后直接丢弃，不再进录音库。
    # 只对设备录音生效，网页/手动上传的内容不自动删，避免误删用户主动录的东西。
    if rec.get("source") == "device" and should_auto_discard(low, reason):
        discard_recording(recording_id)
        return

    execute(
        """UPDATE recordings SET status = ?, duration_ms = ?, ended_at = COALESCE(ended_at, ?),
           low_value = ?, low_reason = ?, value_checked = 1
           WHERE id = ?""",
        (
            status,
            duration,
            ended_at,
            1 if low else 0,
            reason,
            recording_id,
        ),
    )
    emit_recording(
        recording_id,
        "recording",
        {"status": status, "durationMs": duration, "lowValue": bool(low), "lowReason": reason},
    )

    if status != "ready":
        return

    # 前后相隔不到 5 分钟的碎片合成一条。合并后本条可能已被吸收掉，
    # 那种情况下由主记录的 finalize 统一走后面的自动纪要，这里直接收工。
    if allow_merge:
        from .merge import reconcile_after_finalize  # 延迟导入：两边互相依赖

        merged_id = reconcile_after_finalize(recording_id)
        if merged_id != recording_id:
            return

    # 空录音/只有噪声的话没必要再烧 token 生成纪要
    if low:  
        return

    settings = read_settings(rec["user_id"])

    # 本地说话人分离在纪要之前排队：只标说话人，不影响纪要生成
    if settings.get("speakerDiarization") and not rec.get("diarized_at"):
        from .diarize import enqueue_diarize

        enqueue_diarize(recording_id)

    if not settings.get("autoSummarize"):
        return
    if rec.get("summary_status") in ("ready", "generating", "queued"):
        return

    from .summarize import create_summary

    try:
        create_summary(
            userId=rec["user_id"],
            scope="recording",
            recordingId=recording_id,
            providerId=settings.get("summaryProviderId"),
            template=settings.get("summaryTemplate") or "meeting",
        )
    except Exception as e:  # noqa: BLE001
        # 自动纪要失败不应让转写任务整体失败
        print(f"[summarize] 自动生成纪要失败：{e}", flush=True)


def requeue_chunk(chunk_id: str) -> None:
    chunk = one("SELECT * FROM chunks WHERE id = ?", (chunk_id,))
    if not chunk:
        raise ValueError("分片不存在")
    execute(
        "UPDATE chunks SET status = 'queued', error = NULL, updated_at = ? WHERE id = ?",
        (now_iso(), chunk["id"]),
    )
    execute(
        "UPDATE recordings SET status = 'processing', summary_status = 'none', "
        "diarized_at = NULL, speaker_count = NULL, speaker_embeddings = NULL, "
        "speaker_matches = NULL, user_present = NULL, user_similarity = NULL WHERE id = ?",
        (chunk["recording_id"],),
    )
    emit_recording(
        chunk["recording_id"],
        "chunk",
        {"chunkId": chunk_id, "seq": chunk["seq"], "status": "queued"},
    )
    enqueue("transcribe", {"chunkId": chunk_id}, max_attempts=3)
