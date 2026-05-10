"""SessionStart hook — inject the project playbook + session profile as additionalContext."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any

from claude_smart import context_format, cs_cite, hook, ids, state
from claude_smart.reflexio_adapter import Adapter

# Claude-smart's preferred extraction cadence — more frequent, smaller windows
# than reflexio's out-of-box 10/5. Applied idempotently to the reflexio server
# on every SessionStart via Adapter.apply_extraction_defaults.
_CLAUDE_SMART_WINDOW_SIZE = 5
_CLAUDE_SMART_STRIDE_SIZE = 3
_ENABLE_OPTIMIZER_ENV = "CLAUDE_SMART_ENABLE_OPTIMIZER"
_OPTIMIZER_TIMEOUT_SECONDS = 300


def handle(payload: dict[str, Any]) -> None:
    session_id = payload.get("session_id")
    cwd = payload.get("cwd") or None
    if not session_id:
        hook.emit_continue()
        return

    project_id = ids.resolve_project_id(cwd)
    adapter = Adapter()
    adapter.apply_extraction_defaults(
        window_size=_CLAUDE_SMART_WINDOW_SIZE,
        stride_size=_CLAUDE_SMART_STRIDE_SIZE,
    )
    if os.environ.get(_ENABLE_OPTIMIZER_ENV) == "1":
        adapter.apply_optimizer_defaults(
            script_path=_optimizer_assistant_path(),
            timeout_seconds=_OPTIMIZER_TIMEOUT_SECONDS,
        )
    playbooks, profiles = adapter.fetch_both(
        project_id=project_id,
        playbook_top_k=1,
        profile_top_k=1,
    )

    markdown, registry = context_format.render_with_registry(
        project_id=project_id,
        playbooks=playbooks,
        profiles=profiles,
    )
    if not markdown:
        hook.emit_continue()
        return

    cs_cite.ensure_installed()
    state.append_injected(
        session_id,
        (dict(entry, ts=int(time.time())) for entry in registry),
    )

    sys.stdout.write(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "SessionStart",
                    "additionalContext": markdown,
                }
            }
        )
    )
    sys.stdout.write("\n")


def _optimizer_assistant_path() -> str:
    executable = Path(sys.executable)
    suffix = ".exe" if os.name == "nt" else ""
    return str(executable.with_name(f"claude-smart-optimizer-assistant{suffix}"))
