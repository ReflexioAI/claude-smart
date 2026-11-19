"""Exercise physical Windows directory pinning in a disposable fixture."""

import ctypes
import json
import os
import subprocess
import tempfile
from ctypes import wintypes
from pathlib import Path

READ_DIRECTORY = 0x81
SHARE_READ = 0x1
OPEN_EXISTING = 3
BACKUP_SEMANTICS = 0x02000000
OPEN_REPARSE_POINT = 0x00200000
REPARSE_POINT = 0x400
FILE_ATTRIBUTE_TAG_INFO = 9


class Attributes(ctypes.Structure):
    _fields_ = [("attributes", wintypes.DWORD), ("tag", wintypes.DWORD)]


kernel = ctypes.WinDLL("kernel32", use_last_error=True)
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
get_info = kernel.GetFileInformationByHandleEx
get_info.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
get_info.restype = wintypes.BOOL
close = kernel.CloseHandle
close.argtypes = [wintypes.HANDLE]
close.restype = wintypes.BOOL


def open_directory(path):
    handle = create_file(
        str(path),
        READ_DIRECTORY,
        SHARE_READ,
        None,
        OPEN_EXISTING,
        BACKUP_SEMANTICS | OPEN_REPARSE_POINT,
        None,
    )
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    return handle


def attributes(handle):
    info = Attributes()
    if not get_info(
        handle, FILE_ATTRIBUTE_TAG_INFO, ctypes.byref(info), ctypes.sizeof(info)
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    return info.attributes


with tempfile.TemporaryDirectory(prefix="rx-open-scope-") as tmp:
    base = Path(tmp)
    parent = base / "project" / ".opencode"
    scope = parent / "plugins"
    scope.mkdir(parents=True)
    outside = base / "other"
    outside.mkdir()
    (outside / "claude-smart.js").write_text("outside sentinel", encoding="utf-8")
    leaf = scope / "claude-smart.js"
    leaf.write_text("original", encoding="utf-8")
    pinned_parent = open_directory(parent)
    pinned_scope = open_directory(scope)
    try:
        assert not attributes(pinned_parent) & REPARSE_POINT
        assert not attributes(pinned_scope) & REPARSE_POINT
        blocked = []
        for directory in [scope, parent]:
            try:
                os.replace(directory, directory.with_name(directory.name + "-renamed"))
            except OSError as error:
                assert error.winerror in {5, 32}, repr(error)
                blocked.append(error.winerror)
            else:
                raise AssertionError(f"the held ancestor allowed rename: {directory}")
        stage = scope / "candidate.tmp"
        with stage.open("x", encoding="utf-8") as sibling:
            sibling.write("staged update")
        stage.replace(leaf)
        assert leaf.read_text(encoding="utf-8") == "staged update"
    finally:
        close(pinned_scope)
        close(pinned_parent)
    junction = base / "junction"
    subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(junction), str(outside)],
        check=True,
        capture_output=True,
    )
    junction_handle = open_directory(junction)
    try:
        assert attributes(junction_handle) & REPARSE_POINT
    finally:
        close(junction_handle)
        junction.rmdir()
    assert (outside / "claude-smart.js").read_text(
        encoding="utf-8"
    ) == "outside sentinel"
    print(
        json.dumps(
            {
                "ancestor_renames_refused": blocked,
                "existing_regular_loader_refresh_with_held_ancestors": True,
                "junction_open_returned_reparse_metadata": True,
                "outside_target_unchanged": True,
            }
        )
    )
