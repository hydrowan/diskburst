"""macOS extended attributes via ctypes (Python's os.*xattr functions are Linux-only)."""

from __future__ import annotations

import ctypes
import ctypes.util
import os

XATTR_NOFOLLOW = 0x0001

_libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
_libc.listxattr.restype = ctypes.c_ssize_t
_libc.listxattr.argtypes = [ctypes.c_char_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int]
_libc.getxattr.restype = ctypes.c_ssize_t
_libc.getxattr.argtypes = [
    ctypes.c_char_p, ctypes.c_char_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_uint32, ctypes.c_int
]
_libc.setxattr.argtypes = [
    ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p, ctypes.c_size_t, ctypes.c_uint32, ctypes.c_int
]
_libc.removexattr.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_int]


def _check(result: int, path: str) -> int:
    if result < 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err), path)
    return result


def list_names(path: str) -> list[str]:
    """Names of the extended attributes of `path` (symlinks not followed)."""
    raw = os.fsencode(path)
    size = _check(_libc.listxattr(raw, None, 0, XATTR_NOFOLLOW), path)
    if size == 0:
        return []
    buf = ctypes.create_string_buffer(size)
    size = _check(_libc.listxattr(raw, buf, size, XATTR_NOFOLLOW), path)
    return [n.decode("utf-8", "surrogateescape") for n in buf.raw[:size].split(b"\0") if n]


def get(path: str, name: str) -> bytes:
    """Value of extended attribute `name` on `path`."""
    raw, key = os.fsencode(path), name.encode("utf-8", "surrogateescape")
    size = _check(_libc.getxattr(raw, key, None, 0, 0, XATTR_NOFOLLOW), path)
    buf = ctypes.create_string_buffer(max(size, 1))
    size = _check(_libc.getxattr(raw, key, buf, size, 0, XATTR_NOFOLLOW), path)
    return buf.raw[:size]


def put(path: str, name: str, value: bytes) -> None:
    """Create or replace extended attribute `name` on `path`."""
    key = name.encode("utf-8", "surrogateescape")
    _check(_libc.setxattr(os.fsencode(path), key, value, len(value), 0, XATTR_NOFOLLOW), path)


def remove(path: str, name: str) -> None:
    """Delete extended attribute `name` from `path`."""
    _check(_libc.removexattr(os.fsencode(path), name.encode("utf-8", "surrogateescape"), XATTR_NOFOLLOW), path)
