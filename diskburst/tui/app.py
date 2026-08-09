"""Rich TUI for diskburst: sunburst (braille or iTerm2 image) + drill-down sidebar.

A thin client of the running diskburst server. Single-threaded event loop:
cbreak keyboard input multiplexed with a 1 s status poll; redraws only on
change. (Deliberately no DEC 2026 synchronized updates — iTerm2 can drop the
commit half mid-resize, freezing the display.)

    python -m diskburst.tui [--port N] [--renderer braille|image]

Braille is the default renderer; the iTerm2 inline-image renderer stays
available behind --renderer image as an experiment.

Keys: j/k select · h/l out/in · g/G ends · Tab contents/largest · ⏎ Finder
      y copy path · r rescan dir · R rescan all · q quit
      0 header/footer · 1 chart · 2 list · 3 selected  (btop-style toggles)
"""

from __future__ import annotations

import json
import os
import select
import signal
import subprocess
import sys
import termios
import time
import tty
import urllib.parse
import urllib.request

from rich.align import Align
from rich.console import Console
from rich.layout import Layout
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .iterm2 import display_inline, encode_png, rasterize
from .sunburst import _branch_colors, _fmt_bytes, render

SIDEBAR_WIDTH = 44
POLL_SECONDS = 1.0
IMAGE_SIZE = 1000
MOCHA = {
    "text": "#cdd6f4", "sub": "#a6adc8", "muted": "#7f849c",
    "green": "#a6e3a1", "mauve": "#cba6f7", "red": "#f38ba8", "base": "#12121c",
}


class Client:
    """Minimal HTTP client for the diskburst server."""

    def __init__(self, port: int):
        self.base = f"http://127.0.0.1:{port}"

    def get(self, route: str, **params) -> dict:
        query = urllib.parse.urlencode({k: v for k, v in params.items() if v != ""})
        with urllib.request.urlopen(f"{self.base}{route}?{query}", timeout=10) as res:
            return json.load(res)

    def post(self, route: str, body: dict | None = None) -> dict:
        req = urllib.request.Request(
            f"{self.base}{route}",
            data=json.dumps(body or {}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as res:
            return json.load(res)


class TuiApp:
    def __init__(self, port: int, renderer: str):
        self.client = Client(port)
        self.console = Console()
        self.renderer = renderer
        self.path = ""
        self.sel = 0
        self.tab = "contents"  # or "largest"
        self.status: dict = {}
        self.tree: dict = {}
        self.largest: list = []
        self.message = ""
        self._message_at = 0.0
        self._png: bytes | None = None
        self._last_poll = 0.0
        self._seen_change = 0.0
        self.chrome = True  # header + footer (window 0)
        self.visible = {1: True, 2: True, 3: True}  # chart, list, selected

    # ------------------------------------------------------------- data

    def refresh_tree(self) -> None:
        self.tree = self.client.get("/api/tree", path=self.path, depth=4)
        if self.tree.get("error"):
            self.path = ""
            self.tree = self.client.get("/api/tree", path="", depth=4)
        self.sel = min(self.sel, max(len(self.items()) - 1, 0))
        self._png = None  # image cache invalid

    def refresh_largest(self) -> None:
        data = self.client.get("/api/largest", path=self.path, n=100)
        self.largest = data.get("files", [])

    def poll(self) -> bool:
        """Update status; True if the tree changed underneath us."""
        try:
            status = self.client.get("/api/status")
        except OSError:
            return False
        changed = False
        live_change = status.get("live", {}).get("last_change", 0)
        if self.status:
            if self.status.get("state") != "done" and status.get("state") == "done":
                changed = True
            if live_change > self._seen_change and status.get("state") == "done":
                changed = True
        self._seen_change = live_change
        self.status = status
        return changed

    def items(self) -> list[dict]:
        return self.tree.get("children") or []

    # ------------------------------------------------------------- keys

    def handle(self, key: str) -> bool:
        """Apply a keypress; returns False to quit."""
        items = self.items()
        if key == "q":
            return False
        elif key in ("j", "DOWN"):
            self.sel = (self.sel + 1) % max(len(items), 1)
        elif key in ("k", "UP"):
            self.sel = (self.sel - 1) % max(len(items), 1)
        elif key == "g":
            self.sel = 0
        elif key == "G":
            self.sel = max(len(items) - 1, 0)
        elif key in ("l", "RIGHT"):
            item = items[self.sel] if self.sel < len(items) else None
            if item and item.get("type") == "dir" and item.get("path"):
                self.path = item["path"]
                self.sel = 0
                self.refresh_tree()
                if self.tab == "largest":
                    self.refresh_largest()
        elif key in ("h", "LEFT"):
            if self.path:
                leaving = self.path
                roots = self.status.get("roots", [])
                self.path = "" if leaving in roots else (
                    leaving[: leaving.rfind("/")] or "/"
                )
                self.refresh_tree()
                names = [i.get("path") for i in self.items()]
                self.sel = names.index(leaving) if leaving in names else 0
                if self.tab == "largest":
                    self.refresh_largest()
        elif key == "\t":
            self.tab = "largest" if self.tab == "contents" else "contents"
            if self.tab == "largest":
                self.refresh_largest()
        elif key == "\r":
            target = self._selected_path() or self.path
            if target:
                self.client.post("/api/reveal", {"path": target})
                self.toast("revealed in Finder")
        elif key == "y":
            target = self._selected_path() or self.path
            if target:
                subprocess.run(["pbcopy"], input=target.encode())
                self.toast(f"copied {target[-46:]}")
        elif key == "r":
            if self.path:
                result = self.client.post("/api/rescan_dir", {"path": self.path})
                self.toast("rescanning dir" if result.get("ok") else result.get("error", ""))
        elif key == "R":
            self.client.post("/api/rescan")
            self.toast("full rescan started")
        elif key == "0":
            self.chrome = not self.chrome
        elif key in ("1", "2", "3"):
            n = int(key)
            self.visible[n] = not self.visible[n]
            if not any(self.visible.values()):
                self.visible[n] = True  # never collapse everything
        return True

    def _selected_item(self) -> dict | None:
        if self.tab == "largest":
            if self.sel < len(self.largest):
                f = self.largest[self.sel]
                return {"name": f["path"].rsplit("/", 1)[-1], "path": f["path"],
                        "type": "file", "size": f["size"], "mtime": f.get("mtime")}
            return None
        items = self.items()
        return items[self.sel] if self.sel < len(items) else None

    def _selected_path(self) -> str:
        item = self._selected_item()
        return (item or {}).get("path", "")

    # ---------------------------------------------------------- drawing

    def sidebar_width(self) -> int:
        if not self.visible[1]:
            return self.console.size.width  # chart collapsed: side takes it all
        return min(SIDEBAR_WIDTH, max(28, self.console.size.width // 3))

    def compose(self, chart_text: Text | str) -> Layout:
        layout = Layout()
        rows = [Layout(name="body", ratio=1)]
        if self.chrome:
            rows = [Layout(self._header(), name="header", size=1), *rows,
                    Layout(self._footer(), name="footer", size=1)]
        layout.split_column(*rows)

        title = Text(f"1 · {shorten_home(self.path) or 'All scanned'}",
                     style=f"bold {MOCHA['text']}")
        if isinstance(chart_text, str):
            chart = chart_text
        else:
            chart_text.no_wrap = True
            chart_text.overflow = "crop"  # a mid-resize oversized chart crops, never wraps
            chart = Align.center(chart_text, vertical="middle")
        columns = []
        if self.visible[1]:
            columns.append(Layout(
                Panel(chart, title=title, border_style=MOCHA["muted"]),
                name="chart", ratio=1,
            ))
        if self.visible[2] or self.visible[3]:
            columns.append(Layout(name="side", size=self.sidebar_width()))
        layout["body"].split_row(*columns)

        if self.visible[2] or self.visible[3]:
            side_rows = []
            if self.visible[2]:
                side_rows.append(Layout(self._sidebar(), name="list", ratio=1))
            if self.visible[3]:
                info_size = 9 if self.visible[2] else None
                side_rows.append(
                    Layout(self._info_panel(), name="info", size=info_size)
                )
            layout["side"].split_column(*side_rows)
        return layout

    def _header(self) -> Text:
        s = self.status
        disk = s.get("disk", {})
        head = Text(no_wrap=True)
        head.append(" diskburst ", style=f"bold {MOCHA['text']}")
        head.append(f" {_fmt_bytes(disk.get('free', 0))} free ", style=MOCHA["red"])
        head.append(f"· {_fmt_bytes(s.get('bytes', 0))} scanned ", style=MOCHA["sub"])
        head.append(f"· {s.get('files', 0):,} files ", style=MOCHA["sub"])
        state = s.get("state", "…")
        style = MOCHA["green"] if state == "done" else MOCHA["mauve"]
        head.append(f"· {state}", style=style)
        if s.get("live", {}).get("enabled"):
            head.append(" · live", style=MOCHA["green"])
        return head

    def _footer(self) -> Text:
        hints = (
            " j/k select · h/l out/in · tab contents/largest · ⏎ finder · y copy"
            " · r/R rescan · 0-3 toggle windows · q quit"
        )
        return Text(hints, style=MOCHA["muted"], no_wrap=True)

    def _sidebar(self):
        if self.tab == "largest":
            return self._largest_panel()
        table = Table.grid(padding=(0, 1))
        table.add_column(width=2)
        table.add_column(ratio=1, no_wrap=True)
        table.add_column(justify="right")
        items = self.items()
        colors = _branch_colors(items)
        top = max((i.get("size", 0) for i in items), default=1)
        height = self.console.size.height - 6
        start = max(0, min(self.sel - height // 2, len(items) - height))
        for i, item in enumerate(items[start: start + height], start=start):
            rgb = colors[i] if item.get("type") != "rest" else (88, 91, 112)
            selected = i == self.sel
            marker = Text("▎" if selected else " ", style=MOCHA["mauve"])
            marker.append("■", style=f"rgb({rgb[0]},{rgb[1]},{rgb[2]})")
            name = shorten_home(item.get("name", "?"))
            if item.get("type") == "dir":
                name += "/"
            bar_len = max(1, round(item.get("size", 0) / top * 8))
            label = Text(name, style=f"bold {MOCHA['text']}" if selected else MOCHA["text"])
            label.append("  " + "▁" * bar_len, style=f"rgb({rgb[0]},{rgb[1]},{rgb[2]})")
            size = Text(_fmt_bytes(item.get("size", 0)), style=MOCHA["sub"])
            if selected:
                for part in (marker, label, size):
                    part.stylize("on #262636")
            table.add_row(marker, label, size)
        return Panel(table, title="2 · contents", border_style=MOCHA["muted"])

    def _largest_panel(self):
        table = Table.grid(padding=(0, 1))
        table.add_column(ratio=1, no_wrap=True)
        table.add_column(justify="right")
        height = self.console.size.height - 6
        start = max(0, min(self.sel - height // 2, len(self.largest) - height))
        for i, f in enumerate(self.largest[start: start + height], start=start):
            name = Text(shorten_home(f["path"]), style=MOCHA["text"])
            size = Text(_fmt_bytes(f["size"]), style=MOCHA["sub"])
            if i == self.sel:
                name.stylize("reverse")
                size.stylize("reverse")
            table.add_row(name, size)
        return Panel(table, title="2 · largest files", border_style=MOCHA["muted"])

    def _info_panel(self) -> Panel:
        body = Text(no_wrap=False)
        if self.message:
            body.append("▸ ", style=MOCHA["mauve"])
            body.append(self.message + "\n\n", style=f"bold {MOCHA['mauve']}")
        item = self._selected_item()
        if item is not None:
            total = max(self.tree.get("size", 0), 1)
            pct = item.get("size", 0) / total * 100
            body.append(item.get("name", "?"), style=f"bold {MOCHA['text']}")
            body.append(f"\n{_fmt_bytes(item.get('size', 0))}", style=MOCHA["text"])
            body.append(f" · {pct:.1f}% of view", style=MOCHA["sub"])
            if item.get("type") == "dir" and item.get("files"):
                body.append(f" · {item['files']:,} files", style=MOCHA["sub"])
            mtime = item.get("mtime") or item.get("newest")
            if mtime:
                body.append(f"\nmodified {fmt_age(mtime)}", style=MOCHA["sub"])
            if item.get("path"):
                body.append(f"\n{shorten_home(item['path'])}", style=MOCHA["muted"])
        return Panel(body, title="3 · selected", border_style=MOCHA["muted"])

    def toast(self, message: str) -> None:
        self.message = message
        self._message_at = time.monotonic()

    def _center_lines(self) -> list[str] | None:
        item = self._selected_item()
        if item is None or item.get("type") == "rest":
            return None  # engine falls back to the view total
        total = max(self.tree.get("size", 0), 1)
        lines = [item.get("name", "?"),
                 f"{_fmt_bytes(item.get('size', 0))} · {item.get('size', 0) / total * 100:.0f}%"]
        if item.get("type") == "dir" and item.get("files"):
            lines.append(f"{item['files']:,} files")
        mtime = item.get("mtime") or item.get("newest")
        if mtime:
            lines.append(fmt_age(mtime))
        return lines

    def draw(self, screen, clear: bool = False) -> None:
        size = self.console.size
        if clear:
            # after a resize settles: multiplexers (zellij) keep stale cells in
            # newly exposed regions; a clear immediately followed by a full
            # frame is safe once the drag has ended
            sys.stdout.write("\x1b[2J\x1b[H")
        if size.width < 44 or size.height < 10:
            screen.update(Align.center(
                Text("terminal too small for diskburst", style=MOCHA["muted"]),
                vertical="middle",
            ))
            return
        side = self.sidebar_width() if (self.visible[2] or self.visible[3]) else 0
        # exact inner size of the chart panel: borders eat 2 cols + 2 rows,
        # header/footer eat 2 rows when visible
        chart_cols = size.width - side - 2
        chart_rows = size.height - 2 - (2 if self.chrome else 0)
        if self.renderer == "image":
            screen.update(self.compose(""))
            if self.visible[1]:
                self._draw_image(chart_cols, chart_rows)
        else:
            width = max(10, min(chart_cols, chart_rows * 2))
            highlight = None
            if self.tab == "contents" and self.sel < len(self.items()):
                if self.items()[self.sel].get("type") != "rest":
                    highlight = self.sel
            lines = render(self.tree, width=width, rings=3,
                           highlight=highlight, center=self._center_lines())
            screen.update(self.compose(Text.from_ansi("\n".join(lines))))

    def _draw_image(self, chart_cols: int, chart_rows: int) -> None:
        if self._png is None:
            self._png = encode_png(rasterize(self.tree, IMAGE_SIZE, 4), IMAGE_SIZE, IMAGE_SIZE)
        cells = max(20, min(chart_cols - 2, (chart_rows - 2) * 2))
        sys.stdout.write("\x1b[3;3H")  # inside the chart panel border
        display_inline(self._png, width_cells=cells)


def shorten_home(path: str) -> str:
    if path.startswith("/Users/") and path.count("/") >= 2:
        parts = path.split("/")
        return "~" + ("/" + "/".join(parts[3:]) if len(parts) > 3 else "")
    return path


def fmt_age(mtime: float) -> str:
    days = (time.time() - mtime) / 86400
    if days < 1:
        return "today"
    if days < 30:
        return f"{days:.0f}d ago"
    if days < 365:
        return f"{days / 30:.0f}mo ago"
    return f"{days / 365:.1f}y ago"


ARROWS = {65: "UP", 66: "DOWN", 67: "RIGHT", 68: "LEFT"}


def read_keys(fd: int) -> list[str]:
    """Parse every key out of the input buffer — autorepeat delivers bursts."""
    data = os.read(fd, 256)
    keys: list[str] = []
    i = 0
    while i < len(data):
        if data[i] == 0x1B and i + 2 < len(data) and data[i + 1] == 0x5B:
            arrow = ARROWS.get(data[i + 2])
            if arrow:
                keys.append(arrow)
            i += 3
        else:
            keys.append(chr(data[i]))
            i += 1
    return keys


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="diskburst terminal UI")
    parser.add_argument("--port", type=int, default=8890)
    parser.add_argument(
        "--renderer", choices=("braille", "image"), default="braille",
        help="chart renderer (image = iTerm2 inline PNG, experimental)",
    )
    args = parser.parse_args()

    if not sys.stdin.isatty():
        raise SystemExit("diskburst tui needs an interactive terminal")

    app = TuiApp(args.port, args.renderer)
    try:
        app.poll()
        app.refresh_tree()
    except OSError:
        raise SystemExit(
            f"diskburst server not reachable on port {args.port} — start it first"
        ) from None

    fd = sys.stdin.fileno()
    old_attrs = termios.tcgetattr(fd)
    tty.setcbreak(fd)
    resized = [False]
    signal.signal(signal.SIGWINCH, lambda *_: resized.__setitem__(0, True))
    try:
        with app.console.screen() as screen:
            app.draw(screen)
            running = True
            settle_at = 0.0  # one extra redraw after a resize storm ends
            while running:
                ready, _, _ = select.select([sys.stdin], [], [], 0.25)
                dirty = False
                if resized[0]:
                    resized[0] = False
                    # debounce: no drawing DURING a drag (the terminal may not
                    # drain our writes mid-resize, blocking the whole loop) —
                    # draw once after resize events pause
                    settle_at = time.monotonic() + 0.25
                needs_clear = False
                if settle_at and time.monotonic() >= settle_at:
                    settle_at = 0.0
                    dirty = True
                    needs_clear = True  # repaint from scratch at the new size
                if ready:
                    for key in read_keys(fd):
                        try:
                            running = app.handle(key)
                        except OSError as exc:  # server briefly unreachable etc.
                            app.toast(f"error: {exc}")
                        if not running:
                            break
                    dirty = True
                if app.message and time.monotonic() - app._message_at > 3.0:
                    app.message = ""  # toasts expire on their own
                    dirty = True
                if time.monotonic() - app._last_poll > POLL_SECONDS:
                    app._last_poll = time.monotonic()
                    if app.poll():
                        app.refresh_tree()
                        if app.tab == "largest":
                            app.refresh_largest()
                        dirty = True
                    elif app.status.get("state") != "done":
                        dirty = True  # keep the header counters moving mid-scan
                if dirty and running:
                    app.draw(screen, clear=needs_clear)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_attrs)
