"""数据库后端：SQLite（内置）与 PostgreSQL（外置）。

上层只通过 `Conn` 暴露的少量方法访问数据库：

    execute(sql, params) -> cursor   # cursor 支持 .rowcount / .fetchone() / .fetchall()
    executemany(sql, seq)
    script(ddl)                      # 执行多条 DDL
    columns(table) -> set[str]
    tables() -> set[str]
    ping() -> str                    # 返回服务端版本，用于「测试连接」
    commit() / rollback() / close()

SQL 仍然按 SQLite 的 `?` 占位符书写，PostgreSQL 侧在 execute 里统一翻译，
所以业务代码（routes / services）一行都不用改。
"""

from __future__ import annotations

import re
import sqlite3
import threading
from pathlib import Path
from typing import Any, Iterable, Sequence

from .sqlutil import split_statements, translate

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")


class DatabaseError(RuntimeError):
    """带人话提示的数据库错误。"""


# ---------------------------------------------------------------- SQLite

class SqliteConn:
    name = "sqlite"

    def __init__(self, path: Path) -> None:
        self.path = path
        self.raw = sqlite3.connect(str(path), check_same_thread=False, timeout=20)
        self.raw.row_factory = sqlite3.Row
        self.raw.execute("PRAGMA journal_mode=WAL")
        self.raw.execute("PRAGMA foreign_keys=ON")
        self.raw.execute("PRAGMA busy_timeout=8000")

    def execute(self, sql: str, params: Sequence[Any] = ()):
        return self.raw.execute(sql, tuple(params))

    def executemany(self, sql: str, seq: Iterable[Sequence[Any]]):
        return self.raw.executemany(sql, [tuple(p) for p in seq])

    def script(self, ddl: str) -> None:
        self.raw.executescript(ddl)

    def commit(self) -> None:
        self.raw.commit()

    def rollback(self) -> None:
        self.raw.rollback()

    def close(self) -> None:
        self.raw.close()

    def columns(self, table: str) -> set[str]:
        return {row["name"] for row in self.raw.execute(f"PRAGMA table_info({table})")}

    def tables(self) -> set[str]:
        rows = self.raw.execute("SELECT name FROM sqlite_master WHERE type='table'")
        return {row["name"] for row in rows}

    def ping(self) -> str:
        return f"SQLite {sqlite3.sqlite_version}"


# ------------------------------------------------------------ PostgreSQL

def pg_params(cfg: dict) -> dict:
    """把配置字典转成 psycopg 的连接参数。"""
    return {
        "host": (cfg.get("host") or "127.0.0.1").strip(),
        "port": int(cfg.get("port") or 5432),
        "dbname": (cfg.get("database") or "recall").strip(),
        "user": (cfg.get("user") or "postgres").strip(),
        "password": cfg.get("password") or "",
        "connect_timeout": int(cfg.get("connect_timeout") or 6),
        "application_name": "recall-workbench",
        "sslmode": (cfg.get("sslmode") or "prefer").strip(),
    }


_PG_HINTS = {
    "3D000": "目标数据库不存在（保存时会尝试自动创建）",
    "28P01": "用户名或密码不正确",
    "28000": "认证失败：请检查用户名/密码，以及 pg_hba.conf 的访问规则",
    "08001": "无法连接到服务器：请检查主机、端口是否可达，以及是否允许远程访问",
    "08006": "连接被中断",
    "42501": "权限不足：当前账号没有建库/建表权限",
    "42P01": "表不存在",
    "53300": "连接数已满",
}

# 连接阶段的错误 psycopg 拿不到 sqlstate，只能靠报错文本认
_MISSING_DB = re.compile(r'database "[^"]+" does not exist', re.I)
_AUTH_FAIL = re.compile(
    r"password authentication failed|no pg_hba\.conf entry|role \"[^\"]+\" does not exist",
    re.I,
)
_UNREACHABLE = re.compile(
    r"connection refused|could not connect|timeout expired|name or service not known"
    r"|nodename nor servname|connection timed out|network is unreachable",
    re.I,
)


def is_missing_database(exc: BaseException) -> bool:
    return bool(_MISSING_DB.search(str(exc)))


def pg_error_text(exc: BaseException) -> str:
    text = str(exc).strip()
    state = getattr(exc, "sqlstate", None)
    hint = _PG_HINTS.get(state or "")
    if not hint:
        if _MISSING_DB.search(text):
            hint, state = _PG_HINTS["3D000"], "3D000"
        elif _AUTH_FAIL.search(text):
            hint, state = _PG_HINTS["28P01"], "28P01"
        elif _UNREACHABLE.search(text):
            hint, state = _PG_HINTS["08001"], "08001"
    raw = text.splitlines()[0] if text else exc.__class__.__name__
    return f"{hint}（{state}）" if hint and state else raw


def _psycopg():
    try:
        import psycopg  # noqa: F401
    except ImportError as e:  # pragma: no cover - 依赖缺失时的兜底
        raise DatabaseError(
            "未安装 psycopg，无法连接 PostgreSQL。请在 server-py 下执行：uv sync"
        ) from e
    return psycopg


class PgConn:
    name = "postgres"

    def __init__(self, cfg: dict, raw=None, release=None, discard=None) -> None:
        """raw/release 为空时自建连接（用于探测），否则复用连接池借出的连接。"""
        self.cfg = cfg
        self._release = release
        self._discard = discard

        if raw is not None:
            self.raw = raw
            return

        psycopg = _psycopg()
        from psycopg.rows import dict_row

        try:
            self.raw = psycopg.connect(**pg_params(cfg), row_factory=dict_row)
        except psycopg.Error as e:
            raise DatabaseError(pg_error_text(e)) from e

        schema = (cfg.get("schema") or "").strip()
        if schema:
            if not _IDENT.match(schema):
                raise DatabaseError(f"schema 名称不合法：{schema}")
            with self.raw.cursor() as cur:
                cur.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
                cur.execute(f'SET search_path TO "{schema}"')
            self.raw.commit()

    def execute(self, sql: str, params: Sequence[Any] = ()):
        cur = self.raw.cursor()
        cur.execute(translate(sql), _coerce(params))
        return cur

    def executemany(self, sql: str, seq: Iterable[Sequence[Any]]):
        cur = self.raw.cursor()
        cur.executemany(translate(sql), [_coerce(p) for p in seq])
        return cur

    def script(self, ddl: str) -> None:
        cur = self.raw.cursor()
        try:
            # 多实例同时首启时避免并发 ALTER/CREATE 打架
            cur.execute("SELECT pg_advisory_xact_lock(7610253)")
            for stmt in split_statements(ddl):
                if stmt.lstrip().upper().startswith("PRAGMA"):
                    continue
                cur.execute(stmt)
            self.raw.commit()
        except Exception:
            self.raw.rollback()
            raise

    def commit(self) -> None:
        self.raw.commit()

    def rollback(self) -> None:
        self.raw.rollback()

    def close(self) -> None:
        if self._release is not None:
            self._release()
            self._release = None
            return
        try:
            self.raw.close()
        except Exception:  # noqa: BLE001 - 旧连接可能已经断了
            pass

    def discard(self) -> None:
        """连接坏了的时候用：别还回池子继续祸害别人，让池直接关掉它。"""
        raw, self.raw = getattr(self, "raw", None), None
        discard, self._discard = self._discard, None
        release, self._release = self._release, None
        try:
            if discard is not None:
                discard()  # 池的 putconn(raw, close=True)
            elif release is not None:
                release()
            elif raw is not None:
                raw.close()
        except Exception:  # noqa: BLE001 - 坏连接收尾失败无所谓
            pass

    def columns(self, table: str) -> set[str]:
        rows = self.execute(
            """SELECT column_name FROM information_schema.columns
               WHERE table_schema = ANY(current_schemas(false)) AND table_name = ?""",
            (table,),
        ).fetchall()
        return {r["column_name"] for r in rows}

    def tables(self) -> set[str]:
        rows = self.execute(
            """SELECT table_name FROM information_schema.tables
               WHERE table_schema = ANY(current_schemas(false))"""
        ).fetchall()
        return {r["table_name"] for r in rows}

    def ping(self) -> str:
        row = self.execute(
            "SELECT current_setting('server_version') AS v, current_database() AS db"
        ).fetchone()
        return f"PostgreSQL {row['v']} / {row['db']}"


def _coerce(params: Sequence[Any]) -> tuple:
    """schema 里用 INTEGER 存布尔，PG 不接受 Python bool，这里统一转 1/0。"""
    out = []
    for p in params:
        if isinstance(p, bool):
            out.append(int(p))
        else:
            out.append(p)
    return tuple(out)


# --------------------------------------------------------------- 后端选择

class Backend:
    name = "sqlite"

    def connect(self) -> Any:  # pragma: no cover - 抽象
        raise NotImplementedError

    def describe(self) -> str:  # pragma: no cover - 抽象
        return self.name

    def close(self) -> None:
        """切库/关服时释放后端持有的资源（只有 PostgreSQL 有连接池）。"""


class SqliteBackend(Backend):
    name = "sqlite"

    def __init__(self, path: Path) -> None:
        self.path = path

    def connect(self) -> SqliteConn:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        return SqliteConn(self.path)

    def describe(self) -> str:
        return f"SQLite · {self.path}"


class PostgresBackend(Backend):
    """PostgreSQL 用连接池：线程从池里借连接，用完归还，省掉反复握手。

    池是懒创建的，一个进程一个池；切库时 close() 会把池关掉（异步，不阻塞请求）。
    """

    name = "postgres"

    def __init__(self, cfg: dict) -> None:
        self.cfg = cfg
        self._pool = None
        self._lock = threading.Lock()

    def _configure(self, conn) -> None:
        from psycopg.rows import dict_row

        conn.row_factory = dict_row
        schema = (self.cfg.get("schema") or "").strip()
        if schema and _IDENT.match(schema):
            with conn.cursor() as cur:
                cur.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
                cur.execute(f'SET search_path TO "{schema}"')
            conn.commit()

    def _get_pool(self):
        if self._pool is not None:
            return self._pool
        with self._lock:
            if self._pool is None:
                try:
                    from psycopg_pool import ConnectionPool
                except ImportError as e:  # pragma: no cover
                    raise DatabaseError("未安装 psycopg-pool，请执行 uv sync") from e
                from psycopg.conninfo import make_conninfo

                params = pg_params(self.cfg)
                params.pop("connect_timeout", None)
                # search_path 写进连接参数，池回收连接时 RESET ALL 也不会把它抹掉
                schema = (self.cfg.get("schema") or "").strip()
                if schema and _IDENT.match(schema) and schema != "public":
                    params["options"] = f'-c search_path="{schema}"'
                self._pool = ConnectionPool(
                    conninfo=make_conninfo(**params),
                    min_size=0,
                    max_size=int(self.cfg.get("pool_size") or 10),
                    timeout=6,
                    kwargs={"connect_timeout": 6},
                    configure=self._configure,
                    open=True,
                    name="recall-pg",
                )
        return self._pool

    def connect(self) -> PgConn:
        pool = self._get_pool()
        try:
            raw = pool.getconn(timeout=6)
        except Exception as e:  # noqa: BLE001 - 池超时/连不上都归为数据库不可用
            from psycopg_pool import PoolTimeout

            if isinstance(e, PoolTimeout):
                # 池被占满（或连接一直建不起来）。临时直连一次：
                # 连得上就照常干活，连不上就让真正的错误（08001/28P01）浮出来，
                # 而不是给用户一句 "couldn't get a connection" 干等 6 秒。
                print("[db] 连接池暂时取不到连接，改用临时直连")
                return PgConn(self.cfg)
            raise DatabaseError(f"从连接池取连接失败：{pg_error_text(e)}") from e
        if raw.closed:
            pool.putconn(raw, close=True)
            raise DatabaseError("从连接池取到的连接已失效，请重试")
        return PgConn(
            self.cfg,
            raw=raw,
            release=lambda: pool.putconn(raw),
            discard=lambda: pool.putconn(raw, close=True),
        )

    def close(self) -> None:
        pool, self._pool = self._pool, None
        if pool is None:
            return

        def _shutdown() -> None:
            try:
                pool.close()
            except Exception:  # noqa: BLE001
                pass

        # 池里有别的线程正在用的连接，close() 会等它们归还；放后台线程里等，别卡请求
        threading.Thread(target=_shutdown, name="pg-pool-close", daemon=True).start()

    def describe(self) -> str:
        p = pg_params(self.cfg)
        return f"PostgreSQL · {p['user']}@{p['host']}:{p['port']}/{p['dbname']}"


def build_backend(cfg: dict, sqlite_path: Path) -> Backend:
    if (cfg.get("driver") or "sqlite").lower() == "postgres":
        return PostgresBackend(cfg)
    return SqliteBackend(sqlite_path)


# ------------------------------------------------------------- 管理型操作

def probe(cfg: dict) -> dict:
    """测试连接：返回服务端版本。"""
    conn = PgConn(cfg)
    try:
        return {"ok": True, "version": conn.ping()}
    finally:
        conn.close()


def probe_server(cfg: dict) -> dict:
    """只关心服务器能不能连上，以及目标库是否存在（不存在也算「服务器可达」）。"""
    psycopg = _psycopg()
    params = pg_params(cfg)
    try:
        conn = psycopg.connect(**params)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT current_setting('server_version') AS v, current_database() AS db")
                row = cur.fetchone()
            version = f"PostgreSQL {row[0]} / {row[1]}"
        finally:
            conn.close()
        return {"ok": True, "version": version, "databaseExists": True}
    except psycopg.Error as e:
        if not is_missing_database(e):
            return {"ok": False, "message": pg_error_text(e), "databaseExists": False}

    admin = dict(params, dbname="postgres")
    try:
        conn = psycopg.connect(**admin)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT current_setting('server_version')")
                server = cur.fetchone()[0]
        finally:
            conn.close()
    except psycopg.Error as e:
        return {
            "ok": False,
            "message": f"服务器可连接，但目标库不存在，且无法自动创建：{pg_error_text(e)}",
            "databaseExists": False,
        }

    return {
        "ok": True,
        "version": f"PostgreSQL {server} / {params['dbname']}（尚未创建）",
        "databaseExists": False,
    }


def ensure_database(cfg: dict) -> bool:
    """目标库不存在时用维护库（postgres）建一个，返回是否新建。"""
    psycopg = _psycopg()
    params = pg_params(cfg)
    target = params["dbname"]

    try:
        conn = psycopg.connect(**params)
        conn.close()
        return False
    except psycopg.OperationalError as e:
        if not is_missing_database(e):
            raise DatabaseError(pg_error_text(e)) from e

    admin = dict(params, dbname="postgres")
    try:
        with psycopg.connect(**admin, autocommit=True) as conn:
            with conn.cursor() as cur:
                cur.execute(f'CREATE DATABASE "{target.replace(chr(34), "")}"')
        return True
    except psycopg.Error as e:
        raise DatabaseError(
            f"数据库 {target} 不存在，且自动创建失败：{pg_error_text(e)}。"
            "请先在 PostgreSQL 里手动建库，或用有建库权限的账号。"
        ) from e


__all__ = [
    "Backend",
    "DatabaseError",
    "PgConn",
    "PostgresBackend",
    "SqliteBackend",
    "SqliteConn",
    "build_backend",
    "ensure_database",
    "pg_error_text",
    "pg_params",
    "probe",
]
