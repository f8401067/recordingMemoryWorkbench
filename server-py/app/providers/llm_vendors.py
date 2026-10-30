"""语言模型服务商预设。

这里只放「怎么连」的元信息（地址、默认模型、协议），不放任何密钥。
前端的「添加语言模型服务」直接读这份清单，选了服务商就自动带出 Base URL 与默认模型，
再点「自动获取」就能从各家 `/models` 接口把真实模型名拉下来。
"""

from __future__ import annotations

from urllib.parse import urlparse

import httpx

MODELS_TIMEOUT = httpx.Timeout(20.0, connect=8.0)

# group 只影响设置页的分组顺序；protocol 决定用哪套请求格式，openai-compat 覆盖绝大多数国产服务商
VENDORS: list[dict] = [
    # ---------------- 国内 ----------------
    {
        "kind": "dashscope",
        "label": "阿里百炼 · 通义千问",
        "short": "通义千问",
        "group": "国内",
        "protocol": "openai-compat",
        "hint": "DashScope 兼容模式，Qwen 全系都能用，中文纪要稳。",
        "baseUrl": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "defaultModel": "qwen3.7-plus",
        "models": ["qwen3.7-max", "qwen3.7-plus", "qwen3.6-flash", "qwen-long"],
        "modelsApi": True,
        "keysUrl": "https://bailian.console.aliyun.com/?apiKey=1",
    },
    {
        "kind": "deepseek",
        "label": "DeepSeek 深度求索",
        "short": "DeepSeek",
        "group": "国内",
        "protocol": "openai-compat",
        "hint": "便宜、中文好，长录音总结性价比最高。",
        "baseUrl": "https://api.deepseek.com",
        "defaultModel": "deepseek-v4-flash",
        "models": ["deepseek-v4-flash", "deepseek-v4-pro"],
        "modelsApi": True,
        "keysUrl": "https://platform.deepseek.com/api_keys",
    },
    {
        "kind": "zhipu",
        "label": "智谱 · GLM",
        "short": "智谱 GLM",
        "group": "国内",
        "protocol": "openai-compat",
        "hint": "GLM 系列，结构化输出稳定，适合纪要 JSON。",
        "baseUrl": "https://open.bigmodel.cn/api/paas/v4",
        "defaultModel": "glm-5",
        "models": ["glm-5", "glm-4.6", "glm-4.5-air", "glm-4-flash"],
        "modelsApi": True,
        "keysUrl": "https://bigmodel.cn/usercenter/apikeys",
    },
    {
        "kind": "moonshot",
        "label": "月之暗面 · Kimi",
        "short": "Kimi",
        "group": "国内",
        "protocol": "openai-compat",
        "hint": "超长上下文，几小时会议记录塞得进去。",
        "baseUrl": "https://api.moonshot.cn/v1",
        "defaultModel": "kimi-k2-turbo-preview",
        "models": ["kimi-k2-turbo-preview", "moonshot-v1-128k", "moonshot-v1-32k", "moonshot-v1-8k"],
        "modelsApi": True,
        "keysUrl": "https://platform.moonshot.cn/console/api-keys",
    },
    {
        "kind": "siliconflow",
        "label": "硅基流动 SiliconFlow",
        "short": "硅基流动",
        "group": "国内",
        "protocol": "openai-compat",
        "hint": "一个 Key 接 200+ 模型，DeepSeek / Qwen 都有，方便比价。",
        "baseUrl": "https://api.siliconflow.cn/v1",
        "defaultModel": "deepseek-ai/DeepSeek-V3",
        "models": ["deepseek-ai/DeepSeek-V3", "Qwen/Qwen3-8B", "Qwen/Qwen2.5-72B-Instruct"],
        "modelsApi": True,
        "keysUrl": "https://cloud.siliconflow.cn/account/ak",
    },
    {
        "kind": "ark",
        "label": "字节 · 火山方舟（豆包）",
        "short": "豆包",
        "group": "国内",
        "protocol": "openai-compat",
        "hint": "注意：多数情况模型名要填方舟的推理接入点 ID（ep- 开头）。",
        "baseUrl": "https://ark.cn-beijing.volces.com/api/v3",
        "defaultModel": "doubao-2.0-pro-256k",
        "models": ["doubao-2.0-pro-256k", "doubao-seed-1-6-250615"],
        "modelsApi": False,
        "keysUrl": "https://console.volcengine.com/ark",
    },
    {
        "kind": "qianfan",
        "label": "百度智能云 · 千帆（文心）",
        "short": "文心一言",
        "group": "国内",
        "protocol": "openai-compat",
        "hint": "千帆 v2 接口，用「API Key」而不是老的 AK/SK。",
        "baseUrl": "https://qianfan.baidubce.com/v2",
        "defaultModel": "ernie-4.5-turbo-128k",
        "models": ["ernie-4.5-turbo-128k", "ernie-speed-128k", "ernie-lite-8k"],
        "modelsApi": False,
        "keysUrl": "https://console.bce.baidu.com/iam/#/iam/apikey/list",
    },
    {
        "kind": "hunyuan",
        "label": "腾讯 · 混元",
        "short": "混元",
        "group": "国内",
        "protocol": "openai-compat",
        "hint": "混元 OpenAI 兼容接口，Key 在腾讯云控制台新建。",
        "baseUrl": "https://api.hunyuan.cloud.tencent.com/v1",
        "defaultModel": "hunyuan-turbos-latest",
        "models": ["hunyuan-turbos-latest", "hunyuan-plus-latest", "hunyuan-lite"],
        "modelsApi": False,
        "keysUrl": "https://console.cloud.tencent.com/hunyuan/api-key",
    },
    {
        "kind": "spark",
        "label": "科大讯飞 · 星火",
        "short": "星火",
        "group": "国内",
        "protocol": "openai-compat",
        "hint": "HTTP 接口的 Key 是控制台里的「API 密码」，不是 AppID。",
        "baseUrl": "https://spark-api-open.xf-yun.com/v1",
        "defaultModel": "4.0Ultra",
        "models": ["4.0Ultra", "generalv3.5", "max-32k", "lite"],
        "modelsApi": False,
        "keysUrl": "https://console.xfyun.cn/services/bmx1",
    },
    {
        "kind": "minimax",
        "label": "MiniMax 稀宇科技",
        "short": "MiniMax",
        "group": "国内",
        "protocol": "openai-compat",
        "hint": "国内站 api.minimaxi.com；海外账号改成 api.minimax.io。",
        "baseUrl": "https://api.minimaxi.com/v1",
        "defaultModel": "MiniMax-M2.7",
        "models": ["MiniMax-M2.7", "MiniMax-M2.5", "MiniMax-M2"],
        "modelsApi": True,
        "keysUrl": "https://platform.minimaxi.com/user-center/basic-information/interface-key",
    },
    {
        "kind": "stepfun",
        "label": "阶跃星辰 StepFun",
        "short": "阶跃",
        "group": "国内",
        "protocol": "openai-compat",
        "hint": "Step 系列，多模态见长，纯文本也能用。",
        "baseUrl": "https://api.stepfun.com/v1",
        "defaultModel": "step-2-16k",
        "models": ["step-2-16k", "step-1-32k", "step-1-8k"],
        "modelsApi": False,
        "keysUrl": "https://platform.stepfun.com/interface-key",
    },
    # ---------------- 海外 ----------------
    {
        "kind": "openai",
        "label": "OpenAI",
        "short": "OpenAI",
        "group": "海外",
        "protocol": "openai-compat",
        "hint": "官方接口，模型名可用「自动获取」刷新。",
        "baseUrl": "https://api.openai.com/v1",
        "defaultModel": "gpt-4o-mini",
        "models": ["gpt-4o-mini", "gpt-4o"],
        "modelsApi": True,
        "keysUrl": "https://platform.openai.com/api-keys",
    },
    {
        "kind": "anthropic",
        "label": "Anthropic Claude",
        "short": "Claude",
        "group": "海外",
        "protocol": "anthropic",
        "hint": "官方 /v1/messages 协议，长文总结质量高。",
        "baseUrl": "https://api.anthropic.com",
        "defaultModel": "claude-sonnet-4-20250514",
        "models": ["claude-sonnet-4-20250514", "claude-opus-4-20250514"],
        "modelsApi": True,
        "keysUrl": "https://console.anthropic.com/settings/keys",
    },
    {
        "kind": "openrouter",
        "label": "OpenRouter（聚合）",
        "short": "OpenRouter",
        "group": "海外",
        "protocol": "openai-compat",
        "hint": "一个 Key 调各家模型，模型名带前缀，如 openai/gpt-4o-mini。",
        "baseUrl": "https://openrouter.ai/api/v1",
        "defaultModel": "openai/gpt-4o-mini",
        "models": ["openai/gpt-4o-mini", "anthropic/claude-sonnet-4"],
        "modelsApi": True,
        "keysUrl": "https://openrouter.ai/keys",
    },
    # ---------------- 通用 ----------------
    {
        "kind": "openai-compat",
        "label": "自定义（OpenAI 兼容）",
        "short": "OpenAI 兼容",
        "group": "通用",
        "protocol": "openai-compat",
        "hint": "中转站、vLLM、Ollama、LM Studio 都填这里，地址填到 /v1 为止。",
        "baseUrl": "",
        "defaultModel": "",
        "models": [],
        "modelsApi": True,
        "requiresKey": False,
    },
    {
        "kind": "mock",
        "label": "演示引擎（模拟数据）",
        "short": "演示引擎",
        "group": "通用",
        "protocol": "mock",
        "hint": "不调用外部服务，用于验证整体流程。",
        "baseUrl": "",
        "defaultModel": "mock",
        "models": [],
        "modelsApi": False,
    },
]

VENDOR_BY_KIND: dict[str, dict] = {v["kind"]: v for v in VENDORS}


def vendor_for(kind: str | None) -> dict | None:
    return VENDOR_BY_KIND.get(kind or "")


def protocol_for(kind: str | None) -> str:
    v = vendor_for(kind)
    if v:
        return v["protocol"]
    if kind in ("anthropic", "mock", "openai-compat"):
        return kind  # 老数据直接存的协议名
    return "openai-compat"


def base_url_for(kind: str | None) -> str | None:
    v = vendor_for(kind)
    return (v.get("baseUrl") or None) if v else None


def api_root(base_url: str | None, kind: str | None = None) -> str:
    """把用户填的地址补成 API 根路径。

    只有裸域名才补 `/v1`；`/compatible-mode/v1`、`/api/paas/v4`、`/api/v3`
    这类已经带版本段的原样保留（老版本一律追加 /v1，会把火山、智谱的地址拼坏）。
    """
    proto = protocol_for(kind)
    fallback = base_url_for(kind) or "https://api.openai.com/v1"
    base = (base_url or fallback).strip().rstrip("/")
    if proto == "anthropic":
        return base
    if not urlparse(base).path.strip("/"):
        return f"{base}/v1"
    return base


def chat_endpoint(base_url: str | None, kind: str | None) -> str:
    root = api_root(base_url, kind)
    return f"{root}/v1/messages" if protocol_for(kind) == "anthropic" else f"{root}/chat/completions"


def fetch_models(kind: str | None, base_url: str | None, api_key: str | None) -> list[str]:
    """调服务商的 /models 拉真实模型名。失败就抛 RuntimeError，文案直接给用户看。"""
    key = (api_key or "").strip()
    if protocol_for(kind) == "mock":
        return []
    if not key:
        raise RuntimeError("请先填写 API Key，再自动获取模型")

    proto = protocol_for(kind)
    root = api_root(base_url, kind)
    try:
        with httpx.Client(timeout=MODELS_TIMEOUT) as client:
            if proto == "anthropic":
                res = client.get(
                    f"{root}/v1/models?limit=100",
                    headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
                )
            else:
                res = client.get(f"{root}/models", headers={"Authorization": f"Bearer {key}"})
    except httpx.HTTPError as e:
        raise RuntimeError(f"网络不可达（{e.__class__.__name__}）：请检查 Base URL") from e

    if res.status_code in (401, 403):
        raise RuntimeError("Key 校验失败：请确认 Key 有效、且已开通该服务")
    if res.status_code == 404:
        raise RuntimeError("该服务商不支持自动拉取模型，请手动填写模型名")
    if res.status_code >= 400:
        raise RuntimeError(f"拉取失败 {res.status_code}：{res.text[:200]}")

    try:
        data = res.json()
    except ValueError as e:
        raise RuntimeError("返回的不是 JSON，Base URL 可能指错了路径") from e

    items = data.get("data") if isinstance(data, dict) else data
    ids = [str(it["id"]) for it in items or [] if isinstance(it, dict) and it.get("id")]
    if not ids:
        raise RuntimeError("服务端返回了空模型列表，请手动填写模型名")
    return sorted(set(ids))


__all__ = [
    "VENDORS",
    "VENDOR_BY_KIND",
    "api_root",
    "base_url_for",
    "chat_endpoint",
    "fetch_models",
    "protocol_for",
    "vendor_for",
]
