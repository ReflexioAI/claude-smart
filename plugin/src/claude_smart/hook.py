"""Dispatch table for claude-smart hook events.

The plugin's ``hook_entry.sh`` calls either
``python -m claude_smart.hook <event>`` (legacy Claude Code shape) or
``python -m claude_smart.hook <host> <event>`` once per hook invocation.
This module reads the hook JSON from stdin, routes to the matching handler,
and makes sure no unhandled exception ever propagates.

Every fire produces one structured JSON line in
``~/.claude-smart/hook.log`` via ``hook_log.log_event`` — covers the
event name, session id, internal-invocation skip, handler outcome
(``ok`` / ``raised:<Exc>`` / ``unknown_event``), and for Stop/SessionEnd
the publish status + count. The log is the forensic trail for chasing
"hook didn't publish" mysteries from a single file.
"""

from __future__ import annotations

import json
import logging
import sys
from collections.abc import Callable
from contextlib import redirect_stdout
from io import StringIO
from typing import Any

from claude_smart import hook_log, ids, runtime
from claude_smart.internal_call import is_internal_invocation

_LOGGER = logging.getLogger(__name__)


# Stop and SessionEnd return ``(PublishStatus, int)`` so the dispatcher can
# log the publish outcome; the other handlers return ``None`` (or nothing).
# Use ``Any`` here rather than two specialised typedefs — the dispatcher
# narrows at the call site via ``isinstance(result, tuple)``.
def _load_handlers() -> dict[str, Callable[[dict[str, Any]], Any]]:
    from claude_smart.events import (
        post_tool,
        pre_tool,
        session_end,
        session_start,
        stop,
        user_prompt,
    )

    return {
        "session-start": session_start.handle,
        "user-prompt": user_prompt.handle,
        "pre-tool": pre_tool.handle,
        "post-tool": post_tool.handle,
        "stop": stop.handle,
        "session-end": session_end.handle,
    }


def _read_stdin_json() -> dict[str, Any]:
    """Parse stdin as JSON. Returns {} on empty or malformed input."""
    try:
        raw = sys.stdin.read()
    except Exception as exc:  # noqa: BLE001
        _LOGGER.debug("stdin read failed: %s", exc)
        return {}
    if not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        _LOGGER.debug("stdin JSON decode failed: %s", exc)
        return {}
    return parsed if isinstance(parsed, dict) else {}


def emit_continue() -> None:
    """Fallback stdout — tells Claude Code to keep going without injection."""
    payload = {"continue": True}
    if not runtime.is_codex():
        payload["suppressOutput"] = True
    sys.stdout.write(json.dumps(payload))
    sys.stdout.write("\n")


def _parse_args(argv: list[str]) -> tuple[str, str]:
    """Return ``(host, event)`` for old and new hook argv shapes."""
    if not argv:
        return runtime.HOST_CLAUDE_CODE, ""
    if len(argv) >= 2 and argv[0] in runtime.VALID_HOSTS:
        return argv[0], argv[1]
    return runtime.HOST_CLAUDE_CODE, argv[0]


def main(argv: list[str] | None = None) -> int:
    """Entry point used by ``python -m claude_smart.hook`` and the console script."""
    argv = argv if argv is not None else sys.argv[1:]
    host, event = _parse_args(argv)
    runtime.set_host(host)
    if not event:
        _LOGGER.warning("hook dispatcher called with no event name")
        emit_continue()
        return 0

    payload = _read_stdin_json()

    # Self-feedback guard: when this hook fires inside reflexio's own
    # `claude -p` subprocess (the claude-code LLM provider), skip all
    # handlers so we don't publish the extractor's system prompt back
    # into reflexio. See claude_smart.internal_call for detection logic.
    if is_internal_invocation(payload):
        hook_log.log_event(
            event=event,
            host=host,
            session_id=payload.get("session_id"),
            project_id=ids.resolve_project_id(payload.get("cwd")),
            cwd=payload.get("cwd"),
            internal_skipped=True,
            handler_status="ok",
        )
        emit_continue()
        return 0

    handlers = _load_handlers()
    handler = handlers.get(event)
    if handler is None:
        _LOGGER.warning("unknown hook event: %s", event)
        hook_log.log_event(
            event=event,
            host=host,
            session_id=payload.get("session_id"),
            project_id=ids.resolve_project_id(payload.get("cwd")),
            cwd=payload.get("cwd"),
            handler_status="unknown_event",
        )
        emit_continue()
        return 0

    handler_status = "ok"
    publish_status: str | None = None
    publish_count: int | None = None
    handler_stdout = StringIO()
    try:
        with redirect_stdout(handler_stdout):
            result = handler(payload)
        if isinstance(result, tuple) and len(result) == 2:
            publish_status, publish_count = result
    except Exception as exc:  # noqa: BLE001 — hooks must never crash the session.
        _LOGGER.exception("hook handler %s raised: %s", event, exc)
        handler_status = f"raised:{type(exc).__name__}: {exc}"
        emit_continue()
    else:
        # Handlers that inject context already emit the full hook JSON. Silent
        # handlers still need the continue response so the parent session does
        # not wait on an empty stdout payload.
        captured = handler_stdout.getvalue()
        if captured.strip():
            sys.stdout.write(captured)
        else:
            emit_continue()
    hook_log.log_event(
        event=event,
        host=host,
        session_id=payload.get("session_id"),
        project_id=ids.resolve_project_id(payload.get("cwd")),
        cwd=payload.get("cwd"),
        handler_status=handler_status,
        publish_status=publish_status,
        publish_count=publish_count,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
