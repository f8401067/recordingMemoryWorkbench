"""对外 MCP 服务的令牌、客户端会话与调用记录。

和「设置 → MCP 工具」相反：那边是把**别人的** MCP 接进来用，这里是**把忆录自己**
作为一台 MCP 服务开放出去，给 Claude / Cursor / WorkBuddy 之类的客户端调用。

三件事：

1. 每个账号一把 Token，第一次打开设置页时自动生成，重置后旧值立刻失效
2. initialize 握手时记住 clientInfo，后续请求靠 Mcp-Session-Id 归到同一个客户端
3. 握手、列工具、调工具、鉴权失败都留一条记录，设置页能直接看到「谁在什么时候调了什么」
"""

from __future__ import annotations

import json
import secrets
from typing import Any

from ..config import uid
from ..db import all_, execute, now_iso, one

TOKEN_PREFIX = "mcps_"
MAX_LOGS = 300
MAX_SESSIONS = 100
SUPPORTED_PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")
DEFAULT_PROTOCOL = SUPPORTED_PROTOCOLS[0]


def _new_token() -> str:
    return TOKEN_PREFIX + secrets.token_urlsafe(32)


def _mask(token: str) -> str:
    s = str(token or "")
    return f"{s[:9]}...{s[-4:]}" if len(s) > 16 else s


# ------------------------------------------------------------------ #
# Token
# ------------------------------------------------------------------ #
def ensure_token(user_id: str) -> dict:
    """惰性生成：第一次访问设置页时自动建一把，不需要用户在页面上点「创建」。"""
    row = one("SELECT * FROM mcp_service_tokens WHERE user_id = ?", (user_id,))
    if row:
        return row
    t = now_iso()
    execute(
        """INSERT INTO mcp_service_tokens (id, user_id, token, enabled, created_at)
           VALUES (?, ?, ?, 1, ?)
           ON CONFLICT(user_id) DO NOTHING""",
        (uid("mcpt_"), user_id, _new_token(), t),
    )
    return one("SELECT * FROM mcp_service_tokens WHERE user_id = ?", (user_id,)) or {}


def reset_token(user_id: str) -> dict:
    ensure_token(user_id)
    execute(
        "UPDATE mcp_service_tokens SET token = ?, reset_at = ? WHERE user_id = ?",
        (_new_token(), now_iso(), user_id),
    )
    return one("SELECT * FROM mcp_service_tokens WHERE user_id = ?", (user_id,)) or {}


def set_enabled(user_id: str, enabled: bool) -> dict:
    ensure_token(user_id)
    execute(
        "UPDATE mcp_service_tokens SET enabled = ? WHERE user_id = ?",
        (1 if enabled else 0, user_id),
    )
    return one("SELECT * FROM mcp_service_tokens WHERE user_id = ?", (user_id,)) or {}


def resolve_token(token: str) -> dict | None:
    """返回 {user_id, token_id, username}；Token 不存在 / 被停用都算无效。"""
    if not token:
        return None
    row = one(
        """SELECT t.id AS token_id, t.user_id, t.enabled, u.username
           FROM mcp_service_tokens t JOIN users u ON u.id = t.user_id
           WHERE t.token = ?""",
        (token,),
    )
    if not row or not row.get("enabled"):
        return None
    execute("UPDATE mcp_service_tokens SET last_used_at = ? WHERE id = ?", (now_iso(), row["token_id"]))
    return row


# ------------------------------------------------------------------ #
# 客户端会话
# ------------------------------------------------------------------ #
def touch_session(
    session_id: str,
    *,
    user_id: str,
    token_id: str | None = None,
    client_name: str | None = None,
    client_version: str | None = None,
    protocol_version: str | None = None,
    ip: str | None = None,
    user_agent: str | None = None,
    initialize: bool = False,
) -> None:
    """握手时写入 clientInfo；后续请求只刷新最近活跃时间与调用次数。"""
    if not session_id:
        return
    t = now_iso()
    if initialize:
        execute(
            """INSERT INTO mcp_service_sessions
               (id, user_id, token_id, client_name, client_version, protocol_version,
                ip, user_agent, first_seen_at, last_seen_at, call_count)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
               ON CONFLICT(id) DO UPDATE SET
                 client_name = excluded.client_name,
                 client_version = excluded.client_version,
                 protocol_version = excluded.protocol_version,
                 token_id = excluded.token_id,
                 last_seen_at = excluded.last_seen_at,
                 call_count = mcp_service_sessions.call_count + 1""",
            (
                session_id,
                user_id,
                token_id,
                (client_name or "未知客户端")[:120],
                (client_version or "")[:60],
                (protocol_version or "")[:40],
                (ip or "")[:80],
                (user_agent or "")[:300],
                t,
                t,
            ),
        )
    else:
        execute(
            """UPDATE mcp_service_sessions
               SET last_seen_at = ?, call_count = call_count + 1 WHERE id = ?""",
            (t, session_id),
        )
    # 只保留最近一批会话，避免表无限长（日志另有独立保留策略）
    execute(
        """DELETE FROM mcp_service_sessions WHERE user_id = ? AND id NOT IN (
             SELECT id FROM mcp_service_sessions WHERE user_id = ?
             ORDER BY last_seen_at DESC LIMIT ?
           )""",
        (user_id, user_id, MAX_SESSIONS),
    )


def session_info(session_id: str, user_id: str) -> dict | None:
    if not session_id:
        return None
    return one(
        "SELECT * FROM mcp_service_sessions WHERE id = ? AND user_id = ?",
        (session_id, user_id),
    )


def session_any(session_id: str) -> dict | None:
    """鉴权失败时只有 session id，用它反查账号好把失败原因记到那个人名下。"""
    if not session_id:
        return None
    return one("SELECT * FROM mcp_service_sessions WHERE id = ?", (session_id,))


def list_clients(user_id: str, limit: int = 20) -> list[dict]:
    rows = all_(
        """SELECT * FROM mcp_service_sessions WHERE user_id = ?
           ORDER BY last_seen_at DESC LIMIT ?""",
        (user_id, max(1, min(100, limit))),
    )
    return [
        {
            "sessionId": r["id"],
            "clientName": r.get("client_name") or "未知客户端",
            "clientVersion": r.get("client_version") or "",
            "protocolVersion": r.get("protocol_version") or "",
            "ip": r.get("ip") or "",
            "userAgent": r.get("user_agent") or "",
            "firstSeenAt": r["first_seen_at"],
            "lastSeenAt": r["last_seen_at"],
            "callCount": r.get("call_count") or 0,
        }
        for r in rows
    ]


# ------------------------------------------------------------------ #
# 调用记录
# ------------------------------------------------------------------ #
def sanitize_args(method: str, args: Any) -> str:
    """日志里绝不落音频原文：上传工具的 base64 换成字节数描述。"""
    if not isinstance(args, dict):
        return ""
    safe = dict(args)
    for key in ("audioBase64", "audio", "data", "content"):
        if key in safe and isinstance(safe[key], str) and len(safe[key]) > 200:
            safe[key] = f"<已省略 {len(safe[key])} 字符>"
    if method == "tools/call":
        inner = safe.get("arguments")
        if isinstance(inner, dict):
            safe["arguments"] = {
                k: (f"<已省略 {len(v)} 字符>" if isinstance(v, str) and len(v) > 200 else v)
                for k, v in inner.items()
            }
    return json.dumps(safe, ensure_ascii=False)[:1500]


def log_call(
    user_id: str,
    *,
    session_id: str | None = None,
    client_name: str | None = None,
    method: str,
    tool: str | None = None,
    args: Any = None,
    ok: bool = True,
    error: str | None = None,
    duration_ms: int = 0,
    ip: str | None = None,
) -> None:
    try:
        execute(
            """INSERT INTO mcp_service_logs
               (id, user_id, session_id, client_name, method, tool, args, ok, error,
                duration_ms, ip, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                uid("msl_"),
                user_id,
                (session_id or "")[:80] or None,
                (client_name or "未知客户端")[:120],
                (method or "")[:80],
                (tool or "")[:120] or None,
                sanitize_args(method, args),
                1 if ok else 0,
                (str(error)[:500] if error else None),
                int(duration_ms or 0),
                (ip or "")[:80],
                now_iso(),
            ),
        )
        execute(
            """DELETE FROM mcp_service_logs WHERE user_id = ? AND id NOT IN (
                 SELECT id FROM mcp_service_logs WHERE user_id = ?
                 ORDER BY created_at DESC LIMIT ?
               )""",
            (user_id, user_id, MAX_LOGS),
        )
    except Exception as exc:  # noqa: BLE001 - 写日志失败不能影响调用结果
        print(f"[mcp-service] 调用日志写入失败：{exc}", flush=True)


def list_logs(user_id: str, limit: int = 60) -> list[dict]:
    rows = all_(
        """SELECT * FROM mcp_service_logs WHERE user_id = ?
           ORDER BY created_at DESC LIMIT ?""",
        (user_id, max(1, min(300, limit))),
    )
    return [
        {
            "id": r["id"],
            "sessionId": r.get("session_id") or "",
            "clientName": r.get("client_name") or "未知客户端",
            "method": r.get("method") or "",
            "tool": r.get("tool") or "",
            "args": r.get("args") or "",
            "ok": bool(r.get("ok")),
            "error": r.get("error") or "",
            "durationMs": r.get("duration_ms") or 0,
            "ip": r.get("ip") or "",
            "createdAt": r["created_at"],
        }
        for r in rows
    ]


def clear_logs(user_id: str) -> None:
    execute("DELETE FROM mcp_service_logs WHERE user_id = ?", (user_id,))
    execute("DELETE FROM mcp_service_sessions WHERE user_id = ?", (user_id,))


def stats(user_id: str) -> dict:
    total = one(
        "SELECT COUNT(*) AS n FROM mcp_service_logs WHERE user_id = ?", (user_id,)
    ) or {"n": 0}
    clients = one(
        "SELECT COUNT(*) AS n FROM mcp_service_sessions WHERE user_id = ?", (user_id,)
    ) or {"n": 0}
    last = one(
        "SELECT created_at FROM mcp_service_logs WHERE user_id = ? ORDER BY created_at DESC LIMIT 1",
        (user_id,),
    )
    tools = one(
        """SELECT COUNT(*) AS n FROM mcp_service_logs
           WHERE user_id = ? AND method = 'tools/call'""",
        (user_id,),
    ) or {"n": 0}
    return {
        "totalCalls": int(total["n"] or 0),
        "toolCalls": int(tools["n"] or 0),
        "clientCount": int(clients["n"] or 0),
        "lastCallAt": (last or {}).get("created_at"),
    }


def public_config(user_id: str, tools: list[dict]) -> dict:
    row = ensure_token(user_id)
    return {
        "enabled": bool(row.get("enabled", 1)),
        "endpointPath": "/mcp",
        "ssePath": "/sse",
        "token": row.get("token") or "",
        "tokenMasked": _mask(row.get("token") or ""),
        "createdAt": row.get("created_at"),
        "resetAt": row.get("reset_at"),
        "lastUsedAt": row.get("last_used_at"),
        "tools": [
            {"name": t["name"], "description": t["description"], "inputSchema": t["inputSchema"]}
            for t in tools
        ],
        "stats": stats(user_id),
    }
