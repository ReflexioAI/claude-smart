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

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

_LOGGER = logging.getLogger(__name__)

# Anchored on $HOME so unit tests can monkeypatch ``_LOG_PATH`` to a
# tmp_path. The directory is created lazily on first write — keeping
# import side-effect-free matches the rest of the package.
_LOG_PATH = Path.home() / ".claude-smart" / "hook.log"

_TRUNCATE_LIMIT = 4096


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
        record["publish_count"] = int(publish_count or 0)
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
        # Open mode "a" is atomic on POSIX for writes <= PIPE_BUF (4 KiB);
        # we truncate fields above to stay below that and avoid a heavier
        # ``flock`` dance that the hook fire path can't afford.
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line)
    except OSError as exc:
        _LOGGER.debug("hook_log: write to %s failed: %s", path, exc)


def log_path() -> Path:
    """Return the active log path, honouring ``CLAUDE_SMART_HOOK_LOG``.

    Resolution order:

    1. ``CLAUDE_SMART_HOOK_LOG`` env var — escape hatch for tests and
       advanced users who want the log elsewhere.
    2. ``_LOG_PATH`` module attribute — what monkeypatched tests override.
    3. ``~/.claude-smart/hook.log`` — the default.

    Returns:
        Path: The absolute path to write to.
    """
    override = os.environ.get("CLAUDE_SMART_HOOK_LOG")
    if override:
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
