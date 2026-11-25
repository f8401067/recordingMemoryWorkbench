"""「找寻」页的工具推荐：让大模型从全部 MCP 工具里挑出本次真用得上的几个。

只影响界面展示，不收窄对话里实际可调用的工具集合。模型输出经过编号白名单、
去重和数量约束；模型不可用或输出非法时返回空列表，界面就不会把一堆无关
工具摆出来（宁可不推荐，也不列全量）。
"""

from __future__ import annotations

import json
import re
from typing import Any

from ..db import all_
from .providers import build_llm, resolve_llm
from .mcp_servers import chat_tools

MAX_IN_PROMPT = 24
MAX_PICK = 6

SYSTEM = """你是「忆录」找寻页的 MCP 工具推荐器。

【输入】用户的问题、本次回答，以及该用户配置好的全部 MCP 工具（带编号、名称、说明、参数）。
【任务】只挑出本次「确实用得上、而且现在就能正确调用」的工具，作为推荐展示给用户。

判断标准：
1. 与这次问题或回答的主题相关：能补充检索、能保存/查找这次结论、能把结果发到对应服务。
2. 参数能从当前上下文得出；必填参数需要用户额外提供 ID、账号、外部凭据的，不要推荐。
3. 同一服务里功能重复的只留最合适的一个；纯破坏性操作（删除、清空）不要推荐。
4. 宁缺毋滥，最多 6 个；一个都不合适就返回空列表。不要为了凑数把工具都列上。

只输出一个 JSON 对象，不要代码块、不要解释文字：
{"reason":"一句话说明整体判断","tools":[{"ref":"t1","reason":"为什么用得上"}]}
ref 必须来自给定清单，不要编造。"""


def _loads_loose(raw: str) -> dict | None:
    if not raw:
        return None
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return data
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


def _alternative_providers(user_id: str, primary_id: str) -> list[dict]:
    """默认引擎吐空字时的备胎；只影响这一处轻量推荐，不改主链路的引擎选择。"""
    try:
        return all_(
            """SELECT * FROM providers WHERE user_id = ? AND category = 'llm'
               AND enabled = 1 AND kind <> 'mock' AND id <> ?
               ORDER BY is_default DESC, created_at ASC LIMIT 2""",
            (user_id, primary_id),
        )
    except Exception:  # noqa: BLE001
        return []


def _prompt(question: str, answer: str, tools: list[dict]) -> str:
    lines = [
        f"【问题】{question[:300]}",
        f"【本次回答】{(answer or '（还没生成回答）')[:800]}",
        "",
        "【全部可用 MCP 工具】",
    ]
    for i, t in enumerate(tools[:MAX_IN_PROMPT], start=1):
        desc = str(t.get("description") or t.get("title") or "").strip()[:140]
        params = ", ".join(str(p) for p in (t.get("params") or []))[:160]
        lines.append(
            f"[t{i}] {t.get('serverName') or ''} · {t['name']}"
            + (f" — {desc}" if desc else "")
        )
        if params:
            lines.append(f"     参数: {params}")
    if len(tools) > MAX_IN_PROMPT:
        lines.append(f"（另有 {len(tools) - MAX_IN_PROMPT} 个工具未列出，不要选它们）")
    return "\n".join(lines)


def recommend_chat_tools(
    user_id: str,
    question: str,
    answer: str,
    tools: list[dict] | None = None,
    *,
    provider_id: str | None = None,
    provider: dict | None = None,
) -> dict:
    """返回 ``{"tools": [...], "by": "llm"|"mock"|"none", "reason", "note"}``。

    ``tools`` 传 run_find 已经读好的缓存清单，避免再查一次库。
    """
    empty: dict[str, Any] = {"tools": [], "by": "none", "reason": "", "note": ""}
    tools = chat_tools(user_id) if tools is None else tools
    if not tools:
        return {**empty, "note": "还没有开启可用于对话的 MCP 工具"}

    provider = provider or resolve_llm(user_id, provider_id)
    if str(provider.get("kind") or "") == "mock":
        return {**empty, "by": "mock", "note": "演示引擎无法判断工具，本次不推荐"}

    refs = {f"t{i}": t for i, t in enumerate(tools[:MAX_IN_PROMPT], start=1)}
    messages = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": _prompt(question, answer, tools)},
    ]
    try:
        switched = ""
        data: dict = {}
        engines = [provider, *_alternative_providers(user_id, str(provider.get("id") or ""))]
        for i, engine in enumerate(engines):
            try:
                raw = build_llm(engine).chat(
                    messages, json_mode=True, temperature=0.2, max_tokens=350
                )
                parsed = _loads_loose(raw)
            except Exception:  # noqa: BLE001 - 单台引擎失败换下一台
                continue
            # 只认「能解析出 tools 数组」的回复；空回复 / 纯文本 / 跑偏的 JSON 都换引擎
            if isinstance(parsed, dict) and isinstance(parsed.get("tools"), list):
                data = parsed
                if i:
                    switched = str(engine.get("name") or engine.get("model") or "备用引擎")
                break
        if not data:
            return {**empty, "note": "模型没有返回有效的工具判断，本次不推荐工具"}

        reason = str(data.get("reason") or "").strip()[:160]
        picks: list[dict] = []
        raw_items = data["tools"]
        for item in raw_items[: MAX_PICK * 2]:
            if not isinstance(item, dict):
                continue
            t = refs.get(str(item.get("ref") or "").strip())
            if not t or any(p["key"] == t["key"] for p in picks):
                continue
            picks.append(
                {
                    "key": t["key"],
                    "name": t["name"],
                    "title": t.get("title") or "",
                    "description": t.get("description") or "",
                    "params": t.get("params") or [],
                    "serverId": t["serverId"],
                    "serverName": t.get("serverName") or "",
                    "reason": str(item.get("reason") or "").strip()[:120],
                }
            )
            if len(picks) >= MAX_PICK:
                break
        note = f"默认引擎无响应，已改用「{switched}」判断" if switched else ""
        return {"tools": picks, "by": "llm", "reason": reason, "note": note}
    except Exception as exc:  # noqa: BLE001 - 推荐是尽力而为，不能拖垮找寻
        return {**empty, "note": f"工具推荐失败：{str(exc)[:120]}"}
