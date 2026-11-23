"""Dry-run planning: choose one keeper per content group and plan clones over the other copies."""

from __future__ import annotations

import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field

from .candidates import SkipTally, content_groups, multi_copy_groups, screen
from .inventory import Family, Inode, build_inventory
from .listing import WalkStats


@dataclass(slots=True)
class RedundantCopy:
    """One physical copy planned away; `inodes` pairs each inode with whether all its names are planned."""

    sha256: str
    size: int
    first_path: str
    cloneid: int
    refcnt: int
    inodes: list[tuple[Inode, bool]]


@dataclass
class Plan:
    """Everything the dry run learned: replacements, redundant copies, skips and inventory stats."""

    roots: list[str]
    records: list[dict] = field(default_factory=list)
    redundant: list[RedundantCopy] = field(default_factory=list)
    skips: SkipTally = field(default_factory=SkipTally)
    families: dict[int, Family] = field(default_factory=dict)
    walk: WalkStats = field(default_factory=WalkStats)
    groups: int = 0
    files: int = 0


def keeper_rank(unit: list[Inode]) -> tuple:
    """Sort key: most references (hard links or clone family), then oldest mtime, then path."""
    refs = max(max(i.refcnt, i.nlink) for i in unit)
    return -refs, min(i.mtime_ns for i in unit), min(n for i in unit for n in i.names)


def plan_group(sha256: str, inodes: list[Inode], skips: SkipTally) -> tuple[list[dict], list[RedundantCopy]]:
    """Plan replacements for one content group.

    Args:
        sha256: content digest shared by `inodes`.
        inodes: byte-identical inodes spanning at least two physical copies.
        skips: hard links with names outside the roots are tallied here.

    Returns:
        (replacement records, redundant physical copies).
    """
    units: dict[tuple, list[Inode]] = defaultdict(list)
    for inode in inodes:
        units[inode.unit_key].append(inode)
    keeper_unit = min(units.values(), key=keeper_rank)
    keeper_path = min(n for i in keeper_unit for n in i.names)
    keeper = next(i for i in keeper_unit if keeper_path in i.names)
    records, redundant = [], []
    for unit in units.values():
        if unit is keeper_unit:
            continue
        pairs = []
        for inode in unit:
            if not inode.all_names_inside:
                skips.add("hardlink_names_outside_roots", inode)
            else:
                records.extend(_record(sha256, inode, name, keeper, keeper_path) for name in inode.names)
            pairs.append((inode, inode.all_names_inside))
        first = min(n for i in unit for n in i.names)
        redundant.append(RedundantCopy(sha256, unit[0].size, first, unit[0].cloneid, unit[0].refcnt, pairs))
    return records, redundant


def _record(sha256: str, inode: Inode, path: str, keeper: Inode, keeper_path: str) -> dict:
    return {
        "path": path, "keeper": keeper_path, "sha256": sha256, "size": inode.size,
        "private": inode.private, "inode": inode.fileid, "nlink": inode.nlink,
        "mtime_ns": inode.mtime_ns, "mode": inode.mode, "cloneid": inode.cloneid,
        "keeper_inode": keeper.fileid, "keeper_mtime_ns": keeper.mtime_ns,
    }


def build_plan(roots: list[str], min_size: int, min_age_hours: float, workers: int, now_ns: int) -> Plan:
    """Run the read-only pipeline: walk, size groups, screening, hashing, keeper choice.

    Args:
        roots: canonical roots on one volume.
        min_size: smallest logical size considered.
        min_age_hours: skip files modified more recently than this.
        workers: hashing processes.
        now_ns: planning time.

    Returns:
        The full Plan.
    """
    plan = Plan(roots)
    inodes, plan.families, plan.walk = build_inventory(roots, min_size)
    plan.files = sum(len(i.names) for i in inodes.values())
    _progress(f"listed {plan.files:,} names / {len(inodes):,} inodes >= min size, {plan.walk.dirs:,} dirs")
    same_size = [i for g in multi_copy_groups(list(inodes.values()), lambda i: i.size).values() for i in g]
    screened = screen(same_size, now_ns, min_age_hours, plan.skips)
    _progress(f"{len(same_size):,} same-size inodes, {len(screened):,} after screening; hashing")
    groups = content_groups(screened, workers, plan.skips)
    for sha256, members in sorted(groups.items()):
        records, redundant = plan_group(sha256, members, plan.skips)
        plan.records.extend(records)
        plan.redundant.extend(redundant)
    plan.records.sort(key=lambda r: r["path"])
    plan.groups = len(groups)
    _progress(f"{plan.groups:,} content groups, {len(plan.records):,} planned replacements")
    return plan


def _progress(message: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {message}", file=sys.stderr, flush=True)
