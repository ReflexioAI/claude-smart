"""Detect hook invocations that should not be published to reflexio.

Two distinct sources of unwanted hook fires:

1. **Reflexio's own LLM provider.** The ``claude-code`` LiteLLM provider
   (see ``reflexio.server.llm.providers.claude_code_provider._run_cli``)
   shells out to the ``claude`` CLI to answer extractor prompts. That
   subprocess is a full Claude Code invocation, so it fires *our* hooks
   too — and without a guard, the Stop hook publishes the extractor's
   own system prompt back into reflexio as a user interaction.
   Reflexio then trains on its own internals.

2. **Other tools' headless ``claude -p`` subprocesses.** Third-party
   plugins (e.g. claude-mem) spawn their own ``claude -p`` sessions
   for memory/observation extraction. Those sessions also fire the
   user's globally-installed claude-smart hooks, and the system prompt
   passed to ``-p`` shows up in the transcript as a user message —
   leaking text like ``"You are a Claude-Mem, a specialized observer
   tool..."`` into reflexio as a fake user interaction.

Detection signals, OR'd:
  - ``CLAUDE_CODE_ENTRYPOINT`` is anything other than ``"cli"`` —
    the interactive REPL sets ``cli``; headless ``claude -p`` sets
    ``sdk-cli`` (and the SDKs may set other values). This catches
    case (2) for any third-party tool, not just claude-mem.
  - Env var ``CLAUDE_SMART_INTERNAL=1``, set by reflexio's provider
    before spawning ``claude``. Belt-and-suspenders for case (1) in
    case the entrypoint check ever misses a future SDK variant.
  - ``payload.cwd`` resolves inside the reflexio submodule. Catches
    direct interactive ``claude`` runs from inside reflexio (manual
    debugging) that would otherwise pollute the corpus.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from claude_smart import runtime

_ENTRYPOINT_VAR = "CLAUDE_CODE_ENTRYPOINT"
_INTERACTIVE_ENTRYPOINT = "cli"

# Reflexio submodule lives at <repo>/reflexio when this package runs from
# a dev checkout (<repo>/plugin/src/claude_smart/internal_call.py); anchor
# relative to this file so the check follows the real checkout if the
# repo is relocated. In install mode the submodule is absent — the env
# marker is the primary signal and this path never matches.
#
# The path computation is tightly coupled to the current layout: if this
# module moves, ``_REFLEXIO_DIR`` silently stops matching and only the
# env signal remains. ``CLAUDE_SMART_REFLEXIO_DIR`` lets callers (and
# tests) override the path without touching the module.
_THIS_DIR = Path(__file__).resolve().parent
_REFLEXIO_DIR = Path(
    os.environ.get("CLAUDE_SMART_REFLEXIO_DIR") or _THIS_DIR.parents[2] / "reflexio"
)


def is_internal_invocation(payload: dict[str, Any]) -> bool:
    """True if this hook fire originated from reflexio's own LLM provider.

    Args:
        payload (dict[str, Any]): Parsed Claude Code hook payload. Only
            ``cwd`` is inspected.

    Returns:
        bool: True when the env marker is set or ``cwd`` points inside
            the reflexio submodule. False otherwise, including when
            ``cwd`` is missing or unresolvable.
    """
    if runtime.is_internal_invocation_env():
        return True
    entrypoint = os.environ.get(_ENTRYPOINT_VAR)
    if entrypoint and entrypoint != _INTERACTIVE_ENTRYPOINT:
        return True
    cwd = payload.get("cwd")
    if not isinstance(cwd, str) or not cwd:
        return False
    try:
        resolved = Path(cwd).resolve()
    except OSError:
        return False
    try:
        resolved.relative_to(_REFLEXIO_DIR)
    except ValueError:
        return False
    return True
