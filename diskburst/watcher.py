"""Live mode: FSEvents (via watchdog) keeps the served tree current between rescans.

Filesystem events mark directories dirty; a debounced flusher re-reads each
dirty directory in place — files re-summed, new subdirectories scanned in
full, vanished ones dropped — then cumulative totals refresh for the touched
roots. Import fails cleanly when watchdog is not installed; callers treat
live mode as optional.

Known drift: hardlink dedup and cross-device checks are skipped in live
updates (rare in practice); the next full rescan is authoritative.
"""

from __future__ import annotations

import os
import threading
import time

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from .scanner import BLOCK_BYTES, DirNode, compute_totals

DEBOUNCE_SECONDS = 2.0
MAX_DIRS_PER_FLUSH = 200  # overflow is dropped, not queued — live mode is best-effort

# High-churn, low-value paths: watching them floods the flusher with hundreds of
# events/second (process temp dirs, browser caches). Their sizes still update on
# every full rescan; the Advisor tab covers caches explicitly.
DENY_SUBSTRINGS = (
    "/private/var/folders/",
    "/private/var/db/",
    "/Library/Caches/",
    "/.git/objects/",
)


class _Handler(FileSystemEventHandler):
    def __init__(self, watcher: "LiveWatcher"):
        self._watcher = watcher

    def on_any_event(self, event) -> None:
        self._watcher.mark(os.path.dirname(event.src_path))
        if event.is_directory:
            self._watcher.mark(event.src_path)
        dest = getattr(event, "dest_path", None)
        if dest:
            self._watcher.mark(os.path.dirname(dest))


class LiveWatcher:
    """Watches the app's scan roots and applies changes to the served tree."""

    def __init__(self, app, debounce: float = DEBOUNCE_SECONDS):
        self.app = app
        self.debounce = debounce
        self.events = 0
        self.marks = 0  # raw event marks received (diagnostic)
        self.flushes = 0  # flush ticks that had work (diagnostic)
        self.find_failures = 0  # dirty paths not resolvable in the tree (diagnostic)
        self.last_change = 0.0
        self._dirty: set[str] = set()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._observer = Observer()

    def start(self) -> None:
        handler = _Handler(self)
        for root in self.app.scanner.root_paths:
            self._observer.schedule(handler, root, recursive=True)
        self._observer.daemon = True
        self._observer.start()
        threading.Thread(target=self._flush_loop, daemon=True).start()

    def stop(self) -> None:
        self._stop.set()
        self._observer.stop()

    def mark(self, path: str) -> None:
        if not path or any(deny in path + "/" for deny in DENY_SUBSTRINGS):
            return
        with self._lock:
            self.marks += 1
            self._dirty.add(path)

    # ------------------------------------------------------------ flushing

    def _flush_loop(self) -> None:
        while not self._stop.wait(self.debounce):
            with self._lock:
                batch, self._dirty = self._dirty, set()
            if not batch:
                continue
            scanner = self.app.scanner
            if (
                not scanner.done.is_set()
                or self.app.pending is not None
                or self.app.pending_dir is not None
            ):
                continue  # a scan is in flight and will capture these changes; drop
            self.flushes += 1
            touched_roots: set[str] = set()
            for path in sorted(batch)[:MAX_DIRS_PER_FLUSH]:
                root_path = self._refresh_dir(scanner, path)
                if root_path:
                    touched_roots.add(root_path)
            for root_path in touched_roots:
                compute_totals(scanner.roots[root_path])
            if touched_roots:
                self.events += min(len(batch), MAX_DIRS_PER_FLUSH)
                self.last_change = time.time()

    def _refresh_dir(self, scanner, path: str) -> str | None:
        """Re-read one directory in place; returns the root it lives under."""
        found = scanner.find(path)
        if found is None:
            self.find_failures += 1
            # not scanned yet (e.g. fresh deep tree): escalate to the parent next tick
            parent = os.path.dirname(path.rstrip("/"))
            if parent and parent != path and scanner.find(parent) is not None:
                self.mark(parent)
            return None
        node, node_path = found
        try:
            entries = os.scandir(node_path)
        except OSError:
            node.error = True
            return self._root_of(scanner, node_path)
        own = 0
        nfiles = 0
        newest = 0.0
        bigs: list[tuple[str, int, float]] = []
        live_dirs: set[str] = set()
        with entries:
            for entry in entries:
                try:
                    if entry.is_symlink():
                        continue
                    st = entry.stat(follow_symlinks=False)
                    if entry.is_dir(follow_symlinks=False):
                        live_dirs.add(entry.name)
                        own += st.st_blocks * BLOCK_BYTES
                        if entry.name not in node.children:
                            node.children[entry.name] = self._scan_subtree(
                                entry.path, scanner.big_file_bytes
                            )
                    elif entry.is_file(follow_symlinks=False):
                        size = st.st_blocks * BLOCK_BYTES
                        own += size
                        nfiles += 1
                        newest = max(newest, st.st_mtime)
                        if size >= scanner.big_file_bytes:
                            bigs.append((entry.name, size, st.st_mtime))
                except OSError:
                    pass
        for name in [n for n in node.children if n not in live_dirs]:
            del node.children[name]
        node.own_bytes = own
        node.file_count = nfiles
        node.own_newest = newest
        node.big_files = bigs
        return self._root_of(scanner, node_path)

    def _scan_subtree(self, path: str, big_file_bytes: int) -> DirNode:
        """Synchronously scan a newly appeared directory tree."""
        root = DirNode(name=os.path.basename(path))
        stack = [(root, path)]
        while stack:
            node, node_path = stack.pop()
            try:
                entries = os.scandir(node_path)
            except OSError:
                node.error = True
                continue
            own = 0
            nfiles = 0
            newest = 0.0
            with entries:
                for entry in entries:
                    try:
                        if entry.is_symlink():
                            continue
                        st = entry.stat(follow_symlinks=False)
                        if entry.is_dir(follow_symlinks=False):
                            own += st.st_blocks * BLOCK_BYTES
                            child = DirNode(name=entry.name)
                            node.children[entry.name] = child
                            stack.append((child, entry.path))
                        elif entry.is_file(follow_symlinks=False):
                            size = st.st_blocks * BLOCK_BYTES
                            own += size
                            nfiles += 1
                            newest = max(newest, st.st_mtime)
                            if size >= big_file_bytes:
                                node.big_files.append((entry.name, size, st.st_mtime))
                    except OSError:
                        pass
            node.own_bytes = own
            node.file_count = nfiles
            node.own_newest = newest
        return root

    @staticmethod
    def _root_of(scanner, path: str) -> str | None:
        for root_path in scanner.root_paths:
            if path == root_path or path.startswith(root_path.rstrip("/") + "/"):
                return root_path
        return None
