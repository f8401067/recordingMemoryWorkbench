"""把微雪 ePaper 板的 200x200 界面按 epd_ui.c 的排版渲染成 PNG。

用法：cd esp32 && python3 tools/preview_epd.py /tmp/epd.png
排版改动请同步 main/epd_ui.c，这个脚本只是"镜子"。
不接屏也能检查字有没有超出边界、有没有叠在一起。
"""
import re
import struct
import sys
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_font():
    h = (ROOT / "main" / "ui_font.h").read_text()
    chars = re.search(r'ui_font_chars\[\] = "([^"]*)"', h).group(1)
    data = [int(x, 16) for x in re.findall(r"0x([0-9A-Fa-f]{2})", h.split("ui_font_data[] = {")[1])]
    return {c: data[i * 5:(i + 1) * 5] for i, c in enumerate(chars)}


FONT = load_font()
FW, FH, FADV = 5, 7, 6
W = H = 200

WHITE = 1
BLACK = 0

T_TITLE, T_BIG, T_MID, T_SMALL, TOP_H = 2, 4, 3, 2, 20


class Screen:
    def __init__(self):
        self.p = [[WHITE] * W for _ in range(H)]

    def fill(self, x, y, w, h, c):
        for j in range(max(0, y), min(H, y + h)):
            for i in range(max(0, x), min(W, x + w)):
                self.p[j][i] = c

    def frame(self, x, y, w, h, c, t=1):
        self.fill(x, y, w, t, c)
        self.fill(x, y + h - t, w, t, c)
        self.fill(x, y, t, h, c)
        self.fill(x + w - t, y, t, h, c)

    def dot(self, cx, cy, r, c):
        for j in range(-r, r + 1):
            for i in range(-r, r + 1):
                if i * i + j * j <= r * r and 0 <= cx + i < W and 0 <= cy + j < H:
                    self.p[cy + j][cx + i] = c

    def text_w(self, s, scale):
        return max(0, len(s) * FADV * scale - scale) if s else 0

    def fit_scale(self, s, max_w, max_scale):
        for sc in range(max_scale, 1, -1):
            if self.text_w(s, sc) <= max_w:
                return sc
        return 1

    def text(self, x, y, scale, c, s):
        for ch in s.upper():
            g = FONT.get(ch)
            if g:
                for col in range(FW):
                    for row in range(FH):
                        if g[col] & (1 << row):
                            self.fill(x + col * scale, y + row * scale, scale, scale, c)
            x += FADV * scale

    def ctext(self, y, scale, c, s):
        self.text((W - self.text_w(s, scale)) // 2, y, scale, c, s)

    def cline(self, y, sc, s):
        self.ctext(y, sc, BLACK, s)
        return y + 7 * sc + 10

    def rtext(self, xr, y, scale, c, s):
        self.text(xr - self.text_w(s, scale), y, scale, c, s)


def top_bar(cv, m):
    cv.fill(0, 0, W, TOP_H, BLACK)
    cv.text(6, (TOP_H - 7 * T_TITLE) // 2, T_TITLE, WHITE, m["title"])
    if m["code"] and m["kind"] != "prov":
        cv.rtext(W - 6, (TOP_H - 7) // 2, 1, WHITE, m["code"])


def footer(cv, s):
    if s:
        sc = cv.fit_scale(s, W - 16, T_SMALL)
        cv.ctext(H - 7 * sc - 8, sc, BLACK, s)


def word_screen(cv, m, word):
    y = TOP_H + 16
    sc = cv.fit_scale(word, W - 40, T_BIG)
    y = cv.cline(y, sc, word)
    if m["line"]:
        y = cv.cline(y, cv.fit_scale(m["line"], W - 20, T_MID), m["line"])
    footer(cv, m["hint"])


def render(m):
    cv = Screen()
    k = m["kind"]

    if k == "prov":
        top_bar(cv, m)
        y = cv.cline(TOP_H + 24, T_SMALL, "CONNECT TO WIFI")
        cx, cw, ch = 10, W - 20, 7 * T_MID + 14
        cv.frame(cx, y, cw, ch, BLACK, 2)
        sc = cv.fit_scale(m["line"], cw - 16, T_MID)
        cv.ctext(y + (ch - 7 * sc) // 2, sc, BLACK, m["line"])
        y += ch + 12
        y = cv.cline(y, T_SMALL, "OPEN IN BROWSER")
        y = cv.cline(y, cv.fit_scale(m["hint"], W - 24, T_BIG), m["hint"])
        footer(cv, "2.4G WIFI ONLY")

    elif k == "pair":
        top_bar(cv, m)
        y = cv.cline(TOP_H + 12, T_SMALL, "DEVICE CODE")
        if m["code"]:
            y = cv.cline(y, cv.fit_scale(m["code"], W - 24, T_BIG), m["code"])
        if m["line"]:
            y = cv.cline(y, cv.fit_scale(m["line"], W - 20, T_MID), m["line"])
        footer(cv, m["hint"] or "ADD IN WEB UI")

    elif k == "rec":
        top_bar(cv, m)
        y = TOP_H + 24
        sc = cv.fit_scale("00:00", W - 60, T_BIG)
        tw = cv.text_w("00:00", sc)
        tx = (W - tw) // 2
        cv.dot(tx - 16, y + 7 * sc // 2, 7, BLACK)
        cv.text(tx, y, sc, BLACK, m["time"])
        y += 7 * sc + 14
        y = cv.cline(y, cv.fit_scale("HOLD TO STOP", W - 20, T_MID), "HOLD TO STOP")
        footer(cv, "SPEAKING? LED BLINKS")

    elif k == "upload":
        top_bar(cv, m)
        y = cv.cline(TOP_H + 16, T_BIG, m["line"])
        bw, bh = W - 40, 16
        bx = 20
        cv.frame(bx, y, bw, bh, BLACK, 2)
        cv.fill(bx + 3, y + 3, (bw - 6) * 2 // 3, bh - 6, BLACK)
        y += bh + 8
        cv.cline(y, T_MID, "2/3")
        footer(cv, m["hint"] or "KEEP POWER ON")

    elif k in ("done", "skip", "err"):
        top_bar(cv, m)
        word_screen(cv, m, {"done": "DONE", "skip": "SKIP", "err": "ERROR"}[k])

    else:  # ready
        top_bar(cv, m)
        y = cv.cline(TOP_H + 20, cv.fit_scale("HOLD 0.7S", W - 16, T_BIG), "HOLD 0.7S")
        y = cv.cline(y, T_MID, "TO RECORD")
        if m["line"]:
            y = cv.cline(y, cv.fit_scale(m["line"], W - 20, T_SMALL), m["line"])
        if m["hint"]:
            cv.cline(y, T_MID, m["hint"])

    return cv


def png(cv):
    raw = b"".join(b"\x00" + bytes(0 if v else 255 for v in row) for row in cv.p)

    def chunk(t, d):
        c = t + d
        return struct.pack(">I", len(d)) + c + struct.pack(">I", zlib.crc32(c) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", W, H, 8, 0, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9))
            + chunk(b"IEND", b""))


SCREENS = [
    dict(kind="prov", title="WIFI", code="170144", line="RECALL-170144",
         hint="192.168.4.1", time=""),
    dict(kind="pair", title="PAIR", code="170144", line="DEVICE",
         hint="ADD IN WEB UI", time=""),
    dict(kind="ready", title="READY", code="170144", line="FREE 5120KB",
         hint="UNPAIRED", time=""),
    dict(kind="rec", title="REC", code="170144", line="", hint="", time="02:40"),
    dict(kind="upload", title="SEND", code="170144", line="UPLOAD",
         hint="KEEP POWER ON", time=""),
    dict(kind="done", title="SENT", code="170144", line="2 FILES 180KB",
         hint="TRANSCRIBING", time=""),
    dict(kind="err", title="ERR", code="170144", line="NO WIFI",
         hint="KEPT IN FLASH", time=""),
]


def main():
    out = sys.argv[1] if len(sys.argv) > 1 else "/tmp/epd.png"
    cols, rowsx, pad = 4, 2, 8
    GW, GH = cols * W + (cols + 1) * pad, rowsx * H + (rowsx + 1) * pad
    grid = [[1] * GW for _ in range(GH)]

    for i, m in enumerate(SCREENS):
        cv = render(m)
        ox = pad + (i % cols) * (W + pad)
        oy = pad + (i // cols) * (H + pad)
        for y in range(H):
            grid[oy + y][ox:ox + W] = cv.p[y]

    raw = b"".join(b"\x00" + bytes(0 if v else 255 for v in row) for row in grid)

    def chunk(t, d):
        c = t + d
        return struct.pack(">I", len(d)) + c + struct.pack(">I", zlib.crc32(c) & 0xFFFFFFFF)

    data = (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", GW, GH, 8, 0, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9))
            + chunk(b"IEND", b""))
    Path(out).write_bytes(data)
    print("OK", out)


if __name__ == "__main__":
    main()
