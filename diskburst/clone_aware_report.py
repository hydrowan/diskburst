"""One-shot APFS clone-aware usage report: apparent, unique and freed-if-deleted per directory."""

from __future__ import annotations

import argparse
import json
import os
import sys

from .scanner import DEFAULT_WORKERS, DirNode, Scanner

GIB = 1 << 30
DEFAULT_DEPTH = 1
DEFAULT_ROW_LIMIT = 50
PROGRESS_INTERVAL_S = 5.0
NO_RETAINED_FILES = sys.maxsize


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m diskburst.clone_aware_report",
        description="Scan once and print apparent, unique (each APFS clone group once) and "
        "freed-if-deleted sizes per directory, sorted by freed. Read-only; writes no cache.",
    )
    parser.add_argument("roots", nargs="+", help="directories to scan")
    parser.add_argument(
        "--depth", type=int, default=DEFAULT_DEPTH,
        help="report directories down to this many levels below each root (0 = roots only)",
    )
    parser.add_argument(
        "--limit", type=int, default=DEFAULT_ROW_LIMIT,
        help="print at most this many rows (0 = all)",
    )
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS, help="scan threads")
    parser.add_argument("--json", action="store_true", help="print JSON instead of a table")
    return parser


def run_scan(roots: list[str], workers: int) -> Scanner:
    """Run a clone-aware scan to completion, printing progress to an interactive stderr."""
    scanner = Scanner(
        roots, big_file_bytes=NO_RETAINED_FILES, workers=workers,
        clone_aware=True, refresh_totals=False,
    )
    scanner.start()
    while not scanner.done.wait(PROGRESS_INTERVAL_S):
        if sys.stderr.isatty():
            print(f"\r{scanner.stats.files:,} files scanned", end="", file=sys.stderr)
    if sys.stderr.isatty():
        print(file=sys.stderr)
    return scanner


def collect_rows(scanner: Scanner, depth: int) -> list[dict]:
    """Rows for every directory at most `depth` levels below a root, sorted by freed bytes."""
    rows = []
    stack: list[tuple[DirNode, str, int]] = [(n, p, 0) for p, n in scanner.roots.items()]
    while stack:
        node, path, level = stack.pop()
        rows.append({
            "path": path,
            "apparent_bytes": node.total_bytes,
            "unique_bytes": node.unique_bytes,
            "freed_if_deleted_bytes": node.freed_bytes,
            "files": node.total_files,
        })
        if level < depth:
            stack.extend((c, os.path.join(path, name), level + 1) for name, c in node.children.items())
    rows.sort(key=lambda r: r["freed_if_deleted_bytes"], reverse=True)
    return rows


def scan_summary(scanner: Scanner) -> dict:
    """Files, directories, errors, seconds and files/s of a finished scan."""
    seconds = scanner.finished_in or 0.0
    return {
        "files": scanner.stats.files,
        "dirs": scanner.stats.dirs,
        "errors": scanner.stats.errors,
        "listing_failures": scanner.stats.listing_failures,
        "seconds": round(seconds, 2),
        "files_per_second": round(scanner.stats.files / seconds) if seconds else 0,
        "clone_aware": scanner.clone_ledger is not None,
    }


def format_table(rows: list[dict], summary: dict) -> str:
    """Render rows as a GiB table with a scan summary footer."""
    lines = [f"{'apparent':>10} {'unique':>10} {'freed':>10} {'files':>11}  path   (GiB)"]
    for r in rows:
        lines.append(
            f"{r['apparent_bytes'] / GIB:10.2f} {r['unique_bytes'] / GIB:10.2f} "
            f"{r['freed_if_deleted_bytes'] / GIB:10.2f} {r['files']:11,d}  {r['path']}"
        )
    lines.append(
        f"freed = space released if that directory alone were deleted. Scanned "
        f"{summary['files']:,} files in {summary['seconds']:.1f} s "
        f"({summary['files_per_second']:,} files/s), {summary['errors']} errors, "
        f"{summary['listing_failures']} bulk listings fell back to os.scandir"
        + ("" if summary["clone_aware"] else "; no clone data, unique = freed = apparent")
    )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    scanner = run_scan(args.roots, args.workers)
    rows = collect_rows(scanner, args.depth)
    if args.limit:
        rows = rows[: args.limit]
    summary = scan_summary(scanner)
    if args.json:
        print(json.dumps({"roots": scanner.root_paths, "depth": args.depth, **summary, "rows": rows}))
    else:
        print(format_table(rows, summary))


if __name__ == "__main__":
    main()
