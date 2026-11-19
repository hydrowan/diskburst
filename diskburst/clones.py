"""APFS clone and hard-link accounting: unique and freed-if-deleted sizes per directory.

A hard-linked inode is a group of names; a clone group is a group of inodes.
Freed: an inode sits at the lowest common ancestor (LCA) of its names once all
of them were scanned, and a clone group's full size is credited at the LCA of
its inodes once every reference was scanned; below that only private bytes
count. Unique: a group counts once in every ancestor of one of its names, via
+size per name and -size at the LCA of preorder-adjacent names.
"""

from __future__ import annotations

import threading
from itertools import pairwise
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .scanner import DirNode

VIRTUAL_ROOT = 0  # parent of every scan root; credits landing here are dropped


class TreeIndex:
    """Preorder numbering of scanned directories for LCA queries and size credits."""

    def __init__(self, roots: list[DirNode]):
        self.order, self.parents, self.depths, self.nodes = preorder_index(roots)

    def lca(self, names: list[DirNode]) -> int:
        """Return the preorder index of the LCA of the given directories."""
        indices = [self.order[id(n)] for n in names]
        return lowest_common_ancestor(min(indices), max(indices), self.parents, self.depths)

    def credit(self, index: int, attr: str, amount: int) -> None:
        """Add `amount` to `attr` of the directory at `index` (dropped at the virtual root)."""
        if index != VIRTUAL_ROOT:
            node = self.nodes[index]
            setattr(node, attr, getattr(node, attr) + amount)

    def add_unique(self, names: list[DirNode], amount: int) -> None:
        """Count `amount` once in every ancestor of at least one of `names`."""
        indices = sorted(self.order[id(n)] for n in names)
        for i in indices:
            self.nodes[i].own_unique += amount
        for a, b in pairwise(indices):
            lca = lowest_common_ancestor(a, b, self.parents, self.depths)
            self.credit(lca, "own_unique", -amount)


class CloneLedger:
    """Thread-safe record of clone members and hard-linked names seen during a scan."""

    def __init__(self):
        # cloneid -> [alloc, refcnt, private_sum, names, inode_count, unplaced_inodes]
        self._groups: dict[int, list] = {}
        # (dev, fileid) -> [nlink, alloc, private, cloneid, refcnt, names]
        self._inodes: dict[tuple[int, int], list] = {}
        self._lock = threading.Lock()

    def record(self, node: DirNode, clones: list[tuple], links: list[tuple]) -> None:
        """Add one directory's single-name clone members and hard-linked names.

        Args:
            node: directory holding the entries.
            clones: (cloneid, alloc, private, refcnt) per single-name clone member;
                its private bytes are already in node.own_freed.
            links: (dev, fileid, nlink, alloc, private, cloneid, refcnt) per hard-linked name.
        """
        with self._lock:
            for cloneid, alloc, private, refcnt in clones:
                group = self._group(cloneid, alloc, refcnt)
                group[2] += private
                group[3].append(node)
                group[4] += 1
            for dev, fileid, nlink, alloc, private, cloneid, refcnt in links:
                inode = self._inodes.get((dev, fileid))
                if inode is None:
                    self._inodes[(dev, fileid)] = [nlink, alloc, private, cloneid, refcnt, [node]]
                else:
                    inode[5].append(node)

    def _group(self, cloneid: int, alloc: int, refcnt: int) -> list:
        group = self._groups.get(cloneid)
        if group is None:
            group = self._groups[cloneid] = [alloc, refcnt, 0, [], 0, 0]
        group[0] = max(group[0], alloc)
        return group

    def apply(self, roots: list[DirNode]) -> None:
        """Add every inode's and clone group's unique and freed credits to the tree.

        Args:
            roots: scan roots whose trees hold every recorded node.
        """
        tree = TreeIndex(roots)
        for inode in self._inodes.values():
            self._place_inode(tree, *inode)
        for alloc, refcnt, private_sum, names, inode_count, unplaced in self._groups.values():
            tree.add_unique(names, alloc)
            if inode_count >= refcnt and not unplaced:
                tree.credit(tree.lca(names), "own_freed", alloc - private_sum)

    def _place_inode(self, tree: TreeIndex, nlink, alloc, private, cloneid, refcnt, names) -> None:
        position = tree.lca(names) if len(names) >= nlink else None
        if refcnt > 1:
            group = self._group(cloneid, alloc, refcnt)
            group[3].extend(names)
            group[4] += 1
            if position is None:
                group[5] += 1
                return
            group[2] += private
            tree.credit(position, "own_freed", private)
            return
        tree.add_unique(names, alloc)
        if position is not None:
            tree.credit(position, "own_freed", private if refcnt == 1 else alloc)


def preorder_index(roots: list[DirNode]) -> tuple[dict[int, int], list[int], list[int], list]:
    """Number every directory in preorder beneath a virtual root.

    Args:
        roots: scan roots.

    Returns:
        (id(node) -> index, parent index per index, depth per index, node per index).
    """
    order: dict[int, int] = {}
    parents, depths, nodes = [VIRTUAL_ROOT], [0], [None]
    stack = [(root, VIRTUAL_ROOT) for root in reversed(roots)]
    while stack:
        node, parent = stack.pop()
        index = len(nodes)
        order[id(node)] = index
        parents.append(parent)
        depths.append(depths[parent] + 1)
        nodes.append(node)
        stack.extend((child, index) for child in node.children.values())
    return order, parents, depths, nodes


def lowest_common_ancestor(a: int, b: int, parents: list[int], depths: list[int]) -> int:
    """Return the preorder index of the deepest common ancestor of indices a and b."""
    while depths[a] > depths[b]:
        a = parents[a]
    while depths[b] > depths[a]:
        b = parents[b]
    while a != b:
        a, b = parents[a], parents[b]
    return a
