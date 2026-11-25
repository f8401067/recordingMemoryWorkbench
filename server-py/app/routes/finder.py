from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException

from ..security import current_user
from ..services.finder import run_find
from ..services.mcp_dispatch import decide_and_push

router = APIRouter(prefix="/api/find", tags=["find"])


@router.post("")
def find(
    body: dict[str, Any] = Body(default_factory=dict),
    user: dict = Depends(current_user),
) -> dict:
    """「找寻」：大模型按问题挑选检索工具，返回答案 + 最匹配的录音 + 当天列表跳转。

    body:
      question: 必填，用户的问题或要找的内容
      history:  可选，[{"role":"user"|"assistant","content":"..."}]，只传此前的对话
      providerId: 可选，指定语言模型服务商
    """
    question = " ".join(str(body.get("question") or "").split())
    if not question:
        raise HTTPException(400, "请输入要找寻的内容")
    if len(question) > 2000:
        raise HTTPException(400, "问题过长，请精简到 2000 字以内")

    history = body.get("history")
    if not isinstance(history, list):
        history = []
    history = [
        {"role": m["role"], "content": str(m.get("content") or "")[:1200]}
        for m in history
        if isinstance(m, dict) and m.get("role") in ("user", "assistant") and m.get("content")
    ][-6:]

    provider_id = body.get("providerId")
    return run_find(
        user,
        question,
        history=history,
        provider_id=str(provider_id) if provider_id else None,
    )


@router.post("/push")
def push(
    body: dict[str, Any] = Body(default_factory=dict),
    user: dict = Depends(current_user),
) -> dict:
    """把已经生成的回答发给配置成「手动」的 MCP 服务，由大模型挑选工具。

    body:
      question:   这次提问
      answer:     要发送的总结文字
      matches:    可选，依据的录音摘要
      serverIds:  可选，只发给指定服务；不传则发给所有手动模式的服务
      providerId: 可选，指定用来做工具决策的语言模型
    """
    answer = str(body.get("answer") or "").strip()
    if not answer:
        raise HTTPException(400, "没有可发送的内容")
    if len(answer) > 8000:
        raise HTTPException(400, "内容过长，请精简后再发送")

    server_ids = body.get("serverIds")
    if server_ids is not None and not isinstance(server_ids, list):
        raise HTTPException(400, "serverIds 必须是数组")

    try:
        out = decide_and_push(
            user["id"],
            str(body.get("question") or ""),
            answer,
            mode="manual",
            server_ids=[str(s) for s in server_ids] if server_ids else None,
            provider_id=str(body["providerId"]) if body.get("providerId") else None,
            extra={"matches": str(body.get("matches") or "")},
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"推送失败：{str(exc)[:200]}") from exc

    results = out["results"]
    return {
        "ok": bool(results) and all(r["ok"] for r in results),
        "results": results,
        "by": out.get("by"),
        "reason": out.get("reason") or "",
        "message": out.get("note")
        or ("" if results else "没有配置成手动推送的 MCP 服务，或模型判断这次不需要发送"),
    }
