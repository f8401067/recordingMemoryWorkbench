from __future__ import annotations

import contextlib
import sys

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse

from .config import (
    ADMIN_PASSWORD,
    ADMIN_USER,
    ASR_CONCURRENCY,
    HOST,
    PORT,
    SUMMARY_CONCURRENCY,
    WEB_DIST,
)
from .db import current_backend, get_conn, init_db, one, probe_status
from .db_config import SQLITE_PATH, load_db_config
from .db_health import (
    acquire_instance_lock,
    backup_snapshot,
    latest_backup as _latest_backup,
    quick_check,
    release_instance_lock,
)
from .jobs import queue_stats, register, start_worker, stop_worker
from .routes import auth, database, devices, local_asr, open, providers, recordings, setup, summaries
from .services.devices import start_sweeper, stop_sweeper
from .services.summarize import handle_summarize_job
from .services.transcribe import handle_transcribe_job
from .services.users import create_user


@contextlib.asynccontextmanager
async def lifespan(_: FastAPI):
    # 内置 SQLite 先做两道闸：同库只允许一个进程写、启动前体检。
    # 这两步挡住的正是「日志刷 disk I/O error 然后 malformed」那条路。
    if load_db_config()["driver"] == "sqlite":
        holder = acquire_instance_lock()
        if holder:
            raise SystemExit(
                "[db] 已经有一个忆录后端在用同一个 SQLite 文件，为免写坏数据，本次启动中止。\n"
                f"[db] 占用者：{holder.strip()}\n"
                "[db] 处理办法：先停掉旧进程（lsof -nP -i :8787 找到 PID），再启动。\n"
                "[db] 顺带提醒：SQLite 只能用单进程，不要开多 worker。"
            )
        broken = quick_check()
        if broken:
            latest = _latest_backup()
            hint = f"{latest}" if latest else "（暂无自动备份）"
            raise SystemExit(
                "[db] 数据库文件体检没通过，为避免越写越坏，本次启动中止。\n"
                f"[db] 文件：{SQLITE_PATH}\n"
                f"[db] SQLite 的说法：{broken}\n"
                f"[db] 最近一份自动备份：{hint}\n"
                "[db] 建议：把备份复制回 data/recall.db（同时删掉 -wal/-shm）再启动；"
                "或执行 `sqlite3 data/recall.db .recover` 抢救。"
            )

    db_ok = True
    try:
        init_db()
    except Exception as exc:  # noqa: BLE001 - 数据库连不上也要能把服务启起来，好让用户改配置
        db_ok = False
        print(f"[db] 初始化失败：{exc}")
        print("[db] 服务仍会启动，请打开网页在「设置 → 数据存储」里修正连接信息")

    if db_ok and load_db_config()["driver"] == "sqlite":
        try:
            snap = backup_snapshot()
            if snap:
                print(f"[db] 已备份当前数据 → {snap}")
            else:
                print("[db] 本次没有生成备份（库文件还不存在？）")
        except Exception as exc:  # noqa: BLE001 - 备份失败不该拦住启动，但必须说出来
            print(f"[db] 备份失败：{exc}")
            print("[db] 刚重建过库的话这是正常的；否则建议手工导出一份，别指望下次还能救回来。")

    register("transcribe", handle_transcribe_job, ASR_CONCURRENCY)
    register("summarize", handle_summarize_job, SUMMARY_CONCURRENCY)

    if db_ok:
        start_worker(1.0)
        # 设备推流是"来一段算一段"的，静默超时后自动收尾 + 生成纪要
        start_sweeper(10.0)

        count = one("SELECT COUNT(*) AS n FROM users") or {"n": 0}
        if not count["n"]:
            if load_db_config()["driver"] == "sqlite":
                create_user(ADMIN_USER, ADMIN_PASSWORD, "管理员")
                print(f"[init] 已创建初始账号：{ADMIN_USER} / {ADMIN_PASSWORD}（请尽快修改密码）")
            else:
                print("[init] PostgreSQL 中还没有任何账号，请打开网页完成初始化向导")

    print(f"[init] 忆录服务已启动 → http://localhost:{PORT}")
    yield

    stop_sweeper()
    stop_worker()
    with contextlib.suppress(Exception):
        if current_backend().name == "sqlite":
            conn = get_conn()
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            conn.commit()
    release_instance_lock()


app = FastAPI(
    title="忆录 Recall API",
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/api/docs",
    redoc_url=None,
    openapi_url="/api/openapi.json",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(HTTPException)
async def http_error(_: Request, exc: HTTPException) -> JSONResponse:
    """统一错误体为 { error }，与前端 api() 的解析约定保持一致。"""
    return JSONResponse({"error": exc.detail}, status_code=exc.status_code)


@app.exception_handler(RequestValidationError)
async def validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
    print(f"[warn] 请求参数校验失败：{exc.errors()}")
    return JSONResponse({"error": "请求参数不合法"}, status_code=422)


@app.exception_handler(Exception)
async def unhandled_error(_: Request, exc: Exception) -> JSONResponse:
    import traceback

    # 数据库层面的错误单独给出 503，前端可以据此提示「去改数据库配置」
    if _is_db_error(exc):
        print(f"[db] 请求失败：{exc}")
        text = str(exc).lower()
        if "malformed" in text or "not a database" in text:
            # 文件级损坏（多半是多个进程同时写过同一个 SQLite）。
            # 这里只报错不再重试，免得越写越坏。
            return JSONResponse(
                {
                    "error": "数据库文件已损坏（database disk image is malformed），请先停止服务，"
                    "用 data/backups 里最近一份备份覆盖 data/recall.db，或执行 "
                    "`sqlite3 data/recall.db .recover` 抢救。",
                    "code": "db_corrupted",
                },
                status_code=503,
            )
        return JSONResponse(
            {"error": f"数据库不可用：{str(exc)[:200]}", "code": "db_unavailable"},
            status_code=503,
        )

    traceback.print_exc()
    return JSONResponse({"error": f"服务内部错误：{str(exc)[:200]}"}, status_code=500)


def _is_db_error(exc: Exception) -> bool:
    import sqlite3

    from .database import DatabaseError

    if isinstance(exc, (DatabaseError, sqlite3.Error)):
        return True
    try:  # psycopg 没装时上面就够用了
        import psycopg

        return isinstance(exc, psycopg.Error)
    except ImportError:
        return False


@app.get("/api/health")
def health() -> dict:
    status = probe_status()
    return {
        "ok": True,
        "version": "1.0.0",
        "runtime": "python",
        "db": {
            "driver": status["driver"],
            "target": status["target"],
            "connected": status["connected"],
            "initialized": status["initialized"],
            "needsAdmin": status["needsAdmin"],
        },
        "queue": queue_stats() if status["connected"] else [],
    }


app.include_router(auth.router)
app.include_router(auth.key_router)
app.include_router(setup.router)
app.include_router(database.router)
app.include_router(recordings.router)
app.include_router(summaries.router)
app.include_router(providers.router)
app.include_router(local_asr.router)
app.include_router(devices.router)
app.include_router(open.router)


@app.get("/{full_path:path}")
def spa(full_path: str):
    """静态前端 + SPA 回退。"""
    if full_path.startswith("api/"):
        raise HTTPException(404, "接口不存在")

    if full_path:
        candidate = (WEB_DIST / full_path).resolve()
        try:
            inside = candidate.is_relative_to(WEB_DIST.resolve())
        except AttributeError:  # Python < 3.9
            inside = str(candidate).startswith(str(WEB_DIST.resolve()))
        if inside and candidate.is_file():
            return FileResponse(candidate)

    index = WEB_DIST / "index.html"
    if index.is_file():
        return FileResponse(index, headers={"Cache-Control": "no-cache"})

    return JSONResponse(
        {
            "message": "前端尚未构建，请在 web/ 下执行 npm run build，或用 npm run dev 启动开发服务器。"
        }
    )


def main() -> None:
    # print() 重定向到文件时默认块缓冲，[db]/[init] 这些诊断信息会一直卡在缓冲区里，
    # 排查问题时看着像「什么都没打印」。改成行缓冲，落盘即时。
    with contextlib.suppress(Exception):
        sys.stdout.reconfigure(line_buffering=True)

    uvicorn.run(
        "app.main:app",
        host=HOST,
        port=PORT,
        reload=False,
        log_level="info",
        access_log=False,
    )


if __name__ == "__main__":
    main()
