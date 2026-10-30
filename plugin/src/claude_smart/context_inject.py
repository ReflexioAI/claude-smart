"""Shared "search reflexio, render markdown, emit hookSpecificOutput" pipeline.

PreToolUse and UserPromptSubmit both (a) run a query-aware reflexio
search, (b) render the hits as an index with ``context_format.render_inline_with_registry``,
(c) persist the citation registry for the Stop hook to resolve, and
(d) emit a Claude Code ``hookSpecificOutput.additionalContext`` envelope
on stdout. This module owns that shared pipeline so the two hook
handlers keep exactly one source of truth for the injection contract —
the envelope shape, the registry schema, and the injected context append.

The caller remains responsible for handler-specific framing (PreToolUse
needs ``hook.emit_continue()`` on the empty path; UserPromptSubmit wraps
the search in ``try/except`` so a failed reflexio never breaks a user's
turn) — see the two call sites for the small policy differences.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time

from claude_smart import context_format, state
from claude_smart.reflexio_adapter import SEARCHED_ENTITY_TYPE_COUNT, Adapter

_LOGGER = logging.getLogger(__name__)

# Total results one hook firing may request, across all collections. Hooks
# fire on every prompt and mutating tool call, and server-side dedup
# backfills next-best matches each time, so the low-ranked tail is the least
# likely to apply. Enforced by asking the server for fewer results, never by
# dropping returned ones: the server marks every result it returns as served
# for the session, so a dropped result would stay hidden until a reset.
DEFAULT_MAX_INJECTED_ITEMS = 6
_MAX_ITEMS_ENV = "CLAUDE_SMART_MAX_INJECTED_ITEMS"
_THRESHOLD_ENV = "CLAUDE_SMART_SEARCH_THRESHOLD"


def max_injected_items() -> int | None:
    """Return the per-hook cap on injected items.

    Reads ``CLAUDE_SMART_MAX_INJECTED_ITEMS``: a positive integer sets the
    cap, ``0`` removes it, and an unset, empty or invalid value uses
    ``DEFAULT_MAX_INJECTED_ITEMS``. See ``per_type_top_k`` for how it is
    enforced.

    Returns:
        int | None: The cap, or ``None`` for no cap.
    """
    raw = os.environ.get(_MAX_ITEMS_ENV, "").strip()
    if not raw:
        return DEFAULT_MAX_INJECTED_ITEMS
    try:
        value = int(raw)
    except ValueError:
        _LOGGER.debug("ignoring invalid %s=%r", _MAX_ITEMS_ENV, raw)
        return DEFAULT_MAX_INJECTED_ITEMS
    if value < 0:
        return DEFAULT_MAX_INJECTED_ITEMS
    return value or None


def per_type_top_k(top_k: int, max_items: int | None) -> int:
    """Return the per-collection ``top_k`` that keeps a search within the cap.

    One search returns up to ``top_k`` results for each searched collection,
    so the total never exceeds ``max_items`` when ``top_k`` is
    ``max_items // collections``. The server requires at least one result per
    collection, so caps below the collection count act as one per collection.

    Args:
        top_k (int): The hook's own per-collection limit.
        max_items (int | None): Total cap, or ``None`` for none.

    Returns:
        int: The ``top_k`` to request, at least 1.
    """
    if max_items is None:
        return top_k
    return max(1, min(top_k, max_items // SEARCHED_ENTITY_TYPE_COUNT))


def search_threshold() -> float | None:
    """Return the configured search similarity threshold, if any.

    Reads ``CLAUDE_SMART_SEARCH_THRESHOLD``, a number from 0 to 1. Unset,
    empty or invalid means no threshold is sent and the server default applies.

    Returns:
        float | None: The threshold, or ``None`` to omit it.
    """
    raw = os.environ.get(_THRESHOLD_ENV, "").strip()
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError:
        value = -1.0
    if not 0.0 <= value <= 1.0:
        _LOGGER.debug("ignoring invalid %s=%r", _THRESHOLD_ENV, raw)
        return None
    return value


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
        hook_event_name (str): ``"PreToolUse"`` or ``"UserPromptSubmit"``;
            echoed verbatim in the hook envelope so Claude Code attributes
            the context to the right event.
        top_k (int): Cap on hits per collection; lowered so the total
            requested stays within ``max_injected_items`` (``per_type_top_k``).
        adapter (Adapter | None): Injection seam for tests. A fresh
            ``Adapter()`` is used when ``None``.

    Returns:
        bool: ``True`` when markdown was emitted to stdout; ``False``
            when the search returned nothing to inject.
    """
    adapter = adapter or Adapter()
    # Set by SessionStart after a compaction; claimed by exactly one search.
    reset_seen = bool(session_id) and state.take_search_reset(session_id)
    errors_before = len(getattr(adapter, "read_errors", ()))
    user_playbooks, agent_playbooks, profiles = adapter.search_all(
        project_id=project_id,
        query=query,
        top_k=per_type_top_k(top_k, max_injected_items()),
        # Scopes server-side dedup: rules already injected into this session
        # are not returned again; next-best matches backfill instead.
        session_id=session_id or None,
        request_id=state.reserve_publish_request_id(session_id) if session_id else None,
        threshold=search_threshold(),
        reset_session_seen=reset_seen,
    )
    if reset_seen and len(getattr(adapter, "read_errors", ())) > errors_before:
        # The search never reached the server; keep the reset for the next one.
        state.request_search_reset(session_id)
    host = os.environ.get("CLAUDE_SMART_HOST", "")
    renderer = (
        context_format.render_inline_compact_with_registry
        if hook_event_name == "UserPromptSubmit" and host == "codex"
        else context_format.render_inline_with_registry
    )
    markdown, registry = renderer(
        project_id=project_id,
        user_playbooks=user_playbooks,
        agent_playbooks=agent_playbooks,
        profiles=profiles,
        session_id=session_id,
        budget_chars=context_format.inline_budget_chars(host),
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


__all__ = [
    "emit_context",
    "max_injected_items",
    "per_type_top_k",
    "search_threshold",
]
