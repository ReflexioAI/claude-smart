"""Dispatch table for claude-smart hook events.

The plugin's ``hook_entry.sh`` calls either
``python -m claude_smart.hook <event>`` (legacy Claude Code shape) or
``python -m claude_smart.hook <host> <event>`` once per hook invocation.
This module reads the hook JSON from stdin, routes to the matching handler,
and makes sure no unhandled exception ever propagates.
"""

from __future__ import annotations

import json
import logging
import sys
from typing import Any, Callable

from claude_smart import runtime
from claude_smart.internal_call import is_internal_invocation

_LOGGER = logging.getLogger(__name__)


def _load_handlers() -> dict[str, Callable[[dict[str, Any]], None]]:
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
        emit_continue()
        return 0

    handlers = _load_handlers()
    handler = handlers.get(event)
    if handler is None:
        _LOGGER.warning("unknown hook event: %s", event)
        emit_continue()
        return 0

    try:
        handler(payload)
    except Exception as exc:  # noqa: BLE001 — hooks must never crash the session.
        _LOGGER.exception("hook handler %s raised: %s", event, exc)
        emit_continue()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
