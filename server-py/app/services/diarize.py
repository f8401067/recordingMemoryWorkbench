"""录音转写完成后的说话人分离。

流程：
  1. 把这条录音的所有分片按 start_offset_ms 拼成一条 16kHz 单声道 WAV
     （分片之间的空档补静音，保证时间轴和屏幕上看到的一致）
  2. 交给独立推理进程跑 pyannote 分割 + 3D-Speaker 聚类
  3. 按时间重叠给每个转写句子选一个说话人，写回 chunks.segments 的 ``speaker`` 字段

只处理本地引擎产出的录音；模型没下载或用户没开开关时直接跳过，不影响转写和纪要。
"""

from __future__ import annotations

import json
import shutil
import tempfile
import wave
from typing import Any

from ..audio import to_pcm16k
from ..config import TMP_DIR
from ..db import all_, execute, now_iso, one
from ..jobs import enqueue
from ..logbuf import log
from ..providers.local_asr import remote_diarize
from . import local_asr_models as store
from .settings import read_settings

SAMPLE_RATE = 16000
BYTES_PER_MS = SAMPLE_RATE // 1000 * 2  # 16 位单声道：32 字节/毫秒


def _build_wav(chunks: list[dict], path: str) -> tuple[int, int]:
    """按分片偏移拼出完整 WAV，返回 (总时长 ms, 实际参与的分片数)。"""
    pieces: list[tuple[int, bytes]] = []
    end_byte = 0
    total_ms = 0

    for c in chunks:
        offset_ms = int(c.get("start_offset_ms") or 0)
        try:
            pcm, _ = to_pcm16k(c["path"])
        except Exception as e:  # noqa: BLE001 - 单个分片坏了不该拖垮整条
            log("warn", "diarize", f"分片 {c.get('seq')} 解码失败，已跳过：{e}")
            continue
        if not pcm:
            continue
        start_byte = offset_ms * BYTES_PER_MS
        end = start_byte + len(pcm)
        pieces.append((start_byte, pcm))
        end_byte = max(end_byte, end)
        total_ms = max(total_ms, offset_ms + int(c.get("duration_ms") or 0))

    if not pieces or end_byte <= 0:
        return 0, 0

    # 时间轴按实际音频长度兜底，duration_ms 记小了也不会截掉尾巴
    total_ms = max(total_ms, end_byte // BYTES_PER_MS)
    buf = bytearray(end_byte)
    for start, pcm in pieces:
        buf[start : start + len(pcm)] = pcm

    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(bytes(buf))
    return total_ms, len(pieces)


def _pick_speaker(intervals: list[dict], start_ms: int, end_ms: int) -> int | None:
    """和句子时间重叠最多的说话人；完全不重叠时取时间上最近的那个。"""
    if not intervals:
        return None
    if end_ms <= start_ms:
        end_ms = start_ms + 1

    best: int | None = None
    best_overlap = 0
    best_center_gap = None
    center = (start_ms + end_ms) / 2

    for it in intervals:
        overlap = min(end_ms, it["endMs"]) - max(start_ms, it["startMs"])
        gap = abs((it["startMs"] + it["endMs"]) / 2 - center)
        if overlap > best_overlap:
            best_overlap = overlap
            best = it["speaker"]
        if best_overlap == 0 and (best_center_gap is None or gap < best_center_gap):
            best_center_gap = gap
            best = it["speaker"]

    return best


def _apply_speakers(recording_id: str, intervals: list[dict]) -> None:
    """把说话人编号（1 起）写进每个分片的 segments。"""
    from .transcribe import parse_segments

    chunks = all_(
        "SELECT * FROM chunks WHERE recording_id = ? ORDER BY seq ASC",
        (recording_id,),
    )
    for c in chunks:
        segs = parse_segments(c)
        if not segs:
            continue
        for s in segs:
            start = int(s.get("startMs") or 0)
            end = int(s.get("endMs") or start)
            s.pop("speaker", None)
            s.pop("speakerName", None)
            s.pop("speakerIsSelf", None)
            spk = _pick_speaker(intervals, start, end)
            if spk is not None:
                s["speaker"] = spk + 1  # 从 1 开始，显示为「说话人 1」
        execute(
            "UPDATE chunks SET segments = ?, updated_at = ? WHERE id = ?",
            (json.dumps(segs, ensure_ascii=False), now_iso(), c["id"]),
        )


def handle_diarize_job(payload: dict) -> None:
    recording_id = payload.get("recordingId")
    if not recording_id:
        return

    rec = one("SELECT * FROM recordings WHERE id = ?", (recording_id,))
    if not rec:
        return
    if rec.get("diarized_at"):
        return

    settings = read_settings(rec["user_id"])
    if not settings.get("speakerDiarization"):
        return

    models = store.diarization_models()
    if not models:
        log("warn", "diarize", "说话人分离模型还没下载，跳过本次")
        return

    chunks = all_(
        """SELECT * FROM chunks WHERE recording_id = ? AND status = 'done'
           ORDER BY seq ASC""",
        (recording_id,),
    )
    if not chunks:
        return

    td = tempfile.mkdtemp(dir=TMP_DIR)
    try:
        wav_path = f"{td}/input.wav"
        duration_ms, used = _build_wav(chunks, wav_path)
        if not used or duration_ms < 1000:
            log("info", "diarize", f"{recording_id} 没有可用音频，跳过")
            return

        log("info", "diarize", f"{recording_id} 开始分离（{len(chunks)} 片 / {duration_ms // 1000}s）")
        result = remote_diarize(
            models=models,
            file_path=wav_path,
            duration_ms=duration_ms,
            num_threads=int(settings.get("diarizationThreads") or 2),
            threshold=float(settings.get("diarizationThreshold") or 0.7),
        )
    finally:
        # macOS 上 unlink 偶尔会返回 Errno 18，删不掉不能把已经成功的分离结果一起丢掉
        shutil.rmtree(td, ignore_errors=True)

    intervals = result.get("segments") or []
    speaker_count = int(result.get("numSpeakers") or 0)
    centroids = result.get("speakers") or []

    # 单人录音也保留 speaker 编号：认出来是「我」时可以显示名字
    _apply_speakers(recording_id, intervals)

    execute(
        """UPDATE recordings
           SET diarized_at = ?, speaker_count = ?, speaker_embeddings = ?,
               speaker_matches = NULL, user_present = NULL, user_similarity = NULL
           WHERE id = ?""",
        (
            now_iso(),
            speaker_count,
            json.dumps(centroids, ensure_ascii=False) if centroids else None,
            recording_id,
        ),
    )
    # 用刚存下的声纹质心匹配「我」和已录入的角色；没有角色时只留质心，供事后标记
    from .voice_profiles import apply_recording

    apply_recording(recording_id, rec["user_id"])
    log("info", "diarize", f"{recording_id} 完成，共 {speaker_count} 个说话人")


def enqueue_diarize(recording_id: str) -> None:
    """避免重复入队：同一条录音已经有排队/在跑的任务就不再加。"""
    exists = one(
        """SELECT 1 AS x FROM jobs
           WHERE type = 'diarize' AND status IN ('pending', 'running')
             AND payload LIKE ?""",
        (f"%{recording_id}%",),
    )
    if exists:
        return
    enqueue("diarize", {"recordingId": recording_id}, max_attempts=2)
