"""「找寻」：让大模型在用户的录音记忆库里按需调用多种检索工具。

四条检索路径：

- `keyword`  字面精确匹配（标题 / 备忘 / 转写原文 / 纪要正文）
- `vector`   TF-IDF 稀疏向量余弦相似度（做中文语义近似匹配，不依赖外部 embedding 服务）
- `date`     按本地日期范围列出录音
- `recording` 读取某条录音的转写片段与纪要，用于核实细节

大模型只负责「决定调哪个工具、参数是什么」和「基于证据作答」，真正的检索在服务端完成。
默认的演示引擎（kind=mock）不调用外部模型，直接走确定性的双路检索 + 摘录式回答。

延迟主要在串行 LLM 调用上，所以：模型选定一条检索路径后，服务端在同一轮里把互补的
字面/语义/日期检索一并跑掉（``_complementary_tools``），拿到证据就不再浪费一轮决策；
推送决策与工具推荐这两个独立调用也并行执行。TF-IDF 索引按数据签名落盘复用。
"""

from __future__ import annotations

import json
import math
import os
import pickle
import re
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from ..config import DATA_DIR
from ..db import all_, one
from ..services.mcp_dispatch import decide_and_push
from ..services.mcp_recommend import recommend_chat_tools
from ..services.mcp_servers import call_chat_tool, chat_tools, push_targets
from ..services.providers import build_llm, resolve_llm
from ..services.summarize import day_range, local_date, local_time
from ..services.transcribe import parse_segments

# 一次「找寻」最多让模型决策几轮（不含最后的作答）。
# 决策轮是串行 LLM 调用，是端到端延迟的主要来源；同一轮里已经把互补的检索
# 一起跑掉（见 _complementary_tools），正常一轮就能拿到证据，所以压到 2 轮兜底。
MAX_ROUNDS = 2
# 外部 MCP 工具最多调几次：它们是"把结果送出去"，不该霸占检索轮次
MAX_MCP_CALLS = 2
MAX_EVIDENCE = 24
SNIPPET_CHARS = 240
INDEX_TTL = 600
INDEX_MAX_CHUNKS = 8000
INDEX_MAX_RECORDINGS = 600

_WEEK_CN = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]

FIND_SYSTEM = """你是「忆录」的找寻助手，负责在用户自己的录音记忆库里检索并回答。

当前本地时间：{now}（{weekday}）。

你只能通过下面这些工具检索，一次决定调用一个：
- search_keyword：字面精确搜索标题、备忘、转写原文和纪要正文。适合人名、地名、产品名、专有名词、原话。
- search_vector：向量（语义）搜索，按意思相近匹配。适合「最近聊过什么」「关于某个主题」这类模糊描述。
- list_recordings：按本地日期范围列出录音。适合「昨天」「上周」「几月几号」这类时间问题。
- get_recording：读取指定录音的转写与纪要片段，用来核实细节。
- final：证据已经足够，进入回答。
{mcp_block}
决策规则：
1. 专有名词/原话优先 search_keyword；主题、感受、模糊回忆优先 search_vector。
2. 另一条检索路径服务端会一起跑，不用为了「再搜一次」而多调一轮。
3. 最多 {max_rounds} 轮，能用一次检索解决就不要再搜。
4. 只输出一个 JSON 对象，不要 Markdown 代码块，不要任何解释文字：
   {{"tool":"search_vector","args":{{"q":"...","limit":6}},"reason":"..."}}
   或 {{"tool":"final","reason":"..."}}
参数说明：search_keyword / search_vector 用 q、limit；list_recordings 用 dateFrom、dateTo（YYYY-MM-DD）、limit；get_recording 用 recordingId、focus（可选）。
相对时间请先按上面的「当前本地时间」换算成具体日期。"""


# ------------------------------------------------------------------ #
# 通用小工具
# ------------------------------------------------------------------ #
def _clean(text: Any, limit: int = SNIPPET_CHARS) -> str:
    s = " ".join(str(text or "").split())
    if len(s) <= limit:
        return s
    return s[:limit].rstrip("，。,.、；; ") + "…"


def _escape_like(q: str) -> str:
    return q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _loads_loose(raw: str) -> dict | None:
    """模型偶尔会裹一层 ```json 或前后加话，这里尽量把第一个 JSON 对象抠出来。"""
    if not raw:
        return None
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else None
    except (json.JSONDecodeError, TypeError):
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        try:
            data = json.loads(text[start : end + 1])
            return data if isinstance(data, dict) else None
        except (json.JSONDecodeError, TypeError):
            return None
    return None


def _now_date() -> datetime:
    return datetime.now()


def _day_text(d: datetime) -> str:
    return f"{d.year}-{d.month:02d}-{d.day:02d}"


def _relative_range(question: str) -> tuple[str, str] | None:
    """从问题里解析「昨天 / 上周 / 3月5日 / 2026-10-30」这类时间说法。"""
    today = _now_date().date()
    q = question or ""

    if "前天" in q:
        d = today - timedelta(days=2)
        return _day_text(d), _day_text(d)
    if "昨天" in q:
        d = today - timedelta(days=1)
        return _day_text(d), _day_text(d)
    if "今天" in q or "今日" in q:
        return _day_text(today), _day_text(today)

    if "上周" in q or "上星期" in q:
        last_monday = today - timedelta(days=today.weekday() + 7)
        return _day_text(last_monday), _day_text(last_monday + timedelta(days=6))
    if "本周" in q or "这周" in q or "这个星期" in q or "这星期" in q:
        monday = today - timedelta(days=today.weekday())
        return _day_text(monday), _day_text(today)

    if "上个月" in q or "上月" in q:
        first_this = today.replace(day=1)
        last_prev_end = first_this - timedelta(days=1)
        return _day_text(last_prev_end.replace(day=1)), _day_text(last_prev_end)
    if "本月" in q or "这个月" in q:
        return _day_text(today.replace(day=1)), _day_text(today)

    if "最近" in q or "近来" in q:
        return _day_text(today - timedelta(days=7)), _day_text(today)

    m = re.search(r"(20\d{2})[-/年](\d{1,2})[-/月](\d{1,2})", q)
    if m:
        try:
            d = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3))).date()
            return _day_text(d), _day_text(d)
        except ValueError:
            pass

    m = re.search(r"(?<!\d)(\d{1,2})月(\d{1,2})[日号]", q)
    if m:
        try:
            d = datetime(today.year, int(m.group(1)), int(m.group(2))).date()
            return _day_text(d), _day_text(d)
        except ValueError:
            pass

    return None


def _parse_day(value: Any, fallback: str = "") -> str:
    s = str(value or "").strip()
    if re.match(r"^\d{4}-\d{2}-\d{2}$", s):
        return s
    m = re.search(r"(20\d{2})[-/年](\d{1,2})[-/月](\d{1,2})", s)
    if m:
        try:
            d = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            return _day_text(d)
        except ValueError:
            return fallback
    return fallback


# ------------------------------------------------------------------ #
# 元数据
# ------------------------------------------------------------------ #
def _recordings_meta(user_id: str) -> dict[str, dict]:
    rows = all_(
        """SELECT id, title, started_at, ended_at, duration_ms, memo, source, summary_status
           FROM recordings WHERE user_id = ?""",
        (user_id,),
    )
    return {str(r["id"]): r for r in rows}


def _hit(
    rec: dict | None,
    *,
    text: str,
    kind: str,
    score: float,
    start_ms: int | None = None,
) -> dict:
    started_at = (rec or {}).get("started_at")
    return {
        "recordingId": (rec or {}).get("id"),
        "title": (rec or {}).get("title") or "",
        "date": local_date(started_at) if started_at else "",
        "time": local_time(started_at) if started_at else "",
        "startedAt": started_at,
        "durationMs": (rec or {}).get("duration_ms") or 0,
        "kind": kind,
        "text": _clean(text),
        "startMs": start_ms,
        "score": float(score),
    }


# ------------------------------------------------------------------ #
# 工具 1：字面精确搜索
# ------------------------------------------------------------------ #
def _snippet_of(text: str, q: str, limit: int = SNIPPET_CHARS) -> str:
    clean = " ".join(str(text or "").split())
    if not clean:
        return ""
    idx = clean.lower().find(q.lower())
    if idx < 0:
        return _clean(clean, limit)
    start = max(0, idx - limit // 3)
    piece = clean[start : start + limit]
    if start > 0:
        piece = "…" + piece
    if start + limit < len(clean):
        piece = piece + "…"
    return piece


def search_keyword(user_id: str, q: str, limit: int = 8) -> list[dict]:
    q = (q or "").strip()
    if not q:
        return []
    pat = "%" + _escape_like(q) + "%"
    hits: list[dict] = []

    title_rows = all_(
        """SELECT id, title, started_at, ended_at, duration_ms, memo, source, summary_status
           FROM recordings
           WHERE user_id = ? AND (
             LOWER(title) LIKE LOWER(?) OR LOWER(COALESCE(memo, '')) LIKE LOWER(?)
           )
           ORDER BY started_at DESC LIMIT ?""",
        (user_id, pat, pat, limit),
    )
    for r in title_rows:
        field = "title" if q.lower() in str(r["title"]).lower() else "memo"
        hits.append(_hit(r, text=_snippet_of(r.get(field) or r.get("title") or "", q), kind=field, score=1.6))

    chunk_rows = all_(
        """SELECT c.recording_id, c.transcript, c.segments,
                  r.title, r.started_at, r.ended_at, r.duration_ms, r.memo, r.source, r.summary_status
           FROM chunks c JOIN recordings r ON r.id = c.recording_id
           WHERE r.user_id = ? AND LOWER(COALESCE(c.transcript, '')) LIKE LOWER(?)
           ORDER BY r.started_at DESC, c.seq ASC LIMIT ?""",
        (user_id, pat, limit * 4),
    )
    seen_rec: set[str] = set()
    for row in chunk_rows:
        if str(row["recording_id"]) in seen_rec:
            continue
        segs = parse_segments(row)
        matched = None
        for s in segs:
            text = str(s.get("text") or "")
            if q.lower() in text.lower():
                matched = s
                break
        if matched is None:
            snippet = _snippet_of(row.get("transcript") or "", q)
            start_ms = None
        else:
            snippet = _snippet_of(matched.get("text") or "", q)
            start_ms = matched.get("startMs")
        if not snippet:
            continue
        seen_rec.add(str(row["recording_id"]))
        rec = {
            "id": row["recording_id"],
            "title": row.get("title"),
            "started_at": row.get("started_at"),
            "duration_ms": row.get("duration_ms"),
        }
        hits.append(_hit(rec, text=snippet, kind="transcript", score=2.2, start_ms=start_ms))

    summary_rows = all_(
        """SELECT id, recording_id, date_from, title, markdown
           FROM summaries
           WHERE user_id = ? AND status = 'ready'
             AND (LOWER(title) LIKE LOWER(?) OR LOWER(COALESCE(markdown, '')) LIKE LOWER(?))
           ORDER BY date_from DESC LIMIT ?""",
        (user_id, pat, pat, limit),
    )
    if summary_rows:
        meta = _recordings_meta(user_id)
        for s in summary_rows:
            rec = meta.get(str(s.get("recording_id") or "")) if s.get("recording_id") else None
            text = _snippet_of(s.get("markdown") or s.get("title") or "", q)
            if not text:
                continue
            if rec:
                hits.append(_hit(rec, text=text, kind="summary", score=1.2))
            else:
                hits.append(
                    {
                        "recordingId": None,
                        "title": f"{s.get('date_from')} 回忆 · {s.get('title')}",
                        "date": s.get("date_from"),
                        "time": "",
                        "startedAt": None,
                        "durationMs": 0,
                        "kind": "summary",
                        "text": text,
                        "startMs": None,
                        "score": 0.9,
                    }
                )

    return _dedupe(hits, limit)


def _dedupe(hits: list[dict], limit: int) -> list[dict]:
    """同一段文字只留一条，按分数排序。"""
    out: list[dict] = []
    seen: set[tuple] = set()
    for h in sorted(hits, key=lambda x: x.get("score") or 0, reverse=True):
        key = (h.get("recordingId"), str(h.get("text") or "")[:48])
        if key in seen:
            continue
        seen.add(key)
        out.append(h)
        if len(out) >= limit:
            break
    return out


# ------------------------------------------------------------------ #
# 工具 2：向量（语义）检索
# ------------------------------------------------------------------ #
_index_cache: dict[str, tuple[float, str, dict]] = {}


_CJK = re.compile(r"[\u4e00-\u9fff]")
_WORD = re.compile(r"[a-zA-Z0-9]+")


def _tokens(text: str) -> list[str]:
    low = str(text or "").lower()
    out = _WORD.findall(low)
    chars = _CJK.findall(low)
    out.extend(chars)
    out.extend(chars[i] + chars[i + 1] for i in range(len(chars) - 1))
    return out


def _signature(user_id: str) -> str:
    c = one(
        """SELECT COUNT(*) AS n, MAX(c.updated_at) AS u
           FROM chunks c JOIN recordings r ON r.id = c.recording_id
           WHERE r.user_id = ?""",
        (user_id,),
    ) or {}
    s = one(
        """SELECT COUNT(*) AS n, MAX(updated_at) AS u FROM summaries WHERE user_id = ?""",
        (user_id,),
    ) or {}
    return f"{c.get('n')}:{c.get('u')}:{s.get('n')}:{s.get('u')}"


def _build_index(user_id: str) -> dict:
    recs = all_(
        """SELECT id, title, started_at, ended_at, duration_ms
           FROM recordings WHERE user_id = ?
           ORDER BY started_at DESC LIMIT ?""",
        (user_id, INDEX_MAX_RECORDINGS),
    )
    meta = {str(r["id"]): r for r in recs}

    docs: list[dict] = []

    chunks = all_(
        """SELECT c.recording_id, c.segments, c.transcript
           FROM chunks c JOIN recordings r ON r.id = c.recording_id
           WHERE r.user_id = ? AND COALESCE(c.transcript, '') <> ''
           ORDER BY r.started_at DESC, c.seq ASC LIMIT ?""",
        (user_id, INDEX_MAX_CHUNKS),
    )
    for row in chunks:
        rec = meta.get(str(row["recording_id"]))
        if not rec:
            continue
        segs = parse_segments(row)
        if segs:
            for s in segs:
                text = _clean(s.get("text"), 400)
                if len(text) >= 4:
                    docs.append(
                        {
                            "text": text,
                            "recordingId": str(row["recording_id"]),
                            "startMs": s.get("startMs"),
                            "kind": "transcript",
                            "rec": rec,
                        }
                    )
        else:
            for piece in str(row.get("transcript") or "").split("\n"):
                text = _clean(piece, 400)
                if len(text) >= 8:
                    docs.append(
                        {
                            "text": text,
                            "recordingId": str(row["recording_id"]),
                            "startMs": None,
                            "kind": "transcript",
                            "rec": rec,
                        }
                    )

    sums = all_(
        """SELECT recording_id, date_from, title, markdown
           FROM summaries
           WHERE user_id = ? AND status = 'ready' AND COALESCE(markdown, '') <> ''
           ORDER BY date_from DESC LIMIT 300""",
        (user_id,),
    )
    for s in sums:
        rec = meta.get(str(s.get("recording_id") or "")) if s.get("recording_id") else None
        for piece in re.split(r"\n{2,}|^#{1,6}\s+", str(s.get("markdown") or ""), flags=re.M):
            text = _clean(piece, 400)
            if len(text) < 12:
                continue
            docs.append(
                {
                    "text": text,
                    "recordingId": str(rec["id"]) if rec else None,
                    "startMs": None,
                    "kind": "summary",
                    "rec": rec,
                    "date": (s.get("date_from") if not rec else None),
                    "title": s.get("title") or "",
                }
            )

    df: Counter = Counter()
    counts: list[Counter] = []
    for d in docs:
        c = Counter(_tokens(d["text"]))
        counts.append(c)
        for term in set(c):
            df[term] += 1

    n = max(1, len(docs))
    idf = {t: math.log((n + 1) / (v + 1)) + 1.0 for t, v in df.items()}

    vecs: list[dict[str, float]] = []
    for c in counts:
        v = {t: (1.0 + math.log(tf)) * idf.get(t, 0.0) for t, tf in c.items()}
        norm = math.sqrt(sum(x * x for x in v.values())) or 1.0
        vecs.append({t: x / norm for t, x in v.items() if x > 0})

    return {"docs": docs, "vecs": vecs, "idf": idf}


_INDEX_DIR = DATA_DIR / "finder_index"
# 分词 / 加权算法一变就递增，让旧的落盘索引自动失效，避免读到不兼容的向量。
INDEX_VERSION = 2


def _index_path(user_id: str) -> Path:
    return _INDEX_DIR / f"{user_id}.pkl"


def _load_index_disk(user_id: str, sig: str) -> dict | None:
    """按数据签名从磁盘复用索引：进程重启 / 内存缓存过期后不必再全量重建。

    索引文件只写在本机 ``data/finder_index`` 下、内容完全由本进程生成，
    所以用 pickle 是安全的（不读外部来源的字节）。
    """
    try:
        with _index_path(user_id).open("rb") as f:
            payload = pickle.load(f)
        if not isinstance(payload, dict):
            return None
        if payload.get("v") != INDEX_VERSION or payload.get("sig") != sig:
            return None
        built = payload.get("built")
        if isinstance(built, dict) and {"docs", "vecs", "idf"} <= set(built):
            return built
    except FileNotFoundError:
        return None
    except Exception as exc:  # noqa: BLE001 - 落盘格式坏了就当没缓存，重建即可
        print(f"[finder] 读取检索索引失败，改为重建：{exc}", flush=True)
    return None


def _save_index_disk(user_id: str, sig: str, built: dict) -> None:
    """尽力而为地落盘；写失败只影响下次冷启动速度，不影响本次检索。"""
    try:
        _INDEX_DIR.mkdir(parents=True, exist_ok=True)
        path = _index_path(user_id)
        # 带上 pid + 纳秒时间戳，避免并发请求同时写同一个临时文件
        tmp = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
        with tmp.open("wb") as f:
            pickle.dump(
                {"v": INDEX_VERSION, "sig": sig, "built": built},
                f,
                protocol=pickle.HIGHEST_PROTOCOL,
            )
        os.replace(tmp, path)
    except Exception as exc:  # noqa: BLE001 - 索引只是加速手段
        print(f"[finder] 检索索引落盘失败：{exc}", flush=True)


def _index(user_id: str) -> dict:
    sig = _signature(user_id)
    hit = _index_cache.get(user_id)
    now = time.time()
    if hit and hit[0] > now and hit[1] == sig:
        return hit[2]
    built = _load_index_disk(user_id, sig)
    if built is None:
        built = _build_index(user_id)
        _save_index_disk(user_id, sig, built)
    _index_cache[user_id] = (now + INDEX_TTL, sig, built)
    return built


def search_vector(user_id: str, q: str, limit: int = 8) -> list[dict]:
    q = (q or "").strip()
    if not q:
        return []
    idx = _index(user_id)
    docs, vecs = idx["docs"], idx["vecs"]
    if not docs:
        return []

    qt = Counter(_tokens(q))
    qv = {t: (1.0 + math.log(tf)) * idx["idf"].get(t, 0.0) for t, tf in qt.items()}
    norm = math.sqrt(sum(x * x for x in qv.values())) or 1.0
    qv = {t: x / norm for t, x in qv.items() if x > 0}
    if not qv:
        return []

    scored: list[tuple[float, int]] = []
    for i, dv in enumerate(vecs):
        # 稀疏向量只遍历较短的一侧
        if len(qv) <= len(dv):
            score = sum(w * dv.get(t, 0.0) for t, w in qv.items())
        else:
            score = sum(w * qv.get(t, 0.0) for t, w in dv.items())
        if score > 0.06:
            scored.append((score, i))

    scored.sort(reverse=True)
    hits: list[dict] = []
    used_rec: set[str] = set()
    for score, i in scored:
        d = docs[i]
        doc_key = d.get("recordingId") or f"day:{d.get('date')}"
        if doc_key in used_rec:
            continue
        used_rec.add(doc_key)
        rec = d.get("rec") or {
            "title": f"{d.get('date')} 回忆 · {d.get('title')}",
            "started_at": None,
            "duration_ms": 0,
        }
        h = _hit(rec, text=d["text"], kind=d["kind"], score=score, start_ms=d.get("startMs"))
        if not d.get("recordingId"):
            h["recordingId"] = None
            h["title"] = rec.get("title") or ""
            h["date"] = h["date"] or (d.get("date") or "")
            h["time"] = ""
        hits.append(h)
        if len(hits) >= limit:
            break
    return hits


# ------------------------------------------------------------------ #
# 工具 3：按日期列录音
# ------------------------------------------------------------------ #
def _batch_excerpts(recording_ids: list[str], per: int = 160) -> dict[str, str]:
    if not recording_ids:
        return {}
    marks = ", ".join("?" for _ in recording_ids)
    rows = all_(
        f"""SELECT recording_id, transcript FROM chunks
            WHERE recording_id IN ({marks}) AND COALESCE(transcript, '') <> ''
            ORDER BY recording_id, seq ASC""",
        tuple(recording_ids),
    )
    out: dict[str, str] = {}
    for r in rows:
        rid = str(r["recording_id"])
        if rid in out:
            continue
        out[rid] = _clean(r.get("transcript") or "", per)
    return out


def list_by_date(user_id: str, date_from: str, date_to: str, limit: int = 10) -> list[dict]:
    frm, to = day_range(date_from, date_to)
    rows = all_(
        """SELECT id, title, started_at, ended_at, duration_ms, memo, source, summary_status
           FROM recordings
           WHERE user_id = ? AND started_at >= ? AND started_at <= ?
           ORDER BY started_at ASC LIMIT ?""",
        (user_id, frm, to, limit),
    )
    if not rows:
        return []
    excerpts = _batch_excerpts([str(r["id"]) for r in rows])
    hits = []
    for r in rows:
        text = r.get("memo") or excerpts.get(str(r["id"])) or r.get("title") or ""
        hits.append(_hit(r, text=text, kind="date", score=0.8))
    return hits


# ------------------------------------------------------------------ #
# 工具 4：读单条录音
# ------------------------------------------------------------------ #
def get_recording(user_id: str, recording_id: str, focus: str = "") -> list[dict]:
    rec = one(
        "SELECT * FROM recordings WHERE id = ? AND user_id = ?",
        (recording_id, user_id),
    )
    if not rec:
        return []

    chunks = all_(
        "SELECT * FROM chunks WHERE recording_id = ? ORDER BY seq ASC",
        (recording_id,),
    )
    segments = []
    for c in chunks:
        segments.extend(parse_segments(c))
    if not segments:
        for c in chunks:
            for piece in str(c.get("transcript") or "").split("\n"):
                if piece.strip():
                    segments.append({"text": piece, "startMs": None})

    focus_terms = set(t for t in _tokens(focus) if len(t) >= 1) if focus else set()
    ranked = []
    for s in segments:
        text = str(s.get("text") or "")
        if not text.strip():
            continue
        overlap = len(focus_terms & set(_tokens(text))) if focus_terms else 0
        ranked.append((overlap, text, s.get("startMs")))
    ranked.sort(key=lambda x: x[0], reverse=True)

    hits = [
        _hit(rec, text=text, kind="transcript", score=1.5 + 0.1 * idx, start_ms=start)
        for idx, (_, text, start) in enumerate(ranked[:3])
    ]

    summary = one(
        """SELECT title, markdown FROM summaries WHERE recording_id = ?
           ORDER BY created_at DESC LIMIT 1""",
        (recording_id,),
    )
    if summary and summary.get("markdown"):
        hits.append(
            _hit(
                rec,
                text=_snippet_of(summary["markdown"], focus, 320) if focus else summary["markdown"],
                kind="summary",
                score=1.0,
            )
        )
    return hits


# ------------------------------------------------------------------ #
# 工具调度
# ------------------------------------------------------------------ #
def _run_tool(user_id: str, tool: str, args: dict) -> tuple[list[dict], str]:
    args = args if isinstance(args, dict) else {}
    if tool == "search_keyword":
        q = str(args.get("q") or args.get("query") or args.get("keyword") or "").strip()
        return search_keyword(user_id, q, _int(args.get("limit"), 8)), f"精确搜索「{q}」"
    if tool == "search_vector":
        q = str(args.get("q") or args.get("query") or args.get("keyword") or "").strip()
        return search_vector(user_id, q, _int(args.get("limit"), 8)), f"语义匹配「{q}」"
    if tool == "list_recordings":
        frm = _parse_day(args.get("dateFrom") or args.get("from") or args.get("date"))
        to = _parse_day(args.get("dateTo") or args.get("to"), frm)
        if not frm:
            frm = to or _day_text(_now_date())
        if not to:
            to = frm
        hits = list_by_date(user_id, frm, to, _int(args.get("limit"), 10))
        return hits, f"按日期列出 {frm} ~ {to}"
    if tool == "get_recording":
        rid = str(args.get("recordingId") or args.get("id") or "").strip()
        hits = get_recording(user_id, rid, str(args.get("focus") or ""))
        return hits, f"读取录音 {rid[:8]}"
    return [], ""


def _int(value: Any, default: int) -> int:
    try:
        return max(1, min(20, int(value)))
    except (TypeError, ValueError):
        return default


def _complementary_tools(tool: str, args: dict, question: str) -> list[tuple[str, dict]]:
    """同一个决策轮里顺手把互补的检索也跑掉。

    模型一次只决定调一个工具，但字面搜索和语义搜索本来就该一起做；如果让它
    下一轮再决定另一条路，就多一次串行 LLM 调用。这里由服务端补齐：模型选了
    其中一条，就把另一条（以及问题里明显提到的时间范围）一并执行。
    """
    if tool not in ("search_keyword", "search_vector", "list_recordings"):
        return []

    q = str(args.get("q") or args.get("query") or args.get("keyword") or question).strip()
    limit = _int(args.get("limit"), 8)
    out: list[tuple[str, dict]] = []
    if tool != "search_keyword":
        out.append(("search_keyword", {"q": q, "limit": limit}))
    if tool != "search_vector":
        out.append(("search_vector", {"q": q, "limit": limit}))
    rng = _relative_range(question)
    if rng and tool != "list_recordings":
        out.append(("list_recordings", {"dateFrom": rng[0], "dateTo": rng[1], "limit": 10}))
    return out


def _tool_signature(tool: str, args: dict) -> str:
    """把同一意图的不同参数写法归一到同一个签名，避免模型换个字段名重复检索。"""
    args = args if isinstance(args, dict) else {}
    if tool in ("search_keyword", "search_vector"):
        q = str(args.get("q") or args.get("query") or args.get("keyword") or "").strip()
        return f"{tool}:{q}"
    if tool == "list_recordings":
        frm = _parse_day(args.get("dateFrom") or args.get("from") or args.get("date"))
        to = _parse_day(args.get("dateTo") or args.get("to"), frm) or frm
        return f"{tool}:{frm}:{to}"
    if tool == "get_recording":
        return f"{tool}:{str(args.get('recordingId') or args.get('id') or '').strip()}"
    return f"{tool}:{json.dumps(args, ensure_ascii=False, sort_keys=True)}"


def _merge_evidence(
    evidence: list[dict],
    seen: dict[str, dict],
    hits: list[dict],
    tool: str,
    label: str,
) -> list[dict]:
    added: list[dict] = []
    for h in hits:
        text = _clean(h.get("text"))
        if not text:
            continue
        key = f"{h.get('recordingId')}|{text[:48]}"
        item = seen.get(key)
        if item:
            item["score"] = max(item["score"], h.get("score") or 0)
            if tool not in item["tools"]:
                item["tools"].append(tool)
            added.append(item)
            continue
        item = {
            "index": len(evidence) + 1,
            "recordingId": h.get("recordingId"),
            "title": h.get("title") or "",
            "date": h.get("date") or "",
            "time": h.get("time") or "",
            "startedAt": h.get("startedAt"),
            "durationMs": h.get("durationMs") or 0,
            "kind": h.get("kind") or "transcript",
            "text": text,
            "startMs": h.get("startMs"),
            "score": float(h.get("score") or 0),
            "tools": [tool],
            "label": label,
        }
        evidence.append(item)
        seen[key] = item
        added.append(item)
        if len(evidence) >= MAX_EVIDENCE:
            break
    return added


def _observation(added: list[dict], note: str) -> dict:
    return {
        "tool_result": note,
        "hits": [
            {
                "index": e["index"],
                "date": e["date"],
                "time": e["time"],
                "title": e["title"],
                "source": e["kind"],
                "text": _clean(e["text"], 160),
            }
            for e in added
        ],
    }


# ------------------------------------------------------------------ #
# 大模型决策循环
# ------------------------------------------------------------------ #
def _mcp_prompt_block(mcp_tools: list[dict]) -> str:
    """把用户配置的 MCP 工具写进提示词。工具太多时只列前 12 个，避免提示词失控。"""
    if not mcp_tools:
        return "（本次对话没有可用的外部 MCP 工具）"
    lines = [
        "可用的外部 MCP 工具（用户自己接入的服务，用于把结果保存/发送出去；不要用它们检索录音）："
    ]
    for t in mcp_tools[:12]:
        desc = _clean(t.get("description"), 140).rstrip("。.；; ")
        params = "、".join(t.get("params") or []) or "无"
        lines.append(f"- {t['key']}：{desc or t.get('title') or t['name']}。参数：{params}")
    if len(mcp_tools) > 12:
        lines.append(f"（另有 {len(mcp_tools) - 12} 个工具未列出）")
    lines.append("需要调用时，tool 直接写上面的完整名字，args 按参数说明给出 JSON 对象。")
    return "\n".join(lines)


def _agent_loop(
    user_id: str,
    question: str,
    history: list[dict],
    provider: dict,
    evidence: list[dict],
    seen: dict[str, dict],
    trace: list[dict],
    mcp_tools: list[dict] | None = None,
) -> None:
    llm = build_llm(provider)
    now = _now_date()
    system = FIND_SYSTEM.format(
        now=now.strftime("%Y-%m-%d %H:%M"),
        weekday=_WEEK_CN[now.weekday()],
        max_rounds=MAX_ROUNDS,
        mcp_block=_mcp_prompt_block(mcp_tools or []),
    )
    messages: list[dict[str, str]] = [{"role": "system", "content": system}]
    for m in (history or [])[-6:]:
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        content = str(m.get("content") or "").strip()
        if role in ("user", "assistant") and content:
            messages.append({"role": role, "content": content[:800]})
    messages.append({"role": "user", "content": question})

    called: set[str] = set()
    mcp_map = {t["key"]: t for t in (mcp_tools or [])}
    mcp_calls = 0
    for round_i in range(MAX_ROUNDS):
        raw = llm.chat(messages, json_mode=True, temperature=0.2, max_tokens=600)
        decision = _loads_loose(raw)
        if not decision:
            # 模型偶发返回非 JSON（或空正文）时，别直接放弃：按问题特征走确定性检索
            rng = _relative_range(question)
            if rng:
                tool, args = "list_recordings", {"dateFrom": rng[0], "dateTo": rng[1], "limit": 20}
            else:
                tool, args = "search_keyword", {"q": question, "limit": 8}
            reason = "模型输出无法解析，改用确定性检索"
        else:
            tool = str(decision.get("tool") or "").strip()
            reason = _clean(decision.get("reason"), 120)
            args = decision.get("args") if isinstance(decision.get("args"), dict) else {}

        if decision and tool == "final":
            trace.append({"round": round_i + 1, "tool": "final", "reason": reason, "hits": 0})
            return

        # 外部 MCP 工具：调用结果直接回给模型，不进"证据"列表（它们不是录音）
        if tool in mcp_map:
            target = mcp_map[tool]
            if mcp_calls >= MAX_MCP_CALLS:
                break
            args = args if isinstance(args, dict) else {}
            signature = _tool_signature(tool, args)
            if signature in called:
                break
            called.add(signature)
            mcp_calls += 1
            res = call_chat_tool(user_id, target, args)
            ok = bool(res.get("ok")) and not res.get("isError")
            text = res.get("text") or ""
            if not text and res.get("structured") is not None:
                text = json.dumps(res["structured"], ensure_ascii=False)
            trace.append(
                {
                    "round": round_i + 1,
                    "tool": tool,
                    "args": args,
                    "reason": reason,
                    "label": f"{target['serverName']} · {target['name']}",
                    "hits": 0,
                    "kind": "mcp",
                    "ok": ok,
                }
            )
            messages.append({"role": "assistant", "content": raw[:1200]})
            messages.append(
                {
                    "role": "user",
                    "content": "【外部工具结果】"
                    + json.dumps(
                        {
                            "tool": tool,
                            "ok": ok,
                            "result": _clean(text or res.get("error") or "（没有返回内容）", 1200),
                        },
                        ensure_ascii=False,
                    ),
                }
            )
            continue

        if tool not in ("search_keyword", "search_vector", "list_recordings", "get_recording"):
            tool = "search_vector"
            args = {"q": question}
        signature = _tool_signature(tool, args)
        if signature in called:
            break
        called.add(signature)

        hits, label = _run_tool(user_id, tool, args)
        added = _merge_evidence(evidence, seen, hits, tool, label)
        trace.append(
            {
                "round": round_i + 1,
                "tool": tool,
                "args": args,
                "reason": reason,
                "label": label,
                "hits": len(added),
            }
        )

        # 同轮补齐：模型只选了一条路，另一条由服务端顺手跑掉。这样通常一轮就能
        # 拿到足够证据，不必再为「换条路再搜一次」多付一次串行 LLM 调用。
        for comp_tool, comp_args in _complementary_tools(tool, args, question):
            comp_sig = _tool_signature(comp_tool, comp_args)
            if comp_sig in called:
                continue
            called.add(comp_sig)
            comp_hits, comp_label = _run_tool(user_id, comp_tool, comp_args)
            comp_added = _merge_evidence(evidence, seen, comp_hits, comp_tool, comp_label)
            trace.append(
                {
                    "round": round_i + 1,
                    "tool": comp_tool,
                    "args": comp_args,
                    "reason": "同轮补齐",
                    "label": comp_label,
                    "hits": len(comp_added),
                }
            )
            added.extend(comp_added)

        messages.append({"role": "assistant", "content": raw[:1200]})
        messages.append(
            {
                "role": "user",
                "content": "【工具结果】" + json.dumps(_observation(added, label), ensure_ascii=False),
            }
        )
        if added:
            # 已经有证据了。再让模型决策一轮通常只会得到 final，白跑一次 LLM；
            # 直接进入作答，把常见路径的串行调用从 3~4 次降到 2 次。
            break
        messages.append(
            {
                "role": "user",
                "content": "【提示】这次没有检索到结果。可以换一个工具或换一种说法；如果确实没有，直接返回 final。",
            }
        )


def _evidence_block(evidence: list[dict]) -> str:
    lines = []
    for e in evidence[:16]:
        src = {"transcript": "转写", "summary": "纪要", "title": "标题", "memo": "备忘", "date": "列表"}.get(
            e["kind"], e["kind"]
        )
        when = f"{e['date']} {e['time']}".strip()
        title = e["title"] or "未命名录音"
        lines.append(f"[{e['index']}] {when}《{title}》·{src}：{_clean(e['text'], 200)}")
    return "\n".join(lines) if lines else "（没有检索到任何证据）"


def _synthesize(
    llm,
    question: str,
    evidence: list[dict],
    history: list[dict],
) -> tuple[str, list[int], list[int]] | None:
    system = (
        "你是「忆录」的找寻助手。请只依据下面给出的证据回答，不要使用证据之外的信息，"
        "证据不足就明确说没有找到。用户问的是问题且有答案时，直接回答；否则概括找到的录音。"
        "用简体中文口语化表达，不超过 120 字，不要列清单、不要编造。"
        "只输出一个 JSON 对象：{\"answer\":\"...\",\"citations\":[证据编号...],\"matches\":[最准确的录音证据编号...]}"
        "其中 matches 最多 3 个，按相关度排序。"
    )
    parts = [f"【证据】\n{_evidence_block(evidence)}", f"【用户问题】\n{question}"]
    for m in (history or [])[-2:]:
        if isinstance(m, dict) and m.get("role") == "user":
            parts.insert(-1, f"【此前的提问】{str(m.get('content') or '')[:200]}")

    raw = llm.chat(
        [{"role": "system", "content": system}, {"role": "user", "content": "\n\n".join(parts)}],
        json_mode=True,
        temperature=0.2,
        max_tokens=800,
    )
    data = _loads_loose(raw)
    if not data or not isinstance(data.get("answer"), str):
        return None
    text = data["answer"].strip()
    if not text:
        return None

    def idx_list(key: str) -> list[int]:
        out = []
        for v in data.get(key) or []:
            try:
                i = int(v)
            except (TypeError, ValueError):
                continue
            if 1 <= i <= len(evidence):
                out.append(i)
        return out

    return text, idx_list("citations"), idx_list("matches")


# ------------------------------------------------------------------ #
# 兜底：演示引擎 / 模型不可用
# ------------------------------------------------------------------ #
def _demo_pipeline(
    user_id: str,
    question: str,
    evidence: list[dict],
    seen: dict[str, dict],
    trace: list[dict],
) -> None:
    rng = _relative_range(question)
    round_i = 1
    if rng:
        hits = list_by_date(user_id, rng[0], rng[1], 10)
        added = _merge_evidence(evidence, seen, hits, "list_recordings", f"按日期列出 {rng[0]} ~ {rng[1]}")
        trace.append({"round": round_i, "tool": "list_recordings", "label": f"{rng[0]} ~ {rng[1]}", "hits": len(added)})
        round_i += 1

    keyword_hits = search_keyword(user_id, question, 8)
    added = _merge_evidence(evidence, seen, keyword_hits, "search_keyword", f"精确搜索「{question[:30]}」")
    trace.append({"round": round_i, "tool": "search_keyword", "hits": len(added)})
    round_i += 1

    vector_hits = search_vector(user_id, question, 8)
    added = _merge_evidence(evidence, seen, vector_hits, "search_vector", f"语义匹配「{question[:30]}」")
    trace.append({"round": round_i, "tool": "search_vector", "hits": len(added)})


def _fallback_answer(question: str, evidence: list[dict]) -> tuple[str, list[int], list[int]]:
    if not evidence:
        return "我在你的录音里没有找到相关的内容。换个关键词或时间说法再试试？", [], []

    ranked = sorted(evidence, key=lambda e: e["score"], reverse=True)
    top = ranked[0]
    when = f"{top['date']} {top['time']}".strip()
    is_question = any(w in question for w in ("吗", "什么", "怎么", "为什么", "哪", "谁", "多少", "？", "?"))
    if is_question:
        answer = f"根据 {when} 的录音《{top['title']}》：{_clean(top['text'], 160)}"
    else:
        answer = f"找到 {len(ranked)} 条相关录音，最相关的是 {when} 的《{top['title']}》：{_clean(top['text'], 160)}"

    indices = []
    for e in ranked[:3]:
        indices.append(e["index"])
    return answer, indices[:2], indices


# ------------------------------------------------------------------ #
# 输出组装
# ------------------------------------------------------------------ #
def _match_items(evidence: list[dict], indices: list[int], citations: list[int]) -> list[dict]:
    picked: list[dict] = []
    seen_rec: set[str] = set()
    for i in indices:
        if i < 1 or i > len(evidence):
            continue
        e = evidence[i - 1]
        rid = e.get("recordingId")
        key = rid or f"day:{e.get('date')}"
        if key in seen_rec:
            continue
        seen_rec.add(key)
        date = e.get("date") or ""
        picked.append(
            {
                "recordingId": rid,
                "title": e.get("title") or "",
                "date": date,
                "time": e.get("time") or "",
                "startedAt": e.get("startedAt"),
                "durationMs": e.get("durationMs") or 0,
                "snippet": _clean(e.get("text"), 200),
                "kind": e.get("kind"),
                "startMs": e.get("startMs"),
                "score": round(float(e.get("score") or 0), 3),
                "matchedBy": e.get("label") or "",
                "cited": i in citations,
                "jumpUrl": f"/calendar?view=day&date={date}" if date else "/calendar",
                "recordingUrl": f"/recordings/{rid}" if rid else None,
            }
        )
        if len(picked) >= 3:
            break
    return picked


def _matches_text(matches: list[dict]) -> str:
    """推送给外部工具时的「依据」一行文本。"""
    parts = []
    for m in matches[:3]:
        when = " ".join(x for x in [m.get("date") or "", m.get("time") or ""] if x)
        title = m.get("title") or "未命名录音"
        parts.append(f"{when}《{title}》".strip())
    return "；".join(parts)


def run_find(
    user: dict,
    question: str,
    history: list[dict] | None = None,
    provider_id: str | None = None,
) -> dict:
    started = time.time()
    user_id = user["id"]
    question = " ".join(str(question or "").split())
    provider = resolve_llm(user_id, provider_id)

    # 用户配置的外部工具：拿缓存清单，不去现场握手
    try:
        mcp_tools = chat_tools(user_id)
    except Exception as exc:  # noqa: BLE001 - MCP 配置坏了不该拖垮找寻
        print(f"[mcp] 读取可用工具失败：{exc}", flush=True)
        mcp_tools = []

    evidence: list[dict] = []
    seen: dict[str, dict] = {}
    trace: list[dict] = []
    mode = "llm"
    answer = ""
    citations: list[int] = []
    matches_idx: list[int] = []
    note = ""

    demo = str(provider.get("kind") or "") == "mock"
    if demo:
        mode = "demo"
        _demo_pipeline(user_id, question, evidence, seen, trace)
        answer, citations, matches_idx = _fallback_answer(question, evidence)
        note = "当前使用演示引擎，未调用外部大模型；回答由检索结果直接摘录。"
    else:
        try:
            _agent_loop(user_id, question, history or [], provider, evidence, seen, trace, mcp_tools)
        except Exception as exc:  # noqa: BLE001 - 模型不可用时退回确定性检索
            mode = "fallback"
            note = f"大模型调用失败，已退回本地检索：{str(exc)[:160]}"
            _demo_pipeline(user_id, question, evidence, seen, trace)

        # 模型可能一轮都没检索就直接 final；补一次确定性双路检索，别让回答凭空空转。
        if mode != "fallback" and not evidence:
            _demo_pipeline(user_id, question, evidence, seen, trace)

        synthesized = None
        if mode != "fallback" and evidence:
            try:
                synthesized = _synthesize(build_llm(provider), question, evidence, history or [])
            except Exception as exc:  # noqa: BLE001
                mode = "fallback"
                note = f"大模型作答失败，已退回摘录式回答：{str(exc)[:160]}"

        if synthesized:
            answer, citations, matches_idx = synthesized
        else:
            if mode != "fallback":
                mode = "fallback"
            answer, citations, matches_idx = _fallback_answer(question, evidence)

    if not matches_idx:
        ranked = sorted(evidence, key=lambda e: e["score"], reverse=True)
        matches_idx = [e["index"] for e in ranked[:3]]

    matches = _match_items(evidence, matches_idx, citations)
    if not matches:
        # LLM 选出的编号指向了别处时，按分数兜底
        matches = _match_items(
            evidence,
            [e["index"] for e in sorted(evidence, key=lambda x: x["score"], reverse=True)[:3]],
            citations,
        )

    # 让大模型判断发不发、发到哪台服务（推送决策），同时挑出本次真正用得上的工具
    # （工具推荐）。两者互相独立、各自一次 LLM 调用，串行等于白等一个往返，这里并行。
    pushes: list[dict] = []
    push_note = ""
    recommendation: dict = {"tools": [], "by": "none", "reason": "", "note": ""}
    want_push = bool(answer and evidence)
    want_recommend = bool(mcp_tools and answer and not demo)

    def _do_push() -> tuple[list[dict], str, str]:
        try:
            out = decide_and_push(
                user_id,
                question,
                answer,
                mode="auto",
                provider_id=provider_id,
                extra={"matches": _matches_text(matches)},
            )
            return out["results"], out.get("note") or "", ""
        except Exception as exc:  # noqa: BLE001 - 推送是尽力而为
            return [], "", f"MCP 推送失败：{str(exc)[:120]}"

    def _do_recommend() -> dict:
        try:
            return recommend_chat_tools(user_id, question, answer, mcp_tools, provider=provider)
        except Exception as exc:  # noqa: BLE001 - 推荐失败不影响回答
            print(f"[mcp] 工具推荐失败：{exc}", flush=True)
            return {"tools": [], "by": "none", "reason": "", "note": ""}

    if want_push or want_recommend:
        with ThreadPoolExecutor(max_workers=2) as pool:
            push_future = pool.submit(_do_push) if want_push else None
            recommend_future = pool.submit(_do_recommend) if want_recommend else None
            if push_future is not None:
                pushes, push_note, push_error = push_future.result()
                if push_error:
                    note = (note + " " if note else "") + push_error
            if recommend_future is not None:
                recommendation = recommend_future.result()

    manual_targets: list[dict] = []
    try:
        manual_targets = [
            {"serverId": s["id"], "serverName": s.get("name") or ""}
            for s in push_targets(user_id, "manual")
        ]
    except Exception:  # noqa: BLE001
        manual_targets = []

    return {
        "answer": answer,
        "matches": matches,
        "evidence": [
            {
                "index": e["index"],
                "date": e["date"],
                "time": e["time"],
                "title": e["title"],
                "kind": e["kind"],
                "text": _clean(e["text"], 180),
            }
            for e in sorted(evidence, key=lambda x: x["index"])[:6]
        ],
        "trace": trace,
        "mode": mode,
        "note": note,
        "provider": {
            "id": provider.get("id"),
            "name": provider.get("name"),
            "model": provider.get("model"),
        },
        "mcp": {
            "tools": [
                {
                    "key": t["key"],
                    "name": t["name"],
                    "title": t["title"],
                    "description": t["description"],
                    "params": t["params"],
                    "serverId": t["serverId"],
                    "serverName": t["serverName"],
                }
                for t in mcp_tools
            ],
            "pushes": pushes,
            "pushNote": push_note,
            "manualTargets": manual_targets,
            "recommendedTools": recommendation["tools"],
            "recommendBy": recommendation["by"],
            "recommendReason": recommendation.get("reason") or "",
            "recommendNote": recommendation.get("note") or "",
        },
        "tookMs": int((time.time() - started) * 1000),
    }
