"""本地说话人分离推理（sherpa-onnx）。

pyannote 分割模型负责切出发言区间，3D-Speaker 声纹嵌入负责判断两个区间是不是
同一个人，最后用 FastClustering 聚成若干说话人。整套纯 ONNX，在 CPU 上跑，
音频不出本机。

这个模块只做推理，不碰数据库；主进程通过 ``local_asr_worker`` 的独立进程调用，
避免 native 层卡死把转写队列带停。
"""

from __future__ import annotations

import wave

import numpy as np
import sherpa_onnx

SAMPLE_RATE = 16000
# 每个说话人最多取 60 秒音频算声纹，够稳又不会让长会议变慢
MAX_EMBED_MS = 60_000
# 少于 2 秒的聚类不生成质心，避免把噪声当成角色模板
MIN_EMBED_MS = 2_000


def _speaker_embeddings(samples: np.ndarray, segments: list[dict], emb_model: str, threads: int) -> list[dict]:
    """按说话人把发言区间拼起来，算一个归一化声纹质心。"""
    extractor = sherpa_onnx.SpeakerEmbeddingExtractor(
        sherpa_onnx.SpeakerEmbeddingExtractorConfig(
            model=str(emb_model),
            num_threads=threads,
            provider="cpu",
        )
    )
    groups: dict[int, list[tuple[int, int]]] = {}
    for s in segments:
        start = max(0, int(s["startMs"] * SAMPLE_RATE / 1000))
        end = max(0, int(s["endMs"] * SAMPLE_RATE / 1000))
        if end > start:
            groups.setdefault(int(s["speaker"]), []).append((start, end))

    result: list[dict] = []
    max_samples = int(MAX_EMBED_MS * SAMPLE_RATE / 1000)
    for speaker, spans in groups.items():
        pieces: list[np.ndarray] = []
        total = 0
        for start, end in spans:
            start = min(start, len(samples))
            end = min(end, len(samples))
            if end <= start:
                continue
            take = min(end - start, max_samples - total)
            if take <= 0:
                break
            pieces.append(samples[start : start + take])
            total += take
        if total < int(MIN_EMBED_MS * SAMPLE_RATE / 1000) or not pieces:
            continue

        stream = extractor.create_stream()
        stream.accept_waveform(SAMPLE_RATE, np.concatenate(pieces).astype(np.float32))
        stream.input_finished()
        if not extractor.is_ready(stream):
            continue
        vec = np.asarray(extractor.compute(stream), dtype=np.float32)
        norm = float(np.linalg.norm(vec))
        if norm <= 0:
            continue
        vec = vec / norm
        result.append(
            {
                "speaker": speaker,
                # 保留 6 位小数，JSON 体积小且对余弦相似度没有影响
                "embedding": [round(float(x), 6) for x in vec],
                "speechMs": int(total * 1000 // SAMPLE_RATE),
            }
        )
    return result


def diarize_file(req: dict) -> dict:
    """对一段 16kHz 单声道 WAV 做说话人分离。

    req 字段：
      file: WAV 路径
      models: {"segmentation": ..., "embedding": ...}
      numThreads / threshold / numClusters / minDurationOn / minDurationOff
    """
    path = str(req.get("file") or "")
    models = req.get("models") or {}
    seg_model = str(models.get("segmentation") or "")
    emb_model = str(models.get("embedding") or "")
    if not path or not seg_model or not emb_model:
        raise RuntimeError("说话人分离缺少音频或模型路径")

    with wave.open(path, "rb") as w:
        if w.getnchannels() != 1 or w.getsampwidth() != 2:
            raise RuntimeError("说话人分离只接受 16kHz 单声道 PCM WAV")
        sample_rate = w.getframerate()
        samples = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768.0

    threads = int(req.get("numThreads") or 2)
    threshold = float(req.get("threshold") or 0.7)
    clusters = int(req.get("numClusters") or -1)

    config = sherpa_onnx.OfflineSpeakerDiarizationConfig(
        segmentation=sherpa_onnx.OfflineSpeakerSegmentationModelConfig(
            pyannote=sherpa_onnx.OfflineSpeakerSegmentationPyannoteModelConfig(model=seg_model),
            num_threads=threads,
            provider="cpu",
        ),
        embedding=sherpa_onnx.SpeakerEmbeddingExtractorConfig(
            model=emb_model,
            num_threads=threads,
            provider="cpu",
        ),
        clustering=sherpa_onnx.FastClusteringConfig(num_clusters=clusters, threshold=threshold),
        min_duration_on=float(req.get("minDurationOn") or 0.3),
        min_duration_off=float(req.get("minDurationOff") or 0.5),
    )
    if not config.validate():
        raise RuntimeError("说话人分离模型配置无效，请检查模型文件是否完整")

    diarizer = sherpa_onnx.OfflineSpeakerDiarization(config)
    if int(diarizer.sample_rate) != int(sample_rate):
        raise RuntimeError(f"采样率不匹配：模型要求 {diarizer.sample_rate}，音频是 {sample_rate}")

    result = diarizer.process(samples)
    segments = [
        {
            "speaker": int(s.speaker),
            "startMs": max(0, int(round(float(s.start) * 1000))),
            "endMs": max(0, int(round(float(s.end) * 1000))),
        }
        for s in result.sort_by_start_time()
    ]

    # 聚类编号可能不连续（如 0/2/5），按首次出现顺序重排成 0..N-1，界面上才是「说话人 1/2/…」
    order: list[int] = []
    seen: set[int] = set()
    for s in segments:
        spk = int(s["speaker"])
        if spk not in seen:
            seen.add(spk)
            order.append(spk)
    remap = {old: i for i, old in enumerate(order)}
    for s in segments:
        s["speaker"] = remap[int(s["speaker"])]

    # 声纹质心算不出来不影响分离结果，只是不能事后点名/判断「我是否参与」
    speakers: list[dict] = []
    try:
        speakers = _speaker_embeddings(samples, segments, emb_model, threads)
    except Exception:  # noqa: BLE001 - 纯增强信息，失败就跳过
        speakers = []

    return {
        "numSpeakers": int(result.num_speakers),
        "numSegments": int(result.num_segments),
        "segments": segments,
        "speakers": speakers,
    }
