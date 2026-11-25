"""让大模型按工具的 schema 给出一份「建议参数」。

三个入口共用：

- 设置 → MCP → 调试面板：选中工具先预填建议参数，用户改完再调；
- 纪要详情页一键发送：点工具先弹出可编辑的参数预览，确认再发；
- 添加 / 刷新 MCP 服务：后台异步预生成，存 ``mcp_tools.suggested_args``。

大模型判断时**只看工具自己的 schema 与说明**，不塞纪要正文等业务数据；
生成的参数经过白名单 + 类型校验才落库 / 返回，非法输出退回规则兜底。
"""

from __future__ import annotations

import json
import re
from typing import Any

from ..db import all_, execute, now_iso, one
from .providers import build_llm, resolve_llm

MAX_PROPS_IN_PROMPT = 14
MAX_ARGS_CHARS = 4000

SYSTEM = """你是「忆录」的 MCP 工具参数助手。根据工具的 inputSchema 和说明，给出一组合法的调用参数。

【工具】{tool_line}
【参数 schema】{schema_json}

规则：
1. 只输出 schema 里声明的字段，键名严格一致，不要发明字段。
2. 类型必须对齐：string / integer / number / boolean / array / object。
3. required 字段不能缺；可选字段拿不准就不要填。
4. 值要有真实感：标题写「今日会议纪要」、日期写今天（YYYY-MM-DD）、语言写 zh、数量给 1。
   但不要编造具体的笔记 id、对方账号、URL 这类你没有依据的值——那种字段留空字符串或干脆不填。
5. 只输出一个 JSON 对象，不要 Markdown 代码块、不要解释文字；没有可填的字段时输出 {{}}。"""


def _loads(raw: Any, fallback: Any) -> Any:
    if isinstance(raw, (dict, list)):
        return raw
    if not raw:
        return fallback
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return fallback


def _spec_type(spec: Any) -> str:
    """JSON Schema 的 type 可能是字符串、数组（可空）或 anyOf。"""
    if not isinstance(spec, dict):
        return "string"
    typ = spec.get("type")
    if isinstance(typ, list):
        for t in typ:
            if isinstance(t, str) and t != "null":
                return t
        return "string"
    if isinstance(typ, str):
        return typ
    any_of = spec.get("anyOf")
    if isinstance(any_of, list):
        for sub in any_of:
            if isinstance(sub, dict) and isinstance(sub.get("type"), str):
                return sub["type"]
    return "string"


def _coerce(value: Any, spec: Any) -> Any:
    """按 schema 类型纠正值；None / 空串不硬转成 "None"，按类型给空值。"""
    typ = _spec_type(spec)
    enum = spec.get("enum") if isinstance(spec, dict) else None
    if value is None or (isinstance(value, str) and not value.strip()):
        if isinstance(enum, list) and enum:
            return enum[0]
        return {"string": "", "integer": 1, "number": 1.0, "boolean": False, "array": [], "object": {}}.get(typ, "")
    try:
        if typ == "integer":
            out = int(float(value))
        elif typ == "number":
            out = float(value)
        elif typ == "boolean":
            if isinstance(value, str):
                out = value.strip().lower() in ("true", "1", "yes", "on")
            else:
                out = bool(value)
        elif typ == "array":
            out = value if isinstance(value, list) else []
        elif typ == "object":
            out = value if isinstance(value, dict) else {}
        else:
            out = str(value)
    except (TypeError, ValueError):
        out = {"string": "", "integer": 1, "number": 1.0, "boolean": False, "array": [], "object": {}}.get(typ, "")
    if isinstance(enum, list) and enum and out not in enum:
        return enum[0]
    return out


def _compact_schema(schema: Any) -> dict:
    """只带模型需要的部分进提示词：类型、枚举、说明，丢掉嵌套细节。"""
    data = schema if isinstance(schema, dict) else {}
    props = data.get("properties") or {}
    out: dict[str, Any] = {}
    for key, spec in list(props.items())[:MAX_PROPS_IN_PROMPT]:
        if not isinstance(spec, dict):
            continue
        item: dict[str, Any] = {"type": _spec_type(spec)}
        enum = spec.get("enum")
        if isinstance(enum, list) and enum:
            item["enum"] = enum[:6]
        desc = str(spec.get("description") or "").strip()[:80]
        if desc:
            item["description"] = desc
        out[str(key)] = item
    return {"properties": out, "required": [str(k) for k in (data.get("required") or [])]}


def _rule_fallback(tool: dict) -> dict:
    """模型不可用时的兜底：必填字段给类型默认值，其余留空。"""
    schema = tool.get("inputSchema") if isinstance(tool.get("inputSchema"), dict) else {}
    props = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
    required = [str(k) for k in (schema.get("required") or [])]
    out: dict[str, Any] = {}
    for key in required:
        spec = props.get(key)
        if isinstance(spec, dict) and isinstance(spec.get("enum"), list) and spec["enum"]:
            out[key] = spec["enum"][0]
        else:
            out[key] = _coerce(None, spec)
    return out


def _normalize_suggestion(tool: dict, raw: Any) -> dict:
    """白名单 + 类型对齐：模型多写的键丢掉，类型错的按 schema 纠正。"""
    schema = tool.get("inputSchema") if isinstance(tool.get("inputSchema"), dict) else {}
    props = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
    required = [str(k) for k in (schema.get("required") or [])]
    src = raw if isinstance(raw, dict) else {}
    # 模型偶尔包一层 {"args": {...}}
    if isinstance(src.get("args"), dict):
        src = src["args"]

    out: dict[str, Any] = {}
    for key, spec in props.items():
        if key not in src:
            continue
        out[str(key)] = _coerce(src[key], spec)
    for key in required:
        if key not in out:
            out[key] = _coerce(None, props.get(key))
    return out


def _alternative_providers(user_id: str, primary_id: str) -> list[dict]:
    """默认引擎吐空字 / 不返回 JSON 时的备胎；只影响参数建议这一处轻量链路。"""
    try:
        return all_(
            """SELECT * FROM providers WHERE user_id = ? AND category = 'llm'
               AND enabled = 1 AND kind <> 'mock' AND id <> ?
               ORDER BY is_default DESC, created_at ASC LIMIT 2""",
            (user_id, primary_id),
        )
    except Exception:  # noqa: BLE001
        return []


def _loads_loose(raw: str) -> dict | None:
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
    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end > start:
        try:
            data = json.loads(text[start : end + 1])
            return data if isinstance(data, dict) else None
        except (json.JSONDecodeError, TypeError):
            return None
    return None


def suggest_args(user_id: str, tool: dict, *, provider_id: str | None = None) -> tuple[dict, str]:
    """让大模型给出建议参数。返回 ``(args, note)``，note 说明来源（大模型 / 兜底）。

    大模型挂了 / 输出非法时退回规则兜底，不让参数建议拖垮主流程。
    """
    schema = tool.get("inputSchema") if isinstance(tool.get("inputSchema"), dict) else {}
    props = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
    if not props:
        return {}, "这个工具没有参数"

    name = str(tool.get("name") or "")
    server = str(tool.get("serverName") or "")
    desc = str(tool.get("description") or tool.get("title") or "").strip()[:140]
    tool_line = f"[{server}] {name} — {desc}" if server else f"{name} — {desc}"

    provider = resolve_llm(user_id, provider_id)
    if str(provider.get("kind") or "") == "mock":
        return _rule_fallback(tool), "演示引擎无法判断参数，已按 schema 兜底"

    sys_msg = SYSTEM.replace("{tool_line}", tool_line).replace(
        "{schema_json}", json.dumps(_compact_schema(schema), ensure_ascii=False)
    )
    engines = [provider, *_alternative_providers(user_id, str(provider.get("id") or ""))]
    last_err = ""
    for i, engine in enumerate(engines):
        try:
            raw = build_llm(engine).chat(
                [
                    {"role": "system", "content": sys_msg},
                    {"role": "user", "content": "按上面的规则给出这一个工具的参数建议。"},
                ],
                json_mode=True,
                temperature=0.2,
                max_tokens=500,
            )
            data = _loads_loose(str(raw or ""))
            if data is None:
                raise ValueError("模型输出不是 JSON")
            note = "大模型按工具 schema 生成"
            if i:
                note += f"（默认引擎无响应，已改用「{engine.get('name') or engine.get('model') or '备用引擎'}」）"
            return _normalize_suggestion(tool, data), note
        except Exception as exc:  # noqa: BLE001 - 单台引擎失败换下一台
            last_err = str(exc)[:80]
            continue
    return _rule_fallback(tool), f"模型不可用（{last_err or '全部引擎无响应'}），已按 schema 兜底"


# ------------------------------------------------------------------ #
# 落库：调试面板 / 纪要发送 / 刷新后预生成 都读这里
# ------------------------------------------------------------------ #
def get_suggested(user_id: str, server_id: str, tool_name: str) -> dict:
    row = one(
        "SELECT suggested_args, suggested_at FROM mcp_tools WHERE server_id = ? AND user_id = ? AND name = ?",
        (server_id, user_id, tool_name),
    )
    args = _loads((row or {}).get("suggested_args"), None)
    return args if isinstance(args, dict) else {}


def save_suggested(user_id: str, server_id: str, tool_name: str, args: dict) -> None:
    """人工改过的参数写回库里，下次直接用它（刷新服务也会保留）。"""
    execute(
        "UPDATE mcp_tools SET suggested_args = ?, suggested_at = ? "
        "WHERE server_id = ? AND user_id = ? AND name = ?",
        (
            json.dumps(args, ensure_ascii=False)[:MAX_ARGS_CHARS],
            now_iso(),
            server_id,
            user_id,
            tool_name,
        ),
    )


def validate_args(tool: dict, args: Any) -> dict:
    """前端回传的人工修改：只认 schema 里声明的键，类型对齐。

    tool 的 schema 可能在 ``inputSchema``（调试面板 / 预生成链路）或
    ``schema``（summary_tools 缓存链路）两个键上，都认。
    """
    schema = tool.get("inputSchema") if isinstance(tool.get("inputSchema"), dict) else None
    if not isinstance(schema, dict):
        schema = tool.get("schema") if isinstance(tool.get("schema"), dict) else {}
    props = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
    raw = args if isinstance(args, dict) else {}
    if not props:
        return {}
    out: dict[str, Any] = {}
    for key, spec in props.items():
        if key in raw:
            out[str(key)] = _coerce(raw[key], spec)
    return out


def prefetch_suggestions(user_id: str, server_id: str) -> None:
    """添加 / 刷新服务后后台跑：还没有建议参数的工具，逐个让大模型生成。"""
    try:
        rows = all_(
            "SELECT name, title, description, input_schema FROM mcp_tools "
            "WHERE server_id = ? AND user_id = ? AND (suggested_args IS NULL OR suggested_args = '')",
            (server_id, user_id),
        )
    except Exception:  # noqa: BLE001 - 预生成失败不影响主流程
        return
    for r in rows:
        tool = {
            "name": r["name"],
            "title": r.get("title") or "",
            "description": r.get("description") or "",
            "inputSchema": _loads(r.get("input_schema"), {}),
        }
        try:
            args, _note = suggest_args(user_id, tool)
            if args:
                save_suggested(user_id, server_id, tool["name"], args)
        except Exception:  # noqa: BLE001 - 单个工具失败不影响其余
            continue


def prefetch_suggestions_bg(user_id: str, server_id: str) -> None:
    """刷新连接是同步请求，逐工具问模型太慢，丢后台线程跑。"""
    import threading

    threading.Thread(
        target=prefetch_suggestions,
        args=(user_id, server_id),
        daemon=True,
        name="mcp-suggest-args",
    ).start()
