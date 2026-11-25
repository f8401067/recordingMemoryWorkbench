"""由大模型决定「这次总结要不要发、发给哪台 MCP、调哪个工具、填什么参数」。

用户只配置服务（地址、鉴权、自动/手动、可选的一句自然语言偏好），
不再手填工具名和参数模板。调度器把工具清单与 schema 交给模型，
对模型输出做白名单 + 类型校验后才真正调用；模型不可用或输出非法时
退回确定性兜底（挑一个能吃文本的工具，把总结填进必填字段）。

纪要一键发送 / 参数预览走另一条路：工具的「建议参数」由
``mcp_arg_suggest`` 按 schema 预生成并存库，这里只把建议参数与纪要
内容合并，用户改过的参数优先。
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any

from ..services.providers import build_llm, resolve_llm
from .mcp_arg_suggest import validate_args
from .mcp_servers import call_server_tool, find_summary_tool, push_candidates

MAX_CALLS = 3
MAX_TOOLS_IN_PROMPT = 16

SYSTEM = """你是「忆录」的 MCP 工具调度器。根据一份内容，决定是否把它交给外部服务，以及用哪个工具。

【输入】会给出内容正文、相关背景，以及所有可用的 MCP 工具（含参数 schema）。
【本次场景】{scenario}
【前提】用户已经把这些服务配置成「接收内容」；因此只要内容有实质信息，默认就应该发送。
仅在「内容为空/纯客套/没有信息量」或「服务备注明确排除这类内容」时才 send=false。

规则：
1. 只从给定清单里选工具，用它的 ref（如 s1.t2），不要编造服务或工具。
2. args 必须严格符合该工具的 schema：键名、类型对齐；required 字段不能缺；不要塞无关字段。
3. 正文放在工具里最像「文本/内容」的字段（text / content / body / note / message…）。
   标题、标签、日期等字段按语义填，不要把所有字段都塞同一段话。
   不要选纯查询/读取类工具（list/get/search），它们没有地方接收文本。
   正文较长时，在正文字段填 "{{FULL_TEXT}}"，系统会自动替换成完整内容；也可以自己提炼后填入。
4. 需要发给多个服务时，每个服务最多选一个工具；总共不超过 3 条。
5. 服务备注（如果给了）优先级高于你的默认判断。
6. 只输出一个 JSON 对象，不要 Markdown 代码块、不要解释文字：
   {"send": true, "reason": "一句话说明", "calls": [{"ref": "s1.t1", "args": {...}, "reason": "..."}]}
   不发送时输出 {"send": false, "reason": "...", "calls": []}"""

SAVE_WORDS = ("save", "add", "create", "write", "note", "append", "record", "push", "send", "insert", "post")
TEXT_WORDS = ("text", "content", "body", "message", "note", "summary", "markdown", "value", "description")


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


def _compact_schema(schema: Any) -> dict:
    """只把模型需要的部分带进提示词，避免 description/嵌套 schema 把窗口吃光。"""
    data = schema if isinstance(schema, dict) else {}
    props = data.get("properties") or {}
    out: dict[str, Any] = {}
    for key, spec in list(props.items())[:12]:
        if not isinstance(spec, dict):
            continue
        item: dict[str, Any] = {"type": spec.get("type") or "string"}
        desc = str(spec.get("description") or "").strip()[:80]
        if desc:
            item["description"] = desc
        if isinstance(spec.get("enum"), list):
            item["enum"] = spec["enum"][:6]
        out[str(key)] = item
    return {"properties": out, "required": [str(k) for k in (data.get("required") or [])]}


def _build_prompt(
    question: str,
    answer: str,
    matches: str,
    tools: list[dict],
    values: dict,
    scenario: str,
    label: str = "AI 总结",
) -> str:
    lines = [
        f"【任务】{scenario}",
        "",
        "【内容】",
        f"标题 / 问题：{question[:300]}",
        f"{label}（共 {len(answer)} 字）：{answer[:1500]}",
        f"依据录音：{matches[:300] or '（无）'}",
        f"日期：{values['date']}",
        '',
        '【提示】需要完整正文时，在正文字段填 "{{FULL_TEXT}}"，系统会自动替换。',
        "",
        "【可用 MCP 工具】",
    ]
    by_server: dict[str, str] = {}
    for t in tools[:MAX_TOOLS_IN_PROMPT]:
        by_server.setdefault(t["serverId"], t.get("serverHint") or "")
        desc = str(t.get("description") or t.get("title") or "").strip()[:120]
        lines.append(
            f"[{t['ref']}] 服务「{t['serverName']}」· {t['name']}"
            + (f" — {desc}" if desc else "")
        )
        lines.append("      schema: " + json.dumps(_compact_schema(t.get("schema")), ensure_ascii=False))
    if len(tools) > MAX_TOOLS_IN_PROMPT:
        lines.append(f"（另有 {len(tools) - MAX_TOOLS_IN_PROMPT} 个工具未列出，不要选它们）")
    for i, (_, hint) in enumerate(by_server.items(), start=1):
        if hint:
            lines.append(f"服务备注（s{i}）：{hint[:200]}")
    return "\n".join(lines)


def _coerce(value: Any, typ: str) -> Any:
    if typ == "string":
        if isinstance(value, (dict, list)):
            return json.dumps(value, ensure_ascii=False)[:4000]
        return str(value)[:4000]
    if typ in ("integer", "number"):
        try:
            n = float(value)
            return int(n) if typ == "integer" else n
        except (TypeError, ValueError):
            return 0
    if typ == "boolean":
        if isinstance(value, str):
            return value.strip().lower() in ("true", "1", "yes", "on")
        return bool(value)
    if typ == "array":
        return value if isinstance(value, list) else []
    if typ == "object":
        return value if isinstance(value, dict) else {}
    return value


def _text_slots(tool: dict) -> list[str]:
    """工具里能装下一段文字的字段，按「像不像正文」排序。"""
    schema = tool.get("schema") if isinstance(tool.get("schema"), dict) else {}
    props = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
    slots = [
        str(k)
        for k, spec in props.items()
        if isinstance(spec, dict) and str(spec.get("type") or "string") == "string"
    ]
    slots.sort(key=lambda k: (0 if any(w in k.lower() for w in TEXT_WORDS) else 1, k))
    return slots


def _normalize_args(tool: dict, args: Any, values: dict) -> dict:
    """白名单 + 类型转换 + 必填补全。模型乱填的字段直接丢掉。"""
    schema = tool.get("schema") if isinstance(tool.get("schema"), dict) else {}
    props = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
    required = [str(k) for k in (schema.get("required") or [])]
    raw = args if isinstance(args, dict) else {}

    out: dict[str, Any] = {}
    if props:
        for key, spec in props.items():
            if key not in raw:
                continue
            typ = str((spec or {}).get("type") or "string") if isinstance(spec, dict) else "string"
            out[str(key)] = _coerce(raw[key], typ)
    else:
        # 没声明 schema 的工具：原样透传，但限键数与长度
        for k, v in list(raw.items())[:8]:
            out[str(k)] = v if not isinstance(v, str) else v[:4000]

    for key in required:
        if key in out:
            continue
        spec = props.get(key) if isinstance(props.get(key), dict) else {}
        typ = str(spec.get("type") or "string")
        low = key.lower()
        if typ == "string":
            if "question" in low or "query" in low:
                out[key] = values["question"]
            elif "date" in low or "time" in low:
                out[key] = values["date"]
            elif "match" in low or "source" in low or "reference" in low:
                out[key] = values["matches"]
            elif "title" in low or "name" in low or "subject" in low:
                out[key] = values["title"]
            else:
                out[key] = values["answer"]
        else:
            out[key] = _coerce(None, typ)

    # 模型漏填正文、或工具没有 required 字段时，保证内容有地方落
    if not any(isinstance(v, str) and len(v) > 20 for v in out.values()):
        slots = _text_slots(tool)
        if slots:
            out.setdefault(slots[0], values["answer"])

    # 模型要求传完整正文时，服务端替它展开，避免长纪要被提示词截断
    for key, val in list(out.items()):
        if isinstance(val, str) and "{{FULL_TEXT}}" in val:
            out[key] = val.replace("{{FULL_TEXT}}", values["answer"])[:8000]
    return out


def _fallback(tools: list[dict], values: dict) -> list[dict]:
    """模型不可用时：每个服务挑一个最像「保存文本」的工具，把总结填进必填字段。"""
    by_server: dict[str, list[dict]] = {}
    for t in tools:
        by_server.setdefault(t["serverId"], []).append(t)

    calls: list[dict] = []
    for group in by_server.values():
        group = [t for t in group if _text_slots(t)]
        if not group:
            continue

        def score(t: dict) -> tuple[int, int]:
            schema = t.get("schema") if isinstance(t.get("schema"), dict) else {}
            props = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
            required = [str(k) for k in (schema.get("required") or [])]
            has_text = any(
                "string" in str((props.get(k) or {}).get("type") or "string")
                and any(w in k.lower() for w in TEXT_WORDS)
                for k in required
            )
            name = (t["name"] + " " + (t.get("description") or "")).lower()
            return (1 if any(w in name for w in SAVE_WORDS) else 0, 1 if has_text else 0)

        best = sorted(group, key=score, reverse=True)[0]
        schema = best.get("schema") if isinstance(best.get("schema"), dict) else {}
        required = [str(k) for k in (schema.get("required") or [])]
        # 连一个必填文本字段都没有的工具，别硬塞内容
        props = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
        if required and not any(
            str((props.get(k) or {}).get("type") or "string") == "string" for k in required
        ):
            continue
        calls.append(
            {
                "tool": best,
                "args": _normalize_args(best, {}, values),
                "reason": "兜底：按工具必填字段自动填充",
            }
        )
        if len(calls) >= MAX_CALLS:
            break
    return calls


def _decide_and_call(
    user_id: str,
    question: str,
    answer: str,
    *,
    values: dict,
    servers: list[dict],
    tools: list[dict],
    provider_id: str | None,
    scenario: str,
    label: str = "AI 总结",
    source: str = "push",
) -> dict:
    """决策核心：编号 → 让模型选工具 → 校验 → 执行 → 汇总结果。"""
    empty = {"results": [], "by": "none", "reason": "", "note": ""}
    text = str(answer or "").strip()

    # 给工具编号：模型只需输出短 ref，不用抄一长串 serverId
    index = {s["id"]: i + 1 for i, s in enumerate(servers)}
    counter: dict[str, int] = {}
    for t in tools:
        sid = t["serverId"]
        counter[sid] = counter.get(sid, 0) + 1
        t["ref"] = f"s{index[sid]}.t{counter[sid]}"
    ref_map = {t["ref"]: t for t in tools[:MAX_TOOLS_IN_PROMPT]}

    by = "llm"
    reason = ""
    note = ""
    calls: list[dict] = []
    provider = resolve_llm(user_id, provider_id)

    if str(provider.get("kind") or "") == "mock":
        by, reason = "fallback", "演示引擎无法决策"
        calls = _fallback(tools, values)
        note = "演示引擎无法判断工具，已按工具参数自动发送"
    else:
        try:
            raw = build_llm(provider).chat(
                [
                    {"role": "system", "content": SYSTEM.replace("{scenario}", scenario)},
                    {"role": "user", "content": _build_prompt(question, text, values["matches"], tools, values, scenario, label)},
                ],
                json_mode=True,
                temperature=0.2,
                max_tokens=700,
            )
            decision = _loads_loose(raw) or {}
            reason = str(decision.get("reason") or "").strip()[:200]
            if decision.get("send") is False:
                return {**empty, "by": "llm", "reason": reason, "note": f"模型判断这次不发送：{reason or '内容不需要外发'}"}
            for item in (decision.get("calls") or [])[:MAX_CALLS]:
                if not isinstance(item, dict):
                    continue
                tool = ref_map.get(str(item.get("ref") or ""))
                if not tool or not _text_slots(tool):
                    continue
                calls.append(
                    {
                        "tool": tool,
                        "args": _normalize_args(tool, item.get("args"), values),
                        "reason": str(item.get("reason") or "")[:160],
                    }
                )
            if not calls:
                raise ValueError("模型没有给出有效的工具调用")
        except Exception as exc:  # noqa: BLE001 - 决策失败走兜底，不让推送拖垮对话
            by, reason = "fallback", f"模型决策失败：{str(exc)[:120]}"
            calls = _fallback(tools, values)
            note = "工具改由服务端兜底选择（模型未能决策）"

    results: list[dict] = []
    for call in calls[:MAX_CALLS]:
        tool, args = call["tool"], call["args"]
        res = call_server_tool(user_id, tool["serverId"], tool["name"], args, source=source)
        ok = bool(res.get("ok")) and not res.get("isError")
        results.append(
            {
                "serverId": tool["serverId"],
                "serverName": tool["serverName"],
                "tool": tool["name"],
                "ok": ok,
                "message": str(res.get("text") or res.get("error") or "")[:400],
                "durationMs": int(res.get("latencyMs") or 0),
                "reason": call["reason"],
                "by": by,
            }
        )
    return {"results": results, "by": by, "reason": reason, "note": note}


def decide_and_push(
    user_id: str,
    question: str,
    answer: str,
    *,
    mode: str = "manual",
    server_ids: list[str] | None = None,
    extra: dict | None = None,
    provider_id: str | None = None,
) -> dict:
    """「找寻」得到的总结：按服务的自动/手动模式推送。"""
    empty = {"results": [], "by": "none", "reason": "", "note": ""}
    text = str(answer or "").strip()
    if not text:
        return empty

    servers, tools = push_candidates(user_id, mode, server_ids)
    if not servers:
        return empty
    if not tools:
        return {**empty, "note": "这些服务还没有工具清单，请先在设置里刷新连接"}

    values = {
        "answer": text,
        "question": str(question or ""),
        "date": datetime.now().strftime("%Y-%m-%d"),
        "title": str((extra or {}).get("title") or ""),
        "matches": str((extra or {}).get("matches") or ""),
    }
    return _decide_and_call(
        user_id,
        question,
        text,
        values=values,
        servers=servers,
        tools=tools,
        provider_id=provider_id,
        scenario="这是一次「找寻」对话得到的总结，用户已把这些服务设为接收总结。",
    )


def decide_for_summary(
    user_id: str,
    summary: dict,
    *,
    provider_id: str | None = None,
) -> dict:
    """AI 纪要详情页：用户主动发起，让模型挑工具处理整份纪要。"""
    empty = {"results": [], "by": "none", "reason": "", "note": ""}
    text = str(summary.get("markdown") or "").strip()
    if not text:
        return {**empty, "note": "这份纪要还没有正文"}
    title = str(summary.get("title") or "").strip() or "未命名纪要"

    # 详情页是显式触发，所有开启了「接收内容」的服务都参与，不再区分 auto/manual
    servers, tools = push_candidates(user_id)
    if not servers:
        return {**empty, "note": "还没有开启「接收内容」的 MCP 服务，可在设置 → MCP 工具里打开推送"}
    if not tools:
        return {**empty, "note": "这些服务还没有工具清单，请先在设置里刷新连接"}

    values = {
        "answer": text,
        "question": title,
        "date": str(summary.get("date_from") or datetime.now().strftime("%Y-%m-%d")),
        "title": title,
        "matches": "",
    }
    return _decide_and_call(
        user_id,
        title,
        text,
        values=values,
        servers=servers,
        tools=tools,
        provider_id=provider_id,
        scenario="用户在一份 AI 纪要详情页触发了「让 AI 决定」，请把这份纪要交给合适的 MCP 工具。",
        label="纪要正文",
        source="summary",
    )


def _semantic_fixup(key: str, val: Any, values: dict) -> Any:
    """标题 / 日期类字段按语义补纪要信息，避免同一大段正文重复出现。"""
    if not isinstance(val, str):
        return val
    low = key.lower()
    if any(w in low for w in ("title", "subject", "name", "heading")):
        return values["title"][:200]
    if any(w in low for w in ("date", "day", "time")):
        return values["date"]
    if len(val) > 120:
        return ""
    return val


def compose_summary_args(tool: dict, summary: dict, overrides: Any = None) -> dict:
    """把「建议参数 + 纪要内容 + 用户修改」合并成最终调用参数。

    优先级：用户手动改的键 > 建议参数（大模型按 schema 预生成）> 按纪要语义补全。
    纪要正文只进识别出的正文字段，不重复塞进标题 / 日期字段。
    """
    cap = tool.get("capability") if isinstance(tool.get("capability"), dict) else {}
    body = str(cap.get("bodyField") or "")
    title_field = str(cap.get("titleField") or "")
    date_field = str(cap.get("dateField") or "")

    text = str(summary.get("markdown") or "").strip()
    title = str(summary.get("title") or "").strip() or "未命名纪要"
    values = {
        "answer": text,
        "question": title,
        "date": str(summary.get("date_from") or datetime.now().strftime("%Y-%m-%d")),
        "title": title,
        "matches": "",
    }

    # 底：建议参数；没有就用规则兜底（_normalize_args 必填补全）
    suggested = tool.get("suggestedArgs")
    if not isinstance(suggested, dict):
        suggested = {}
    args = _normalize_args(tool, suggested, values)

    # 用户手动改的键优先（已按 schema 校验过类型），先于语义补全应用，
    # 否则用户改的 title 会被 _semantic_fixup 覆盖回纪要标题
    if isinstance(overrides, dict):
        schema = tool.get("schema") if isinstance(tool.get("schema"), dict) else {}
        props = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
        for key, val in overrides.items():
            if props and str(key) not in props:
                continue
            spec = props.get(key) if isinstance(props.get(key), dict) else {}
            args[str(key)] = _coerce(val, str(spec.get("type") or "string"))

    # 纪要内容按语义落到正文字段
    if body:
        args[body] = text[:8000]
    elif not any(isinstance(v, str) and len(v) > 20 for v in args.values()):
        slots = _text_slots(tool)
        if slots:
            args.setdefault(slots[0], text[:8000])

    for key in list(args):
        if key == body:
            continue
        # 用户显式改过的键不再被语义补全覆盖
        if isinstance(overrides, dict) and key in overrides:
            continue
        args[key] = _semantic_fixup(key, args[key], values)
    if title_field and title_field != body and not (isinstance(overrides, dict) and title_field in overrides):
        args[title_field] = title[:200]
    if date_field and date_field != body and not (isinstance(overrides, dict) and date_field in overrides):
        args[date_field] = values["date"]

    schema = tool.get("schema") if isinstance(tool.get("schema"), dict) else {}
    props = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
    if props:
        args = {k: v for k, v in args.items() if k in props}
    return args


def run_summary_tool(
    user_id: str,
    summary: dict,
    server_id: str,
    tool_name: str,
    overrides: Any = None,
) -> dict:
    """纪要详情页的一键按钮：用户点了哪个工具，就直接用哪个工具。

    参数 = 工具的建议参数（大模型按 schema 预生成、存库）+ 纪要内容，
    用户在预览里改过的键（overrides）优先。不额外等一次大模型决策。
    """
    empty = {"results": [], "by": "none", "reason": "", "note": ""}
    text = str(summary.get("markdown") or "").strip()
    if not text:
        return {**empty, "note": "这份纪要还没有正文"}

    tool = find_summary_tool(user_id, server_id, tool_name)
    if not tool:
        return {**empty, "note": "这个工具不能接收纪要，或服务已被停用/删除"}

    args = compose_summary_args(tool, summary, overrides)
    res = call_server_tool(user_id, server_id, tool_name, args, source="summary")
    ok = bool(res.get("ok")) and not res.get("isError")
    return {
        "results": [
            {
                "serverId": server_id,
                "serverName": tool["serverName"],
                "tool": tool_name,
                "ok": ok,
                "message": str(res.get("text") or res.get("error") or "")[:400],
                "durationMs": int(res.get("latencyMs") or 0),
                "reason": "按工具建议参数 + 纪要正文自动填充",
                "by": "direct",
            }
        ],
        "by": "direct",
        "reason": "",
        "note": "",
    }
