"""Per-file metadata the dedupe must preserve or refuse: flags, ACLs, extended attributes."""

from __future__ import annotations

import ctypes
import ctypes.util
import os

from . import xattr

ALLOWED_XATTRS = frozenset({"com.apple.provenance", "com.apple.quarantine"})
# The kernel stamps provenance itself; user processes cannot set or remove it.
OS_MANAGED_XATTRS = frozenset({"com.apple.provenance"})
ACL_TYPE_EXTENDED = 0x00000100
ACL_FIRST_ENTRY = 0

_libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
_libc.acl_get_link_np.restype = ctypes.c_void_p
_libc.acl_get_link_np.argtypes = [ctypes.c_char_p, ctypes.c_int]
_libc.acl_get_entry.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p)]
_libc.acl_free.argtypes = [ctypes.c_void_p]


def has_acl(path: str) -> bool:
    """Return True if `path` (not followed) carries at least one extended ACL entry."""
    acl = _libc.acl_get_link_np(os.fsencode(path), ACL_TYPE_EXTENDED)
    if not acl:
        return False
    try:
        entry = ctypes.c_void_p()
        return _libc.acl_get_entry(acl, ACL_FIRST_ENTRY, ctypes.byref(entry)) == 0
    finally:
        _libc.acl_free(acl)


def unusual_metadata(path: str, flags: int) -> tuple[list[str], list[str]]:
    """Reasons a file is unsafe to clone over or clone from, and its xattr names.

    Args:
        path: file to inspect (symlinks not followed).
        flags: BSD st_flags of the file.

    Returns:
        (reasons, xattr names); reasons is empty for a plain file.
    """
    names = xattr.list_names(path)
    reasons = [f"xattr:{n}" for n in names if n not in ALLOWED_XATTRS]
    if flags:
        reasons.append(f"flags:{flags:#x}")
    if has_acl(path):
        reasons.append("acl")
    return reasons, names


def read_xattrs(path: str) -> dict[str, bytes]:
    """Return every extended attribute of `path` (not followed) by name."""
    return {
        name: xattr.get(path, name)
        for name in xattr.list_names(path)
    }


def replace_xattrs(path: str, wanted: dict[str, bytes]) -> None:
    """Make the extended attributes of `path` equal `wanted`, except OS-managed ones."""
    for name in xattr.list_names(path):
        if name not in wanted and name not in OS_MANAGED_XATTRS:
            xattr.remove(path, name)
    for name, value in wanted.items():
        if name not in OS_MANAGED_XATTRS:
            xattr.put(path, name, value)


def restore_metadata(path: str, st: os.stat_result, xattrs: dict[str, bytes]) -> None:
    """Give `path` the ownership, mode, xattrs, times and flags recorded in `st`/`xattrs`."""
    current = os.lstat(path)
    if (current.st_uid, current.st_gid) != (st.st_uid, st.st_gid):
        os.chown(path, st.st_uid, st.st_gid, follow_symlinks=False)
    os.chmod(path, st.st_mode & 0o7777, follow_symlinks=False)
    replace_xattrs(path, xattrs)
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns), follow_symlinks=False)
    if st.st_flags != current.st_flags:
        os.chflags(path, st.st_flags, follow_symlinks=False)
