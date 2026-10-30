from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any

from ..bus import emit_recording
from ..config import uid
from ..db import all_, execute, now_iso, one
from ..jobs import enqueue
from .prompts import TEMPLATE_LABEL, build_chunk_prompt, build_system_prompt
from .providers import build_llm, resolve_llm
from .settings import read_settings
from .transcribe import transcript_with_timestamps

SummaryTemplate = str

MAX_INPUT_CHARS = 16_000
MAP_CHUNK_CHARS = 12_000


def _parse(iso: str | None) -> datetime:
    s = (iso or "").replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        dt = datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.astimezone()  # 视为本地时间
    return dt


def _to_z(dt: datetime) -> str:
    u = dt.astimezone(timezone.utc)
    return u.strftime("%Y-%m-%dT%H:%M:%S.") + f"{u.microsecond // 1000:03d}Z"


def local_date(iso: str | None) -> str:
    d = _parse(iso)
    return f"{d.year}-{d.month:02d}-{d.day:02d}"


def local_time(iso: str | None) -> str:
    d = _parse(iso)
    return f"{d.hour:02d}:{d.minute:02d}"


def day_range(date_from: str, date_to: str) -> tuple[str, str]:
    start = datetime.fromisoformat(f"{date_from}T00:00:00").astimezone()
    end = datetime.fromisoformat(f"{date_to}T23:59:59.999").astimezone()
    return _to_z(start), _to_z(end)


# ------------------------------------------------------------------ #
# 创建纪要任务
# ------------------------------------------------------------------ #
def create_summary(
    *,
    userId: str,
    scope: str,
    recordingId: str | None = None,
    dateFrom: str | None = None,
    dateTo: str | None = None,
    providerId: str | None = None,
    template: str | None = None,
    force: bool = False,
    title: str | None = None,
) -> dict:
    d_from = dateFrom or ""
    d_to = dateTo or d_from

    if scope == "recording":
        rec = one(
            "SELECT * FROM recordings WHERE id = ? AND user_id = ?",
            (recordingId, userId),
        )
        if not rec:
            raise ValueError("录音不存在")
        d_from = local_date(rec["started_at"])
        d_to = d_from
    if scope == "day":
        d_to = d_from

    if scope == "recording":
        existing = one("SELECT * FROM summaries WHERE recording_id = ?", (recordingId,))
    else:
        existing = one(
            """SELECT * FROM summaries WHERE user_id = ? AND scope = ? AND date_from = ? AND date_to = ?""",
            (userId, scope, d_from, d_to),
        )

    # 重新生成时没指定风格 → 沿用这条纪要原来的风格，避免和用户当时的选择不一致
    tpl = template or (existing.get("template") if existing else None) or "meeting"
    t = now_iso()

    if existing:
        if not force and existing["status"] in ("queued", "generating", "ready"):
            return existing
        execute(
            """UPDATE summaries SET status = 'queued', error = NULL, provider_id = ?,
               template = ?, title = ?, updated_at = ? WHERE id = ?""",
            (providerId, tpl, title or existing["title"], t, existing["id"]),
        )
        summary = one("SELECT * FROM summaries WHERE id = ?", (existing["id"],))
    else:
        sid = uid("sum_")
        if title:
            new_title = title
        elif scope == "recording":
            new_title = ""
        elif scope == "day":
            new_title = f"{d_from} 日报"
        else:
            new_title = f"{d_from} 至 {d_to} 汇总"
        execute(
            """INSERT INTO summaries
               (id, user_id, recording_id, scope, date_from, date_to, title, provider_id,
                template, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?)""",
            (sid, userId, recordingId, scope, d_from, d_to, new_title, providerId, tpl, t, t),
        )
        summary = one("SELECT * FROM summaries WHERE id = ?", (sid,))

    assert summary is not None

    if scope == "recording" and recordingId:
        execute("UPDATE recordings SET summary_status = 'queued' WHERE id = ?", (recordingId,))
        emit_recording(recordingId, "summary", {"status": "queued", "summaryId": summary["id"]})

    enqueue(
        "summarize",
        {"summaryId": summary["id"], "template": tpl, "force": bool(force)},
        max_attempts=2,
    )
    return summary


# ------------------------------------------------------------------ #
# 生成纪要
# ------------------------------------------------------------------ #
def _build_context(summary: dict, template: str) -> dict[str, Any]:
    label = TEMPLATE_LABEL.get(template, TEMPLATE_LABEL["meeting"])

    if summary["scope"] == "recording" and summary.get("recording_id"):
        rec = one("SELECT * FROM recordings WHERE id = ?", (summary["recording_id"],))
        if not rec:
            raise ValueError("录音不存在")
        text = transcript_with_timestamps(rec["id"])
        if not text.strip():
            raise ValueError("该录音还没有可用的转写内容")
        cnt = one("SELECT COUNT(*) AS n FROM chunks WHERE recording_id = ?", (rec["id"],)) or {"n": 0}
        range_label = f"{local_date(rec['started_at'])} {local_time(rec['started_at'])}"
        if rec.get("ended_at"):
            range_label += f" - {local_time(rec['ended_at'])}"
        return {
            "label": label,
            "rangeLabel": range_label,
            "body": text,
            "stats": {
                "recordings": 1,
                "chunks": cnt["n"],
                "chars": len(text),
                "durationMs": rec.get("duration_ms") or 0,
            },
            "fallbackTitle": rec["title"],
        }

    frm, to = day_range(summary["date_from"], summary["date_to"])
    recs = all_(
        """SELECT * FROM recordings WHERE user_id = ? AND started_at >= ? AND started_at <= ?
           AND status IN ('ready','processing') ORDER BY started_at ASC""",
        (summary["user_id"], frm, to),
    )
    if not recs:
        raise ValueError("所选时间段内没有可整理的录音")

    parts: list[str] = []
    chars = 0
    chunks = 0
    duration = 0
    for rec in recs:
        text = transcript_with_timestamps(rec["id"])
        if not text.strip():
            continue
        span = f"{local_date(rec['started_at'])} {local_time(rec['started_at'])}"
        if rec.get("ended_at"):
            span += f"-{local_time(rec['ended_at'])}"
        parts.append(f"### 录音：{rec['title']}（{span}）\n{text}")
        chars += len(text)
        duration += rec.get("duration_ms") or 0
        cnt = one("SELECT COUNT(*) AS n FROM chunks WHERE recording_id = ?", (rec["id"],)) or {"n": 0}
        chunks += cnt["n"]

    if not parts:
        raise ValueError("所选时间段内没有可用的转写内容")

    if summary["date_from"] == summary["date_to"]:
        range_label = summary["date_from"]
    else:
        range_label = f"{summary['date_from']} 至 {summary['date_to']}"

    return {
        "label": label,
        "rangeLabel": range_label,
        "body": "\n\n".join(parts),
        "stats": {
            "recordings": len(recs),
            "chunks": chunks,
            "chars": chars,
            "durationMs": duration,
        },
        "fallbackTitle": summary["title"],
    }


def _user_prompt(ctx: dict, extra_note: str = "") -> str:
    return f"""【整理类型】{ctx['label']}
【时间范围】{ctx['rangeLabel']}
【输入说明】以下为语音转写原文，方括号内是时间戳。
{extra_note}
--- 转写原文开始 ---
{ctx['body']}
--- 转写原文结束 ---"""


def _chunk_text(text: str, size: int) -> list[str]:
    if len(text) <= size:
        return [text]
    out: list[str] = []
    cur = ""
    for line in text.split("\n"):
        if cur and len(cur) + len(line) > size:
            out.append(cur)
            cur = ""
        cur += ("\n" if cur else "") + line
    if cur:
        out.append(cur)
    return out


def _extract_json(raw: str) -> Any:
    s = raw.strip()
    fence = re.search(r"```(?:json)?\s*([\s\S]*?)```", s)
    if fence:
        s = fence.group(1).strip()
    start = s.find("{")
    end = s.rfind("}")
    if start >= 0 and end > start:
        s = s[start : end + 1]
    return json.loads(s)


def _fallback_mindmap(title: str, markdown: str) -> str:
    lines = [f"# {title or '纪要'}"]
    for line in markdown.split("\n"):
        h2 = re.match(r"^##\s+(.+)", line)
        if h2:
            lines.append(f"## {h2.group(1).strip()}")
            continue
        item = re.match(r"^\s*[-*]\s+(?:\[.\]\s*)?(.+)", line)
        if item:
            lines.append("### " + item.group(1).replace("**", "")[:40])
    return "\n".join(lines)


def handle_summarize_job(payload: dict) -> None:
    summary = one("SELECT * FROM summaries WHERE id = ?", (payload.get("summaryId"),))
    if not summary:
        return
    force = bool(payload.get("force"))
    if summary["status"] == "ready" and not force:
        return

    template = payload.get("template") or summary.get("template") or "meeting"

    execute(
        "UPDATE summaries SET status = 'generating', error = NULL, updated_at = ? WHERE id = ?",
        (now_iso(), summary["id"]),
    )
    if summary.get("recording_id"):
        execute(
            "UPDATE recordings SET summary_status = 'generating' WHERE id = ?",
            (summary["recording_id"],),
        )
        emit_recording(
            summary["recording_id"],
            "summary",
            {"status": "generating", "summaryId": summary["id"]},
        )

    try:
        ctx = _build_context(summary, template)
        provider = resolve_llm(summary["user_id"], summary.get("provider_id"))
        llm = build_llm(provider)

        # 风格决定结构与侧重点；用户自定义要求会替换掉风格描述，规则与 JSON 契约始终保留
        custom = str(read_settings(summary["user_id"]).get("summaryPrompt") or "")
        system_prompt = build_system_prompt(template, custom)

        final_user = _user_prompt(ctx)
        if len(ctx["body"]) > MAX_INPUT_CHARS:
            # 长文本：先分段提炼要点，再汇总
            parts = _chunk_text(ctx["body"], MAP_CHUNK_CHARS)
            chunk_prompt = build_chunk_prompt(template)
            notes: list[str] = []
            for i, part in enumerate(parts):
                note = llm.chat(
                    [
                        {"role": "system", "content": chunk_prompt},
                        {"role": "user", "content": f"第 {i + 1}/{len(parts)} 段：\n{part}"},
                    ],
                    temperature=0.2,
                )
                notes.append(note.strip())
            final_user = _user_prompt(
                {**ctx, "body": "\n\n".join(notes)},
                "（说明：原始文本较长，以上是分段提炼后的要点，请据此整理完整纪要。）\n",
            )

        raw = llm.chat(
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": final_user},
            ],
            json_mode=True,
            temperature=0.3,
        )

        try:
            parsed = _extract_json(raw)
            if not isinstance(parsed, dict):
                raise ValueError("not an object")
        except (json.JSONDecodeError, ValueError):
            parsed = {
                "title": ctx["fallbackTitle"],
                "summary": "",
                "markdown": raw,
                "mindmap": _fallback_mindmap(ctx["fallbackTitle"], raw),
                "highlights": [],
                "todos": [],
            }

        title = str(parsed.get("title") or ctx["fallbackTitle"] or "未命名纪要")[:60]
        markdown = str(parsed.get("markdown") or parsed.get("summary") or "")
        mindmap = str(parsed.get("mindmap") or _fallback_mindmap(title, markdown))
        stats = {
            **ctx["stats"],
            "highlights": parsed.get("highlights") or [],
            "todos": parsed.get("todos") or [],
        }

        execute(
            """UPDATE summaries SET title = ?, markdown = ?, mindmap = ?, model = ?, provider_id = ?,
               status = 'ready', error = NULL, stats = ?, updated_at = ? WHERE id = ?""",
            (
                title,
                markdown,
                mindmap,
                provider.get("model") or "",
                provider["id"],
                json.dumps(stats, ensure_ascii=False),
                now_iso(),
                summary["id"],
            ),
        )

        if summary.get("recording_id"):
            execute(
                "UPDATE recordings SET summary_status = 'ready' WHERE id = ?",
                (summary["recording_id"],),
            )
            emit_recording(
                summary["recording_id"],
                "summary",
                {"status": "ready", "summaryId": summary["id"]},
            )
    except Exception as e:  # noqa: BLE001
        msg = str(e)[:500]
        execute(
            "UPDATE summaries SET status = 'failed', error = ?, updated_at = ? WHERE id = ?",
            (msg, now_iso(), summary["id"]),
        )
        if summary.get("recording_id"):
            execute(
                "UPDATE recordings SET summary_status = 'failed' WHERE id = ?",
                (summary["recording_id"],),
            )
            emit_recording(
                summary["recording_id"],
                "summary",
                {"status": "failed", "error": msg},
            )
        raise


def public_summary(s: dict) -> dict:
    stats: Any = None
    if s.get("stats"):
        try:
            stats = json.loads(s["stats"])
        except (json.JSONDecodeError, TypeError):
            stats = None
    return {
        "id": s["id"],
        "recordingId": s.get("recording_id"),
        "scope": s["scope"],
        "dateFrom": s["date_from"],
        "dateTo": s["date_to"],
        "title": s["title"],
        "template": s.get("template") or "meeting",
        "markdown": s.get("markdown"),
        "mindmap": s.get("mindmap"),
        "status": s["status"],
        "error": s.get("error"),
        "model": s.get("model"),
        "stats": stats,
        "createdAt": s["created_at"],
        "updatedAt": s["updated_at"],
    }
