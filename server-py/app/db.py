"""数据访问门面。

业务代码只认这里的 4 个函数：`all_ / one / execute / execute_many`，底层是
PostgreSQL 连接池，连接串只从配置文件读（见 app/db_config.py）。

两件事值得注意：

1. **后端只建一次**：配置文件改了就重启进程，不再有「运行中切库」这种能力。
2. **结构演进交给迁移**：启动时 `run_migrations()`（Alembic，见 app/db_migrate.py），
   不再维护 schema.sql。
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Any

from .database import DatabaseError, Backend, build_backend, ensure_database, is_missing_database
from .db_config import describe, load_db_config
from .db_migrate import run_migrations

_backend: Backend | None = None
_lock = threading.RLock()
_last_error: str | None = None


def now_iso() -> str:
    """与前端/旧数据保持一致的 ISO8601 UTC 字符串（毫秒 + Z 语义）。"""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


# ------------------------------------------------------------ 连接管理

def current_backend() -> Backend:
    """懒建后端：配置写错时把错误留给调用方，而不是在 import 阶段崩掉。"""
    global _backend
    if _backend is not None:
        return _backend
    with _lock:
        if _backend is None:
            _backend = build_backend(load_db_config())
        return _backend


def get_conn():
    """取一条连接。连接来自池子，**用完必须 `_release()`**，不能缓存在线程上。

    缓存在线程上会把池里的连接一个个钉死：池满 10 条之后，任何落到新线程的
    请求都要等 6 秒然后报 "couldn't get a connection"，线上表现就是登录转圈后
    直接失败。
    """
    return current_backend().connect()


def _release(conn, *, broken: bool = False) -> None:
    """归还连接：坏的直接丢弃，好的提交后还池。"""
    if conn is None:
        return
    try:
        if broken and hasattr(conn, "discard"):
            conn.discard()
            return
        # 事务没结束就把连接还回池，psycopg_pool 会替你 rollback 并打一行警告。
        # 这里先收尾，安静地还回去。
        try:
            conn.commit()
        except Exception:  # noqa: BLE001 - 连接已经断了，直接关
            pass
        conn.close()
    except Exception:  # noqa: BLE001 - 归还失败不该盖住业务错误
        pass


def close_backend() -> None:
    """关服时释放连接池。"""
    global _backend
    backend, _backend = _backend, None
    if backend is not None:
        backend.close()


def init_db() -> None:
    """建库（不存在时）+ 跑迁移。启动阶段调用。"""
    global _last_error
    try:
        cfg = load_db_config()
        try:
            run_migrations(cfg)
        except Exception as exc:  # noqa: BLE001
            # 目标库还没建：用维护库（postgres）建一个再来一次
            if not is_missing_database(exc):
                raise
            ensure_database(cfg)
            print(f"[db] 已创建数据库 {cfg['database']}")
            run_migrations(cfg)
        _last_error = None
    except Exception as exc:  # noqa: BLE001 - 记下来，让 /api/health 能说明原因
        _last_error = str(exc)
        raise


# ------------------------------------------------------------ 状态探测

def probe_status() -> dict[str, Any]:
    """给前端/健康检查用：连没连上、有没有建过表、要不要创建管理员。"""
    out: dict[str, Any] = {
        "target": describe(),
        "connected": False,
        "initialized": False,
        "needsAdmin": False,
        "userCount": 0,
        "serverVersion": None,
        "error": _last_error,
    }
    try:
        # 探测只借一下连接，问完版本和表名就还，别让它一直占着池
        conn = get_conn()
        try:
            out["serverVersion"] = conn.ping()
            out["initialized"] = "users" in conn.tables()
        finally:
            _release(conn)
        if out["initialized"]:
            row = one("SELECT COUNT(*) AS n FROM users")
            out["userCount"] = int((row or {}).get("n") or 0)
            revision = one("SELECT version_num FROM alembic_version")
            out["schemaRevision"] = (revision or {}).get("version_num")
        out["connected"] = True
        out["needsAdmin"] = out["userCount"] == 0
        out["error"] = None
    except Exception as e:  # noqa: BLE001 - 探测失败本身就是结果
        out["error"] = str(e)
    return out


# ------------------------------------------------------------ 查询封装

def _is_connection_error(exc: BaseException) -> bool:
    if isinstance(exc, DatabaseError):
        return True
    try:
        import psycopg
    except ImportError:  # pragma: no cover
        return False
    return isinstance(exc, (psycopg.OperationalError, psycopg.InterfaceError))


def _read(handler):
    """读操作：连接坏了就换一条重试一次；无论成败都要把连接还池。"""
    for attempt in (1, 2):
        conn = get_conn()
        broken = False
        try:
            out = handler(conn)
            conn.commit()  # 读也提交：否则连接会一直挂着只读事务
            return out
        except Exception as exc:  # noqa: BLE001
            broken = _is_connection_error(exc)
            if not broken or attempt == 2:
                raise
            print(f"[db] 连接异常（{exc}），换一条连接重试一次")
        finally:
            _release(conn, broken=broken)
    return None  # pragma: no cover


def all_(sql: str, params: tuple | list = ()) -> list[dict[str, Any]]:
    return _read(lambda conn: [dict(r) for r in conn.execute(sql, params).fetchall()])


def one(sql: str, params: tuple | list = ()) -> dict[str, Any] | None:
    row = _read(lambda conn: conn.execute(sql, params).fetchone())
    return dict(row) if row else None


def execute(sql: str, params: tuple | list = ()):
    conn = get_conn()
    broken = False
    try:
        cur = conn.execute(sql, params)
        conn.commit()
        return cur
    except Exception as exc:  # noqa: BLE001
        # 写操作不自动重试（怕重复写），但要把坏连接丢掉，别让后面的请求继续踩
        if _is_connection_error(exc):
            broken = True
        raise
    finally:
        _release(conn, broken=broken)


def execute_returning(sql: str, params: tuple | list = ()) -> dict[str, Any] | None:
    """执行一条写语句并读回 RETURNING 结果。

    必须在同一个连接里取结果 —— `execute()` 返回时连接已经还池，cursor 随之失效。
    """
    conn = get_conn()
    broken = False
    try:
        row = conn.execute(sql, params).fetchone()
        conn.commit()
        return dict(row) if row else None
    except Exception as exc:  # noqa: BLE001
        if _is_connection_error(exc):
            broken = True
        raise
    finally:
        _release(conn, broken=broken)


def execute_many(sql: str, seq: list[tuple]) -> None:
    conn = get_conn()
    broken = False
    try:
        conn.executemany(sql, seq)
        conn.commit()
    except Exception as exc:  # noqa: BLE001
        if _is_connection_error(exc):
            broken = True
        raise
    finally:
        _release(conn, broken=broken)
