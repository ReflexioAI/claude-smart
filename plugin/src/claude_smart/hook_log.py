"""Structured, append-only log of every hook fire and its outcome.

Why a dedicated log rather than relying on the Python ``logging`` module:
hooks run as short-lived subprocesses with no preconfigured handler, so
``_LOGGER.warning(...)`` calls silently vanish to stderr that Claude Code
captures but typically does not surface. The dispatcher already wraps
handlers in a broad ``except`` (so a hook crash never breaks the user's
session) — without a forensic trail the silent-fail mode is invisible.

This module writes one JSON line per hook fire to
``~/.claude-smart/hook.log`` from ``hook.main`` after the handler returns.
The Stop and SessionEnd handlers additionally piggyback their publish
outcome into the record so the chain hook→adapter→backend is fully
observable from a single file.

Failure policy: every public function in this module swallows all
``OSError``/``Exception`` paths. Logging is best-effort; a stuck disk
or read-only ``$HOME`` must never abort an in-progress hook.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import time
from collections.abc import Iterator
from pathlib import Path
from typing import IO, Any

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
            calls, or invocations from inside the reflexio submodule).
        handler_status (str): ``"ok"`` for a clean return, ``"unknown_event"``
            when no handler is registered, or ``"raised:<ExcClass>: <msg>"``
            when the handler raised — formatted by ``hook.main``.
        publish_status (str | None): Status returned by
            ``publish.publish_unpublished`` (``"ok"`` / ``"failed"`` /
            ``"nothing"``). Only emitted by Stop + SessionEnd.
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

    try:
        line = json.dumps(record, ensure_ascii=False, default=str) + "\n"
    except (TypeError, ValueError) as exc:
        _LOGGER.debug("hook_log: json encode failed: %s", exc)
        return

    path = log_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        _LOGGER.debug("hook_log: mkdir for %s failed: %s", path.parent, exc)
        return

    # Hold an exclusive cross-process flock through the entire append +
    # rotate critical section. The original rationale ("POSIX atomic
    # append below PIPE_BUF") protects the *append* alone but not the
    # ``read_bytes → write tmp → rename`` rotation sequence: a concurrent
    # appender that lands between rotation's read and rename has its
    # record silently dropped by the rename. The lock closes that gap.
    #
    # On Windows ``_fcntl`` is None and writes proceed unlocked — same
    # behaviour as before. The original PIPE_BUF-based append atomicity
    # is preserved by ``_TRUNCATE_LIMIT`` keeping each record below 4 KiB.
    with _rotation_lock(path):
        try:
            with path.open("a", encoding="utf-8") as fh:
                fh.write(line)
        except OSError as exc:
            _LOGGER.debug("hook_log: write to %s failed: %s", path, exc)
            return

        # Rotate AFTER the append so the just-written record always survives,
        # even on the call that crosses the cap. A single oversized record
        # still gets through and is dropped by the *next* rotation when the
        # file is read back.
        _maybe_rotate(path)


def log_path() -> Path:
    """Return the active log path, honouring ``CLAUDE_SMART_HOOK_LOG``.

    Resolution order:

    1. ``CLAUDE_SMART_HOOK_LOG`` env var — boolean-like values enable the
       default log, while path-like values redirect it.
    2. ``_LOG_PATH`` module attribute — what monkeypatched tests override.
    3. ``~/.claude-smart/hook.log`` — the default.

    Returns:
        Path: The absolute path to write to.
    """
    override = os.environ.get("CLAUDE_SMART_HOOK_LOG")
    if override:
        override = override.strip()
        if not override:
            return _LOG_PATH
        if override.lower() in {"1", "true", "yes", "on"}:
            return _LOG_PATH
        return Path(override)
    return _LOG_PATH


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
def _rotation_lock(path: Path) -> Iterator[None]:
    """Hold an exclusive cross-process flock on a sibling ``.lock`` file.

    Serialises every ``log_event`` call against itself so the rotation
    ``read_bytes → write tmp → rename`` sequence cannot interleave with a
    concurrent append. On Windows or when the lock cannot be acquired
    (no ``fcntl``, read-only parent, etc.) the lock degrades to a no-op
    and the caller proceeds unlocked — matches the pre-existing
    best-effort policy of this module.

    Args:
        path (Path): The log file path; the lock file is a sibling at
            ``<path>.lock``.

    Yields:
        None.
    """
    if _fcntl is None:
        yield
        return
    lock_path = path.with_name(path.name + ".lock")
    fh: IO[bytes] | None = None
    try:
        fh = lock_path.open("ab")
    except OSError as exc:
        _LOGGER.debug("hook_log: lock open %s failed: %s", lock_path, exc)
        yield
        return
    try:
        _fcntl.flock(fh.fileno(), _fcntl.LOCK_EX)
    except OSError as exc:
        _LOGGER.debug("hook_log: flock acquire failed: %s", exc)
        fh.close()
        yield
        return
    try:
        yield
    finally:
        with contextlib.suppress(OSError):
            _fcntl.flock(fh.fileno(), _fcntl.LOCK_UN)
        with contextlib.suppress(OSError):
            fh.close()


def _maybe_rotate(path: Path) -> None:
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
        size = path.stat().st_size
    except OSError as exc:
        _LOGGER.debug("hook_log: stat for rotation failed: %s", exc)
        return

    if size < _MAX_LOG_BYTES:
        return

    try:
        data = path.read_bytes()
    except OSError as exc:
        _LOGGER.debug("hook_log: read for rotation failed: %s", exc)
        return

    midpoint = len(data) // 2
    cut = data.find(b"\n", midpoint)
    # ``find`` returns -1 when the second half has no newline at all,
    # meaning the file is one giant record with no framing left to
    # preserve. Drop the lot rather than split a record mid-byte.
    tail = b"" if cut == -1 else data[cut + 1 :]

    # Atomic replace: write to a sibling temp file then rename. This
    # avoids the truncation window where a concurrent reader would see
    # an empty file, and matches the contract that ``hook.log`` always
    # contains complete JSONL framing.
    tmp_path = path.with_name(path.name + ".rot")
    try:
        tmp_path.write_bytes(tail)
        tmp_path.replace(path)
    except OSError as exc:
        _LOGGER.debug("hook_log: rotation rename failed: %s", exc)
        # Best-effort cleanup of the temp file; ignore failures.
        with contextlib.suppress(OSError):
            tmp_path.unlink(missing_ok=True)
