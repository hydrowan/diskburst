"""Localhost HTTP server exposing the scan as JSON plus the static UI.

Strictly read-only with one benign action: POST /api/reveal runs Finder's
`open` / `open -R` on a scanned path. Nothing here deletes, moves, or writes.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .advisor import advise
from .cache import save_cache
from .diff import diff_against_snapshot
from .dupes import find_duplicates, full_hash
from .filter import FilterError, TreeFilter
from .scanner import Scanner, compute_totals
from .serialize import serialize_view

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
}


def disk_free() -> dict:
    st = os.statvfs("/System/Volumes/Data" if os.path.exists("/System/Volumes/Data") else "/")
    return {"free": st.f_bavail * st.f_frsize, "total": st.f_blocks * st.f_frsize}


class App:
    """Owns the served scanner; rescans run in the background and swap in when complete.

    The served tree therefore always stays complete (cached or previous scan) —
    except on a cold start with no cache, where the first scan streams in live.
    """

    def __init__(
        self,
        make_scanner: Callable[[], Scanner],
        cached: Scanner | None = None,
        live: bool = True,
    ):
        self._make_scanner = make_scanner
        self._lock = threading.Lock()
        self.pending: Scanner | None = None
        self.pending_dir: tuple[Scanner, str] | None = None  # (sub-scanner, subtree path)
        self._from_cache = cached is not None
        self.scanner = cached if cached is not None else make_scanner()
        self._memo: dict = {}  # (scanner id, kind, arg) -> computed result
        self._live_requested = live
        self.watcher = None

    def memo(self, kind: str, arg: str, compute):
        """Cache expensive per-scan computations (dupes, diffs) until the tree changes."""
        generation = (id(self.scanner), self.scanner.stats.files)
        if self._memo.get("generation") != generation:
            self._memo = {"generation": generation}
        key = (kind, arg)
        if key not in self._memo:
            self._memo[key] = compute()
        return self._memo[key]

    def start(self) -> None:
        """Kick off scanning (separate from __init__ so the port binds first)."""
        if self._from_cache:
            self._start_watcher()  # cached tree is complete; watch it right away
            self.rescan()  # and refresh it in the background
        else:
            self.scanner.start()
            threading.Thread(
                target=self._save_when_done, args=(self.scanner,), daemon=True
            ).start()

    def _start_watcher(self) -> None:
        if not self._live_requested or self.watcher is not None:
            return
        try:
            from .watcher import LiveWatcher

            watcher = LiveWatcher(self)
            watcher.start()
            self.watcher = watcher
        except ImportError:
            self.watcher = None  # watchdog not installed — live mode off

    def rescan(self) -> None:
        with self._lock:
            if self.pending is not None:
                return
            fresh = self._make_scanner()
            self.pending = fresh
        fresh.start()
        threading.Thread(target=self._adopt_when_done, args=(fresh,), daemon=True).start()

    def _adopt_when_done(self, fresh: Scanner) -> None:
        fresh.done.wait()
        if fresh.cancelled:
            with self._lock:
                if self.pending is fresh:
                    self.pending = None
            return
        save_cache(fresh)
        with self._lock:
            old = self.scanner
            self.scanner = fresh
            self.pending = None
        if old is not fresh:
            old.close()
        self._start_watcher()

    def _save_when_done(self, scanner: Scanner) -> None:
        scanner.done.wait()
        if scanner is self.scanner:
            save_cache(scanner)
            self._start_watcher()

    def cancel_rescan(self) -> tuple[bool, str]:
        """Cancel a background rescan (full or dir). The initial scan can't be cancelled."""
        with self._lock:
            target = self.pending or (self.pending_dir[0] if self.pending_dir else None)
            if target is None:
                return False, "no rescan running"
            target.close()  # adopt/graft threads observe `cancelled` and discard it
        return True, "ok"

    def rescan_dir(self, path: str) -> tuple[bool, str]:
        """Rescan one subtree and graft it into the served tree when done."""
        with self._lock:
            if self.pending is not None or self.pending_dir is not None:
                return False, "a rescan is already running"
            if not self.scanner.done.is_set():
                return False, "initial scan still running"
            if self.scanner.find(path) is None:
                return False, "path not in scanned tree"
            sub = Scanner(
                [path],
                big_file_bytes=self.scanner.big_file_bytes,
                workers=self.scanner.workers,
            )
            self.pending_dir = (sub, path)
        sub.start()
        threading.Thread(target=self._graft_when_done, args=(sub, path), daemon=True).start()
        return True, "ok"

    def _graft_when_done(self, sub: Scanner, path: str) -> None:
        sub.done.wait()
        if sub.cancelled:
            with self._lock:
                self.pending_dir = None
            return
        with self._lock:
            target = self.scanner
            grafted = _graft_subtree(target, path, sub.roots[sub.root_paths[0]])
            self.pending_dir = None
        if grafted:
            save_cache(target)
        sub.close()


def _graft_subtree(target: Scanner, path: str, fresh) -> bool:
    """Replace the node at `path` in target's tree with a freshly scanned subtree.

    Files hardlinked both inside and outside the subtree may count twice after a
    graft (each scan claims hardlinks independently) — negligible in practice
    and corrected by the next full rescan.
    """
    if path in target.roots:
        fresh.name = path
        target.roots[path] = fresh
    else:
        found = target.find(os.path.dirname(path.rstrip("/")))
        if found is None:
            return False
        parent_node, _ = found
        fresh.name = os.path.basename(path.rstrip("/"))
        parent_node.children[fresh.name] = fresh
    dirs = 0
    for root_path, root in target.roots.items():
        if path == root_path or path.startswith(root_path.rstrip("/") + "/"):
            compute_totals(root)
        stack = [root]
        while stack:
            node = stack.pop()
            dirs += 1
            stack.extend(node.children.values())
    target.stats.bytes = sum(r.total_bytes for r in target.roots.values())
    target.stats.files = sum(r.total_files for r in target.roots.values())
    target.stats.dirs = dirs
    return True


class ApiHandler(BaseHTTPRequestHandler):
    app: App  # injected by make_server

    def do_GET(self) -> None:  # noqa: N802 (http.server API)
        url = urlparse(self.path)
        scanner = self.app.scanner
        if url.path in STATIC_FILES:
            self._send_static(*STATIC_FILES[url.path])
        elif url.path == "/api/status":
            self._send_json(self._status(scanner))
        elif url.path == "/api/tree":
            q = parse_qs(url.query)
            path = q.get("path", [""])[0]
            depth = min(int(q.get("depth", ["4"])[0]), 6)
            try:
                flt = self._tree_filter(scanner, q)
            except FilterError as e:
                self._send_json({"error": str(e)}, status=400)
                return
            self._send_json(serialize_view(scanner, path, depth, flt))
        elif url.path == "/api/largest":
            q = parse_qs(url.query)
            n = min(int(q.get("n", ["200"])[0]), 1000)
            path = q.get("path", [""])[0]
            try:
                flt = self._tree_filter(scanner, q)
            except FilterError as e:
                self._send_json({"error": str(e)}, status=400)
                return
            files = scanner.largest_files(n, path=path, match=flt.match if flt else None)
            self._send_json(
                {
                    "path": path,
                    "files": [{"path": p, "size": s, "mtime": m} for p, s, m in files],
                }
            )
        elif url.path == "/api/diff":
            q = parse_qs(url.query)
            which = q.get("baseline", ["prev"])[0]
            if which not in ("prev", "oldest") and not which.isdigit():
                which = "prev"
            result = self.app.memo(
                "diff", which, lambda: diff_against_snapshot(scanner, which)
            )
            path = q.get("path", [""])[0]
            if path and result.get("available"):
                prefix = path.rstrip("/") + "/"
                result = {
                    **result,
                    "scope": path,
                    "entries": [
                        e
                        for e in result["entries"]
                        if e["path"] == path or e["path"].startswith(prefix)
                    ],
                }
            self._send_json(result)
        elif url.path == "/api/advisor":
            self._send_json({"entries": self.app.memo("advisor", "", lambda: advise(scanner))})
        elif url.path == "/api/duplicates":
            self._send_json(self.app.memo("dupes", "", lambda: find_duplicates(scanner)))
        else:
            self.send_error(404)

    def do_POST(self) -> None:  # noqa: N802
        route = urlparse(self.path).path
        if route == "/api/rescan":
            self.app.rescan()
            self._send_json({"ok": True})
        elif route == "/api/rescan_dir":
            ok, msg = self.app.rescan_dir(self._body().get("path", ""))
            self._send_json({"ok": ok, "error": None if ok else msg}, status=200 if ok else 409)
        elif route == "/api/rescan/cancel":
            ok, msg = self.app.cancel_rescan()
            self._send_json({"ok": ok, "error": None if ok else msg}, status=200 if ok else 409)
        elif route == "/api/duplicates/verify":
            self._verify_duplicates()
        elif route == "/api/reveal":
            self._reveal()
        else:
            self.send_error(404)

    def _tree_filter(self, scanner: Scanner, q: dict):
        """Build (memoized) TreeFilter from query params; None when no filter active."""
        query = q.get("q", [""])[0]
        mode = q.get("mode", ["fuzzy"])[0]
        exts = [e for e in q.get("exts", [""])[0].split(",") if e.strip()]
        if not query.strip() and not exts:
            return None
        key = f"{mode}\x00{query}\x00{','.join(sorted(exts))}"
        return self.app.memo("filter", key, lambda: TreeFilter(scanner, mode, query, exts))

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        return json.loads(self.rfile.read(length) or b"{}")

    def _verify_duplicates(self) -> None:
        paths = self._body().get("paths", [])[:50]
        allowed = [
            p
            for p in paths
            if any(
                p == root or p.startswith(root.rstrip("/") + "/")
                for root in self.app.scanner.root_paths
            )
        ]
        hashes = {p: full_hash(p) for p in allowed}
        values = [h for h in hashes.values() if h]
        match = len(values) == len(allowed) >= 2 and len(set(values)) == 1
        self._send_json({"hashes": hashes, "match": match})

    def _reveal(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length) or b"{}")
        path = os.path.abspath(body.get("path", ""))
        allowed = any(
            path == root or path.startswith(root.rstrip("/") + "/")
            for root in self.app.scanner.root_paths
        )
        if not allowed or not os.path.lexists(path):
            self._send_json({"ok": False, "error": "path outside scanned roots"}, status=400)
            return
        open_dir = body.get("mode") == "open" and os.path.isdir(path)
        subprocess.Popen(["open", path] if open_dir else ["open", "-R", path])
        self._send_json({"ok": True})

    def _status(self, s: Scanner) -> dict:
        pending = self.app.pending
        pending_dir = self.app.pending_dir
        state = "done" if s.done.is_set() else "scanning"
        if state == "done" and (pending is not None or pending_dir is not None):
            state = "rescanning"
        out = {
            "state": state,
            "files": s.stats.files,
            "dirs": s.stats.dirs,
            "bytes": s.stats.bytes,
            "errors": s.stats.errors,
            "current": s.stats.current_path,
            "elapsed": round(s.finished_in or (time.monotonic() - s.started_at), 1),
            "cache_age": s.cache_age,
            "roots": s.root_paths,
            "disk": disk_free(),
            "live": self._live_status(),
        }
        active = pending if pending is not None else (pending_dir[0] if pending_dir else None)
        if active is not None:
            out["rescan"] = {
                "files": active.stats.files,
                "bytes": active.stats.bytes,
                "current": active.stats.current_path,
                "elapsed": round(time.monotonic() - active.started_at, 1),
                "scope": pending_dir[1] if pending_dir else "",
            }
        return out

    def _live_status(self) -> dict:
        w = self.app.watcher
        if w is None:
            return {"enabled": False, "events": 0, "last_change": 0}
        return {
            "enabled": True,
            "events": w.events,
            "last_change": w.last_change,
            "marks": w.marks,
            "flushes": w.flushes,
            "find_failures": w.find_failures,
        }

    def _send_json(self, obj: dict, status: int = 200) -> None:
        data = json.dumps(obj).encode("utf-8", errors="replace")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_static(self, filename: str, content_type: str) -> None:
        with open(os.path.join(STATIC_DIR, filename), "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args) -> None:  # silence per-request stderr noise
        pass


def make_server(app: App, host: str = "127.0.0.1", port: int = 8890) -> ThreadingHTTPServer:
    handler = type("BoundApiHandler", (ApiHandler,), {"app": app})
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    return server
