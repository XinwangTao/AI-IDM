#!/usr/bin/env python3
"""生成扩展图标（纯标准库，无 PIL）：圆角蓝底 + 白色下载箭头。

    python browser-extension/icons/generate_icons.py
"""

import os
import struct
import zlib

BASE = 128
S = 4                     # 超采样倍数
RADIUS = 30               # 圆角半径（BASE 坐标）
TOP = (59, 130, 246)      # #3b82f6
BOTTOM = (29, 78, 216)    # #1d4ed8


def in_round_rect(x, y, r):
    if x < 0 or y < 0 or x >= BASE or y >= BASE:
        return False
    cx = min(max(x, r), BASE - r)
    cy = min(max(y, r), BASE - r)
    return (x - cx) ** 2 + (y - cy) ** 2 <= r * r


def in_arrow(x, y):
    # 竖杆
    if 56 <= x <= 72 and 30 <= y <= 72:
        return True
    # 箭头：上宽下尖的三角形（y 越大越窄，收成尖端）
    if 58 <= y <= 102:
        half = (102 - y) / (102 - 58) * 31
        return abs(x - 64) <= half
    return False


def render(size):
    px = []
    step = BASE / size
    for j in range(size):
        row = []
        for i in range(size):
            r = g = b = a = 0
            for sy in range(S):
                for sx in range(S):
                    x = (i + (sx + 0.5) / S) * step
                    y = (j + (sy + 0.5) / S) * step
                    if not in_round_rect(x, y, RADIUS):
                        continue
                    t = y / BASE
                    cr = int(TOP[0] + (BOTTOM[0] - TOP[0]) * t)
                    cg = int(TOP[1] + (BOTTOM[1] - TOP[1]) * t)
                    cb = int(TOP[2] + (BOTTOM[2] - TOP[2]) * t)
                    if in_arrow(x, y):
                        cr = cg = cb = 255
                    r += cr; g += cg; b += cb; a += 255
            n = S * S
            row.append((r // n, g // n, b // n, a // n))
        px.append(row)
    return px


def write_png(path, px):
    h = len(px)
    w = len(px[0])
    raw = b"".join(b"\x00" + b"".join(struct.pack("BBBB", *p) for p in row) for row in px)

    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    png = (b"\x89PNG\r\n\x1a\n"
           + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(raw, 9))
           + chunk(b"IEND", b""))
    with open(path, "wb") as f:
        f.write(png)


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    for size in (16, 32, 48, 128):
        write_png(os.path.join(here, f"icon{size}.png"), render(size))
        print("wrote", f"icon{size}.png")


if __name__ == "__main__":
    main()
