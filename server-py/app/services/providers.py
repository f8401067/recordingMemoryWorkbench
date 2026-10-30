from __future__ import annotations

import io
import json
import time
import uuid
import wave
from pathlib import Path

from ..config import ASR_CONCURRENCY, TMP_DIR
from ..db import all_, execute, now_iso, one
from ..providers.asr import ASR_KINDS, create_asr_engine
from ..providers.llm import LLM_KINDS, create_llm_engine, is_empty_reply_error
from ..providers.llm_vendors import fetch_models


def mask_key(key: str | None) -> str:
    if not key:
        return ""
    if len(key) <= 10:
        return "****"
    return f"{key[:4]}...{key[-4:]}"


def _safe_params(raw: str | None) -> dict:
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}


def public_provider(p: dict) -> dict:
    return {
        "id": p["id"],
        "category": p["category"],
        "kind": p["kind"],
        "name": p["name"],
        "baseUrl": p.get("base_url"),
        "model": p.get("model"),
        "params": _safe_params(p.get("params")),
        "isDefault": bool(p.get("is_default")),
        "enabled": bool(p.get("enabled")),
        "apiKeyMasked": mask_key(p.get("api_key")),
        "hasApiKey": bool(p.get("api_key")),
    }


def seed_demo_providers(user_id: str) -> None:
    count = one("SELECT COUNT(*) AS n FROM providers WHERE user_id = ?", (user_id,))
    if count and count["n"] > 0:
        return
    ts = now_iso()
    for category in ("asr", "llm"):
        execute(
            """INSERT INTO providers
               (id, user_id, category, kind, name, base_url, api_key, model, params, is_default, enabled, created_at)
               VALUES (?, ?, ?, 'mock', ?, NULL, NULL, 'mock', NULL, 1, 1, ?)""",
            ("prv_" + uuid.uuid4().hex[:20], user_id, category, "演示引擎", ts),
        )


def find_provider(user_id: str, provider_id: str) -> dict | None:
    return one("SELECT * FROM providers WHERE id = ? AND user_id = ?", (provider_id, user_id))


def default_provider(user_id: str, category: str) -> dict | None:
    row = one(
        """SELECT * FROM providers WHERE user_id = ? AND category = ? AND enabled = 1
           AND is_default = 1 LIMIT 1""",
        (user_id, category),
    )
    if row:
        return row
    return one(
        """SELECT * FROM providers WHERE user_id = ? AND category = ? AND enabled = 1
           ORDER BY created_at ASC LIMIT 1""",
        (user_id, category),
    )


def _virtual_mock(category: str, user_id: str) -> dict:
    return {
        "id": f"virtual-mock-{category}",
        "user_id": user_id,
        "category": category,
        "kind": "mock",
        "name": "演示引擎",
        "base_url": None,
        "api_key": None,
        "model": "mock",
        "params": None,
        "is_default": 1,
        "enabled": 1,
        "created_at": now_iso(),
    }


def resolve_asr(user_id: str, preferred_id: str | None = None) -> dict:
    if preferred_id:
        p = find_provider(user_id, preferred_id)
        if p and p.get("enabled"):
            return p
    return default_provider(user_id, "asr") or _virtual_mock("asr", user_id)


def resolve_llm(user_id: str, preferred_id: str | None = None) -> dict:
    if preferred_id:
        p = find_provider(user_id, preferred_id)
        if p and p.get("enabled"):
            return p
    return default_provider(user_id, "llm") or _virtual_mock("llm", user_id)


def build_asr(provider: dict):
    return create_asr_engine(provider)


def build_llm(provider: dict):
    return create_llm_engine(provider)


def provider_catalog() -> dict:
    return {"asr": ASR_KINDS, "llm": LLM_KINDS}


def _silent_wav(seconds: float = 0.6, sample_rate: int = 16000) -> Path:
    path = TMP_DIR / f"probe-{uuid.uuid4().hex}.wav"
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(b"\x00\x00" * int(sample_rate * seconds))
    path.write_bytes(buf.getvalue())
    return path


def merge_probe_payload(body: dict, saved: dict | None = None) -> dict:
    """把「还没保存的表单」拼成一条可测试的服务商记录。

    没填的字段（尤其是 Key）优先沿用已保存的值，这样编辑时不用重复输入密钥。
    """
    saved = saved or {}
    api_key = (body.get("apiKey") or "").strip() or saved.get("api_key")
    base_url = body.get("baseUrl")
    if base_url in (None, ""):
        base_url = saved.get("base_url")
    model = body.get("model")
    if model in (None, ""):
        model = saved.get("model")

    params = body.get("params")
    if isinstance(params, dict):
        params_raw = json.dumps(params, ensure_ascii=False) if params else None
    elif isinstance(params, str):
        params_raw = params or None
    else:
        params_raw = saved.get("params")

    return {
        "id": body.get("providerId") or saved.get("id") or "probe",
        "user_id": saved.get("user_id"),
        "category": body.get("category") or saved.get("category") or "llm",
        "kind": body.get("kind") or saved.get("kind"),
        "name": body.get("name") or saved.get("name") or "未命名",
        "base_url": base_url or None,
        "api_key": api_key or None,
        "model": model or None,
        "params": params_raw,
        "is_default": 0,
        "enabled": 1,
    }


def list_provider_models(kind: str | None, base_url: str | None, api_key: str | None) -> list[str]:
    return fetch_models(kind, base_url, api_key)


def test_provider(provider: dict) -> dict:
    start = time.time()
    try:
        if provider["category"] == "asr":
            if provider["kind"] == "mock":
                return {"ok": True, "message": "演示引擎可用（不调用外部服务）", "latencyMs": 0}
            if str(provider.get("kind") or "").startswith("local-"):
                # 先做零成本体检（依赖装没装、模型下没下），再真跑 0.6 秒静音验证链路
                from ..providers.local_asr import local_engine_ready

                ready, msg = local_engine_ready(provider)
                if not ready:
                    return {"ok": False, "message": msg, "latencyMs": int((time.time() - start) * 1000)}
                engine = create_asr_engine(provider)
                tmp = _silent_wav()
                try:
                    res = engine.transcribe(str(tmp), "audio/wav", None, 600)
                finally:
                    tmp.unlink(missing_ok=True)
                meta = res.get("meta") or {}
                used = meta.get("model") or provider.get("model") or ""
                return {
                    "ok": True,
                    "message": f"{msg}；静音片段推理通过（{used}）",
                    "latencyMs": int((time.time() - start) * 1000),
                }
            engine = create_asr_engine(provider)
            tmp = _silent_wav()
            try:
                engine.transcribe(str(tmp), "audio/wav", None, 600)
            finally:
                tmp.unlink(missing_ok=True)
            return {
                "ok": True,
                "message": "连接成功，识别接口可正常调用",
                "latencyMs": int((time.time() - start) * 1000),
            }

        if provider["kind"] == "mock":
            return {"ok": True, "message": "演示引擎可用（不调用外部服务）", "latencyMs": 0}
        llm = create_llm_engine(provider)
        try:
            text = llm.chat([{"role": "user", "content": "只回复两个字：正常"}], max_tokens=64)
        except Exception as e:  # noqa: BLE001
            if is_empty_reply_error(e):
                return {
                    "ok": True,
                    "message": "接口已连通（该模型未返回正文，多为推理型模型）",
                    "latencyMs": int((time.time() - start) * 1000),
                }
            raise
        reply = (text or "").strip()
        return {
            "ok": True,
            "message": f"连接成功，模型回复：{reply[:40] or '（空）'}",
            "latencyMs": int((time.time() - start) * 1000),
        }
    except Exception as e:  # noqa: BLE001
        return {
            "ok": False,
            "message": str(e)[:400],
            "latencyMs": int((time.time() - start) * 1000),
        }


def list_providers(user_id: str, category: str | None = None) -> list[dict]:
    if category:
        rows = all_(
            """SELECT * FROM providers WHERE user_id = ? AND category = ?
               ORDER BY is_default DESC, created_at ASC""",
            (user_id, category),
        )
    else:
        rows = all_(
            """SELECT * FROM providers WHERE user_id = ?
               ORDER BY category, is_default DESC, created_at ASC""",
            (user_id,),
        )
    return [public_provider(r) for r in rows]


__all__ = [
    "ASR_CONCURRENCY",
    "build_asr",
    "build_llm",
    "default_provider",
    "find_provider",
    "list_providers",
    "list_provider_models",
    "mask_key",
    "merge_probe_payload",
    "provider_catalog",
    "public_provider",
    "resolve_asr",
    "resolve_llm",
    "seed_demo_providers",
    "test_provider",
]
