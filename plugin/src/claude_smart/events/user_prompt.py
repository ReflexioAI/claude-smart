"""UserPromptSubmit hook — buffer the user turn and inject matching context.

Two responsibilities, in order:

1. Buffer the prompt into the session JSONL (this is the sole source of
   "User" role turns downstream — Claude Code replays the rest of the
   transcript via tool events, not UserPromptSubmit).
2. Use the prompt text as a search query against reflexio's preferences +
   skills and emit the top hits as ``hookSpecificOutput.additionalContext``
   so Claude sees relevant rules before planning the response.

The PreToolUse hook does similar retrieval keyed to tool-call text; this
hook covers the gap where a prompt-only turn (e.g. a question answered
from context without edits) never fires PreToolUse and so would otherwise
see no injected context at all. The shared pipeline lives in
``context_inject.emit_context``.

Retrieval is best-effort: any failure from search (reflexio unreachable,
HTTP timeout, unexpected shape) is caught so the buffered-prompt
behaviour is always preserved. PreToolUse does not wrap — a tool-call
injection failure is invisible to the user, whereas a failed user-turn
would silently lose the prompt.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from claude_smart import context_inject, ids, state

_LOGGER = logging.getLogger(__name__)
_TOP_K = 3


def handle(payload: dict[str, Any]) -> None:
    """UserPromptSubmit dispatcher — buffers the prompt, then injects context.

    Args:
        payload (dict[str, Any]): The Claude Code hook payload. Expected keys
            ``session_id``, ``prompt``, and optionally ``cwd``.

    Returns:
        None: Side effects only — appends to the session buffer and may
            write a ``hookSpecificOutput`` JSON document to stdout.
    """
    session_id = payload.get("session_id")
    prompt = payload.get("prompt") or ""
    if not session_id or not prompt:
        return

    project_id = ids.resolve_project_id(payload.get("cwd"))
    state.append(
        session_id,
        {
            "ts": int(time.time()),
            "role": "User",
            "content": prompt,
            "user_id": project_id,
        },
    )

    try:
        context_inject.emit_context(
            session_id=session_id,
            project_id=project_id,
            query=prompt,
            hook_event_name="UserPromptSubmit",
            top_k=_TOP_K,
        )
    except Exception as exc:  # noqa: BLE001 — never break the user's turn
        _LOGGER.debug("user_prompt context inject failed: %s", exc)
