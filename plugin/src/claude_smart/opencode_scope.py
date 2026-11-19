"""Open OpenCode files without following project-controlled links."""

from __future__ import annotations

import json
import os
import signal
import stat
import sys
from collections.abc import Callable
from contextlib import ExitStack
from pathlib import Path
from secrets import token_hex
from typing import BinaryIO, TypedDict


def open_scoped_file(
    path: Path,
    lifetime: ExitStack,
    *,
    writable: bool = False,
    rollback_creation: list[Callable[[], None]] | None = None,
    remove_existing: list[Callable[[], None]] | None = None,
) -> tuple[BinaryIO, str]:
    """Hold verified ancestors and a regular leaf for the requested operation."""
    parts = path.parts
    if not path.is_absolute() or len(parts) < 2 or ".." in parts:
        raise ValueError("OpenCode path must be absolute and canonical")
    create = rollback_creation is not None
    if os.name == "nt":
        return _open_windows_file(
            path,
            lifetime,
            writable=writable,
            rollback_creation=rollback_creation,
            remove_existing=remove_existing,
        )
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    parent = os.open(parts[0], directory_flags)
    lifetime.callback(os.close, parent)
    for component in parts[1:-1]:
        try:
            child = os.open(component, directory_flags, dir_fd=parent)
        except FileNotFoundError:
            if not create:
                raise
            try:
                os.mkdir(component, dir_fd=parent)
            except FileExistsError:
                pass
            child = os.open(component, directory_flags, dir_fd=parent)
        lifetime.callback(os.close, child)
        parent = child
    flags = (os.O_RDWR if writable else os.O_RDONLY) | os.O_NOFOLLOW | os.O_NONBLOCK
    if create:
        flags |= os.O_CREAT | os.O_EXCL
    fd = os.open(parts[-1], flags, 0o600, dir_fd=parent)
    source, identity = _open_regular_file(fd, lifetime, writable)
    remove = rollback_creation if create else remove_existing
    if remove is not None:
        name = parts[-1]

        def remove_created() -> None:
            _remove_posix_entry(parent, name, identity, discard_missing=create)

        remove.append(remove_created)
    return source, identity


def _remove_posix_entry(
    parent: int, name: str, identity: str, *, discard_missing: bool
) -> None:
    # A checked public pathname is not stable across unlink. Move its current
    # entry to an owned directory, then inspect the entry we actually moved.
    private = f".claude-smart-removal-{token_hex(12)}"
    os.mkdir(private, 0o700, dir_fd=parent)
    retained = False
    try:
        quarantine = os.open(
            private, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent
        )
        try:
            try:
                os.rename(name, "entry", src_dir_fd=parent, dst_dir_fd=quarantine)
            except FileNotFoundError:
                if discard_missing:
                    return
                raise
            retained = True
            moved = os.stat("entry", follow_symlinks=False, dir_fd=quarantine)
            replaced = f"{moved.st_dev}:{moved.st_ino}" != identity
            if replaced:
                try:
                    # Restore the substituted entry only if its original name
                    # is still free. A second substitution is kept in place.
                    os.link(
                        "entry",
                        name,
                        src_dir_fd=quarantine,
                        dst_dir_fd=parent,
                        follow_symlinks=False,
                    )
                except OSError as error:
                    raise ValueError(
                        f"OpenCode file changed before removal; its replacement is preserved in {private}/entry"
                    ) from error
            os.unlink("entry", dir_fd=quarantine)
            retained = False
            if replaced:
                raise ValueError(
                    "OpenCode file changed before removal; its replacement was restored"
                )
        finally:
            os.close(quarantine)
    finally:
        if not retained:
            os.rmdir(private, dir_fd=parent)


def _open_regular_file(
    fd: int, lifetime: ExitStack, writable: bool
) -> tuple[BinaryIO, str]:
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError("OpenCode path must be a regular, unlinked file")
        source = os.fdopen(fd, "r+b" if writable else "rb")
    except BaseException:
        os.close(fd)
        raise
    return lifetime.enter_context(source), f"{info.st_dev}:{info.st_ino}"


def _open_windows_file(
    path: Path,
    lifetime: ExitStack,
    *,
    writable: bool,
    rollback_creation: list[Callable[[], None]] | None,
    remove_existing: list[Callable[[], None]] | None,
) -> tuple[BinaryIO, str]:
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
    set_info = kernel.SetFileInformationByHandle
    set_info.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
    set_info.restype = wintypes.BOOL
    create = rollback_creation is not None

    close = kernel.CloseHandle
    close.argtypes = [wintypes.HANDLE]
    close.restype = wintypes.BOOL

    def open_no_follow(name: Path, *, directory: bool) -> int:
        # Directory data access makes Windows enforce sharing restrictions
        # on rename. FILE_READ_ATTRIBUTES alone does not enforce them.
        access = 0x81 if directory else 0xC0000000 if writable else 0x80000000
        if (create or remove_existing is not None) and not directory:
            access |= 0x10000  # DELETE, for handle-based compensation on failure.
        flags = 0x00200000 | (0x02000000 if directory else 0)
        # The installer may already hold the validated leaf for refresh.
        # A leaf read may coexist with that writer but cannot be replaced.
        share = 0x1 if directory or writable else 0x3
        disposition = 1 if create and not directory else 3
        handle = create_file(str(name), access, share, None, disposition, flags, None)
        if handle == ctypes.c_void_p(-1).value:
            error = ctypes.get_last_error()
            if error in {2, 3}:
                raise FileNotFoundError(str(name))
            if error in {80, 183}:
                raise FileExistsError(str(name))
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
        try:
            handle = open_no_follow(parent, directory=True)
        except FileNotFoundError:
            if not create:
                raise
            parent.mkdir(exist_ok=True)
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
        fd = msvcrt.open_osfhandle(
            leaf, (os.O_RDWR if writable else os.O_RDONLY) | os.O_BINARY
        )
    except BaseException:
        close(leaf)
        raise
    try:
        source = lifetime.enter_context(os.fdopen(fd, "r+b" if writable else "rb"))
    except BaseException:
        os.close(fd)
        raise
    remove = rollback_creation if create else remove_existing
    if remove is not None:

        def remove_created() -> None:
            delete = wintypes.BOOL(True)
            if not set_info(leaf, 4, ctypes.byref(delete), ctypes.sizeof(delete)):
                raise ctypes.WinError(ctypes.get_last_error())

        remove.append(remove_created)
    return source, identity


_MAX_CONFIG_BYTES = 1024 * 1024


def _read_bytes(file: BinaryIO) -> bytes:
    contents = file.read(_MAX_CONFIG_BYTES + 1)
    if len(contents) > _MAX_CONFIG_BYTES:
        raise ValueError("OpenCode file exceeds the installer limit")
    return contents


def _snapshot(path: Path) -> dict[str, str | None]:
    with ExitStack() as lifetime:
        try:
            file, identity = open_scoped_file(path, lifetime)
        except FileNotFoundError:
            return {"contents": None, "identity": None}
        return {"contents": _read_bytes(file).decode("utf-8"), "identity": identity}


def _write_bytes(file: BinaryIO, data: bytes) -> None:
    file.seek(0)
    file.write(data)
    file.truncate()
    file.flush()
    os.fsync(file.fileno())


class _Edit(TypedDict):
    path: str
    replacement: str | None
    contents: str | None
    identity: str | None


def _publish(edits: list[_Edit], *, check: bool) -> None:
    """Verify the existing files in a plan before editing any of them."""
    with ExitStack() as lifetime:
        prepared = []
        removals = []
        for edit in edits:
            path = Path(edit["path"])
            contents = edit["replacement"]
            replacement = None if contents is None else contents.encode("utf-8")
            prior = edit["contents"]
            if replacement is not None and len(replacement) > _MAX_CONFIG_BYTES:
                raise ValueError("OpenCode edit exceeds the installer limit")
            if prior is None:
                if replacement is None:
                    raise ValueError("OpenCode removal requires a file snapshot")
                if (
                    edit["identity"] is not None
                    or _snapshot(path)["identity"] is not None
                ):
                    raise ValueError("OpenCode file appeared before publication")
                if check:
                    with ExitStack() as probe:
                        remove_probe = []
                        open_scoped_file(
                            path.with_name(f".claude-smart-loader-{token_hex(8)}.tmp"),
                            probe,
                            writable=True,
                            rollback_creation=remove_probe,
                        )
                        remove_probe[0]()
                prepared.append((path, None, None, replacement))
                continue
            original = prior.encode("utf-8")
            file, identity = open_scoped_file(
                path,
                lifetime,
                writable=(replacement is not None and original != replacement),
                remove_existing=removals if replacement is None else None,
            )
            if edit["identity"] != identity or _read_bytes(file) != original:
                raise ValueError("OpenCode file changed before publication")
            if replacement is not None and original != replacement:
                prepared.append((path, file, original, replacement))

        if len(removals) > 1:
            raise ValueError("An OpenCode scope can remove only its loader")
        if check:
            return
        with ExitStack() as compensation:
            for path, file, prior, replacement in prepared:
                if file is None:
                    remove_created = []
                    file, _ = open_scoped_file(
                        path, lifetime, writable=True, rollback_creation=remove_created
                    )
                    compensation.callback(remove_created[0])
                else:
                    file.seek(0)
                    if _read_bytes(file) != prior:
                        raise ValueError("OpenCode file changed before publication")
                    compensation.callback(_write_bytes, file, prior)
                _write_bytes(file, replacement)
            for remove in removals:
                remove()
            compensation.pop_all()


def main() -> int:
    try:
        request = json.load(sys.stdin.buffer)
        if sys.argv[1:] == ["snapshot"]:
            response = [_snapshot(Path(path)) for path in request]
        elif sys.argv[1:] in (["check"], ["publish"]):
            _publish(request, check=sys.argv[1] == "check")
            response = {"ok": True}
        else:
            return 2
    except (OSError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 1
    print(json.dumps(response))
    return 0


if __name__ == "__main__":

    def _interrupt(signum, _frame):
        signal.signal(signum, signal.SIG_IGN)
        raise InterruptedError("OpenCode scope edit interrupted")

    signal.signal(signal.SIGTERM, _interrupt)
    signal.signal(signal.SIGINT, _interrupt)
    raise SystemExit(main())
