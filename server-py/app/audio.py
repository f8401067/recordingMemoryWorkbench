from __future__ import annotations

import shutil
import subprocess
import uuid
from pathlib import Path

from .config import TMP_DIR

_ffmpeg_ok: bool | None = None


def has_ffmpeg() -> bool:
    global _ffmpeg_ok
    if _ffmpeg_ok is None:
        _ffmpeg_ok = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None
    return _ffmpeg_ok


def run(args: list[str], timeout: int = 600) -> subprocess.CompletedProcess:
    return subprocess.run(
        args,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
    )


def probe_duration_ms(path: str | Path) -> int:
    try:
        res = run(
            [
                "ffprobe",
                "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            timeout=60,
        )
        value = float(res.stdout.decode().strip())
        return max(0, round(value * 1000))
    except (ValueError, subprocess.SubprocessError):
        return 0


def _tmp_path(suffix: str) -> Path:
    return TMP_DIR / f"{uuid.uuid4().hex}{suffix}"


def _discard(path: Path) -> None:
    """删临时文件；删不掉也不能把已经成功的转写结果一起丢掉。

    受限环境（沙箱 / 只读挂载 / 文件被别的进程占着）里 unlink 会抛 OSError，
    甚至被翻译成 Errno 18（Cross-device link）。这时文件留着让清理脚本处理即可。
    """
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def to_pcm16k(path: str | Path) -> tuple[bytes, int]:
    """转成 16kHz 单声道 s16le PCM，供流式 ASR 推送。"""
    if not has_ffmpeg():
        raise RuntimeError("未检测到 ffmpeg，请先安装：brew install ffmpeg")
    out = _tmp_path(".pcm")
    try:
        res = run([
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-i", str(path),
            "-ar", "16000", "-ac", "1", "-f", "s16le",
            str(out),
        ])
        if res.returncode != 0:
            raise RuntimeError(f"音频转码失败：{res.stderr.decode()[:200]}")
        data = out.read_bytes()
        return data, round(len(data) / 2 / 16000 * 1000)
    finally:
        _discard(out)


def to_wav16k(path: str | Path) -> tuple[bytes, int]:
    """转成 16kHz 单声道 WAV，供同步 ASR（base64 上传）使用。"""
    if not has_ffmpeg():
        raise RuntimeError("未检测到 ffmpeg，请先安装：brew install ffmpeg")
    out = _tmp_path(".wav")
    try:
        res = run([
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-i", str(path),
            "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le",
            str(out),
        ])
        if res.returncode != 0:
            raise RuntimeError(f"音频转码失败：{res.stderr.decode()[:200]}")
        data = out.read_bytes()
        return data, max(0, round((len(data) - 44) / 2 / 16000 * 1000))
    finally:
        _discard(out)


def split_audio(input_path: str | Path, out_dir: str | Path, segment_sec: int = 60) -> list[dict]:
    """把长音频切成若干片段。先尝试无损切分，失败则转码成 mp3。"""
    if not has_ffmpeg():
        raise RuntimeError("未检测到 ffmpeg，请先安装：brew install ffmpeg")

    src = Path(input_path)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    for old in out.iterdir():
        old.unlink(missing_ok=True)

    ext = src.suffix.lower() or ".bin"
    pattern = str(out / f"part%04d{ext}")

    res = run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(src),
        "-f", "segment",
        "-segment_time", str(segment_sec),
        "-reset_timestamps", "1",
        "-c", "copy",
        pattern,
    ])
    parts = sorted(p for p in out.iterdir() if p.name.startswith("part"))

    if res.returncode != 0 or not parts:
        for p in parts:
            p.unlink(missing_ok=True)
        res = run([
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-i", str(src),
            "-f", "segment",
            "-segment_time", str(segment_sec),
            "-reset_timestamps", "1",
            "-c:a", "libmp3lame", "-b:a", "64k", "-ar", "16000", "-ac", "1",
            str(out / "part%04d.mp3"),
        ])
        parts = sorted(p for p in out.iterdir() if p.name.startswith("part"))

    result: list[dict] = []
    for p in parts:
        if p.stat().st_size < 1024:
            continue
        result.append({"file": str(p), "durationMs": probe_duration_ms(p)})
    return result
