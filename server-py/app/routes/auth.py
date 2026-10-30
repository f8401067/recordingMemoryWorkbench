from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException

from ..config import uid
from ..db import all_, execute, now_iso, one
from ..security import create_token, current_user, hash_password, verify_password
from ..services.settings import read_settings
from ..services.users import create_user, public_user

router = APIRouter(prefix="/api/auth", tags=["auth"])
key_router = APIRouter(prefix="/api/api-keys", tags=["api-keys"])


@router.post("/register")
def register(body: dict[str, Any] = Body(default_factory=dict)) -> dict:
    username = str(body.get("username") or "").strip()
    password = str(body.get("password") or "")
    if len(username) < 2:
        raise HTTPException(400, "用户名至少 2 个字符")
    if len(password) < 6:
        raise HTTPException(400, "密码至少 6 位")
    if one("SELECT id FROM users WHERE username = ?", (username,)):
        raise HTTPException(409, "用户名已存在")

    user = create_user(username, password, body.get("displayName"))
    token = create_token(user["id"], user["username"])
    return {"token": token, "user": public_user(user), "settings": read_settings(user["id"])}


@router.post("/login")
def login(body: dict[str, Any] = Body(default_factory=dict)) -> dict:
    username = str(body.get("username") or "")
    password = str(body.get("password") or "")
    user = one("SELECT * FROM users WHERE username = ?", (username,))
    if not user or not verify_password(password, user["password_hash"]):
        raise HTTPException(401, "用户名或密码错误")
    token = create_token(user["id"], user["username"])
    return {"token": token, "user": public_user(user), "settings": read_settings(user["id"])}


def _uid_of(user: dict) -> str:
    return user["id"]


@router.get("/me")
def me(user: dict = Depends(current_user)) -> dict:
    row = one("SELECT * FROM users WHERE id = ?", (_uid_of(user),))
    return {
        "user": public_user(row) if row else None,
        "settings": read_settings(row["id"]) if row else None,
    }


@router.patch("/me")
def update_me(body: dict[str, Any] = Body(default_factory=dict), user: dict = Depends(current_user)) -> dict:
    user_id = _uid_of(user)
    display_name = body.get("displayName")
    password = body.get("password")
    if display_name:
        execute(
            "UPDATE users SET display_name = ? WHERE id = ?",
            (str(display_name)[:40], user_id),
        )
    if password and len(str(password)) >= 6:
        execute(
            "UPDATE users SET password_hash = ? WHERE id = ?",
            (hash_password(str(password)), user_id),
        )
    row = one("SELECT * FROM users WHERE id = ?", (user_id,))
    assert row is not None
    return {"user": public_user(row)}


# ---------------- API Key（开放接口凭证） ----------------
@key_router.get("")
def list_keys(user: dict = Depends(current_user)) -> list[dict]:
    rows = all_(
        "SELECT * FROM api_keys WHERE user_id = ? ORDER BY created_at DESC",
        (_uid_of(user),),
    )
    return [
        {
            "id": r["id"],
            "name": r["name"],
            "key": f"{r['key_value'][:8]}...{r['key_value'][-4:]}",
            "createdAt": r["created_at"],
            "lastUsedAt": r.get("last_used_at"),
        }
        for r in rows
    ]


@key_router.post("", status_code=201)
def create_key(body: dict[str, Any] = Body(default_factory=dict), user: dict = Depends(current_user)) -> dict:
    user_id = _uid_of(user)
    name = str(body.get("name") or "默认密钥")[:40]
    key_value = "rk_" + uid("").ljust(32, "0")[:32]
    key_id = uid("key_")
    execute(
        """INSERT INTO api_keys (id, user_id, name, key_value, created_at)
           VALUES (?, ?, ?, ?, ?)""",
        (key_id, user_id, name, key_value, now_iso()),
    )
    return {"id": key_id, "name": name, "key": key_value}


@key_router.delete("/{key_id}")
def delete_key(key_id: str, user: dict = Depends(current_user)) -> dict:
    execute("DELETE FROM api_keys WHERE id = ? AND user_id = ?", (key_id, _uid_of(user)))
    return {"ok": True}
