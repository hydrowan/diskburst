"""Threaded, strictly read-only disk usage scanner.

Walks directory trees with os.scandir on a worker pool, accumulating
allocated-block sizes (st_blocks * 512 — the space you would actually free).
Never follows symlinks, never crosses filesystem boundaries, counts
hardlinked files once. The tree stays queryable while the scan runs;
cumulative totals are refreshed periodically and finalized on completion.
"""

from __future__ import annotations

import heapq
import os
import queue
import threading
import time
from dataclasses import dataclass, field

BLOCK_BYTES = 512
# Benchmarked on M-series/APFS: 4-8 workers ~100k entries/s warm; 12+ degrades
# (GIL handoff convoy), 48 was 1.7x SLOWER than 8. The kernel serializes APFS
# metadata ops past ~2 cores anyway, so more threads only add contention.
DEFAULT_WORKERS = 8
DEFAULT_BIG_FILE_BYTES = 1 * 1000 * 1000  # retain individual files >= 1 MB (filterable by name)


@dataclass(slots=True)
class DirNode:
    """One directory. `own_bytes` covers entries directly inside; totals are cumulative."""

    name: str
    own_bytes: int = 0
    total_bytes: int = 0
    file_count: int = 0
    total_files: int = 0
    own_newest: float = 0.0  # newest mtime among direct files
    newest: float = 0.0  # newest mtime in subtree (set by compute_totals)
    error: bool = False
    children: dict[str, "DirNode"] = field(default_factory=dict)
    big_files: list[tuple[str, int, float]] = field(default_factory=list)  # (name, bytes, mtime)


@dataclass(slots=True)
class ScanStats:
    files: int = 0
    dirs: int = 0
    bytes: int = 0
    errors: int = 0
    current_path: str = ""


class Scanner:
    """Scans one or more root directories concurrently; results live under `self.roots`."""

    def __init__(
        self,
        root_paths: list[str],
        big_file_bytes: int = DEFAULT_BIG_FILE_BYTES,
        workers: int = DEFAULT_WORKERS,
    ):
        self.root_paths = [os.path.abspath(os.path.expanduser(p)) for p in root_paths]
        self.roots: dict[str, DirNode] = {p: DirNode(name=p) for p in self.root_paths}
        self.big_file_bytes = big_file_bytes
        self.workers = workers
        self.stats = ScanStats()
        self.done = threading.Event()
        self.started_at = time.monotonic()
        self.finished_in: float | None = None
        self.cache_age: float | None = None  # set when this tree was loaded from disk cache
        self._queue: queue.Queue = queue.Queue()
        self._stats_lock = threading.Lock()
        self._hardlink_lock = threading.Lock()
        self._seen_hardlinks: set[tuple[int, int]] = set()
        self._cancelled = False

    def start(self) -> None:
        for path, node in self.roots.items():
            try:
                dev = os.lstat(path).st_dev
            except OSError:
                node.error = True
                self.stats.errors += 1
                continue
            self._queue.put((node, path, dev))
        for _ in range(self.workers):
            threading.Thread(target=self._worker, daemon=True).start()
        threading.Thread(target=self._refresher, daemon=True).start()
        threading.Thread(target=self._completion_watcher, daemon=True).start()

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    def close(self) -> None:
        """Cancel an in-flight scan and let worker threads exit (used on rescan/cancel).

        Remaining queued directories drain unscanned, so `done` still fires;
        callers must check `cancelled` before trusting the (partial) tree.
        """
        self._cancelled = True
        for _ in range(self.workers):
            self._queue.put(None)
        # In-flight _scan_dir calls may enqueue children AFTER the sentinels; once
        # all workers have exited nobody would task_done those, deadlocking join().
        threading.Thread(target=self._drain_after_cancel, daemon=True).start()

    def _drain_after_cancel(self) -> None:
        while not self.done.is_set():
            try:
                self._queue.get(timeout=0.2)
            except queue.Empty:
                continue
            self._queue.task_done()
        for _ in range(self.workers):  # release any workers whose sentinel we consumed
            self._queue.put(None)

    # ------------------------------------------------------------- workers

    def _worker(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                self._queue.task_done()
                return
            node, path, dev = item
            try:
                if not self._cancelled:
                    self._scan_dir(node, path, dev)
            finally:
                self._queue.task_done()

    def _scan_dir(self, node: DirNode, path: str, dev: int) -> None:
        own = 0
        nfiles = 0
        nerrors = 0
        newest = 0.0
        try:
            entries = os.scandir(path)
        except OSError:
            node.error = True
            with self._stats_lock:
                self.stats.errors += 1
                self.stats.dirs += 1
            return
        with entries:
            for entry in entries:
                try:
                    if entry.is_symlink():
                        continue
                    st = entry.stat(follow_symlinks=False)
                    if entry.is_dir(follow_symlinks=False):
                        if st.st_dev != dev:
                            continue  # do not cross mount points
                        own += st.st_blocks * BLOCK_BYTES  # the dir entry itself
                        child = DirNode(name=entry.name)
                        node.children[entry.name] = child
                        self._queue.put((child, entry.path, dev))
                    elif entry.is_file(follow_symlinks=False):
                        if st.st_nlink > 1 and not self._claim_hardlink(st):
                            continue
                        size = st.st_blocks * BLOCK_BYTES
                        own += size
                        nfiles += 1
                        if st.st_mtime > newest:
                            newest = st.st_mtime
                        if size >= self.big_file_bytes:
                            node.big_files.append((entry.name, size, st.st_mtime))
                except OSError:
                    nerrors += 1
        node.own_bytes = own
        node.file_count = nfiles
        node.own_newest = newest
        if nerrors:
            node.error = True
        with self._stats_lock:
            self.stats.files += nfiles
            self.stats.dirs += 1
            self.stats.bytes += own
            self.stats.errors += nerrors
            self.stats.current_path = path

    def _claim_hardlink(self, st: os.stat_result) -> bool:
        key = (st.st_dev, st.st_ino)
        with self._hardlink_lock:
            if key in self._seen_hardlinks:
                return False
            self._seen_hardlinks.add(key)
            return True

    # ------------------------------------------------------- housekeeping

    def _completion_watcher(self) -> None:
        self._queue.join()
        for node in self.roots.values():
            compute_totals(node)
        self.finished_in = time.monotonic() - self.started_at
        self.done.set()

    def _refresher(self) -> None:
        while not self.done.wait(timeout=2.0):
            for node in self.roots.values():
                compute_totals(node)

    # ------------------------------------------------------------ queries

    def find(self, path: str) -> tuple[DirNode, str] | None:
        """Resolve an absolute path to its node. Returns (node, node_path) or None."""
        path = path.rstrip("/") or "/"
        for root_path in sorted(self.root_paths, key=len, reverse=True):
            if path == root_path or path.startswith(root_path.rstrip("/") + "/"):
                node = self.roots[root_path]
                rel = path[len(root_path) :].strip("/")
                for part in rel.split("/") if rel else []:
                    child = node.children.get(part)
                    if child is None:
                        return None
                    node = child
                return node, path
        return None

    def iter_big_files(self, path: str = ""):
        """Yield (absolute_path, bytes, mtime) for every retained file, scoped to `path`."""
        if path:
            found_node = self.find(path)
            starts = [(found_node[0], found_node[1])] if found_node else []
        else:
            starts = [(root, root_path) for root_path, root in self.roots.items()]
        for root, root_path in starts:
            stack = [(root, root_path)]
            while stack:
                node, node_path = stack.pop()
                for name, size, mtime in node.big_files:
                    yield os.path.join(node_path, name), size, mtime
                for name, child in list(node.children.items()):
                    stack.append((child, os.path.join(node_path, name)))

    def largest_files(
        self, n: int = 200, path: str = "", match=None
    ) -> list[tuple[str, int, float]]:
        """Top-n retained files as (absolute_path, bytes, mtime), scoped and filtered."""
        found = [
            (s, p, m)
            for p, s, m in self.iter_big_files(path)
            if match is None or match(os.path.basename(p))
        ]
        return [(p, s, m) for s, p, m in heapq.nlargest(n, found)]


def compute_totals(root: DirNode) -> None:
    """Iterative post-order recompute of cumulative sizes (safe mid-scan; snapshots children)."""
    stack: list[tuple[DirNode, bool]] = [(root, False)]
    while stack:
        node, visited = stack.pop()
        if visited:
            total = node.own_bytes
            files = node.file_count
            newest = node.own_newest
            for child in list(node.children.values()):
                total += child.total_bytes
                files += child.total_files
                if child.newest > newest:
                    newest = child.newest
            node.total_bytes = total
            node.total_files = files
            node.newest = newest
        else:
            stack.append((node, True))
            for child in list(node.children.values()):
                stack.append((child, False))
