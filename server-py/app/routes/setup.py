"""首次初始化：数据库连不上时告诉你怎么改配置，没有账号时创建第一个管理员。

数据库连接信息只来自配置文件（server-py/.env 或容器环境变量），页面上不再提供修改入口。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, HTTPException

from ..config import ENV_FILE
from ..db import init_db, probe_status
from ..security import create_token
from ..services.settings import read_settings
from ..services.users import create_user, public_user

router = APIRouter(prefix="/api/setup", tags=["setup"])


@router.get("/status")
def status() -> dict:
    st = probe_status()
    return {
        **st,
        # 配置写错时前端直接把这两个字段显示出来，省得用户到处找
        "configFile": str(ENV_FILE),
        "configKey": "DATABASE_URL",
    }


@router.post("/admin")
def create_admin(body: dict[str, Any] = Body(default_factory=dict)) -> dict:
    st = probe_status()

    if st["connected"] and not st["initialized"]:
        try:
            init_db()
        except Exception as e:  # noqa: BLE001
            raise HTTPException(500, f"建表失败：{e}") from e
        st = probe_status()

    if not st["connected"]:
        raise HTTPException(503, f"数据库还不可用：{st.get('error') or '连接失败'}")
    if st["userCount"] > 0:
        raise HTTPException(409, "数据库里已经有账号了，请直接登录")

    username = str(body.get("username") or "").strip()
    password = str(body.get("password") or "")
    if len(username) < 2:
        raise HTTPException(400, "用户名至少 2 个字符")
    if len(password) < 6:
        raise HTTPException(400, "密码至少 6 位")
    if len(password) > 72:
        raise HTTPException(400, "密码最长 72 位")

    user = create_user(username, password, str(body.get("displayName") or "").strip() or username)
    token = create_token(user["id"], user["username"])
    return {
        "token": token,
        "user": public_user(user),
        "settings": read_settings(user["id"]),
        "status": probe_status(),
    }
