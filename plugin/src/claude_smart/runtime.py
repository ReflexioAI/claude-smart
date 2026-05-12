"""Host/runtime state shared by claude-smart entrypoints.

The plugin can be loaded by Claude Code or Codex, but v1 intentionally keeps
one memory namespace. The host value is for payload quirks and install UX; the
Reflexio agent version remains shared so both hosts see the same learned rules.
"""

from __future__ import annotations

import os

HOST_ENV = "CLAUDE_SMART_HOST"
INTERNAL_ENV = "CLAUDE_SMART_INTERNAL"

HOST_CLAUDE_CODE = "claude-code"
HOST_CODEX = "codex"
VALID_HOSTS = frozenset({HOST_CLAUDE_CODE, HOST_CODEX})

_SHARED_AGENT_VERSION = "claude-code"
_current_host: str | None = None


def set_host(value: str | None) -> str:
    """Set the current host, returning the normalized value."""
    global _current_host
    host = value if value in VALID_HOSTS else HOST_CLAUDE_CODE
    _current_host = host
    os.environ[HOST_ENV] = host
    return host


def host() -> str:
    """Return the current host, defaulting to Claude Code for compatibility."""
    if _current_host is not None:
        return _current_host
    value = os.environ.get(HOST_ENV)
    return value if value in VALID_HOSTS else HOST_CLAUDE_CODE


def is_codex() -> bool:
    """True when the current hook invocation came from Codex."""
    return host() == HOST_CODEX


def agent_version() -> str:
    """Reflexio agent version used for shared learning across hosts."""
    return _SHARED_AGENT_VERSION


def is_internal_invocation_env() -> bool:
    """Generic recursion guard used by local assistant subprocesses."""
    return os.environ.get(INTERNAL_ENV) == "1"
