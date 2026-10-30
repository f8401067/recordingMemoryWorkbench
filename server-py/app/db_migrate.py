"""数据库结构迁移（Alembic）。

结构演进不再靠 `schema.sql` + 手写 `ALTER TABLE`，而是版本化的迁移脚本：

    server-py/app/migrations/versions/0001_initial.py

- 启动时自动 `upgrade head`（幂等，新库建表、老库补列）
- 手工执行：`cd server-py && .venv/bin/alembic -c alembic.ini upgrade head`
  （需要先在 alembic.ini 里填 sqlalchemy.url，或直接用下面的 Python 入口）
- 新增迁移：`uv run alembic -c alembic.ini revision -m "xxx"`，然后手写 SQL

迁移脚本写的是两库通用的 SQL，所以 SQLite 与 PostgreSQL 共用一套历史。
"""

from __future__ import annotations

import threading
from pathlib import Path
from urllib.parse import quote

from alembic import command
from alembic.config import Config

from .db_config import SQLITE_PATH, load_db_config

APP_DIR = Path(__file__).resolve().parent
SERVER_DIR = APP_DIR.parent
MIGRATIONS_DIR = APP_DIR / "migrations"
ALEMBIC_INI = SERVER_DIR / "alembic.ini"

_lock = threading.Lock()


def database_url(cfg: dict | None = None) -> str:
    """把当前数据库配置转成 SQLAlchemy URL（只为迁移引擎服务，查询不走 SQLAlchemy）。"""
    cfg = cfg or load_db_config()
    if cfg["driver"] == "sqlite":
        return f"sqlite:///{SQLITE_PATH}"

    user = quote(cfg["user"], safe="")
    password = quote(cfg["password"], safe="")
    query = f"?sslmode={quote(cfg['sslmode'], safe='')}"
    return (
        f"postgresql+psycopg://{user}:{password}"
        f"@{cfg['host']}:{cfg['port']}/{cfg['database']}{query}"
    )


def alembic_config(cfg: dict | None = None) -> Config:
    conf = Config(str(ALEMBIC_INI))
    conf.set_main_option("script_location", str(MIGRATIONS_DIR))
    # ConfigParser 会把 % 当插值符号，密码里有 % 时必须转义
    conf.set_main_option("sqlalchemy.url", database_url(cfg).replace("%", "%%"))
    return conf


def run_migrations(cfg: dict | None = None) -> None:
    with _lock:
        command.upgrade(alembic_config(cfg), "head")


def current_revision(cfg: dict | None = None) -> str | None:
    try:
        from alembic.runtime.migration import MigrationContext
        from sqlalchemy import create_engine

        engine = create_engine(database_url(cfg), poolclass=None)
        try:
            with engine.connect() as conn:
                return MigrationContext.configure(conn).get_current_revision()
        finally:
            engine.dispose()
    except Exception:  # noqa: BLE001 - 只用于展示
        return None


__all__ = ["ALEMBIC_INI", "MIGRATIONS_DIR", "alembic_config", "current_revision", "database_url", "run_migrations"]
