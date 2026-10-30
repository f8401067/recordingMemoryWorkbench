from __future__ import annotations

import os
import secrets
import uuid
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent.parent
load_dotenv(Path(__file__).resolve().parent.parent / ".env")


def _data_dir() -> Path:
    raw = os.getenv("DATA_DIR")
    p = Path(raw).expanduser() if raw else ROOT / "data"
    if not p.is_absolute():
        p = ROOT / p
    return p


DATA_DIR = _data_dir()
UPLOADS_DIR = DATA_DIR / "uploads"
TMP_DIR = DATA_DIR / "tmp"
WEB_DIST = ROOT / "web" / "dist"

for _d in (DATA_DIR, UPLOADS_DIR, TMP_DIR):
    _d.mkdir(parents=True, exist_ok=True)

PORT = int(os.getenv("PORT", "8787"))
HOST = os.getenv("HOST", "0.0.0.0")
JWT_SECRET = os.getenv("JWT_SECRET") or secrets.token_hex(32)
JWT_ALG = "HS256"
JWT_EXPIRE_DAYS = 30

MAX_CHUNK_BYTES = 64 * 1024 * 1024
ASR_CONCURRENCY = int(os.getenv("ASR_CONCURRENCY", "2"))
SUMMARY_CONCURRENCY = 2

ADMIN_USER = os.getenv("ADMIN_USER", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin123")


def uid(prefix: str = "") -> str:
    """与旧 Node 版本一致的 id 生成：前缀 + 20 位 hex。"""
    return prefix + uuid.uuid4().hex[:20]
