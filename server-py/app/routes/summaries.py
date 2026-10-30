from __future__ import annotations

import json
import re
import urllib.parse
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from fastapi.responses import Response

from ..db import all_, execute, one
from ..jobs import drain
from ..security import current_user
from ..services.summarize import create_summary, public_summary

router = APIRouter(prefix="/api/summaries", tags=["summaries"])

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _stats_of(s: dict) -> Any:
    try:
        return json.loads(s["stats"]) if s.get("stats") else None
    except (json.JSONDecodeError, TypeError):
        return None


@router.get("")
def list_summaries(
    scope: str | None = None,
    from_: str | None = Query(None, alias="from"),
    to: str | None = None,
    limit: int = 60,
    user: dict = Depends(current_user),
) -> list[dict]:
    where = ["user_id = ?"]
    params: list[Any] = [user["id"]]
    if scope:
        where.append("scope = ?")
        params.append(scope)
    if from_:
        where.append("date_to >= ?")
        params.append(from_)
    if to:
        where.append("date_from <= ?")
        params.append(to)

    lim = max(1, min(200, limit or 60))
    rows = all_(
        f"""SELECT * FROM summaries WHERE {' AND '.join(where)}
            ORDER BY date_from DESC, created_at DESC LIMIT ?""",
        (*params, lim),
    )
    out: list[dict] = []
    for r in rows:
        p = public_summary(r)
        p.pop("markdown", None)
        p.pop("mindmap", None)
        p["highlights"] = (_stats_of(r) or {}).get("highlights") or []
        out.append(p)
    return out


@router.get("/{summary_id}")
def get_summary(summary_id: str, user: dict = Depends(current_user)) -> dict:
    s = one(
        "SELECT * FROM summaries WHERE id = ? AND user_id = ?",
        (summary_id, user["id"]),
    )
    if not s:
        raise HTTPException(404, "纪要不存在")
    return public_summary(s)


@router.post("/day")
def create_day_summary(
    body: dict[str, Any] = Body(default_factory=dict),
    user: dict = Depends(current_user),
) -> dict:
    date = str(body.get("date") or "")
    if not _DATE_RE.match(date):
        raise HTTPException(400, "日期格式应为 YYYY-MM-DD")
    try:
        s = create_summary(
            userId=user["id"],
            scope="day",
            dateFrom=date,
            dateTo=date,
            providerId=body.get("providerId"),
            template=body.get("template"),
            force=body.get("force", True),
        )
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    drain()
    return public_summary(s)


@router.post("/range")
def create_range_summary(
    body: dict[str, Any] = Body(default_factory=dict),
    user: dict = Depends(current_user),
) -> dict:
    frm = body.get("from")
    to = body.get("to")
    if not frm or not to:
        raise HTTPException(400, "需要 from 与 to")
    try:
        s = create_summary(
            userId=user["id"],
            scope="range",
            dateFrom=str(frm),
            dateTo=str(to),
            providerId=body.get("providerId"),
            template=body.get("template"),
            force=body.get("force", True),
        )
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    drain()
    return public_summary(s)


@router.delete("/{summary_id}")
def delete_summary(summary_id: str, user: dict = Depends(current_user)) -> dict:
    execute(
        "DELETE FROM summaries WHERE id = ? AND user_id = ?",
        (summary_id, user["id"]),
    )
    return {"ok": True}


@router.get("/{summary_id}/export")
def export_summary(summary_id: str, user: dict = Depends(current_user)) -> Response:
    s = one(
        "SELECT * FROM summaries WHERE id = ? AND user_id = ?",
        (summary_id, user["id"]),
    )
    if not s:
        raise HTTPException(404, "纪要不存在")

    span = s["date_from"] if s["date_to"] == s["date_from"] else f"{s['date_from']} - {s['date_to']}"
    md = "\n".join(
        [
            f"# {s['title']}",
            "",
            f"> {span}",
            "",
            s.get("markdown") or "",
            "",
            "---",
            "",
            "## 思维导图大纲",
            "",
            "```markdown",
            s.get("mindmap") or "",
            "```",
        ]
    )
    filename = urllib.parse.quote(s["title"])
    return Response(
        content=md,
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}.md"'},
    )
