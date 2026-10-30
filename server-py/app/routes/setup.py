"""首次初始化向导（无需登录）。

只有两种情况下允许匿名调用：
1. 数据库连不上（配置写错了，得留一条自救通道）；
2. 数据库里一个账号都没有（刚建好库，还没有管理员）。
创建管理员之后，这个接口就永久 409 了。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, HTTPException

from ..db import init_db, probe_status
from ..db_config import public_config
from ..security import create_token
from ..services.settings import read_settings
from ..services.users import create_user, public_user

router = APIRouter(prefix="/api/setup", tags=["setup"])


@router.get("/status")
def status() -> dict:
    st = probe_status()
    return {**st, "config": public_config()}


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
