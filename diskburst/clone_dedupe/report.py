"""Human-readable dry-run summary built from the accounting buckets."""

from __future__ import annotations

from .accounting import BUCKET_FIELDS
from .planner import Plan

GIB = 1 << 30
TOP_FOLDERS = 30
CAVEAT = (
    "Freed bytes use each file's APFS private size; a clone family's shared blocks count as freed "
    "only when every member is under the roots and every member is planned. Shared blocks are "
    "estimated as max(alloc) - sum(private) over the members seen, which is exact for unedited "
    "clones and UNDER-states the family's blocks when members were edited (their union exceeds "
    "the largest member), so 'freed' and 'held' are both lower bounds in that case. Snapshots "
    "or other clones created after planning would keep blocks alive and lower the real gain."
)


def _row(label: str, bucket: dict[str, int]) -> str:
    gib = [f"{bucket[k] / GIB:9.2f}" for k in BUCKET_FIELDS[2:]]
    return f"{bucket['physical_copies']:>9,} {bucket['replacements']:>9,} {' '.join(gib)}  {label}"


def _table(title: str, rows: dict[str, dict], limit: int | None = None) -> list[str]:
    header = (f"{'copies':>9} {'replace':>9} {'logical':>9} {'freed':>9} {'out-clone':>9} "
              f"{'out-link':>9} {'in-unplan':>9}  (GiB)")
    ordered = sorted(rows.items(), key=lambda kv: (-kv[1]["freed_bytes"], -kv[1]["logical_bytes"]))
    return ["", f"## {title}", header, *(_row(k, v) for k, v in ordered[:limit])]


def format_summary(plan: Plan, totals: dict) -> str:
    """Render totals, per-root, per-folder, per-extension and skip tables as text."""
    t = totals["total"]
    lines = [
        f"Content groups with >1 physical copy: {plan.groups:,}; redundant physical copies: "
        f"{t['physical_copies']:,}; planned replacements (paths): {t['replacements']:,}",
        f"(a) logical bytes of redundant copies: {t['logical_bytes'] / GIB:.2f} GiB",
        f"(b) really freed (private sizes + fully planned clone families): {t['freed_bytes'] / GIB:.2f} GiB",
        f"(c) NOT freed, held by clone family members outside the roots: "
        f"{t['held_by_clones_outside_roots'] / GIB:.2f} GiB",
        f"(d) NOT freed, hard links with names outside the roots (skipped): "
        f"{t['held_by_hardlinks_outside_roots'] / GIB:.2f} GiB",
        f"(e) NOT freed, clone family members inside the roots that are not planned: "
        f"{t['held_by_unplanned_clones_inside'] / GIB:.2f} GiB",
        "", CAVEAT,
    ]
    lines += _table("Per root", totals["roots"])
    lines += _table(f"Per top-level folder (top {TOP_FOLDERS} by freed)", totals["folders"], TOP_FOLDERS)
    lines += _table("Per extension", totals["extensions"])
    lines += ["", "## Skipped (names, logical GiB)"]
    lines += [f"{n:>9,} {b / GIB:9.2f}  {reason}" for reason, (n, b) in sorted(plan.skips.reasons.items())]
    lines += ["", "## Extended attributes seen on same-size candidates (names)"]
    lines += [f"{n:>9,}  {name}" for name, n in plan.skips.xattrs.most_common()]
    w = plan.walk
    lines += ["", f"Walk: {plan.files:,} names >= min size, {w.dirs:,} dirs, {w.failed_dirs} unlistable dirs, "
              f"{w.symlinks:,} symlinks not followed, {w.other_volume} other-volume entries, "
              f"{w.other_types} other entries"]
    return "\n".join(lines)
