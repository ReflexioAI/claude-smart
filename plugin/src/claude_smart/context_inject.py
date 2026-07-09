"""Shared UserPromptSubmit search/render/emit pipeline.

The hook runs a query-aware reflexio search, renders the hits with
``context_format.render_inline_with_registry``, persists the citation
registry for the Stop hook to resolve, and emits a Claude Code
``hookSpecificOutput.additionalContext`` envelope on stdout.
"""

from __future__ import annotations

import json
import os
import sys
import time

from claude_smart import context_format, state
from claude_smart.reflexio_adapter import Adapter


def emit_context(
    *,
    session_id: str,
    project_id: str,
    query: str,
    hook_event_name: str,
    top_k: int,
    adapter: Adapter | None = None,
) -> bool:
    """Search reflexio, render hits, emit ``additionalContext`` on stdout.

    Args:
        session_id (str): Claude Code session id; used to scope the
            per-session citation registry.
        project_id (str): reflexio ``user_id`` for this repo.
        query (str): Free-text query routed to reflexio's unified
            ``/api/search`` endpoint, which fans out to user playbooks
            (project-scoped), agent playbooks (global), and preferences
            (project-scoped) server-side.
        hook_event_name (str): ``"UserPromptSubmit"``;
            echoed verbatim in the hook envelope so Claude Code attributes
            the context to the right event.
        top_k (int): Cap on hits per collection.
        adapter (Adapter | None): Injection seam for tests. A fresh
            ``Adapter()`` is used when ``None``.

    Returns:
        bool: ``True`` when markdown was emitted to stdout; ``False``
            when the search returned nothing to inject.
    """
    user_playbooks, agent_playbooks, profiles = (adapter or Adapter()).search_all(
        project_id=project_id,
        query=query,
        top_k=top_k,
        # Scopes server-side dedup: rules already injected into this session
        # are not returned again; next-best matches backfill instead.
        session_id=session_id or None,
    )
    renderer = (
        context_format.render_inline_compact_with_registry
        if hook_event_name == "UserPromptSubmit"
        and os.environ.get("CLAUDE_SMART_HOST") == "codex"
        else context_format.render_inline_with_registry
    )
    markdown, registry = renderer(
        project_id=project_id,
        user_playbooks=user_playbooks,
        agent_playbooks=agent_playbooks,
        profiles=profiles,
    )
    if not markdown:
        return False

    state.append_injected(
        session_id,
        (dict(entry, ts=int(time.time())) for entry in registry),
    )

    sys.stdout.write(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": hook_event_name,
                    "additionalContext": markdown,
                }
            }
        )
    )
    sys.stdout.write("\n")
    return True


__all__ = ["emit_context"]
