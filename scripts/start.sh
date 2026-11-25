#!/usr/bin/env bash
# 一键启动 Recall 工作台（后端 + 已构建前端，同一个端口）
#
# 说明：
#   - 后端 FastAPI 会直接托管 web/dist 里的静态页面，所以日常只需启动这一个进程。
#   - 源码有更新时会自动重新构建前端，不用手动 npm run build。
#   - 退出（Ctrl+C 或关窗口）会同时停掉后端。

set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT" || exit 1

# 双击启动时 PATH 可能不含 brew，补齐常见位置
export PATH="/opt/homebrew/bin:/usr/local/bin:$HOME/.local/bin:$PATH"

# 关键：某些工具（如 WorkBuddy / Claude CLI）会注入 PYTHONPATH，
# 里面的 sitecustomize 给 os.remove 挂了安全删除钩子，
# 会导致本地 ASR 子进程清理临时文件时直接 exit(1)（报 code=1 意外退出）。
unset PYTHONPATH PYTHONHOME PYTHONSTARTUP

say() { printf '%s\n' "$*"; }
die() {
  printf '\n❌ %s\n' "$*"
  if [ -t 0 ]; then printf '\n按回车关闭窗口…'; read -r _; fi
  exit 1
}

SERVER_PID=""
TAIL_PID=""

cleanup() {
  PIDS=""
  [ -n "${TAIL_PID:-}" ] && PIDS="$PIDS $TAIL_PID"
  [ -n "${SERVER_PID:-}" ] && PIDS="$PIDS $SERVER_PID"
  [ -z "$PIDS" ] && return 0

  # 只处理还活着的
  ALIVE=""
  for p in $PIDS; do kill -0 "$p" 2>/dev/null && ALIVE="$ALIVE $p"; done
  [ -z "$ALIVE" ] && return 0

  printf '\n'
  say "正在关闭服务…"
  for p in $ALIVE; do kill "$p" 2>/dev/null; done

  # 最多等 5 秒优雅退出；后端有时会被后台线程拖住，到点强杀，避免留下孤儿进程
  i=0
  while [ "$i" -lt 5 ]; do
    STILL=""
    for p in $ALIVE; do kill -0 "$p" 2>/dev/null && STILL="$STILL $p"; done
    [ -z "$STILL" ] && break
    sleep 1
    i=$((i + 1))
  done
  for p in $ALIVE; do kill -9 "$p" 2>/dev/null; done
  wait 2>/dev/null
  say "已停止。"
  return 0
}
trap cleanup EXIT INT TERM HUP

# ---------- 读取端口 ----------
PORT="$(grep -E '^PORT=' server-py/.env 2>/dev/null | tail -1 | cut -d= -f2 | tr -d '[:space:]')"
PORT="${PORT:-8787}"
URL="http://127.0.0.1:${PORT}"

# ---------- 依赖检查 ----------
[ -f server-py/.env ] || {
  cp server-py/.env.example server-py/.env
  say "已根据 .env.example 生成 server-py/.env"
}

if [ ! -d server-py/.venv ]; then
  command -v uv >/dev/null 2>&1 || die "缺少 uv，无法安装后端依赖。请先执行：brew install uv"
  say "首次运行：安装后端依赖…"
  (cd server-py && uv sync) || die "后端依赖安装失败"
fi

if [ ! -d web/node_modules ]; then
  say "首次运行：安装前端依赖…"
  (cd web && npm install) || die "前端依赖安装失败"
fi

command -v ffmpeg >/dev/null 2>&1 || say "⚠️  未检测到 ffmpeg，音频切片与格式转换会失败（brew install ffmpeg）"

# ---------- 数据库 ----------
DB_HOST="$(sed -n 's|^DATABASE_URL=.*@\([^:/]*\):[0-9]*/.*|\1|p' server-py/.env | tail -1)"
DB_PORT="$(sed -n 's|^DATABASE_URL=.*@[^:/]*:\([0-9]*\)/.*|\1|p' server-py/.env | tail -1)"
DB_HOST="${DB_HOST:-127.0.0.1}"
DB_PORT="${DB_PORT:-5432}"

db_up() { nc -z -G 1 "$DB_HOST" "$DB_PORT" >/dev/null 2>&1; }

if ! db_up; then
  say "⚠️  PostgreSQL ${DB_HOST}:${DB_PORT} 未就绪，尝试拉起对应容器…"
  C="$(docker ps -a --format '{{.Names}}	{{.Ports}}' 2>/dev/null \
        | awk -v p=":${DB_PORT}->" 'index($0, p) { print $1; exit }')"
  [ -n "${C:-}" ] && docker start "$C" >/dev/null 2>&1 && say "已启动容器：$C"
  for _ in $(seq 1 20); do db_up && break; sleep 1; done
  db_up || die "数据库连不上（${DB_HOST}:${DB_PORT}）。请先启动 PostgreSQL 再试。"
fi

# ---------- 前端构建（按需） ----------
NEED_BUILD=0
[ -f web/dist/index.html ] || NEED_BUILD=1
if [ "$NEED_BUILD" = 0 ]; then
  if find web/src web/index.html web/tailwind.config.js web/postcss.config.js web/vite.config.ts \
       -newer web/dist/index.html -print -quit 2>/dev/null | grep -q .; then
    NEED_BUILD=1
  fi
fi
if [ "$NEED_BUILD" = 1 ]; then
  say "前端源码有更新，正在构建…"
  (cd web && npm run build) || die "前端构建失败"
fi

# ---------- 已在运行？ ----------
if curl -s --noproxy '*' -f -m 2 -o /dev/null "${URL}/api/setup/status" 2>/dev/null; then
  say "服务已经在运行，直接打开浏览器：${URL}"
  [ "${NO_OPEN:-0}" = "1" ] || open "$URL"
  if [ -t 0 ]; then printf '（2 秒后关闭窗口）\n'; sleep 2; fi
  exit 0
fi

if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  die "端口 ${PORT} 被其它程序占用，请先释放它（lsof -nP -iTCP:${PORT} -sTCP:LISTEN）。"
fi

# ---------- 启动后端 ----------
mkdir -p data/logs
LOG="data/logs/server.log"
: >> "$LOG"
say "启动后端…（日志：${LOG}）"

(cd server-py && exec .venv/bin/python -m app.main) >> "$LOG" 2>&1 &
SERVER_PID=$!

READY=0
for _ in $(seq 1 60); do
  if ! kill -0 "$SERVER_PID" 2>/dev/null; then
    say ""
    say "后端启动失败，日志最后 40 行："
    tail -40 "$LOG"
    die "启动失败，请把上面的报错发给我。"
  fi
  if curl -s --noproxy '*' -f -m 1 -o /dev/null "${URL}/api/setup/status" 2>/dev/null; then
    READY=1
    break
  fi
  sleep 1
done
[ "$READY" = 1 ] || die "等了 60 秒后端还没就绪，看看日志：tail -50 ${LOG}"

say ""
say "✅ 已就绪：${URL}"
say "   账号见 server-py/.env 里的 ADMIN_USER / ADMIN_PASSWORD"
say "   按 Ctrl+C 或关闭窗口停止服务"
say ""

[ "${NO_OPEN:-0}" = "1" ] || open "$URL"

# 跟随日志，保持服务在前台运行。
# 用后台 + wait 而不是直接前台 tail：否则收到信号时被中断的命令会被 bash 重新执行一遍。
tail -f "$LOG" &
TAIL_PID=$!
wait "$TAIL_PID"
