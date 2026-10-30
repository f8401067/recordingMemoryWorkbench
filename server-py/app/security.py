from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import bcrypt
import jwt
from fastapi import Header, HTTPException, Query

from .config import JWT_ALG, JWT_EXPIRE_DAYS, JWT_SECRET


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8")[:72], bcrypt.gensalt(rounds=10)).decode()


def verify_password(password: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8")[:72], hashed.encode())
    except (ValueError, TypeError):
        return False


def create_token(user_id: str, username: str) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": user_id,
        "username": username,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(days=JWT_EXPIRE_DAYS)).timestamp()),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALG)


def decode_token(token: str) -> dict[str, Any]:
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALG])
    except jwt.PyJWTError as e:
        raise HTTPException(status_code=401, detail="未登录或登录已过期") from e


def current_user(authorization: str | None = Header(default=None)) -> dict[str, str]:
    """FastAPI 依赖：解析 Bearer Token，返回 {id, username}。"""
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="未登录或登录已过期")
    claims = decode_token(authorization[7:].strip())
    sub = claims.get("sub")
    if not sub:
        raise HTTPException(status_code=401, detail="未登录或登录已过期")
    return {"id": str(sub), "username": str(claims.get("username") or "")}


def optional_current_user(authorization: str | None = Header(default=None)) -> dict[str, str] | None:
    """解析失败不报错，返回 None。数据库故障/首次初始化时需要这种宽松鉴权。"""
    if not authorization:
        return None
    try:
        return current_user(authorization)
    except HTTPException:
        return None


def media_user(
    authorization: str | None = Header(default=None),
    token: str | None = Query(default=None),
) -> dict[str, str]:
    """音频这类由 <audio>/<a> 直接发起的请求带不了自定义头，允许 ?token= 兜底。

    优先用 Authorization 头（和普通接口一致），没有才看 query。
    """
    raw = ""
    if authorization and authorization.lower().startswith("bearer "):
        raw = authorization[7:].strip()
    elif token:
        raw = token.strip()
    if not raw:
        raise HTTPException(status_code=401, detail="未登录或登录已过期")
    claims = decode_token(raw)
    sub = claims.get("sub")
    if not sub:
        raise HTTPException(status_code=401, detail="未登录或登录已过期")
    return {"id": str(sub), "username": str(claims.get("username") or "")}
