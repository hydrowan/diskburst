"""Compare the current scan against a historical snapshot: what grew, what shrank.

The diff descends into changed directories to attribute change as deeply as
possible: a matched directory pair recurses; entries are emitted for added or
removed subtrees, changed retained files, and significant small-file churn
within a directory ("files in <dir>").
"""

from __future__ import annotations

import os

from .cache import _decode, history_snapshots, load_snapshot
from .scanner import DirNode, Scanner, compute_totals

MIN_DELTA_BYTES = 5 * 1000 * 1000
MAX_ENTRIES = 1000  # full-tree cap; the UI filters by zoom scope client-side of this list


def diff_against_snapshot(scanner: Scanner, which: str = "prev") -> dict:
    """Diff the served tree against a stored snapshot ('prev', 'oldest', or a stamp)."""
    snapshots = history_snapshots(scanner.root_paths, scanner.big_file_bytes)
    payload = load_snapshot(scanner.root_paths, scanner.big_file_bytes, which=which)
    if payload is None:
        return {"available": False, "snapshots": snapshots}
    entries: list[dict] = []
    for root_path, node in scanner.roots.items():
        encoded = payload["trees"].get(root_path)
        if encoded is None:
            continue
        old_root, _ = _decode(encoded)
        compute_totals(old_root)
        entries.extend(_diff_nodes(old_root, node, root_path))
    entries.sort(key=lambda e: abs(e["delta"]), reverse=True)
    return {
        "available": True,
        "baseline_saved_at": payload["saved_at"],
        "baseline_stamp": payload.get("_stamp"),
        "snapshots": snapshots,
        "entries": entries[:MAX_ENTRIES],
    }


def _diff_nodes(old_root: DirNode, new_root: DirNode, root_path: str) -> list[dict]:
    entries: list[dict] = []
    stack = [(old_root, new_root, root_path)]
    while stack:
        old, new, path = stack.pop()
        _diff_files(old, new, path, entries)
        churn = new.own_bytes - old.own_bytes
        retained_delta = sum(s for _, s, _ in new.big_files) - sum(
            s for _, s, _ in old.big_files
        )
        if abs(churn - retained_delta) >= MIN_DELTA_BYTES:  # change among small files
            entries.append(_entry(path, "dir-files", old.own_bytes, new.own_bytes))
        for name in set(old.children) | set(new.children):
            if name == ".Trash":
                continue  # moves to Trash already show as removals at the source
            o, n = old.children.get(name), new.children.get(name)
            child_path = os.path.join(path, name)
            ob = o.total_bytes if o else 0
            nb = n.total_bytes if n else 0
            if abs(nb - ob) < MIN_DELTA_BYTES:
                continue
            if o is not None and n is not None:
                stack.append((o, n, child_path))
            else:
                entries.append(_entry(child_path, "dir-added" if n else "dir-removed", ob, nb))
    return entries


def _diff_files(old: DirNode, new: DirNode, path: str, entries: list[dict]) -> None:
    old_sizes = {name: size for name, size, _ in old.big_files}
    new_sizes = {name: size for name, size, _ in new.big_files}
    for name in set(old_sizes) | set(new_sizes):
        before = old_sizes.get(name, 0)
        after = new_sizes.get(name, 0)
        if abs(after - before) < MIN_DELTA_BYTES:
            continue
        kind = "file-added" if not before else "file-removed" if not after else "file-changed"
        entries.append(_entry(os.path.join(path, name), kind, before, after))


def _entry(path: str, kind: str, before: int, after: int) -> dict:
    return {"path": path, "kind": kind, "before": before, "after": after, "delta": after - before}
