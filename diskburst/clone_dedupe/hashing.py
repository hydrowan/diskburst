"""Streaming SHA-256 digests (head-only or full) fanned out over a small process pool."""

from __future__ import annotations

import hashlib
import os
from collections.abc import Callable, Iterator
from concurrent.futures import ProcessPoolExecutor

HEAD_BYTES = 64 * 1024
CHUNK_BYTES = 4 * 1024 * 1024
POOL_CHUNKSIZE = 32
BATCH_PATHS = 4096


def head_digest(path: str) -> str | None:
    """SHA-256 of the first HEAD_BYTES of `path`; None if unreadable."""
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read(HEAD_BYTES)).hexdigest()
    except OSError:
        return None


def full_digest(path: str) -> str | None:
    """SHA-256 of all of `path`, streamed; None if unreadable."""
    try:
        with open(path, "rb") as f:
            return hashlib.file_digest(f, "sha256").hexdigest()
    except OSError:
        return None


def sampled_digest(path: str, samples: int = 8) -> str | None:
    """SHA-256 over `samples` evenly spaced CHUNK_BYTES windows plus the size; None if unreadable."""
    try:
        with open(path, "rb") as f:
            size = os.fstat(f.fileno()).st_size
            digest = hashlib.sha256(str(size).encode())
            for i in range(samples):
                f.seek(size * i // samples)
                digest.update(f.read(CHUNK_BYTES))
            return digest.hexdigest()
    except OSError:
        return None


def digest_many(digest: Callable[[str], str | None], paths: list[str], workers: int) -> Iterator[str | None]:
    """Yield `digest(path)` for each path in order, using `workers` processes (1 = inline).

    Args:
        digest: a top-level function from this module.
        paths: files to digest.
        workers: process count.

    Returns:
        Iterator of digests aligned with `paths`.
    """
    if workers <= 1:
        yield from map(digest, paths)
        return
    with ProcessPoolExecutor(workers) as pool:
        # Executor.map submits every task up front; batching bounds pending futures.
        for start in range(0, len(paths), BATCH_PATHS):
            yield from pool.map(digest, paths[start:start + BATCH_PATHS], chunksize=POOL_CHUNKSIZE)
