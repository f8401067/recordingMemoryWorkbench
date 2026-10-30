"""SQL 方言转换工具。

项目里的 SQL 一直按 SQLite 的 `?` 占位符书写，为了让同一份语句也能跑在 PostgreSQL
上，这里做两件事：

1. `translate()`：`?` → `%s`，并把字面量 `%` 转义成 `%%`（psycopg 的 pyformat 需求）；
2. `split_statements()`：把 `schema.sql` 这种多语句脚本切成单条，供不支持
   `executescript()` 的驱动逐条执行。

两者都是逐字符扫描的，会跳过字符串字面量、注释和 PostgreSQL 的 $tag$ 引用，
避免把 `'?'`、`'100%%'` 这类内容误伤。
"""

from __future__ import annotations

import re

_NORMAL = "normal"
_SINGLE = "single"
_DOUBLE = "double"
_LINE = "line"
_BLOCK = "block"

_DOLLAR_TAG = re.compile(r"\$[A-Za-z_][A-Za-z0-9_]*\$|\$\$")


def _dollar_end(sql: str, i: int) -> int:
    """若 i 处是 $tag$/$$ 引用的开头，返回闭合标签的结束位置，否则 -1。"""
    m = _DOLLAR_TAG.match(sql, i)
    if not m:
        return -1
    tag = m.group(0)
    close = sql.find(tag, m.end())
    return close + len(tag) if close >= 0 else -1


def translate(sql: str) -> str:
    """把 SQLite 风格的占位符改写成 psycopg 的 `%s`。"""
    out: list[str] = []
    i = 0
    n = len(sql)
    state = _NORMAL
    while i < n:
        ch = sql[i]
        nxt = sql[i + 1] if i + 1 < n else ""

        if state == _NORMAL:
            if ch == "'":
                state = _SINGLE
                out.append(ch)
            elif ch == '"':
                state = _DOUBLE
                out.append(ch)
            elif ch == "-" and nxt == "-":
                state = _LINE
                out.append("--")
                i += 2
                continue
            elif ch == "/" and nxt == "*":
                state = _BLOCK
                out.append("/*")
                i += 2
                continue
            elif ch == "$":
                end = _dollar_end(sql, i)
                if end > 0:
                    out.append(sql[i:end])
                    i = end
                    continue
                out.append(ch)
            elif ch == "?":
                out.append("%s")
            elif ch == "%":
                out.append("%%")
            else:
                out.append(ch)

        elif state == _SINGLE:
            if ch == "'":
                if nxt == "'":
                    out.append("''")
                    i += 2
                    continue
                state = _NORMAL
                out.append(ch)
            else:
                out.append("%%" if ch == "%" else ch)

        elif state == _DOUBLE:
            if ch == '"':
                if nxt == '"':
                    out.append('""')
                    i += 2
                    continue
                state = _NORMAL
                out.append(ch)
            else:
                out.append("%%" if ch == "%" else ch)

        elif state == _LINE:
            out.append(ch)
            if ch == "\n":
                state = _NORMAL

        else:  # _BLOCK
            if ch == "*" and nxt == "/":
                out.append("*/")
                i += 2
                state = _NORMAL
                continue
            out.append(ch)

        i += 1

    return "".join(out)


def split_statements(sql: str) -> list[str]:
    """按分号切成单条语句，忽略字符串/注释里的分号。"""
    out: list[str] = []
    buf: list[str] = []
    i = 0
    n = len(sql)
    state = _NORMAL
    while i < n:
        ch = sql[i]
        nxt = sql[i + 1] if i + 1 < n else ""

        if state == _NORMAL:
            if ch == "'":
                state = _SINGLE
            elif ch == '"':
                state = _DOUBLE
            elif ch == "-" and nxt == "-":
                state = _LINE
            elif ch == "/" and nxt == "*":
                state = _BLOCK
            elif ch == "$":
                end = _dollar_end(sql, i)
                if end > 0:
                    buf.append(sql[i:end])
                    i = end
                    continue
            elif ch == ";":
                stmt = "".join(buf).strip()
                if stmt:
                    out.append(stmt)
                buf.clear()
                i += 1
                continue
        elif state == _SINGLE:
            if ch == "'":
                if nxt == "'":
                    buf.append("''")
                    i += 2
                    continue
                state = _NORMAL
        elif state == _DOUBLE:
            if ch == '"':
                if nxt == '"':
                    buf.append('""')
                    i += 2
                    continue
                state = _NORMAL
        elif state == _LINE:
            buf.append(ch)
            if ch == "\n":
                state = _NORMAL
            i += 1
            continue
        else:  # _BLOCK
            buf.append(ch)
            if ch == "*" and nxt == "/":
                buf.append("/")
                i += 2
                state = _NORMAL
                continue
            i += 1
            continue

        buf.append(ch)
        i += 1

    tail = "".join(buf).strip()
    if tail:
        out.append(tail)
    return out


__all__ = ["translate", "split_statements"]
