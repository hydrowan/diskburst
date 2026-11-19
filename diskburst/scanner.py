"""Threaded, strictly read-only disk usage scanner.

Walks directory trees with os.scandir on a worker pool, accumulating
allocated-block sizes (st_blocks * 512). Never follows symlinks, never crosses
filesystem boundaries, counts hardlinked files once. The tree stays queryable
while the scan runs; cumulative totals are refreshed periodically and
finalized on completion. With `clone_aware`, macOS scans list directories via
getattrlistbulk and also compute APFS clone-aware unique and freed sizes; there
apparent size and file count include every hard-link name, and unique counts
each inode once.
"""

from __future__ import annotations

import heapq
import os
import queue
import threading
import time
from dataclasses import dataclass, field

from . import bulk_listing
from .clones import CloneLedger

BLOCK_BYTES = 512
# Benchmarked on M-series/APFS: 4-8 workers ~100k entries/s warm; 12+ degrades
# (GIL handoff convoy), 48 was 1.7x SLOWER than 8. The kernel serializes APFS
# metadata ops past ~2 cores anyway, so more threads only add contention.
DEFAULT_WORKERS = 8
DEFAULT_BIG_FILE_BYTES = 1 * 1000 * 1000  # retain individual files >= 1 MB (filterable by name)
BULK_LISTING_ATTEMPTS = 2


@dataclass(slots=True)
class DirNode:
    """One directory. `own_*` cover entries directly inside; totals are cumulative.

    unique: bytes counting each APFS clone group once. freed: bytes released if
    this directory alone were deleted. Both equal the apparent size without
    clone data, and own_unique / own_freed include clone-group corrections.
    """

    name: str
    own_bytes: int = 0
    total_bytes: int = 0
    own_unique: int = 0
    unique_bytes: int = 0
    own_freed: int = 0
    freed_bytes: int = 0
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
    listing_failures: int = 0  # bulk listings that failed and fell back to os.scandir
    current_path: str = ""


class Scanner:
    """Scans one or more root directories concurrently; results live under `self.roots`."""

    def __init__(
        self,
        root_paths: list[str],
        big_file_bytes: int = DEFAULT_BIG_FILE_BYTES,
        workers: int = DEFAULT_WORKERS,
        clone_aware: bool = False,
        refresh_totals: bool = True,
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
        self.refresh_totals = refresh_totals
        self.clone_ledger = CloneLedger() if clone_aware and bulk_listing.AVAILABLE else None

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
        if self.refresh_totals:
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
                    scan = self._scan_dir if self.clone_ledger is None else self._scan_dir_bulk
                    scan(node, path, dev)
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
                        if st.st_nlink > 1 and not self._claim_hardlink((st.st_dev, st.st_ino)):
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
        node.own_unique = node.own_freed = own
        self._finish_dir(node, path, own, nfiles, newest, nerrors)

    def _list_bulk(self, path: str) -> list[tuple] | None:
        for _ in range(BULK_LISTING_ATTEMPTS):
            try:
                return bulk_listing.list_directory(path)
            except OSError:
                continue
        with self._stats_lock:
            self.stats.listing_failures += 1
        return None

    def _scan_dir_bulk(self, node: DirNode, path: str, dev: int) -> None:
        entries = self._list_bulk(path)
        if entries is None:
            self._scan_dir(node, path, dev)
            return
        own = unique = freed = nfiles = nerrors = 0
        newest = 0.0
        clones: list[tuple] = []
        links: list[tuple] = []
        for (objtype, name, entry_dev, mtime, fileid, nlink, alloc, private, cloneid, refcnt,
             is_mount) in entries:
            if objtype == bulk_listing.VDIR:
                if entry_dev != dev or is_mount:
                    continue
                own += alloc
                unique += alloc
                freed += alloc
                child = DirNode(name=name)
                node.children[name] = child
                self._queue.put((child, os.path.join(path, name), dev))
                continue
            if objtype != bulk_listing.VREG:
                continue
            if alloc < 0:
                nerrors += 1
                continue
            own += alloc
            nfiles += 1
            if mtime > newest:
                newest = mtime
            if alloc >= self.big_file_bytes:
                node.big_files.append((name, alloc, mtime))
            if nlink > 1:
                links.append((entry_dev, fileid, nlink, alloc, private, cloneid, refcnt))
            elif refcnt > 1:
                clones.append((cloneid, alloc, private, refcnt))
                freed += private
            else:
                unique += alloc
                # refcnt 1 with private < alloc is a modified clone still sharing blocks;
                # refcnt 0 (e.g. decmpfs-compressed) reports no usable private size.
                freed += private if refcnt == 1 else alloc
        if clones or links:
            self.clone_ledger.record(node, clones, links)
        node.own_unique = unique
        node.own_freed = freed
        self._finish_dir(node, path, own, nfiles, newest, nerrors)

    def _finish_dir(
        self, node: DirNode, path: str, own: int, nfiles: int, newest: float, nerrors: int
    ) -> None:
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

    def _claim_hardlink(self, key: tuple[int, int]) -> bool:
        with self._hardlink_lock:
            if key in self._seen_hardlinks:
                return False
            self._seen_hardlinks.add(key)
            return True

    # ------------------------------------------------------- housekeeping

    def _completion_watcher(self) -> None:
        self._queue.join()
        if self.clone_ledger is not None and not self._cancelled:
            self.clone_ledger.apply(list(self.roots.values()))
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
            unique = node.own_unique
            freed = node.own_freed
            files = node.file_count
            newest = node.own_newest
            for child in list(node.children.values()):
                total += child.total_bytes
                unique += child.unique_bytes
                freed += child.freed_bytes
                files += child.total_files
                if child.newest > newest:
                    newest = child.newest
            node.total_bytes = total
            node.unique_bytes = unique
            node.freed_bytes = freed
            node.total_files = files
            node.newest = newest
        else:
            stack.append((node, True))
            for child in list(node.children.values()):
                stack.append((child, False))
