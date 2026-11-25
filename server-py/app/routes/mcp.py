from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query

from ..db import one
from ..security import current_user
from ..services.mcp_arg_suggest import get_suggested, suggest_args, validate_args
from ..services.mcp_dispatch import compose_summary_args, decide_for_summary, run_summary_tool
from ..services.mcp_servers import (
    call_server_tool,
    chat_tools,
    create_server,
    delete_server,
    find_server,
    find_summary_tool,
    import_config,
    list_logs,
    list_servers,
    list_tools,
    public_server,
    push_candidates,
    refresh_server,
    set_suggested_args,
    set_tool_capability,
    summary_tools,
    update_server,
)

router = APIRouter(prefix="/api/mcp", tags=["mcp"])


def _not_found() -> HTTPException:
    return HTTPException(404, "MCP 服务不存在")


@router.get("/servers")
def get_servers(user: dict = Depends(current_user)) -> list[dict]:
    return list_servers(user["id"])


@router.post("/servers", status_code=201)
def add_server(
    body: dict[str, Any] = Body(default_factory=dict),
    user: dict = Depends(current_user),
) -> dict:
    try:
        return create_server(user["id"], body)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/servers/import")
def import_servers(
    body: dict[str, Any] = Body(default_factory=dict),
    user: dict = Depends(current_user),
) -> dict:
    """粘贴 JSON 配置导入。兼容 Claude Desktop 的 mcpServers 写法。"""
    raw = body.get("json")
    if raw is None:
        raw = body.get("config")
    if not raw:
        raise HTTPException(400, "请粘贴 JSON 配置")
    try:
        result = import_config(user["id"], raw)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if not result["added"] and not result["skipped"]:
        raise HTTPException(400, "没有解析到任何 MCP 服务")
    return result


@router.get("/tools")
def available_tools(scope: str = "chat", user: dict = Depends(current_user)) -> dict:
    """工具清单。

    - ``scope=chat``：写进「找寻」提示词的工具（对话中可用）
    - ``scope=summary`` / ``push``：添加服务时识别出的「可发送 AI 纪要」的工具
    """
    if scope in ("summary", "push"):
        tools = [
            {
                "key": f"{t['serverId']}::{t['name']}",
                "name": t["name"],
                "title": t["title"],
                "description": t["description"],
                "params": t["params"],
                "serverId": t["serverId"],
                "serverName": t["serverName"],
                "bodyField": t["bodyField"],
                "titleField": t["titleField"],
                "sendSummary": True,
            }
            for t in summary_tools(user["id"])
        ]
    else:
        tools = [
            {
                "key": t["key"],
                "name": t["name"],
                "title": t["title"],
                "description": t["description"],
                "params": t["params"],
                "serverId": t["serverId"],
                "serverName": t["serverName"],
            }
            for t in chat_tools(user["id"])
        ]
    return {"count": len(tools), "tools": tools}


@router.post("/run")
def run_decision(
    body: dict[str, Any] = Body(default_factory=dict),
    user: dict = Depends(current_user),
) -> dict:
    """在 AI 纪要详情页手动发起：让大模型挑工具处理这份纪要。

    body: ``summaryId``（必填）、可选 ``providerId``。
    正文由服务端按 id 读取，避免前端把整篇 markdown 重传一遍。
    """
    summary_id = str(body.get("summaryId") or "").strip()
    if not summary_id:
        raise HTTPException(400, "缺少纪要 id")
    row = one(
        "SELECT * FROM summaries WHERE id = ? AND user_id = ?",
        (summary_id, user["id"]),
    )
    if not row:
        raise HTTPException(404, "纪要不存在")
    if row.get("status") != "ready":
        raise HTTPException(400, "这份纪要还没生成完成")

    out = decide_for_summary(
        user["id"],
        row,
        provider_id=str(body["providerId"]) if body.get("providerId") else None,
    )
    results = out["results"]
    return {
        "ok": bool(results) and all(r["ok"] for r in results),
        "results": results,
        "by": out.get("by"),
        "reason": out.get("reason") or "",
        "message": out.get("note") or ("" if results else "没有可调用的工具，或模型判断这次不需要发送"),
    }


@router.post("/run-tool")
def run_tool_for_summary(
    body: dict[str, Any] = Body(default_factory=dict),
    user: dict = Depends(current_user),
) -> dict:
    """AI 纪要下方的「一键发送」：用户点了哪个工具，就直接用哪个工具。

    body: ``summaryId`` / ``serverId`` / ``tool``（工具名），可选 ``args``。
    正文由服务端按 id 从 summaries 表读取；参数以工具的建议参数（大模型按
    schema 预生成）为底，``args`` 里用户改过的键覆盖建议值。
    """
    summary_id = str(body.get("summaryId") or "").strip()
    server_id = str(body.get("serverId") or "").strip()
    tool_name = str(body.get("tool") or body.get("toolName") or "").strip()
    if not summary_id or not server_id or not tool_name:
        raise HTTPException(400, "缺少纪要 id、服务 id 或工具名")

    row = one(
        "SELECT * FROM summaries WHERE id = ? AND user_id = ?",
        (summary_id, user["id"]),
    )
    if not row:
        raise HTTPException(404, "纪要不存在")
    if row.get("status") != "ready":
        raise HTTPException(400, "这份纪要还没生成完成")

    # 用户改过的键先过一遍 schema 校验，未声明的键直接丢
    tool = find_summary_tool(user["id"], server_id, tool_name)
    overrides = body.get("args")
    if isinstance(overrides, dict) and tool:
        overrides = validate_args(tool, overrides)

    out = run_summary_tool(user["id"], row, server_id, tool_name, overrides)
    results = out["results"]
    return {
        "ok": bool(results) and all(r["ok"] for r in results),
        "results": results,
        "by": out.get("by"),
        "message": out.get("note")
        or (
            "已发送"
            if results and all(r["ok"] for r in results)
            else (results[0].get("message") if results else "没有可调用的工具")
        ),
    }


@router.post("/run-tool-preview")
def preview_summary_args(
    body: dict[str, Any] = Body(default_factory=dict),
    user: dict = Depends(current_user),
) -> dict:
    """纪要详情页点工具后、发送前：给出这份纪要的参数预览（可编辑）。

    body: ``summaryId`` / ``serverId`` / ``tool``。不真正调用服务，只返回
    建议参数 + 纪要内容合并后的结果，用户改完再点发送。
    """
    summary_id = str(body.get("summaryId") or "").strip()
    server_id = str(body.get("serverId") or "").strip()
    tool_name = str(body.get("tool") or body.get("toolName") or "").strip()
    if not summary_id or not server_id or not tool_name:
        raise HTTPException(400, "缺少纪要 id、服务 id 或工具名")

    row = one(
        "SELECT * FROM summaries WHERE id = ? AND user_id = ?",
        (summary_id, user["id"]),
    )
    if not row:
        raise HTTPException(404, "纪要不存在")

    tool = find_summary_tool(user["id"], server_id, tool_name)
    if not tool:
        raise HTTPException(400, "这个工具不能接收纪要，或服务已被停用/删除")

    args = compose_summary_args(tool, row)
    return {
        "tool": tool_name,
        "serverId": server_id,
        "serverName": tool["serverName"],
        "args": args,
        "schema": tool.get("schema") or {},
        "bodyField": (tool.get("capability") or {}).get("bodyField") or "",
        "note": "参数由大模型按工具 schema 建议、可直接修改；正文已填入识别出的正文字段",
    }


@router.post("/servers/{server_id}/tools/{tool}/suggest-args")
def suggest_tool_args(
    server_id: str,
    tool: str,
    body: dict[str, Any] = Body(default_factory=dict),
    user: dict = Depends(current_user),
) -> dict:
    """调试面板 / 纪要发送：现场让大模型按工具 schema 重新生成一份建议参数。"""
    try:
        tools = list_tools(user["id"], server_id)
    except LookupError as exc:
        raise _not_found() from exc
    item = next((t for t in tools if t["name"] == tool), None)
    if not item:
        raise HTTPException(404, "工具不存在，请先刷新服务")
    full = {**item, "serverName": "", "serverId": server_id}
    srv = find_server(user["id"], server_id)
    if srv:
        full["serverName"] = srv.get("name") or ""
    args, note = suggest_args(user["id"], full, provider_id=str(body["providerId"]) if body.get("providerId") else None)
    return {"args": args, "note": note}


@router.patch("/servers/{server_id}/tools/{tool}/args")
def set_tool_args(
    server_id: str,
    tool: str,
    body: dict[str, Any] = Body(default_factory=dict),
    user: dict = Depends(current_user),
) -> dict:
    """把人工改过的参数存为这个工具的默认值（刷新服务也不会丢）。"""
    try:
        return set_suggested_args(user["id"], server_id, tool, body.get("args"))
    except LookupError as exc:
        raise _not_found() from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/servers/{server_id}/tools/{tool}/args")
def get_tool_args(server_id: str, tool: str, user: dict = Depends(current_user)) -> dict:
    """读库里存的建议参数（调试面板预填用）。"""
    if not find_server(user["id"], server_id):
        raise _not_found()
    return {"args": get_suggested(user["id"], server_id, tool)}


@router.get("/logs")
def call_logs(
    serverId: str | None = Query(None),
    limit: int = Query(20, ge=1, le=100),
    user: dict = Depends(current_user),
) -> list[dict]:
    return list_logs(user["id"], serverId, limit)


@router.get("/servers/{server_id}")
def get_server(server_id: str, user: dict = Depends(current_user)) -> dict:
    row = find_server(user["id"], server_id)
    if not row:
        raise _not_found()
    return public_server(row, with_tools=True)


@router.patch("/servers/{server_id}")
def patch_server(
    server_id: str,
    body: dict[str, Any] = Body(default_factory=dict),
    user: dict = Depends(current_user),
) -> dict:
    try:
        return update_server(user["id"], server_id, body)
    except LookupError as exc:
        raise _not_found() from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.delete("/servers/{server_id}")
def remove_server(server_id: str, user: dict = Depends(current_user)) -> dict:
    delete_server(user["id"], server_id)
    return {"ok": True}


@router.post("/servers/{server_id}/refresh")
def refresh(server_id: str, user: dict = Depends(current_user)) -> dict:
    """重新连接并刷新工具清单，返回连通性详情。"""
    data = refresh_server(user["id"], server_id)
    if data is None:
        raise _not_found()
    return data


@router.get("/servers/{server_id}/tools")
def tools(server_id: str, user: dict = Depends(current_user)) -> dict:
    try:
        items = list_tools(user["id"], server_id)
    except LookupError as exc:
        raise _not_found() from exc
    return {"count": len(items), "tools": items}


@router.patch("/servers/{server_id}/tools/{tool}/capability")
def set_capability_route(
    server_id: str,
    tool: str,
    body: dict[str, Any] = Body(default_factory=dict),
    user: dict = Depends(current_user),
) -> dict:
    """手动指定 / 取消「可发送纪要」，或恢复自动判定。

    人工决策写进 capability.manual，刷新服务与判定规则升级都不会覆盖。
    """
    try:
        return set_tool_capability(user["id"], server_id, tool, body)
    except LookupError as exc:
        raise _not_found() from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/servers/{server_id}/tools/{tool}/call")
def call_tool_route(
    server_id: str,
    tool: str,
    body: dict[str, Any] = Body(default_factory=dict),
    user: dict = Depends(current_user),
) -> dict:
    """调试面板：拿参数直接调一次工具，结果原样返回。"""
    args = body.get("args")
    if isinstance(args, str):
        import json

        try:
            args = json.loads(args) if args.strip() else {}
        except json.JSONDecodeError as exc:
            raise HTTPException(400, f"参数不是合法 JSON：{exc}") from exc
    if args is not None and not isinstance(args, dict):
        raise HTTPException(400, "args 必须是 JSON 对象")
    try:
        return call_server_tool(user["id"], server_id, tool, args or {}, source="debug")
    except LookupError as exc:
        raise _not_found() from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
