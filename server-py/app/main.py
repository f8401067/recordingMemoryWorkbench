from __future__ import annotations

import contextlib
import sys
import threading

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse

from .config import (
    ADMIN_PASSWORD,
    ADMIN_USER,
    ASR_CONCURRENCY,
    ENV_FILE,
    HOST,
    PORT,
    SUMMARY_CONCURRENCY,
    WEB_DIST,
)
from . import logbuf
from .db import all_, close_backend, init_db, one, probe_status
from .jobs import queue_stats, register, start_worker, stop_worker
from .routes import (
    actions,
    auth,
    devices,
    finder,
    local_asr,
    mcp,
    mcp_service,
    open as open_routes,
    providers,
    recordings,
    setup,
    summaries,
    system,
    voice_profiles,
)
from .services.devices import start_sweeper, stop_sweeper
from .services.diarize import handle_diarize_job
from .services.summarize import handle_revise_job, handle_summarize_job
from .services.transcribe import handle_transcribe_job
from .services.transcript_value import backfill_no_topic, backfill_unchecked
from .services.users import create_user

# 尽早劫持 stdout/stderr：设置页的「日志」标签要看到 print、异常栈和 uvicorn 输出
logbuf.install()


def _backfill_values() -> None:
    try:
        n = backfill_unchecked()
        if n:
            print(f"[value] 已为 {n} 条历史录音补齐内容判定", flush=True)
        m = backfill_no_topic()
        if m:
            print(f"[value] 已把 {m} 条 AI 判定无话题的历史录音收进折叠组", flush=True)
    except Exception as exc:  # noqa: BLE001 - 后台补算，失败不影响服务
        print(f"[value] 历史录音补算失败：{exc}", flush=True)


def _backfill_diarization() -> None:
    """给开关打开前就转写完成的录音补排队；已分离/正在排队的会跳过。"""
    try:
        from .services.diarize import enqueue_diarize
        from .services.settings import read_settings

        total = 0
        for user in all_("SELECT id FROM users"):
            if not read_settings(user["id"]).get("speakerDiarization"):
                continue
            rows = all_(
                """SELECT r.id FROM recordings r
                   WHERE r.user_id = ? AND r.status = 'ready' AND r.diarized_at IS NULL
                     AND EXISTS (SELECT 1 FROM chunks c
                                 WHERE c.recording_id = r.id AND c.status = 'done')""",
                (user["id"],),
            )
            for row in rows:
                enqueue_diarize(row["id"])
                total += 1
        if total:
            print(f"[diarize] 已为 {total} 条历史录音排队补跑说话人分离", flush=True)
    except Exception as exc:  # noqa: BLE001 - 补跑失败不影响服务启动
        print(f"[diarize] 历史录音补跑入队失败：{exc}", flush=True)


@contextlib.asynccontextmanager
async def lifespan(_: FastAPI):
    db_ok = True
    try:
        init_db()
    except Exception as exc:  # noqa: BLE001 - 数据库连不上也要能把服务启起来，好让页面说清原因
        db_ok = False
        print(f"[db] 初始化失败：{exc}")
        print(f"[db] 数据库只从配置文件读取：{ENV_FILE}（容器部署改 docker-compose 的环境变量）")
        print('[db] 期望内容形如：DATABASE_URL=postgresql://用户:密码@127.0.0.1:5432/recall?sslmode=disable')

    register("transcribe", handle_transcribe_job, ASR_CONCURRENCY)
    register("summarize", handle_summarize_job, SUMMARY_CONCURRENCY)
    register("revise_summary", handle_revise_job, SUMMARY_CONCURRENCY)
    register("diarize", handle_diarize_job, 1)

    if db_ok:
        start_worker(1.0)
        # 设备推流是"来一段算一段"的，静默超时后自动收尾 + 生成纪要
        start_sweeper(10.0)

        count = one("SELECT COUNT(*) AS n FROM users") or {"n": 0}
        if not count["n"]:
            create_user(ADMIN_USER, ADMIN_PASSWORD, "管理员")
            print(f"[init] 已创建初始账号：{ADMIN_USER} / {ADMIN_PASSWORD}（请尽快修改密码）")

        # 老录音补一次"有没有内容"的判定（新增该逻辑之前的数据 value_checked=0）
        threading.Thread(target=_backfill_values, daemon=True).start()
        # 开关打开之前转写完成的录音不会自动分离，启动时统一补排队（分离任务本身串行）
        threading.Thread(target=_backfill_diarization, daemon=True).start()

    print(f"[init] 忆录服务已启动 → http://localhost:{PORT}")
    yield

    stop_sweeper()
    stop_worker()
    close_backend()


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
async def http_error(request: Request, exc: HTTPException) -> JSONResponse:
    """统一错误体为 { error }，与前端 api() 的解析约定保持一致。"""
    if exc.status_code >= 500:
        logbuf.log("error", "api", f"{request.method} {request.url.path} → {exc.status_code} {exc.detail}")
    return JSONResponse({"error": exc.detail}, status_code=exc.status_code)


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    print(f"[warn] 请求参数校验失败：{exc.errors()}")
    logbuf.log("warn", "api", f"{request.method} {request.url.path} 参数不合法")
    return JSONResponse({"error": "请求参数不合法"}, status_code=422)


@app.exception_handler(Exception)
async def unhandled_error(request: Request, exc: Exception) -> JSONResponse:
    import traceback

    # 数据库层面的错误单独给出 503，前端可以据此提示去检查数据库配置
    if _is_db_error(exc):
        print(f"[db] 请求失败：{exc}")
        logbuf.log("error", "db", f"{request.method} {request.url.path} 数据库不可用：{str(exc)[:300]}")
        return JSONResponse(
            {"error": f"数据库不可用：{str(exc)[:200]}", "code": "db_unavailable"},
            status_code=503,
        )

    traceback.print_exc()
    logbuf.log("error", "api", f"{request.method} {request.url.path} 未处理异常：{str(exc)[:300]}")
    return JSONResponse({"error": f"服务内部错误：{str(exc)[:200]}"}, status_code=500)


def _is_db_error(exc: Exception) -> bool:
    from .database import DatabaseError

    if isinstance(exc, DatabaseError):
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
            "driver": "postgres",
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
app.include_router(recordings.router)
app.include_router(actions.router)
app.include_router(finder.router)
app.include_router(summaries.router)
app.include_router(providers.router)
app.include_router(mcp.router)
app.include_router(mcp_service.mgmt)
app.include_router(mcp_service.proto)
app.include_router(local_asr.router)
app.include_router(voice_profiles.router)
app.include_router(devices.router)
app.include_router(open_routes.router)
app.include_router(system.router)


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
