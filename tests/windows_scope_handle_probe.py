"""Exercise physical Windows directory pinning in a disposable fixture."""

import ctypes
import json
import os
import subprocess
import tempfile
from contextlib import ExitStack
from ctypes import wintypes
from pathlib import Path

from probe_opencode_scope import open_scoped_file

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
    node_identities = subprocess.check_output(
        [
            "node",
            "-e",
            "const fs = require('fs'), p = process.argv[1], fd = fs.openSync(p, 'r');"
            "console.log([fs.lstatSync(p, {bigint:true}),fs.fstatSync(fd, {bigint:true})]"
            ".map(s => String(s.dev) + ':' + String(s.ino)).join(' '));fs.closeSync(fd);",
            str(leaf),
        ],
        text=True,
    ).split()
    with ExitStack() as stack:
        scoped, identity = open_scoped_file(leaf, stack)
        assert scoped.read() == b"original"
        print(
            json.dumps(
                {"node_identities": node_identities, "scoped_identity": identity}
            )
        )
        assert node_identities == [identity, identity]
    with subprocess.Popen(
        [
            "node",
            "-e",
            "const fd = require('fs').openSync(process.argv[1], 'r+');"
            "console.log('open');process.stdin.resume();"
            "process.stdin.on('end', () => require('fs').closeSync(fd));",
            str(leaf),
        ],
        text=True,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
    ) as writer:
        try:
            assert writer.stdout.readline().strip() == "open"
            with ExitStack() as stack:
                guarded, identity = open_scoped_file(leaf, stack)
                assert guarded.read() == b"original"
                assert node_identities == [identity, identity]
                print(
                    json.dumps(
                        {
                            "existing_node_writer_coexisted_with_anchored_save_guard": True
                        }
                    )
                )
        finally:
            writer.stdin.close()
            assert writer.wait(timeout=10) == 0
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
        try:
            stage.replace(leaf)
        except PermissionError as error:
            assert error.winerror == 32, repr(error)
            replace_denied = True
        else:
            raise AssertionError("the held scope unexpectedly allowed replacement")
        with leaf.open("r+", encoding="utf-8") as current:
            assert current.read() == "original"
            current.seek(0)
            current.write("staged update")
            current.truncate()
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
                "sibling_replace_denied": replace_denied,
                "existing_regular_loader_refresh_with_held_ancestors": True,
                "junction_open_returned_reparse_metadata": True,
                "outside_target_unchanged": True,
            }
        )
    )


from probe_opencode_scope import _publish, _snapshot  # noqa: E402

with tempfile.TemporaryDirectory() as temporary:
    scope = Path(temporary).resolve()
    existing = scope / "opencode.jsonc"
    existing.write_bytes(b"original")
    new = scope / ".opencode/plugins/claude-smart.js"
    plan = [
        {"path": str(existing), **_snapshot(existing), "replacement": "updated"},
        {"path": str(new), **_snapshot(new), "replacement": "generated"},
    ]
    _publish(plan, check=True)
    assert existing.read_bytes() == b"original" and not new.exists()
    assert not list(new.parent.glob("*.tmp"))
    _publish(plan, check=False)
    assert existing.read_bytes() == b"updated"
    assert new.read_bytes() == b"generated"
    with ExitStack() as stack:
        created = []
        rollback = scope / "new-file"
        handle, _ = open_scoped_file(
            rollback, stack, writable=True, rollback_creation=created
        )
        handle.write(b"partial")
        handle.flush()
        created[0]()
    assert not rollback.exists()
    print(
        json.dumps(
            {
                "native_scoped_writer_checked_and_published": True,
                "created_file_rollback": True,
            }
        )
    )


with tempfile.TemporaryDirectory() as temporary:
    scope = Path(temporary).resolve()
    loader = scope / ".opencode/plugins/claude-smart.js"
    edit = {"path": str(loader), **_snapshot(loader), "replacement": "generated"}
    _publish([edit], check=False)
    remove = {"path": str(loader), **_snapshot(loader), "replacement": None}
    _publish([remove], check=True)
    assert loader.is_file()
    _publish([remove], check=False)
    assert not loader.exists()
    print(json.dumps({"native_scoped_loader_removal": True}))


with tempfile.TemporaryDirectory() as temporary:
    base = Path(temporary).resolve()
    redirect, outside = base / "redirect", base / "outside"
    outside.mkdir()
    (outside / "sentinel").write_text("outside", encoding="utf-8")
    mkdir = os.mkdir
    is_dir = Path.is_dir
    fired = False

    def collide(path, *args, **kwargs):
        global fired
        if Path(path) == redirect and not fired:
            fired = True
            subprocess.run(
                ["cmd.exe", "/d", "/c", "mklink", "/J", str(redirect), str(outside)],
                check=True,
                capture_output=True,
            )
        return mkdir(path, *args, **kwargs)

    def refuse_follow(path):
        if path == redirect:
            raise AssertionError("the scope owner followed the directory collision")
        return is_dir(path)

    os.mkdir = collide
    Path.is_dir = refuse_follow
    try:
        with ExitStack() as stack:
            try:
                open_scoped_file(
                    redirect / "claude-smart.js",
                    stack,
                    writable=True,
                    rollback_creation=[],
                )
            except ValueError as error:
                assert "redirects" in str(error), str(error)
            else:
                raise AssertionError("scope accepted a redirected parent")
    finally:
        Path.is_dir = is_dir
        os.mkdir = mkdir
        redirect.rmdir()
    assert fired and not (outside / "claude-smart.js").exists()
    assert (outside / "sentinel").read_text(encoding="utf-8") == "outside"
    print(
        json.dumps({"native_directory_creation_collision_refused_without_follow": True})
    )
