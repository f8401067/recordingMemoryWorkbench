from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..security import current_user
from ..services import voice_profiles as store

router = APIRouter(prefix="/api/voice-profiles", tags=["voice-profiles"])


class EnrollBody(BaseModel):
    recordingId: str
    speaker: int
    name: str = ""
    isSelf: bool = False


@router.get("")
def list_profiles(user: dict = Depends(current_user)) -> dict:
    """已录入的声纹角色；「我」排在最前面。"""
    return {"profiles": store.list_profiles(user["id"])}


@router.post("/from-recording")
def enroll_from_recording(body: EnrollBody, user: dict = Depends(current_user)) -> dict:
    """把某条录音里已分离的说话人固化成角色（isSelf=true 表示「我」）。"""
    try:
        profile = store.enroll_from_recording(
            user["id"],
            body.recordingId,
            int(body.speaker),
            body.name,
            bool(body.isSelf),
        )
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return {"ok": True, "profile": profile, "profiles": store.list_profiles(user["id"])}


@router.delete("/{profile_id}")
def delete_profile(profile_id: str, user: dict = Depends(current_user)) -> dict:
    if not store.delete_profile(user["id"], profile_id):
        raise HTTPException(404, "角色不存在或已删除")
    return {"ok": True, "profiles": store.list_profiles(user["id"])}
