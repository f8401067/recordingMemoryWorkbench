# ---------- 前端构建 ----------
FROM node:22-slim AS web-builder
WORKDIR /app/web
COPY web/package*.json ./
RUN npm install --no-audit --no-fund
COPY web/ ./
RUN npm run build

# ---------- 后端运行时 ----------
FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# 用 uv 安装依赖，和本地开发保持一致
RUN pip install --no-cache-dir uv

# 本地开源 ASR（sherpa-onnx + faster-whisper）默认不装，镜像小一半；需要时
# docker build --build-arg INSTALL_LOCAL_ASR=1 ...
ARG INSTALL_LOCAL_ASR=0

WORKDIR /app/server-py
COPY server-py/pyproject.toml server-py/uv.lock ./
RUN if [ "$INSTALL_LOCAL_ASR" = "1" ]; then \
        uv sync --frozen --no-dev --extra local-asr; \
    else \
        uv sync --frozen --no-dev; \
    fi

COPY server-py/app ./app
COPY server-py/alembic.ini ./alembic.ini
COPY --from=web-builder /app/web/dist /app/web/dist

ENV PYTHONUNBUFFERED=1 \
    PORT=8787 \
    HOST=0.0.0.0 \
    DATA_DIR=/app/data

EXPOSE 8787
VOLUME ["/app/data"]

WORKDIR /app/server-py
CMD ["/app/server-py/.venv/bin/python", "-m", "app.main"]
