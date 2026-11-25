"""对外 MCP 服务的工具清单与实现。

工具按「客户端拿忆录做什么」来设计，覆盖写入、按日期取纪要、检索、待办和概览。
所有查询都强制带 user_id —— Token 决定数据可见范围，客户端拿不到别人的录音。
"""

from __future__ import annotations

import base64
import binascii
import json
import re
from datetime import date
from typing import Any, Callable

from ..db import all_, one
from ..jobs import drain
from . import finder
from .audio_upload import create_transcription_job, ext_from_mime, wait_transcription
from .summarize import create_summary, day_range, local_date, local_time, public_summary
from .transcribe import transcript_for

MAX_AUDIO_BYTES = 32 * 1024 * 1024
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _date(value: Any, fallback_today: bool = True) -> str:
    raw = str(value or "").strip()
    if not raw:
        if fallback_today:
            return date.today().isoformat()
        raise ValueError("需要日期，格式 YYYY-MM-DD")
    if not _DATE_RE.match(raw):
        raise ValueError("日期格式应为 YYYY-MM-DD")
    try:
        date.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError(f"不是有效日期：{raw}") from exc
    return raw


def _int(value: Any, default: int, low: int = 1, high: int = 100) -> int:
    try:
        return max(low, min(high, int(value)))
    except (TypeError, ValueError):
        return default


def _stats(s: dict) -> dict:
    v = public_summary(s).get("stats")
    return v if isinstance(v, dict) else {}


def _text(value: Any, limit: int = 20_000) -> str:
    s = str(value or "")
    return s if len(s) <= limit else s[:limit] + f"\n…（已截断，共 {len(s)} 字）"


# ------------------------------------------------------------------ #
# 工具定义
# ------------------------------------------------------------------ #
TOOLS: list[dict] = [
    {
        "name": "upload_recording",
        "description": (
            "上传一段录音（音频文件的 base64），效果与设备/开放 API 上传一致："
            "建立录音记录、自动转写成文字，转写完成后按设置自动生成 AI 纪要。"
            "支持 wav / mp3 / webm / m4a / ogg 等 ffmpeg 能识别的格式。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "audioBase64": {
                    "type": "string",
                    "description": "音频文件内容的 base64，可带 data:audio/wav;base64, 前缀",
                },
                "mimeType": {
                    "type": "string",
                    "description": "音频 MIME，如 audio/wav / audio/mpeg / audio/webm；默认 audio/webm",
                },
                "filename": {"type": "string", "description": "原始文件名，用来推断扩展名，可选"},
                "title": {"type": "string", "description": "录音标题，不填自动生成"},
                "language": {"type": "string", "description": "识别语言 zh / en / auto，默认 zh"},
                "recordedAt": {"type": "string", "description": "录制开始时间 ISO8601，补传历史录音时建议带上"},
                "wait": {
                    "type": "boolean",
                    "description": "true 时同步等待转写完成并返回全文（最长 120 秒），默认 false 立即返回录音 id",
                },
                "timeoutMs": {"type": "integer", "description": "wait=true 时的最长等待毫秒数，默认 120000"},
            },
            "required": ["audioBase64"],
        },
    },
    {
        "name": "get_daily_summary",
        "description": (
            "对外输出指定日期的 AI 纪要：默认返回这一天的「每日回忆」全文，"
            "并附带当天每条录音各自的 AI 纪要、待办与高亮。"
            "日期格式 YYYY-MM-DD，不填取今天。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "date": {"type": "string", "description": "YYYY-MM-DD，不填取今天"},
                "format": {
                    "type": "string",
                    "enum": ["markdown", "json"],
                    "description": "markdown（默认，直接可读）或 json（结构化字段）",
                },
                "includeRecordings": {
                    "type": "boolean",
                    "description": "是否附带当天每条录音的 AI 纪要，默认 true",
                },
                "generateIfMissing": {
                    "type": "boolean",
                    "description": "当天还没有日报时是否立即触发生成，默认 false",
                },
            },
        },
    },
    {
        "name": "list_recordings",
        "description": "按日期区间列出录音（标题、时间、时长、转写状态、纪要状态），用于先看清单再取详情。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "dateFrom": {"type": "string", "description": "YYYY-MM-DD，不填取今天"},
                "dateTo": {"type": "string", "description": "YYYY-MM-DD，不填与 dateFrom 相同"},
                "limit": {"type": "integer", "description": "最多返回多少条，默认 20，上限 100"},
            },
        },
    },
    {
        "name": "get_recording",
        "description": "按录音 id 读取全文转写文字、句级时间戳和这条录音的 AI 纪要。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "recordingId": {"type": "string", "description": "录音 id，可由 list_recordings / search_memory 得到"},
                "maxChars": {"type": "integer", "description": "全文最多返回多少字，默认 20000"},
            },
            "required": ["recordingId"],
        },
    },
    {
        "name": "search_memory",
        "description": "在录音标题、备注、转写全文和 AI 纪要里做关键词检索，返回命中的片段及所在录音。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "要检索的关键词"},
                "limit": {"type": "integer", "description": "最多返回多少条命中，默认 8"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "list_summaries",
        "description": "列出已有的 AI 纪要 / 回忆（单条录音、日报、周报、月报等），可按日期区间与范围筛选。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "dateFrom": {"type": "string", "description": "YYYY-MM-DD，不填不限"},
                "dateTo": {"type": "string", "description": "YYYY-MM-DD，不填不限"},
                "scope": {
                    "type": "string",
                    "description": "recording / day / week / month / year / range，不填全部",
                },
                "limit": {"type": "integer", "description": "默认 20，上限 100"},
            },
        },
    },
    {
        "name": "get_action_items",
        "description": "读取待办清单：已确认的正式待办（可筛选状态）+ AI 从最近纪要里抽出的候选待办。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "enum": ["all", "pending", "done"],
                    "description": "正式待办的状态筛选，默认 all",
                },
                "limit": {"type": "integer", "description": "默认 50，上限 200"},
            },
        },
    },
    {
        "name": "generate_summary",
        "description": "触发（或重新）生成指定日期 / 区间的 AI 回忆。任务异步执行，返回纪要 id 与当前状态。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "date": {"type": "string", "description": "单日 YYYY-MM-DD；与 dateFrom/dateTo 二选一"},
                "dateFrom": {"type": "string", "description": "区间起点 YYYY-MM-DD"},
                "dateTo": {"type": "string", "description": "区间终点 YYYY-MM-DD"},
                "force": {"type": "boolean", "description": "已有纪要时是否强制重跑，默认 true"},
            },
        },
    },
    {
        "name": "get_service_status",
        "description": "查看这台忆录服务的概览：账号、录音总数、今日录音数、纪要数、后台队列状态。",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


# ------------------------------------------------------------------ #
# 工具实现
# ------------------------------------------------------------------ #
def tool_upload_recording(user_id: str, a: dict) -> dict:
    raw = str(a.get("audioBase64") or "").strip()
    if not raw:
        raise ValueError("audioBase64 不能为空")
    if raw.startswith("data:"):
        head, _, raw = raw.partition(",")
        mime = head[5:].split(";")[0] or "audio/webm"
    else:
        mime = str(a.get("mimeType") or "audio/webm")
    try:
        data = base64.b64decode(re.sub(r"\s+", "", raw), validate=False)
    except (binascii.Error, ValueError) as exc:
        raise ValueError(f"base64 解码失败：{exc}") from exc
    if not data:
        raise ValueError("音频内容为空")
    if len(data) > MAX_AUDIO_BYTES:
        raise ValueError(f"音频不能超过 {MAX_AUDIO_BYTES // 1024 // 1024} MB")

    filename = str(a.get("filename") or "").strip()
    if not filename:
        filename = "audio" + ext_from_mime(mime)

    job = create_transcription_job(
        user_id=user_id,
        data=data,
        mime=mime,
        filename=filename,
        title=str(a.get("title") or "").strip() or None,
        language=str(a.get("language") or "zh"),
        started_at=str(a.get("recordedAt") or "").strip() or None,
        source="api",
    )
    if bool(a.get("wait")):
        result = wait_transcription(job, _int(a.get("timeoutMs"), 120_000, 1000, 300_000))
        result["note"] = "转写完成后会按设置自动生成这条录音的 AI 纪要"
        return result
    return {
        "id": job["id"],
        "title": job["title"],
        "status": "processing",
        "durationMs": job["durationMs"],
        "size": job["size"],
        "note": "已排队转写，可用 get_recording 查询进度；完成后会自动生成 AI 纪要",
    }


def _day_summary_row(user_id: str, day: str) -> dict | None:
    return one(
        """SELECT * FROM summaries WHERE user_id = ? AND scope = 'day'
           AND date_from = ? AND date_to = ? ORDER BY created_at DESC LIMIT 1""",
        (user_id, day, day),
    )


def _recording_summaries(user_id: str, day: str, limit: int = 30) -> list[dict]:
    rows = all_(
        """SELECT s.*, r.title AS rec_title, r.started_at AS rec_started_at
           FROM summaries s LEFT JOIN recordings r ON r.id = s.recording_id
           WHERE s.user_id = ? AND s.scope = 'recording' AND s.date_from = ?
           ORDER BY r.started_at ASC LIMIT ?""",
        (user_id, day, limit),
    )
    out = []
    for r in rows:
        st = _stats(r)
        out.append(
            {
                "summaryId": r["id"],
                "recordingId": r.get("recording_id"),
                "title": r.get("rec_title") or r.get("title"),
                "time": local_time(r.get("rec_started_at")) if r.get("rec_started_at") else "",
                "status": r["status"],
                "markdown": r.get("markdown") or "",
                "todos": st.get("todos") or [],
                "highlights": st.get("highlights") or [],
            }
        )
    return out


def tool_get_daily_summary(user_id: str, a: dict) -> Any:
    day = _date(a.get("date"))
    fmt = str(a.get("format") or "markdown").lower()
    include = a.get("includeRecordings") is not False
    row = _day_summary_row(user_id, day)

    if not row and bool(a.get("generateIfMissing")):
        s = create_summary(userId=user_id, scope="day", dateFrom=day, dateTo=day, force=True)
        drain()
        row = one("SELECT * FROM summaries WHERE id = ?", (s["id"],))

    day_st = _stats(row) if row else {}
    recs = _recording_summaries(user_id, day) if include else []
    period_rows = all_(
        """SELECT id, title, scope, date_from, date_to, status FROM summaries
           WHERE user_id = ? AND scope IN ('range','week','month','year')
             AND date_from <= ? AND date_to >= ?
           ORDER BY date_from DESC LIMIT 5""",
        (user_id, day, day),
    )
    recordings_count = one(
        """SELECT COUNT(*) AS n FROM recordings WHERE user_id = ?
           AND started_at >= ? AND started_at <= ?""",
        (user_id, *day_range(day, day)),
    ) or {"n": 0}

    payload = {
        "date": day,
        "recordingCount": int(recordings_count["n"] or 0),
        "daySummary": (
            {
                "summaryId": row["id"],
                "title": row["title"],
                "status": row["status"],
                "markdown": row.get("markdown") or "",
                "mindmap": row.get("mindmap") or "",
                "todos": day_st.get("todos") or [],
                "highlights": day_st.get("highlights") or [],
                "moods": day_st.get("moods") or [],
                "updatedAt": row.get("updated_at"),
            }
            if row
            else None
        ),
        "recordingSummaries": recs,
        "periodSummaries": [
            {
                "summaryId": p["id"],
                "title": p["title"],
                "scope": p["scope"],
                "dateFrom": p["date_from"],
                "dateTo": p["date_to"],
                "status": p["status"],
            }
            for p in period_rows
        ],
        "hint": (
            "这一天还没有 AI 纪要，可把 generateIfMissing 设为 true 立即生成，"
            "或调用 generate_summary 工具。"
            if not row
            else ""
        ),
    }

    if fmt == "json":
        return payload

    lines = [f"# {day} 的 AI 纪要", ""]
    ds = payload["daySummary"]
    if ds:
        lines += [f"> 状态：{ds['status']} · 标题：{ds['title']}", "", ds["markdown"] or "（正文还在生成中）"]
        if ds["todos"]:
            lines += ["", "## 待办"]
            for t in ds["todos"]:
                lines.append(f"- {t.get('text') if isinstance(t, dict) else t}")
        if ds["highlights"]:
            lines += ["", "## 高亮"]
            for h in ds["highlights"]:
                lines.append(f"- {h.get('text') if isinstance(h, dict) else h}")
    else:
        lines += ["（这一天还没有生成每日回忆）", "", payload["hint"]]
    if recs:
        lines += ["", "## 当天各条录音的 AI 纪要"]
        for r in recs:
            lines += ["", f"### {r['time']} {r['title']}", "", r["markdown"] or "（还在生成中）"]
    lines += ["", "---", f"当天共 {payload['recordingCount']} 条录音 · {len(recs)} 份单条纪要"]
    return "\n".join(lines)


def tool_list_recordings(user_id: str, a: dict) -> dict:
    frm = _date(a.get("dateFrom"))
    to = _date(a.get("dateTo"), fallback_today=False) if a.get("dateTo") else frm
    if to < frm:
        raise ValueError("dateTo 不能早于 dateFrom")
    limit = _int(a.get("limit"), 20, 1, 100)
    start, end = day_range(frm, to)
    rows = all_(
        """SELECT id, title, started_at, ended_at, duration_ms, status,
                  summary_status, source, memo
           FROM recordings WHERE user_id = ? AND started_at >= ? AND started_at <= ?
           ORDER BY started_at DESC LIMIT ?""",
        (user_id, start, end, limit),
    )
    return {
        "dateFrom": frm,
        "dateTo": to,
        "count": len(rows),
        "recordings": [
            {
                "recordingId": r["id"],
                "title": r["title"],
                "date": local_date(r["started_at"]),
                "time": local_time(r["started_at"]),
                "startedAt": r["started_at"],
                "durationMs": r.get("duration_ms") or 0,
                "status": r["status"],
                "summaryStatus": r.get("summary_status"),
                "source": r.get("source"),
                "memo": r.get("memo") or "",
            }
            for r in rows
        ],
    }


def tool_get_recording(user_id: str, a: dict) -> dict:
    rid = str(a.get("recordingId") or "").strip()
    if not rid:
        raise ValueError("需要 recordingId")
    rec = one("SELECT * FROM recordings WHERE id = ? AND user_id = ?", (rid, user_id))
    if not rec:
        raise ValueError("录音不存在")
    t = transcript_for(rid)
    s = one(
        """SELECT * FROM summaries WHERE recording_id = ? AND user_id = ?
           ORDER BY created_at DESC LIMIT 1""",
        (rid, user_id),
    )
    st = _stats(s) if s else {}
    return {
        "recordingId": rid,
        "title": rec["title"],
        "date": local_date(rec["started_at"]),
        "time": local_time(rec["started_at"]),
        "status": rec["status"],
        "durationMs": rec.get("duration_ms") or 0,
        "summaryStatus": rec.get("summary_status"),
        "text": _text(t["text"], _int(a.get("maxChars"), 20_000, 500, 200_000)),
        "segments": t["segments"][:2000],
        "chunks": {"total": t["chunkCount"], "done": t["doneChunks"], "failed": t["failedChunks"]},
        "summary": (
            {
                "summaryId": s["id"],
                "title": s["title"],
                "status": s["status"],
                "markdown": s.get("markdown") or "",
                "todos": st.get("todos") or [],
                "highlights": st.get("highlights") or [],
            }
            if s
            else None
        ),
    }


def tool_search_memory(user_id: str, a: dict) -> dict:
    q = str(a.get("query") or a.get("q") or "").strip()
    if not q:
        raise ValueError("需要检索关键词 query")
    hits = finder.search_keyword(user_id, q, _int(a.get("limit"), 8, 1, 30))
    return {"query": q, "count": len(hits), "hits": hits}


def tool_list_summaries(user_id: str, a: dict) -> dict:
    where = ["user_id = ?"]
    params: list[Any] = [user_id]
    frm = str(a.get("dateFrom") or "").strip()
    to = str(a.get("dateTo") or "").strip()
    if frm:
        where.append("date_to >= ?")
        params.append(_date(frm, fallback_today=False))
    if to:
        where.append("date_from <= ?")
        params.append(_date(to, fallback_today=False))
    scope = str(a.get("scope") or "").strip()
    if scope:
        where.append("scope = ?")
        params.append(scope)
    params.append(_int(a.get("limit"), 20, 1, 100))
    rows = all_(
        f"""SELECT * FROM summaries WHERE {' AND '.join(where)}
            ORDER BY date_from DESC, created_at DESC LIMIT ?""",
        tuple(params),
    )
    return {
        "count": len(rows),
        "summaries": [
            {
                "summaryId": r["id"],
                "title": r["title"],
                "scope": r["scope"],
                "dateFrom": r["date_from"],
                "dateTo": r["date_to"],
                "status": r["status"],
                "recordingId": r.get("recording_id"),
                "preview": _text(r.get("markdown") or "", 200),
            }
            for r in rows
        ],
    }


def tool_get_action_items(user_id: str, a: dict) -> dict:
    status = str(a.get("status") or "all").lower()
    where = "user_id = ?" if status == "all" else "user_id = ? AND status = ?"
    params: list[Any] = [user_id] + ([] if status == "all" else [status])
    params.append(_int(a.get("limit"), 50, 1, 200))
    rows = all_(
        f"""SELECT id, text, tag, owner, due, date, status, created_at
            FROM action_items WHERE {where} ORDER BY created_at DESC LIMIT ?""",
        tuple(params),
    )
    items = [
        {
            "id": r["id"],
            "text": r["text"],
            "tag": r.get("tag") or "未分类",
            "owner": r.get("owner") or "",
            "due": r.get("due") or "",
            "date": r.get("date") or "",
            "status": r["status"],
            "createdAt": r["created_at"],
        }
        for r in rows
    ]

    # AI 候选：最近几份 ready 纪要里的 todos，去掉已经收进正式待办的
    known = {re.sub(r"\W+", "", i["text"]).lower() for i in items}
    suggestions: list[dict] = []
    for s in all_(
        """SELECT id, title, date_from, stats FROM summaries
           WHERE user_id = ? AND status = 'ready' AND stats IS NOT NULL
           ORDER BY date_from DESC LIMIT 30""",
        (user_id,),
    ):
        try:
            todos = (json.loads(s["stats"]) or {}).get("todos") or []
        except (TypeError, ValueError):
            continue
        for t in todos:
            text = str(t.get("text") if isinstance(t, dict) else t or "").strip()
            key = re.sub(r"\W+", "", text).lower()
            if not text or key in known:
                continue
            known.add(key)
            suggestions.append(
                {
                    "text": text,
                    "tag": (t.get("tag") if isinstance(t, dict) else "") or "未分类",
                    "date": s["date_from"],
                    "summaryId": s["id"],
                    "summaryTitle": s["title"],
                }
            )
    return {"items": items, "suggestions": suggestions[:50], "total": len(items)}


def tool_generate_summary(user_id: str, a: dict) -> dict:
    frm = str(a.get("dateFrom") or "").strip()
    to = str(a.get("dateTo") or "").strip()
    one_day = str(a.get("date") or "").strip()
    force = a.get("force") is not False
    if frm and to:
        _date(frm, fallback_today=False)
        _date(to, fallback_today=False)
        s = create_summary(
            userId=user_id, scope="range", dateFrom=frm, dateTo=to, force=force
        )
    else:
        day = _date(one_day)
        s = create_summary(
            userId=user_id, scope="day", dateFrom=day, dateTo=day, force=force
        )
    drain()
    return {
        "summaryId": s["id"],
        "scope": s["scope"],
        "dateFrom": s["date_from"],
        "dateTo": s["date_to"],
        "status": s["status"],
        "note": "整理任务已排队；周/月/年回忆会先自动补齐缺失的日报/月报再汇总，稍后用 get_daily_summary / list_summaries 读取结果",
    }


def tool_get_service_status(user_id: str, _a: dict) -> dict:
    total = one("SELECT COUNT(*) AS n FROM recordings WHERE user_id = ?", (user_id,)) or {"n": 0}
    today = date.today().isoformat()
    start, end = day_range(today, today)
    today_n = one(
        "SELECT COUNT(*) AS n FROM recordings WHERE user_id = ? AND started_at >= ? AND started_at <= ?",
        (user_id, start, end),
    ) or {"n": 0}
    summaries = one("SELECT COUNT(*) AS n FROM summaries WHERE user_id = ?", (user_id,)) or {"n": 0}
    ready = one(
        "SELECT COUNT(*) AS n FROM summaries WHERE user_id = ? AND status = 'ready'", (user_id,)
    ) or {"n": 0}
    pending = one("SELECT COUNT(*) AS n FROM jobs WHERE status IN ('pending','running')") or {"n": 0}
    last = one(
        "SELECT id, title, started_at FROM recordings WHERE user_id = ? ORDER BY started_at DESC LIMIT 1",
        (user_id,),
    )
    return {
        "service": "忆录 Recall MCP",
        "date": today,
        "recordings": int(total["n"] or 0),
        "recordingsToday": int(today_n["n"] or 0),
        "summaries": int(summaries["n"] or 0),
        "summariesReady": int(ready["n"] or 0),
        "jobsInFlight": int(pending["n"] or 0),
        "latestRecording": (
            {"recordingId": last["id"], "title": last["title"], "date": local_date(last["started_at"])}
            if last
            else None
        ),
        "toolCount": len(TOOLS),
    }


HANDLERS: dict[str, Callable[[str, dict], Any]] = {
    "upload_recording": tool_upload_recording,
    "get_daily_summary": tool_get_daily_summary,
    "list_recordings": tool_list_recordings,
    "get_recording": tool_get_recording,
    "search_memory": tool_search_memory,
    "list_summaries": tool_list_summaries,
    "get_action_items": tool_get_action_items,
    "generate_summary": tool_generate_summary,
    "get_service_status": tool_get_service_status,
}


def dispatch_tool(user_id: str, name: str, args: dict) -> str:
    """返回写进 MCP content[].text 的文本；业务错误直接抛，由协议层转成 isError。"""
    handler = HANDLERS.get(name)
    if not handler:
        raise KeyError(f"未知工具：{name}")
    result = handler(user_id, args if isinstance(args, dict) else {})
    if isinstance(result, str):
        return result
    return json.dumps(result, ensure_ascii=False, indent=2)
