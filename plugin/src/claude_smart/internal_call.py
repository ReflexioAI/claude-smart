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
  - ``CLAUDE_CODE_ENTRYPOINT`` is anything other than an interactive entrypoint
    (``"cli"`` or Claude Desktop's ``"claude-desktop"``). Headless
    ``claude -p`` sets ``sdk-cli`` (and the SDKs may set other values). This
    catches case (2) for any third-party tool, not just claude-mem.
  - Env var ``CLAUDE_SMART_INTERNAL=1``, set by reflexio's provider
    before spawning ``claude``. Belt-and-suspenders for case (1) in
    case the entrypoint check ever misses a future SDK variant.
  - ``payload.cwd`` resolves inside the side-by-side reflexio checkout. Catches
    direct interactive ``claude`` runs from inside reflexio during local
    debugging that would otherwise pollute the corpus.
  - Known Codex-internal prompt templates (title generation and home-screen
    suggestions). These are model calls made by Codex itself, not user
    coding turns, and must never be reflected into claude-smart memory.
  - Known orphan Codex-internal response bodies, for cases where the Stop
    payload contains only the generated metadata and not the internal prompt.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from claude_smart import runtime

_ENTRYPOINT_VAR = "CLAUDE_CODE_ENTRYPOINT"
_INTERACTIVE_ENTRYPOINTS = frozenset({"cli", "claude-desktop"})
_CODEX_TITLE_PROMPT_PREFIX = (
    "You are a helpful assistant. You will be presented with a user prompt, "
    "and your job is to provide a short title for a task"
)
_CODEX_SUGGESTIONS_PROMPT_PREFIX = "# Overview\n\nGenerate 0 to 3 "
_CODEX_SUGGESTIONS_PROMPT_MARKER = (
    "hyperpersonalized suggestions for what this user can do with Codex "
    "in this local project:"
)
_CODEX_SUGGESTIONS_APPS_MARKER = (
    "Get an understanding of the user's intent and goals by deeply viewing "
    "their connected apps."
)

# Reflexio usually lives next to claude-smart during local development:
# <workspace>/claude-smart and <workspace>/reflexio. Anchor relative to this
# file so the check follows the real checkout if the repo is relocated. In
# install mode that sibling checkout is absent — the env marker is the primary
# signal and this path never matches.
#
# The path computation is tightly coupled to the current layout: if this
# module moves, ``_REFLEXIO_DIR`` silently stops matching and only the
# env signal remains. ``CLAUDE_SMART_REFLEXIO_DIR`` lets callers (and
# tests) override the path without touching the module.
_THIS_DIR = Path(__file__).resolve().parent
_REFLEXIO_DIR = Path(
    os.environ.get("CLAUDE_SMART_REFLEXIO_DIR") or _THIS_DIR.parents[3] / "reflexio"
).resolve()


def is_internal_invocation(payload: dict[str, Any]) -> bool:
    """True if this hook fire originated from reflexio's own LLM provider.

    Args:
        payload (dict[str, Any]): Parsed Claude Code hook payload. Only
            ``cwd`` is inspected.

    Returns:
        bool: True when the env marker is set or ``cwd`` points inside
            the Reflexio checkout. False otherwise, including when
            ``cwd`` is missing or unresolvable.
    """
    if runtime.is_internal_invocation_env():
        return True
    entrypoint = os.environ.get(_ENTRYPOINT_VAR)
    if entrypoint and entrypoint not in _INTERACTIVE_ENTRYPOINTS:
        return True
    if runtime.is_codex() and is_codex_internal_prompt(payload.get("prompt")):
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


def is_codex_internal_prompt(prompt: Any) -> bool:
    """True for Codex's own UI/task prompts, not user-authored turns."""
    if not isinstance(prompt, str):
        return False
    text = prompt.strip()
    return text.startswith(_CODEX_TITLE_PROMPT_PREFIX) or (
        text.startswith(_CODEX_SUGGESTIONS_PROMPT_PREFIX)
        and _CODEX_SUGGESTIONS_PROMPT_MARKER in text
        and _CODEX_SUGGESTIONS_APPS_MARKER in text
    )


def is_codex_title_response(content: Any) -> bool:
    """True for Codex's title-generator response body.

    Codex can run a separate title-generation task whose Stop payload contains
    only the assistant response, e.g. ``{"title":"Fix tests"}``, with no
    corresponding user turn. That metadata is useful to Codex's UI, but it is
    not a user interaction and should not be published to reflexio.
    """
    if not isinstance(content, str):
        return False
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        return False
    return (
        isinstance(parsed, dict)
        and set(parsed) == {"title"}
        and isinstance(parsed.get("title"), str)
        and bool(parsed["title"].strip())
    )


def is_codex_suggestions_response(content: Any) -> bool:
    """True for Codex's home-screen suggestion-generator response body.

    Codex can run a separate suggestion task whose Stop payload contains
    generated JSON like ``{"suggestions": [...]}`` with no corresponding user
    turn. Those suggestions are UI metadata, not a user interaction.
    """
    if not isinstance(content, str):
        return False
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        return False
    if not isinstance(parsed, dict) or set(parsed) != {"suggestions"}:
        return False
    suggestions = parsed.get("suggestions")
    if not isinstance(suggestions, list):
        return False
    for item in suggestions:
        if not isinstance(item, dict):
            return False
        if set(item) != {"title", "description", "prompt", "appId"}:
            return False
        if not all(isinstance(item.get(key), str) for key in item):
            return False
    return True
