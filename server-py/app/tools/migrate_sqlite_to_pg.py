"""把内置 SQLite 的数据搬到 PostgreSQL。

网页上的「切换数据库」不做迁移（新库是空的），需要保留旧数据时用这个脚本：

    cd server-py
    .venv/bin/python -m app.tools.migrate_sqlite_to_pg \
        --url postgresql://postgres:secret@127.0.0.1:5432/recall

常用参数：
    --sqlite PATH    源 SQLite 文件，默认 <DATA_DIR>/recall.db
    --from-config    目标库直接用 data/config.json 里已保存的 PostgreSQL 配置
    --force          目标库已有数据时先清空再导入（危险，会删目标库的数据）
    --dry-run        只统计，不写入
    --batch N        每批插入行数，默认 300

导入完成后建议把 `data/uploads/` 一并拷到新机器（音频不在数据库里）。
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path
from urllib.parse import unquote, urlparse

from ..database import ensure_database, pg_error_text
from ..db_config import SQLITE_PATH, load_db_config
from ..db_migrate import run_migrations

# 按外键依赖排序，先父后子
TABLES = [
    "users",
    "api_keys",
    "providers",
    "recordings",
    "chunks",
    "summaries",
    "jobs",
    "settings",
    "devices",
    "device_claims",
    "app_meta",
]


def _cfg_from_url(url: str) -> dict:
    parsed = urlparse(url)
    if parsed.scheme not in ("postgres", "postgresql"):
        raise SystemExit(f"只支持 postgresql:// 形式的地址，收到：{parsed.scheme or url}")
    return {
        "driver": "postgres",
        "host": parsed.hostname or "127.0.0.1",
        "port": parsed.port or 5432,
        "database": (parsed.path or "/recall").lstrip("/") or "recall",
        "user": unquote(parsed.username or "postgres"),
        "password": unquote(parsed.password or ""),
        "sslmode": "prefer",
        "schema": "public",
    }


def _sqlite_rows(path: Path, table: str) -> tuple[list[str], list[tuple]]:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(f"SELECT * FROM {table}").fetchall()
        columns = list(rows[0].keys()) if rows else [
            r["name"] for r in conn.execute(f"PRAGMA table_info({table})")
        ]
        return columns, [tuple(r) for r in rows]
    finally:
        conn.close()


def _sqlite_tables(path: Path) -> set[str]:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="SQLite → PostgreSQL 数据迁移")
    ap.add_argument("--url", help="目标库 postgresql://user:pass@host:port/db")
    ap.add_argument("--from-config", action="store_true", help="用 data/config.json 里的 PostgreSQL 配置")
    ap.add_argument("--sqlite", default=str(SQLITE_PATH), help="源 SQLite 文件")
    ap.add_argument("--force", action="store_true", help="目标库非空时先清空")
    ap.add_argument("--dry-run", action="store_true", help="只统计不写入")
    ap.add_argument("--batch", type=int, default=300)
    args = ap.parse_args(argv)

    src = Path(args.sqlite).expanduser()
    if not src.is_file():
        print(f"找不到源文件：{src}")
        return 2

    if args.url:
        cfg = _cfg_from_url(args.url)
    elif args.from_config:
        cfg = load_db_config()
        if cfg["driver"] != "postgres":
            print("data/config.json 里当前不是 PostgreSQL，先用 --url 指定目标库")
            return 2
    else:
        ap.print_help()
        return 2

    import psycopg
    from psycopg.rows import dict_row

    print(f"源：{src}")
    print(f"目标：{cfg['user']}@{cfg['host']}:{cfg['port']}/{cfg['database']}")

    existing = _sqlite_tables(src)
    tables = [t for t in TABLES if t in existing]
    print(f"待迁移表：{', '.join(tables)}")

    ensure_database(cfg)
    run_migrations(cfg)  # 目标库结构与当前版本对齐

    params = {
        "host": cfg["host"],
        "port": cfg["port"],
        "dbname": cfg["database"],
        "user": cfg["user"],
        "password": cfg["password"],
        "sslmode": cfg.get("sslmode") or "prefer",
        "connect_timeout": 8,
    }

    total = 0
    ok = True
    try:
        with psycopg.connect(**params, row_factory=dict_row) as conn:
            counts = {
                t: conn.execute(f"SELECT COUNT(*) AS n FROM {t}").fetchone()["n"] for t in tables
            }
            occupied = {t: n for t, n in counts.items() if n}
            if occupied and not args.force and not args.dry_run:
                print("\n目标库已有数据，已中止：")
                for t, n in occupied.items():
                    print(f"  {t}: {n} 行")
                print("确需覆盖请加 --force（会清空这些表）")
                return 3

            if args.dry_run:
                print("\n[dry-run] 源库行数：")
                for t in tables:
                    _, rows = _sqlite_rows(src, t)
                    total += len(rows)
                    print(f"  {t}: {len(rows)} 行")
                print(f"合计 {total} 行，未写入目标库")
                return 0

            if occupied:
                with conn.cursor() as cur:
                    cur.execute(f"TRUNCATE {', '.join(tables)} CASCADE")
                conn.commit()
                print(f"已清空目标库 {len(occupied)} 张表")

            for table in tables:
                columns, rows = _sqlite_rows(src, table)
                if not rows:
                    print(f"  {table}: 0 行（跳过）")
                    continue
                placeholders = ", ".join(["%s"] * len(columns))
                collist = ", ".join(f'"{c}"' for c in columns)
                sql = f'INSERT INTO {table} ({collist}) VALUES ({placeholders})'
                with conn.cursor() as cur:
                    for i in range(0, len(rows), args.batch):
                        cur.executemany(sql, rows[i : i + args.batch])
                conn.commit()
                total += len(rows)
                print(f"  {table}: {len(rows)} 行")

            print("\n校验：")
            ok = True
            for table in tables:
                _, rows = _sqlite_rows(src, table)
                got = conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
                flag = "✓" if got == len(rows) else "✗"
                if got != len(rows):
                    ok = False
                print(f"  {flag} {table}: 源 {len(rows)} / 目标 {got}")
    except psycopg.Error as e:
        print(f"\n导入失败：{pg_error_text(e)}")
        return 1

    print(f"\n完成，共 {total} 行。别忘了把 data/uploads/ 一起拷过去。")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
