"""系统状态：任务看板 + 进程日志。

设置页的「任务 / 日志」两个标签就靠这里。任务列表做了两件事：

1. 把 jobs 表里冷冰冰的 payload 翻译成人话（转写哪条录音的第几片、整理哪段时间的回忆）
2. 按归属过滤：任务表本身没有 user_id，只能顺着 payload 里的 summaryId / chunkId
   反查属主，查不到属主的（清理类任务）对所有人可见但不含用户内容。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from .. import jobs, logbuf
from ..db import all_, now_iso, one
from ..security import current_user

router = APIRouter(prefix="/api/system", tags=["system"])

TYPE_LABEL = {"transcribe": "语音转写", "summarize": "整理回忆"}

SCOPE_LABEL = {"recording": "单条录音", "day": "一天", "range": "一段时间"}


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _ms_between(start: str | None, end: datetime | None = None) -> int | None:
    s = _parse_iso(start)
    if not s:
        return None
    e = end or datetime.now(timezone.utc)
    if s.tzinfo is None:
        s = s.replace(tzinfo=timezone.utc)
    if e.tzinfo is None:
        e = e.replace(tzinfo=timezone.utc)
    return max(0, int((e - s).total_seconds() * 1000))


def _placeholders(n: int) -> str:
    return ", ".join("?" for _ in range(n))


def _enrich(rows: list[dict]) -> list[dict[str, Any]]:
    """把 payload 里的 id 批量换成标题，顺带带上属主用于鉴权。"""
    payloads: list[dict] = []
    for r in rows:
        try:
            payloads.append(json.loads(r.get("payload") or "{}"))
        except (json.JSONDecodeError, TypeError):
            payloads.append({})

    summary_ids = {p.get("summaryId") for p in payloads if p.get("summaryId")}
    chunk_ids = {p.get("chunkId") for p in payloads if p.get("chunkId")}

    summaries: dict[str, dict] = {}
    if summary_ids:
        for s in all_(
            f"""SELECT id, user_id, title, scope, date_from, date_to, status, template
                FROM summaries WHERE id IN ({_placeholders(len(summary_ids))})""",
            tuple(summary_ids),
        ):
            summaries[s["id"]] = s

    chunks: dict[str, dict] = {}
    if chunk_ids:
        for c in all_(
            f"""SELECT c.id, c.seq, c.recording_id, c.status, r.title, r.user_id
                FROM chunks c JOIN recordings r ON r.id = c.recording_id
                WHERE c.id IN ({_placeholders(len(chunk_ids))})""",
            tuple(chunk_ids),
        ):
            chunks[c["id"]] = c

    out: list[dict[str, Any]] = []
    for row, payload in zip(rows, payloads):
        owner: str | None = None
        title = ""
        detail = ""
        scope: str | None = None
        recording_id: str | None = None

        if payload.get("summaryId") and payload["summaryId"] in summaries:
            s = summaries[payload["summaryId"]]
            owner = s["user_id"]
            scope = s["scope"]
            span = s["date_from"] if s["date_to"] == s["date_from"] else f"{s['date_from']} ~ {s['date_to']}"
            title = s["title"] or span
            detail = f"{SCOPE_LABEL.get(s['scope'], s['scope'])} · {span}"
        elif payload.get("chunkId") and payload["chunkId"] in chunks:
            c = chunks[payload["chunkId"]]
            owner = c["user_id"]
            recording_id = c["recording_id"]
            title = c["title"] or "未命名录音"
            detail = f"第 {c['seq'] + 1} 片 · {c['status']}"
        else:
            title = TYPE_LABEL.get(row["type"], row["type"])
            detail = "无关联记录（可能已被删除）"

        status = row["status"]
        elapsed = _ms_between(row.get("started_at"), _parse_iso(row.get("finished_at")) if row.get("finished_at") else None)
        out.append(
            {
                "id": row["id"],
                "type": row["type"],
                "typeLabel": TYPE_LABEL.get(row["type"], row["type"]),
                "status": status,
                "title": title,
                "detail": detail,
                "scope": scope,
                "recordingId": recording_id,
                "attempts": row.get("attempts") or 0,
                "maxAttempts": row.get("max_attempts") or 3,
                "error": row.get("error"),
                "createdAt": row.get("created_at"),
                "startedAt": row.get("started_at"),
                "heartbeatAt": row.get("heartbeat_at"),
                "finishedAt": row.get("finished_at"),
                "runAt": row.get("run_at"),
                "elapsedMs": elapsed,
                "heartbeatAgeMs": _ms_between(row.get("heartbeat_at")) if status == "running" else None,
                "ownerId": owner,
                "retryable": status == "failed",
            }
        )
    return out


def _owned(rows: list[dict], user_id: str) -> list[dict[str, Any]]:
    return [t for t in _enrich(rows) if not t["ownerId"] or t["ownerId"] == user_id]


@router.get("/tasks")
def task_board(
    limit: int = Query(30, ge=1, le=200),
    user: dict = Depends(current_user),
) -> dict:
    running_rows = all_(
        "SELECT * FROM jobs WHERE status = 'running' ORDER BY started_at ASC"
    )
    queued_rows = all_(
        "SELECT * FROM jobs WHERE status = 'pending' ORDER BY run_at ASC, created_at ASC LIMIT ?",
        (limit,),
    )
    recent_rows = all_(
        """SELECT * FROM jobs WHERE status IN ('done', 'failed')
           ORDER BY COALESCE(finished_at, created_at) DESC LIMIT ?""",
        (limit,),
    )

    running = _owned(running_rows, user["id"])
    queued = _owned(queued_rows, user["id"])
    recent = _owned(recent_rows, user["id"])

    counts = {"running": len(running), "queued": len(queued), "failed": 0, "done": 0}
    for t in recent:
        if t["status"] == "failed":
            counts["failed"] += 1
        else:
            counts["done"] += 1

    return {
        "now": now_iso(),
        "running": running,
        "queued": queued,
        "recent": recent,
        "counts": counts,
        "concurrency": jobs.concurrency_map(),
        "worker": jobs.heartbeat_config(),
    }


@router.post("/tasks/{job_id}/retry")
def retry_task(job_id: str, user: dict = Depends(current_user)) -> dict:
    row = jobs.get_job(job_id)
    if not row:
        raise HTTPException(404, "任务不存在")
    task = _owned([row], user["id"])
    if not task:
        raise HTTPException(404, "任务不存在")
    if row["status"] not in ("failed", "done"):
        raise HTTPException(400, "只有已结束的任务可以重新排队")
    if not jobs.requeue(job_id):
        raise HTTPException(400, "重新排队失败")
    jobs.drain()
    return {"ok": True}


@router.get("/logs")
def list_logs(
    limit: int = Query(300, ge=1, le=2000),
    level: str | None = None,
    q: str | None = None,
    after: int | None = None,
    user: dict = Depends(current_user),  # noqa: ARG001 - 只为鉴权
) -> dict:
    return logbuf.query(limit=limit, level=level, q=q, after=after)


@router.delete("/logs")
def clear_logs(user: dict = Depends(current_user)) -> dict:  # noqa: ARG001
    logbuf.clear()
    return {"ok": True}
