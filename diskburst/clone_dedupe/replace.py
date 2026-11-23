"""Replace one planned path with a clone of its keeper, atomically and only after re-verification."""

from __future__ import annotations

import hashlib
import os
import stat

from .clonefile import clone_file
from .hashing import CHUNK_BYTES, full_digest, sampled_digest
from .metadata import read_xattrs, restore_metadata, unusual_metadata

TEMP_PREFIX = ".diskburst-clone-dedupe-"
TEMP_SUFFIX = ".tmp"


class Skip(Exception):
    """The planned replacement is no longer safe; nothing was changed."""


class VerifyFailure(Exception):
    """The path was replaced but the result does not match the plan."""


def temp_path_for(path: str) -> str:
    """Deterministic, recognisable temp name in the same directory as `path`."""
    digest = hashlib.sha256(os.fsencode(os.path.basename(path))).hexdigest()[:16]
    return os.path.join(os.path.dirname(path), f"{TEMP_PREFIX}{digest}{TEMP_SUFFIX}")


def _identity(st: os.stat_result) -> tuple:
    return st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns


def check_against_plan(record: dict) -> os.stat_result:
    """Return the target's lstat if both files still match the plan, else raise Skip."""
    try:
        target, keeper = os.lstat(record["path"]), os.lstat(record["keeper"])
    except FileNotFoundError as e:
        raise Skip("missing") from e
    if not (stat.S_ISREG(target.st_mode) and stat.S_ISREG(keeper.st_mode)):
        raise Skip("not_regular_file")
    if target.st_dev != keeper.st_dev or target.st_ino == keeper.st_ino:
        raise Skip("same_inode_or_other_volume")
    if (target.st_ino, target.st_size, target.st_mtime_ns) != (record["inode"], record["size"], record["mtime_ns"]):
        raise Skip("target_changed_since_plan")
    if target.st_nlink > record["nlink"]:
        raise Skip("target_gained_hard_links")
    if (keeper.st_ino, keeper.st_size, keeper.st_mtime_ns) != (
        record["keeper_inode"], record["size"], record["keeper_mtime_ns"]
    ):
        raise Skip("keeper_changed_since_plan")
    if unusual_metadata(record["path"], target.st_flags)[0] or unusual_metadata(record["keeper"], keeper.st_flags)[0]:
        raise Skip("unusual_metadata")
    return target


def files_equal(a: str, b: str) -> bool:
    """Byte-for-byte comparison of two files, streamed in chunks."""
    with open(a, "rb") as fa, open(b, "rb") as fb:
        while True:
            chunk_a, chunk_b = fa.read(CHUNK_BYTES), fb.read(CHUNK_BYTES)
            if chunk_a != chunk_b:
                return False
            if not chunk_a:
                return True


def _fsync(path: str) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def remove_stale_temp(tmp: str) -> None:
    """Delete a temp file left by an interrupted run (only regular files with our name)."""
    try:
        if stat.S_ISREG(os.lstat(tmp).st_mode):
            os.unlink(tmp)
    except FileNotFoundError:
        pass


def replace_with_clone(record: dict, verify: str) -> None:
    """Clone the keeper to a temp file, prove it equals the target, then rename it over the target.

    Args:
        record: one plan record.
        verify: "full" (SHA-256 vs plan) or "sampled" (windows vs keeper) post-rename check.

    Raises:
        Skip: precondition failed; the target is untouched.
        VerifyFailure: the replaced file does not match the plan.
    """
    path = record["path"]
    target = check_against_plan(record)
    xattrs = read_xattrs(path)
    tmp = temp_path_for(path)
    remove_stale_temp(tmp)
    clone_file(record["keeper"], tmp)
    try:
        if not files_equal(tmp, path):
            raise Skip("content_differs")
        restore_metadata(tmp, target, xattrs)
        _fsync(tmp)
        if _identity(os.lstat(path)) != _identity(target):
            raise Skip("target_changed_during_apply")
        os.rename(tmp, path)
    finally:
        remove_stale_temp(tmp)
    _fsync(os.path.dirname(path))
    verify_replaced(record, target, verify)


def verify_replaced(record: dict, before: os.stat_result, verify: str) -> None:
    """Raise VerifyFailure unless the new file has the planned size, mode, mtime and content."""
    st = os.lstat(record["path"])
    if not stat.S_ISREG(st.st_mode) or st.st_ino == before.st_ino:
        raise VerifyFailure("not_replaced")
    if (st.st_size, st.st_mode, st.st_mtime_ns) != (before.st_size, before.st_mode, before.st_mtime_ns):
        raise VerifyFailure("metadata_mismatch")
    if verify == "full":
        ok = full_digest(record["path"]) == record["sha256"]
    else:
        ok = sampled_digest(record["path"]) == sampled_digest(record["keeper"])
    if not ok:
        raise VerifyFailure("content_mismatch")
