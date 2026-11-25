from __future__ import annotations

import base64
import json
import threading
import time
import uuid
from pathlib import Path
from typing import Any, TypedDict

import httpx
import websocket

from ..audio import has_ffmpeg, probe_duration_ms, to_pcm16k, to_wav16k

ASR_TIMEOUT = httpx.Timeout(180.0, connect=20.0)


class Segment(TypedDict):
    startMs: int
    endMs: int
    text: str


class AsrResult(TypedDict):
    text: str
    segments: list[Segment]
    meta: dict[str, Any]


def _params(provider: dict) -> dict:
    raw = provider.get("params")
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}


def guess_mime(ext: str) -> str:
    return {
        ".webm": "audio/webm",
        ".m4a": "audio/mp4",
        ".mp4": "audio/mp4",
        ".mp3": "audio/mpeg",
        ".wav": "audio/wav",
        ".ogg": "audio/ogg",
        ".aac": "audio/aac",
    }.get(ext.lower(), "application/octet-stream")


class AsrEngine:
    def transcribe(
        self,
        file_path: str,
        mime: str | None = None,
        language: str | None = None,
        duration_ms: int | None = None,
    ) -> AsrResult:
        raise NotImplementedError


class ParaformerRealtime(AsrEngine):
    """阿里百炼 Paraformer 实时识别：WebSocket 推送本地 PCM，返回逐句时间戳。"""

    def __init__(self, provider: dict) -> None:
        self.p = provider

    def transcribe(self, file_path, mime=None, language=None, duration_ms=None) -> AsrResult:
        api_key = (self.p.get("api_key") or "").strip()
        if not api_key:
            raise RuntimeError("缺少 DashScope API Key")
        if not has_ffmpeg():
            raise RuntimeError("未检测到 ffmpeg，请先安装：brew install ffmpeg")

        cfg = _params(self.p)
        model = self.p.get("model") or "paraformer-realtime-v2"
        url = cfg.get("wsUrl") or "wss://dashscope.aliyuncs.com/api-ws/v1/inference"

        pcm, audio_ms = to_pcm16k(file_path)
        lang = [language] if language and language != "auto" else ["zh", "en"]
        task_id = str(uuid.uuid4())

        sentences: dict[str, Segment] = {}
        finished = threading.Event()
        state: dict[str, Any] = {"error": None}
        started = threading.Event()

        ws = websocket.create_connection(
            url,
            header=[
                f"Authorization: bearer {api_key}",
                "X-DashScope-DataInspection: enable",
            ],
            timeout=20,
            enable_multithread=True,
        )
        ws.settimeout(max(90.0, audio_ms / 1000 * 3 + 45))

        def sender() -> None:
            try:
                started.wait(60)
                if finished.is_set():
                    return
                for offset in range(0, len(pcm), 3200):
                    if finished.is_set():
                        return
                    ws.send_binary(pcm[offset: offset + 3200])
                    time.sleep(0.02)
                ws.send(json.dumps({
                    "header": {"action": "finish-task", "task_id": task_id},
                }))
            except Exception as e:  # noqa: BLE001
                state["error"] = str(e)

        try:
            ws.send(json.dumps({
                "header": {"action": "run-task", "task_id": task_id, "streaming": "duplex"},
                "payload": {
                    "task_group": "audio",
                    "task": "asr",
                    "function": "recognition",
                    "model": model,
                    "parameters": {
                        "format": "pcm",
                        "sample_rate": 16000,
                        "language_hints": lang,
                        "disfluency_removal_enabled": False,
                        "punctuation_prediction_enabled": True,
                        "inverse_text_normalization_enabled": True,
                    },
                    "input": {},
                },
            }))

            threading.Thread(target=sender, daemon=True).start()

            while not finished.is_set():
                try:
                    raw = ws.recv()
                except websocket.WebSocketTimeoutException:
                    raise RuntimeError(f"ASR 超时（{round(audio_ms / 1000)}s 音频）")
                if raw is None or raw == "":
                    break
                if isinstance(raw, bytes):
                    continue
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue

                event = (msg.get("header") or {}).get("event")
                if event == "task-started":
                    started.set()
                elif event == "result-generated":
                    sentence = ((msg.get("payload") or {}).get("output") or {}).get("sentence") or {}
                    sid = str(sentence.get("sentence_id") or f"{sentence.get('begin_time')}-{sentence.get('end_time')}")
                    sentences[sid] = {
                        "startMs": int(sentence.get("begin_time") or 0),
                        "endMs": int(sentence.get("end_time") or 0),
                        "text": str(sentence.get("text") or ""),
                    }
                elif event == "task-finished":
                    finished.set()
                elif event == "task-failed":
                    err = (msg.get("header") or {}).get("error_message") or "ASR 任务失败"
                    raise RuntimeError(err)
        finally:
            finished.set()
            try:
                ws.close()
            except Exception:  # noqa: BLE001
                pass

        if state["error"] and not sentences:
            raise RuntimeError(f"ASR 推送失败：{state['error']}")

        items = sorted(
            (s for s in sentences.values() if s["text"].strip()),
            key=lambda s: s["startMs"],
        )
        return {
            "text": "".join(s["text"] for s in items),
            "segments": items,
            "meta": {"engine": "paraformer-realtime", "model": model},
        }


class DashscopeQwenAsr(AsrEngine):
    """Qwen3-ASR 同步接口：整段音频 base64 提交，准确率高但无句级时间戳。"""

    def __init__(self, provider: dict) -> None:
        self.p = provider

    def transcribe(self, file_path, mime=None, language=None, duration_ms=None) -> AsrResult:
        api_key = (self.p.get("api_key") or "").strip()
        if not api_key:
            raise RuntimeError("缺少 DashScope API Key")
        if not has_ffmpeg():
            raise RuntimeError("未检测到 ffmpeg，请先安装：brew install ffmpeg")

        wav, wav_ms = to_wav16k(file_path)
        base = (self.p.get("base_url") or "https://dashscope.aliyuncs.com").rstrip("/")
        cfg = _params(self.p)
        model = self.p.get("model") or "qwen3-asr-flash"

        asr_options = {"enable_lid": True, "enable_itn": True}
        asr_options.update(cfg.get("asrOptions") or {})
        if language and language != "auto":
            asr_options["language"] = language

        body = {
            "model": model,
            "input": {
                "messages": [
                    {"role": "system", "content": [{"text": ""}]},
                    {
                        "role": "user",
                        "content": [
                            {"audio": "data:audio/wav;base64," + base64.b64encode(wav).decode()}
                        ],
                    },
                ]
            },
            "parameters": {"asr_options": asr_options},
        }

        with httpx.Client(timeout=ASR_TIMEOUT) as client:
            res = client.post(
                f"{base}/api/v1/services/aigc/multimodal-generation/generation",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json=body,
            )
        if res.status_code >= 400:
            raise RuntimeError(f"ASR 请求失败 {res.status_code}：{res.text[:300]}")

        data = res.json()
        choices = (((data.get("output") or {}).get("choices")) or [])
        content = (((choices[0] if choices else {}).get("message")) or {}).get("content")
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            text = "".join(str(c.get("text") or "") for c in content if isinstance(c, dict))
        else:
            text = ""
        text = text.strip()
        segments: list[Segment] = (
            [{"startMs": 0, "endMs": wav_ms, "text": text}] if text else []
        )
        return {
            "text": text,
            "segments": segments,
            "meta": {"engine": "qwen3-asr", "model": model, "usage": data.get("usage")},
        }


class OpenAiCompatibleAsr(AsrEngine):
    """任意 OpenAI 兼容 /audio/transcriptions 接口。

    云端网关要 Key；本机跑的 faster-whisper-server / speaches / FunASR 一般不需要，
    所以 Key 为空时只是不带 Authorization 头，不报错。
    """

    def __init__(self, provider: dict) -> None:
        self.p = provider

    def transcribe(self, file_path, mime=None, language=None, duration_ms=None) -> AsrResult:
        api_key = (self.p.get("api_key") or "").strip()

        base = (self.p.get("base_url") or "https://api.openai.com/v1").rstrip("/")
        root = base if base.endswith("/v1") else f"{base}/v1"
        cfg = _params(self.p)
        path = Path(file_path)
        data = path.read_bytes()

        form: dict[str, Any] = {
            "model": self.p.get("model") or "whisper-1",
            "response_format": cfg.get("responseFormat") or "verbose_json",
        }
        if language and language != "auto":
            form["language"] = language

        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        with httpx.Client(timeout=ASR_TIMEOUT) as client:
            res = client.post(
                f"{root}/audio/transcriptions",
                headers=headers,
                files={"file": (path.name, data, mime or guess_mime(path.suffix))},
                data=form,
            )
        if res.status_code >= 400:
            raise RuntimeError(f"ASR 请求失败 {res.status_code}：{res.text[:300]}")

        try:
            payload = res.json()
        except ValueError:
            return {"text": res.text.strip(), "segments": [], "meta": {"engine": "openai-compatible"}}

        segments: list[Segment] = []
        for s in payload.get("segments") or []:
            segments.append({
                "startMs": round(float(s.get("start") or 0) * 1000),
                "endMs": round(float(s.get("end") or 0) * 1000),
                "text": str(s.get("text") or "").strip(),
            })
        return {
            "text": str(payload.get("text") or "").strip(),
            "segments": segments,
            "meta": {"engine": "openai-compatible", "model": self.p.get("model")},
        }


DEMO_LINES = [
    "这是一段由演示引擎生成的转写结果。",
    "配置真实的语音识别服务商之后，这里会显示真实的录音内容。",
    "当前使用的是模拟数据，用于验证上传、转写、纪要和思维导图的完整链路。",
]


class MockAsr(AsrEngine):
    def transcribe(self, file_path, mime=None, language=None, duration_ms=None) -> AsrResult:
        duration = duration_ms or probe_duration_ms(file_path) or 6000
        per = max(1500, duration // len(DEMO_LINES))
        segments: list[Segment] = [
            {
                "startMs": i * per,
                "endMs": min(duration, (i + 1) * per),
                "text": text,
            }
            for i, text in enumerate(DEMO_LINES)
        ]
        time.sleep(0.4)
        return {
            "text": "".join(s["text"] for s in segments),
            "segments": segments,
            "meta": {"engine": "mock"},
        }


def create_asr_engine(provider: dict) -> AsrEngine:
    kind = provider.get("kind")
    if kind == "dashscope-paraformer":
        return ParaformerRealtime(provider)
    if kind == "dashscope-qwen-asr":
        return DashscopeQwenAsr(provider)
    if kind == "openai-whisper":
        return OpenAiCompatibleAsr(provider)
    if kind in ("local-sherpa-onnx", "local-faster-whisper", "local-whisper-cpp", "local-mlx-qwen3-asr"):
        # 延迟导入：没装本地依赖 / 没装 whisper.cpp 的用户不该在启动时被拖累
        from .local_asr import FasterWhisperLocal, MlxQwen3AsrLocal, SherpaOnnxLocal, WhisperCppLocal

        if kind == "local-sherpa-onnx":
            return SherpaOnnxLocal(provider)
        if kind == "local-faster-whisper":
            return FasterWhisperLocal(provider)
        if kind == "local-mlx-qwen3-asr":
            return MlxQwen3AsrLocal(provider)
        return WhisperCppLocal(provider)
    if kind == "mock":
        return MockAsr()
    raise RuntimeError(f"未知的 ASR 引擎类型：{kind}")


ASR_KINDS = [
    {
        "kind": "local-sherpa-onnx",
        "label": "本地 · sherpa-onnx（Qwen3-ASR / SenseVoice）",
        "hint": "国产开源中文模型，离线跑、音频不出本机。Qwen3-ASR 中文最准，SenseVoice 最快。",
        "requiresKey": False,
        "requiresBaseUrl": False,
        "localBackend": "sherpa-onnx",
    },
    {
        "kind": "local-mlx-qwen3-asr",
        "label": "本地 · MLX Qwen3-ASR 1.7B（Apple Silicon）",
        "hint": "Apple Silicon 专属：mlx-community 的 6bit 量化权重走 Metal，中文准确率比 0.6B 高一档。",
        "requiresKey": False,
        "requiresBaseUrl": False,
        "localBackend": "mlx-qwen3-asr",
    },
    {
        "kind": "local-faster-whisper",
        "label": "本地 · faster-whisper（离线开源）",
        "hint": "音频不出本机。pip 装一个包即可，CPU 能跑，模型在设置页一键下载。",
        "requiresKey": False,
        "requiresBaseUrl": False,
        "localBackend": "faster-whisper",
    },
    {
        "kind": "local-whisper-cpp",
        "label": "本地 · whisper.cpp（离线开源）",
        "hint": "单文件二进制，Apple Silicon 走 Metal、N 卡走 CUDA，速度最快。需先装 whisper-cli。",
        "requiresKey": False,
        "requiresBaseUrl": False,
        "localBackend": "whisper-cpp",
    },
    {
        "kind": "dashscope-paraformer",
        "label": "阿里百炼 · Paraformer 实时",
        "hint": "逐句时间戳，流式推送本地音频，无需公网地址。云端方案里延迟最好。",
    },
    {
        "kind": "dashscope-qwen-asr",
        "label": "阿里百炼 · Qwen3-ASR",
        "hint": "同步接口，识别准确率高，无句级时间戳。",
    },
    {
        "kind": "openai-whisper",
        "label": "OpenAI 兼容 /audio/transcriptions",
        "hint": "云端网关要 Key；本机跑的 faster-whisper-server / speaches 可留空。",
    },
    {
        "kind": "mock",
        "label": "演示引擎（模拟数据）",
        "hint": "不调用外部服务，用于验证整体流程。",
    },
]
