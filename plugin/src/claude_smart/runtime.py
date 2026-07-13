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
HOST_OPENCODE = "opencode"
HOST_UNKNOWN = "unknown"
VALID_HOSTS = frozenset({HOST_CLAUDE_CODE, HOST_CODEX, HOST_OPENCODE})

_SHARED_AGENT_VERSION = "claude-code"
_current_host: str | None = None


def _resolve_host(value: str | None, fallback: str) -> str:
    return value if value in VALID_HOSTS else fallback


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
