"""Host/runtime state shared by claude-smart entrypoints.

The plugin intentionally keeps one memory namespace across supported hosts.
The host value is for payload quirks, attribution, and install UX; the Reflexio
agent version remains shared so every host sees the same learned rules.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

HOST_ENV = "CLAUDE_SMART_HOST"
INTERNAL_ENV = "CLAUDE_SMART_INTERNAL"

HOST_CLAUDE_CODE = "claude-code"
HOST_CODEX = "codex"
HOST_CURSOR = "cursor"
HOST_OPENCODE = "opencode"
HOST_UNKNOWN = "unknown"
VALID_HOSTS = frozenset(
    {HOST_CLAUDE_CODE, HOST_CODEX, HOST_CURSOR, HOST_OPENCODE}
)

_SHARED_AGENT_VERSION = "claude-code"
_current_host: str | None = None


def _resolve_host(value: str | None, fallback: str) -> str:
    return value if value in VALID_HOSTS else fallback


def resolve_hook_host(
    declared_host: str, payload: Mapping[str, Any]
) -> str:
    """Resolve the actual host behind a normalized hook invocation.

    Cursor loads the Claude Code plugin directly, so its hook command declares
    ``claude-code`` even though Cursor launched the subprocess. Positive Cursor
    signals may refine that declaration. Explicit Codex/OpenCode invocations
    and real Claude Code entrypoints always retain their declared host.

    Args:
        declared_host (str): Host selected by the hook command arguments.
        payload (Mapping[str, Any]): Normalized hook payload.

    Returns:
        str: The effective host used for local attribution and hook telemetry.
    """
    if declared_host != HOST_CLAUDE_CODE:
        return declared_host
    if os.environ.get("CLAUDE_CODE_ENTRYPOINT"):
        return declared_host
    if os.environ.get("CURSOR_TRANSCRIPT_PATH"):
        return HOST_CURSOR
    if os.environ.get("CURSOR_VERSION") and os.environ.get("CURSOR_PROJECT_DIR"):
        return HOST_CURSOR

    transcript_path = payload.get("transcript_path")
    if not isinstance(transcript_path, str) or not transcript_path:
        return declared_host
    try:
        cursor_root = (Path.home() / ".cursor").resolve()
        Path(transcript_path).expanduser().resolve().relative_to(cursor_root)
    except (OSError, RuntimeError, ValueError):
        return declared_host
    return HOST_CURSOR


def set_host(value: str | None) -> str:
    """Set the current host, returning the normalized value."""
    global _current_host
    _current_host = value if value is not None else HOST_UNKNOWN
    host = _resolve_host(_current_host, HOST_CLAUDE_CODE)
    os.environ[HOST_ENV] = host
    return host


def host() -> str:
    """Return the current host, defaulting to Claude Code for compatibility."""
    value = _current_host if _current_host is not None else os.environ.get(HOST_ENV)
    return _resolve_host(value, HOST_CLAUDE_CODE)


def attribution_host() -> str:
    """Return the explicitly selected record host, or unknown when unset."""
    value = _current_host if _current_host is not None else os.environ.get(HOST_ENV)
    return _resolve_host(value, HOST_UNKNOWN)


def is_codex() -> bool:
    """True when the current hook invocation came from Codex."""
    return host() == HOST_CODEX


def is_opencode() -> bool:
    """True when the current hook invocation came from OpenCode."""
    return host() == HOST_OPENCODE


def agent_version() -> str:
    """Reflexio agent version used for shared learning across hosts."""
    return _SHARED_AGENT_VERSION


def is_internal_invocation_env() -> bool:
    """Generic recursion guard used by local assistant subprocesses."""
    return os.environ.get(INTERNAL_ENV) == "1"
