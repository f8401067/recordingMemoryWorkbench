"""进程日志环形缓冲。

设置页的「日志」标签要能看到后端到底在干什么，但服务端没有集中式日志设施：

- 大量诊断信息是 `print()` 打出来的（[db] / [jobs] / [summarize] ...）
- 异常栈走 `traceback.print_exc()`，直接写 stderr
- uvicorn 自己的日志也写到 stderr

所以这里直接劫持 `sys.stdout` / `sys.stderr`，把每一行同时写进原始流和内存环形缓冲。
任务级事件（开始 / 完成 / 失败 / 重新排队）用 `log()` 写结构化条目，不落盘：
进程重启即清空，避免日志文件无限长大，也避免把用户数据写进日志。
"""

from __future__ import annotations

import re
import sys
import threading
from collections import deque
from datetime import datetime
from typing import Any

MAX_ENTRIES = 4000
MAX_MESSAGE = 4000

LEVELS = ("debug", "info", "warn", "error")
_ORDER = {name: i for i, name in enumerate(LEVELS)}

_lock = threading.RLock()
_entries: deque[dict[str, Any]] = deque(maxlen=MAX_ENTRIES)
_seq = 0
_installed = False

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
_UVICORN_RE = re.compile(r"^(DEBUG|INFO|WARNING|WARN|ERROR|CRITICAL):\s+")
_TAG_RE = re.compile(r"^\[([A-Za-z0-9_.:-]{1,24})\]\s*")


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


def _norm_level(level: str) -> str:
    level = (level or "info").lower()
    if level in ("warning", "warn"):
        return "warn"
    if level in ("critical", "fatal", "err"):
        return "error"
    return level if level in _ORDER else "info"


def record(level: str, source: str, message: str) -> dict[str, Any] | None:
    """写入一条日志。message 里的换行会按行拆开，保证一条记录就是一行。"""
    global _seq
    text = _ANSI_RE.sub("", str(message or "")).strip("\r\n")
    if not text.strip():
        return None
    text = text[:MAX_MESSAGE]
    with _lock:
        _seq += 1
        entry = {
            "id": _seq,
            "ts": _now(),
            "level": _norm_level(level),
            "source": (source or "app")[:24],
            "message": text,
        }
        _entries.append(entry)
        return entry


def log(level: str, source: str, message: str) -> None:
    """结构化事件：只进缓冲，不回显到终端（终端里已有对应的 print）。"""
    record(level, source, message)


def record_line(line: str, stream: str) -> None:
    """把 stdout/stderr 的一行原始输出归类成日志条目。"""
    text = _ANSI_RE.sub("", line).rstrip("\r\n")
    if not text.strip():
        return

    level = "info"
    source = "stdout" if stream == "stdout" else "stderr"

    m = _UVICORN_RE.match(text)
    if m:
        raw = m.group(1).lower()
        level = "warn" if raw in ("warning", "warn") else ("error" if raw in ("error", "critical") else raw)
        source = "uvicorn"
        text = text[m.end():]
    else:
        m = _TAG_RE.match(text)
        if m:
            source = m.group(1)
            text = text[m.end():]

    if stream == "stderr" and level == "info" and source != "uvicorn":
        # 没有级别前缀的 stderr：异常栈当错误，其余当警告
        level = "error" if ("Traceback" in text or "Error" in text or "error" in text) else "warn"
    elif stream == "stdout" and level == "info" and ("失败" in text or "错误" in text):
        level = "warn"

    record(level, source, text)


class _Tee:
    """把写入透传给真实流，同时按行记到缓冲里。"""

    def __init__(self, target: Any, name: str) -> None:
        self._target = target
        self._name = name
        self._buf = ""
        self._tlock = threading.Lock()

    def write(self, data: Any) -> int:
        try:
            self._target.write(data)
        except Exception:  # noqa: BLE001 - 终端没了也不能让业务崩
            pass
        try:
            text = data if isinstance(data, str) else str(data)
            with self._tlock:
                self._buf += text
                while "\n" in self._buf:
                    line, self._buf = self._buf.split("\n", 1)
                    record_line(line, self._name)
                if len(self._buf) > 8000:  # 长时间没有换行的输出，别把内存顶爆
                    record_line(self._buf, self._name)
                    self._buf = ""
        except Exception:  # noqa: BLE001 - 记日志失败不该影响打印
            pass
        return len(data) if hasattr(data, "__len__") else 0

    def flush(self) -> None:
        try:
            self._target.flush()
        except Exception:  # noqa: BLE001
            pass

    def isatty(self) -> bool:
        return False

    def writable(self) -> bool:
        return True

    def readable(self) -> bool:
        return False

    def fileno(self) -> int:
        return self._target.fileno()

    @property
    def encoding(self) -> str:
        return getattr(self._target, "encoding", "utf-8")

    @property
    def errors(self) -> str:
        return getattr(self._target, "errors", "strict")

    def __getattr__(self, item: str) -> Any:
        return getattr(self._target, item)


def install() -> None:
    """在进程启动早期调用一次（重复调用无副作用）。"""
    global _installed
    with _lock:
        if _installed:
            return
        _installed = True
    stdout = getattr(sys, "__stdout__", None) or sys.stdout
    stderr = getattr(sys, "__stderr__", None) or sys.stderr
    sys.stdout = _Tee(stdout, "stdout")
    sys.stderr = _Tee(stderr, "stderr")


def query(
    limit: int = 200,
    level: str | None = None,
    q: str | None = None,
    after: int | None = None,
) -> dict[str, Any]:
    """按级别 / 关键字 / 游标查询，返回按时间正序的条目。"""
    min_order = _ORDER.get(_norm_level(level), -1) if level and level != "all" else -1
    needle = (q or "").strip().lower()

    with _lock:
        items = list(_entries)
        total = _seq

    out = []
    for e in items:
        if after is not None and e["id"] <= after:
            continue
        if min_order >= 0 and _ORDER.get(e["level"], 1) < min_order:
            continue
        if needle and needle not in e["message"].lower() and needle not in e["source"].lower():
            continue
        out.append(e)

    if limit and len(out) > limit:
        out = out[-limit:]
    return {"entries": out, "total": total, "capacity": MAX_ENTRIES}


def clear() -> None:
    with _lock:
        _entries.clear()
