"""SessionStart hook — apply startup defaults without broad memory retrieval."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

from claude_smart import hook
from claude_smart.reflexio_adapter import Adapter
from claude_smart.stall_banner import render_banner

# Claude-smart's preferred extraction cadence — more frequent, smaller windows
# than reflexio's out-of-box 10/5. Applied idempotently to the reflexio server
# on every SessionStart via Adapter.apply_extraction_defaults.
#
# Env-var overrides let benchmark harnesses (SWE-bench Verified, in particular)
# run with ``CLAUDE_SMART_STRIDE_SIZE=1`` so a single short autonomous session
# triggers extraction on its sole publish instead of being silently filtered
# out by the stride pre-filter (``new < stride_size``). Production users keep
# the defaults; the values are only read on SessionStart, so a launcher that
# sets the env var has predictable effect for the entire Claude Code session.
#
# Parsing is defensive: a non-integer env value falls back to the default
# rather than raising at import time. SessionStart is the hook that wires up
# every subsequent hook, so an import-time crash here would silently disable
# the whole plugin for the session.


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


_CLAUDE_SMART_WINDOW_SIZE = _int_env("CLAUDE_SMART_WINDOW_SIZE", 5)
_CLAUDE_SMART_STRIDE_SIZE = _int_env("CLAUDE_SMART_STRIDE_SIZE", 3)
# Optimizer is on by default. Set this env var to "0" to skip pushing the
# claude-smart optimizer defaults on SessionStart (kill switch).
_DISABLE_OPTIMIZER_ENV = "CLAUDE_SMART_ENABLE_OPTIMIZER"
_OPTIMIZER_TIMEOUT_SECONDS = 300


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
    session_id = payload.get("session_id")
    if not session_id:
        hook.emit_continue()
        return

    adapter = _adapter()

    # Stall banner — prepended to additionalContext, fires at most once per
    # stall event (controlled server-side via mark_stall_notified).
    banner = _stall_banner(adapter)

    adapter.apply_extraction_defaults(
        window_size=_CLAUDE_SMART_WINDOW_SIZE,
        stride_size=_CLAUDE_SMART_STRIDE_SIZE,
    )
    if os.environ.get(_DISABLE_OPTIMIZER_ENV) != "0":
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

    try:
        adapter.mark_stall_notified()
    except Exception:  # noqa: BLE001 — telemetry must not break session.
        pass


def _optimizer_assistant_path() -> str:
    executable = Path(sys.executable)
    suffix = ".exe" if os.name == "nt" else ""
    return str(executable.with_name(f"claude-smart-optimizer-assistant{suffix}"))
