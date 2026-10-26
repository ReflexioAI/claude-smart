"""SessionStart hook — show learning stalls without broad memory retrieval."""

from __future__ import annotations

import json
import os
import sys
from contextlib import suppress
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from claude_smart import hook, publish_outbox, runtime
from claude_smart.reflexio_adapter import Adapter
from claude_smart.stall_banner import render_banner

# Optimizer defaults are local-only by default because they store an absolute
# path to the claude-smart helper process. Set this env var to "1" to force
# enabling against a non-local Reflexio URL, or "0" to disable everywhere.
_OPTIMIZER_ENV = "CLAUDE_SMART_ENABLE_OPTIMIZER"
_OPTIMIZER_TIMEOUT_SECONDS = 300
_LOCAL_REFLEXIO_HOSTS = {"", "localhost", "127.0.0.1", "::1"}


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
    if runtime.is_codex():
        publish_outbox.kick()
    session_id = payload.get("session_id")
    if not session_id:
        hook.emit_continue()
        return

    adapter = _adapter()

    # Stall banner — prepended to additionalContext, fires at most once per
    # stall event (controlled server-side via mark_stall_notified).
    banner = _stall_banner(adapter)

    if _should_apply_optimizer_defaults(adapter):
        adapter.apply_optimizer_defaults(
            script_path=_optimizer_assistant_path(),
            timeout_seconds=_OPTIMIZER_TIMEOUT_SECONDS,
        )

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


def _optimizer_assistant_path() -> str:
    executable = Path(sys.executable)
    suffix = ".exe" if os.name == "nt" else ""
    return str(executable.with_name(f"claude-smart-optimizer-assistant{suffix}"))


def _should_apply_optimizer_defaults(adapter: Any) -> bool:
    flag = os.environ.get(_OPTIMIZER_ENV, "").strip().lower()
    if flag in {"0", "false", "no", "off"}:
        return False
    if flag in {"1", "true", "yes", "on"}:
        return True
    return _is_local_reflexio_url(getattr(adapter, "url", ""))


def _is_local_reflexio_url(url: str) -> bool:
    if not url:
        return True
    parsed = urlparse(url)
    host = parsed.hostname if parsed.scheme else url.split(":", 1)[0]
    return (host or "").lower() in _LOCAL_REFLEXIO_HOSTS
