"""对外 MCP 服务：Streamable HTTP（+ 旧版 HTTP/SSE）、令牌、调用记录。

协议按 MCP 2025-06-18 的 Streamable HTTP 实现，只用到 JSON-RPC 2.0 的
initialize / tools/list / tools/call / ping 等少数方法，返回 ``application/json``
或单条 SSE ``message`` 事件，兼容主流客户端（Claude Desktop / Cursor / WorkBuddy 等）。
不依赖 mcp SDK 的服务端实现，避免 SDK 大版本升级把内置服务一起带崩。
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from fastapi import APIRouter, Body, Depends, Query, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from ..config import uid
from ..security import current_user
from ..services import mcp_service as store
from ..services.mcp_tools import HANDLERS, TOOLS, dispatch_tool

mgmt = APIRouter(prefix="/api/mcp-service", tags=["mcp-service"])
proto = APIRouter(tags=["mcp-service"])

SERVER_INFO = {"name": "recall-mcp-service", "version": "1.0.0"}
INSTRUCTIONS = (
    "忆录 · Recall：本机录音记忆库。可以上传音频（自动转写并生成 AI 纪要）、"
    "按日期读取 AI 回忆、检索录音全文、查看待办。日期一律用 YYYY-MM-DD。"
)
PROTOCOL_HEADER = "MCP-Protocol-Version"


# ------------------------------------------------------------------ #
# 管理接口（JWT 登录）
# ------------------------------------------------------------------ #
@mgmt.get("")
def get_config(user: dict = Depends(current_user)) -> dict:
    return store.public_config(user["id"], TOOLS)


@mgmt.post("/reset-token")
def reset_token(user: dict = Depends(current_user)) -> dict:
    row = store.reset_token(user["id"])
    return {"ok": True, "token": row.get("token") or "", "resetAt": row.get("reset_at")}


@mgmt.patch("")
def set_enabled(
    body: dict[str, Any] = Body(default_factory=dict),
    user: dict = Depends(current_user),
) -> dict:
    if "enabled" not in body:
        row = store.ensure_token(user["id"])
    else:
        row = store.set_enabled(user["id"], bool(body["enabled"]))
    return {"ok": True, "enabled": bool(row.get("enabled", 1))}


@mgmt.get("/logs")
def get_logs(limit: int = Query(60, ge=1, le=300), user: dict = Depends(current_user)) -> dict:
    return {
        "clients": store.list_clients(user["id"], 20),
        "logs": store.list_logs(user["id"], limit),
        "stats": store.stats(user["id"]),
    }


@mgmt.delete("/logs")
def clear_logs(user: dict = Depends(current_user)) -> dict:
    store.clear_logs(user["id"])
    return {"ok": True}


# ------------------------------------------------------------------ #
# 协议层：鉴权 / 上下文
# ------------------------------------------------------------------ #
def _token_of(request: Request) -> str:
    token = request.headers.get("x-mcp-token") or request.headers.get("x-api-key") or ""
    auth = request.headers.get("authorization") or ""
    if auth.lower().startswith("bearer "):
        token = token or auth[7:].strip()
    return (token or request.query_params.get("token") or request.query_params.get("key") or "").strip()


def _request_meta(request: Request) -> dict:
    return {
        "ip": request.client.host if request.client else "",
        "ua": (request.headers.get("user-agent") or "")[:300],
    }


def _context(request: Request, token_row: dict, session_id: str = "") -> dict:
    meta = _request_meta(request)
    sess = store.session_info(session_id, token_row["user_id"]) if session_id else None
    return {
        "user_id": token_row["user_id"],
        "token_id": token_row["token_id"],
        "session_id": session_id,
        "client_name": (sess or {}).get("client_name") or meta["ua"][:60] or "未知客户端",
        "ip": meta["ip"],
        "ua": meta["ua"],
        "protocol": request.headers.get(PROTOCOL_HEADER) or store.DEFAULT_PROTOCOL,
    }


def _unauthorized(request: Request, hint: str = "") -> JSONResponse:
    # 已知会话的失败请求也留一条记录，方便看出「哪个客户端的 Token 过期了」
    sid = request.headers.get("mcp-session-id") or ""
    if sid:
        row = store.session_any(sid)
        if row:
            store.log_call(
                row["user_id"],
                session_id=sid,
                client_name=row.get("client_name"),
                method="auth",
                ok=False,
                error="Token 无效或已停用",
                ip=_request_meta(request)["ip"],
            )
    return JSONResponse(
        {"error": "MCP Token 无效或已停用，请到「设置 → 数据与设备 → MCP 服务」重置/启用"},
        status_code=401,
        headers={"WWW-Authenticate": "Bearer", "Access-Control-Expose-Headers": "Mcp-Session-Id"},
    )


def _ok(result: dict | list | None, msg_id: Any) -> dict:
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _err(msg_id: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def _log(ctx: dict, *, method: str, args: Any = None, ok: bool = True,
         error: str | None = None, tool: str | None = None, duration_ms: int = 0) -> None:
    store.log_call(
        ctx["user_id"],
        session_id=ctx.get("session_id"),
        client_name=ctx.get("client_name"),
        method=method,
        tool=tool,
        args=args,
        ok=ok,
        error=error,
        duration_ms=duration_ms,
        ip=ctx.get("ip"),
    )


def _touch(ctx: dict, request: Request, initialize: bool = False, **kwargs: Any) -> None:
    """普通请求刷新会话；握手或会话丢失时补建一行，保证调用记录能归到客户端。"""
    sid = ctx.get("session_id")
    if not sid:
        return
    if not initialize and store.session_info(sid, ctx["user_id"]):
        store.touch_session(sid, user_id=ctx["user_id"])
        return
    store.touch_session(
        sid,
        user_id=ctx["user_id"],
        token_id=ctx.get("token_id"),
        client_name=kwargs.get("client_name") or ctx.get("client_name"),
        client_version=kwargs.get("client_version"),
        protocol_version=kwargs.get("protocol_version") or ctx.get("protocol"),
        ip=ctx.get("ip"),
        user_agent=ctx.get("ua"),
        initialize=True,
    )


async def _dispatch_one(msg: Any, ctx: dict, request: Request) -> tuple[dict | None, dict]:
    """处理一条 JSON-RPC 消息，返回 (响应对象或 None, 附加响应头)。"""
    if not isinstance(msg, dict):
        return _err(None, -32600, "Invalid Request"), {}
    msg_id = msg.get("id")
    has_id = "id" in msg and msg_id is not None
    method = str(msg.get("method") or "")
    params = msg.get("params") if isinstance(msg.get("params"), dict) else {}

    # 通知（notifications/initialized 等）不带 id，按规范只回 202
    if not has_id:
        return None, {}

    if method == "initialize":
        sid = uid("mss_")
        wanted = str(params.get("protocolVersion") or store.DEFAULT_PROTOCOL)
        version = wanted if wanted in store.SUPPORTED_PROTOCOLS else store.DEFAULT_PROTOCOL
        info = params.get("clientInfo") if isinstance(params.get("clientInfo"), dict) else {}
        client_name = str(info.get("name") or "未知客户端")[:120]
        client_version = str(info.get("version") or "")[:60]
        store.touch_session(
            sid,
            user_id=ctx["user_id"],
            token_id=ctx.get("token_id"),
            client_name=client_name,
            client_version=client_version,
            protocol_version=version,
            ip=ctx.get("ip"),
            user_agent=ctx.get("ua"),
            initialize=True,
        )
        _log(
            {**ctx, "session_id": sid, "client_name": client_name},
            method="initialize",
            args={"protocolVersion": version, "clientInfo": info},
        )
        return (
            _ok(
                {
                    "protocolVersion": version,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": SERVER_INFO,
                    "instructions": INSTRUCTIONS,
                },
                msg_id,
            ),
            {"Mcp-Session-Id": sid, PROTOCOL_HEADER: version},
        )

    if method == "tools/list":
        _touch(ctx, request)
        _log(ctx, method="tools/list")
        return _ok({"tools": TOOLS}, msg_id), {}

    if method == "tools/call":
        _touch(ctx, request)
        name = str(params.get("name") or "")
        args = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
        if name not in HANDLERS:
            _log(ctx, method="tools/call", tool=name, args=params, ok=False, error="未知工具")
            return _err(msg_id, -32602, f"未知工具：{name}"), {}
        started = time.perf_counter()
        try:
            text = await asyncio.to_thread(dispatch_tool, ctx["user_id"], name, args)
            error = None
        except Exception as exc:  # noqa: BLE001 - 工具错误按 MCP 规范放进 result
            text = f"调用失败：{exc}"
            error = str(exc)
        duration = int((time.perf_counter() - started) * 1000)
        _log(
            ctx,
            method="tools/call",
            tool=name,
            args=params,
            ok=error is None,
            error=error,
            duration_ms=duration,
        )
        return (
            _ok({"content": [{"type": "text", "text": text}], "isError": error is not None}, msg_id),
            {},
        )

    if method == "ping":
        return _ok({}, msg_id), {}
    if method in ("resources/list", "resources/templates/list"):
        return _ok({"resources": []}, msg_id), {}
    if method == "prompts/list":
        return _ok({"prompts": []}, msg_id), {}
    if method in ("logging/setLevel", "completion/complete"):
        return _ok({}, msg_id), {}

    _log(ctx, method=method, args=params, ok=False, error="不支持的方法")
    return _err(msg_id, -32601, f"不支持的方法：{method}"), {}


def _base_headers(extra: dict | None = None) -> dict:
    return {
        PROTOCOL_HEADER: store.DEFAULT_PROTOCOL,
        "Access-Control-Expose-Headers": f"Mcp-Session-Id, {PROTOCOL_HEADER}",
        "Cache-Control": "no-cache",
        **(extra or {}),
    }


async def _handle_http(request: Request, ctx: dict) -> Response:
    raw = await request.body()
    try:
        payload = json.loads(raw or b"{}")
    except (ValueError, UnicodeDecodeError):
        return JSONResponse(_err(None, -32700, "JSON 解析失败"), headers=_base_headers())

    messages = payload if isinstance(payload, list) else [payload]
    responses: list[dict] = []
    headers: dict = {}
    for msg in messages:
        res, extra = await _dispatch_one(msg, ctx, request)
        if res is not None:
            responses.append(res)
        headers.update(extra)
    headers.update(_base_headers(headers))
    headers[PROTOCOL_HEADER] = headers.get(PROTOCOL_HEADER) or ctx.get("protocol") or store.DEFAULT_PROTOCOL

    if not responses:
        return Response(status_code=202, headers=headers)
    body: Any = responses if isinstance(payload, list) else responses[0]

    accept = request.headers.get("accept") or ""
    if "text/event-stream" in accept and "application/json" not in accept:
        async def sse_body():
            yield f"event: message\ndata: {json.dumps(body, ensure_ascii=False)}\n\n"

        return StreamingResponse(
            sse_body(),
            media_type="text/event-stream",
            headers=headers,
        )
    return JSONResponse(body, headers=headers)


# ------------------------------------------------------------------ #
# 协议端点：POST /mcp（Streamable HTTP）
# ------------------------------------------------------------------ #
@proto.post("/mcp")
async def mcp_endpoint(request: Request) -> Response:
    row = store.resolve_token(_token_of(request))
    if not row:
        return _unauthorized(request)
    sid = (
        request.headers.get("mcp-session-id")
        or request.query_params.get("sessionId")
        or ""
    ).strip()[:80]
    ctx = _context(request, row, sid)
    return await _handle_http(request, ctx)


@proto.get("/mcp")
def mcp_get_not_supported() -> Response:
    """不开独立的 GET SSE 流：需要旧版 SSE 的客户端用 /sse。"""
    return Response(status_code=405, headers={"Allow": "POST, DELETE", **_base_headers()})


@proto.delete("/mcp")
def mcp_delete_session(request: Request) -> Response:
    row = store.resolve_token(_token_of(request))
    if not row:
        return _unauthorized(request)
    return Response(status_code=204, headers=_base_headers())


# ------------------------------------------------------------------ #
# 兼容旧客户端：GET /sse + POST /messages
# ------------------------------------------------------------------ #
_sse_queues: dict[str, asyncio.Queue] = {}


@proto.get("/sse")
async def sse_endpoint(request: Request) -> Response:
    row = store.resolve_token(_token_of(request))
    if not row:
        return _unauthorized(request)
    token = _token_of(request)
    sid = uid("mss_")
    queue: asyncio.Queue = asyncio.Queue()
    _sse_queues[sid] = queue
    store.touch_session(
        sid,
        user_id=row["user_id"],
        token_id=row["token_id"],
        client_name=request.headers.get("user-agent") or "旧版 SSE 客户端",
        protocol_version=request.headers.get(PROTOCOL_HEADER) or "2024-11-05",
        ip=_request_meta(request)["ip"],
        user_agent=_request_meta(request)["ua"],
        initialize=True,
    )

    async def events():
        try:
            yield f"event: endpoint\ndata: /messages?sessionId={sid}&token={token}\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                yield f"event: message\ndata: {json.dumps(item, ensure_ascii=False)}\n\n"
        finally:
            _sse_queues.pop(sid, None)

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", **_base_headers()},
    )


@proto.post("/messages")
async def sse_messages(request: Request, sessionId: str = Query("")) -> Response:
    row = store.resolve_token(_token_of(request))
    if not row:
        return _unauthorized(request)
    sid = (sessionId or request.query_params.get("sessionId") or "").strip()[:80]
    ctx = _context(request, row, sid)
    if sid and not store.session_info(sid, row["user_id"]):
        _touch(ctx, request, initialize=True)

    raw = await request.body()
    try:
        payload = json.loads(raw or b"{}")
    except (ValueError, UnicodeDecodeError):
        payload = None
    messages = payload if isinstance(payload, list) else [payload]
    for msg in messages:
        res, _ = await _dispatch_one(msg, ctx, request)
        if res is not None:
            queue = _sse_queues.get(sid)
            if queue is not None:
                await queue.put(res)
    return Response(status_code=202, headers=_base_headers())
