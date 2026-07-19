#!/usr/bin/env python3
"""
Generate windows/spark.ico — the Token Maxxing app icon.

Renders the same orange "spark" mark used on the overlay's boot screen onto a
rounded dark tile, at several sizes, and packs them into a multi-resolution
.ico (PNG-embedded, Vista+). Run once whenever the brand mark changes:

    cd windows && py make_icon.py

Requires only PyQt6 (already a build dependency). No Pillow needed.
"""

import math
import struct
from PyQt6.QtCore import Qt, QBuffer, QByteArray, QPointF, QRectF
from PyQt6.QtGui import QImage, QPainter, QColor, QPainterPath

# Brand colors (mirror claude_monitor_overlay.py)
C_PANEL  = QColor(6, 7, 10)         # dark tile
C_CLAUDE = QColor(216, 116, 80)     # spark orange
C_TEXT   = QColor(255, 255, 255)    # hot center

SIZES = [16, 24, 32, 48, 64, 128, 256]


def _draw_spark(p: QPainter, cx, cy, outer, inner_r, rays, color, center_col):
    """Static rendition of the overlay's _draw_spark (t fixed for a crisp icon)."""
    da = 0.45
    rot = -math.pi / 2  # one ray pointing up
    for i in range(rays):
        a = rot + i * (math.pi * 2 / rays)
        tx, ty = cx + math.cos(a) * outer,      cy + math.sin(a) * outer
        b1x, b1y = cx + math.cos(a + da) * inner_r, cy + math.sin(a + da) * inner_r
        b2x, b2y = cx + math.cos(a - da) * inner_r, cy + math.sin(a - da) * inner_r
        path = QPainterPath()
        path.moveTo(tx, ty)
        path.lineTo(b1x, b1y)
        path.lineTo(b2x, b2y)
        path.closeSubpath()
        p.fillPath(path, color)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(color)
    p.drawEllipse(QPointF(cx, cy), inner_r, inner_r)
    p.setBrush(center_col)
    p.drawEllipse(QPointF(cx, cy), max(1.0, inner_r - inner_r * 0.42), max(1.0, inner_r - inner_r * 0.42))


def render(size: int) -> QImage:
    img = QImage(size, size, QImage.Format.Format_ARGB32)
    img.fill(Qt.GlobalColor.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)

    # Rounded dark tile background
    radius = size * 0.22
    bg = QPainterPath()
    bg.addRoundedRect(QRectF(0, 0, size, size), radius, radius)
    p.fillPath(bg, C_PANEL)

    # Centered spark, scaled to the tile
    cx = cy = size / 2
    outer = size * 0.40
    inner = size * 0.13
    _draw_spark(p, cx, cy, outer, inner, 6, C_CLAUDE, C_TEXT)
    p.end()
    return img


def png_bytes(img: QImage) -> bytes:
    ba = QByteArray()           # keep alive for the buffer's lifetime
    buf = QBuffer(ba)
    buf.open(QBuffer.OpenModeFlag.WriteOnly)
    img.save(buf, "PNG")
    buf.close()
    return bytes(ba)


def write_ico(path: str, images):
    pngs = [(img.width(), png_bytes(img)) for img in images]
    n = len(pngs)
    out = bytearray()
    out += struct.pack("<HHH", 0, 1, n)            # ICONDIR: reserved, type=1(icon), count
    offset = 6 + n * 16                            # header + directory entries
    for w, data in pngs:
        dim = 0 if w >= 256 else w                 # 0 means 256 in ICO
        out += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(data), offset)
        offset += len(data)
    for _w, data in pngs:
        out += data
    with open(path, "wb") as f:
        f.write(out)


if __name__ == "__main__":
    import os, sys
    # A QGuiApplication is required before QPainter/QImage work.
    from PyQt6.QtGui import QGuiApplication
    _app = QGuiApplication(sys.argv)

    imgs = [render(s) for s in SIZES]
    out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "spark.ico")
    write_ico(out_path, imgs)
    print(f"Wrote {out_path}  ({len(SIZES)} sizes: {', '.join(map(str, SIZES))})")
