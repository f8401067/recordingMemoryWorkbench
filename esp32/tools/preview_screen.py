"""把 display.c 的三种界面用同一套字库渲染成 PNG，方便不接屏也能预览。

用法：cd esp32 && python3 tools/preview_screen.py /tmp/screens.png
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
CW, CH = 128, 64


class Canvas:
    def __init__(self):
        self.p = [[0] * CW for _ in range(CH)]

    def px(self, x, y):
        if 0 <= x < CW and 0 <= y < CH:
            self.p[y][x] = 1

    def hline(self, x0, x1, y):
        for x in range(x0, x1 + 1):
            self.px(x, y)

    def rect(self, x, y, w, h):
        for j in range(h):
            self.hline(x, x + w - 1, y + j)

    def text(self, x, y, scale, s):
        for ch in s.upper():
            g = FONT.get(ch)
            if g:
                for col in range(FW):
                    for row in range(FH):
                        if g[col] & (1 << row):
                            if scale == 1:
                                self.px(x + col, y + row)
                            else:
                                self.rect(x + col * scale, y + row * scale, scale, scale)
            x += FADV * scale

    @staticmethod
    def tw(s, scale):
        return max(0, len(s) * FADV * scale - scale)

    def ctext(self, y, scale, s):
        self.text((CW - self.tw(s, scale)) // 2, y, scale, s)

    def rtext(self, xr, y, scale, s):
        self.text(xr - self.tw(s, scale), y, scale, s)


def render(kind, title, code, line, hint):
    cv = Canvas()
    cv.text(1, 1, 1, title)
    if code and kind != "prov":
        cv.rtext(CW - 1, 1, 1, code)
    cv.hline(0, CW - 1, 10)
    if kind == "prov":
        cv.ctext(13, 1, "CONNECT TO")
        w = cv.tw(line, 1)
        bx = (CW - w) // 2 - 3
        cv.hline(bx, bx + w + 5, 24)
        cv.hline(bx, bx + w + 5, 34)
        for y in range(24, 35):
            cv.px(bx, y)
            cv.px(bx + w + 5, y)
        cv.ctext(26, 1, line)
        cv.ctext(44, 1, hint)
        cv.ctext(56, 1, "2.4G ONLY")
    elif kind == "pair":
        cv.ctext(13, 2, "PAIR")
        cv.ctext(30, 3, code)
        cv.ctext(54, 1, hint)
    elif kind == "rec":
        cv.rect(5, 20, 9, 9)
        cv.ctext(15, 2, "02:41")
        cells = 20
        on = 13
        for i in range(cells):
            cv.rect(4 + i * 6, 46, 4, 8) if i < on else None
        cv.text(1, 56, 1, hint)
    elif kind == "ready":
        cv.ctext(14, 2, "HOLD 0.7S")
        cv.ctext(34, 1, "TO RECORD")
        cv.ctext(46, 1, line)
        cv.ctext(56, 1, hint)
    return cv


SCREENS = [
    ("prov", "WIFI", "", "RECALL-485412", "192.168.4.1"),
    ("pair", "PAIR", "485412", "", "ADD IN WEB UI"),
    ("ready", "READY", "485412", "FREE 12000KB", "PAIRED"),
    ("rec", "REC", "485412", "", "HOLD TO STOP / V58S"),
]


def write_png(path, pix):
    h, w = len(pix), len(pix[0])
    raw = b"".join(b"\x00" + bytes(r) for r in pix)

    def chunk(tag, data):
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    out = b"\x89PNG\r\n\x1a\n"
    out += chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 0, 0, 0, 0))
    out += chunk(b"IDAT", zlib.compress(raw, 9))
    out += chunk(b"IEND", b"")
    Path(path).write_bytes(out)


def main():
    out = sys.argv[1] if len(sys.argv) > 1 else "/tmp/screens.png"
    scale, pad = 5, 12
    cols = len(SCREENS)
    tw = cols * (CW * scale) + (cols + 1) * pad
    th = CH * scale + 2 * pad
    img = [[255] * tw for _ in range(th)]
    for i, (kind, title, code, line, hint) in enumerate(SCREENS):
        cv = render(kind, title, code, line, hint)
        ox = pad + i * (CW * scale + pad)
        oy = pad
        for y in range(CH):
            for x in range(CW):
                if cv.p[y][x]:
                    for dy in range(scale):
                        for dx in range(scale):
                            img[oy + y * scale + dy][ox + x * scale + dx] = 20
        for x in range(ox - 1, ox + CW * scale + 1):
            for y in (oy - 1, oy + CH * scale):
                if 0 <= x < tw and 0 <= y < th:
                    img[y][x] = 150
        for y in range(oy - 1, oy + CH * scale + 1):
            for x in (ox - 1, ox + CW * scale):
                if 0 <= x < tw and 0 <= y < th:
                    img[y][x] = 150
    write_png(out, img)
    print("wrote", out, tw, "x", th)


if __name__ == "__main__":
    main()
