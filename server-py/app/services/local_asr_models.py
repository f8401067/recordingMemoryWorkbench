"""本地 ASR 模型仓库：目录约定、可用性探测、后台下载。

三处约定，前后端都以此为准：

- 模型统一放在 `<DATA_DIR>/models/`，删掉整个目录即可回收空间
- whisper.cpp 用 GGML 单文件：`models/whisper-cpp/ggml-<size>.bin`
- faster-whisper 用 CT2 目录：`models/faster-whisper/<size>/`（含 model.bin 等）

下载在后台线程里跑，进度存在进程内字典里，接口按 id 轮询即可。断点续传靠
HTTP Range，源站不支持就从头下（truncate 后重来）。
"""

from __future__ import annotations

import os
import shutil
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from ..config import DATA_DIR

MODELS_DIR = DATA_DIR / "models"
WHISPER_CPP_DIR = MODELS_DIR / "whisper-cpp"
FASTER_WHISPER_DIR = MODELS_DIR / "faster-whisper"
SHERPA_DIR = MODELS_DIR / "sherpa-onnx"

for _d in (MODELS_DIR, WHISPER_CPP_DIR, FASTER_WHISPER_DIR, SHERPA_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# HuggingFace 在国内常被墙，默认走 hf-mirror；有外网直连需求设 HF_DOWNLOAD_SOURCE=hf。
# 设了 HF_ENDPOINT 则以它为准（也可以用 HF_ENDPOINT 指向公司内网镜像）。
HF_MIRROR = "https://hf-mirror.com"
HF_ENDPOINT = (os.getenv("HF_ENDPOINT") or "").rstrip("/")
PREFER_OFFICIAL = (os.getenv("HF_DOWNLOAD_SOURCE") or "mirror").lower() == "hf"

# huggingface_hub 自己认这个环境变量；不设的话它会去连被墙的 huggingface.co
os.environ.setdefault("HF_ENDPOINT", HF_ENDPOINT or HF_MIRROR)
# 镜像站不支持 Xet 的 CAS 协议（会 401），强制走普通 HTTP 下载
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")


def _hf_urls(path: str) -> list[str]:
    mirror = f"{HF_MIRROR}/{path}"
    official = f"https://huggingface.co/{path}"
    if HF_ENDPOINT:
        ordered = [f"{HF_ENDPOINT}/{path}", mirror, official]
    elif PREFER_OFFICIAL:
        ordered = [official, mirror]
    else:
        ordered = [mirror, official]
    seen: set[str] = set()
    return [u for u in ordered if not (u in seen or seen.add(u))]


FASTER_WHISPER_SIZES = [
    ("tiny", "Tiny", 75, "最快，适合试跑；中文准确率一般"),
    ("base", "Base", 145, "轻量，短句可用"),
    ("small", "Small", 484, "推荐起步：中文明显好于 base，CPU 也能跑"),
    ("medium", "Medium", 1530, "准确率更高，CPU 上偏慢"),
    ("large-v3", "Large v3", 3100, "准确率最好，建议有 GPU / 大内存"),
    ("large-v3-turbo", "Large v3 Turbo", 1620, "接近 large-v3 的质量，速度快数倍"),
]

WHISPER_CPP_SIZES = [
    ("tiny", "Tiny", 75, "最快，精度一般"),
    ("base", "Base", 142, "轻量"),
    ("small", "Small", 466, "推荐起步：质量与速度平衡"),
    ("medium", "Medium", 1500, "更准，CPU 上慢"),
    ("large-v3", "Large v3", 3100, "最准，Apple Silicon / CUDA 上可用"),
    ("large-v3-turbo", "Large v3 Turbo", 1620, "质量接近 large-v3，速度更快"),
]

BACKENDS = {
    "sherpa-onnx": {
        "label": "sherpa-onnx（Qwen3-ASR / SenseVoice / Paraformer，纯 ONNX，不吃 PyTorch）",
        "install": "cd server-py && uv sync --extra local-asr",
    },
    "faster-whisper": {
        "label": "faster-whisper（Python 包，CPU/GPU 通用）",
        "install": "cd server-py && uv sync --extra local-asr",
    },
    "whisper-cpp": {
        "label": "whisper.cpp（单文件二进制，Apple Silicon / CUDA 更快）",
        "install": "brew install whisper-cpp   # 或自行编译后设 WHISPER_CPP_BIN",
    },
}

# sherpa-onnx 的预转换模型：GitHub Releases 的 asr-models tag，解压即用。
# 国产模型（Qwen3-ASR / SenseVoice / Paraformer）中文都明显强于 Whisper。
GITHUB_ASR_MODELS = "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models"
GH_PROXY = os.getenv("SHERPA_GH_PROXY", "https://gh-proxy.com")

SHERPA_MODELS = [
    (
        "qwen3-asr-0.6b",
        "Qwen3-ASR 0.6B",
        838,
        "中文最准（52 语种 + 22 方言，自动语种），比 Whisper large 强一档；CPU 可跑，慢一些",
        "sherpa-onnx-qwen3-asr-0.6B-int8-2026-03-25.tar.bz2",
    ),
    (
        "sensevoice-small",
        "SenseVoice Small",
        158,
        "234M 非自回归，CPU 上约 17 倍实时；中英粤日韩，自带情感/音频事件。速度首选",
        "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2025-09-09.tar.bz2",
    ),
    (
        "paraformer-zh",
        "Paraformer 中文",
        218,
        "达摩院中文模型，句级时间戳稳，适合纯中文会议；不含标点",
        "sherpa-onnx-paraformer-zh-int8-2025-10-07.tar.bz2",
    ),
]

_DOWNLOADS: dict[str, dict[str, Any]] = {}
_LOCK = threading.Lock()

# whisper.cpp 二进制候选名：新版叫 whisper-cli，老版叫 main
_CPP_BINARIES = ("whisper-cli", "whisper-cpp", "whisper", "main")


# ------------------------------------------------------------------ #
# 探测
# ------------------------------------------------------------------ #
def faster_whisper_version() -> str | None:
    try:
        import faster_whisper  # noqa: F401
    except Exception:  # noqa: BLE001 - ImportError 之外还可能是 ctranslate2 缺库
        return None
    return getattr(faster_whisper, "__version__", "已安装")


def whisper_cpp_binary(explicit: str | None = None) -> str | None:
    """显式路径 > WHISPER_CPP_BIN > PATH 里的常见名字。"""
    for cand in (explicit, os.getenv("WHISPER_CPP_BIN")):
        if cand and Path(cand).expanduser().is_file():
            return str(Path(cand).expanduser())
    for name in _CPP_BINARIES:
        found = shutil.which(name)
        if found:
            return found
    return None


def sherpa_onnx_version() -> str | None:
    try:
        import sherpa_onnx  # noqa: F401
    except Exception:  # noqa: BLE001 - 缺 onnxruntime 动态库也会抛非 ImportError
        return None
    return getattr(sherpa_onnx, "__version__", "已安装")


def backend_status() -> dict[str, dict]:
    version = faster_whisper_version()
    binary = whisper_cpp_binary()
    sherpa = sherpa_onnx_version()
    return {
        "sherpa-onnx": {
            "available": bool(sherpa),
            "detail": f"sherpa-onnx {sherpa}" if sherpa else "未安装",
            "install": BACKENDS["sherpa-onnx"]["install"],
        },
        "faster-whisper": {
            "available": bool(version),
            "detail": f"faster-whisper {version}" if version else "未安装",
            "install": BACKENDS["faster-whisper"]["install"],
        },
        "whisper-cpp": {
            "available": bool(binary),
            "detail": binary or "未找到 whisper-cli",
            "install": BACKENDS["whisper-cpp"]["install"],
        },
    }


# ------------------------------------------------------------------ #
# 模型路径
# ------------------------------------------------------------------ #
def whisper_cpp_path(model: str | None) -> Path | None:
    """把模型字段解析成实际文件：绝对路径 / ggml-x.bin / x（尺寸名）。"""
    if not model:
        return None
    p = Path(model).expanduser()
    if p.is_file():
        return p
    name = p.name
    if not name.startswith("ggml-"):
        name = f"ggml-{name}"
    if not name.endswith(".bin"):
        name = f"{name}.bin"
    cand = WHISPER_CPP_DIR / name
    return cand if cand.is_file() else None


def faster_whisper_path(size: str | None) -> Path | None:
    if not size:
        return None
    p = Path(size).expanduser()
    if (p / "model.bin").is_file():
        return p
    cand = FASTER_WHISPER_DIR / p.name
    return cand if (cand / "model.bin").is_file() else None


def _sherpa_ready(d: Path) -> bool:
    """sherpa 模型包解压后是一个含若干 .onnx 的目录。"""
    if not d.is_dir():
        return False
    try:
        return any(f.suffix == ".onnx" for f in d.iterdir() if f.is_file())
    except OSError:
        return False


def sherpa_archive(model_id: str | None) -> str | None:
    for mid, _label, _mb, _hint, arc in SHERPA_MODELS:
        if mid == (model_id or "").strip():
            return arc
    return None


def sherpa_path(model_id: str | None) -> Path | None:
    """模型字段可能是内置 id、解压后的目录名，或直接给绝对目录。"""
    if not model_id:
        return None
    direct = Path(model_id).expanduser()
    if _sherpa_ready(direct):
        return direct
    arc = sherpa_archive(model_id)
    names = [arc[: -len(".tar.bz2")]] if arc else []
    names.append(direct.name)
    for name in names:
        cand = SHERPA_DIR / name
        if _sherpa_ready(cand):
            return cand
    return None


def is_installed(backend: str, model_id: str) -> bool:
    if backend == "whisper-cpp":
        return whisper_cpp_path(model_id) is not None
    if backend == "sherpa-onnx":
        return sherpa_path(model_id) is not None
    return faster_whisper_path(model_id) is not None


def model_size_mb(backend: str, model_id: str) -> float | None:
    if backend == "whisper-cpp":
        p = whisper_cpp_path(model_id)
        return round(p.stat().st_size / 1024 / 1024, 1) if p else None
    p = sherpa_path(model_id) if backend == "sherpa-onnx" else faster_whisper_path(model_id)
    if not p:
        return None
    return round(sum(f.stat().st_size for f in p.glob("*") if f.is_file()) / 1024 / 1024, 1)


# ------------------------------------------------------------------ #
# 清单
# ------------------------------------------------------------------ #
def _entry(backend: str, model_id: str, label: str, size_mb: int, hint: str) -> dict:
    return {
        "id": model_id,
        "backend": backend,
        "label": label,
        "sizeMB": size_mb,
        "hint": hint,
        "installed": is_installed(backend, model_id),
        "downloadedMB": model_size_mb(backend, model_id),
    }


def list_models(backend: str | None = None) -> list[dict]:
    out: list[dict] = []
    if backend in (None, "sherpa-onnx"):
        out += [
            _entry("sherpa-onnx", mid, label, mb, hint)
            for mid, label, mb, hint, _arc in SHERPA_MODELS
        ]
    if backend in (None, "faster-whisper"):
        out += [_entry("faster-whisper", i, lab, mb, hint) for i, lab, mb, hint in FASTER_WHISPER_SIZES]
    if backend in (None, "whisper-cpp"):
        out += [_entry("whisper-cpp", i, lab, mb, hint) for i, lab, mb, hint in WHISPER_CPP_SIZES]
    return out


def status() -> dict:
    return {
        "modelsDir": str(MODELS_DIR),
        "hfEndpoint": os.environ.get("HF_ENDPOINT"),
        "downloads": list_downloads(),
    }


def list_downloads() -> list[dict]:
    with _LOCK:
        items = [dict(v) for v in _DOWNLOADS.values()]
    return sorted(items, key=lambda d: d.get("startedAt") or 0, reverse=True)


# ------------------------------------------------------------------ #
# 下载
# ------------------------------------------------------------------ #
def _set(rec: dict, **kw: Any) -> None:
    with _LOCK:
        rec.update(kw)
        rec["updatedAt"] = time.time()
        _DOWNLOADS[rec["id"]] = rec


def _fetch(urls: list[str], dest: Path, rec: dict) -> None:
    """按顺序试每个源；支持 Range 续传。"""
    last_err: Exception | None = None
    for url in urls:
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            part = dest.with_suffix(dest.suffix + ".part")
            have = part.stat().st_size if part.exists() else 0
            req = urllib.request.Request(url, headers={"User-Agent": "recall-workbench"})
            if have:
                req.add_header("Range", f"bytes={have}-")
            with urllib.request.urlopen(req, timeout=60) as res:
                total = int(res.headers.get("Content-Length") or 0)
                if res.status == 200 and have:
                    have = 0  # 源站不支持 Range，从头写
                total = total + have if total else 0
                _set(rec, status="downloading", total=total, received=have, source=url)
                with open(part, "r+b" if have else "wb") as fh:
                    if have:
                        fh.seek(have)
                    else:
                        fh.truncate(0)
                    while True:
                        block = res.read(1 << 20)
                        if not block:
                            break
                        fh.write(block)
                        have += len(block)
                        _set(rec, received=have)
            part.replace(dest)
            _set(rec, status="done", received=dest.stat().st_size, message=None)
            return
        except Exception as e:  # noqa: BLE001 - 换下一个源继续
            last_err = e
            continue
    raise RuntimeError(f"下载失败：{last_err}")


def _download_faster_whisper(model_id: str, rec: dict) -> None:
    dest = FASTER_WHISPER_DIR / model_id
    est = next((mb for i, _, mb, _ in FASTER_WHISPER_SIZES if i == model_id), 0) * 1024 * 1024
    _set(rec, status="downloading", total=est, message="正在准备下载…")

    # huggingface_hub 不回调进度，只能自己盯目录大小
    stop = threading.Event()

    def watch() -> None:
        while not stop.wait(1.5):
            got = sum(
                f.stat().st_size
                for f in dest.glob("*")
                if f.is_file()
            )
            _set(rec, received=got, total=max(est, rec.get("total") or 0))

    watcher = threading.Thread(target=watch, daemon=True)
    watcher.start()
    try:
        _fetch_faster_whisper(model_id, dest, rec)
        got = sum(f.stat().st_size for f in dest.glob("*") if f.is_file())
        _set(rec, status="done", received=got, total=max(got, est), message=None)
    finally:
        stop.set()


def _fetch_faster_whisper(model_id: str, dest: Path, rec: dict) -> None:
    try:
        from faster_whisper.utils import download_model  # type: ignore

        download_model(model_id, output_dir=str(dest))
        return
    except ImportError as e:
        raise RuntimeError("faster-whisper 未安装，先执行：uv sync --extra local-asr") from e
    except Exception as e:  # noqa: BLE001 - 老版本签名/镜像兼容问题，退回自己拉
        last = e

    try:
        from huggingface_hub import snapshot_download  # type: ignore
    except ImportError as e:
        raise RuntimeError(f"下载失败：{last}") from e

    _set(rec, message="正在从 HuggingFace 镜像拉取…")
    try:
        snapshot_download(
            repo_id=f"Systran/faster-whisper-{model_id}",
            local_dir=str(dest),
            allow_patterns=[
                "config.json", "model.bin", "tokenizer.json", "vocabulary.*",
                "preprocessor_config.json", "*.txt",
            ],
        )
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"下载失败：{e}") from e


def _sherpa_urls(archive: str) -> list[str]:
    direct = f"{GITHUB_ASR_MODELS}/{archive}"
    urls = [direct]
    if GH_PROXY:
        urls.append(f"{GH_PROXY.rstrip('/')}/{direct}")
    return urls


def _extract_tar_bz2(archive: Path, dest: Path, rec: dict) -> None:
    import tarfile

    _set(rec, message="正在解压…")
    with tarfile.open(archive, "r:bz2") as tf:
        try:
            tf.extractall(dest, filter="data")  # py3.12+ 防目录穿越
        except TypeError:
            tf.extractall(dest)


def _download_sherpa(model_id: str, rec: dict) -> None:
    archive = sherpa_archive(model_id)
    if not archive:
        raise RuntimeError(f"未知的 sherpa 模型：{model_id}")
    est = next((mb for mid, _l, mb, _h, _a in SHERPA_MODELS if mid == model_id), 0) * 1024 * 1024
    _set(rec, status="downloading", total=est, message="正在下载模型包…")
    tarball = SHERPA_DIR / archive
    _fetch(_sherpa_urls(archive), tarball, rec)
    _extract_tar_bz2(tarball, SHERPA_DIR, rec)
    tarball.unlink(missing_ok=True)
    p = sherpa_path(model_id)
    if not p:
        raise RuntimeError("解压完成但没有找到 .onnx 文件，压缩包可能不完整，请重试")
    got = sum(f.stat().st_size for f in p.glob("*") if f.is_file())
    _set(rec, status="done", received=got, total=max(got, est), message=None)


def _run_download(rec: dict) -> None:
    backend, model_id = rec["backend"], rec["modelId"]
    try:
        if backend == "sherpa-onnx":
            _download_sherpa(model_id, rec)
        elif backend == "faster-whisper":
            _download_faster_whisper(model_id, rec)
        else:
            dest = WHISPER_CPP_DIR / f"ggml-{model_id}.bin"
            _fetch(_hf_urls(f"ggerganov/whisper.cpp/resolve/main/ggml-{model_id}.bin"), dest, rec)
    except Exception as e:  # noqa: BLE001
        _set(rec, status="error", message=str(e)[:300])


def start_download(backend: str, model_id: str) -> dict:
    if backend not in BACKENDS:
        raise ValueError("backend 只能是 sherpa-onnx / faster-whisper / whisper-cpp")
    if backend == "sherpa-onnx":
        known = {mid for mid, *_ in SHERPA_MODELS}
    elif backend == "faster-whisper":
        known = {i for i, *_ in FASTER_WHISPER_SIZES}
    else:
        known = {i for i, *_ in WHISPER_CPP_SIZES}
    if model_id not in known:
        raise ValueError(f"未知的模型：{model_id}")
    if is_installed(backend, model_id):
        return {"id": f"{backend}:{model_id}", "backend": backend, "modelId": model_id, "status": "done"}

    key = f"{backend}:{model_id}"
    with _LOCK:
        cur = _DOWNLOADS.get(key)
        if cur and cur["status"] == "downloading":
            return dict(cur)
        rec = {
            "id": key,
            "backend": backend,
            "modelId": model_id,
            "status": "downloading",
            "received": 0,
            "total": 0,
            "startedAt": time.time(),
            "message": None,
        }
        _DOWNLOADS[key] = rec
    threading.Thread(target=_run_download, args=(rec,), daemon=True).start()
    return dict(rec)


def remove_model(backend: str, model_id: str) -> bool:
    if backend == "whisper-cpp":
        p = whisper_cpp_path(model_id)
        if p:
            p.unlink(missing_ok=True)
            return True
        return False
    if backend == "sherpa-onnx":
        p = sherpa_path(model_id)
        if p:
            shutil.rmtree(p, ignore_errors=True)
            return True
        return False
    p = faster_whisper_path(model_id)
    if p:
        shutil.rmtree(p, ignore_errors=True)
        return True
    return False


__all__ = [
    "MODELS_DIR",
    "SHERPA_DIR",
    "backend_status",
    "faster_whisper_path",
    "faster_whisper_version",
    "is_installed",
    "list_models",
    "remove_model",
    "sherpa_onnx_version",
    "sherpa_path",
    "start_download",
    "status",
    "whisper_cpp_binary",
    "whisper_cpp_path",
]
