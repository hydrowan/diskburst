"""Persist completed scans to disk so reopening shows the last result instantly.

The cache is a gzipped JSON snapshot of the directory tree (dirs + retained big
files only, not every file), keyed by the scanned roots and retention threshold.
A loaded cache is served immediately while a fresh scan runs in the background.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import sys
import threading
import time

from .scanner import DirNode, Scanner, compute_totals

CACHE_DIR = os.path.expanduser("~/.cache/diskburst")
CACHE_VERSION = 2  # v2: mtimes on files and dirs
_save_lock = threading.Lock()  # rotation + write must not interleave across threads
HISTORY_CAP = 12  # hard ceiling on stored snapshots per key
_JSON_RECURSION_LIMIT = 50_000  # tree depth is path depth (<=~512 on APFS); default 1000 is tight


def cache_key(root_paths: list[str], big_file_bytes: int) -> str:
    key = json.dumps([sorted(root_paths), big_file_bytes])
    return hashlib.sha1(key.encode()).hexdigest()[:12]


def cache_path(root_paths: list[str], big_file_bytes: int) -> str:
    return os.path.join(CACHE_DIR, f"scan-{cache_key(root_paths, big_file_bytes)}.json.gz")


def _history_files(key: str) -> list[str]:
    """Timestamped snapshot files for a key, oldest first."""
    try:
        names = [
            n for n in os.listdir(CACHE_DIR)
            if n.startswith(f"scan-{key}-") and n.endswith(".json.gz")
        ]
    except OSError:
        return []
    return [os.path.join(CACHE_DIR, n) for n in sorted(names)]


def snapshot_stamp(path: str) -> int:
    """Unix timestamp encoded in a history filename."""
    return int(os.path.basename(path).rsplit("-", 1)[1].split(".")[0])


def _rotate_history(root_paths: list[str], big_file_bytes: int) -> None:
    """Move the current 'latest' snapshot into history, then thin by age.

    Rescans are cheap and frequent, so per-rescan history is noise. Retention
    is time-bucketed: ~2-hourly for the last 6 h, daily out to 8 days, weekly
    beyond — so diffs can span days of change, not minutes.
    """
    latest = cache_path(root_paths, big_file_bytes)
    key = cache_key(root_paths, big_file_bytes)
    if os.path.exists(latest):
        stamp = int(os.path.getmtime(latest))
        os.replace(latest, os.path.join(CACHE_DIR, f"scan-{key}-{stamp:012d}.json.gz"))
    files = _history_files(key)
    now = time.time()
    kept: list[str] = []
    last_kept_stamp = None
    for path in reversed(files):  # newest first; the newest is always kept
        stamp = snapshot_stamp(path)
        age = now - stamp
        spacing = 7200 if age < 6 * 3600 else 86400 if age < 8 * 86400 else 7 * 86400
        if last_kept_stamp is None or last_kept_stamp - stamp >= spacing:
            kept.append(path)
            last_kept_stamp = stamp
    keep = set(kept[:HISTORY_CAP])
    for path in files:
        if path not in keep:
            os.unlink(path)


def history_snapshots(root_paths: list[str], big_file_bytes: int) -> list[dict]:
    """Available baselines, newest first, as {stamp, age} dicts."""
    now = time.time()
    files = _history_files(cache_key(root_paths, big_file_bytes))
    return [{"stamp": snapshot_stamp(p), "age": now - snapshot_stamp(p)} for p in reversed(files)]


def load_snapshot(root_paths: list[str], big_file_bytes: int, which: str = "prev") -> dict | None:
    """Load a snapshot payload: 'prev' (newest), 'oldest', or a specific stamp."""
    key = cache_key(root_paths, big_file_bytes)
    history = _history_files(key)
    if not history:
        return None
    if which == "prev":
        path = history[-1]
    elif which == "oldest":
        path = history[0]
    else:
        path = next((p for p in history if str(snapshot_stamp(p)) == which), history[-1])
    payload = _read_payload(path)
    if payload is not None:
        payload["_stamp"] = snapshot_stamp(path)
    return payload


def _read_payload(path: str) -> dict | None:
    try:
        old_limit = sys.getrecursionlimit()
        sys.setrecursionlimit(_JSON_RECURSION_LIMIT)
        try:
            with gzip.open(path, "rb") as f:
                payload = json.loads(f.read())
        finally:
            sys.setrecursionlimit(old_limit)
        return payload if payload.get("version") == CACHE_VERSION else None
    except (OSError, ValueError):
        return None


def save_cache(scanner: Scanner) -> None:
    payload = {
        "version": CACHE_VERSION,
        "saved_at": time.time(),
        "roots": scanner.root_paths,
        "big_file_bytes": scanner.big_file_bytes,
        "trees": {p: _encode(node) for p, node in scanner.roots.items()},
    }
    old_limit = sys.getrecursionlimit()
    sys.setrecursionlimit(_JSON_RECURSION_LIMIT)
    try:
        data = json.dumps(payload).encode("utf-8", errors="replace")
    finally:
        sys.setrecursionlimit(old_limit)
    with _save_lock:
        path = cache_path(scanner.root_paths, scanner.big_file_bytes)
        os.makedirs(CACHE_DIR, mode=0o700, exist_ok=True)
        _rotate_history(scanner.root_paths, scanner.big_file_bytes)
        tmp = f"{path}.{threading.get_ident()}.tmp"
        with gzip.open(tmp, "wb", compresslevel=1) as f:
            f.write(data)
        os.chmod(tmp, 0o600)  # snapshots are a filename census of the disk — owner-only
        os.replace(tmp, path)


def load_cache(root_paths: list[str], big_file_bytes: int, workers: int) -> Scanner | None:
    """Rebuild a completed Scanner from cache, or None if absent/stale-format/corrupt."""
    path = cache_path([os.path.abspath(os.path.expanduser(p)) for p in root_paths], big_file_bytes)
    payload = _read_payload(path)
    if payload is None:
        return None
    try:
        scanner = Scanner(payload["roots"], big_file_bytes=big_file_bytes, workers=workers)
        dirs = 0
        for root_path, encoded in payload["trees"].items():
            node, count = _decode(encoded)
            scanner.roots[root_path] = node
            compute_totals(node)
            dirs += count
        scanner.stats.files = sum(r.total_files for r in scanner.roots.values())
        scanner.stats.bytes = sum(r.total_bytes for r in scanner.roots.values())
        scanner.stats.dirs = dirs
        scanner.finished_in = 0.0
        scanner.cache_age = time.time() - payload["saved_at"]
        scanner.done.set()
        return scanner
    except (ValueError, KeyError):
        return None


def _encode(root: DirNode) -> dict:
    out_root: dict = {}
    stack = [(root, out_root)]
    while stack:
        node, out = stack.pop()
        out["n"] = node.name
        out["o"] = node.own_bytes
        out["f"] = node.file_count
        if node.own_newest:
            out["m"] = node.own_newest
        if node.error:
            out["e"] = 1
        if node.big_files:
            out["b"] = [[name, size, mtime] for name, size, mtime in node.big_files]
        if node.children:
            kids: list[dict] = []
            for child in node.children.values():
                child_out: dict = {}
                kids.append(child_out)
                stack.append((child, child_out))
            out["c"] = kids
    return out_root


def _decode(encoded: dict) -> tuple[DirNode, int]:
    root = DirNode(name=encoded["n"])
    stack = [(encoded, root)]
    count = 0
    while stack:
        enc, node = stack.pop()
        count += 1
        node.own_bytes = enc["o"]
        node.file_count = enc["f"]
        node.own_newest = enc.get("m", 0.0)
        node.error = bool(enc.get("e"))
        node.big_files = [(name, size, mtime) for name, size, mtime in enc.get("b", [])]
        for child_enc in enc.get("c", []):
            child = DirNode(name=child_enc["n"])
            node.children[child.name] = child
            stack.append((child_enc, child))
    return root, count
