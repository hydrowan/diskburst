"""Braille sunburst engine: rasterize the disk map onto a terminal dot grid.

Braille cells give 2x4 dots per character; with the usual ~1:2 terminal cell
aspect the dot lattice is square, so an annulus renders round. Color is per
cell (ANSI truecolor), decided by majority vote of the arcs covering its dots.
Dependency-free — output is ANSI strings; wrap with rich's Text.from_ansi()
to embed in a rich layout.

Demo (against a running diskburst server):
    python -m diskburst.tui.sunburst [--path DIR] [--width N] [--rings N]
"""

from __future__ import annotations

import math
from bisect import bisect_right
from collections import Counter

BRAILLE_BASE = 0x2800
DOT_BITS = ((0x01, 0x08), (0x02, 0x10), (0x04, 0x20), (0x40, 0x80))  # [row][col]

# Catppuccin Mocha accents, same slot order as the web UI.
PALETTE = [
    (137, 180, 250), (250, 179, 135), (166, 227, 161), (203, 166, 247),
    (243, 139, 168), (249, 226, 175), (148, 226, 213), (245, 194, 231),
]
HOME_BLUE = (137, 180, 250)
LIBRARY_MAUVE = (203, 166, 247)
FILE_GRAY = (127, 132, 156)
REST_GRAY = (88, 91, 112)
SURFACE = (18, 18, 28)
MIN_ARC_DEG = 0.4
HOLE_FRACTION = 0.40
RING_GAP_FRACTION = 0.12  # blank share of each ring's width, separates the rings


def _fade(rgb: tuple[int, int, int], depth: int) -> tuple[int, int, int]:
    f = min((depth - 1) * 0.07, 0.21)  # deeper rings recede toward the surface
    return tuple(round(c * (1 - f) + s * f) for c, s in zip(rgb, SURFACE))


def _brighten(rgb: tuple[int, int, int]) -> tuple[int, int, int]:
    return tuple(round(c + (255 - c) * 0.25) for c in rgb)


def _dim(rgb: tuple[int, int, int]) -> tuple[int, int, int]:
    return tuple(round(c * 0.45 + s * 0.55) for c, s in zip(rgb, SURFACE))


def _branch_colors(items: list[dict]) -> list[tuple[int, int, int]]:
    """Semantic pins matching the web UI: home -> blue, Library -> mauve."""
    colors: list[tuple[int, int, int] | None] = []
    for item in items:
        path = item.get("path", "") or ""
        if item.get("type") == "dir" and path.count("/") == 2 and path.startswith("/Users/"):
            colors.append(HOME_BLUE)
        elif item.get("name") == "Library":
            colors.append(LIBRARY_MAUVE)
        else:
            colors.append(None)
    used = {c for c in colors if c}
    slot = 0
    for i, color in enumerate(colors):
        if color is None:
            while slot < len(PALETTE) and PALETTE[slot] in used:
                slot += 1
            colors[i] = PALETTE[slot % len(PALETTE)]
            slot += 1
    return colors


def collect_arcs(
    tree: dict, rings: int, highlight: int | None = None
) -> dict[int, list[tuple[float, float, tuple]]]:
    """Per-depth lists of (start_deg, end_deg, rgb), angles clockwise from top.

    `highlight` selects a top-level child index: its whole branch brightens
    and every other arc dims toward the surface.
    """
    per_depth: dict[int, list[tuple[float, float, tuple]]] = {d: [] for d in range(1, rings + 1)}
    total = max(tree.get("size", 0), 1)
    top = tree.get("children") or []
    top_colors = _branch_colors(top)

    def walk(items: list[dict], depth: int, start: float,
             color: tuple | None, branch: int | None) -> None:
        angle = start
        for i, item in enumerate(items or []):
            span = item.get("size", 0) / total * 360
            if span >= MIN_ARC_DEG and depth <= rings:
                top_i = i if depth == 1 else branch
                if item.get("type") == "rest":
                    rgb = REST_GRAY
                elif item.get("type") == "file":
                    rgb = _fade(FILE_GRAY, depth)
                else:
                    rgb = _fade(color if depth > 1 else top_colors[i], depth)
                if highlight is not None and item.get("type") != "rest":
                    rgb = _brighten(rgb) if top_i == highlight else _dim(rgb)
                elif highlight is not None:
                    rgb = _dim(rgb)
                per_depth[depth].append((angle, angle + span, rgb))
                if item.get("type") == "dir":
                    walk(item.get("children") or [], depth + 1,
                         angle, color if depth > 1 else top_colors[i], top_i)
            angle += span
    walk(top, 1, 0.0, None, None)
    return per_depth


def make_classifier(per_depth: dict, hole: float, ring_w: float, rings: int, seg_gap: float):
    """(r, theta_deg) -> rgb | None, honoring ring gaps and inter-slice gaps.

    `seg_gap` is the blank arc length between adjacent slices, in the same
    units as r (dots or pixels); it converts to degrees per-radius so gaps
    stay visually constant across rings.
    """
    starts = {d: [a0 for a0, _, _ in segs] for d, segs in per_depth.items()}
    ring_gap = ring_w * RING_GAP_FRACTION

    def classify(r: float, theta: float):
        band = r - hole
        if band < 0:
            return None
        depth = int(band // ring_w) + 1
        if depth > rings:
            return None
        if band % ring_w > ring_w - ring_gap:
            return None  # gap between rings
        segs = per_depth[depth]
        if not segs:
            return None
        i = bisect_right(starts[depth], theta) - 1
        if i < 0:
            return None
        a0, a1, rgb = segs[i]
        if theta >= a1:
            return None
        gap_deg = math.degrees(seg_gap / max(r, 1.0))
        if a1 - a0 < 358 and (theta - a0 < gap_deg or a1 - theta < gap_deg):
            return None  # gap between adjacent slices
        return rgb

    return classify


def render(
    tree: dict,
    width: int = 80,
    rings: int = 3,
    highlight: int | None = None,
    center: list[str] | None = None,
) -> list[str]:
    """Render the sunburst as ANSI-colored lines (width chars, ~width/2 rows).

    `center` supplies the lines shown in the hole (first line bold, rest
    muted); defaults to the tree's total size.
    """
    w_chars = max(20, width)
    h_chars = w_chars // 2
    dots_w, dots_h = 2 * w_chars, 4 * h_chars
    radius = min(dots_w, dots_h) / 2 - 1
    hole = radius * HOLE_FRACTION
    ring_w = (radius - hole) / rings

    per_depth = collect_arcs(tree, rings, highlight=highlight)
    classify = make_classifier(per_depth, hole, ring_w, rings, seg_gap=0.9)

    chars = [[0] * w_chars for _ in range(h_chars)]
    votes: list[list[Counter | None]] = [[None] * w_chars for _ in range(h_chars)]
    cx, cy = dots_w / 2, dots_h / 2
    for py in range(dots_h):
        dy = py - cy + 0.5
        for px in range(dots_w):
            dx = px - cx + 0.5
            r = math.hypot(dx, dy)
            if r > radius:
                continue
            theta = math.degrees(math.atan2(dx, -dy)) % 360  # 0 at top, clockwise
            rgb = classify(r, theta)
            if rgb is None:
                continue
            row, col = py // 4, px // 2
            chars[row][col] |= DOT_BITS[py % 4][px % 2]
            if votes[row][col] is None:
                votes[row][col] = Counter()
            votes[row][col][rgb] += 1

    if not center:
        center = [_fmt_bytes(tree.get("size", 0))]
    max_len = max(6, int(w_chars * 0.34))  # keep lines inside the hole
    center = [line[:max_len] for line in center[:4] if line]
    start_row = h_chars // 2 - len(center) // 2
    overlays = {
        start_row + i: ((w_chars - len(text)) // 2, text, i == 0)
        for i, text in enumerate(center)
        if 0 <= start_row + i < h_chars
    }
    lines = []
    for y in range(h_chars):
        parts: list[str] = []
        prev = None
        overlay = overlays.get(y)
        x = 0
        while x < w_chars:
            if overlay and x == overlay[0]:  # center text, cell-aligned
                col, text, is_title = overlay
                style = "\x1b[1m" if is_title else "\x1b[38;2;166;173;200m"
                parts.append(f"\x1b[0m{style}{text}\x1b[0m")
                prev = None
                x += len(text)
                continue
            bits = chars[y][x]
            if not bits:
                parts.append(" ")
                prev = None
            else:
                rgb = votes[y][x].most_common(1)[0][0]
                if rgb != prev:
                    parts.append(f"\x1b[38;2;{rgb[0]};{rgb[1]};{rgb[2]}m")
                    prev = rgb
                parts.append(chr(BRAILLE_BASE | bits))
            x += 1
        parts.append("\x1b[0m")
        lines.append("".join(parts))
    return lines


def _fmt_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1000 or unit == "TB":
            return f"{n:.0f} {unit}" if n >= 100 or unit == "B" else f"{n:.1f} {unit}"
        n /= 1000
    return f"{n:.0f} TB"


def legend(tree: dict, top_n: int = 6) -> list[str]:
    """Colored swatch lines for the largest top-level branches."""
    items = (tree.get("children") or [])[:top_n]
    colors = _branch_colors(tree.get("children") or [])
    out = []
    for item, rgb in zip(items, colors):
        name = item.get("name", "?")
        if name.startswith("/Users/") and name.count("/") == 2:
            name = "~"
        swatch = f"\x1b[38;2;{rgb[0]};{rgb[1]};{rgb[2]}m⣿\x1b[0m"
        out.append(f"  {swatch} {name:<24} {_fmt_bytes(item.get('size', 0)):>9}")
    return out


def main() -> None:
    import argparse
    import json
    import shutil
    import urllib.request

    parser = argparse.ArgumentParser(description="Braille sunburst of the diskburst tree")
    parser.add_argument("--path", default="", help="zoom to this directory ('' = all roots)")
    parser.add_argument("--width", type=int, default=0, help="chart width in columns (default fits terminal height)")
    parser.add_argument("--rings", type=int, default=3)
    parser.add_argument("--port", type=int, default=8890)
    args = parser.parse_args()

    term = shutil.get_terminal_size()
    # fit BOTH axes: the chart is width/2 rows tall; leave room for the legend
    width = args.width or max(24, min(term.columns - 2, (term.lines - 10) * 2, 90))
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
    print()
    for line in render(tree, width=width, rings=args.rings):
        print(line)
    print()
    for line in legend(tree):
        print(line)
    print()


if __name__ == "__main__":
    main()
