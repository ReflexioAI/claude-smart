"""PreToolUse hook — just-in-time skill + preference inject before a mutating tool.

Fires only for tools listed in ``hooks.json``'s PreToolUse matcher
(Edit/Write/NotebookEdit/Bash). Composes a query from the tool call and
delegates to ``context_inject.emit_context`` for the shared
search-render-emit pipeline, falling back to ``hook.emit_continue`` when
there is nothing to inject or the search raises.
"""

from __future__ import annotations

import logging
from typing import Any

from claude_smart import context_inject, hook, ids, query_compose

_LOGGER = logging.getLogger(__name__)
_TOP_K = 3


def handle(payload: dict[str, Any]) -> None:
    """PreToolUse dispatcher — never raises; degrades to ``emit_continue``."""
    session_id = payload.get("session_id")
    tool_name = payload.get("tool_name")
    tool_input = payload.get("tool_input") or {}
    if not session_id or not tool_name:
        hook.emit_continue()
        return

    query = query_compose.from_tool_call(tool_name, tool_input)
    if not query:
        hook.emit_continue()
        return

    project_id = ids.resolve_project_id(payload.get("cwd"))
    try:
        emitted = context_inject.emit_context(
            session_id=session_id,
            project_id=project_id,
            query=query,
            hook_event_name="PreToolUse",
            top_k=_TOP_K,
        )
    except Exception as exc:  # noqa: BLE001 — never block a tool call.
        _LOGGER.debug("pre_tool context inject failed: %s", exc)
        emitted = False
    if not emitted:
        hook.emit_continue()
