"""Duplicate finder over retained big files: size-match, then head+tail fingerprint.

Candidates share an exact allocated size; each is then fingerprinted with
SHA-256 over (logical size, first 1 MB, last 1 MB) — near-certain for real
duplicates without reading multi-GB files end to end. Read-only; suggested
dedupe commands are display-only strings.
"""

from __future__ import annotations

import hashlib
import os
from collections import defaultdict

from .scanner import Scanner

MIN_DUPE_BYTES = 50 * 1000 * 1000
CHUNK = 1024 * 1024
MAX_CANDIDATES = 800


def find_duplicates(scanner: Scanner) -> dict:
    """Groups of probable duplicates, biggest wasted bytes first."""
    by_size: dict[int, list[tuple[str, float]]] = defaultdict(list)
    for path, size, mtime in scanner.iter_big_files():
        if size >= MIN_DUPE_BYTES and "/.Trash/" not in path:
            by_size[size].append((path, mtime))

    groups = []
    candidates = 0
    for size, files in sorted(by_size.items(), reverse=True):
        if len(files) < 2 or candidates > MAX_CANDIDATES:
            continue
        candidates += len(files)
        by_print: dict[str, list[dict]] = defaultdict(list)
        for path, mtime in files:
            digest = _fingerprint(path)
            if digest is not None:
                by_print[digest].append({"path": path, "mtime": mtime})
        for digest, members in by_print.items():
            if len(members) < 2:
                continue
            members.sort(key=lambda m: len(m["path"]))  # keep the shortest path
            keep = members[0]["path"]
            groups.append(
                {
                    "size": size,
                    "wasted": size * (len(members) - 1),
                    "keep": keep,
                    "files": [m["path"] for m in members],
                    "commands": [
                        f'rm "{m["path"]}" && ln -s "{keep}" "{m["path"]}"'
                        for m in members[1:]
                    ],
                }
            )
    groups.sort(key=lambda g: g["wasted"], reverse=True)
    return {"groups": groups, "wasted": sum(g["wasted"] for g in groups)}


def full_hash(path: str) -> str | None:
    """Full-file SHA-256 (streamed); None if unreadable. Used for on-demand verification."""
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            while chunk := f.read(4 * CHUNK):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def _fingerprint(path: str) -> str | None:
    """SHA-256 of (logical size, head 1MB, tail 1MB); None if unreadable."""
    try:
        with open(path, "rb") as f:
            size = os.fstat(f.fileno()).st_size
            h = hashlib.sha256(str(size).encode())
            h.update(f.read(CHUNK))
            if size > 2 * CHUNK:
                f.seek(-CHUNK, os.SEEK_END)
            h.update(f.read(CHUNK))
        return h.hexdigest()
    except OSError:
        return None
