"""本地开源 ASR 引擎。

两条路线，用户按机器情况二选一：

- ``faster-whisper``：pip 安装的 CT2 推理（CPU int8 / CUDA），跨平台，模型自动缓存
- ``whisper.cpp``：调用本机 ``whisper-cli`` 二进制，Apple Silicon 用 Metal、N 卡用 CUDA

两者都完全离线，音频不出本机。模型统一由 ``services.local_asr_models`` 管理，
模型对象在进程内缓存，避免每片音频都重新加载几百 MB 权重。
"""

from __future__ import annotations

from contextlib import contextmanager

import json
import os
import queue
import re
import subprocess
import sys
import tempfile
import threading
from collections import deque
from pathlib import Path
from typing import Any

from ..audio import to_pcm16k, to_wav16k
from ..services import local_asr_models as store
from .asr import AsrEngine, AsrResult, Segment

# Whisper 系模型对中文常常输出繁体；带一句简体提示词能明显纠正
ZH_PROMPT = "以下是普通话的句子，请使用简体中文。"

# 本地推理很吃 CPU/内存，默认串行；确实想并行再调 LOCAL_ASR_CONCURRENCY
_LOCAL_SEM = threading.Semaphore(max(1, int(os.getenv("LOCAL_ASR_CONCURRENCY", "1"))))
_CPP_SEM = threading.Semaphore(max(1, int(os.getenv("LOCAL_ASR_CONCURRENCY", "1"))))

# 本地推理跑在 native 层，偶发死锁（ONNX Runtime 线程池 / sherpa-onnx 内部等待）。
# 无限期等锁会把 8 个 worker 全挂住、整个转写队列停摆，所以统一改成带超时获取：
# 超时就抛错，让任务走重试 / 失败，至少队列还能继续动。
LOCAL_LOCK_TIMEOUT = float(os.getenv("LOCAL_ASR_LOCK_TIMEOUT", "300"))


# ------------------------------------------------------------------ #
# 进程隔离：把推理放进独立进程
#
# 本地引擎在 native 层会偶发死锁（ONNX Runtime 线程池挂起）。死锁时主进程里那几个
# 线程永远卡住，而 Python 杀不掉一个卡在 C 扩展里的线程 —— 转写队列就整个停摆。
# 放进独立进程后，主进程只发请求、等结果：超时就 kill 整个 worker，下次自动重新拉起
# （代价是重新加载一次模型）。可用 LOCAL_ASR_ISOLATE=0 退回进程内推理。
# ------------------------------------------------------------------ #
ISOLATED = os.getenv("LOCAL_ASR_ISOLATE", "1").lower() not in ("0", "false", "no")
SERVER_ROOT = str(Path(__file__).resolve().parents[2])  # server-py/
WORKER_TIMEOUT = float(os.getenv("LOCAL_ASR_TIMEOUT", "180"))


class _LocalWorker:
    """一个常驻推理子进程，请求串行发、结果按序回。"""

    def __init__(self, provider: dict) -> None:
        self.provider = provider
        self.proc: subprocess.Popen | None = None
        self.q: queue.Queue | None = None
        self.lock = threading.Lock()
        # 子进程的 stderr 必须收着：它一崩（导入失败、缺库、native 段错误前的报错）
        # 只在 stderr 里有线索，直接继承父进程的话会混进服务日志，用户看到的永远
        # 只有一句「意外退出（code=1）」，根本没法排查。
        self.err: deque[str] = deque(maxlen=60)

    def _spawn(self) -> None:
        # PYTHONPATH 一定要清掉：某些环境（AI 助手的 shell 注入、容器 entrypoint）
        # 会把它指向带 sitecustomize.py 的目录，那玩意会挂 os.remove 之类的钩子。
        # 推理进程每次都要清理临时 pcm，被钩子拦住就直接 exit(1)，
        # 表现就是「子进程意外退出（code=1）」，而手工跑又完全正常，极难排查。
        env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "app.providers.local_asr_worker"],
            cwd=SERVER_ROOT,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            errors="replace",
        )
        q: queue.Queue = queue.Queue()
        self.q = q
        proc = self.proc
        self.err.clear()

        def _pump() -> None:
            try:
                assert proc.stdout is not None
                for line in proc.stdout:
                    q.put(line)
            except Exception:  # noqa: BLE001
                pass
            q.put(None)  # EOF

        def _pump_err() -> None:
            try:
                assert proc.stderr is not None
                for line in proc.stderr:
                    self.err.append(line.rstrip())
            except Exception:  # noqa: BLE001
                pass

        threading.Thread(target=_pump, daemon=True, name="local-asr-stdout").start()
        threading.Thread(target=_pump_err, daemon=True, name="local-asr-stderr").start()

    def _tail(self, n: int = 8) -> str:
        """取子进程 stderr 的最后几行，压成一行给用户看。"""
        lines = [x for x in list(self.err) if x.strip()][-n:]
        return " / ".join(lines)[-800:]

    def _alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def kill(self) -> None:
        if self.proc is not None:
            if self.proc.poll() is None:
                try:
                    self.proc.kill()
                    self.proc.wait(timeout=5)
                except Exception:  # noqa: BLE001
                    pass
        self.proc = None
        self.q = None

    def call(self, req: dict, timeout: float) -> dict:
        with self.lock:
            if not self._alive():
                self._spawn()
            assert self.proc is not None and self.proc.stdin is not None and self.q is not None
            try:
                self.proc.stdin.write(json.dumps(req, ensure_ascii=False) + "\n")
                self.proc.stdin.flush()
            except (BrokenPipeError, OSError, ValueError) as e:
                self.kill()
                raise RuntimeError(f"本地 ASR 子进程通信失败：{e}") from e

            try:
                line = self.q.get(timeout=timeout)
            except queue.Empty as e:
                tail = self._tail()
                self.kill()
                raise RuntimeError(
                    f"本地 ASR 推理超过 {int(timeout)}s 没返回，已杀掉引擎进程，下一次会自动重启"
                    + (f"；崩溃输出：{tail}" if tail else "")
                ) from e

            if line is None:
                code = self.proc.poll()
                tail = self._tail()
                self.kill()
                raise RuntimeError(
                    f"本地 ASR 子进程意外退出（code={code}）"
                    + (f"：{tail}" if tail else "（子进程没有留下任何输出）")
                )

        data = json.loads(line)
        if not data.get("ok"):
            raise RuntimeError(str(data.get("error") or "本地转写失败"))
        return dict(data.get("result") or {})


_WORKERS: dict[str, _LocalWorker] = {}
_WORKERS_LOCK = threading.Lock()


def _worker_for(provider: dict) -> _LocalWorker:
    key = "|".join(
        str(provider.get(k) or "") for k in ("id", "kind", "model")
    )
    with _WORKERS_LOCK:
        w = _WORKERS.get(key)
        if w is None:
            w = _LocalWorker(provider)
            _WORKERS[key] = w
        return w


def remote_transcribe(provider: dict, file_path, mime=None, language=None, duration_ms=None) -> AsrResult:
    dur_s = float(duration_ms or 0) / 1000
    timeout = max(WORKER_TIMEOUT, dur_s * 10 + 60)
    req = {
        "provider": provider,
        "file": str(file_path),
        "mime": mime,
        "language": language,
        "durationMs": duration_ms,
    }
    return _worker_for(provider).call(req, timeout)  # type: ignore[return-value]


def remote_diarize(
    models: dict[str, str],
    file_path: str,
    duration_ms: int = 0,
    num_threads: int = 2,
    threshold: float = 0.7,
    num_clusters: int = -1,
) -> dict:
    """在独立进程里跑说话人分离，返回 {numSpeakers, segments}。"""
    timeout = max(300.0, float(duration_ms or 0) / 1000 * 0.5 + 120)
    req = {
        "task": "diarize",
        "file": str(file_path),
        "models": models,
        "numThreads": num_threads,
        "threshold": threshold,
        "numClusters": num_clusters,
    }
    return _worker_for({"id": "diarize", "kind": "diarize"}).call(req, timeout)


@contextmanager
def _hold(lock, what: str):
    if not lock.acquire(timeout=LOCAL_LOCK_TIMEOUT):
        raise RuntimeError(
            f"本地 ASR 引擎忙（等待「{what}」超过 {int(LOCAL_LOCK_TIMEOUT)}s），"
            "疑似推理卡死，已放弃本次转写"
        )
    try:
        yield
    finally:
        lock.release()

_FW_CACHE: dict[str, Any] = {}
_FW_LOCK = threading.Lock()
_FW_MAX = 2  # 同时留两份权重（small + turbo 之类），再多会明显吃内存



def _cfg(provider: dict) -> dict:
    raw = provider.get("params")
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}


def _lang(language: str | None) -> str | None:
    return None if not language or language == "auto" else language


def _clean(text: str) -> str:
    return " ".join(text.split()).strip()


class FasterWhisperLocal(AsrEngine):
    """faster-whisper（CTranslate2）：整段音频一次转写，带句级时间戳。"""

    def __init__(self, provider: dict) -> None:
        self.p = provider

    def _model(self):
        try:
            from faster_whisper import WhisperModel  # type: ignore
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(
                "未安装 faster-whisper，先执行：cd server-py && uv sync --extra local-asr"
            ) from e

        cfg = _cfg(self.p)
        model_id = (self.p.get("model") or cfg.get("model") or "small").strip()
        local = store.faster_whisper_path(model_id)
        target = str(local) if local else model_id  # 没下载过就让库自己按名字取（走 HF 缓存）

        device = str(cfg.get("device") or "auto").lower()
        if device == "auto":
            device = "cuda" if cfg.get("cuda") else "cpu"
        compute = str(cfg.get("computeType") or ("float16" if device == "cuda" else "int8"))
        threads = int(cfg.get("cpuThreads") or max(1, (os.cpu_count() or 4) // 2))

        key = f"{target}|{device}|{compute}|{threads}"
        with _hold(_FW_LOCK, "加载 faster-whisper 模型"):
            hit = _FW_CACHE.get(key)
            if hit is not None:
                return hit
            try:
                model = WhisperModel(
                    target,
                    device=device,
                    compute_type=compute,
                    cpu_threads=threads,
                    download_root=str(store.FASTER_WHISPER_DIR),
                )
            except Exception as e:  # noqa: BLE001
                raise RuntimeError(
                    f"加载本地模型失败（{target}）：{e}。可在「设置 → 语音识别」里下载模型，"
                    "或用 HF_ENDPOINT=https://hf-mirror.com 走镜像。"
                ) from e
            while len(_FW_CACHE) >= _FW_MAX:
                _FW_CACHE.pop(next(iter(_FW_CACHE)))
            _FW_CACHE[key] = model
            return model

    def transcribe(self, file_path, mime=None, language=None, duration_ms=None) -> AsrResult:
        if ISOLATED:
            return remote_transcribe(self.p, file_path, mime, language, duration_ms)
        cfg = _cfg(self.p)
        model = self._model()
        lang = _lang(language)
        # 用 ffmpeg 解成 16k float32 直接喂模型：绕开 faster-whisper 对 PyAV 版本的挑剔
        pcm, wav_ms = to_pcm16k(file_path)
        try:
            import numpy as np  # type: ignore
        except ImportError as e:  # faster-whisper 依赖它，正常不会缺
            raise RuntimeError("缺少 numpy，请重新执行 uv sync --extra local-asr") from e
        audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0

        prompt = cfg.get("initialPrompt") or (ZH_PROMPT if lang == "zh" else None)

        with _hold(_LOCAL_SEM, "faster-whisper 推理"):
            segments_iter, info = model.transcribe(
                audio,
                language=lang,
                beam_size=int(cfg.get("beamSize") or 5),
                vad_filter=bool(cfg.get("vadFilter", True)),
                vad_parameters={"min_silence_duration_ms": 500},
                condition_on_previous_text=False,
                word_timestamps=False,
                initial_prompt=prompt,
                temperature=0.0,
            )
            segments: list[Segment] = []
            for s in segments_iter:
                text = _clean(s.text)
                if not text:
                    continue
                segments.append(
                    {
                        "startMs": int(round(s.start * 1000)),
                        "endMs": int(round(s.end * 1000)),
                        "text": text,
                    }
                )

        if not segments:
            return {"text": "", "segments": [], "meta": {"engine": "faster-whisper", "audioMs": wav_ms}}

        return {
            "text": "".join(s["text"] for s in segments),
            "segments": segments,
            "meta": {
                "engine": "faster-whisper",
                "model": self.p.get("model"),
                "language": getattr(info, "language", None),
                "durationMs": int(round((getattr(info, "duration", 0) or 0) * 1000)) or wav_ms,
            },
        }


def _parse_ts(raw: str) -> int:
    """'00:01:02,500' → 62500"""
    try:
        hms, _, ms = raw.replace(".", ",").partition(",")
        h, m, s = (int(x) for x in hms.split(":"))
        return ((h * 60 + m) * 60 + s) * 1000 + int((ms or "0").ljust(3, "0")[:3])
    except (ValueError, AttributeError):
        return 0


class WhisperCppLocal(AsrEngine):
    """whisper.cpp：子进程调用 whisper-cli，读它输出的 JSON。"""

    def __init__(self, provider: dict) -> None:
        self.p = provider

    def transcribe(self, file_path, mime=None, language=None, duration_ms=None) -> AsrResult:
        cfg = _cfg(self.p)
        binary = store.whisper_cpp_binary(cfg.get("binaryPath"))
        if not binary:
            raise RuntimeError(
                "未找到 whisper-cli。macOS 执行 brew install whisper-cpp，"
                "或编译后把路径填到「二进制路径」/ 环境变量 WHISPER_CPP_BIN。"
            )
        model_field = self.p.get("model") or cfg.get("model")
        model_path = store.whisper_cpp_path(model_field)
        if not model_path:
            raise RuntimeError(
                f"本地模型不存在：{model_field or '未填写'}。请先在「设置 → 语音识别」下载模型，"
                f"或把 ggml-*.bin 放到 {store.WHISPER_CPP_DIR}/"
            )

        wav, wav_ms = to_wav16k(file_path)
        threads = int(cfg.get("threads") or max(1, (os.cpu_count() or 4) // 2))
        timeout = max(600, int(wav_ms / 1000 * 6) + 120)
        lang = _lang(language) or "auto"
        prompt = cfg.get("initialPrompt") or (ZH_PROMPT if lang == "zh" else None)

        with tempfile.TemporaryDirectory() as td:
            wav_path = Path(td) / "in.wav"
            wav_path.write_bytes(wav)
            prefix = Path(td) / "out"
            cmd = [
                binary,
                "-m", str(model_path),
                "-f", str(wav_path),
                "-oj",                    # 输出 JSON
                "-of", str(prefix),       # 输出前缀 → out.json
                "-t", str(threads),
                "-l", lang,
                "-np",                    # 只留结果，不刷进度
            ]
            if cfg.get("noGpu"):
                cmd.append("-ng")
            if prompt:
                cmd += ["--prompt", prompt]

            with _hold(_CPP_SEM, "whisper.cpp 推理"):
                res = subprocess.run(cmd, capture_output=True, timeout=timeout, check=False)
            if res.returncode != 0:
                tail = (res.stderr or res.stdout or b"").decode("utf-8", "ignore").strip()[-400:]
                raise RuntimeError(f"whisper.cpp 执行失败（{res.returncode}）：{tail or '无输出'}")

            js = prefix.with_suffix(".json")
            if not js.is_file():
                raise RuntimeError("whisper.cpp 没有生成 JSON 结果，请确认版本支持 -oj")
            data = json.loads(js.read_text(encoding="utf-8"))

        segments: list[Segment] = []
        for item in data.get("transcription") or []:
            text = _clean(str(item.get("text") or ""))
            if not text:
                continue
            offsets = item.get("offsets") or {}
            start = offsets.get("from")
            end = offsets.get("to")
            if not isinstance(start, (int, float)):
                start = _parse_ts((item.get("timestamps") or {}).get("from", ""))
            if not isinstance(end, (int, float)):
                end = _parse_ts((item.get("timestamps") or {}).get("to", ""))
            segments.append({"startMs": int(start), "endMs": int(end), "text": text})

        return {
            "text": "".join(s["text"] for s in segments),
            "segments": segments,
            "meta": {
                "engine": "whisper-cpp",
                "model": model_path.name,
                "binary": binary,
                "audioMs": wav_ms,
            },
        }


# ------------------------------------------------------------------ #
# sherpa-onnx：一套 ONNX 运行时跑 Qwen3-ASR / SenseVoice / Paraformer
# ------------------------------------------------------------------ #
_SHERPA_CACHE: dict[str, Any] = {}
_SHERPA_LOCK = threading.Lock()
_SHERPA_MAX = 2  # Qwen3-ASR 0.6B 常驻约 1GB 内存，别贪多

_SENT_SPLIT = re.compile(r"(?<=[。！？!?；;])")


def _sherpa_family(d: Path) -> str:
    name = d.name.lower()
    if "qwen3" in name:
        return "qwen3"
    if "paraformer" in name:
        return "paraformer"
    if "sense-voice" in name or "sensevoice" in name:
        return "sensevoice"
    if any(f.name.startswith("conv_frontend") for f in d.glob("*")):
        return "qwen3"
    return "sensevoice"


def _pick_onnx(d: Path, *keys: str) -> str | None:
    """按关键字挑 .onnx，优先 int8（体积小、CPU 快）。"""
    hits = [f for f in d.glob("*.onnx") if any(k in f.name.lower() for k in keys)]
    if not hits:
        return None
    hits.sort(key=lambda f: (0 if "int8" in f.name.lower() else 1, f.stat().st_size))
    return str(hits[0])


def _pick_tokenizer(d: Path) -> str | None:
    for p in d.iterdir():
        if "tokenizer" in p.name.lower():
            return str(p)
    return None


def _window_bounds(audio, sr: int, max_len: int, min_sec: float = 5.0):
    """按最长 max_len 切块，切点尽量落在静音上，避免把一个词劈成两半。"""
    import numpy as np  # type: ignore

    hop = int(sr * 0.02)  # 20ms 一帧
    min_len = int(min_sec * sr)
    n = len(audio)
    pos = 0
    while pos < n:
        end = min(pos + max_len, n)
        if end < n and end - pos > min_len + hop * 4:
            seg = audio[pos + min_len : end]
            m = (len(seg) // hop) * hop
            if m > 0:
                rms = np.sqrt((seg[:m].reshape(-1, hop) ** 2).mean(axis=1))
                cut = pos + min_len + int(np.argmin(rms)) * hop
                if cut > pos + min_len:
                    end = cut
        yield pos, end
        pos = end


def _split_sentences(text: str, stamps: list[float], base: float, dur: float) -> list[Segment]:
    """把一段识别结果按标点切句；stamps 是逐字符时间点，按字数比例映射。"""
    parts = [p.strip() for p in _SENT_SPLIT.split(text) if p.strip()]
    if not parts:
        return []
    if not stamps:
        step = dur / len(parts)
        return [
            {"startMs": int((base + i * step) * 1000), "endMs": int((base + (i + 1) * step) * 1000), "text": p}
            for i, p in enumerate(parts)
        ]

    total = sum(len(p) for p in parts) or 1
    n = len(stamps)
    out: list[Segment] = []
    acc = 0
    for p in parts:
        i0 = min(int(acc / total * n), n - 1)
        acc += len(p)
        i1 = min(max(int(acc / total * n), i0 + 1), n)
        out.append(
            {
                "startMs": int((base + stamps[i0]) * 1000),
                "endMs": int((base + stamps[i1 - 1]) * 1000),
                "text": p,
            }
        )
    return out


class SherpaOnnxLocal(AsrEngine):
    """sherpa-onnx 离线识别：Qwen3-ASR / SenseVoice / Paraformer，纯 ONNX，不需要 PyTorch。"""

    def __init__(self, provider: dict) -> None:
        self.p = provider

    def _recognizer(self, language: str | None):
        try:
            import sherpa_onnx  # type: ignore
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(
                "未安装 sherpa-onnx，先执行：cd server-py && uv sync --extra local-asr"
            ) from e

        cfg = _cfg(self.p)
        d = store.sherpa_path(self.p.get("model"))
        if not d:
            raise RuntimeError(
                f"本地模型不存在：{self.p.get('model') or '未填写'}。"
                "请先在「设置 → 语音识别」里下载，或把解压后的模型目录放到 "
                f"{store.SHERPA_DIR}/"
            )

        family = _sherpa_family(d)
        lang = _lang(language) or ""
        threads = int(cfg.get("cpuThreads") or max(1, (os.cpu_count() or 4) // 2))
        key = f"{d}|{family}|{threads}|{lang if family == 'sensevoice' else ''}"

        with _hold(_SHERPA_LOCK, "加载 sherpa-onnx 模型"):
            hit = _SHERPA_CACHE.get(key)
            if hit is not None:
                return hit, family
            try:
                if family == "qwen3":
                    conv = _pick_onnx(d, "conv_frontend")
                    enc = _pick_onnx(d, "encoder")
                    dec = _pick_onnx(d, "decoder")
                    tok = _pick_tokenizer(d)
                    if not all((conv, enc, dec, tok)):
                        raise RuntimeError("Qwen3-ASR 模型目录不完整（缺 encoder/decoder/tokenizer）")
                    model = sherpa_onnx.OfflineRecognizer.from_qwen3_asr(
                        conv_frontend=conv,
                        encoder=enc,
                        decoder=dec,
                        tokenizer=tok,
                        num_threads=threads,
                        max_new_tokens=int(cfg.get("maxNewTokens") or 256),
                    )
                elif family == "paraformer":
                    m = _pick_onnx(d, "model")
                    if not m:
                        raise RuntimeError("Paraformer 模型目录不完整（缺 model.onnx）")
                    model = sherpa_onnx.OfflineRecognizer.from_paraformer(
                        paraformer=m,
                        tokens=str(d / "tokens.txt"),
                        num_threads=threads,
                    )
                else:
                    m = _pick_onnx(d, "model")
                    if not m:
                        raise RuntimeError("SenseVoice 模型目录不完整（缺 model.onnx）")
                    model = sherpa_onnx.OfflineRecognizer.from_sense_voice(
                        model=m,
                        tokens=str(d / "tokens.txt"),
                        num_threads=threads,
                        language=lang,
                        use_itn=True,
                    )
            except Exception as e:  # noqa: BLE001
                raise RuntimeError(f"加载本地模型失败（{d.name}）：{e}") from e

            while len(_SHERPA_CACHE) >= _SHERPA_MAX:
                _SHERPA_CACHE.pop(next(iter(_SHERPA_CACHE)))
            _SHERPA_CACHE[key] = model
            return model, family

    def transcribe(self, file_path, mime=None, language=None, duration_ms=None) -> AsrResult:
        if ISOLATED:
            return remote_transcribe(self.p, file_path, mime, language, duration_ms)
        cfg = _cfg(self.p)
        pcm, wav_ms = to_pcm16k(file_path)
        try:
            import numpy as np  # type: ignore
        except ImportError as e:
            raise RuntimeError("缺少 numpy，请重新执行 uv sync --extra local-asr") from e

        audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        recognizer, family = self._recognizer(language)
        sr = 16000
        # SenseVoice / Paraformer 单次能吃几十秒；Qwen3 上限更长。
        # 统一 25 秒一段，长会议录音也不会被截断。
        win = max(5, int(cfg.get("windowSec") or 25)) * sr

        segments: list[Segment] = []
        for start, end in _window_bounds(audio, sr, win):
            chunk = audio[start:end]
            if chunk.size < sr * 0.2:  # 不足 0.2 秒的尾巴直接丢
                continue
            with _hold(_LOCAL_SEM, "sherpa-onnx 推理"):
                stream = recognizer.create_stream()
                stream.accept_waveform(sr, chunk)
                recognizer.decode_stream(stream)
                text = _clean(stream.result.text)
                stamps = [float(t) for t in (getattr(stream.result, "timestamps", None) or [])]
            if not text:
                continue
            segments.extend(_split_sentences(text, stamps, start / sr, chunk.size / sr))

        if not segments:
            return {"text": "", "segments": [], "meta": {"engine": "sherpa-onnx", "audioMs": wav_ms}}

        path = store.sherpa_path(self.p.get("model"))
        return {
            "text": "".join(s["text"] for s in segments),
            "segments": segments,
            "meta": {
                "engine": f"sherpa-onnx/{family}",
                "model": path.name if path else self.p.get("model"),
                "audioMs": wav_ms,
            },
        }


# ------------------------------------------------------------------ #
# MLX Qwen3-ASR：Apple Silicon 专用，mlx-community 的 6bit 权重走 Metal
# ------------------------------------------------------------------ #
_MLX_CACHE: dict[str, Any] = {}
_MLX_LOCK = threading.Lock()
_MLX_MAX = 1  # 1.7B 6bit 常驻约 2GB，别多留

# mlx-audio 认的是语种英文名，不是 zh / en
_MLX_LANG = {
    "zh": "Chinese",
    "yue": "Cantonese",
    "en": "English",
    "ja": "Japanese",
    "ko": "Korean",
}


class MlxQwen3AsrLocal(AsrEngine):
    """MLX Qwen3-ASR：Apple Silicon 上用 Metal 跑 6bit 量化权重。"""

    def __init__(self, provider: dict) -> None:
        self.p = provider

    def _model(self):
        try:
            from mlx_audio.stt import load  # type: ignore
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(
                "未安装 mlx-audio，先执行：cd server-py && uv sync --extra local-asr"
            ) from e

        cfg = _cfg(self.p)
        model_id = (self.p.get("model") or cfg.get("model") or "qwen3-asr-1.7b-6bit").strip()
        local = store.mlx_path(model_id)
        if not local:
            raise RuntimeError(
                f"本地 MLX 模型不存在：{model_id}。请先在「设置 → 语音识别」里下载，"
                f"或把 MLX 目录放到 {store.MLX_DIR}/"
            )

        key = str(local)
        with _hold(_MLX_LOCK, "加载 MLX 模型"):
            hit = _MLX_CACHE.get(key)
            if hit is not None:
                return hit
            try:
                model = load(key)
            except Exception as e:  # noqa: BLE001
                raise RuntimeError(f"加载本地 MLX 模型失败（{local.name}）：{e}") from e
            while len(_MLX_CACHE) >= _MLX_MAX:
                _MLX_CACHE.pop(next(iter(_MLX_CACHE)))
            _MLX_CACHE[key] = model
            return model

    def transcribe(self, file_path, mime=None, language=None, duration_ms=None) -> AsrResult:
        if ISOLATED:
            return remote_transcribe(self.p, file_path, mime, language, duration_ms)
        model = self._model()
        wav, wav_ms = to_wav16k(file_path)
        lang = _MLX_LANG.get((language or "").strip().lower())

        # mlx-audio 的 generate 直接吃文件路径；WAV 有原生快速路径，不用过 ffmpeg
        with tempfile.TemporaryDirectory() as td:
            wav_path = Path(td) / "in.wav"
            wav_path.write_bytes(wav)
            with _hold(_LOCAL_SEM, "MLX Qwen3-ASR 推理"):
                out = model.generate(str(wav_path), language=lang, verbose=False)

        segments: list[Segment] = []
        for item in getattr(out, "segments", None) or []:
            text = _clean(str(item.get("text") or ""))
            if not text:
                continue
            segments.append(
                {
                    "startMs": int(round(float(item.get("start") or 0) * 1000)),
                    "endMs": int(round(float(item.get("end") or 0) * 1000)),
                    "text": text,
                }
            )

        text = _clean(str(getattr(out, "text", "") or ""))
        if not segments and text:
            segments = [{"startMs": 0, "endMs": wav_ms, "text": text}]
        if not text:
            text = "".join(s["text"] for s in segments)

        path = store.mlx_path(self.p.get("model"))
        detected = getattr(out, "language", None)
        if isinstance(detected, list):
            detected = detected[0] if detected else None
        return {
            "text": text,
            "segments": segments,
            "meta": {
                "engine": "mlx-qwen3-asr",
                "model": path.name if path else self.p.get("model"),
                "language": detected,
                "audioMs": wav_ms,
            },
        }


def local_engine_ready(provider: dict) -> tuple[bool, str]:
    """给「测试」按钮用的轻量体检：不乱加载模型，也不跑推理。"""
    kind = provider.get("kind")
    cfg = _cfg(provider)
    if kind == "local-sherpa-onnx":
        version = store.sherpa_onnx_version()
        if not version:
            return False, "未安装 sherpa-onnx，执行：cd server-py && uv sync --extra local-asr"
        model = provider.get("model")
        path = store.sherpa_path(model)
        if not path:
            if store.mlx_repo(model):
                return False, f"模型 {model} 是 MLX 专用权重，请把类型改成「本地 · MLX Qwen3-ASR」"
            return False, f"模型 {model or '未填写'} 还没下载，点「下载」或换一个已下载的模型"
        return True, f"sherpa-onnx {version} · {path.name} 已就位（CPU 本地推理，音频不出本机）"

    if kind == "local-faster-whisper":
        version = store.faster_whisper_version()
        if not version:
            return False, "未安装 faster-whisper，执行：cd server-py && uv sync --extra local-asr"
        model = provider.get("model") or "small"
        if not store.faster_whisper_path(model):
            return False, f"模型 {model} 还没下载，点「下载」或更换已下载的模型"
        return True, f"faster-whisper {version} · 模型 {model} 已就位（推理在 CPU/GPU 本地完成）"

    if kind == "local-mlx-qwen3-asr":
        if not store.apple_silicon():
            return False, "MLX 只支持 Apple Silicon Mac（M 系列芯片），Intel Mac / Linux 用不了"
        version = store.mlx_audio_version()
        if not version:
            return False, "未安装 mlx-audio，执行：cd server-py && uv sync --extra local-asr"
        model = provider.get("model") or "qwen3-asr-1.7b-6bit"
        path = store.mlx_path(model)
        if not path:
            return False, f"模型 {model} 还没下载，点「下载」或换一个已下载的模型"
        return True, f"mlx-audio {version} · {path.name} 已就位（Metal 本地推理，音频不出本机）"

    binary = store.whisper_cpp_binary(cfg.get("binaryPath"))
    if not binary:
        return False, "未找到 whisper-cli，macOS 执行 brew install whisper-cpp"
    model_path = store.whisper_cpp_path(provider.get("model"))
    if not model_path:
        return False, f"模型 {provider.get('model') or '未填写'} 不存在，请先在设置页下载"
    return True, f"whisper.cpp 就绪 · {binary} · {model_path.name}"


__all__ = [
    "FasterWhisperLocal",
    "MlxQwen3AsrLocal",
    "SherpaOnnxLocal",
    "WhisperCppLocal",
    "local_engine_ready",
]
