"""Execute a dry-run plan: resumable, limited, logged, with free-space and failure stops."""

from __future__ import annotations

import json
import os
import time
from collections import Counter
from dataclasses import dataclass

from .plan_io import read_plan
from .replace import Skip, VerifyFailure, replace_with_clone


@dataclass(frozen=True)
class ApplyLimits:
    """Stop conditions for one apply run."""

    max_files: int | None = None
    max_bytes: int | None = None
    min_free_bytes: int = 500 * 1000 * 1000
    max_failures: int = 3
    max_plan_age_hours: float = 24.0
    verify: str = "full"


def completed_paths(log_path: str) -> set[str]:
    """Paths already replaced according to an existing apply log."""
    if not os.path.exists(log_path):
        return set()
    with open(log_path, encoding="utf-8") as f:
        return {e["path"] for line in f if (e := json.loads(line)).get("status") == "replaced"}


def inside_roots(path: str, roots: list[str]) -> bool:
    """True if `path`'s directory is a real (symlink-free) directory under one of `roots`."""
    directory = os.path.dirname(path)
    if os.path.realpath(directory) != directory:
        return False
    return any(directory == r or directory.startswith(r + os.sep) for r in roots)


def free_bytes(path: str) -> int:
    """Bytes available to unprivileged users on the volume holding `path`."""
    vfs = os.statvfs(path)
    return vfs.f_bavail * vfs.f_frsize


def stop_reason(record: dict, counts: Counter, limits: ApplyLimits) -> str | None:
    """Name the limit that stops the run before `record`, if any."""
    if limits.max_files is not None and counts["replaced"] >= limits.max_files:
        return "limit_files"
    if limits.max_bytes is not None and counts["replaced_bytes"] + record["size"] > limits.max_bytes:
        return "limit_bytes"
    if free_bytes(os.path.dirname(record["path"])) < limits.min_free_bytes:
        return "free_space_below_floor"
    if counts["failed"] > limits.max_failures:
        return "too_many_failures"
    return None


def apply_record(record: dict, roots: list[str], verify: str) -> tuple[str, str]:
    """Attempt one replacement; return (status, reason) with status replaced/skipped/failed."""
    if not (inside_roots(record["path"], roots) and inside_roots(record["keeper"], roots)):
        return "skipped", "outside_roots"
    try:
        replace_with_clone(record, verify)
    except Skip as e:
        return "skipped", str(e)
    except VerifyFailure as e:
        return "failed", f"verify:{e}"
    except OSError as e:
        return "failed", f"oserror:{e.errno}:{e.strerror}"
    return "replaced", ""


def run_apply(plan_path: str, log_path: str, limits: ApplyLimits) -> Counter:
    """Apply every not-yet-completed record of a plan, appending one JSON line per outcome.

    Args:
        plan_path: plan written by the dry run.
        log_path: append-only JSONL log; also the resume source.
        limits: stop conditions and verification mode.

    Returns:
        Counts of replaced/skipped/failed records and replaced bytes.
    """
    header, records = read_plan(plan_path, limits.max_plan_age_hours)
    done, counts = completed_paths(log_path), Counter()
    with open(log_path, "a", encoding="utf-8", buffering=1) as log:
        for record in records:
            if record["path"] in done:
                counts["already_done"] += 1
                continue
            if stop := stop_reason(record, counts, limits):
                log.write(json.dumps({"t_ns": time.time_ns(), "status": "stopped", "reason": stop}) + "\n")
                counts[f"stopped:{stop}"] += 1
                break
            status, reason = apply_record(record, header["roots"], limits.verify)
            counts[status] += 1
            counts["replaced_bytes"] += record["size"] if status == "replaced" else 0
            log.write(json.dumps({
                "t_ns": time.time_ns(), "path": record["path"], "keeper": record["keeper"],
                "status": status, "reason": reason, "size": record["size"],
            }, ensure_ascii=False) + "\n")
    return counts
