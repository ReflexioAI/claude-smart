"""Resolve stable identifiers for Claude Code sessions.

Two identifiers matter to reflexio:

- ``session_id``: Claude Code's per-session id, passed in hook stdin. We
  forward it to reflexio's interaction ``session_id`` field so individual
  turns remain attributable to their conversation, but it is no longer
  the scope key for extracted preferences.
- ``project_id``: a stable, cross-session name for the project. We use
  this as reflexio's ``user_id`` for preferences, so user preferences
  extracted in one session are visible to every later session in the
  same repo. ``agent_version`` is hardcoded to ``"claude-code"`` in the
  adapter so shared skills roll up globally across all projects rather than
  per project.
"""

from __future__ import annotations

import logging
import os
import subprocess  # noqa: S404 — git invocation with a fixed flag set.
from pathlib import Path, PureWindowsPath

from claude_smart import env_config

_LOGGER = logging.getLogger(__name__)

_USER_ID_OVERRIDE_ENV = "REFLEXIO_USER_ID"


def _basename(path: str | os.PathLike[str]) -> str:
    """Return a cwd basename, including native Windows paths under Git Bash."""
    text = os.fspath(path)
    if "\\" in text or (len(text) >= 2 and text[0].isalpha() and text[1] == ":"):
        return PureWindowsPath(text).name or "unknown-project"
    return Path(path).name or "unknown-project"


def resolve_project_id(cwd: str | os.PathLike[str] | None = None) -> str:
    """Return a stable project identifier for the given working directory.

    Local and managed/API-key installs both use the git/project name as
    Reflexio's ``user_id``.

    Prefers the basename of the git toplevel (so worktrees, submodules, and
    `cd src/` all still map to the same project). Falls back to the cwd
    basename when the directory is not inside a git repo.

    Args:
        cwd: Working directory to resolve. Defaults to ``os.getcwd()``.

    Returns:
        str: A non-empty identifier. Never raises.
    """
    base = Path(cwd) if cwd is not None else Path.cwd()
    try:
        result = subprocess.run(  # noqa: S603, S607 — fixed argv, cwd is a Path.
            ["git", "rev-parse", "--show-toplevel"],  # noqa: S607
            cwd=base,
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
        if result.returncode == 0:
            toplevel = result.stdout.strip()
            if toplevel:
                return _basename(toplevel)
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired) as exc:
        _LOGGER.debug("git toplevel resolution failed: %s", exc)
    return _basename(cwd) if cwd is not None else _basename(base)


def resolve_user_id(cwd: str | os.PathLike[str] | None = None) -> str:
    """Return the Reflexio ``user_id`` for the given working directory.

    Honors the ``REFLEXIO_USER_ID`` env var as an explicit override (e.g. so a
    user can point multiple projects at a single Reflexio identity), and
    otherwise falls back to :func:`resolve_project_id`. The env var is
    loaded from ``~/.reflexio/.env`` before reading the process environment so
    CLI commands and hooks use the same identity resolution.

    Args:
        cwd: Working directory to resolve when falling back to the project id.

    Returns:
        str: The override value if set and non-empty, else the project id.
    """
    env_config.load_reflexio_env()
    override = os.environ.get(_USER_ID_OVERRIDE_ENV, "").strip()
    if override:
        return override
    return resolve_project_id(cwd)
