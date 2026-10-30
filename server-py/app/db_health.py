"""SQLite 的三个保命动作：单实例锁、启动自检、自动备份。

来由：一次真实事故。当时同一个 `data/recall.db` 被两个后端进程同时打开，
外加脚本在跑 `PRAGMA integrity_check` / 手工 cp，-wal 与 -shm 状态对不上，
日志先刷 "disk I/O error"，随后变成 "database disk image is malformed"，
最后只能靠一份手工拷贝的副本把数据捞回来。

所以这里做三件事：

1. `acquire_instance_lock()`：同一个 SQLite 文件只允许一个后端进程写。
   `flock` 随进程退出自动释放，不会留死锁文件。
2. `quick_check()`：启动时体检，坏了就明确说坏了，并且**不写**，
   而不是让每个接口都回一句模糊的 503。
3. `backup_snapshot()`：每次启动成功后留一份 `VACUUM INTO` 快照，
   只保留最近几份。真空导出是原子的，不会拷到半个 WAL 状态。

以上都只对内置 SQLite 生效；外置 PostgreSQL 有自己的备份体系，不需要这些。
"""

from __future__ import annotations

import os
import re
import sqlite3
import time
from pathlib import Path

from .db_config import SQLITE_PATH

DATA_DIR = SQLITE_PATH.parent
BACKUP_DIR = DATA_DIR / "backups"
LOCK_PATH = DATA_DIR / ".sqlite.lock"

KEEP_BACKUPS = 5
_SAFE_NAME = re.compile(r"^recall-(\d{8}-\d{6})\.db$")

_lock_file = None  # 持有引用，进程存活期间锁就在


# ------------------------------------------------------------ 单实例锁

def acquire_instance_lock() -> str | None:
    """拿到独占锁返回 None，被别的进程占着返回占用者描述。"""
    global _lock_file

    try:
        import fcntl
    except ImportError:  # pragma: no cover - Windows 走不到这里（本项目只跑 Linux/macOS）
        return None

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    fh = open(LOCK_PATH, "a+", encoding="utf-8")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.seek(0)
        holder = fh.read().strip() or "未知进程"
        fh.close()
        return holder

    fh.seek(0)
    fh.truncate()
    fh.write(f"pid={os.getpid()} since={time.strftime('%Y-%m-%d %H:%M:%S')}\n")
    fh.flush()
    _lock_file = fh
    return None


def release_instance_lock() -> None:
    global _lock_file
    if _lock_file is None:
        return
    try:
        import fcntl

        fcntl.flock(_lock_file.fileno(), fcntl.LOCK_UN)
    except Exception:  # noqa: BLE001
        pass
    try:
        _lock_file.close()
    except Exception:  # noqa: BLE001
        pass
    _lock_file = None


# ------------------------------------------------------------ 启动自检

def quick_check(path: Path | None = None) -> str | None:
    """体检通过返回 None，否则返回 SQLite 的原话。

    先跑 `quick_check`，再对小库补一遍 `integrity_check`。
    只跑 quick_check 会漏掉「索引页坏了但表页还好」这种情况：那时
    `PRAGMA quick_check` 回 ok，可一查表就报 malformed——2026-10-30 就是这么
    带着坏库跑起来的，`COUNT(*)` 全挂而启动自检说没问题。
    大库（>200MB）跳过第二轮，避免拖慢启动。
    """
    target = path or SQLITE_PATH
    if not target.exists():
        return None

    def run(pragma: str) -> str:
        conn = sqlite3.connect(f"file:{target}?mode=ro", uri=True)
        try:
            rows = conn.execute(pragma).fetchall()
        finally:
            conn.close()
        text = "; ".join(str(r[0]) for r in rows) if rows else ""
        return "" if text.strip().lower() == "ok" else (text or "未知损坏")

    try:
        first = run("PRAGMA quick_check(1)")
        if first:
            return first
        if target.stat().st_size <= 200 * 1024 * 1024:
            return run("PRAGMA integrity_check(20)") or None
        return None
    except sqlite3.DatabaseError as exc:
        return str(exc)


# ------------------------------------------------------------ 自动备份

def backup_snapshot(prefix: str = "recall") -> Path | None:
    """导出当前库快照，返回文件路径；SQLite 不可用时返回 None。"""
    if not SQLITE_PATH.exists():
        return None
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out = BACKUP_DIR / f"{prefix}-{stamp}.db"
    if out.exists():  # 同一秒启两次
        return out

    # 从只读连接 VACUUM INTO，避免影响业务连接的事务状态
    src = sqlite3.connect(f"file:{SQLITE_PATH}?mode=ro", uri=True)
    try:
        src.execute("VACUUM INTO ?", (str(out),))
    finally:
        src.close()

    _prune_backups()
    return out


def _prune_backups() -> None:
    try:
        files = sorted(
            (p for p in BACKUP_DIR.iterdir() if _SAFE_NAME.match(p.name)),
            key=lambda p: p.name,
        )
    except OSError:
        return
    for old in files[:-KEEP_BACKUPS] if len(files) > KEEP_BACKUPS else []:
        try:
            old.unlink()
        except OSError:
            pass


def latest_backup() -> Path | None:
    """最近一份自动备份，供启动报错时提示用。"""
    try:
        files = [p for p in BACKUP_DIR.iterdir() if _SAFE_NAME.match(p.name)]
    except OSError:
        return None
    return max(files, key=lambda p: p.name) if files else None
