"""把 BOX0 的 240x240 彩色界面按 lcd_ui.c 的排版渲染成 PNG，不接屏也能预览。

用法：cd esp32 && python3 tools/preview_box0.py /tmp/box0.png
排版改动请同步 lcd_ui.c，这个脚本只是"镜子"。
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
    w, hh, adv = 5, 7, 6
    return {c: data[i * w:(i + 1) * w] for i, c in enumerate(chars)}, w, hh, adv


FONT, FW, FH, FADV = load_font()
W = H = 240

C_BG = (0x0882)
C_CARD = (0x1925)
C_FG = (0xFFFF)
C_DIM = (0x8CB4)
C_ACC = (0x1656)
C_WARN = (0xFD40)
C_BAD = (0xFA6A)
C_BAR = (0x3186)


def rgb565(v):
    r = (v >> 11) & 0x1F
    g = (v >> 5) & 0x3F
    b = v & 0x1F
    return (r << 3) | (r >> 2), (g << 2) | (g >> 4), (b << 3) | (b >> 2)


def rgb(c):
    if isinstance(c, tuple):
        return c
    return rgb565(c)


T_TITLE, T_BIG, T_MID, T_SMALL, TOP_H = 2, 5, 3, 2, 40


class Screen:
    def __init__(self):
        self.p = [[rgb(C_BG)] * W for _ in range(H)]

    def fill(self, x, y, w, h, c):
        c = rgb(c)
        for j in range(max(0, y), min(H, y + h)):
            for i in range(max(0, x), min(W, x + w)):
                self.p[j][i] = c

    def frame(self, x, y, w, h, c, t=1):
        self.fill(x, y, w, t, c)
        self.fill(x, y + h - t, w, t, c)
        self.fill(x, y, t, h, c)
        self.fill(x + w - t, y, t, h, c)

    def dot(self, cx, cy, r, c):
        c = rgb(c)
        for j in range(-r, r + 1):
            for i in range(-r, r + 1):
                if i * i + j * j <= r * r:
                    x, y = cx + i, cy + j
                    if 0 <= x < W and 0 <= y < H:
                        self.p[y][x] = c

    def text_w(self, s, scale):
        return max(0, len(s) * FADV * scale - scale) if s else 0

    def fit_scale(self, s, max_w, max_scale):
        for sc in range(max_scale, 1, -1):
            if self.text_w(s, sc) <= max_w:
                return sc
        return 1

    def text(self, x, y, scale, c, s):
        c = rgb(c)
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

    def rtext(self, xr, y, scale, c, s):
        self.text(xr - self.text_w(s, scale), y, scale, c, s)


def top_bar(cv, m, accent):
    cv.fill(0, 0, W, TOP_H, C_CARD)
    cv.fill(0, TOP_H - 2, W, 2, accent)
    cv.text(10, (TOP_H - 2 - 7 * T_TITLE) // 2, T_TITLE, C_FG, m["title"])
    if m["code"] and m["kind"] != "prov":
        cv.rtext(W - 10, (TOP_H - 2 - 7 * T_SMALL) // 2, T_SMALL, C_DIM, m["code"])


def footer(cv, s, color):
    if s:
        sc = cv.fit_scale(s, W - 20, T_SMALL)
        cv.ctext(H - 7 * sc - 8, sc, color, s)


def word_screen(cv, m, word, color):
    sc = cv.fit_scale(word, W - 40, T_BIG)
    cv.ctext(58, sc, color, word)
    if m["line"]:
        s2 = cv.fit_scale(m["line"], W - 32, T_MID)
        cv.ctext(58 + 7 * sc + 22, s2, C_FG, m["line"])
    footer(cv, m["hint"], C_DIM)


def render(m):
    cv = Screen()
    if m["kind"] == "prov":
        top_bar(cv, m, C_ACC)
        y = TOP_H + 12
        cv.ctext(y, T_SMALL, C_DIM, "CONNECT TO WIFI")
        y += 7 * T_SMALL + 10
        cx, cw, ch = 14, W - 28, 7 * T_BIG + 16
        cv.fill(cx, y, cw, ch, C_CARD)
        cv.frame(cx, y, cw, ch, C_ACC, 2)
        sc = cv.fit_scale(m["line"], cw - 24, T_BIG)
        cv.ctext(y + (ch - 7 * sc) // 2, sc, C_ACC, m["line"])
        y += ch + 12
        cv.ctext(y, T_SMALL, C_DIM, "OPEN IN BROWSER")
        y += 7 * T_SMALL + 6
        s2 = cv.fit_scale(m["hint"], W - 32, T_MID)
        cv.ctext(y, s2, C_FG, m["hint"])
        footer(cv, "2.4G WIFI ONLY", C_DIM)
    elif m["kind"] == "rec":
        top_bar(cv, m, C_BAD)
        t = "02:41"
        cv.dot(W // 2 - cv.text_w(t, T_BIG) // 2 - 22, TOP_H + 24 + 7 * T_BIG // 2, 11, C_BAD)
        cv.ctext(TOP_H + 24, T_BIG, C_FG, t)
        cells, bw, gap = 20, 8, 1
        total = cells * (bw + gap) - gap
        bx = (W - total) // 2
        by = TOP_H + 24 + 7 * T_BIG + 18
        for i in range(cells):
            c = C_BAR if i >= 13 else (C_BAD if False else C_ACC)
            cv.fill(bx + i * (bw + gap), by, bw, 14, c)
        footer(cv, m["hint"], C_DIM)
    elif m["kind"] == "upload":
        top_bar(cv, m, C_ACC)
        cv.ctext(TOP_H + 12, T_BIG, C_FG, m["line"])
        bw, bh = W - 40, 18
        bx, by = 20, TOP_H + 12 + 7 * T_BIG + 16
        cv.frame(bx, by, bw, bh, C_ACC, 2)
        cv.fill(bx + 3, by + 3, (bw - 6) * 2 // 3, bh - 6, C_ACC)
        cv.ctext(by + bh + 10, T_MID, C_DIM, "2/3")
        footer(cv, m["hint"], C_DIM)
    elif m["kind"] == "pair":
        top_bar(cv, m, C_WARN)
        y = TOP_H + 12
        cv.ctext(y, T_SMALL, C_DIM, "DEVICE CODE")
        y += 7 * T_SMALL + 10
        sc = cv.fit_scale(m["code"], W - 40, T_BIG)
        cv.ctext(y, sc, C_FG, m["code"])
        y += 7 * sc + 14
        cv.ctext(y, T_MID, C_WARN, m["line"])
        footer(cv, m["hint"], C_DIM)
    elif m["kind"] == "ready":
        top_bar(cv, m, C_ACC)
        y = TOP_H + 18
        big = cv.fit_scale("HOLD 0.7S", W - 16, T_BIG)
        cv.ctext(y, big, C_FG, "HOLD 0.7S")
        y += 7 * big + 12
        cv.ctext(y, T_MID, C_ACC, "TO RECORD")
        y += 7 * T_MID + 14
        cv.ctext(y, T_SMALL, C_DIM, m["line"])
        y += 7 * T_SMALL + 8
        cv.ctext(y, T_MID, C_ACC, m["hint"])
    elif m["kind"] == "done":
        top_bar(cv, m, C_ACC)
        word_screen(cv, m, "DONE", C_ACC)
    elif m["kind"] == "err":
        top_bar(cv, m, C_BAD)
        word_screen(cv, m, "ERROR", C_BAD)
    return cv


SCREENS = [
    {"kind": "prov", "title": "WIFI", "code": "", "line": "RECALL-485412", "hint": "192.168.4.1"},
    {"kind": "pair", "title": "PAIR", "code": "485412", "line": "NO WIFI", "hint": "ADD IN WEB UI"},
    {"kind": "ready", "title": "READY", "code": "485412", "line": "FREE 12000KB", "hint": "PAIRED"},
    {"kind": "rec", "title": "REC", "code": "485412", "line": "", "hint": "HOLD TO STOP / V58S"},
    {"kind": "upload", "title": "SEND", "code": "485412", "line": "UPLOAD", "hint": "KEEP POWER ON"},
    {"kind": "done", "title": "SENT", "code": "485412", "line": "2 FILES 180.3KB", "hint": "TRANSCRIBING"},
    {"kind": "err", "title": "ERR", "code": "485412", "line": "NO WIFI", "hint": "KEPT IN FLASH"},
]


def write_png(path, screens):
    scale, pad = 2, 10
    cols = len(screens)
    tw = cols * (W * scale) + (cols + 1) * pad
    th = H * scale + 2 * pad
    img = [[(255, 255, 255)] * tw for _ in range(th)]
    for i, m in enumerate(screens):
        cv = render(m)
        ox = pad + i * (W * scale + pad)
        for y in range(H):
            for x in range(W):
                c = cv.p[y][x]
                for dy in range(scale):
                    for dx in range(scale):
                        img[pad + y * scale + dy][ox + x * scale + dx] = c

    raw = b"".join(
        b"\x00" + bytes(v for px in row for v in px) for row in img
    )

    def chunk(tag, data):
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    out = b"\x89PNG\r\n\x1a\n"
    out += chunk(b"IHDR", struct.pack(">IIBBBBB", tw, th, 8, 2, 0, 0, 0))
    out += chunk(b"IDAT", zlib.compress(raw, 9))
    out += chunk(b"IEND", b"")
    Path(path).write_bytes(out)


def main():
    out = sys.argv[1] if len(sys.argv) > 1 else "/tmp/box0.png"
    write_png(out, SCREENS)
    print(f"OK {out}")


if __name__ == "__main__":
    main()
