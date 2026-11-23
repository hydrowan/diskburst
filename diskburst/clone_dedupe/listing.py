"""Recursive getattrlistbulk walk yielding regular files with logical size, flags and clone data."""

from __future__ import annotations

import ctypes
import os
import struct
from collections.abc import Iterator
from typing import NamedTuple

from .. import bulk_listing as bl

ATTR_CMN_FLAGS = 0x00040000
ATTR_FILE_DATALENGTH = 0x00000200
COMMON_ATTRS = (
    bl.ATTR_CMN_RETURNED_ATTRS | bl.ATTR_CMN_NAME | bl.ATTR_CMN_DEVID | bl.ATTR_CMN_OBJTYPE
    | bl.ATTR_CMN_MODTIME | ATTR_CMN_FLAGS | bl.ATTR_CMN_FILEID
)
DIR_ATTRS = bl.ATTR_DIR_MOUNTSTATUS
FILE_ATTRS = bl.ATTR_FILE_LINKCOUNT | bl.ATTR_FILE_ALLOCSIZE | ATTR_FILE_DATALENGTH
ATTRLIST = struct.pack(
    "HHIIIII", bl.ATTR_BIT_MAP_COUNT, 0, COMMON_ATTRS, 0, DIR_ATTRS, FILE_ATTRS, bl.CLONE_ATTRS
)
# Packed in bit order: length, returned attrs, name ref, devid, objtype, modtime, flags, fileid.
RECORD_HEAD = struct.Struct("=I5IiIiIqqIQ")
NAME_REF_OFFSET = 24
FILE_TAIL = struct.Struct("=IqqqQI")  # linkcount, allocsize, datalength, private, cloneid, refcnt
DIR_TAIL = struct.Struct("=I")
VLNK = 5
NANOS = 1_000_000_000


class FileEntry(NamedTuple):
    """One regular-file name as listed; private == alloc and refcnt == 0 without clone data."""

    path: str
    dev: int
    fileid: int
    nlink: int
    size: int
    alloc: int
    private: int
    cloneid: int
    refcnt: int
    mtime_ns: int
    flags: int


class WalkStats:
    """Counts of directories listed, listing failures and skipped non-regular entries."""

    def __init__(self):
        self.dirs = self.failed_dirs = self.symlinks = self.other_volume = self.other_types = 0


def walk_files(root: str, stats: WalkStats) -> Iterator[FileEntry]:
    """Yield every regular file under `root` without following symlinks or crossing volumes.

    Args:
        root: directory to walk; must not be a symlink.
        stats: counters updated in place.

    Returns:
        Iterator of FileEntry, one per name (hard links yield each name).
    """
    root_dev = os.lstat(root).st_dev
    buf = ctypes.create_string_buffer(bl.LISTING_BUFFER_BYTES)
    stack = [root]
    while stack:
        directory = stack.pop()
        try:
            records = _list(directory, buf)
        except OSError:
            stats.failed_dirs += 1
            continue
        stats.dirs += 1
        for record in records:
            entry = _classify(directory, record, root_dev, stats)
            if isinstance(entry, str):
                stack.append(entry)
            elif entry is not None:
                yield entry


def _classify(directory: str, record: tuple, root_dev: int, stats: WalkStats):
    objtype, name, dev, is_mount, tail = record
    path = os.path.join(directory, name)
    if dev != root_dev or is_mount:
        stats.other_volume += 1
        return None
    if objtype == bl.VDIR:
        return path
    if objtype == VLNK:
        stats.symlinks += 1
        return None
    if objtype != bl.VREG or tail is None:
        stats.other_types += 1
        return None
    return FileEntry(path, dev, *tail)


def _list(path: str, buf: ctypes.Array) -> list[tuple]:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        view, records = memoryview(buf), []
        options = bl.FSOPT_ATTR_CMN_EXTENDED | bl.FSOPT_PACK_INVAL_ATTRS
        while (count := bl._getattrlistbulk(fd, ATTRLIST, buf, len(buf), options)) > 0:
            offset = 0
            for _ in range(count):
                records.append(_parse(view, offset))
                offset += RECORD_HEAD.unpack_from(view, offset)[0]
        if count < 0:
            err = ctypes.get_errno()
            raise OSError(err, os.strerror(err))
        return records
    finally:
        os.close(fd)


def _parse(view: memoryview, offset: int) -> tuple:
    """Return (objtype, name, dev, is_mount, file_tail | None) for one packed record."""
    (_, _, _, dir_ret, file_ret, clone_ret, name_off, name_len,
     dev, objtype, sec, nsec, flags, fileid) = RECORD_HEAD.unpack_from(view, offset)
    start = offset + NAME_REF_OFFSET + name_off
    name = bytes(view[start:start + name_len - 1]).decode("utf-8", "surrogateescape")
    tail = offset + RECORD_HEAD.size
    if objtype == bl.VDIR:
        status = DIR_TAIL.unpack_from(view, tail)[0] if dir_ret == DIR_ATTRS else 0
        return objtype, name, dev, bool(status & bl.DIR_MNTSTATUS_MNTPOINT), None
    if objtype != bl.VREG or file_ret != FILE_ATTRS:
        return objtype, name, dev, False, None
    nlink, alloc, size, private, cloneid, refcnt = FILE_TAIL.unpack_from(view, tail)
    if clone_ret != bl.CLONE_ATTRS:
        private, cloneid, refcnt = alloc, 0, 0
    return objtype, name, dev, False, (
        fileid, nlink, size, alloc, private, cloneid, refcnt, sec * NANOS + nsec, flags
    )
