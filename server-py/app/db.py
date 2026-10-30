"""数据访问门面。

业务代码只认这里的 4 个函数：`all_ / one / execute / execute_many`，底层既可以是
内置 SQLite，也可以是外置 PostgreSQL，由 `data/config.json` 决定。

两件事值得注意：

1. **换库不用重启**：`reconfigure()` 会把全局后端换掉并让线程本地连接失效，
   每个线程下次取连接时按新后端重连；同时监听 config.json 的 mtime，
   别的进程（多副本、或手改配置文件）改了配置，本进程 1 秒内自动跟上。
2. **结构演进交给迁移**：启动时 `run_migrations()`（Alembic，见 app/db_migrate.py），
   不再维护 schema.sql。
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from typing import Any

from .database import Backend, build_backend
from .db_config import CONFIG_PATH, SQLITE_PATH, describe, load_db_config
from .db_migrate import run_migrations

_local = threading.local()
_backend: Backend = build_backend(load_db_config(), SQLITE_PATH)
_generation = 0
_last_error: str | None = None

_swap_lock = threading.RLock()
_reloading = False
_last_check = 0.0
_RECHECK_SECONDS = 1.0


def now_iso() -> str:
    """与前端/旧数据保持一致的 ISO8601 UTC 字符串（毫秒 + Z 语义）。"""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


# ------------------------------------------------------------ 连接管理

def current_backend() -> Backend:
    return _backend


def _stamp_of_config() -> tuple[int, int] | None:
    try:
        st = CONFIG_PATH.stat()
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


_config_stamp: tuple[int, int] | None = _stamp_of_config()


def get_conn():
    """取一条连接。

    SQLite 用线程本地缓存（建连接便宜，复用省事，且连接状态不能随便丢）；
    PostgreSQL 反过来——连接来自池，**必须用完就还**，不能缓存在线程上。
    缓存在线程上会把池里的连接一个个钉死：池满 8 条之后，任何落到新线程的
    请求都要等 8 秒然后报 "couldn't get a connection"，线上表现就是登录转圈后
    直接失败。所以 PostgreSQL 每次现借现还，见 `_release()`。
    """
    _maybe_reload()

    backend = _backend
    if backend.name != "sqlite":
        return backend.connect()

    conn = getattr(_local, "conn", None)
    if conn is not None and getattr(_local, "gen", -1) == _generation:
        return conn

    if conn is not None:
        try:
            conn.close()
        except Exception:  # noqa: BLE001 - 旧连接可能已经断了
            pass

    conn = backend.connect()
    _local.conn = conn
    _local.gen = _generation
    return conn


def _release(conn, *, broken: bool = False) -> None:
    """归还连接。PostgreSQL 必须还池；SQLite 的连接是线程本地缓存，不能关。"""
    if _backend.name == "sqlite" or conn is None:
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


def reconfigure() -> Backend:
    """按 config.json 重新选后端并初始化（迁移 + 建表）。失败时抛异常，由调用方回滚配置。"""
    global _backend, _generation, _last_error, _config_stamp

    cfg = load_db_config()
    backend = build_backend(cfg, SQLITE_PATH)

    with _swap_lock:
        old = _backend
        _backend = backend
        _generation += 1
        _config_stamp = _stamp_of_config()

    try:
        init_db()
    except Exception as e:
        _last_error = str(e)
        raise
    _last_error = None

    if old is not backend:
        old.close()  # 关掉旧连接池（后台线程里等连接归还）
    return backend


def _maybe_reload() -> None:
    """config.json 变了就自动切过去，多副本部署时不用挨个重启。"""
    global _last_check, _reloading

    now = time.monotonic()
    if now - _last_check < _RECHECK_SECONDS:
        return
    _last_check = now
    if _reloading:
        return
    if _stamp_of_config() == _config_stamp:
        return

    with _swap_lock:
        if _reloading or _stamp_of_config() == _config_stamp:
            return
        _reloading = True
        try:
            reconfigure()
            print(f"[db] 配置已变更，已切换到 {describe()}")
        except Exception as e:  # noqa: BLE001 - 切换失败保持服务可访问，让用户去网页改
            print(f"[db] 配置已变更但切换失败：{e}")
        finally:
            _reloading = False


def init_db() -> None:
    run_migrations()
    conn = get_conn()
    try:
        conn.commit()
    finally:
        _release(conn)


# ------------------------------------------------------------ 状态探测

def probe_status() -> dict[str, Any]:
    """给前端/健康检查用：连没连上、有没有建过表、要不要创建管理员。"""
    cfg = load_db_config()
    out: dict[str, Any] = {
        "driver": cfg["driver"],
        "target": describe(cfg),
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

# 连接级故障（不是 SQL 写错了）：换个连接就好。SQLite 的 "disk I/O error" 最常见，
# 典型场景是 -shm/-wal 被别的进程动过、或者外置盘短暂掉线，DB 本身没坏。
_RECOVERABLE = (
    "disk i/o error",
    "unable to open database file",
    "database is locked",
    # -wal/-shm 被别的进程重置过之后，已打开的连接还拿着旧的页缓存，
    # 就会报这个；磁盘上的库其实是好的，换条连接重新读就好了。
    "disk image is malformed",
    "not a database",
    "closed database",
    "connection is closed",
    "connection already closed",
    "server closed the connection",
    "ssl connection has been closed",
    "terminating connection",
    "connection reset",
)


def _is_connection_error(exc: BaseException) -> bool:
    from .database import DatabaseError

    if isinstance(exc, DatabaseError):
        return True

    import sqlite3

    if isinstance(exc, (sqlite3.OperationalError, sqlite3.InterfaceError, sqlite3.ProgrammingError)):
        text = str(exc).lower()
        return any(hint in text for hint in _RECOVERABLE)

    # sqlite3.DatabaseError 是上面三者的父类，「页缓存过期」报的 malformed 就落在这里：
    # 换连接重试一次；真损坏的话新连接照样报错，不会把问题吞掉。
    if isinstance(exc, sqlite3.DatabaseError):
        text = str(exc).lower()
        return any(hint in text for hint in _RECOVERABLE)

    try:
        import psycopg
    except ImportError:  # pragma: no cover
        return False
    return isinstance(exc, (psycopg.OperationalError, psycopg.InterfaceError))


def _drop_conn() -> None:
    """丢掉当前线程的连接，下次 get_conn() 会重新建一条。"""
    conn = getattr(_local, "conn", None)
    if conn is not None:
        try:
            conn.close()
        except Exception:  # noqa: BLE001 - 已经坏掉的连接关不掉很正常
            pass
    _local.conn = None
    _local.gen = -1


def _read(handler):
    """读操作：连接坏了就换一条重试一次；PostgreSQL 无论成败都要把连接还池。"""
    for attempt in (1, 2):
        conn = get_conn()
        broken = False
        try:
            out = handler(conn)
            conn.commit()  # 读也提交：PostgreSQL 下否则会一直挂着只读事务
            return out
        except Exception as exc:  # noqa: BLE001
            broken = _is_connection_error(exc)
            if not broken or attempt == 2:
                raise
            print(f"[db] 连接异常（{exc}），换一条连接重试一次")
            _drop_conn()
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
            _drop_conn()
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
            _drop_conn()
            broken = True
        raise
    finally:
        _release(conn, broken=broken)
