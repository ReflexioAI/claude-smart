"""SessionStart hook — show learning stalls without broad memory retrieval."""

from __future__ import annotations

import json
import sys
from contextlib import suppress
from typing import Any

from claude_smart import hook, publish_outbox, runtime, state
from claude_smart.reflexio_adapter import Adapter
from claude_smart.stall_banner import render_banner


def _adapter() -> Adapter:
    """Construct the reflexio adapter for this hook invocation.

    Indirected through a factory so tests can monkeypatch the adapter
    construction without touching the ``Adapter`` class itself.

    Returns:
        Adapter: A fresh adapter bound to the current process env.
    """
    return Adapter()


def _stall_banner(adapter: Any) -> str:
    """Return the prepend-able stall banner, or "" if no banner should fire.

    Reads ``adapter.fetch_stall_state()``; if it reports an active, not-yet-
    notified stall, renders a one-line banner via ``stall_banner.render_banner``.
    All exceptions are absorbed: this is defense-in-depth — even though the
    hook dispatcher already wraps ``handle`` in try/except, a stall-path bug
    must never block the existing playbook/profile rendering.

    Args:
        adapter (Any): The adapter to query. Duck-typed so tests can stub.

    Returns:
        str: The banner text, or ``""`` when there is nothing to show.
    """
    try:
        state_obj = adapter.fetch_stall_state()
    except Exception:  # noqa: BLE001 — stall path must never crash the hook.
        return ""
    if state_obj is None:
        return ""
    if not getattr(state_obj, "stalled", False):
        return ""
    if getattr(state_obj, "notified_in_cc", False):
        return ""
    try:
        return render_banner(
            reason=getattr(state_obj, "reason", None),
            reset_estimate=getattr(state_obj, "reset_estimate", None),
        )
    except Exception:  # noqa: BLE001 — render_banner bug must not block playbook injection.
        return ""


def handle(payload: dict[str, Any]) -> None:
    """SessionStart dispatcher: stall banner, plus a reset after compaction.

    Args:
        payload (dict[str, Any]): Host hook payload; ``source`` is
            ``"compact"`` when Claude Code or Codex just compacted the chat.
    """
    if runtime.is_codex():
        publish_outbox.kick()
    session_id = payload.get("session_id")
    if not session_id:
        hook.emit_continue()
        return

    if payload.get("source") == "compact":
        # Compaction keeps the session id, so the server's per-session dedup
        # would keep withholding rules that just left the model's context.
        with suppress(Exception):
            state.request_search_reset(session_id)

    adapter = _adapter()

    # Stall banner — prepended to additionalContext, fires at most once per
    # stall event (controlled server-side via mark_stall_notified).
    banner = _stall_banner(adapter)

    if not banner:
        hook.emit_continue()
        return

    sys.stdout.write(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "SessionStart",
                    "additionalContext": banner,
                }
            }
        )
    )
    sys.stdout.write("\n")

    with suppress(Exception):
        adapter.mark_stall_notified()
