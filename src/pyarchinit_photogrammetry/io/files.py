"""Files put in their place.

:func:`replace_file` puts a file written apart - a product rebuilt in a temporary file beside its final name - in the
place of the file there, in one move: ``os.replace``, everywhere but on Windows. There ``os.replace`` (MoveFileExW
with MOVEFILE_REPLACE_EXISTING) refuses to replace a file that has a handle open on it, even one opened sharing its
deletion, as the link to RustGIS (:mod:`.rustgis_link`) keeps the products it serves open while RustGIS reads their
tiles: rebuilding the orthophoto while RustGIS shows it would fail at its very end. A rename with POSIX semantics
(SetFileInformationByHandle with FileRenameInfoEx and FILE_RENAME_FLAG_POSIX_SEMANTICS; Windows 10 1607 and later,
on NTFS; the way Rust's standard library renames) replaces it: the file replaced loses its name at once and lives on,
nameless, for the handles open on it, as on POSIX. Where that rename is not there (another file system, an older
Windows), ``os.replace`` is tried a few times over a short while, for a handle that goes away (an antivirus reading
the file just written).
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

RETRY_DELAYS = (0.05, 0.1, 0.2, 0.4)  # seconds waited before each new try of os.replace a handle refused (Windows)

# kernel32 (Windows): DELETE access; FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE; OPEN_EXISTING;
# FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT (the name itself is renamed, a folder's or a link's too);
# FileRenameInfoEx; FILE_RENAME_FLAG_REPLACE_IF_EXISTS | FILE_RENAME_FLAG_POSIX_SEMANTICS
_DELETE, _SHARE_ALL, _OPEN_EXISTING = 0x00010000, 0x1 | 0x2 | 0x4, 3
_OPEN_FLAGS = 0x02000000 | 0x00200000
_RENAME_INFO_EX, _REPLACE_POSIX = 22, 0x1 | 0x2
_replace = os.replace  # what the tries call (a test puts a refusal in its place)


def _windows() -> bool:
    return sys.platform == "win32"


def _kernel32():
    """kernel32 through ctypes, with the signatures of the calls :func:`_posix_rename` makes (Windows only)."""
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD,
                                     wintypes.DWORD, wintypes.HANDLE]
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.SetFileInformationByHandle.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    kernel32.SetFileInformationByHandle.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    return kernel32


def _posix_rename(src: Path, dst: Path) -> bool:
    """``src`` given the name ``dst`` with POSIX semantics (Windows): the file at ``dst``, if any, loses its name at
    once, handles open on it or not (they must share its deletion). False when Windows does not do it - the file
    system or the system too old for FileRenameInfoEx, a handle on ``dst`` that does not share its deletion, ``src``
    not opened: nothing is changed then."""
    import ctypes

    kernel32 = _kernel32()
    handle = kernel32.CreateFileW(str(src), _DELETE, _SHARE_ALL, None, _OPEN_EXISTING, _OPEN_FLAGS, None)
    if not handle or handle == ctypes.c_void_p(-1).value:  # INVALID_HANDLE_VALUE
        return False
    try:
        # the name as Windows counts it, in UTF-16 units (a character outside the BMP is two): sized and measured on
        # its encoding, never on Python's characters
        encoded = os.path.abspath(dst).encode("utf-16-le", "surrogatepass")

        class RenameInformation(ctypes.Structure):  # FILE_RENAME_INFO, its FileName as long as the name and its end
            _fields_ = [("flags", ctypes.c_uint32), ("root", ctypes.c_void_p), ("length", ctypes.c_uint32),
                        ("name", ctypes.c_uint16 * (len(encoded) // 2 + 1))]

        information = RenameInformation(_REPLACE_POSIX, None, len(encoded))  # FileNameLength in bytes, no end
        ctypes.memmove(information.name, encoded, len(encoded))
        return bool(kernel32.SetFileInformationByHandle(handle, _RENAME_INFO_EX, ctypes.byref(information),
                                                        ctypes.sizeof(information)))
    finally:
        kernel32.CloseHandle(handle)


def replace_file(src: str | Path, dst: str | Path) -> None:
    """The file at ``src`` put at ``dst``, in the place of the file there if any, in one move: ``os.replace``; on
    Windows first a rename with POSIX semantics (:func:`_posix_rename`), which a handle open on the file replaced -
    the link's raster server reading it for RustGIS - does not stop as it stops ``os.replace``, then ``os.replace``;
    both tried again after each of :data:`RETRY_DELAYS` while a handle refuses them (an antivirus reading the file
    just written refuses the rename for a moment, while ``os.replace`` never gets past the server's handle). Raises
    what ``os.replace`` raises (the last refusal included)."""
    if not _windows():
        _replace(src, dst)
        return
    for delay in (*RETRY_DELAYS, None):
        if _posix_rename(Path(src), Path(dst)):
            return
        try:
            _replace(src, dst)
            return
        except PermissionError:
            if delay is None:
                raise
            time.sleep(delay)


__all__ = ["replace_file", "RETRY_DELAYS"]
