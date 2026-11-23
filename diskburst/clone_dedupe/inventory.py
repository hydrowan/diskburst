"""Walk the roots once into per-inode records and per-clone-family block statistics."""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from .listing import FileEntry, WalkStats, walk_files


@dataclass(slots=True)
class Inode:
    """One inode with every name seen under the roots; `mode` is filled in by lstat later."""

    fileid: int
    nlink: int
    size: int
    alloc: int
    private: int
    cloneid: int
    refcnt: int
    mtime_ns: int
    flags: int
    names: list[str] = field(default_factory=list)
    mode: int = 0

    @property
    def unit_key(self) -> tuple:
        """Physical-copy key: the clone family when shared by clones, else the inode."""
        return ("clone", self.cloneid) if self.refcnt > 1 else ("inode", self.fileid)

    @property
    def all_names_inside(self) -> bool:
        """True if every hard-link name of this inode was seen under the roots."""
        return len(self.names) >= self.nlink


@dataclass(slots=True)
class Family:
    """Distinct inodes seen under the roots for one clone id, with block statistics."""

    refcnt: int
    fileids: set[int] = field(default_factory=set)
    max_alloc: int = 0
    private_sum: int = 0

    @property
    def complete(self) -> bool:
        """True if every member of the family lives under the roots."""
        return len(self.fileids) >= self.refcnt

    @property
    def shared_bytes(self) -> int:
        """Approximate blocks shared between members (exact when no member was edited)."""
        return max(0, self.max_alloc - self.private_sum)


def canonical_roots(roots: list[str]) -> list[str]:
    """Resolve roots to real absolute directories on one volume, rejecting symlinks and nesting."""
    resolved = []
    for root in roots:
        path = os.path.abspath(os.path.expanduser(root))
        if os.path.islink(path) or not os.path.isdir(path):
            raise SystemExit(f"root must be a real directory, not a symlink: {path}")
        resolved.append(os.path.realpath(path))
    if len({os.lstat(r).st_dev for r in resolved}) > 1:
        raise SystemExit("all roots must be on one volume (clones cannot cross volumes)")
    for a in resolved:
        if any(a != b and a.startswith(b + os.sep) for b in resolved):
            raise SystemExit(f"root {a} is nested inside another root")
    return resolved


def build_inventory(roots: list[str], min_size: int) -> tuple[dict[int, Inode], dict[int, Family], WalkStats]:
    """Walk `roots` into inodes of at least `min_size` bytes and clone families of any size.

    Args:
        roots: canonical roots on one volume.
        min_size: smallest logical size kept as a dedupe candidate.

    Returns:
        (fileid -> Inode, cloneid -> Family, walk statistics).
    """
    inodes: dict[int, Inode] = {}
    families: dict[int, Family] = {}
    stats = WalkStats()
    for root in roots:
        for entry in walk_files(root, stats):
            if entry.refcnt > 1:
                _add_to_family(families, entry)
            if entry.size >= min_size:
                _add_name(inodes, entry)
    return inodes, families, stats


def _add_to_family(families: dict[int, Family], entry: FileEntry) -> None:
    family = families.get(entry.cloneid)
    if family is None:
        family = families[entry.cloneid] = Family(entry.refcnt)
    if entry.fileid not in family.fileids:
        family.fileids.add(entry.fileid)
        family.max_alloc = max(family.max_alloc, entry.alloc)
        family.private_sum += entry.private


def _add_name(inodes: dict[int, Inode], entry: FileEntry) -> None:
    inode = inodes.get(entry.fileid)
    if inode is None:
        inode = inodes[entry.fileid] = Inode(
            entry.fileid, entry.nlink, entry.size, entry.alloc, entry.private,
            entry.cloneid, entry.refcnt, entry.mtime_ns, entry.flags,
        )
    inode.names.append(entry.path)
