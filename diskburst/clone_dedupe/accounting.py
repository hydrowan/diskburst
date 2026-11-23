"""Space accounting for a plan: logical duplicates vs bytes really freed vs bytes held elsewhere."""

from __future__ import annotations

import os
from collections import Counter, defaultdict

from .planner import Plan, RedundantCopy

BUCKET_FIELDS = (
    "physical_copies", "replacements", "logical_bytes", "freed_bytes",
    "held_by_clones_outside_roots", "held_by_hardlinks_outside_roots", "held_by_unplanned_clones_inside",
)


def new_bucket() -> dict[str, int]:
    """Zeroed counters for one row of the summary."""
    return dict.fromkeys(BUCKET_FIELDS, 0)


def locate(path: str, roots: list[str]) -> tuple[str, str, str]:
    """Return (root, root/<top-level folder>, lower-case extension) for a planned path."""
    root = next(r for r in roots if path.startswith(r + os.sep))
    top = path[len(root) + 1:].split(os.sep, 1)[0]
    ext = os.path.splitext(path)[1].lower() or "(none)"
    return root, os.path.join(root, top), ext


def copy_bytes(copy: RedundantCopy, plan: Plan, planned_inodes: set[int], seen: set[int]) -> Counter:
    """Bytes freed or held for one redundant physical copy; family blocks count once via `seen`."""
    out = Counter(physical_copies=1, logical_bytes=copy.size)
    for inode, planned in copy.inodes:
        if planned:
            out["replacements"] += len(inode.names)
            out["freed_bytes"] += inode.private
        else:
            out["held_by_hardlinks_outside_roots"] += inode.private
    if copy.refcnt > 1 and copy.cloneid not in seen:
        seen.add(copy.cloneid)
        family = plan.families[copy.cloneid]
        if not family.complete:
            out["held_by_clones_outside_roots"] += family.shared_bytes
        elif family.fileids <= planned_inodes:
            out["freed_bytes"] += family.shared_bytes
        else:
            out["held_by_unplanned_clones_inside"] += family.shared_bytes
    return out


def account(plan: Plan) -> dict:
    """Aggregate copy_bytes per root, per top-level folder, per extension and in total.

    Args:
        plan: a finished dry-run plan.

    Returns:
        {"total", "roots", "folders", "extensions"} buckets plus outside clone families.
    """
    planned_inodes = {r["inode"] for r in plan.records}
    seen: set[int] = set()
    rows = {k: defaultdict(new_bucket) for k in ("roots", "folders", "extensions")}
    total, outside = new_bucket(), {}
    for copy in plan.redundant:
        amounts = copy_bytes(copy, plan, planned_inodes, seen)
        for kind, key in zip(rows, locate(copy.first_path, plan.roots)):
            _add(rows[kind][key], amounts)
        _add(total, amounts)
        if amounts["held_by_clones_outside_roots"]:
            outside[copy.cloneid] = (amounts["held_by_clones_outside_roots"], copy.first_path)
    return {"total": total, **{k: dict(v) for k, v in rows.items()}, "outside_clone_families": outside}


def _add(bucket: dict[str, int], amounts: Counter) -> None:
    for name, value in amounts.items():
        bucket[name] += value
