"""ctypes binding for macOS clonefile(2)."""

from __future__ import annotations

import ctypes
import ctypes.util
import os

CLONE_NOFOLLOW = 0x0001

_libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
_clonefile = getattr(_libc, "clonefile", None)
AVAILABLE = _clonefile is not None
if AVAILABLE:
    _clonefile.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint32]


def clone_file(src: str, dst: str) -> None:
    """Create `dst` as an APFS clone of `src` without following symlinks.

    Args:
        src: existing regular file.
        dst: path that must not exist yet, on the same volume.

    Raises:
        OSError: clonefile failed (errno set by the kernel).
    """
    if not AVAILABLE:
        raise OSError(0, "clonefile is unavailable on this platform")
    if _clonefile(os.fsencode(src), os.fsencode(dst), CLONE_NOFOLLOW) != 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err), dst)
