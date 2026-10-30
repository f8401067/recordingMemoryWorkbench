from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query

from ..config import uid
from ..db import all_, execute, now_iso, one
from ..security import current_user
from ..services.providers import (
    find_provider,
    list_provider_models,
    list_providers,
    merge_probe_payload,
    provider_catalog,
    public_provider,
    test_provider,
)
from ..services.settings import read_settings, write_settings
from ..services.summarize import local_date

router = APIRouter(prefix="/api", tags=["providers"])


@router.get("/providers/catalog")
def catalog() -> dict:
    return provider_catalog()


def _probe_target(body: dict, user: dict) -> dict:
    """表单里还没保存的配置，加上已保存的 Key，拼成一条可用的服务商记录。"""
    saved = None
    provider_id = body.get("providerId")
    if provider_id:
        saved = find_provider(user["id"], provider_id)
        if not saved:
            raise HTTPException(404, "服务商不存在")
    merged = merge_probe_payload(body, saved)
    if not merged.get("kind"):
        raise HTTPException(400, "缺少服务商类型")
    return merged


@router.post("/providers/probe")
def probe_provider(
    body: dict[str, Any] = Body(default_factory=dict),
    user: dict = Depends(current_user),
) -> dict:
    """不用先保存：拿当前表单里的值直接测一次连通性。"""
    return test_provider(_probe_target(body, user))


@router.post("/providers/models")
def provider_models(
    body: dict[str, Any] = Body(default_factory=dict),
    user: dict = Depends(current_user),
) -> dict:
    """按当前地址与 Key 去服务商拉真实模型名，填进下拉。"""
    target = _probe_target(body, user)
    if str(target.get("kind") or "").startswith("local-"):
        raise HTTPException(400, "本地引擎的模型请从下面的列表里选")
    try:
        models = list_provider_models(
            target.get("kind"), target.get("base_url"), target.get("api_key")
        )
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, str(e)[:300]) from e
    return {"models": models, "count": len(models)}


@router.get("/providers")
def get_providers(
    category: str | None = None,
    user: dict = Depends(current_user),
) -> list[dict]:
    return list_providers(user["id"], category)


@router.post("/providers", status_code=201)
def create_provider(
    body: dict[str, Any] = Body(default_factory=dict),
    user: dict = Depends(current_user),
) -> dict:
    category = body.get("category")
    kind = body.get("kind")
    name = body.get("name")
    if not category or not kind or not name:
        raise HTTPException(400, "缺少 category / kind / name")
    if category not in ("asr", "llm"):
        raise HTTPException(400, "category 只能是 asr 或 llm")

    pid = uid("prv_")
    is_first = not one(
        "SELECT id FROM providers WHERE user_id = ? AND category = ?",
        (user["id"], category),
    )
    execute(
        """INSERT INTO providers
           (id, user_id, category, kind, name, base_url, api_key, model, params,
            is_default, enabled, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)""",
        (
            pid,
            user["id"],
            category,
            kind,
            str(name)[:40],
            body.get("baseUrl"),
            body.get("apiKey"),
            body.get("model"),
            json.dumps(body["params"], ensure_ascii=False) if body.get("params") else None,
            1 if (is_first or body.get("isDefault")) else 0,
            now_iso(),
        ),
    )
    if body.get("isDefault"):
        execute(
            "UPDATE providers SET is_default = 0 WHERE user_id = ? AND category = ? AND id <> ?",
            (user["id"], category, pid),
        )
    row = find_provider(user["id"], pid)
    assert row is not None
    return public_provider(row)


@router.patch("/providers/{provider_id}")
def update_provider(
    provider_id: str,
    body: dict[str, Any] = Body(default_factory=dict),
    user: dict = Depends(current_user),
) -> dict:
    p = find_provider(user["id"], provider_id)
    if not p:
        raise HTTPException(404, "服务商不存在")

    sets: list[str] = []
    params: list[Any] = []
    if body.get("name") is not None:
        sets.append("name = ?")
        params.append(str(body["name"])[:40])
    if body.get("baseUrl") is not None:
        sets.append("base_url = ?")
        params.append(body["baseUrl"] or None)
    # 空字符串表示"保留原 Key"，未传表示不变
    if body.get("apiKey") is not None and body.get("apiKey") != "":
        sets.append("api_key = ?")
        params.append(body["apiKey"])
    if body.get("model") is not None:
        sets.append("model = ?")
        params.append(body["model"] or None)
    if body.get("params") is not None:
        sets.append("params = ?")
        params.append(
            json.dumps(body["params"], ensure_ascii=False) if body["params"] else None
        )
    if body.get("enabled") is not None:
        sets.append("enabled = ?")
        params.append(1 if body["enabled"] else 0)
    if body.get("isDefault"):
        sets.append("is_default = 1")
        execute(
            "UPDATE providers SET is_default = 0 WHERE user_id = ? AND category = ? AND id <> ?",
            (user["id"], p["category"], provider_id),
        )
    if sets:
        params.extend([provider_id, user["id"]])
        execute(
            f"UPDATE providers SET {', '.join(sets)} WHERE id = ? AND user_id = ?",
            params,
        )
    row = find_provider(user["id"], provider_id)
    assert row is not None
    return public_provider(row)


@router.delete("/providers/{provider_id}")
def delete_provider(provider_id: str, user: dict = Depends(current_user)) -> dict:
    execute(
        "DELETE FROM providers WHERE id = ? AND user_id = ?",
        (provider_id, user["id"]),
    )
    return {"ok": True}


@router.post("/providers/{provider_id}/test")
def test_provider_route(provider_id: str, user: dict = Depends(current_user)) -> dict:
    p = find_provider(user["id"], provider_id)
    if not p:
        raise HTTPException(404, "服务商不存在")
    return test_provider(p)


# ---------------- 用户偏好 ----------------
@router.get("/settings")
def get_settings(user: dict = Depends(current_user)) -> dict:
    return read_settings(user["id"])


@router.patch("/settings")
def patch_settings(
    body: dict[str, Any] = Body(default_factory=dict),
    user: dict = Depends(current_user),
) -> dict:
    return write_settings(user["id"], body)


# ---------------- 概览统计 ----------------
@router.get("/stats")
def stats(user: dict = Depends(current_user)) -> dict:
    totals = one(
        """SELECT COUNT(*) AS recordings, COALESCE(SUM(duration_ms),0) AS durationMs,
                  COALESCE(SUM(LENGTH(COALESCE(memo,''))),0) AS memoChars
           FROM recordings WHERE user_id = ?""",
        (user["id"],),
    ) or {}
    chunks = one(
        """SELECT COALESCE(SUM(LENGTH(COALESCE(c.transcript,''))),0) AS chars,
                  COUNT(*) AS chunks,
                  SUM(CASE WHEN c.status='done' THEN 1 ELSE 0 END) AS done
           FROM chunks c JOIN recordings r ON r.id = c.recording_id WHERE r.user_id = ?""",
        (user["id"],),
    ) or {}
    summary_count = one(
        "SELECT COUNT(*) AS n FROM summaries WHERE user_id = ?",
        (user["id"],),
    ) or {"n": 0}
    days = one(
        "SELECT COUNT(DISTINCT substr(started_at,1,10)) AS n FROM recordings WHERE user_id = ?",
        (user["id"],),
    ) or {"n": 0}
    active_jobs = one(
        "SELECT COUNT(*) AS n FROM jobs WHERE status IN ('pending','running')"
    ) or {"n": 0}

    return {
        "recordings": totals.get("recordings") or 0,
        "durationMs": totals.get("durationMs") or 0,
        "chars": chunks.get("chars") or 0,
        "chunks": chunks.get("chunks") or 0,
        "doneChunks": chunks.get("done") or 0,
        "summaries": summary_count.get("n") or 0,
        "activeDays": days.get("n") or 0,
        "activeJobs": active_jobs.get("n") or 0,
    }


# ---------------- 日历 ----------------
@router.get("/calendar")
def calendar(
    year: int | None = Query(None),
    month: int | None = Query(None),
    frm: str | None = Query(None, alias="from"),
    to: str | None = Query(None),
    user: dict = Depends(current_user),
) -> dict:
    """按天聚合录音与纪要。

    - `?year=&month=`：某个月（月历视图）
    - `?from=&to=`：任意区间（周视图 / 年视图），日期格式 YYYY-MM-DD
    """
    now = datetime.now()
    if frm and to:
        try:
            start = datetime.fromisoformat(frm).astimezone()
            end = datetime.fromisoformat(f"{to}T23:59:59.999").astimezone()
        except ValueError as e:
            raise HTTPException(400, "日期格式应为 YYYY-MM-DD") from e
        d_from, d_to = frm, to
    else:
        y = year or now.year
        m = month or now.month
        start = datetime(y, m, 1).astimezone()
        nxt = datetime(y + 1, 1, 1) if m == 12 else datetime(y, m + 1, 1)
        end = nxt.astimezone() - timedelta(milliseconds=1)
        d_from, d_to = f"{y}-{m:02d}-01", f"{y}-{m:02d}-31"

    def z(dt: datetime) -> str:
        u = dt.astimezone(timezone.utc)
        return u.strftime("%Y-%m-%dT%H:%M:%S.") + f"{u.microsecond // 1000:03d}Z"

    recs = all_(
        """SELECT id, title, started_at, duration_ms, status FROM recordings
           WHERE user_id = ? AND started_at >= ? AND started_at <= ?
           ORDER BY started_at ASC""",
        (user["id"], z(start), z(end)),
    )
    sums = all_(
        """SELECT id, title, date_from, status, scope FROM summaries
           WHERE user_id = ? AND date_from >= ? AND date_from <= ?""",
        (user["id"], d_from, d_to),
    )

    days: dict[str, dict] = {}

    def bucket(key: str) -> dict:
        return days.setdefault(
            key,
            {
                "date": key,
                "recordings": 0,
                "durationMs": 0,
                "titles": [],
                "summaryId": None,
                "summaryStatus": None,
            },
        )

    for r in recs:
        k = local_date(r["started_at"])
        cur = bucket(k)
        cur["recordings"] += 1
        cur["durationMs"] += r.get("duration_ms") or 0
        if len(cur["titles"]) < 3:
            cur["titles"].append(r["title"])

    for s in sums:
        cur = bucket(s["date_from"])
        if s["scope"] != "recording":
            cur["summaryId"] = s["id"]
            cur["summaryStatus"] = s["status"]
            cur["summaryTitle"] = s["title"]

    return {
        "year": start.year,
        "month": start.month,
        "from": d_from,
        "to": d_to,
        "days": sorted(days.values(), key=lambda d: d["date"]),
    }
