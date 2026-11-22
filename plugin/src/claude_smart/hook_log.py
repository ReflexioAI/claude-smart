"""Best-effort structured log of hook fires and their outcomes.

Why a dedicated log rather than relying on the Python ``logging`` module:
hooks run as short-lived subprocesses with no preconfigured handler, so
``_LOGGER.warning(...)`` calls silently vanish to stderr that Claude Code
captures but typically does not surface. The dispatcher already wraps
handlers in a broad ``except`` (so a hook crash never breaks the user's
session) — without a forensic trail the silent-fail mode is invisible.

This module attempts one JSON line per hook fire after the handler returns.
The log belongs to the selected session directory or the default account.
The Stop and SessionEnd handlers additionally piggyback their publish
outcome into the record so the chain hook→adapter→backend is fully
observable from a single file.

Failure policy: a busy log lock skips a diagnostic instead of blocking a hook.
Every public function in this module swallows all
``OSError``/``Exception`` paths. Logging is best-effort; a stuck disk
or read-only ``$HOME`` must never abort an in-progress hook.
"""

from __future__ import annotations

import contextlib
import errno
import json
import logging
import os
import secrets
import stat
import time
from collections.abc import Iterator
from functools import partial
from pathlib import Path
from typing import Any

from claude_smart import runtime, state

# ``fcntl`` is POSIX-only. On Windows we skip the cross-process lock and
# accept the unlikely rotation race (claude-smart is primarily a POSIX
# tool for Claude Code's plugin runtime).
try:
    import fcntl as _fcntl
except ImportError:  # pragma: no cover — exercised on Windows only
    _fcntl = None  # type: ignore[assignment]

_LOGGER = logging.getLogger(__name__)

# Anchored on $HOME so unit tests can monkeypatch ``_LOG_PATH`` to a
# tmp_path. The directory is created lazily on first write — keeping
# import side-effect-free matches the rest of the package.
_LOG_PATH = Path.home() / ".claude-smart" / "hook.log"

_TRUNCATE_LIMIT = 4096

# Hard cap on ``hook.log`` size. At ~400 bytes/line and ~50 lines per
# typical session, 5 MB ≈ 12k records ≈ a few hundred sessions of recent
# forensic history — enough to debug the last week, bounded so a power
# user's log can't grow unbounded.
_MAX_LOG_BYTES = 5 * 1024 * 1024


def log_event(
    *,
    event: str,
    host: str,
    session_id: Any = None,
    project_id: Any = None,
    cwd: Any = None,
    internal_skipped: bool = False,
    handler_status: str = "ok",
    publish_status: str | None = None,
    publish_count: int | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    """Append one JSON record describing a hook fire.

    Args:
        event (str): Hook event name (``"stop"``, ``"user-prompt"``, …).
        host (str): Host identifier (``"claude-code"`` / ``"codex"``).
        session_id (Any): Claude Code session id from the payload, or
            ``None`` when the payload didn't carry one.
        project_id (Any): Project-id resolved from ``cwd``. Optional.
        cwd (Any): Working directory reported by the hook payload.
            Optional — preserved verbatim so a future audit can correlate
            fires against workspace directories.
        internal_skipped (bool): True when ``is_internal_invocation``
            short-circuited the dispatcher (reflexio-internal sub-claude
            calls, or invocations from inside a Reflexio checkout).
        handler_status (str): ``"ok"`` for a clean return, ``"unknown_event"``
            when no handler is registered, or ``"raised:<ExcClass>: <msg>"``
            when the handler raised — formatted by ``hook.main``.
        publish_status (str | None): Status returned by
            ``publish.publish_unpublished`` (``"ok"`` / ``"recovered"`` /
            ``"failed"`` / ``"nothing"`` / ``"deferred"`` / ``"parked"``), or
            ``"queued"`` for Codex local enqueue. Worker records report subsequent publication separately.
        publish_count (int | None): Interaction count from the same
            tuple. ``None`` when the handler doesn't publish.
        extra (dict[str, Any] | None): Free-form fields preserved as-is.

    Returns:
        None
    """
    record: dict[str, Any] = {
        "ts": int(time.time()),
        "event": event,
        "host": host,
        "session_id": _truncate(session_id),
        "project_id": _truncate(project_id),
        "cwd": _truncate(cwd),
        "internal_skipped": bool(internal_skipped),
        "handler_status": _truncate(handler_status),
    }
    if publish_status is not None:
        record["publish_status"] = publish_status
        try:
            record["publish_count"] = int(
                publish_count if publish_count is not None else 0
            )
        except (TypeError, ValueError):
            # Defensive: a hostile caller passing an unparseable publish_count
            # must not abort the hook fire. Treat as zero, the record still
            # writes with publish_status preserved.
            record["publish_count"] = 0
    if extra:
        for key, value in extra.items():
            if key in record:
                continue
            record[key] = _truncate(value)
    for key, value in runtime.plugin_metadata().items():
        record.setdefault(key, value)

    try:
        line = json.dumps(record, ensure_ascii=False, default=str) + "\n"
    except (TypeError, ValueError) as exc:
        _LOGGER.debug("hook_log: json encode failed: %s", exc)
        return

    path = log_path()
    protect_account = bool(os.environ.get("PYTEST_CURRENT_TEST"))
    if protect_account and _is_account_log(path):
        return
    try:
        # Create along the configured path so every component is traversable.
        path.parent.mkdir(parents=True, exist_ok=True)
        # Bind all write operations to the same physical directory.
        path = Path(os.path.realpath(path.parent, strict=True)) / path.name
        if protect_account and _is_account_log(path):
            # Also check the bound path in case a link moved after creation.
            return
    except OSError as exc:
        _LOGGER.debug("hook_log: directory for %s unavailable: %s", path, exc)
        return

    directory_fd = None
    try:
        if os.name != "nt":
            # A writable session directory may permit search without listing.
            search_mode = getattr(os, "O_SEARCH", getattr(os, "O_PATH", os.O_RDONLY))
            directory_fd = os.open(path.parent, search_mode | os.O_DIRECTORY)
        _append_and_rotate(path, line, directory_fd)
    except OSError as exc:
        _LOGGER.debug("hook_log: could not append in %s: %s", path.parent, exc)
    finally:
        if directory_fd is not None:
            with contextlib.suppress(OSError):
                os.close(directory_fd)


def _append_and_rotate(path: Path, line: str, directory_fd: int | None) -> None:
    # On POSIX every operation is relative to this open directory, so a rename
    # cannot move writes from the locked directory into its replacement.
    if directory_fd is not None:
        path = Path(path.name)
    opener = partial(_open_nonblocking_regular, directory_fd=directory_fd)
    with _rotation_lock(path, directory_fd) as acquired:
        if not acquired:
            return
        with open(path, "a", encoding="utf-8", opener=opener) as fh:
            fh.write(line)
        _maybe_rotate(path, directory_fd)


def _open_nonblocking_regular(
    path: str, flags: int, *, directory_fd: int | None = None
) -> int:
    fd = os.open(path, flags | getattr(os, "O_NONBLOCK", 0), dir_fd=directory_fd)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(errno.EINVAL, "Diagnostic path must be a regular file", path)
    except OSError:
        os.close(fd)
        raise
    return fd


def log_path() -> Path:
    """Use the shared absolute session directory or the default account log."""
    directory = state.configured_state_dir()
    return (
        directory / "hook.log"
        if directory is not None and directory.is_absolute()
        else _LOG_PATH
    )


def _account_log_path() -> Path:
    """Return the signed-in account's real log, even when ``$HOME`` is redirected."""
    try:
        import pwd

        home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    except (ImportError, KeyError, AttributeError):  # pragma: no cover — Windows
        home = Path.home()
    return home / ".claude-smart" / "hook.log"


def _is_account_log(path: Path) -> bool:
    try:
        return path.expanduser().resolve() == _account_log_path().resolve()
    except (OSError, RuntimeError):
        return True


def _truncate(value: Any) -> Any:
    """Cap string values to ``_TRUNCATE_LIMIT`` chars so single-line writes
    stay below the POSIX atomic-append limit.

    Non-string values pass through unchanged; the json encoder handles
    them. ``None`` becomes ``null`` in the JSON, which is what we want.
    """
    if isinstance(value, str) and len(value) > _TRUNCATE_LIMIT:
        return value[:_TRUNCATE_LIMIT] + "…"
    return value


@contextlib.contextmanager
def _rotation_lock(path: Path, directory_fd: int | None) -> Iterator[bool]:
    """Hold an exclusive cross-process flock on a sibling ``.lock`` file.

    Serialises every ``log_event`` call against itself so the rotation
    ``read_bytes → write tmp → rename`` sequence cannot interleave with a
    concurrent append. On Windows or when the lock cannot be acquired
    (no ``fcntl``), the caller proceeds unlocked. On lock contention or an
    acquisition error, skip this best-effort record rather than waiting or
    racing a concurrent rotation. The durable session/outbox is independent.

    Args:
        path (Path): The log file path; the lock file is a sibling at
            ``<path>.lock``.

    Yields:
        Whether the caller may safely append and rotate.
    """
    if _fcntl is None:
        yield True
        return
    lock_path = path.with_name(path.name + ".lock")
    with contextlib.ExitStack() as opened:
        try:
            fh = opened.enter_context(
                open(
                    lock_path,
                    "ab",
                    opener=partial(
                        _open_nonblocking_regular, directory_fd=directory_fd
                    ),
                )
            )
            _fcntl.flock(fh.fileno(), _fcntl.LOCK_EX | _fcntl.LOCK_NB)
        except OSError as exc:
            _LOGGER.debug("hook_log: could not lock %s: %s", lock_path, exc)
            yield False
            return
        try:
            yield True
        finally:
            with contextlib.suppress(OSError):
                _fcntl.flock(fh.fileno(), _fcntl.LOCK_UN)


def _maybe_rotate(path: Path, directory_fd: int | None = None) -> None:
    """Cap ``path`` at ``_MAX_LOG_BYTES`` by dropping the oldest half in place.

    The fast path is a single ``stat`` syscall — when the file is under
    the cap (the overwhelmingly common case on every fire) we do nothing.
    When over the cap we read the file, find the first newline at-or-after
    the midpoint byte, and atomically replace the original with the bytes
    *after* that newline. Splitting on a newline boundary preserves the
    JSON Lines invariant: every retained line is a complete record.

    We picked this over ``logging.handlers.RotatingFileHandler`` because:

    1. ``hook_log`` deliberately avoids the ``logging`` framework — see
       module docstring. Adding a handler reintroduces the configuration
       fragility we were dodging.
    2. ``RotatingFileHandler`` produces ``hook.log.1``, ``hook.log.2`` …
       sidecar files; our forensic story is "one file, tail it". Users
       have already wired up tooling around that path.
    3. In-place truncate-to-second-half is O(n) on the rare rotation
       event and zero-cost on every other write. Backup file management
       would add steady-state inode pressure for no benefit.

    Args:
        path (Path): The log file to inspect. Need not exist — the
            ``FileNotFoundError`` branch of the ``stat`` call returns
            cleanly via the ``OSError`` guard.

    Returns:
        None
    """
    try:
        current_stat = os.stat(path, dir_fd=directory_fd)
    except OSError as exc:
        _LOGGER.debug("hook_log: stat for rotation failed: %s", exc)
        return

    if current_stat.st_size < _MAX_LOG_BYTES:
        return

    try:
        with open(
            path,
            "rb",
            opener=partial(_open_nonblocking_regular, directory_fd=directory_fd),
        ) as fh:
            data = fh.read()
    except OSError as exc:
        _LOGGER.debug("hook_log: read for rotation failed: %s", exc)
        return

    midpoint = len(data) // 2
    cut = data.find(b"\n", midpoint)
    if cut == len(data) - 1:
        # Keep the newest complete record even when it spans the midpoint.
        cut = data.rfind(b"\n", 0, cut)
        tail = data[cut + 1 :]
    else:
        tail = b"" if cut == -1 else data[cut + 1 :]
    # Unframed data has no complete JSONL record to preserve. A complete
    # newest record is retained above, even when it exceeds the cap.

    # Atomic replace: write to a sibling temp file then rename. This
    # avoids the truncation window where a concurrent reader would see
    # an empty file, and matches the contract that ``hook.log`` always
    # contains complete JSONL framing.
    tmp_path = path.with_name(f".hook-log-{secrets.token_hex(12)}")
    staged_created = False
    try:
        fd = os.open(
            tmp_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
            0o600,
            dir_fd=directory_fd,
        )
        staged_created = True
        with os.fdopen(fd, "wb") as staged:
            staged.write(tail)
            if os.name != "nt":
                mode = stat.S_IMODE(current_stat.st_mode)
                # Keep the replacement private if the original group or mode
                # cannot be restored. Rotation must still cap the log.
                with contextlib.suppress(OSError):
                    fd = staged.fileno()
                    if mode & 0o070 and os.fstat(fd).st_gid != current_stat.st_gid:
                        os.fchown(fd, -1, current_stat.st_gid)
                    os.fchmod(fd, mode)
        os.replace(tmp_path, path, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
    except OSError as exc:
        _LOGGER.debug("hook_log: rotation rename failed: %s", exc)
        if staged_created:
            with contextlib.suppress(OSError):
                os.unlink(tmp_path, dir_fd=directory_fd)
