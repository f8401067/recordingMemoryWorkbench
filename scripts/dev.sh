#!/usr/bin/env bash
# 本地开发：同时启动后端（8787）与前端热更新（5173）
set -e
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

cleanup() {
  echo ""
  echo "正在关闭服务…"
  kill $SERVER_PID $WEB_PID 2>/dev/null || true
  wait 2>/dev/null || true
}
trap cleanup INT TERM

if ! command -v uv >/dev/null 2>&1; then
  echo "未检测到 uv，正在通过 pip 安装…"
  python3 -m pip install --user uv
  export PATH="$HOME/.local/bin:$PATH"
fi

if [ ! -d "$ROOT/server-py/.venv" ]; then
  echo "首次运行，安装后端依赖…"
  (cd "$ROOT/server-py" && uv sync)
fi
if [ ! -d "$ROOT/web/node_modules" ]; then
  echo "首次运行，安装前端依赖…"
  (cd "$ROOT/web" && npm install)
fi
if [ ! -f "$ROOT/server-py/.env" ]; then
  cp "$ROOT/server-py/.env.example" "$ROOT/server-py/.env"
  echo "已生成 server-py/.env，可按需修改"
fi

if ! command -v ffmpeg >/dev/null 2>&1; then
  echo "⚠️  未检测到 ffmpeg，音频切片与格式转换会失败。macOS 可执行：brew install ffmpeg"
fi

echo ""
echo "  后端  http://localhost:8787   （FastAPI）"
echo "  前端  http://localhost:5173   ← 开发时访问这个"
echo ""

(cd "$ROOT/server-py" && .venv/bin/python -m app.main) &
SERVER_PID=$!

(cd "$ROOT/web" && npm run dev) &
WEB_PID=$!

wait
