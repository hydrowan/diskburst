"""macOS getattrlistbulk directory listing with APFS clone attributes, via ctypes."""

from __future__ import annotations

import ctypes
import ctypes.util
import os
import struct
import threading

ATTR_BIT_MAP_COUNT = 5
ATTR_CMN_NAME = 0x00000001
ATTR_CMN_DEVID = 0x00000002
ATTR_CMN_OBJTYPE = 0x00000008
ATTR_CMN_MODTIME = 0x00000400
ATTR_CMN_FILEID = 0x02000000
ATTR_CMN_RETURNED_ATTRS = 0x80000000
ATTR_DIR_MOUNTSTATUS = 0x00000004
ATTR_DIR_ALLOCSIZE = 0x00000008
ATTR_FILE_LINKCOUNT = 0x00000001
ATTR_FILE_ALLOCSIZE = 0x00000004
ATTR_CMNEXT_PRIVATESIZE = 0x00000008
ATTR_CMNEXT_CLONEID = 0x00000100
ATTR_CMNEXT_CLONE_REFCNT = 0x00001000
FSOPT_PACK_INVAL_ATTRS = 0x00000008
FSOPT_ATTR_CMN_EXTENDED = 0x00000020
DIR_MNTSTATUS_MNTPOINT = 0x00000001
VREG, VDIR = 1, 2
LISTING_BUFFER_BYTES = 256 * 1024

COMMON_ATTRS = (
    ATTR_CMN_RETURNED_ATTRS | ATTR_CMN_NAME | ATTR_CMN_DEVID
    | ATTR_CMN_OBJTYPE | ATTR_CMN_MODTIME | ATTR_CMN_FILEID
)
DIR_ATTRS = ATTR_DIR_MOUNTSTATUS | ATTR_DIR_ALLOCSIZE
FILE_ATTRS = ATTR_FILE_LINKCOUNT | ATTR_FILE_ALLOCSIZE
CLONE_ATTRS = ATTR_CMNEXT_PRIVATESIZE | ATTR_CMNEXT_CLONEID | ATTR_CMNEXT_CLONE_REFCNT
ATTRLIST = struct.pack(
    "HHIIIII", ATTR_BIT_MAP_COUNT, 0, COMMON_ATTRS, 0, DIR_ATTRS, FILE_ATTRS, CLONE_ATTRS
)

# Record layout (packed, 4-byte aligned, attributes in bit order within each group):
# u32 length | attribute_set_t returned (5 x u32) | attrreference name (i32 offset
# from itself, u32 length incl. NUL) | i32 devid | u32 objtype | timespec modtime
# (2 x i64) | u64 fileid, then the per-type tail. Dirs carry no file attrs and
# files carry no dir attrs, so the tail differs by objtype.
RECORD_HEAD = struct.Struct("=I5IiIiIqqQ")
NAME_REF_OFFSET = 24
FILE_TAIL = struct.Struct("=IqqQI")  # linkcount, allocsize, privatesize, cloneid, clone_refcnt
DIR_TAIL = struct.Struct("=Iq")  # mountstatus, allocsize
NANOS = 1e9

_libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
_getattrlistbulk = getattr(_libc, "getattrlistbulk", None)
AVAILABLE = _getattrlistbulk is not None
if AVAILABLE:
    _getattrlistbulk.argtypes = [
        ctypes.c_int, ctypes.c_char_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_uint64
    ]
_buffers = threading.local()


def _buffer() -> ctypes.Array:
    buf = getattr(_buffers, "buf", None)
    if buf is None:
        buf = _buffers.buf = ctypes.create_string_buffer(LISTING_BUFFER_BYTES)
    return buf


def list_directory(path: str) -> list[tuple]:
    """List one directory's entries with sizes and clone attributes in bulk.

    Args:
        path: directory to list (symlinks are not followed).

    Returns:
        Tuples (objtype, name, dev, mtime, fileid, nlink, alloc, private, cloneid,
        refcnt, is_mountpoint). `refcnt` is 0 when the volume reports no clone data.

    Raises:
        OSError: the directory cannot be opened or listed.
    """
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        return _read_entries(fd)
    finally:
        os.close(fd)


def _read_entries(fd: int) -> list[tuple]:
    buf = _buffer()
    view = memoryview(buf)
    options = FSOPT_ATTR_CMN_EXTENDED | FSOPT_PACK_INVAL_ATTRS
    entries: list[tuple] = []
    while True:
        count = _getattrlistbulk(fd, ATTRLIST, buf, LISTING_BUFFER_BYTES, options)
        if count < 0:
            err = ctypes.get_errno()
            raise OSError(err, os.strerror(err))
        if count == 0:
            return entries
        offset = 0
        for _ in range(count):
            entries.append(_parse_record(view, offset))
            offset += RECORD_HEAD.unpack_from(view, offset)[0]


def _parse_record(view: memoryview, offset: int) -> tuple:
    (_, _, _, dir_ret, file_ret, clone_ret, name_off, name_len,
     dev, objtype, sec, nsec, fileid) = RECORD_HEAD.unpack_from(view, offset)
    name_start = offset + NAME_REF_OFFSET + name_off
    name = bytes(view[name_start:name_start + name_len - 1]).decode("utf-8", "surrogateescape")
    mtime = sec + nsec / NANOS
    tail = offset + RECORD_HEAD.size
    if objtype == VDIR:
        mount_status, alloc = DIR_TAIL.unpack_from(view, tail) if dir_ret == DIR_ATTRS else (0, 0)
        is_mount = bool(mount_status & DIR_MNTSTATUS_MNTPOINT)
        return objtype, name, dev, mtime, fileid, 1, alloc, alloc, 0, 0, is_mount
    if file_ret != FILE_ATTRS:
        return objtype, name, dev, mtime, fileid, 0, -1, -1, 0, 0, False
    nlink, alloc, private, cloneid, refcnt = FILE_TAIL.unpack_from(view, tail)
    if clone_ret != CLONE_ATTRS:
        private, refcnt = alloc, 0
    return objtype, name, dev, mtime, fileid, nlink, alloc, private, cloneid, refcnt, False
