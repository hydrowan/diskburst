"""Filename filters over the scanned tree: extension sets, substring, fuzzy, regex.

A TreeFilter matches retained files (>= min-file-mb, default 1 MB) by name and
precomputes per-directory filtered byte/file totals for the whole tree, so the
sunburst can render the same map with sizes restricted to matching files.
Files below the retention threshold are invisible to filters (they still count
in the unfiltered view).
"""

from __future__ import annotations

import re

from .scanner import DirNode, Scanner


class FilterError(ValueError):
    """Bad filter spec (e.g. invalid regex) — reported to the client as a 400."""


def build_matcher(mode: str, query: str, exts: list[str]):
    """Return name -> bool. Extension set and text query compose with AND."""
    query = query.strip()
    ext_tuple = tuple("." + e.strip(". ").lower() for e in exts if e.strip(". "))
    text_ok = None
    if query:
        if mode == "regex":
            try:
                rx = re.compile(query, re.IGNORECASE)
            except re.error as e:
                raise FilterError(f"invalid regex: {e}") from None
            text_ok = lambda low: rx.search(low) is not None  # noqa: E731
        elif mode == "fuzzy":
            needle = query.lower()

            def text_ok(low: str, needle=needle) -> bool:  # fzf-style subsequence
                i = 0
                for ch in low:
                    if ch == needle[i]:
                        i += 1
                        if i == len(needle):
                            return True
                return False
        else:  # plain substring
            needle = query.lower()
            text_ok = lambda low: needle in low  # noqa: E731

    def match(name: str) -> bool:
        low = name.lower()
        if ext_tuple and not low.endswith(ext_tuple):
            return False
        return text_ok is None or text_ok(low)

    return match


class TreeFilter:
    """Matcher plus per-node filtered totals, computed once per (scan, spec)."""

    def __init__(self, scanner: Scanner, mode: str, query: str, exts: list[str]):
        self.match = build_matcher(mode, query, exts)
        self._totals: dict[int, tuple[int, int]] = {}
        for root in scanner.roots.values():
            self._compute(root)

    def _compute(self, root: DirNode) -> None:
        stack: list[tuple[DirNode, bool]] = [(root, False)]
        while stack:
            node, visited = stack.pop()
            if visited:
                total = 0
                count = 0
                for name, size, _ in list(node.big_files):
                    if self.match(name):
                        total += size
                        count += 1
                for child in list(node.children.values()):
                    child_total, child_count = self._totals.get(id(child), (0, 0))
                    total += child_total
                    count += child_count
                self._totals[id(node)] = (total, count)
            else:
                stack.append((node, True))
                for child in list(node.children.values()):
                    stack.append((child, False))

    def size(self, node: DirNode) -> int:
        return self._totals.get(id(node), (0, 0))[0]

    def count(self, node: DirNode) -> int:
        return self._totals.get(id(node), (0, 0))[1]

    def files(self, node: DirNode) -> list[tuple[str, int, float]]:
        return [(n, s, m) for n, s, m in list(node.big_files) if self.match(n)]
