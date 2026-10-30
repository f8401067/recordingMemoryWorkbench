from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException

from ..db import one
from ..security import current_user
from ..services.devices import (
    bind_device,
    close_session,
    delete_device,
    get_device,
    list_claims,
    list_devices,
    public_device,
    reset_token,
    update_device,
)

router = APIRouter(prefix="/api/devices", tags=["devices"])


@router.get("")
def get_devices(user: dict = Depends(current_user)) -> list[dict]:
    return list_devices(user["id"])


@router.get("/claims")
def get_claims(user: dict = Depends(current_user)) -> list[dict]:
    """已经开机在轮询、但还没被认领的设备。"""
    return list_claims()


@router.post("/bind")
def bind(body: dict[str, Any] = Body(default_factory=dict), user: dict = Depends(current_user)) -> dict:
    code = str(body.get("code") or "")
    try:
        return bind_device(user["id"], code, body.get("name"))
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    except PermissionError as e:
        raise HTTPException(409, str(e)) from e


@router.patch("/{device_id}")
def patch_device(
    device_id: str,
    body: dict[str, Any] = Body(default_factory=dict),
    user: dict = Depends(current_user),
) -> dict:
    try:
        return update_device(user["id"], device_id, body)
    except ValueError as e:
        raise HTTPException(404, str(e)) from e


@router.post("/{device_id}/reset-token")
def reset_device_token(device_id: str, user: dict = Depends(current_user)) -> dict:
    try:
        return reset_token(user["id"], device_id)
    except ValueError as e:
        raise HTTPException(404, str(e)) from e


@router.post("/{device_id}/close")
def close_device(device_id: str, user: dict = Depends(current_user)) -> dict:
    d = get_device(user["id"], device_id)
    if not d:
        raise HTTPException(404, "设备不存在")
    if not d.get("open_recording_id"):
        return {"ok": True, "closed": False, "reason": "没有进行中的会话"}
    rec_id = close_session(d, reason="manual")
    rec = one("SELECT status, duration_ms FROM recordings WHERE id = ?", (rec_id,)) if rec_id else None
    return {
        "ok": True,
        "closed": True,
        "recordingId": rec_id,
        "status": (rec or {}).get("status"),
        "durationMs": (rec or {}).get("duration_ms") or 0,
    }


@router.delete("/{device_id}")
def remove_device(device_id: str, user: dict = Depends(current_user)) -> dict:
    delete_device(user["id"], device_id)
    return {"ok": True}


@router.get("/{device_id}")
def get_one(device_id: str, user: dict = Depends(current_user)) -> dict:
    d = get_device(user["id"], device_id)
    if not d:
        raise HTTPException(404, "设备不存在")
    return public_device(d)
