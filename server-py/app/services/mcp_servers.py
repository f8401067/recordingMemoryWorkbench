"""用户自己的 MCP 服务：配置、工具缓存、调试调用、总结推送。

设计上的三条约束：

1. **只存远程地址**。没有 stdio，就不会因为填错一行配置在本机起进程。
2. **工具清单落库**。对话时不可能每次去握手列工具，刷新（连接）时才更新缓存。
3. **调用失败不影响主流程**。对话里的工具调用、总结推送都是"尽力而为"，
   失败只记日志、回传给界面，不抛给用户。
"""

from __future__ import annotations

import json
import re
from typing import Any

from ..config import uid
from ..db import all_, execute, now_iso, one
from .mcp_arg_suggest import prefetch_suggestions_bg
from .mcp_client import McpError, call_tool as mcp_call_tool, parse_headers, probe as mcp_probe

PUSH_MODES = ("off", "manual", "auto")
TRANSPORTS = ("auto", "http", "sse")
MAX_LOGS = 60

# 判断「这个工具能不能接收一段 AI 纪要」时用到的词根
TEXT_WORDS = ("text", "content", "body", "message", "note", "summary", "markdown", "value", "description", "memo")
READ_WORDS = ("list", "get", "find", "search", "read", "fetch", "query", "show", "describe", "count", "check", "export")
# 只有「名字以写入动词开头」才算明确的写入工具；note 这类名词太宽，
# 不能让 search_notes / get_note 因为有 required 文本参数就混进按钮列表。
WRITE_PREFIX = ("save", "add", "create", "write", "append", "record", "push", "send", "insert", "post", "store", "upload", "update", "upsert", "publish", "new")
# 改了判定规则就 +1：旧缓存会按当前规则重算，不用用户手动刷新每一台服务
CAP_VERSION = 4


def _is_string_spec(spec: Any) -> bool:
    """JSON Schema 里 type 可能是字符串，也可能是 ["string","null"] / anyOf。"""
    if not isinstance(spec, dict):
        return False
    typ = spec.get("type")
    if isinstance(typ, list):
        return "string" in typ
    if typ:
        return str(typ) == "string"
    if isinstance(spec.get("anyOf"), list):
        return any(_is_string_spec(x) for x in spec["anyOf"])
    return True  # 没声明类型，按字符串处理


def analyze_tool(tool: dict) -> dict:
    """看 inputSchema 猜这个工具能不能接收纪要正文，以及正文放哪个字段。

    判定得保守：纪要按钮点了就要能用，宁可少列也别列出会失败的工具。
    条件依次是「名字不像查询」「有能装正文的 string 字段（字段名带文本词根）」
    「必填项里没有数字/数组类的 ID、URL、附件」——后者意味着工具是在改一个
    已经存在的对象（updateBlinko 需要 id、createComment 需要 noteId），
    不适合拿一份新纪要直接调用。
    """
    schema = tool.get("inputSchema") if isinstance(tool.get("inputSchema"), dict) else {}
    props = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
    required = [str(k) for k in (schema.get("required") or [])]
    string_fields = [str(k) for k, spec in props.items() if _is_string_spec(spec)]
    tool_name = str(tool.get("name") or "").lower()

    looks_read = any(tool_name.startswith(w) for w in READ_WORDS)
    body_fields = [k for k in string_fields if any(w in k.lower() for w in TEXT_WORDS)]
    has_body_field = bool(body_fields)
    write_in_name = any(tool_name.startswith(w) for w in WRITE_PREFIX)

    # 必填的非字符串字段：noteId / id / urls … 服务端没法凭一份纪要猜出来
    needs_context = any(
        k in props and not _is_string_spec(props.get(k)) for k in required
    )

    if looks_read:
        reason = "纯查询工具"
        send = False
    elif not has_body_field:
        reason = "没有可写正文的文本字段"
        send = False
    elif needs_context:
        reason = "必填参数含 ID / 数组等无法自动填充的字段"
        send = False
    else:
        reason = "有正文字段且参数可自动填充" if write_in_name else "有正文字段"
        send = True

    def pick(words: tuple[str, ...]) -> str:
        for k in string_fields:
            if any(w in k.lower() for w in words):
                return k
        return ""

    body_field = body_fields[0] if body_fields else ""
    # 必填的正文类字段优先，避免工具其实要求 content 必填却填到了可选的 description
    for k in required:
        if k in body_fields:
            body_field = k
            break

    return {
        "v": CAP_VERSION,
        "sendSummary": bool(send),
        "bodyField": body_field,
        "titleField": pick(("title", "subject", "name", "heading")),
        "dateField": pick(("date", "day", "time")),
        "reason": reason,
    }


def _tool_capability(row: dict, tool: dict) -> dict:
    cap = _loads(row.get("capability"), None)
    if isinstance(cap, dict) and cap:
        # 人工指定/排除过：规则版本升级、刷新服务都不能把它改回去
        if cap.get("manual"):
            return cap
        if cap.get("v") == CAP_VERSION:
            return cap
    # 旧版本缓存 / 判定规则升级过：按当前 schema 直接重算，不打断页面
    return analyze_tool(tool)


def _string_fields(tool: dict) -> list[str]:
    schema = tool.get("inputSchema") if isinstance(tool.get("inputSchema"), dict) else {}
    props = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
    return [str(k) for k, spec in props.items() if _is_string_spec(spec)]


def _as_bool(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def _mask(secret: str) -> str:
    s = str(secret or "")
    if not s:
        return ""
    if len(s) <= 8:
        return "****"
    return f"{s[:4]}****{s[-4:]}"


def _loads(raw: Any, fallback: Any) -> Any:
    if isinstance(raw, (dict, list)):
        return raw
    if not raw:
        return fallback
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return fallback


def _tools_of(server_id: str) -> list[dict]:
    rows = all_(
        "SELECT name, title, description, input_schema, capability, suggested_args FROM mcp_tools "
        "WHERE server_id = ? ORDER BY name ASC",
        (server_id,),
    )
    out = []
    for r in rows:
        tool = {
            "name": r["name"],
            "title": r.get("title") or "",
            "description": r.get("description") or "",
            "inputSchema": _loads(r.get("input_schema"), {}),
        }
        tool["capability"] = _tool_capability(r, tool)
        # 大模型生成的建议参数（调试面板 / 纪要发送的默认值，可人工改）
        suggested = _loads(r.get("suggested_args"), None)
        if isinstance(suggested, dict) and suggested:
            tool["suggestedArgs"] = suggested
        out.append(tool)
    return out


def _param_names(schema: dict) -> list[str]:
    props = (schema or {}).get("properties") or {}
    required = set((schema or {}).get("required") or [])
    return [f"{k}{'*' if k in required else ''}" for k in props if isinstance(k, str)]


def public_server(row: dict, with_tools: bool = False) -> dict:
    headers = parse_headers(row.get("headers"))
    tools = _tools_of(row["id"])
    data = {
        "id": row["id"],
        "name": row.get("name") or "",
        "url": row.get("url") or "",
        "transport": row.get("transport") or "auto",
        "enabled": bool(row.get("enabled")),
        "useInChat": bool(row.get("use_in_chat")),
        "pushMode": row.get("push_mode") or "manual",
        "pushTool": row.get("push_tool") or "",
        "pushArgs": row.get("push_args") or "",
        "pushHint": row.get("push_hint") or "",
        "headers": {k: _mask(v) for k, v in headers.items()},
        "hasHeaders": bool(headers),
        "lastStatus": row.get("last_status") or "",
        "lastError": row.get("last_error") or "",
        "lastConnectedAt": row.get("last_connected_at"),
        "toolCount": len(tools),
        # 添加/刷新服务时识别出来的「能接收 AI 纪要」的工具数，设置页直接展示
        "summaryToolCount": sum(
            1 for t in tools if (t.get("capability") or {}).get("sendSummary")
        ),
        "createdAt": row.get("created_at"),
    }
    if with_tools:
        data["tools"] = tools
    return data


def find_server(user_id: str, server_id: str) -> dict | None:
    return one(
        "SELECT * FROM mcp_servers WHERE id = ? AND user_id = ?",
        (server_id, user_id),
    )


def list_servers(user_id: str) -> list[dict]:
    rows = all_(
        "SELECT * FROM mcp_servers WHERE user_id = ? ORDER BY created_at ASC",
        (user_id,),
    )
    # 工具清单一起带上：设置页要展开看，前端不用再逐台补请求
    return [public_server(r, with_tools=True) for r in rows]


# ------------------------------------------------------------------ #
# 增删改
# ------------------------------------------------------------------ #
def _clean_payload(body: dict, *, partial: bool = False) -> dict:
    out: dict[str, Any] = {}

    if "name" in body or not partial:
        name = str(body.get("name") or "").strip()
        if not name:
            raise ValueError("请填写服务名称")
        out["name"] = name[:40]

    if "url" in body or not partial:
        url = str(body.get("url") or "").strip()
        if not url:
            raise ValueError("请填写 MCP 服务地址")
        if not re.match(r"^https?://", url, re.I):
            raise ValueError("地址需要以 http:// 或 https:// 开头")
        out["url"] = url[:500]

    if "transport" in body:
        t = str(body.get("transport") or "auto").strip().lower()
        out["transport"] = t if t in TRANSPORTS else "auto"

    if "headers" in body:
        headers = body.get("headers")
        if isinstance(headers, str):
            headers = _loads(headers, {})
        out["headers"] = headers if isinstance(headers, dict) else {}

    if "enabled" in body:
        out["enabled"] = 1 if body.get("enabled") else 0
    if "useInChat" in body:
        out["use_in_chat"] = 1 if body.get("useInChat") else 0

    if "pushMode" in body:
        mode = str(body.get("pushMode") or "manual").strip().lower()
        out["push_mode"] = mode if mode in PUSH_MODES else "manual"
    if "pushHint" in body:
        out["push_hint"] = str(body.get("pushHint") or "").strip()[:500] or None

    return out


def create_server(user_id: str, body: dict) -> dict:
    data = _clean_payload(body)
    server_id = uid("mcp_")
    ts = now_iso()
    execute(
        """INSERT INTO mcp_servers
           (id, user_id, name, url, transport, headers, enabled, use_in_chat,
            push_mode, push_tool, push_args, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            server_id,
            user_id,
            data["name"],
            data["url"],
            data.get("transport", "auto"),
            json.dumps(data["headers"], ensure_ascii=False) if data.get("headers") else None,
            data.get("enabled", 1),
            data.get("use_in_chat", 1),
            data.get("push_mode", "manual"),
            data.get("push_tool"),
            data.get("push_args"),
            ts,
            ts,
        ),
    )
    row = find_server(user_id, server_id)
    assert row is not None
    # 添加后立刻连一次：工具清单直接可见，配置错也能当场看到原因
    return refresh_server(user_id, server_id) or public_server(row)


def update_server(user_id: str, server_id: str, body: dict) -> dict:
    row = find_server(user_id, server_id)
    if not row:
        raise LookupError("MCP 服务不存在")
    data = _clean_payload(body, partial=True)
    if not data:
        return public_server(row, with_tools=True)

    if "headers" in data:
        # 界面上显示的是掩码，原样回传时保留旧值
        old = parse_headers(row.get("headers"))
        merged: dict[str, str] = {}
        for k, v in data["headers"].items():
            key = str(k).strip()
            if not key:
                continue
            if "****" in str(v) and key in old:
                merged[key] = old[key]
            else:
                merged[key] = str(v)
        data["headers"] = json.dumps(merged, ensure_ascii=False) if merged else None

    sets = []
    params: list[Any] = []
    for column, value in data.items():
        sets.append(f"{column} = ?")
        params.append(value)
    sets.append("updated_at = ?")
    params.append(now_iso())
    params.extend([server_id, user_id])
    execute(f"UPDATE mcp_servers SET {', '.join(sets)} WHERE id = ? AND user_id = ?", params)
    return public_server(find_server(user_id, server_id) or {}, with_tools=True)


def delete_server(user_id: str, server_id: str) -> None:
    execute("DELETE FROM mcp_servers WHERE id = ? AND user_id = ?", (server_id, user_id))
    execute("DELETE FROM mcp_call_logs WHERE user_id = ? AND server_id = ?", (user_id, server_id))


# ------------------------------------------------------------------ #
# 连通性 / 工具缓存
# ------------------------------------------------------------------ #
def _save_tools(
    user_id: str, server_id: str, tools: list[dict], old_suggested: dict[str, Any] | None = None
) -> None:
    # 刷新会整表重建工具缓存，先把人工指定/排除的能力与人工改过的建议参数捞出来
    kept: dict[str, dict] = {}
    for r in all_(
        "SELECT name, capability, suggested_args FROM mcp_tools WHERE server_id = ? AND user_id = ?",
        (server_id, user_id),
    ):
        cap = _loads(r.get("capability"), None)
        if isinstance(cap, dict) and cap.get("manual"):
            kept[str(r["name"])] = cap
        old = (old_suggested or {}).get(str(r["name"]))
        if isinstance(old, str) and old.strip():
            kept.setdefault(str(r["name"]), {})["_suggested"] = old

    execute("DELETE FROM mcp_tools WHERE server_id = ? AND user_id = ?", (server_id, user_id))
    if not tools:
        return
    ts = now_iso()
    for t in tools:
        name = str(t.get("name") or "").strip()
        if not name:
            continue
        cap = analyze_tool(t)
        old = kept.get(name)
        suggested_raw = None
        if old:
            suggested_raw = old.pop("_suggested", None)
            # schema 可能变过：人工选的字段若已不存在，退回当前自动推断值
            fields = _string_fields(t)
            merged = {**old, "v": CAP_VERSION, "autoSendSummary": bool(cap.get("sendSummary"))}
            for key in ("bodyField", "titleField", "dateField"):
                val = str(merged.get(key) or "")
                if val and val not in fields:
                    merged[key] = str(cap.get(key) or "")
            if merged.get("sendSummary") and not merged.get("bodyField"):
                merged["bodyField"] = fields[0] if fields else ""
            cap = merged
        execute(
            """INSERT INTO mcp_tools
               (id, server_id, user_id, name, title, description, input_schema,
                capability, suggested_args, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                uid("mcpt_"),
                server_id,
                user_id,
                name[:120],
                (t.get("title") or "")[:200] or None,
                (t.get("description") or "")[:2000] or None,
                json.dumps(t.get("inputSchema") or {}, ensure_ascii=False),
                json.dumps(cap, ensure_ascii=False),
                suggested_raw,
                ts,
            ),
        )
    # 新工具入缓存后，后台让大模型逐个生成建议参数（慢，丢线程不阻塞响应）
    prefetch_suggestions_bg(user_id, server_id)


def refresh_server(user_id: str, server_id: str) -> dict | None:
    """连一次、列工具、写缓存。失败也更新 last_status，让界面能显示原因。"""
    row = find_server(user_id, server_id)
    if not row:
        return None
    res = mcp_probe(row)
    ts = now_iso()
    if res["ok"]:
        # 先记住旧的建议参数：_save_tools 整表重建时按名字带回来
        old_suggested = {
            str(r["name"]): r.get("suggested_args")
            for r in all_(
                "SELECT name, suggested_args FROM mcp_tools WHERE server_id = ? AND user_id = ?",
                (server_id, user_id),
            )
        }
        _save_tools(user_id, server_id, res.get("tools") or [], old_suggested)
        execute(
            "UPDATE mcp_servers SET last_status = 'ok', last_error = NULL, "
            "last_connected_at = ?, updated_at = ? WHERE id = ? AND user_id = ?",
            (ts, ts, server_id, user_id),
        )
    else:
        execute(
            "UPDATE mcp_servers SET last_status = 'error', last_error = ?, "
            "updated_at = ? WHERE id = ? AND user_id = ?",
            (str(res.get("error") or "")[:500], ts, server_id, user_id),
        )
    fresh = find_server(user_id, server_id) or row
    data = public_server(fresh, with_tools=True)
    data["probe"] = {
        "ok": res["ok"],
        "error": res.get("error"),
        "latencyMs": res.get("latencyMs"),
        "transport": res.get("transport"),
        "protocolVersion": res.get("protocolVersion"),
        "serverInfo": res.get("serverInfo"),
    }
    return data


def list_tools(user_id: str, server_id: str) -> list[dict]:
    if not find_server(user_id, server_id):
        raise LookupError("MCP 服务不存在")
    return _tools_of(server_id)


# ------------------------------------------------------------------ #
# 调用与日志
# ------------------------------------------------------------------ #
def _log_call(
    user_id: str,
    server_id: str | None,
    tool: str,
    args: dict,
    result: dict,
    source: str,
) -> None:
    try:
        execute(
            """INSERT INTO mcp_call_logs
               (id, user_id, server_id, tool, args, ok, result, error, duration_ms,
                source, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                uid("mcpl_"),
                user_id,
                server_id,
                tool[:120],
                json.dumps(args, ensure_ascii=False)[:4000],
                1 if result.get("ok") and not result.get("isError") else 0,
                (result.get("text") or json.dumps(result.get("structured"), ensure_ascii=False))[:4000]
                if (result.get("text") or result.get("structured"))
                else None,
                (str(result.get("error"))[:500] if result.get("error") else None),
                int(result.get("latencyMs") or 0),
                source,
                now_iso(),
            ),
        )
        # 每台服务只留最近 MAX_LOGS 条，调试记录不该无限长
        execute(
            """DELETE FROM mcp_call_logs WHERE user_id = ? AND id NOT IN (
                 SELECT id FROM mcp_call_logs WHERE user_id = ?
                 ORDER BY created_at DESC LIMIT ?
               )""",
            (user_id, user_id, MAX_LOGS),
        )
    except Exception as exc:  # noqa: BLE001 - 日志写不进去不能影响调用结果
        print(f"[mcp] 调用日志写入失败：{exc}", flush=True)


def call_server_tool(
    user_id: str, server_id: str, tool: str, args: dict | None, source: str = "debug"
) -> dict:
    row = find_server(user_id, server_id)
    if not row:
        raise LookupError("MCP 服务不存在")
    name = str(tool or "").strip()
    if not name:
        raise ValueError("请选择要调用的工具")
    payload = args if isinstance(args, dict) else {}
    res = mcp_call_tool(row, name, payload)
    _log_call(user_id, server_id, name, payload, res, source)
    return {
        "ok": res["ok"],
        "isError": res.get("isError"),
        "error": res.get("error"),
        "text": res.get("text") or "",
        "content": res.get("content") or [],
        "structured": res.get("structured"),
        "latencyMs": res.get("latencyMs") or 0,
        "transport": res.get("transport"),
    }


def list_logs(user_id: str, server_id: str | None = None, limit: int = 20) -> list[dict]:
    sql = "SELECT * FROM mcp_call_logs WHERE user_id = ?"
    params: list[Any] = [user_id]
    if server_id:
        sql += " AND server_id = ?"
        params.append(server_id)
    sql += " ORDER BY created_at DESC LIMIT ?"
    params.append(max(1, min(100, int(limit))))
    rows = all_(sql, params)
    return [
        {
            "id": r["id"],
            "serverId": r.get("server_id"),
            "tool": r.get("tool") or "",
            "args": _loads(r.get("args"), {}),
            "ok": bool(r.get("ok")),
            "result": r.get("result") or "",
            "error": r.get("error") or "",
            "durationMs": int(r.get("duration_ms") or 0),
            "source": r.get("source") or "debug",
            "createdAt": r.get("created_at"),
        }
        for r in rows
    ]


# ------------------------------------------------------------------ #
# 对话集成
# ------------------------------------------------------------------ #
def _slug(name: str, index: int) -> str:
    s = re.sub(r"[^0-9a-zA-Z]+", "_", str(name or "")).strip("_").lower()
    return s[:24] or f"server{index + 1}"


def _slug_map(servers: list[dict]) -> dict[str, str]:
    used: dict[str, int] = {}
    out: dict[str, str] = {}
    for i, s in enumerate(servers):
        base = _slug(s.get("name") or "", i)
        used[base] = used.get(base, 0) + 1
        out[s["id"]] = base if used[base] == 1 else f"{base}{used[base]}"
    return out


def chat_tools(user_id: str) -> list[dict]:
    """对话可用的工具清单（带命名空间，避免和内置检索工具重名）。"""
    servers = all_(
        "SELECT * FROM mcp_servers WHERE user_id = ? AND enabled = 1 AND use_in_chat = 1 "
        "ORDER BY created_at ASC",
        (user_id,),
    )
    if not servers:
        return []
    slugs = _slug_map(servers)
    out: list[dict] = []
    for s in servers:
        for t in _tools_of(s["id"]):
            schema = t.get("inputSchema") or {}
            out.append(
                {
                    "key": f"mcp__{slugs[s['id']]}__{t['name']}",
                    "serverId": s["id"],
                    "serverName": s.get("name") or "",
                    "name": t["name"],
                    "title": t.get("title") or "",
                    "description": t.get("description") or "",
                    "schema": schema,
                    "params": _param_names(schema),
                }
            )
    return out


def summary_tools(user_id: str) -> list[dict]:
    """所有可以接收 AI 纪要的工具，给纪要详情页渲染一键按钮。

    判定结果在添加/刷新服务时已经算好并缓存，这里不再连 MCP 服务。
    """
    rows = all_(
        """SELECT t.*, s.name AS server_name FROM mcp_tools t
           JOIN mcp_servers s ON s.id = t.server_id
           WHERE t.user_id = ? AND s.enabled = 1
           ORDER BY s.created_at ASC, t.name ASC""",
        (user_id,),
    )
    out: list[dict] = []
    for r in rows:
        tool = {
            "name": r["name"],
            "title": r.get("title") or "",
            "description": r.get("description") or "",
            "inputSchema": _loads(r.get("input_schema"), {}),
        }
        cap = _tool_capability(r, tool)
        if not cap.get("sendSummary"):
            continue
        suggested = _loads(r.get("suggested_args"), None)
        out.append(
            {
                "serverId": r["server_id"],
                "serverName": r.get("server_name") or "",
                "name": tool["name"],
                "title": tool["title"],
                "description": tool["description"],
                "params": _param_names(tool["inputSchema"]),
                "bodyField": cap.get("bodyField") or "",
                "titleField": cap.get("titleField") or "",
                "dateField": cap.get("dateField") or "",
                "capability": cap,
                "schema": tool["inputSchema"],
                "suggestedArgs": suggested if isinstance(suggested, dict) else {},
            }
        )
    return out


def find_summary_tool(user_id: str, server_id: str, tool_name: str) -> dict | None:
    for t in summary_tools(user_id):
        if t["serverId"] == server_id and t["name"] == tool_name:
            return t
    return None


# ------------------------------------------------------------------ #
# 工具能力人工覆盖：不认同自动判定，就自己选 / 取消 / 恢复自动
# ------------------------------------------------------------------ #
def set_suggested_args(user_id: str, server_id: str, tool_name: str, args: Any) -> dict:
    """人工改过的参数写回库里，作为这个工具的默认参数（刷新服务也保留）。"""
    if not find_server(user_id, server_id):
        raise LookupError("MCP 服务不存在")
    row = one(
        "SELECT * FROM mcp_tools WHERE server_id = ? AND user_id = ? AND name = ?",
        (server_id, user_id, tool_name),
    )
    if not row:
        raise LookupError("工具不存在，请先刷新服务")
    tool = {"name": row["name"], "inputSchema": _loads(row.get("input_schema"), {})}
    from .mcp_arg_suggest import validate_args

    clean = validate_args(tool, args)
    if not isinstance(clean, dict):
        clean = {}
    execute(
        "UPDATE mcp_tools SET suggested_args = ?, suggested_at = ?, updated_at = ? WHERE id = ?",
        (json.dumps(clean, ensure_ascii=False)[:4000], now_iso(), now_iso(), row["id"]),
    )
    fresh = find_server(user_id, server_id)
    return public_server(fresh or {}, with_tools=True)


def set_tool_capability(user_id: str, server_id: str, tool_name: str, body: dict) -> dict:
    """手动指定一个工具能不能接收纪要，或恢复自动判定。

    body:
    - ``{"sendSummary": true/false, "bodyField"?: "...", ...}`` 人工指定 / 排除
    - ``{"reset": true}`` 删掉人工标记，回到按 inputSchema 自动判定
    """
    if not find_server(user_id, server_id):
        raise LookupError("MCP 服务不存在")
    row = one(
        "SELECT * FROM mcp_tools WHERE server_id = ? AND user_id = ? AND name = ?",
        (server_id, user_id, tool_name),
    )
    if not row:
        raise LookupError("工具不存在，请先刷新服务")
    tool = {"name": row["name"], "inputSchema": _loads(row.get("input_schema"), {})}
    fields = _string_fields(tool)

    if _as_bool(body.get("reset")):
        cap = analyze_tool(tool)
    else:
        if "sendSummary" not in body:
            raise ValueError("缺少 sendSummary")
        auto = analyze_tool(tool)
        send = _as_bool(body.get("sendSummary"))
        cap: dict[str, Any] = {
            "v": CAP_VERSION,
            "manual": True,
            "sendSummary": send,
            "autoSendSummary": bool(auto.get("sendSummary")),
            "reason": "手动指定为可发送纪要" if send else "手动排除，不再出现在纪要下方",
            "bodyField": "",
            "titleField": "",
            "dateField": "",
        }
        for key in ("bodyField", "titleField", "dateField"):
            raw = body.get(key)
            val = str(raw).strip() if raw is not None else str(auto.get(key) or "")
            if val and val not in fields:
                raise ValueError(f"「{val}」不是 {tool_name} 的字符串参数")
            cap[key] = val
        if send and not cap["bodyField"]:
            if not fields:
                raise ValueError("这个工具没有字符串参数，放不下纪要正文")
            cap["bodyField"] = fields[0]

    execute(
        "UPDATE mcp_tools SET capability = ?, updated_at = ? WHERE id = ?",
        (json.dumps(cap, ensure_ascii=False), now_iso(), row["id"]),
    )
    fresh = find_server(user_id, server_id)
    if not fresh:
        raise LookupError("MCP 服务不存在")
    return public_server(fresh, with_tools=True)


def call_chat_tool(user_id: str, tool: dict, args: dict) -> dict:
    res = mcp_call_tool(
        find_server(user_id, tool["serverId"]) or {}, tool["name"], args
    )
    _log_call(user_id, tool["serverId"], tool["name"], args, res, "chat")
    return res


def push_targets(user_id: str, mode: str | None = None) -> list[dict]:
    """允许接收总结的服务。工具由大模型挑，配置里不再要求手填工具名。"""
    sql = (
        "SELECT * FROM mcp_servers WHERE user_id = ? AND enabled = 1 "
        "AND push_mode <> 'off' "
    )
    params: list[Any] = [user_id]
    if mode:
        sql += "AND push_mode = ? "
        params.append(mode)
    sql += "ORDER BY created_at ASC"
    return all_(sql, params)


def push_candidates(
    user_id: str, mode: str | None = None, server_ids: list[str] | None = None
) -> tuple[list[dict], list[dict]]:
    """给调度器用：候选服务 + 它们缓存的全部工具（含完整 schema）。"""
    servers = push_targets(user_id, mode)
    if server_ids:
        wanted = set(server_ids)
        servers = [s for s in servers if s["id"] in wanted]

    tools: list[dict] = []
    for s in servers:
        for t in _tools_of(s["id"]):
            tools.append(
                {
                    "serverId": s["id"],
                    "serverName": s.get("name") or "",
                    "serverHint": s.get("push_hint") or "",
                    "name": t["name"],
                    "title": t.get("title") or "",
                    "description": t.get("description") or "",
                    "schema": t.get("inputSchema") or {},
                }
            )
    return servers, tools


def import_config(user_id: str, raw: Any) -> dict:
    """从 JSON 配置导入。

    兼容几种常见写法：

    - ``{"mcpServers": {"名字": {"url": "...", "headers": {...}}}}``（Claude Desktop / Cursor）
    - ``{"servers": [{"name": "...", "url": "..."}]}``
    - ``[{"name": "...", "url": "..."}]``
    - 单个 ``{"name": "...", "url": "..."}``

    ``command`` 形式的本地服务会跳过并说明原因。
    """
    data = _loads(raw, None)
    if data is None:
        raise ValueError("不是合法的 JSON")

    entries: list[tuple[str, dict]] = []
    if isinstance(data, dict) and isinstance(data.get("mcpServers"), dict):
        entries = [(str(k), v if isinstance(v, dict) else {}) for k, v in data["mcpServers"].items()]
    elif isinstance(data, dict) and isinstance(data.get("servers"), list):
        entries = [
            (str((v or {}).get("name") or ""), v)
            for v in data["servers"]
            if isinstance(v, dict)
        ]
    elif isinstance(data, list):
        entries = [
            (str((v or {}).get("name") or ""), v) for v in data if isinstance(v, dict)
        ]
    elif isinstance(data, dict):
        entries = [(str(data.get("name") or ""), data)]
    else:
        raise ValueError("JSON 结构不支持")

    if not entries:
        raise ValueError("没有解析到任何 MCP 服务")

    added: list[dict] = []
    skipped: list[dict] = []
    existing = {s["name"]: s["id"] for s in list_servers(user_id)}

    for idx, (name, cfg) in enumerate(entries):
        label = name.strip() or str(cfg.get("name") or f"未命名 {idx + 1}")
        url = str(cfg.get("url") or cfg.get("baseUrl") or cfg.get("endpoint") or "").strip()
        if not url:
            if cfg.get("command"):
                skipped.append({"name": label, "reason": "本地命令（stdio）暂不支持，只支持远程 HTTP/SSE"})
            else:
                skipped.append({"name": label, "reason": "缺少 url"})
            continue
        headers = cfg.get("headers") if isinstance(cfg.get("headers"), dict) else {}
        transport = "sse" if str(cfg.get("type") or cfg.get("transport") or "").lower() == "sse" else "auto"
        payload = {
            "name": label,
            "url": url,
            "transport": transport,
            "headers": headers,
            "enabled": True,
            "useInChat": True,
        }
        try:
            if label in existing:
                # 同名视为更新地址与鉴权，不动推送配置
                updated = update_server(user_id, existing[label], payload)
                added.append(updated)
            else:
                created = create_server(user_id, payload)
                existing[label] = created["id"]
                added.append(created)
        except (ValueError, McpError) as exc:
            skipped.append({"name": label, "reason": str(exc)[:200]})
        except Exception as exc:  # noqa: BLE001
            skipped.append({"name": label, "reason": str(exc)[:200]})

    return {"added": added, "skipped": skipped}
