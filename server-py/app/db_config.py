"""数据库连接配置：只从配置文件读，界面不再提供修改入口。

配置写在 `server-py/.env`（容器部署时写在 `docker-compose.yml` 的环境变量里）::

    DATABASE_URL=postgresql://recall:secret@127.0.0.1:5432/recall?sslmode=disable&schema=public

支持的查询参数：

- `sslmode`：`disable` / `prefer` / `require` / `verify-full`，默认 `prefer`
- `schema`：表放在哪个 schema，默认 `public`（不存在会自动创建）

改完配置需要重启后端才生效。数据库只支持 PostgreSQL。
"""

from __future__ import annotations

import os
from urllib.parse import parse_qs, unquote, urlparse

from .config import ENV_FILE

ENV_KEY = "DATABASE_URL"

DEFAULTS: dict[str, object] = {
    "host": "127.0.0.1",
    "port": 5432,
    "database": "recall",
    "user": "postgres",
    "password": "",
    "sslmode": "prefer",
    "schema": "public",
    "pool_size": 10,
    "connect_timeout": 6,
}

_SCHEMES = ("postgres", "postgresql")


class ConfigError(RuntimeError):
    """配置缺失或写错。"""


def raw_url() -> str:
    return (os.getenv(ENV_KEY) or "").strip()


def configured() -> bool:
    return bool(raw_url())


def normalize(cfg: dict) -> dict:
    """约束类型与取值范围，避免把明显写错的配置带进连接串。"""
    out = dict(DEFAULTS)
    out.update({k: v for k, v in (cfg or {}).items() if v is not None})

    out["host"] = str(out.get("host") or "127.0.0.1").strip()[:120]
    try:
        out["port"] = max(1, min(65535, int(out.get("port") or 5432)))
    except (TypeError, ValueError):
        out["port"] = 5432
    out["database"] = str(out.get("database") or "recall").strip()[:63] or "recall"
    out["user"] = str(out.get("user") or "postgres").strip()[:63]
    out["password"] = str(out.get("password") or "")[:200]
    out["sslmode"] = str(out.get("sslmode") or "prefer").strip()[:16]
    out["schema"] = str(out.get("schema") or "public").strip()[:63] or "public"
    try:
        out["pool_size"] = max(1, min(50, int(out.get("pool_size") or 10)))
    except (TypeError, ValueError):
        out["pool_size"] = 10
    return out


def parse_url(url: str) -> dict:
    parsed = urlparse(url)
    scheme = (parsed.scheme or "").lower()
    if scheme not in _SCHEMES:
        raise ConfigError(
            f"{ENV_KEY} 只支持 PostgreSQL 连接串（postgresql://...），当前是：{scheme or '空'}"
        )

    cfg = {
        "host": parsed.hostname or "127.0.0.1",
        "port": parsed.port or 5432,
        "database": (parsed.path or "/recall").lstrip("/") or "recall",
        "user": unquote(parsed.username or "postgres"),
        "password": unquote(parsed.password or ""),
    }
    params = parse_qs(parsed.query or "")
    for key in ("sslmode", "schema", "pool_size"):
        if params.get(key):
            cfg[key] = params[key][0]
    if params.get("options"):
        # 兼容 psycopg 风格的 options=-c search_path=xxx
        for part in unquote(params["options"][0]).split():
            if part.startswith("search_path="):
                cfg["schema"] = part.split("=", 1)[1].strip('"')
    return normalize(cfg)


_cache: dict | None = None


def load_db_config() -> dict:
    """当前生效的数据库配置；没配或写错时抛 ConfigError。"""
    global _cache
    if _cache is not None:
        return _cache

    url = raw_url()
    if not url:
        raise ConfigError(
            f"没有配置数据库。请在 {ENV_FILE} 里填写：\n"
            f"  {ENV_KEY}=postgresql://用户:密码@主机:5432/recall?sslmode=disable"
        )
    _cache = parse_url(url)
    return _cache


def describe(cfg: dict | None = None) -> str:
    """给人看的连接描述（不含密码），配置不可用时给出提示而不是抛异常。"""
    try:
        cfg = cfg or load_db_config()
    except ConfigError:
        return "PostgreSQL · 未配置（DATABASE_URL 为空）"
    return (
        f"PostgreSQL · {cfg['user']}@{cfg['host']}:{cfg['port']}/{cfg['database']}"
        f"（schema={cfg['schema']}，sslmode={cfg['sslmode']}）"
    )


__all__ = [
    "ConfigError",
    "ENV_KEY",
    "configured",
    "describe",
    "load_db_config",
    "normalize",
    "parse_url",
    "raw_url",
]
