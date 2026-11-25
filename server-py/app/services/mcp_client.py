"""MCP（Model Context Protocol）客户端。

只支持**远程**服务：Streamable HTTP（``http``）与旧版 SSE（``sse``），不执行任何
本地命令 —— 这是刻意的取舍，配置来自网页表单，不应该有能力在服务器上起进程。

对外是同步接口（``probe`` / ``call_tool``），内部用官方 SDK 的异步客户端。
每次操作独立握手：连上 → initialize → 干活 → 断开。工具清单由上层缓存到
``mcp_tools`` 表，所以这里的开销只发生在「刷新工具」和真正调用工具的时候。
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import time
from typing import Any, Callable

MCP_CONNECT_TIMEOUT = 15.0
MCP_LIST_TIMEOUT = 30.0
MCP_CALL_TIMEOUT = 90.0

TRANSPORTS = ("auto", "http", "sse")


class McpError(RuntimeError):
    """连不上 / 握手失败 / 服务端返回错误。"""


# ------------------------------------------------------------------ #
# 同步桥
# ------------------------------------------------------------------ #
def run_sync(coro, timeout: float | None = None) -> Any:
    """在同步代码里跑协程。

    FastAPI 的同步端点跑在线程池里，没有运行中的事件循环，``asyncio.run`` 就够。
    如果以后有人从 async 端点调过来，这里会自动换一个线程，不会炸在
    「asyncio.run() cannot be called from a running event loop」。
    """
    if timeout:
        coro = asyncio.wait_for(coro, timeout)

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


def parse_headers(raw: Any) -> dict[str, str]:
    """headers 字段存的是 JSON 对象文本；也容忍已经是 dict 的情况。"""
    if isinstance(raw, dict):
        data = raw
    elif isinstance(raw, str) and raw.strip():
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return {}
    else:
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, str] = {}
    for k, v in data.items():
        key = str(k).strip()
        if not key:
            continue
        out[key] = str(v)
    return out


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        try:
            return _jsonable(dump(mode="json"))
        except TypeError:
            return _jsonable(dump())
    return str(value)


def _attr(obj: Any, *names: str) -> Any:
    for n in names:
        if hasattr(obj, n):
            val = getattr(obj, n)
            if val is not None:
                return val
    return None


# ------------------------------------------------------------------ #
# 传输层
# ------------------------------------------------------------------ #
def _candidates(transport: str | None) -> list[str]:
    t = (transport or "auto").strip().lower()
    if t in ("http", "streamable-http", "streamable_http"):
        return ["http"]
    if t == "sse":
        return ["sse"]
    return ["http", "sse"]


def _is_local(url: str) -> bool:
    try:
        from urllib.parse import urlparse

        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return False
    return host in ("localhost", "127.0.0.1", "::1", "0.0.0.0", "host.docker.internal")


def _http_client(url: str, headers: dict[str, str]):
    """本机能直连，远端尊重 HTTP(S)_PROXY。本机走代理几乎必然 502。"""
    import httpx2

    return httpx2.AsyncClient(
        headers=headers or None,
        timeout=httpx2.Timeout(MCP_CALL_TIMEOUT, connect=MCP_CONNECT_TIMEOUT),
        trust_env=not _is_local(url),
        follow_redirects=True,
    )


async def _open(kind: str, url: str, headers: dict[str, str]):
    """返回 (read_stream, write_stream)，由调用方在 async with 里消费。"""
    if kind == "sse":
        from mcp.client.sse import sse_client

        return sse_client(
            url,
            headers=headers or None,
            timeout=MCP_CONNECT_TIMEOUT,
            sse_read_timeout=MCP_CALL_TIMEOUT,
            httpx_client_factory=lambda **_: _http_client(url, headers),
        )

    from mcp.client.streamable_http import streamable_http_client

    client = _http_client(url, headers)
    # 客户端要跟着传输层一起关，否则连接会漏
    return _Combined(
        client,
        streamable_http_client(url, http_client=client, terminate_on_close=True),
    )


class _Combined:
    """把一个 AsyncClient 的生命周期和传输层的生命周期绑在一起。"""

    def __init__(self, client, transport_cm) -> None:
        self._client = client
        self._transport_cm = transport_cm
        self._streams = None

    async def __aenter__(self):
        await self._client.__aenter__()
        try:
            self._streams = await self._transport_cm.__aenter__()
        except BaseException:
            await self._client.__aexit__(None, None, None)
            raise
        return self._streams

    async def __aexit__(self, *exc) -> bool:
        try:
            await self._transport_cm.__aexit__(*exc)
        finally:
            await self._client.__aexit__(*exc)
        return False


async def _with_session(server: dict, max_wait: float, action: Callable[[Any, str], Any]) -> Any:
    """依次尝试候选传输方式；都失败时把两边的错误拼起来。"""
    from mcp import ClientSession

    url = str(server.get("url") or "").strip()
    if not url:
        raise McpError("缺少服务地址")
    headers = parse_headers(server.get("headers"))

    async def attempt(kind: str) -> Any:
        async with await _open(kind, url, headers) as streams:
            read, write = streams[0], streams[1]
            async with ClientSession(read, write) as session:
                await session.initialize()
                return await action(session, kind)

    errors: list[str] = []
    for kind in _candidates(server.get("transport")):
        try:
            return await asyncio.wait_for(attempt(kind), max_wait)
        except asyncio.TimeoutError:
            errors.append(f"{kind}：超时（>{int(max_wait)}s）")
        except Exception as exc:  # noqa: BLE001 - 换下一种传输继续试
            errors.append(f"{kind}：{_short_error(exc)}")

    raise McpError("连接失败 · " + "；".join(errors))


def _short_error(exc: BaseException) -> str:
    text = str(exc).strip() or exc.__class__.__name__
    # anyio 的 ExceptionGroup 会带一长串 traceback 文本，这里只留最有用的那层
    if isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        inner = "；".join(_short_error(e) for e in exc.exceptions[:2])
        return inner[:300]
    return text[:300]


# ------------------------------------------------------------------ #
# 对外能力
# ------------------------------------------------------------------ #
def _tool_item(tool: Any) -> dict:
    schema = _attr(tool, "inputSchema", "input_schema") or {}
    return {
        "name": str(_attr(tool, "name") or ""),
        "title": str(_attr(tool, "title") or ""),
        "description": str(_attr(tool, "description") or ""),
        "inputSchema": _jsonable(schema),
    }


def probe(server: dict) -> dict:
    """连一次，列出工具。成功时顺带返回协议版本与服务端信息。"""

    async def action(session, kind: str) -> dict:
        res = await session.list_tools()
        info = _attr(session, "server_info") or {}
        return {
            "transport": kind,
            "protocolVersion": str(_attr(session, "protocol_version") or ""),
            "serverInfo": {
                "name": str(_attr(info, "name") or ""),
                "version": str(_attr(info, "version") or ""),
            },
            "tools": [_tool_item(t) for t in (getattr(res, "tools", None) or [])],
        }

    started = time.time()
    try:
        data = run_sync(_with_session(server, MCP_LIST_TIMEOUT, action), MCP_LIST_TIMEOUT + 10)
        return {
            "ok": True,
            "error": None,
            "latencyMs": int((time.time() - started) * 1000),
            **data,
        }
    except Exception as exc:  # noqa: BLE001 - 连通性检测把失败当结果返回
        return {
            "ok": False,
            "error": _short_error(exc),
            "latencyMs": int((time.time() - started) * 1000),
            "transport": None,
            "protocolVersion": "",
            "serverInfo": {},
            "tools": [],
        }


def call_tool(server: dict, tool: str, args: dict | None = None) -> dict:
    """调用一个工具。``ok`` 只代表这条链路是否跑通，工具自身报错看 ``isError``。"""
    payload = args if isinstance(args, dict) else {}
    started = time.time()

    async def action(session, kind: str) -> dict:
        res = await session.call_tool(str(tool), payload, read_timeout_seconds=MCP_CALL_TIMEOUT)
        content: list[dict] = []
        texts: list[str] = []
        for item in getattr(res, "content", None) or []:
            kind_name = str(_attr(item, "type") or "unknown")
            text = _attr(item, "text")
            if text is not None:
                content.append({"type": kind_name, "text": str(text)})
                texts.append(str(text))
            else:
                content.append({"type": kind_name, "data": _jsonable(item)})
        structured = _attr(res, "structuredContent", "structured_content")
        return {
            "transport": kind,
            "isError": bool(_attr(res, "isError", "is_error") or False),
            "content": content,
            "structured": _jsonable(structured) if structured is not None else None,
            "text": "\n".join(texts).strip(),
        }

    try:
        data = run_sync(_with_session(server, MCP_CALL_TIMEOUT + MCP_CONNECT_TIMEOUT, action), MCP_CALL_TIMEOUT + MCP_CONNECT_TIMEOUT + 10)
        return {"ok": True, "error": None, "latencyMs": int((time.time() - started) * 1000), **data}
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "error": _short_error(exc),
            "latencyMs": int((time.time() - started) * 1000),
            "transport": None,
            "isError": True,
            "content": [],
            "structured": None,
            "text": "",
        }


