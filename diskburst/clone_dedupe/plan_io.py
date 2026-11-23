"""Gzip JSONL plan files: one header line, then one record per planned replacement."""

from __future__ import annotations

import gzip
import json
import os
import time
from collections.abc import Iterator

PLAN_FORMAT = "diskburst-clone-dedupe-plan/1"
NANOS_PER_HOUR = 3600 * 1_000_000_000


def write_plan(path: str, header: dict, records: list[dict]) -> None:
    """Write `header` and `records` to a gzip JSONL file via a same-directory temp + rename."""
    tmp = f"{path}.partial"
    with gzip.open(tmp, "wt", encoding="utf-8") as f:
        f.write(json.dumps({"format": PLAN_FORMAT, **header}) + "\n")
        for record in records:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    os.replace(tmp, path)


def read_plan(path: str, max_age_hours: float, now_ns: int | None = None) -> tuple[dict, Iterator[dict]]:
    """Open a plan, refusing unknown formats and plans older than `max_age_hours`.

    Args:
        path: plan written by write_plan.
        max_age_hours: oldest acceptable plan.
        now_ns: current time (defaults to now).

    Returns:
        (header, iterator over records).
    """
    f = gzip.open(path, "rt", encoding="utf-8")
    header = json.loads(f.readline())
    if header.get("format") != PLAN_FORMAT:
        f.close()
        raise SystemExit(f"not a clone-dedupe plan: {path}")
    age_hours = ((now_ns or time.time_ns()) - header["created_at_ns"]) / NANOS_PER_HOUR
    if age_hours > max_age_hours:
        f.close()
        raise SystemExit(f"plan is {age_hours:.1f} h old (limit {max_age_hours} h); re-run the dry run")
    return header, _records(f)


def _records(f) -> Iterator[dict]:
    with f:
        for line in f:
            yield json.loads(line)
