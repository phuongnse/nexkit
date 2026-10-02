"""Hold native directory handles while trusted workspace controls are restored.

FILE_SHARE_DELETE is deliberately absent. Setup cannot rename, remove or
replace the held directories or any of their ancestors before collection.
Reparse points and remote filesystem paths are rejected before traversal.
See https://learn.microsoft.com/windows/win32/api/fileapi/nf-fileapi-createfilew.
"""

from __future__ import annotations

import ctypes
import os
from contextlib import contextmanager
from ctypes import wintypes as w
from pathlib import Path

from .policy import require


def read_file(path, maximum):
    """Read one bounded regular file without following a reparse point or hardlink."""
    import msvcrt

    require(os.name == "nt", "Native file handles require Windows")
    path = Path(path).absolute()
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [
        w.LPCWSTR,
        w.DWORD,
        w.DWORD,
        w.LPVOID,
        w.DWORD,
        w.DWORD,
        w.HANDLE,
    ]
    kernel.CreateFileW.restype = w.HANDLE
    kernel.GetFileInformationByHandle.argtypes = [w.HANDLE, ctypes.POINTER(FILE_INFO)]
    kernel.GetFileInformationByHandle.restype = w.BOOL
    kernel.GetFileType.argtypes, kernel.GetFileType.restype = [w.HANDLE], w.DWORD
    kernel.CloseHandle.argtypes, kernel.CloseHandle.restype = [w.HANDLE], w.BOOL
    with pinned_directories(path.parent):
        handle = kernel.CreateFileW(
            str(path), 0x80000000, 1, None, 3, 0x00200000 | 0x02000000, None
        )
        if handle == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            details = FILE_INFO()
            if not kernel.GetFileInformationByHandle(handle, ctypes.byref(details)):
                raise ctypes.WinError(ctypes.get_last_error())
            require(
                kernel.GetFileType(handle) == 1
                and not details.attributes & (0x10 | 0x400)
                and details.links == 1
                and not details.size_high
                and details.size_low <= maximum,
                "Expected a bounded regular file without links or reparse points",
            )
            descriptor = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
        except BaseException:
            kernel.CloseHandle(handle)
            raise
        with os.fdopen(descriptor, "rb") as source:
            value = source.read(maximum + 1)
            require(len(value) <= maximum, "File exceeds the accepted size limit")
            return value


class FILE_INFO(ctypes.Structure):
    _fields_ = [
        ("attributes", w.DWORD),
        ("created", w.FILETIME),
        ("accessed", w.FILETIME),
        ("written", w.FILETIME),
        ("volume", w.DWORD),
        ("size_high", w.DWORD),
        ("size_low", w.DWORD),
        ("links", w.DWORD),
        ("index_high", w.DWORD),
        ("index_low", w.DWORD),
    ]


@contextmanager
def pinned_directories(*paths):
    require(os.name == "nt", "Directory handle sealing requires native Windows")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [
        w.LPCWSTR,
        w.DWORD,
        w.DWORD,
        w.LPVOID,
        w.DWORD,
        w.DWORD,
        w.HANDLE,
    ]
    kernel.CreateFileW.restype = w.HANDLE
    kernel.GetFileInformationByHandle.argtypes = [w.HANDLE, ctypes.POINTER(FILE_INFO)]
    kernel.GetFileInformationByHandle.restype = w.BOOL
    kernel.CloseHandle.argtypes, kernel.CloseHandle.restype = [w.HANDLE], w.BOOL
    handles = {}
    try:
        for value in paths:
            root = Path(value).absolute()
            require(not root.drive.startswith("\\\\"), "Use a local native Windows workspace")
            for path in (*reversed(root.parents), root):
                if path in handles:
                    continue
                # FILE_LIST_DIRECTORY participates in sharing checks. A zero
                # access metadata handle does not prevent a later rename,
                # even when FILE_SHARE_DELETE is absent.
                handle = kernel.CreateFileW(str(path), 1, 3, None, 3, 0x00200000 | 0x02000000, None)
                if handle == ctypes.c_void_p(-1).value:
                    raise ctypes.WinError(ctypes.get_last_error())
                handles[path] = handle
                info = FILE_INFO()
                if not kernel.GetFileInformationByHandle(handle, ctypes.byref(info)):
                    raise ctypes.WinError(ctypes.get_last_error())
                require(
                    info.attributes & 0x10 and not info.attributes & 0x400,
                    "Workspace sealing rejects reparse points and non-directories",
                )
        yield
    finally:
        for handle in reversed(list(handles.values())):
            kernel.CloseHandle(handle)
