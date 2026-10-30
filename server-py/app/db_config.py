"""数据库连接的持久化配置。

连接信息写在 `<DATA_DIR>/config.json`，容器部署时随 `data/` 卷一起保留：::

    {
      "database": {
        "driver": "postgres",
        "host": "127.0.0.1",
        "port": 5432,
        "database": "recall",
        "user": "postgres",
        "password": "******",
        "sslmode": "prefer",
        "schema": "public"
      }
    }

也支持用环境变量 `DATABASE_URL` 强制指定（优先级最高，用于 12-factor 部署），
此时文件里的配置会被忽略，界面上只读展示。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from urllib.parse import unquote, urlparse

from .config import DATA_DIR

CONFIG_PATH = DATA_DIR / "config.json"
SQLITE_PATH = DATA_DIR / "recall.db"

DEFAULTS: dict[str, object] = {
    "driver": "sqlite",
    "host": "127.0.0.1",
    "port": 5432,
    "database": "recall",
    "user": "postgres",
    "password": "",
    "sslmode": "prefer",
    "schema": "public",
}


def _read_file() -> dict:
    try:
        raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _from_url(url: str) -> dict:
    parsed = urlparse(url)
    cfg = dict(DEFAULTS)
    cfg.update(
        {
            "driver": "postgres",
            "host": parsed.hostname or "127.0.0.1",
            "port": parsed.port or 5432,
            "database": (parsed.path or "/recall").lstrip("/") or "recall",
            "user": unquote(parsed.username or "postgres"),
            "password": unquote(parsed.password or ""),
        }
    )
    query = parsed.query or ""
    for part in query.split("&"):
        if part.startswith("sslmode="):
            cfg["sslmode"] = part.split("=", 1)[1] or "prefer"
        elif part.startswith("options=") or part.startswith("schema="):
            cfg["schema"] = unquote(part.split("=", 1)[1]) or "public"
    return cfg


def normalize(section: dict) -> dict:
    """约束类型与取值范围，避免把明显写错的配置落盘。"""
    cfg = dict(DEFAULTS)
    cfg.update({k: v for k, v in (section or {}).items() if v is not None})

    cfg["driver"] = "postgres" if str(cfg.get("driver") or "sqlite").lower() in (
        "postgres",
        "postgresql",
        "pg",
    ) else "sqlite"
    cfg["host"] = str(cfg.get("host") or "127.0.0.1").strip()[:120]
    try:
        cfg["port"] = max(1, min(65535, int(cfg.get("port") or 5432)))
    except (TypeError, ValueError):
        cfg["port"] = 5432
    cfg["database"] = str(cfg.get("database") or "recall").strip()[:63] or "recall"
    cfg["user"] = str(cfg.get("user") or "postgres").strip()[:63]
    cfg["password"] = str(cfg.get("password") or "")[:200]
    cfg["sslmode"] = str(cfg.get("sslmode") or "prefer").strip()[:16]
    cfg["schema"] = str(cfg.get("schema") or "public").strip()[:63] or "public"
    return cfg


def has_env_override() -> bool:
    return bool((os.getenv("DATABASE_URL") or "").strip())


def load_db_config() -> dict:
    """当前生效的数据库配置（环境变量优先）。"""
    url = (os.getenv("DATABASE_URL") or "").strip()
    if url:
        return _from_url(url)
    return normalize(_read_file().get("database") or {})


def save_db_config(section: dict) -> None:
    """合并写入 config.json，保留文件里其他段落。"""
    data = _read_file()
    data["database"] = normalize(section)
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(CONFIG_PATH)


def describe(cfg: dict | None = None) -> str:
    cfg = cfg or load_db_config()
    if cfg["driver"] == "sqlite":
        return f"SQLite · {SQLITE_PATH}"
    return (
        f"PostgreSQL · {cfg['user']}@{cfg['host']}:{cfg['port']}/{cfg['database']}"
        f"（schema={cfg.get('schema') or 'public'}）"
    )


def public_config() -> dict:
    """给前端用的视图：不回传密码明文，只告诉它密码有没有配过。"""
    cfg = load_db_config()
    return {
        "driver": cfg["driver"],
        "host": cfg["host"],
        "port": cfg["port"],
        "database": cfg["database"],
        "user": cfg["user"],
        "sslmode": cfg["sslmode"],
        "schema": cfg["schema"],
        "hasPassword": bool(cfg["password"]),
        "target": describe(cfg),
        "configPath": str(CONFIG_PATH),
        "sqlitePath": str(SQLITE_PATH),
        "fromEnv": has_env_override(),
    }


__all__ = [
    "CONFIG_PATH",
    "SQLITE_PATH",
    "describe",
    "has_env_override",
    "load_db_config",
    "normalize",
    "public_config",
    "save_db_config",
]
