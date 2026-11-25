from __future__ import annotations

import json
import re
import uuid
from datetime import date
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from ..db import all_, execute_returning, now_iso, one
from ..security import current_user
from ..services.prompts import TEMPLATE_LABEL, TODO_TAGS

router = APIRouter(prefix="/api/actions", tags=["actions"])

# 去重时忽略空白与标点，避免「去拿快递」「去拿快递。」被当成两件事
_NOISE_RE = re.compile(r"[\s\u3000，。！？、,.!?；;：:（）()【】\[\]“”\"'‘’\-—_…·]+")


def _norm_text(text: str) -> str:
    return _NOISE_RE.sub("", text).lower()


def _tag_of(raw: Any) -> str:
    tag = str(raw or "").strip()
    return tag if tag in TODO_TAGS else "未分类"


# ------------------------------------------------------------ 候选（猜你想做）


def _candidates(user_id: str, limit: int) -> list[dict]:
    """把各份纪要里的待办摊平成候选列表。

    同一件事可能同时出现在单条录音、日报和周报里，按文字去重后只保留最新一条；
    日报/周报里的日期取纪要的 date_from，旧数据没有 tag 的归到「未分类」。
    """
    rows = all_(
        """SELECT id, title, scope, date_from, date_to, template, created_at, updated_at, stats
           FROM summaries
           WHERE user_id = ? AND status = 'ready' AND stats IS NOT NULL
           ORDER BY date_from DESC, updated_at DESC LIMIT ?""",
        (user_id, limit),
    )

    items: list[dict] = []
    seen: set[str] = set()
    for row in rows:
        try:
            stats = json.loads(row["stats"])
        except (json.JSONDecodeError, TypeError):
            continue
        todos = stats.get("todos") if isinstance(stats, dict) else None
        if not isinstance(todos, list):
            continue

        for index, todo in enumerate(todos):
            if isinstance(todo, dict):
                text = str(todo.get("text") or "").strip()
                tag_value = _tag_of(todo.get("tag"))
            else:
                text = str(todo or "").strip()
                tag_value = "未分类"
            key = _norm_text(text)
            if not key or key in seen:
                continue
            seen.add(key)
            items.append(
                {
                    "originKey": key,
                    "text": text,
                    "tag": tag_value,
                    "owner": str(todo.get("owner") or "").strip() if isinstance(todo, dict) else "",
                    "due": str(todo.get("due") or "").strip() if isinstance(todo, dict) else "",
                    "date": row["date_from"],
                    "dateTo": row["date_to"],
                    "summaryId": row["id"],
                    "summaryTitle": row["title"],
                    "scope": row["scope"],
                    "source": TEMPLATE_LABEL.get(row.get("template") or "", "纪要"),
                    "createdAt": row["created_at"],
                }
            )
    return items


# ------------------------------------------------------------ 正式待办


def _item_of(row: dict) -> dict:
    return {
        "id": row["id"],
        "text": row["text"],
        "tag": _tag_of(row.get("tag")),
        "owner": row.get("owner") or "",
        "due": row.get("due") or "",
        "date": row.get("date") or "",
        "summaryId": row.get("summary_id") or "",
        "summaryTitle": row.get("summary_title") or "",
        "source": TEMPLATE_LABEL.get(row.get("summary_template") or "", "手动添加"),
        "status": row.get("status") or "pending",
        "createdAt": row["created_at"],
        "completedAt": row.get("completed_at"),
    }


def _saved_rows(user_id: str) -> list[dict]:
    return all_(
        """SELECT a.id, a.origin_key, a.text, a.tag, a.owner, a.due, a.date, a.summary_id,
                  a.status, a.completed_at, a.created_at,
                  s.title AS summary_title, s.template AS summary_template
           FROM action_items a
           LEFT JOIN summaries s ON s.id = a.summary_id
           WHERE a.user_id = ?
           ORDER BY a.created_at DESC""",
        (user_id,),
    )


def _counts(items: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = {"全部": len(items)}
    for t in TODO_TAGS:
        counts[t] = 0
    counts["未分类"] = 0
    for item in items:
        counts[item["tag"]] = counts.get(item["tag"], 0) + 1
    return counts


@router.get("")
def list_actions(
    tag: str | None = None,
    limit: int = Query(600, ge=1, le=1000),
    user: dict = Depends(current_user),
) -> dict:
    """行动页两段数据：

    - ``suggestions``：AI 抽出来、**今天**的、用户还没加进待办的候选；
    - ``items``：用户点过 + 的正式待办（带勾选状态）。
    """
    candidates = _candidates(user["id"], limit)
    saved = _saved_rows(user["id"])
    keys = {r["origin_key"] for r in saved if r.get("origin_key")}

    today = date.today().isoformat()
    suggestions = [c for c in candidates if c["date"] == today and c["originKey"] not in keys]

    items = [_item_of(r) for r in saved]
    counts = _counts(items)

    picked = (tag or "").strip()
    if picked and picked != "全部":
        items = [x for x in items if x["tag"] == picked]

    return {
        "items": items,
        "suggestions": suggestions,
        "total": len(items),
        "counts": counts,
        "tags": list(TODO_TAGS),
        "today": today,
    }


class AcceptBody(BaseModel):
    text: str
    tag: str = ""
    owner: str = ""
    due: str = ""
    date: str = ""
    summaryId: str = ""


@router.post("/accept")
def accept_action(body: AcceptBody, user: dict = Depends(current_user)) -> dict:
    """把一条「猜你想做」变成正式待办。重复提交同一句话会复用已有那条。"""
    text = body.text.strip()
    key = _norm_text(text)
    if not key:
        raise HTTPException(400, "待办内容不能为空")

    existing = one(
        "SELECT * FROM action_items WHERE user_id = ? AND origin_key = ?",
        (user["id"], key),
    )
    if existing:
        rows = _saved_rows(user["id"])
        row = next((r for r in rows if r["id"] == existing["id"]), None)
        return {"ok": True, "created": False, "item": _item_of(row) if row else None}

    summary_id = (body.summaryId or "").strip() or None
    if summary_id:
        owned = one("SELECT id FROM summaries WHERE id = ? AND user_id = ?", (summary_id, user["id"]))
        if not owned:
            summary_id = None

    new_id = uuid.uuid4().hex
    created_at = now_iso()
    execute_returning(
        """INSERT INTO action_items
             (id, user_id, origin_key, text, tag, owner, due, date, summary_id, status, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)
           ON CONFLICT (user_id, origin_key) DO NOTHING
           RETURNING id""",
        (
            new_id,
            user["id"],
            key,
            text,
            _tag_of(body.tag),
            body.owner.strip(),
            body.due.strip(),
            (body.date or "").strip() or date.today().isoformat(),
            summary_id,
            created_at,
        ),
    )
    rows = _saved_rows(user["id"])
    row = next((r for r in rows if r["origin_key"] == key), None)
    if row is None:
        raise HTTPException(500, "写入待办失败")
    return {"ok": True, "created": row["id"] == new_id, "item": _item_of(row)}


class PatchBody(BaseModel):
    status: str = ""


@router.patch("/{item_id}")
def patch_action(item_id: str, body: PatchBody, user: dict = Depends(current_user)) -> dict:
    """勾选 / 取消勾选正式待办。"""
    status = (body.status or "").strip()
    if status not in ("pending", "done"):
        raise HTTPException(400, "status 只能是 pending 或 done")

    row = one(
        "SELECT id FROM action_items WHERE id = ? AND user_id = ?",
        (item_id, user["id"]),
    )
    if not row:
        raise HTTPException(404, "待办不存在")

    execute_returning(
        """UPDATE action_items
           SET status = ?, completed_at = ?
           WHERE id = ? AND user_id = ?
           RETURNING id""",
        (status, now_iso() if status == "done" else None, item_id, user["id"]),
    )
    row = one(
        """SELECT a.id, a.origin_key, a.text, a.tag, a.owner, a.due, a.date, a.summary_id,
                  a.status, a.completed_at, a.created_at,
                  s.title AS summary_title, s.template AS summary_template
           FROM action_items a LEFT JOIN summaries s ON s.id = a.summary_id
           WHERE a.id = ? AND a.user_id = ?""",
        (item_id, user["id"]),
    )
    return {"ok": True, "item": _item_of(row) if row else None}
