"""Alembic 运行环境。

数据库地址由 app/db_migrate.py 通过配置项 `sqlalchemy.url` 传进来，
所以这里不需要（也不应该）读 app 的配置，方便单独用 CLI 跑。
"""

from __future__ import annotations

from alembic import context
from sqlalchemy import create_engine, pool

config = context.config

TARGET_METADATA = None  # 不用 ORM，全部是手写 SQL 迁移


def _url() -> str:
    return config.get_main_option("sqlalchemy.url")


def run_migrations_offline() -> None:
    context.configure(url=_url(), literal_binds=True, target_metadata=TARGET_METADATA)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(_url(), poolclass=pool.NullPool, future=True)
    try:
        with engine.connect() as connection:
            context.configure(
                connection=connection,
                target_metadata=TARGET_METADATA,
                compare_type=False,
            )
            with context.begin_transaction():
                context.run_migrations()
    finally:
        engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
