from __future__ import annotations

import json
import os
import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from . import logbuf
from .db import all_, execute, execute_returning, now_iso, one

Handler = Callable[[dict], None]

_handlers: dict[str, tuple[Handler, int]] = {}
_running: dict[str, int] = {}
_in_flight: set[str] = set()  # 正在执行、需要刷心跳的任务
_lock = threading.Lock()
_pool: ThreadPoolExecutor | None = None
_scheduler: threading.Thread | None = None
_heartbeat: threading.Thread | None = None
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
    logbuf.log("debug", "job", f"入队 {type_} · {job_id} · {json.dumps(payload, ensure_ascii=False)[:200]}")
    # 立刻唤醒调度线程，避免等下一个轮询周期
    with _lock:
        pass
    _wake.set()
    return job_id


_wake = threading.Event()


def _claim_one(type_: str) -> dict[str, Any] | None:
    """原子领取一条到期任务。

    用 `FOR UPDATE SKIP LOCKED`：多个副本（或以后多机）同时抢，谁先拿到算谁的，
    后来者直接跳过这行不排队等锁。
    """
    ts = now_iso()
    lock = "FOR UPDATE SKIP LOCKED"
    sql = f"""UPDATE jobs
                 SET status = 'running', started_at = ?, heartbeat_at = ?, attempts = attempts + 1
               WHERE id = (
                     SELECT id FROM jobs
                      WHERE status = 'pending' AND type = ? AND run_at <= ?
                      ORDER BY created_at ASC
                      LIMIT 1 {lock}
               )
               RETURNING id, type, payload, attempts, max_attempts"""
    return execute_returning(sql, (ts, ts, type_, ts))


def _backoff_ms(attempts: int) -> int:
    return min(60_000, 3000 * (2 ** attempts))


def _run_job(job: dict) -> None:
    job_id = job["id"]
    type_ = job["type"]
    handler = _handlers.get(type_)
    if not handler:
        execute("UPDATE jobs SET status='failed', error='no handler' WHERE id = ?", (job_id,))
        logbuf.log("error", "job", f"{type_} · {job_id} 没有注册处理函数，任务作废")
        return

    fn, _ = handler
    with _lock:
        _in_flight.add(job_id)
    started = time.monotonic()
    logbuf.log("info", "job", f"开始 {type_} · {job_id}（第 {job.get('attempts') or 1} 次）")
    try:
        payload = json.loads(job["payload"] or "{}")
        fn(payload)
        execute("UPDATE jobs SET status='done', finished_at=? WHERE id = ?", (now_iso(), job_id))
        logbuf.log("info", "job", f"完成 {type_} · {job_id} · {int((time.monotonic() - started) * 1000)}ms")
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
            logbuf.log(
                "warn",
                "job",
                f"失败 {type_} · {job_id}（第 {attempts} 次），{_backoff_ms(attempts) // 1000}s 后重试：{msg}",
            )
        else:
            execute(
                "UPDATE jobs SET status='failed', finished_at=?, error=? WHERE id = ?",
                (now_iso(), msg, job_id),
            )
            logbuf.log("error", "job", f"放弃 {type_} · {job_id}（已试 {attempts} 次）：{msg}")
    finally:
        with _lock:
            _in_flight.discard(job_id)
            _running[type_] = max(0, _running.get(type_, 1) - 1)


def drain() -> None:
    """按各类型的并发上限领取到期任务，交给线程池。"""
    for type_, (_, concurrency) in list(_handlers.items()):
        while True:
            with _lock:
                if _running.get(type_, 0) >= concurrency:
                    break
                _running[type_] = _running.get(type_, 0) + 1
            try:
                job = _claim_one(type_)
            except Exception:  # noqa: BLE001 - 领取失败要先把计数还回去
                with _lock:
                    _running[type_] = max(0, _running.get(type_, 1) - 1)
                raise
            if job is None:
                with _lock:
                    _running[type_] = max(0, _running.get(type_, 1) - 1)
                break
            assert _pool is not None
            _pool.submit(_run_job, job)


# 心跳：执行中的任务每 15 秒刷一次 heartbeat_at。看门狗只认心跳，
# 长任务只要还在跑就不会被误判成僵尸 —— 判活的是"进程有没有在动"，不是"跑了多久"。
HEARTBEAT_SECONDS = float(os.getenv("JOB_HEARTBEAT_SECONDS", "15"))
# 心跳停这么久就认为任务死了（引擎 native 死锁、进程被强杀），放回队列。
STALE_SECONDS = float(os.getenv("JOB_STALE_SECONDS", "120"))
_REAP_INTERVAL = 30.0
_last_reap = 0.0


def _heartbeat_loop() -> None:
    while not _stop.is_set():
        with _lock:
            ids = list(_in_flight)
        if ids:
            ts = now_iso()
            for job_id in ids:
                try:
                    execute(
                        "UPDATE jobs SET heartbeat_at = ? WHERE id = ? AND status = 'running'",
                        (ts, job_id),
                    )
                except Exception:  # noqa: BLE001 - 心跳失败下一轮再来，别把调度线程带崩
                    traceback.print_exc()
        _stop.wait(HEARTBEAT_SECONDS)


def reap_stale() -> int:
    """把心跳停掉的任务放回队列。返回重置数量。"""
    cutoff = (
        (datetime.now(timezone.utc) - timedelta(seconds=STALE_SECONDS))
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )
    cur = execute(
        """UPDATE jobs SET status='pending', run_at=?, error=?, heartbeat_at=NULL
           WHERE status='running'
             AND COALESCE(heartbeat_at, started_at) IS NOT NULL
             AND COALESCE(heartbeat_at, started_at) < ?""",
        (now_iso(), f"心跳停止超过 {int(STALE_SECONDS)}s，已自动重新排队", cutoff),
    )
    return int(getattr(cur, "rowcount", 0) or 0)


def _loop(interval: float) -> None:
    global _last_reap
    while not _stop.is_set():
        try:
            drain()
            if time.monotonic() - _last_reap >= _REAP_INTERVAL:
                _last_reap = time.monotonic()
                n = reap_stale()
                if n:
                    print(f"[jobs] {n} 个任务超过 {int(STALE_SECONDS)}s 未结束，已重新排队")
                    logbuf.log("warn", "job", f"{n} 个任务心跳中断超过 {int(STALE_SECONDS)}s，已重新排队")
        except Exception:  # noqa: BLE001
            traceback.print_exc()
        _wake.wait(interval)
        _wake.clear()


def start_worker(interval: float = 1.0) -> None:
    global _pool, _scheduler, _heartbeat
    # 把上次进程崩溃时卡在 running 的任务放回队列
    execute(
        "UPDATE jobs SET status='pending', run_at=?, heartbeat_at=NULL WHERE status='running'",
        (now_iso(),),
    )
    _pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="job")
    _stop.clear()
    _scheduler = threading.Thread(target=_loop, args=(interval,), daemon=True, name="job-scheduler")
    _scheduler.start()
    _heartbeat = threading.Thread(target=_heartbeat_loop, daemon=True, name="job-heartbeat")
    _heartbeat.start()


def stop_worker() -> None:
    _stop.set()
    _wake.set()
    if _pool:
        _pool.shutdown(wait=False, cancel_futures=True)


def queue_stats() -> list[dict[str, Any]]:
    return all_("SELECT type, status, COUNT(*) AS n FROM jobs GROUP BY type, status")


def concurrency_map() -> dict[str, int]:
    with _lock:
        return {t: c for t, (_, c) in _handlers.items()}


def heartbeat_config() -> dict[str, float]:
    return {"heartbeatSeconds": HEARTBEAT_SECONDS, "staleSeconds": STALE_SECONDS}


def get_job(job_id: str) -> dict[str, Any] | None:
    return one("SELECT * FROM jobs WHERE id = ?", (job_id,))


def requeue(job_id: str) -> bool:
    """把失败任务放回队列（失败重试用）。返回是否真的改动了。"""
    cur = execute(
        """UPDATE jobs SET status='pending', attempts=0, error=NULL, run_at=?,
                          started_at=NULL, heartbeat_at=NULL, finished_at=NULL
           WHERE id = ? AND status IN ('failed', 'done')""",
        (now_iso(), job_id),
    )
    changed = bool(getattr(cur, "rowcount", 0))
    if changed:
        logbuf.log("info", "job", f"手动重新排队 {job_id}")
        _wake.set()
    return changed
