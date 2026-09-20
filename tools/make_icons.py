#!/usr/bin/env python3
"""Logo und Icons des Minecraft Server Managers – ohne Bibliotheken.

Ein Motiv, überall dasselbe: ein leuchtender Grasblock (isometrischer Würfel) auf dunkler,
abgerundeter Kachel. Erzeugt
  web/logo.svg            – Vektorlogo (Seitenleiste, Startseite, Browser-Tab)
  web/favicon.png         – Favicon: daraus nimmt Edge das Symbol für App-Fenster und Taskleiste
  web/icon-192.png, web/icon-512.png – große Tab-/Touch-Icons und Manifest (Edge --app nutzt sie nicht)
  app.ico                 – Windows-Icon 16…256 px (Verknüpfung, Explorer, Tray, Setup)

Aufruf: python tools\\make_icons.py  (wird auch von tools\\build_installer.py benutzt)
"""
from __future__ import annotations

import math
import pathlib
import struct
import zlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
WEB = ROOT / "web"

# ---------------------------------------------------------------- Geometrie (Einheitsquadrat)
T, L, R, C = (0.50, 0.19), (0.21, 0.345), (0.79, 0.345), (0.50, 0.50)
BL, B, BR = (0.21, 0.655), (0.50, 0.81), (0.79, 0.655)
TOP_FACE = [T, R, C, L]
LEFT_FACE = [L, C, B, BL]
RIGHT_FACE = [C, R, BR, B]


def _fringe(a: tuple, b: tuple, steps: int = 7) -> list[tuple]:
    """Blockiger Gras-Saum entlang der Oberkante a→b einer Seitenfläche."""
    pts = [a]
    for i in range(steps):
        d = 0.075 if i % 2 else 0.045
        t0, t1 = i / steps, (i + 1) / steps
        x0, y0 = a[0] + (b[0] - a[0]) * t0, a[1] + (b[1] - a[1]) * t0
        x1, y1 = a[0] + (b[0] - a[0]) * t1, a[1] + (b[1] - a[1]) * t1
        pts += [(x0, y0 + d), (x1, y1 + d)]
    pts.append(b)
    return pts


LEFT_FRINGE = _fringe(L, C)
RIGHT_FRINGE = _fringe(C, R)

# Farben (RGB)
BG_TOP, BG_BOT = (24, 34, 50), (10, 15, 23)
GLOW = (61, 220, 132)
TOP_A, TOP_B = (170, 247, 200), (128, 236, 172)       # Oberseite mit leichtem Verlauf
LEFT, RIGHT = (61, 220, 132), (28, 138, 82)
FRINGE_R = (104, 230, 160)
EDGE = (10, 62, 36)
RADIUS = 0.22
EDGE_W = 0.011


# ---------------------------------------------------------------- Rasterizer

def _inside(poly: list[tuple], x: float, y: float) -> bool:
    inside = False
    n = len(poly)
    j = n - 1
    for i in range(n):
        xi, yi = poly[i]
        xj, yj = poly[j]
        if (yi > y) != (yj > y):
            xcross = xi + (y - yi) * (xj - xi) / (yj - yi)
            if x < xcross:
                inside = not inside
        j = i
    return inside


def _seg_dist(p: tuple, a: tuple, b: tuple) -> float:
    ax, ay = a
    bx, by = b
    px, py = p
    dx, dy = bx - ax, by - ay
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def _in_tile(x: float, y: float) -> bool:
    r = RADIUS
    cx = min(max(x, r), 1 - r)
    cy = min(max(y, r), 1 - r)
    return math.hypot(x - cx, y - cy) <= r


def _lerp(a: tuple, b: tuple, t: float) -> tuple:
    return tuple(a[i] + (b[i] - a[i]) * t for i in range(3))


def _sample(x: float, y: float) -> tuple:
    if not _in_tile(x, y):
        return (0.0, 0.0, 0.0, 0.0)
    col = _lerp(BG_TOP, BG_BOT, y)
    d = math.hypot(x - 0.5, y - 0.53) / 0.46
    if d < 1:
        col = _lerp(col, GLOW, (1 - d) ** 2 * 0.30)
    in_cube = False
    if _inside(TOP_FACE, x, y):
        col = _lerp(TOP_A, TOP_B, (y - T[1]) / (C[1] - T[1]))
        in_cube = True
    elif _inside(LEFT_FACE, x, y):
        col = TOP_B if _inside(LEFT_FRINGE, x, y) else LEFT
        in_cube = True
    elif _inside(RIGHT_FACE, x, y):
        col = FRINGE_R if _inside(RIGHT_FRINGE, x, y) else RIGHT
        in_cube = True
    if in_cube:
        p = (x, y)
        if min(_seg_dist(p, L, C), _seg_dist(p, C, R), _seg_dist(p, C, B)) < EDGE_W:
            col = EDGE
    return (col[0], col[1], col[2], 255.0)


def render_master(size: int = 512, ss: int = 3) -> list[list[tuple]]:
    """RGBA-Pixel (0..255, nicht vormultipliziert) mit Supersampling."""
    rows = []
    inv = 1.0 / size
    for py in range(size):
        row = []
        for px in range(size):
            r = g = b = a = 0.0
            for sy in range(ss):
                for sx in range(ss):
                    cr, cg, cb, ca = _sample((px + (sx + 0.5) / ss) * inv, (py + (sy + 0.5) / ss) * inv)
                    w = ca / 255.0
                    r += cr * w
                    g += cg * w
                    b += cb * w
                    a += ca
            n = ss * ss
            if a > 0:
                w = a / 255.0
                row.append((r / w, g / w, b / w, a / n))
            else:
                row.append((0.0, 0.0, 0.0, 0.0))
        rows.append(row)
    return rows


def downscale(master: list[list[tuple]], size: int) -> list[list[tuple]]:
    """Flächenmittelung (Box-Filter) vom Master auf eine kleinere Größe."""
    m = len(master)
    if size == m:
        return master
    out = []
    for ty in range(size):
        y0, y1 = ty * m / size, (ty + 1) * m / size
        row = []
        for tx in range(size):
            x0, x1 = tx * m / size, (tx + 1) * m / size
            r = g = b = a = 0.0
            wsum = 0.0
            for sy in range(int(y0), min(m, int(math.ceil(y1)))):
                wy = min(y1, sy + 1) - max(y0, sy)
                mrow = master[sy]
                for sx in range(int(x0), min(m, int(math.ceil(x1)))):
                    w = wy * (min(x1, sx + 1) - max(x0, sx))
                    pr, pg, pb, pa = mrow[sx]
                    wa = w * pa / 255.0
                    r += pr * wa
                    g += pg * wa
                    b += pb * wa
                    a += pa * w
                    wsum += w
            if a > 0:
                wa = a / 255.0
                row.append((r / wa, g / wa, b / wa, a / wsum))
            else:
                row.append((0.0, 0.0, 0.0, 0.0))
        out.append(row)
    return out


def _rgba_bytes(img: list[list[tuple]]) -> list[bytes]:
    return [bytes(int(round(v)) for px in row for v in px) for row in img]


# ---------------------------------------------------------------- PNG / ICO

def png_bytes(img: list[list[tuple]]) -> bytes:
    size = len(img)
    raw = b"".join(b"\x00" + row for row in _rgba_bytes(img))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9))
            + chunk(b"IEND", b""))


def _bmp_entry(img: list[list[tuple]]) -> bytes:
    size = len(img)
    header = struct.pack("<IiiHHIIiiII", 40, size, size * 2, 1, 32, 0, size * size * 4, 0, 0, 0, 0)
    xor = bytearray()
    for row in reversed(img):
        for r, g, b, a in row:
            xor += bytes((int(round(b)), int(round(g)), int(round(r)), int(round(a))))
    and_mask = bytes(((size + 31) // 32) * 4 * size)
    return header + bytes(xor) + and_mask


def ico_bytes(images: dict[int, list[list[tuple]]]) -> bytes:
    """Kleine Größen als BMP (für alte Shell-Komponenten), große als PNG."""
    sizes = sorted(images)
    entries, blobs = [], []
    offset = 6 + 16 * len(sizes)
    for s in sizes:
        data = png_bytes(images[s]) if s >= 64 else _bmp_entry(images[s])
        entries.append(struct.pack("<BBBBHHII", s % 256, s % 256, 0, 0, 1, 32, len(data), offset))
        blobs.append(data)
        offset += len(data)
    return struct.pack("<HHH", 0, 1, len(sizes)) + b"".join(entries) + b"".join(blobs)


# ---------------------------------------------------------------- SVG

def _pts(poly: list[tuple]) -> str:
    return " ".join(f"{x * 100:.1f},{y * 100:.1f}" for x, y in poly)


def svg_text() -> str:
    def rgb(c: tuple) -> str:
        return "#%02x%02x%02x" % tuple(int(v) for v in c)

    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100" width="100" height="100">
  <defs>
    <linearGradient id="bg" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="{rgb(BG_TOP)}"/><stop offset="1" stop-color="{rgb(BG_BOT)}"/>
    </linearGradient>
    <radialGradient id="glow" cx="50%" cy="53%" r="46%">
      <stop offset="0" stop-color="{rgb(GLOW)}" stop-opacity="0.30"/><stop offset="1" stop-color="{rgb(GLOW)}" stop-opacity="0"/>
    </radialGradient>
    <linearGradient id="top" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="{rgb(TOP_A)}"/><stop offset="1" stop-color="{rgb(TOP_B)}"/>
    </linearGradient>
  </defs>
  <rect width="100" height="100" rx="{RADIUS * 100:.0f}" fill="url(#bg)"/>
  <rect width="100" height="100" rx="{RADIUS * 100:.0f}" fill="url(#glow)"/>
  <polygon points="{_pts(LEFT_FACE)}" fill="{rgb(LEFT)}"/>
  <polygon points="{_pts(RIGHT_FACE)}" fill="{rgb(RIGHT)}"/>
  <polygon points="{_pts(LEFT_FRINGE)}" fill="{rgb(TOP_B)}"/>
  <polygon points="{_pts(RIGHT_FRINGE)}" fill="{rgb(FRINGE_R)}"/>
  <polygon points="{_pts(TOP_FACE)}" fill="url(#top)"/>
  <path d="M{L[0] * 100:.1f},{L[1] * 100:.1f} L{C[0] * 100:.1f},{C[1] * 100:.1f} L{R[0] * 100:.1f},{R[1] * 100:.1f} M{C[0] * 100:.1f},{C[1] * 100:.1f} L{B[0] * 100:.1f},{B[1] * 100:.1f}"
        fill="none" stroke="{rgb(EDGE)}" stroke-width="{EDGE_W * 200:.1f}" stroke-linecap="round" stroke-linejoin="round"/>
</svg>
"""


# ---------------------------------------------------------------- Alles erzeugen

def make_all(verbose: bool = True) -> None:
    WEB.mkdir(exist_ok=True)
    (WEB / "logo.svg").write_text(svg_text(), encoding="utf-8")
    if verbose:
        print("  web/logo.svg")
    master = render_master(512, 3)
    sizes = {s: downscale(master, s) for s in (16, 20, 24, 32, 40, 48, 64, 128, 192, 256, 512)}
    (WEB / "icon-512.png").write_bytes(png_bytes(sizes[512]))
    (WEB / "icon-192.png").write_bytes(png_bytes(sizes[192]))
    (WEB / "favicon.png").write_bytes(png_bytes(sizes[32]))
    (ROOT / "app.ico").write_bytes(ico_bytes({s: sizes[s] for s in (16, 20, 24, 32, 40, 48, 64, 128, 256)}))
    if verbose:
        print("  web/icon-512.png, web/icon-192.png, web/favicon.png, app.ico (16…256)")


if __name__ == "__main__":
    print("Logo und Icons werden erzeugt …")
    make_all()
    print("Fertig.")
