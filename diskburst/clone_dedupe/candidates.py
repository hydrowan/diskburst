"""Narrow inodes to byte-identical content groups: size, screening, head hash, full SHA-256."""

from __future__ import annotations

import os
import stat
from collections import Counter, defaultdict
from collections.abc import Callable, Hashable

from .hashing import HEAD_BYTES, digest_many, full_digest, head_digest
from .inventory import Inode
from .metadata import unusual_metadata

NANOS_PER_HOUR = 3600 * 1_000_000_000


class SkipTally:
    """Per-reason counts of skipped names and their logical bytes, plus xattr names seen."""

    def __init__(self):
        self.reasons: dict[str, list[int]] = defaultdict(lambda: [0, 0])
        self.xattrs: Counter[str] = Counter()

    def add(self, reason: str, inode: Inode) -> None:
        """Count every name of `inode` as skipped for `reason`."""
        tally = self.reasons[reason]
        tally[0] += len(inode.names)
        tally[1] += len(inode.names) * inode.size


def multi_copy_groups(inodes: list[Inode], key: Callable[[Inode], Hashable]) -> dict:
    """Group inodes by `key`, keeping groups that span at least two physical copies."""
    groups: dict[Hashable, list[Inode]] = defaultdict(list)
    for inode in inodes:
        groups[key(inode)].append(inode)
    return {k: g for k, g in groups.items() if len({i.unit_key for i in g}) > 1}


def screen(inodes: list[Inode], now_ns: int, min_age_hours: float, skips: SkipTally) -> list[Inode]:
    """Drop recently modified, unusual-metadata and changed-since-listing inodes; record mode.

    Args:
        inodes: same-size candidates.
        now_ns: planning time.
        min_age_hours: files modified more recently are skipped.
        skips: tally updated in place.

    Returns:
        Inodes safe to consider, with `mode` set.
    """
    cutoff = now_ns - int(min_age_hours * NANOS_PER_HOUR)
    kept = []
    for inode in inodes:
        reason = _skip_reason(inode, cutoff, skips)
        if reason:
            skips.add(reason, inode)
        else:
            kept.append(inode)
    return kept


def _skip_reason(inode: Inode, cutoff_ns: int, skips: SkipTally) -> str | None:
    if inode.mtime_ns > cutoff_ns:
        return "modified_within_min_age"
    path = inode.names[0]
    try:
        st = os.lstat(path)
        reasons, names = unusual_metadata(path, st.st_flags)
    except OSError:
        return "unreadable"
    skips.xattrs.update(names)
    if not stat.S_ISREG(st.st_mode) or (st.st_ino, st.st_size, st.st_mtime_ns) != (
        inode.fileid, inode.size, inode.mtime_ns
    ):
        return "changed_during_planning"
    if reasons:
        return "unusual_metadata"
    inode.mode = st.st_mode
    return None


def content_groups(inodes: list[Inode], workers: int, skips: SkipTally) -> dict[str, list[Inode]]:
    """Hash candidates (head, then full) and return SHA-256 -> inodes with identical bytes.

    Args:
        inodes: screened candidates.
        workers: hashing processes.
        skips: unreadable files are tallied here.

    Returns:
        Groups spanning at least two physical copies.
    """
    heads = _digests(head_digest, inodes, workers, skips)
    readable = [i for i in inodes if i.fileid in heads]
    by_head = multi_copy_groups(readable, lambda i: (i.size, heads[i.fileid]))
    survivors = [i for g in by_head.values() for i in g]
    small = {i.fileid: heads[i.fileid] for i in survivors if i.size <= HEAD_BYTES}
    full = small | _digests(full_digest, [i for i in survivors if i.size > HEAD_BYTES], workers, skips)
    hashed = [i for i in survivors if i.fileid in full]
    return multi_copy_groups(hashed, lambda i: full[i.fileid])


def _digests(digest, inodes: list[Inode], workers: int, skips: SkipTally) -> dict:
    """fileid -> digest of each readable inode's first name; unreadable ones are tallied."""
    result = {}
    for inode, value in zip(inodes, digest_many(digest, [i.names[0] for i in inodes], workers)):
        if value is None:
            skips.add("unreadable", inode)
        else:
            result[inode.fileid] = value
    return result
