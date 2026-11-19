"""Open OpenCode files without following project-controlled links."""

from __future__ import annotations

import os
import stat
from contextlib import ExitStack
from pathlib import Path
from typing import BinaryIO


def open_scoped_file(path: Path, lifetime: ExitStack) -> tuple[BinaryIO, str]:
    """Hold the verified ancestors and regular leaf until the owner finishes."""
    parts = path.parts
    if not path.is_absolute() or len(parts) < 2 or ".." in parts:
        raise ValueError("OpenCode path must be absolute and canonical")
    if os.name == "nt":
        return _open_windows_file(path, lifetime)
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    parent = os.open(parts[0], directory_flags)
    lifetime.callback(os.close, parent)
    for component in parts[1:-1]:
        parent = os.open(component, directory_flags, dir_fd=parent)
        lifetime.callback(os.close, parent)
    fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
    return _open_regular_file(fd, lifetime)


def _open_regular_file(fd: int, lifetime: ExitStack) -> tuple[BinaryIO, str]:
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError("OpenCode path must be a regular, unlinked file")
        source = os.fdopen(fd, "rb")
    except BaseException:
        os.close(fd)
        raise
    return lifetime.enter_context(source), f"{info.st_dev}:{info.st_ino}"


def _open_windows_file(path: Path, lifetime: ExitStack) -> tuple[BinaryIO, str]:
    import ctypes
    import msvcrt
    from ctypes import wintypes

    class AttributeTag(ctypes.Structure):
        _fields_ = [("attributes", wintypes.DWORD), ("tag", wintypes.DWORD)]

    class HandleInfo(ctypes.Structure):
        _fields_ = [
            ("attributes", wintypes.DWORD),
            ("created", wintypes.FILETIME),
            ("accessed", wintypes.FILETIME),
            ("modified", wintypes.FILETIME),
            ("volume", wintypes.DWORD),
            ("size_hi", wintypes.DWORD),
            ("size_low", wintypes.DWORD),
            ("links", wintypes.DWORD),
            ("index_hi", wintypes.DWORD),
            ("index_low", wintypes.DWORD),
        ]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    drive_type = kernel.GetDriveTypeW
    drive_type.argtypes = [wintypes.LPCWSTR]
    drive_type.restype = wintypes.UINT
    if len(path.drive) != 2 or path.drive[1] != ":" or drive_type(path.anchor) != 3:
        raise ValueError("OpenCode paths must be on a local fixed drive")

    create_file = kernel.CreateFileW
    create_file.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    create_file.restype = wintypes.HANDLE
    file_info = kernel.GetFileInformationByHandleEx
    file_info.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    ]
    file_info.restype = wintypes.BOOL
    handle_info = kernel.GetFileInformationByHandle
    handle_info.argtypes = [wintypes.HANDLE, ctypes.POINTER(HandleInfo)]
    handle_info.restype = wintypes.BOOL
    close = kernel.CloseHandle
    close.argtypes = [wintypes.HANDLE]
    close.restype = wintypes.BOOL

    def open_no_follow(name: Path, *, directory: bool) -> int:
        # Directory data access makes Windows enforce sharing restrictions
        # on rename. FILE_READ_ATTRIBUTES alone does not enforce them.
        access = 0x81 if directory else 0x80000000
        flags = 0x00200000 | (0x02000000 if directory else 0)
        # The installer may already hold the validated leaf for refresh.
        # A leaf read may coexist with that writer but cannot be replaced.
        share = 0x1 if directory else 0x3
        handle = create_file(str(name), access, share, None, 3, flags, None)
        if handle == ctypes.c_void_p(-1).value:
            error = ctypes.get_last_error()
            if error in {2, 3}:
                raise FileNotFoundError(str(name))
            raise ctypes.WinError(error)
        try:
            info = AttributeTag()
            if not file_info(handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
                raise ctypes.WinError(ctypes.get_last_error())
            if info.attributes & 0x400 or bool(info.attributes & 0x10) != directory:
                raise ValueError("OpenCode paths must not use filesystem redirects")
        except BaseException:
            close(handle)
            raise
        return handle

    for parent in reversed(path.parents[:-1]):
        handle = open_no_follow(parent, directory=True)
        lifetime.callback(close, handle)
    leaf = open_no_follow(path, directory=False)
    try:
        info = HandleInfo()
        if not handle_info(leaf, ctypes.byref(info)):
            raise ctypes.WinError(ctypes.get_last_error())
        if info.links != 1:
            raise ValueError("OpenCode path must be a regular, unlinked file")
        # libuv reports the 32-bit volume serial and 64-bit file index.
        identity = f"{info.volume}:{(info.index_hi << 32) | info.index_low}"
        fd = msvcrt.open_osfhandle(leaf, os.O_RDONLY | os.O_BINARY)
    except BaseException:
        close(leaf)
        raise
    try:
        return lifetime.enter_context(os.fdopen(fd, "rb")), identity
    except BaseException:
        os.close(fd)
        raise
