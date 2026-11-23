"""CLI: dry-run planning by default; `--apply --plan FILE` executes a previously written plan."""

from __future__ import annotations

import argparse
import gzip
import json
import time

from .accounting import account
from .apply import ApplyLimits, run_apply
from .inventory import canonical_roots
from .plan_io import write_plan
from .planner import build_plan
from .report import format_summary

DEFAULT_MIN_SIZE = 64 * 1024
DEFAULT_MIN_AGE_HOURS = 48.0
DEFAULT_WORKERS = 4
MAX_WORKERS = 4


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m diskburst.clone_dedupe",
        description="Plan (default, read-only) or apply replacing byte-identical files with APFS clones.",
    )
    p.add_argument("roots", nargs="*", help="directories to deduplicate (dry run)")
    p.add_argument("--plan-out", help="dry run: gzip JSONL plan to write (summaries go beside it)")
    p.add_argument("--min-size", type=int, default=DEFAULT_MIN_SIZE, help="smallest file size in bytes")
    p.add_argument("--min-age-hours", type=float, default=DEFAULT_MIN_AGE_HOURS,
                   help="skip files modified more recently than this")
    p.add_argument("--workers", type=int, default=DEFAULT_WORKERS, help=f"hash processes (max {MAX_WORKERS})")
    p.add_argument("--apply", action="store_true", help="execute --plan instead of planning")
    p.add_argument("--plan", help="apply: plan file written by a dry run")
    p.add_argument("--log", help="apply: append-only JSONL log (default: <plan>.apply.jsonl)")
    p.add_argument("--limit-files", type=int, help="apply: stop after this many replacements")
    p.add_argument("--limit-bytes", type=int, help="apply: stop before exceeding this many logical bytes")
    p.add_argument("--min-free-bytes", type=int, default=ApplyLimits.min_free_bytes,
                   help="apply: stop when the volume's free space drops below this")
    p.add_argument("--max-failures", type=int, default=ApplyLimits.max_failures,
                   help="apply: stop once more than this many replacements fail")
    p.add_argument("--max-plan-age-hours", type=float, default=ApplyLimits.max_plan_age_hours)
    p.add_argument("--verify", choices=("full", "sampled"), default="full",
                   help="apply: post-rename content check")
    return p


def output_stem(plan_out: str) -> str:
    """Strip the plan extensions so summaries sit beside the plan."""
    for suffix in (".jsonl.gz", ".gz", ".jsonl"):
        if plan_out.endswith(suffix):
            return plan_out[: -len(suffix)]
    return plan_out


def dry_run(args: argparse.Namespace) -> None:
    """Plan, write the plan, a text and JSON summary, and the outside clone families."""
    roots, now_ns = canonical_roots(args.roots), time.time_ns()
    plan = build_plan(roots, args.min_size, args.min_age_hours, min(args.workers, MAX_WORKERS), now_ns)
    header = {"created_at_ns": now_ns, "roots": roots, "min_size": args.min_size,
              "min_age_hours": args.min_age_hours}
    write_plan(args.plan_out, header, plan.records)
    totals = account(plan)
    stem = output_stem(args.plan_out)
    with open(f"{stem}.summary.txt", "w", encoding="utf-8") as f:
        f.write(format_summary(plan, totals) + "\n")
    outside = totals.pop("outside_clone_families")
    with open(f"{stem}.summary.json", "w", encoding="utf-8") as f:
        json.dump({**header, "groups": plan.groups, "skips": plan.skips.reasons,
                   "xattrs": plan.skips.xattrs, **totals}, f, indent=1)
    with gzip.open(f"{stem}.outside_clone_families.tsv.gz", "wt", encoding="utf-8") as f:
        f.write("cloneid\theld_bytes\tfirst_path_inside\n")
        f.writelines(f"{c}\t{b}\t{p}\n" for c, (b, p) in sorted(outside.items(), key=lambda kv: -kv[1][0]))
    print(format_summary(plan, totals))


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    if args.apply:
        if not args.plan or args.roots:
            raise SystemExit("--apply needs --plan FILE and takes no roots (they come from the plan)")
        limits = ApplyLimits(args.limit_files, args.limit_bytes, args.min_free_bytes,
                             args.max_failures, args.max_plan_age_hours, args.verify)
        print(json.dumps(run_apply(args.plan, args.log or f"{args.plan}.apply.jsonl", limits)))
        return
    if not args.roots or not args.plan_out:
        raise SystemExit("dry run needs at least one root and --plan-out FILE")
    dry_run(args)


if __name__ == "__main__":
    main()
