from __future__ import annotations

import bisect
import json
import re
import time
from calendar import monthrange
from datetime import date, datetime, timedelta, timezone
from typing import Any

from ..bus import emit_recording
from ..config import uid
from ..db import all_, execute, now_iso, one
from ..jobs import enqueue
from .. import logbuf
from .prompts import (
    PERIOD_TEMPLATE,
    SCOPE_DEFAULT_TEMPLATE,
    TEMPLATE_LABEL,
    TODO_TAGS,
    build_chunk_prompt,
    build_revise_system_prompt,
    build_system_prompt,
    default_template_for,
    period_of,
    prompt_field,
)
from .providers import build_llm, resolve_llm
from .settings import read_settings
from .transcribe import transcript_with_timestamps
from .transcript_value import NO_TOPIC_REASON, clear_no_topic, mark_no_topic

SummaryTemplate = str

MAX_INPUT_CHARS = 16_000
MAP_CHUNK_CHARS = 12_000

# 层级式汇总：周/月/任意时段基于日报，年报基于 12 份月报。
# 下级缺失时先排队补齐，父任务进入 waiting，由轮询任务在下级结算后唤醒。
CASCADE_POLL_SECONDS = 15
CASCADE_TIMEOUT_SECONDS = 60 * 60


def _parse(iso: str | None) -> datetime:
    """解析 ISO 时间并统一换算成本地时区。

    注意：带 Z/+00:00 的时间戳解析出来是 UTC，必须 astimezone() 一次，
    否则 local_date/local_time 会退化成「UTC 日期/时间」，
    凌晨 0-8 点的录音就会被算到前一天（日历显示「这一天没有录音」的元凶）。
    """
    s = (iso or "").replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        dt = datetime.now(timezone.utc)
    return dt.astimezone()  # naive 视为本地时间，aware 统一转本地


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


def _iso_after(seconds: float) -> str:
    """与 now_iso() 同格式的未来时间，供 jobs.run_at 做字符串比较。"""
    dt = datetime.now(timezone.utc) + timedelta(seconds=seconds)
    return dt.isoformat(timespec="milliseconds").replace("+00:00", "Z")


# ------------------------------------------------------------------ #
# 层级式汇总：父子区间、下级状态、级联补齐
# ------------------------------------------------------------------ #
def _child_windows(date_from: str, date_to: str, period: str) -> list[tuple[str, str, str]] | None:
    """父纪要要读的下级区间。

    年报按月（12 份月报），周/月/任意时段按天（若干份日报）。
    区间非法或超过一年返回 None，表示不适用层级式、回退到读转写原文。
    """
    try:
        d1 = date.fromisoformat(date_from)
        d2 = date.fromisoformat(date_to)
    except ValueError:
        return None
    if d2 < d1 or (d2 - d1).days > 366:
        return None
    if period == "year":
        return [
            (
                f"{d1.year}-{m:02d}-01",
                f"{d1.year}-{m:02d}-{monthrange(d1.year, m)[1]:02d}",
                "month",
            )
            for m in range(1, 13)
        ]
    out: list[tuple[str, str, str]] = []
    cur = d1
    while cur <= d2:
        out.append((cur.isoformat(), cur.isoformat(), "day"))
        cur += timedelta(days=1)
    return out


def _collect_children(user_id: str, date_from: str, date_to: str, period: str) -> dict | None:
    """批量盘点下级：ready 直接用，pending 需补生成，空窗口跳过。

    只做两次查询（子纪要 + 区间内有录音的日期），避免年报 12 个窗口、
    月报 30 个窗口逐个查库。返回 None 表示不适用层级式。
    """
    windows = _child_windows(date_from, date_to, period)
    if windows is None:
        return None
    start, end = day_range(date_from, date_to)

    rows = all_(
        """SELECT * FROM summaries
           WHERE user_id = ? AND date_from >= ? AND date_to <= ?
             AND scope IN ('day','range','month') AND status <> 'waiting'""",
        (user_id, date_from, date_to),
    )
    by_window: dict[tuple[str, str], dict] = {}
    for r in rows:
        key = (r["date_from"], r["date_to"])
        old = by_window.get(key)
        if old is None or (r["status"] == "ready" and old["status"] != "ready"):
            by_window[key] = r

    rec_days = sorted(
        {
            local_date(r["started_at"])
            for r in all_(
                """SELECT started_at FROM recordings
                   WHERE user_id = ? AND started_at >= ? AND started_at <= ?
                     AND status IN ('ready','processing')""",
                (user_id, start, end),
            )
        }
    )

    def window_has_recordings(frm: str, to: str) -> bool:
        i = bisect.bisect_left(rec_days, frm)
        return i < len(rec_days) and rec_days[i] <= to

    ready: list[tuple[str, str, str, dict]] = []
    pending: list[tuple[str, str, str]] = []
    failed, empty = 0, 0
    for frm, to, kind in windows:
        row = by_window.get((frm, to))
        status = (row or {}).get("status")
        if status == "ready":
            ready.append((frm, to, kind, row))
        elif status in ("queued", "generating"):
            pending.append((frm, to, kind))
        elif status == "failed":
            # 已经试过并失败，不再无限等待；父纪要跳过这个窗口
            failed += 1
        elif window_has_recordings(frm, to):
            pending.append((frm, to, kind))
        else:
            empty += 1
    return {"ready": ready, "pending": pending, "failed": failed, "empty": empty}


def _spawn_child(user_id: str, frm: str, to: str, kind: str) -> None:
    """排队生成一份缺失的下级纪要（幂等）。

    create_summary(force=False) 对已 ready / queued / generating / waiting 的行
    直接返回，不会重复入队；failed 的行会重新排队重试。月报自己也会按天级联，
    于是年报 → 12 月报 → 各天日报形成两级瀑布。
    """
    try:
        create_summary(
            userId=user_id,
            scope="day" if kind == "day" else "range",
            dateFrom=frm,
            dateTo=to,
            force=False,
        )
    except ValueError:
        # 窗口里刚好没有可整理的转写，父纪要跳过即可
        pass


def _kick_parent(summary: dict, payload: dict) -> None:
    """下级结算完（或超时）→ 把父任务从 waiting 放回正常生成队列。"""
    cur = execute(
        "UPDATE summaries SET status = 'queued', error = NULL, updated_at = ? WHERE id = ? AND status = 'waiting'",
        (now_iso(), summary["id"]),
    )
    if not int(getattr(cur, "rowcount", 0) or 0):
        return
    enqueue(
        "summarize",
        {
            "summaryId": summary["id"],
            "template": payload.get("template"),
            "force": bool(payload.get("force")),
        },
        max_attempts=2,
    )
    logbuf.log("info", "summarize", f"下级回忆已备齐，开始整理「{summary['title'] or summary['id']}」")


def _poll_cascade(summary: dict, payload: dict) -> None:
    """父任务的轻量轮询：只查库和排队，不调 LLM。"""
    fresh = one("SELECT status FROM summaries WHERE id = ?", (summary["id"],))
    if not fresh or fresh["status"] != "waiting":
        return  # 已被别的链路推进、或用户删了/重新排了
    deadline = float(payload.get("deadline") or 0)
    need_wait = True
    pending: list[tuple[str, str, str]] = []
    try:
        period = period_of(summary["scope"], summary["date_from"], summary["date_to"])
        children = _collect_children(
            summary["user_id"], summary["date_from"], summary["date_to"], period
        )
        pending = children["pending"] if children else []
        # 幂等补齐：每轮都试一次，服务重启或链路中断也能自动续上
        for frm, to, kind in pending:
            _spawn_child(summary["user_id"], frm, to, kind)
        need_wait = bool(pending)
    except Exception as e:  # noqa: BLE001 - 查库抖动不该让整条等待链断掉
        logbuf.log("warn", "summarize", f"检查下级回忆失败：{str(e)[:200]}")

    if need_wait and time.time() < deadline:
        enqueue(
            "summarize",
            {**payload},
            run_at=_iso_after(CASCADE_POLL_SECONDS),
            max_attempts=2,
        )
        return
    if need_wait:
        logbuf.log(
            "warn",
            "summarize",
            f"「{summary['title'] or summary['id']}」等待下级超时，仍有 {len(pending)} 份未备齐，先用手头材料整理",
        )
    _kick_parent(summary, payload)


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
    period = period_of(scope, d_from, d_to)
    fallback_tpl = default_template_for(scope, d_from, d_to) if scope != "recording" else SCOPE_DEFAULT_TEMPLATE["recording"]
    tpl = template or (existing.get("template") if existing else None) or fallback_tpl
    # 旧数据升级：早期日报/时段汇总默认用了「会议纪要」，或者周/月/年还停在「时段回忆」，
    # 重新生成时按区间切到对应的回忆风格
    if not template and scope in ("day", "range"):
        if tpl == "meeting" or (tpl == "period" and period in ("day", "week", "month", "year")):
            tpl = fallback_tpl
    t = now_iso()

    if existing:
        if not force and existing["status"] in ("queued", "generating", "waiting", "ready"):
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
            new_title = f"{d_from} 的这一天"
        elif period == "week":
            new_title = f"{d_from} 这一周"
        elif period == "month":
            new_title = f"{d_from[:4]} 年 {int(d_from[5:7])} 月"
        elif period == "year":
            new_title = f"{d_from[:4]} 年"
        else:
            new_title = f"{d_from} 至 {d_to} 这段时间"
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

    # 多日回忆走层级式：先确保下级（日报 / 月报）就绪，再用下级文本汇总。
    # 下级已齐时直接入队，父纪要只读十几份文本，不再重扫整段时间的转写原文。
    children = None
    if scope in ("day", "range") and period != "day":
        children = _collect_children(userId, d_from, d_to, period)

    if children and children["pending"]:
        execute(
            "UPDATE summaries SET status = 'waiting', error = NULL, updated_at = ? WHERE id = ?",
            (now_iso(), summary["id"]),
        )
        summary = one("SELECT * FROM summaries WHERE id = ?", (summary["id"],)) or summary
        # 补齐下级由后台轮询任务做（幂等），HTTP 请求这里只负责排队
        enqueue(
            "summarize",
            {
                "summaryId": summary["id"],
                "template": tpl,
                "force": bool(force),
                "checkChildren": True,
                "deadline": time.time() + CASCADE_TIMEOUT_SECONDS,
            },
            run_at=_iso_after(1),
            max_attempts=2,
        )
        logbuf.log(
            "info",
            "summarize",
            f"「{summary['title'] or summary['id']}」等待 {len(children['pending'])} 份下级回忆备齐",
        )
    else:
        enqueue(
            "summarize",
            {"summaryId": summary["id"], "template": tpl, "force": bool(force)},
            max_attempts=2,
        )
    return summary


# ------------------------------------------------------------------ #
# 生成纪要
# ------------------------------------------------------------------ #
def _context_from_children(
    summary: dict, template: str, children: dict, period: str
) -> dict[str, Any]:
    """用已生成的下级回忆拼输入，替代整段时间的转写原文。"""
    unit = "月" if period == "year" else "天"
    parts: list[str] = []
    acc = {"recordings": 0, "chunks": 0, "chars": 0, "durationMs": 0}
    for frm, _to, kind, row in children["ready"]:
        md = str(row.get("markdown") or "").strip()
        if not md:
            continue
        tag = frm[:7] if kind == "month" else frm
        parts.append(f"### {tag} · {row['title']}\n{md}")
        try:
            st = json.loads(row.get("stats") or "{}")
        except (json.JSONDecodeError, TypeError):
            st = {}
        if isinstance(st, dict):
            for k in acc:
                try:
                    acc[k] += int(st.get(k) or 0)
                except (TypeError, ValueError):
                    pass
    if not parts:
        raise ValueError("下级回忆还没有可用内容，请稍后再重新整理")

    if summary["date_from"] == summary["date_to"]:
        range_label = summary["date_from"]
    else:
        range_label = f"{summary['date_from']} 至 {summary['date_to']}"

    input_desc = (
        f"以下是这段时间里各{unit}的回忆（已由 AI 从当{unit}的录音转写整理），"
        "这些回忆就是本次的原文依据：\n"
        "1. 只使用这些回忆里出现过的信息，不新增人名、数字、时间或结论。\n"
        f"2. 不要逐{unit}复述或罗列流水账，把跨{unit}反复出现的人、事、情绪合并成主线，写清变化与转折。\n"
        "3. 下级回忆里的「待办事项」「人物与心情」是重要线索：保留仍未完成的事，"
        "情绪只沿用有原话或行为支撑的，不要重新臆测。\n"
        "4. 各份回忆里已经修过口语错别字，直接沿用即可。"
    )
    return {
        "label": TEMPLATE_LABEL.get(template, TEMPLATE_LABEL["meeting"]),
        "rangeLabel": range_label,
        "body": "\n\n".join(parts),
        "inputDesc": input_desc,
        "materialLabel": "下级回忆",
        "hierarchical": True,
        "stats": {
            **acc,
            "childSummaries": len(parts),
            "childMissing": children["failed"] + len(children["pending"]),
        },
        "fallbackTitle": summary["title"],
    }


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

    # 层级式：周/月/任意时段读日报，年报读 12 份月报。
    # 返回 None（区间非法或超过一年）时才回退到下面的原始转写路径。
    period = period_of(summary["scope"], summary["date_from"], summary["date_to"])
    if summary["scope"] in ("day", "range") and period != "day":
        children = _collect_children(
            summary["user_id"], summary["date_from"], summary["date_to"], period
        )
        if children is not None:
            return _context_from_children(summary, template, children, period)

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
    material = ctx.get("materialLabel") or "转写原文"
    desc = ctx.get("inputDesc") or "以下为语音转写原文，方括号内是时间戳。"
    return f"""【整理类型】{ctx['label']}
【时间范围】{ctx['rangeLabel']}
【输入说明】{desc}
{extra_note}
--- {material}开始 ---
{ctx['body']}
--- {material}结束 ---"""


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


def _clean_moods(raw: Any) -> list[dict]:
    """把模型返回的 moods 收敛成 [{person, mood, evidence}]。"""
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    for item in raw:
        if isinstance(item, dict):
            person = str(item.get("person") or "").strip()
            mood = str(item.get("mood") or "").strip()
            evidence = str(item.get("evidence") or "").strip()
        else:
            person, mood, evidence = "", str(item or "").strip(), ""
        if not (person and mood):
            continue
        out.append({"person": person[:30], "mood": mood[:40], "evidence": evidence[:80]})
    return out[:6]


def _clean_todos(raw: Any) -> list[dict]:
    """把模型返回的 todos 收敛成 [{text, tag, owner, due}]。

    模型偶尔会把 tag 写成「工作任务」「生活琐事」这类变体，这里只在四个标准标签
    里做精确匹配；对不上的一律标成「未分类」，行动页里仍能显示，只是不参与筛选。
    """
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    for item in raw:
        if isinstance(item, dict):
            text = str(item.get("text") or "").strip()
            tag = str(item.get("tag") or "").strip()
            owner = str(item.get("owner") or "").strip()
            due = str(item.get("due") or "").strip()
        else:
            text, tag, owner, due = str(item or "").strip(), "", "", ""
        if not text:
            continue
        out.append(
            {
                "text": text[:200],
                "tag": tag if tag in TODO_TAGS else "未分类",
                "owner": owner[:40],
                "due": due[:40],
            }
        )
    return out[:20]


def _is_no_topic(parsed: dict, title: str) -> bool:
    """AI 是否判定这段原文没有实际话题（空、噪音或只有零碎文字）。"""
    v = parsed.get("contentful")
    if isinstance(v, bool):
        if not v:
            return True
    elif isinstance(v, str) and v.strip().lower() in ("false", "no", "0"):
        return True
    # 兼容没返回 contentful 的模型：按提示词约定，标题写「无有效内容」
    return (title or "").strip().startswith("无有效内容")


def handle_summarize_job(payload: dict) -> None:
    summary = one("SELECT * FROM summaries WHERE id = ?", (payload.get("summaryId"),))
    if not summary:
        return
    if payload.get("checkChildren"):
        _poll_cascade(summary, payload)
        return
    force = bool(payload.get("force"))
    if summary["status"] == "ready" and not force:
        return

    period = period_of(summary["scope"], summary["date_from"], summary["date_to"])
    template = (
        payload.get("template")
        or summary.get("template")
        or PERIOD_TEMPLATE.get(period, "meeting")
    )
    # 老日报曾被标成会议纪要，周/月/年还停在「时段回忆」，补一次纠正
    if payload.get("template") is None and summary["scope"] in ("day", "range"):
        if template == "meeting" or (template == "period" and period in ("day", "week", "month", "year")):
            template = PERIOD_TEMPLATE.get(period, "period")

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

        # 风格决定结构与侧重点；用户自定义要求会替换掉风格描述，规则与 JSON 契约始终保留。
        # 每个周期（单条/日/周/月/年/任意时段）读各自的提示词字段。
        user_settings = read_settings(summary["user_id"])
        field = prompt_field(template)
        custom = str(user_settings.get(field) or "")
        system_prompt = build_system_prompt(template, custom, bool(ctx.get("hierarchical")))
        source = (
            f"层级输入 {ctx['stats'].get('childSummaries', 0)} 份下级回忆"
            if ctx.get("hierarchical")
            else f"{ctx['stats']['recordings']} 段 / {ctx['stats']['chars']} 字转写原文"
        )
        logbuf.log(
            "info",
            "summarize",
            f"整理「{summary['title'] or summary['id']}」· 类型 {TEMPLATE_LABEL.get(template, template)}"
            f" · 自定义提示词 {('用 ' + field) if custom.strip() else '未设置'}"
            f" · {source}",
        )

        final_user = _user_prompt(ctx)
        if len(ctx["body"]) > MAX_INPUT_CHARS:
            # 长文本：先分段提炼要点，再汇总
            parts = _chunk_text(ctx["body"], MAP_CHUNK_CHARS)
            chunk_prompt = build_chunk_prompt(template, bool(ctx.get("hierarchical")))
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
                "（说明：输入材料较长，以上是分段提炼后的要点，请据此整理完整纪要。）\n",
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
            "todos": _clean_todos(parsed.get("todos")),
            "moods": _clean_moods(parsed.get("moods")),
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
        logbuf.log(
            "info",
            "summarize",
            f"整理完成「{title}」· {provider.get('model') or provider.get('name') or provider['id']} · "
            f"正文 {len(markdown)} 字",
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
            # AI 判定只有零碎文字、拼不出实际话题 → 并进低价值录音，列表里默认收起
            no_topic = _is_no_topic(parsed, title)
            changed = (
                mark_no_topic(summary["recording_id"])
                if no_topic
                else clear_no_topic(summary["recording_id"])
            )
            if changed:
                emit_recording(
                    summary["recording_id"],
                    "recording",
                    {
                        "lowValue": no_topic,
                        "lowReason": NO_TOPIC_REASON if no_topic else None,
                    },
                )
                if no_topic:
                    logbuf.log(
                        "info",
                        "summarize",
                        f"AI 判定「{title}」没有实际话题，已收进低价值录音 · {summary['recording_id']}",
                    )
    except Exception as e:  # noqa: BLE001
        msg = str(e)[:500]
        logbuf.log("error", "summarize", f"整理失败「{summary['title'] or summary['id']}」：{msg}")
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


# ------------------------------------------------------------------ #
# 按用户要求修订已有纪要（AI 修改）
# ------------------------------------------------------------------ #
def handle_revise_job(payload: dict) -> None:
    """读当前纪要 + 依据材料，把用户的要求交给 LLM，原地改这份纪要。

    与初次生成共用解析与落库逻辑，但不动 provider / template，只更内容；
    失败时把 error 写回纪要行，前端照常轮询展示。
    """
    summary = one("SELECT * FROM summaries WHERE id = ?", (payload.get("summaryId"),))
    if not summary:
        return
    instruction = str(payload.get("instruction") or "").strip()
    if not instruction:
        return

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
        # 依据材料与初次生成同源：单条录音读转写原文，父级回忆读下级回忆。
        # 材料只用于约束 AI 不编造，不要求重读全文，长输入同样走分段提炼。
        try:
            ctx = _build_context(summary, summary.get("template") or "meeting")
        except ValueError:
            # 原始材料可能已删除（设备端删了录音等），退回只用当前纪要
            ctx = {
                "body": str(summary.get("markdown") or ""),
                "stats": {},
                "materialLabel": "当前纪要",
                "inputDesc": "以下是这份纪要的当前内容，原始录音材料已不可用。",
                "hierarchical": False,
            }

        provider = resolve_llm(summary["user_id"], summary.get("provider_id"))
        llm = build_llm(provider)

        current_md = str(summary.get("markdown") or "")
        current_title = str(summary.get("title") or "")
        current_todos = []
        try:
            current_todos = (json.loads(summary.get("stats") or "{}") or {}).get("todos") or []
        except (json.JSONDecodeError, TypeError):
            current_todos = []

        system_prompt = build_revise_system_prompt()
        body = ctx["body"]
        if len(body) > MAX_INPUT_CHARS:
            notes: list[str] = []
            parts = _chunk_text(body, MAP_CHUNK_CHARS)
            for i, part in enumerate(parts):
                note = llm.chat(
                    [
                        {"role": "system", "content": build_chunk_prompt(summary.get("template"), bool(ctx.get("hierarchical")))},
                        {"role": "user", "content": f"第 {i + 1}/{len(parts)} 段：\n{part}"},
                    ],
                    temperature=0.2,
                )
                notes.append(note.strip())
            body = "\n\n".join(notes)

        user_prompt = f"""【用户修改要求】
{instruction[:2000]}

【当前纪要】
标题：{current_title}
{current_md}

--- 依据材料开始 ---
{body}
--- 依据材料结束 ---"""

        raw = llm.chat(
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            json_mode=True,
            temperature=0.3,
        )

        try:
            parsed = _extract_json(raw)
            if not isinstance(parsed, dict):
                raise ValueError("not an object")
        except (json.JSONDecodeError, ValueError):
            raise ValueError("模型没有返回合法的 JSON，请换一种说法再试一次")

        title = str(parsed.get("title") or current_title or "未命名纪要")[:60]
        markdown = str(parsed.get("markdown") or parsed.get("summary") or "")
        if not markdown.strip():
            raise ValueError("模型返回的正文为空，请换一种说法再试一次")
        mindmap = str(parsed.get("mindmap") or _fallback_mindmap(title, markdown))
        # 修订结果带不回完整统计时，沿用原 stats 的 recordings/chars 等基本盘
        old_stats = json.loads(summary.get("stats") or "{}") if summary.get("stats") else {}
        stats = {
            **old_stats,
            "highlights": parsed.get("highlights") or [],
            "todos": _clean_todos(parsed.get("todos")) or _clean_todos(current_todos),
            "moods": _clean_moods(parsed.get("moods")),
        }

        execute(
            """UPDATE summaries SET title = ?, markdown = ?, mindmap = ?, stats = ?,
               status = 'ready', error = NULL, updated_at = ?
               WHERE id = ?""",
            (title, markdown, mindmap, json.dumps(stats, ensure_ascii=False), now_iso(), summary["id"]),
        )
        logbuf.log(
            "info",
            "summarize",
            f"按用户要求修订完成「{title}」· {provider.get('model') or provider.get('name') or provider['id']}",
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
        logbuf.log("error", "summarize", f"修订纪要失败「{summary['title'] or summary['id']}」：{msg}")
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
