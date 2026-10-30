from __future__ import annotations

import json
import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

from .db import all_, execute, now_iso, one

Handler = Callable[[dict], None]

_handlers: dict[str, tuple[Handler, int]] = {}
_running: dict[str, int] = {}
_lock = threading.Lock()
_pool: ThreadPoolExecutor | None = None
_scheduler: threading.Thread | None = None
_stop = threading.Event()


def register(type_: str, fn: Handler, concurrency: int = 2) -> None:
    _handlers[type_] = (fn, concurrency)
    _running.setdefault(type_, 0)


def enqueue(
    type_: str,
    payload: dict,
    run_at: str | None = None,
    max_attempts: int = 3,
) -> str:
    job_id = "job_" + uuid.uuid4().hex[:20]
    execute(
        """INSERT INTO jobs (id, type, payload, status, attempts, max_attempts, run_at, created_at)
           VALUES (?, ?, ?, 'pending', 0, ?, ?, ?)""",
        (
            job_id,
            type_,
            json.dumps(payload, ensure_ascii=False),
            max_attempts,
            run_at or now_iso(),
            now_iso(),
        ),
    )
    # 立刻唤醒调度线程，避免等下一个轮询周期
    with _lock:
        pass
    _wake.set()
    return job_id


_wake = threading.Event()


def _claim(job_id: str) -> bool:
    cur = execute(
        """UPDATE jobs SET status = 'running', started_at = ?, attempts = attempts + 1
           WHERE id = ? AND status = 'pending'""",
        (now_iso(), job_id),
    )
    return cur.rowcount > 0


def _backoff_ms(attempts: int) -> int:
    return min(60_000, 3000 * (2 ** attempts))


def _run_job(job: dict) -> None:
    job_id = job["id"]
    type_ = job["type"]
    handler = _handlers.get(type_)
    if not handler:
        execute("UPDATE jobs SET status='failed', error='no handler' WHERE id = ?", (job_id,))
        return

    fn, _ = handler
    try:
        payload = json.loads(job["payload"] or "{}")
        fn(payload)
        execute("UPDATE jobs SET status='done', finished_at=? WHERE id = ?", (now_iso(), job_id))
    except Exception as e:  # noqa: BLE001
        msg = f"{e}"[:800]
        traceback.print_exc()
        fresh = one("SELECT attempts, max_attempts FROM jobs WHERE id = ?", (job_id,)) or {}
        attempts = int(fresh.get("attempts") or 1)
        if attempts < int(fresh.get("max_attempts") or 3):
            run_at = time.strftime(
                "%Y-%m-%dT%H:%M:%S",
                time.gmtime(time.time() + _backoff_ms(attempts) / 1000),
            ) + ".000Z"
            execute(
                "UPDATE jobs SET status='pending', run_at=?, error=? WHERE id = ?",
                (run_at, msg, job_id),
            )
        else:
            execute(
                "UPDATE jobs SET status='failed', finished_at=?, error=? WHERE id = ?",
                (now_iso(), msg, job_id),
            )
    finally:
        with _lock:
            _running[type_] = max(0, _running.get(type_, 1) - 1)


def drain() -> None:
    """扫描到期任务并提交到线程池。"""
    pending = all_(
        "SELECT * FROM jobs WHERE status='pending' AND run_at <= ? ORDER BY created_at ASC LIMIT 40",
        (now_iso(),),
    )
    for job in pending:
        type_ = job["type"]
        handler = _handlers.get(type_)
        if not handler:
            execute("UPDATE jobs SET status='failed', error='no handler' WHERE id = ?", (job["id"],))
            continue
        _, concurrency = handler
        with _lock:
            current = _running.get(type_, 0)
            if current >= concurrency:
                continue
            _running[type_] = current + 1
        if not _claim(job["id"]):
            with _lock:
                _running[type_] = max(0, _running.get(type_, 1) - 1)
            continue
        fresh = one("SELECT * FROM jobs WHERE id = ?", (job["id"],)) or job
        assert _pool is not None
        _pool.submit(_run_job, fresh)


def _loop(interval: float) -> None:
    while not _stop.is_set():
        try:
            drain()
        except Exception:  # noqa: BLE001
            traceback.print_exc()
        _wake.wait(interval)
        _wake.clear()


def start_worker(interval: float = 1.0) -> None:
    global _pool, _scheduler
    # 把上次进程崩溃时卡在 running 的任务放回队列
    execute("UPDATE jobs SET status='pending', run_at=? WHERE status='running'", (now_iso(),))
    _pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="job")
    _stop.clear()
    _scheduler = threading.Thread(target=_loop, args=(interval,), daemon=True, name="job-scheduler")
    _scheduler.start()


def stop_worker() -> None:
    _stop.set()
    _wake.set()
    if _pool:
        _pool.shutdown(wait=False, cancel_futures=True)


def queue_stats() -> list[dict[str, Any]]:
    return all_("SELECT type, status, COUNT(*) AS n FROM jobs GROUP BY type, status")
