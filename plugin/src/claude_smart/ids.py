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


def _git_lines(base: Path, *args: str) -> list[str] | None:
    """Return the output lines of ``git <args>`` run in ``base``, or None on failure."""
    result = subprocess.run(  # noqa: S603 — fixed argv, cwd is a Path.
        ["git", *args],  # noqa: S607
        cwd=base,
        capture_output=True,
        text=True,
        check=False,
        timeout=5,
    )
    if result.returncode != 0:
        return None
    return [line.strip() for line in result.stdout.strip().splitlines()]


def _linked_worktree_main_basename(base: Path) -> str | None:
    """Return the main worktree's basename when ``base`` is in a linked worktree.

    A linked worktree (``git worktree add``, used by Claude Code for
    ``.claude/worktrees/<name>`` sessions) has its own git dir under the main
    repo's common dir, so the two differ. Every other checkout (the main
    worktree, a submodule, a ``--separate-git-dir`` checkout) has equal git and
    common dirs and returns None, keeping its toplevel id. The main worktree is
    taken from ``git worktree list``, which always lists it first; a bare main
    repo has no checkout to name, so it returns None too.
    """
    dirs = _git_lines(base, "rev-parse", "--git-dir", "--git-common-dir")
    if not dirs or len(dirs) != 2:
        return None
    git_dir, common_dir = ((base / d).resolve() for d in dirs)
    if git_dir == common_dir:
        return None
    listing = _git_lines(base, "worktree", "list", "--porcelain")
    if (
        not listing
        or len(listing) < 2
        or not listing[0].startswith("worktree ")
        or listing[1] == "bare"
    ):
        return None
    return _basename(listing[0][len("worktree ") :])


def resolve_project_id(cwd: str | os.PathLike[str] | None = None) -> str:
    """Return a stable project identifier for the given working directory.

    Local and managed/API-key installs both use the git/project name as
    Reflexio's ``user_id``.

    Inside a linked worktree (for example ``<repo>/.claude/worktrees/<name>``)
    this is the basename of the main worktree, so worktree sessions share the
    project's learnings. Otherwise it is the basename of the git toplevel, so
    submodules keep their own id and `cd src/` maps to the same project. Falls
    back to the cwd basename when the directory is not inside a git repo.

    Args:
        cwd: Working directory to resolve. Defaults to ``os.getcwd()``.

    Returns:
        str: A non-empty identifier. Never raises.
    """
    base = Path(cwd) if cwd is not None else Path.cwd()
    try:
        main = _linked_worktree_main_basename(base)
        if main:
            return main
        toplevel = _git_lines(base, "rev-parse", "--show-toplevel")
        if toplevel and toplevel[0]:
            return _basename(toplevel[0])
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired) as exc:
        _LOGGER.debug("git toplevel resolution failed: %s", exc)
    return _basename(cwd) if cwd is not None else _basename(base)


def resolve_user_id(cwd: str | os.PathLike[str] | None = None) -> str:
    """Return the Reflexio ``user_id`` for the given working directory.

    Honors the ``REFLEXIO_USER_ID`` env var as an explicit override (e.g. so a
    user can point multiple projects at a single Reflexio identity), and
    otherwise falls back to :func:`resolve_project_id`. The env var is
    loaded from ``~/.claude-smart/.env`` before reading the process environment so
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
