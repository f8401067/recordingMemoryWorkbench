from __future__ import annotations

import json
import re
from typing import Any

import httpx

from .llm_vendors import VENDORS, api_root, base_url_for, protocol_for

LLM_TIMEOUT = httpx.Timeout(300.0, connect=20.0)


def _params(provider: dict) -> dict:
    raw = provider.get("params")
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}


class LlmEngine:
    def chat(
        self,
        messages: list[dict[str, str]],
        json_mode: bool = False,
        temperature: float = 0.3,
        max_tokens: int | None = None,
    ) -> str:
        raise NotImplementedError


class OpenAiCompatibleLlm(LlmEngine):
    def __init__(self, provider: dict) -> None:
        self.p = provider

    def chat(self, messages, json_mode=False, temperature=0.3, max_tokens=None) -> str:
        api_key = (self.p.get("api_key") or "").strip()
        if not api_key:
            raise RuntimeError("缺少 API Key")

        cfg = _params(self.p)
        root = api_root(self.p.get("base_url"), self.p.get("kind"))
        body: dict[str, Any] = {
            "model": self.p.get("model") or "gpt-4o-mini",
            "messages": messages,
            "temperature": temperature if temperature is not None else cfg.get("temperature", 0.3),
            "stream": False,
        }
        limit = max_tokens or cfg.get("maxTokens")
        if limit:
            body["max_tokens"] = limit
        if json_mode and cfg.get("supportsJsonMode") is not False:
            body["response_format"] = {"type": "json_object"}

        with httpx.Client(timeout=LLM_TIMEOUT) as client:
            res = client.post(
                f"{root}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json=body,
            )
        if res.status_code >= 400:
            raise RuntimeError(f"LLM 请求失败 {res.status_code}：{res.text[:400]}")

        data = res.json()
        choices = data.get("choices") or []
        content = ((choices[0] if choices else {}).get("message") or {}).get("content")
        if not isinstance(content, str):
            raise RuntimeError("LLM 返回格式异常：缺少 choices[0].message.content")
        return content


class AnthropicLlm(LlmEngine):
    def __init__(self, provider: dict) -> None:
        self.p = provider

    def chat(self, messages, json_mode=False, temperature=0.3, max_tokens=None) -> str:
        api_key = (self.p.get("api_key") or "").strip()
        if not api_key:
            raise RuntimeError("缺少 API Key")

        base = (self.p.get("base_url") or base_url_for(self.p.get("kind")) or "https://api.anthropic.com").rstrip("/")
        system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
        rest = [{"role": m["role"], "content": m["content"]} for m in messages if m["role"] != "system"]

        payload: dict[str, Any] = {
            "model": self.p.get("model") or "claude-sonnet-4-20250514",
            "max_tokens": max_tokens or _params(self.p).get("maxTokens") or 4096,
            "temperature": temperature,
            "messages": rest,
        }
        if system:
            payload["system"] = system

        with httpx.Client(timeout=LLM_TIMEOUT) as client:
            res = client.post(
                f"{base}/v1/messages",
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
        if res.status_code >= 400:
            raise RuntimeError(f"LLM 请求失败 {res.status_code}：{res.text[:400]}")

        data = res.json()
        blocks = data.get("content") or []
        return "".join(b.get("text", "") for b in blocks if isinstance(b, dict) and b.get("type") == "text")


class MockLlm(LlmEngine):
    def chat(self, messages, json_mode=False, temperature=0.3, max_tokens=None) -> str:
        user_text = "\n".join(m["content"] for m in messages if m["role"] == "user")
        system_text = "\n".join(m["content"] for m in messages if m["role"] == "system")
        snippet = " ".join(user_text.split())[:120]
        if json_mode:
            # 演示引擎也把命中的风格/自定义提示词回显出来，便于确认整条链路接通
            m = re.search(r"【本次整理类型】\s*([^\n（(]+)", system_text)
            label = (m.group(1).strip() if m else "") or "内容纪要"
            custom = "【用户自定义要求】" in system_text
            return json.dumps(
                {
                    "title": f"演示纪要 · {label}",
                    "markdown": "\n".join([
                        "## 核心概要",
                        f"这是一份由演示引擎按「{label}」结构生成的示例纪要"
                        + ("，已应用你填写的自定义整理提示词。" if custom else "。")
                        + "配置真实的语言模型后，这里会基于录音转写内容自动总结。",
                        "",
                        "## 关键议题",
                        "- 上传与分片转写链路已验证可用",
                        "- 纪要生成与思维导图渲染链路已验证可用",
                        "- 完成服务商配置后即可切换到真实模型",
                        "",
                        "## 待办事项",
                        "- [ ] 在「设置 → AI 服务商」中填入真实 API Key",
                        "- [ ] 用一段真实录音做端到端验证",
                        "",
                        f"> 原始内容片段：{snippet}",
                    ]),
                    "mindmap": "\n".join([
                        "# 演示纪要",
                        "## 录音转写",
                        "### 分片上传",
                        "### 自动转写",
                        "## AI 整理",
                        "### 纪要生成",
                        "### 思维导图",
                        "## 后续动作",
                        "### 配置真实服务商",
                    ]),
                    "highlights": ["链路已打通", "等待真实服务商"],
                    "todos": [{"text": "配置真实服务商", "owner": "", "due": ""}],
                },
                ensure_ascii=False,
            )
        return f"演示回复：{snippet}"


def create_llm_engine(provider: dict) -> LlmEngine:
    proto = protocol_for(provider.get("kind"))
    if proto == "anthropic":
        return AnthropicLlm(provider)
    if proto == "mock":
        return MockLlm()
    if proto == "openai-compat":
        return OpenAiCompatibleLlm(provider)
    raise RuntimeError(f"未知的 LLM 引擎类型：{provider.get('kind')}")


def is_empty_reply_error(e: Exception) -> bool:
    """推理类模型（如 deepseek-reasoner）在 max_tokens 很小时可能只有思维链、没有正文。"""
    return "choices[0].message.content" in str(e)


# 前端「添加语言模型服务」直接渲染这份清单：选了服务商就带出地址与默认模型
LLM_KINDS = [
    {
        "kind": v["kind"],
        "label": v["label"],
        "short": v.get("short") or v["label"],
        "hint": v["hint"],
        "group": v.get("group", "通用"),
        "protocol": v["protocol"],
        "baseUrl": v.get("baseUrl") or "",
        "defaultModel": v.get("defaultModel") or "",
        "models": v.get("models") or [],
        "modelsApi": bool(v.get("modelsApi")),
        "keysUrl": v.get("keysUrl"),
        "requiresKey": v.get("requiresKey", True),
    }
    for v in VENDORS
]
