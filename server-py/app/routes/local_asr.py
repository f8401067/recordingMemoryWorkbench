from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from ..security import current_user
from ..services import local_asr_models as store

router = APIRouter(prefix="/api/asr/local", tags=["local-asr"])


class DownloadBody(BaseModel):
    backend: str
    model: str


@router.get("/status")
def local_status(_: dict = Depends(current_user)) -> dict:
    """本地引擎总览：依赖是否就绪、模型目录、进行中的下载。"""
    return {**store.status(), "models": store.list_models(), "backends": store.backend_status()}


@router.get("/models")
def local_models(
    backend: str | None = Query(None),
    _: dict = Depends(current_user),
) -> dict:
    if backend and backend not in store.BACKENDS:
        raise HTTPException(400, f"未知的 backend：{backend}")
    return {
        "models": store.list_models(backend),
        "backends": store.backend_status(),
        "downloads": store.list_downloads(),
        "modelsDir": str(store.MODELS_DIR),
    }


@router.post("/models/download")
def download_model(body: DownloadBody, _: dict = Depends(current_user)) -> dict:
    try:
        return store.start_download(body.backend, body.model)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@router.delete("/models/{backend}/{model}")
def delete_model(backend: str, model: str, _: dict = Depends(current_user)) -> dict:
    if backend not in store.BACKENDS:
        raise HTTPException(400, f"未知的 backend：{backend}")
    removed = store.remove_model(backend, model)
    if not removed:
        raise HTTPException(404, "模型不存在或已删除")
    return {"ok": True}


@router.get("/diarization")
def diarization_status(_: dict = Depends(current_user)) -> dict:
    """说话人分离模型（pyannote 分割 + 3D-Speaker 声纹嵌入）。"""
    return store.diarization_status()


@router.post("/diarization/download")
def download_diarization(_: dict = Depends(current_user)) -> dict:
    return store.start_diarization_download()


@router.delete("/diarization/model")
def delete_diarization(_: dict = Depends(current_user)) -> dict:
    if not store.remove_diarization_models():
        raise HTTPException(404, "模型不存在或已删除")
    return {"ok": True}
