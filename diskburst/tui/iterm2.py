"""iTerm2 sunburst: rasterize the disk map to PNG and display it inline.

Uses iTerm2's OSC 1337 inline-image protocol for true pixel resolution —
no image libraries, PNG is encoded with stdlib zlib/struct. The image is
rendered large and displayed scaled to the terminal width, so iTerm2's
downscaling provides free antialiasing on Retina displays.

    python -m diskburst.tui.iterm2 [--path DIR] [--rings N] [--size PX] [--out FILE]
"""

from __future__ import annotations

import base64
import math
import os
import struct
import sys
import zlib

from .sunburst import (
    HOLE_FRACTION,
    RING_GAP_FRACTION,
    _fmt_bytes,
    collect_arcs,
    legend,
)

TRANSPARENT = b"\x00\x00\x00\x00"


LUT_STEPS = 3600  # 0.1 degree angular quantization


def _ring_luts(per_depth: dict, rings: int, hole: float, ring_w: float, seg_gap: float):
    """Per-ring angle->RGBA lookup tables with slice gaps baked in."""
    luts = []
    for depth in range(1, rings + 1):
        lut: list[bytes | None] = [None] * LUT_STEPS
        mid_r = hole + (depth - 0.5) * ring_w
        gap_deg = math.degrees(seg_gap / mid_r)
        for a0, a1, rgb in per_depth[depth]:
            lo, hi = a0, a1
            if a1 - a0 < 358:  # keep full-circle arcs gapless
                lo, hi = a0 + gap_deg, a1 - gap_deg
            pixel = bytes((rgb[0], rgb[1], rgb[2], 255))
            for i in range(int(lo * 10) + 1, int(hi * 10)):
                lut[i] = pixel
        luts.append(lut)
    return luts


def rasterize(tree: dict, size: int = 1400, rings: int = 4) -> bytes:
    """RGBA pixel buffer of the sunburst, transparent background.

    Hot loop covers one quadrant only (4-fold symmetry gives the rest) and
    classifies via per-ring angle LUTs — no per-pixel search or gap math.
    """
    buf = bytearray(TRANSPARENT * (size * size))
    radius = size / 2 - 2
    hole = radius * HOLE_FRACTION
    ring_w = (radius - hole) / rings
    per_depth = collect_arcs(tree, rings)
    luts = _ring_luts(per_depth, rings, hole, ring_w, seg_gap=max(2.0, size * 0.0022))

    half = size // 2
    solid = 1.0 - RING_GAP_FRACTION
    hypot, atan2, degrees = math.hypot, math.atan2, math.degrees
    for qy in range(half):  # top half of the quadrant grid; dy < 0
        dy = qy - half + 0.5
        row_top = (qy * size) * 4
        row_bot = ((size - 1 - qy) * size) * 4
        for qx in range(half):  # right half; dx > 0
            dx = qx + 0.5
            r = hypot(dx, dy)
            band = r - hole
            if band < 0 or r > radius:
                continue
            depth, pos = divmod(band, ring_w)
            if depth >= rings or pos > ring_w * solid:
                continue
            lut = luts[int(depth)]
            t = degrees(atan2(dx, -dy)) * 10  # quadrant angle in [0, 900)
            ti = int(t)
            x_r, x_l = (half + qx) * 4, (half - 1 - qx) * 4
            pixel = lut[ti]  # top-right: theta = t
            if pixel is not None:
                buf[row_top + x_r: row_top + x_r + 4] = pixel
            pixel = lut[1800 - 1 - ti]  # bottom-right: 180 - t
            if pixel is not None:
                buf[row_bot + x_r: row_bot + x_r + 4] = pixel
            pixel = lut[1800 + ti]  # bottom-left: 180 + t
            if pixel is not None:
                buf[row_bot + x_l: row_bot + x_l + 4] = pixel
            pixel = lut[3600 - 1 - ti]  # top-left: 360 - t
            if pixel is not None:
                buf[row_top + x_l: row_top + x_l + 4] = pixel
    return bytes(buf)


def encode_png(rgba: bytes, width: int, height: int) -> bytes:
    """Minimal PNG encoder (8-bit RGBA, no filtering)."""
    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data)) + tag + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    stride = width * 4
    raw = b"".join(
        b"\x00" + rgba[y * stride: (y + 1) * stride] for y in range(height)
    )
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(raw, 6))
        + chunk(b"IEND", b"")
    )


def display_inline(png: bytes, width_cells: int) -> None:
    """Emit the OSC 1337 inline-image sequence (iTerm2)."""
    payload = base64.b64encode(png).decode("ascii")
    osc, st = "\x1b]", "\x07"
    if os.environ.get("TMUX"):  # tmux needs the sequence wrapped in DCS passthrough
        osc, st = "\x1bPtmux;\x1b\x1b]", "\x07\x1b\\"
    sys.stdout.write(
        f"{osc}1337;File=inline=1;size={len(png)};width={width_cells};"
        f"preserveAspectRatio=1:{payload}{st}\n"
    )


def main() -> None:
    import argparse
    import json
    import shutil
    import urllib.parse
    import urllib.request

    parser = argparse.ArgumentParser(description="iTerm2 inline-image sunburst")
    parser.add_argument("--path", default="", help="zoom to this directory ('' = all roots)")
    parser.add_argument("--rings", type=int, default=4)
    parser.add_argument("--size", type=int, default=1400, help="render resolution in px")
    parser.add_argument("--port", type=int, default=8890)
    parser.add_argument("--out", default="", help="also save the PNG to this file")
    args = parser.parse_args()

    url = (
        f"http://127.0.0.1:{args.port}/api/tree"
        f"?path={urllib.parse.quote(args.path)}&depth={args.rings}"
    )
    try:
        with urllib.request.urlopen(url, timeout=10) as res:
            tree = json.load(res)
    except OSError:
        raise SystemExit(
            f"diskburst server not reachable on port {args.port} — start it first"
        ) from None
    if tree.get("error"):
        raise SystemExit(f"error: {tree['error']}")

    png = encode_png(rasterize(tree, args.size, args.rings), args.size, args.size)
    if args.out:
        with open(args.out, "wb") as f:
            f.write(png)
    columns = shutil.get_terminal_size().columns
    print()
    display_inline(png, width_cells=min(columns - 2, 80))
    print(f"  \x1b[1m{_fmt_bytes(tree.get('size', 0))}\x1b[22m"
          f"  {tree.get('name') if not args.path else args.path}")
    for line in legend(tree):
        print(line)
    print()


if __name__ == "__main__":
    main()
