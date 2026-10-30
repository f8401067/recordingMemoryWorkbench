"""数据存储配置：测试连接、切换内置 SQLite / 外置 PostgreSQL。

连接信息落到 `data/config.json`，切换后不需要重启进程 —— db.reconfigure() 会换掉
底层后端，各线程下次取连接时自动重连（见 app/db.py）。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException

from ..database import DatabaseError, ensure_database, probe_server
from ..db import probe_status, reconfigure
from ..db_config import (
    SQLITE_PATH,
    has_env_override,
    load_db_config,
    normalize,
    public_config,
    save_db_config,
)
from ..security import optional_current_user

router = APIRouter(prefix="/api/database", tags=["database"])


def _guard(user: dict[str, str] | None = Depends(optional_current_user)) -> dict[str, str]:
    """平时必须登录；数据库连不上或还没账号时放行，否则用户永远改不回配置。"""
    if user:
        return user
    st = probe_status()
    if not st["connected"] or st["needsAdmin"]:
        return {"id": "", "username": "setup"}
    raise HTTPException(401, "未登录或登录已过期")


def _merge_payload(body: dict[str, Any]) -> dict:
    """密码留空表示「沿用已保存的密码」，避免每次编辑都要重输。"""
    cfg = normalize(body)
    if cfg["driver"] == "postgres" and not cfg["password"]:
        cur = load_db_config()
        same = (
            cur["driver"] == "postgres"
            and cur["host"] == cfg["host"]
            and cur["port"] == cfg["port"]
            and cur["database"] == cfg["database"]
            and cur["user"] == cfg["user"]
        )
        if same:
            cfg["password"] = cur["password"]
    return cfg


@router.get("/config")
def get_config(_: dict = Depends(_guard)) -> dict:
    return {"config": public_config(), "status": probe_status()}


@router.post("/test")
def test_connection(
    body: dict[str, Any] = Body(default_factory=dict),
    _: dict = Depends(_guard),
) -> dict:
    cfg = _merge_payload(body)
    if cfg["driver"] == "sqlite":
        return {
            "ok": True,
            "message": f"内置 SQLite 可用，数据文件 {SQLITE_PATH}",
            "serverVersion": None,
            "databaseExists": True,
        }

    try:
        res = probe_server(cfg)
    except DatabaseError as e:
        return {"ok": False, "message": str(e), "serverVersion": None, "databaseExists": False}

    if not res.get("ok"):
        return {
            "ok": False,
            "message": res.get("message") or "连接失败",
            "serverVersion": None,
            "databaseExists": False,
        }

    exists = bool(res.get("databaseExists"))
    return {
        "ok": True,
        "message": (
            "连接成功，库已存在"
            if exists
            else "连接成功，目标库不存在，保存时会自动创建"
        ),
        "serverVersion": res.get("version"),
        "databaseExists": exists,
    }


@router.post("/apply")
def apply_config(
    body: dict[str, Any] = Body(default_factory=dict),
    _: dict = Depends(_guard),
) -> dict:
    if has_env_override():
        raise HTTPException(
            400,
            "当前数据库由 DATABASE_URL 环境变量指定，界面上无法切换。"
            "请修改容器/进程的环境变量后重启。",
        )

    cfg = _merge_payload(body)
    prev = load_db_config()
    created_db = False

    if cfg["driver"] == "postgres":
        try:
            created_db = ensure_database(cfg)
        except DatabaseError as e:
            raise HTTPException(400, str(e)) from e
        check = probe_server(cfg)
        if not check.get("ok"):
            raise HTTPException(400, check.get("message") or "连接失败")

    save_db_config(cfg)
    try:
        reconfigure()
    except Exception as e:  # noqa: BLE001 - 建表失败要回滚配置
        save_db_config(prev)
        try:
            reconfigure()
        except Exception:  # noqa: BLE001
            pass
        raise HTTPException(400, f"切换失败，已回滚到原来的数据库：{e}") from e

    st = probe_status()
    if cfg["driver"] == "sqlite":
        message = "已切换到内置 SQLite（原有的 PostgreSQL 数据不会迁移过来）"
    elif created_db:
        message = "已连接 PostgreSQL，数据库与数据表已自动创建"
    elif st["initialized"]:
        message = "已连接 PostgreSQL，检测到已有数据表"
    else:
        message = "已连接 PostgreSQL，数据表已自动创建"

    return {
        "ok": True,
        "message": message,
        "createdDatabase": created_db,
        "needsAdmin": st["needsAdmin"],
        "status": st,
        "config": public_config(),
    }
