"""Tree → JSON-friendly dicts with size-proportional pruning for the UI.

With a TreeFilter, the same map is emitted with sizes restricted to matching
files: directories with no matches are dropped, `size`/`files` become the
filtered totals, and only matching retained files appear.
"""

from __future__ import annotations

import os

from .filter import TreeFilter
from .scanner import DirNode, Scanner

MAX_CHILDREN_PER_DIR = 50
MIN_FRACTION_OF_VIEW = 0.002  # children below 0.2% of the viewed total collapse into "rest"


def serialize_view(
    scanner: Scanner, path: str, depth: int = 4, flt: TreeFilter | None = None
) -> dict:
    """Serialize the subtree at `path` ('' = synthetic root over all scan roots)."""
    if path:
        found = scanner.find(path)
        if found is None:
            return {"error": "path not found", "path": path}
        node, node_path = found
        total = _size(node, flt)
        min_bytes = max(1, int(total * MIN_FRACTION_OF_VIEW))
        return _node_dict(node, node_path, depth, min_bytes, flt)
    total = sum(_size(r, flt) for r in scanner.roots.values())
    min_bytes = max(1, int(total * MIN_FRACTION_OF_VIEW))
    children = [
        _node_dict(node, root_path, depth - 1, min_bytes, flt)
        for root_path, node in scanner.roots.items()
        if flt is None or flt.size(node) > 0
    ]
    children.sort(key=lambda c: c["size"], reverse=True)
    return {
        "name": "All scanned",
        "path": "",
        "type": "dir",
        "size": total,
        "files": sum(_count(r, flt) for r in scanner.roots.values()),
        "children": children,
    }


def _size(node: DirNode, flt: TreeFilter | None) -> int:
    return flt.size(node) if flt else node.total_bytes


def _count(node: DirNode, flt: TreeFilter | None) -> int:
    return flt.count(node) if flt else node.total_files


def _node_dict(
    node: DirNode, path: str, depth: int, min_bytes: int, flt: TreeFilter | None
) -> dict:
    out = {
        "name": node.name,
        "path": path,
        "type": "dir",
        "size": _size(node, flt),
        "files": _count(node, flt),
        "newest": node.newest,
    }
    if node.error:
        out["error"] = True
    if depth <= 0:
        return out

    dirs = [
        (_size(child, flt), name, child)
        for name, child in list(node.children.items())
        if flt is None or flt.size(child) > 0
    ]
    retained = flt.files(node) if flt else list(node.big_files)
    files = [(size, name, mtime) for name, size, mtime in retained]
    items: list[dict] = []
    shown = 0
    for size, name, child in sorted(dirs, reverse=True, key=lambda t: t[0]):
        if size < min_bytes or len(items) >= MAX_CHILDREN_PER_DIR:
            break
        items.append(_node_dict(child, os.path.join(path, name), depth - 1, min_bytes, flt))
        shown += size
    for size, name, mtime in sorted(files, reverse=True, key=lambda t: t[0]):
        if size < min_bytes or len(items) >= MAX_CHILDREN_PER_DIR:
            break
        items.append(
            {
                "name": name,
                "path": os.path.join(path, name),
                "type": "file",
                "size": size,
                "mtime": mtime,
            }
        )
        shown += size
    rest = out["size"] - shown
    if items and rest > min_bytes:
        items.append({"name": "smaller items", "path": "", "type": "rest", "size": rest})
    if items:
        items.sort(key=lambda c: c["size"], reverse=True)
        out["children"] = items
    return out
