"""Support helpers for the ``cs-cite`` citation channel.

Context injected at SessionStart / PreToolUse tags each skill and
preference bullet with a rank-based id fingerprinted by the underlying
real id (``[cs:s1-1a2b]`` for the first skill whose
``user_playbook_id`` starts with ``1a2b``, ``[cs:p2-c3d4]`` for the
second preference). The injected instruction asks Claude to
end impactful replies with a call like::

    cs-cite p1-c3d4,s2-1a2b

via the Bash tool. The Stop hook later scans the session transcript for
those tool calls and resolves the ids against a per-session registry
persisted at ``~/.claude-smart/sessions/<session_id>.injected.jsonl``.

Why rank + fingerprint: rank alone resets at every injection, so a
later injection's ``s1`` would silently overwrite an earlier entry in
the append-only registry — if Claude cited ``s1`` across a turn
boundary, the resolver would pick the wrong skill. Appending the
first four alphanumeric chars of the real id makes the id stable
across injections in the common case (distinct real ids → distinct
fingerprints), so cross-injection collisions become rare.

This module holds:

- ``rank_id``: ``p{n}-{fp}`` / ``s{n}-{fp}`` tag for a given
  (kind, rank, real_id) tuple. Fingerprint is omitted when no real id
  is available. ``p`` is preference, ``s`` is skill.
- ``CITATION_CMD_RE``: regex matching a valid ``cs-cite`` command line.
- ``ensure_installed``: idempotent copy of ``plugin/bin/cs-cite`` to
  ``~/.claude-smart/bin/cs-cite`` with the executable bit set.
- ``CITATION_INSTRUCTION``: the trailer text appended to injected
  context so Claude knows when and how to call the tool.
"""

from __future__ import annotations

import logging
import re
import shutil
import stat as stat_
from pathlib import Path
from typing import Any

_LOGGER = logging.getLogger(__name__)

_THIS_DIR = Path(__file__).resolve().parent
_PLUGIN_ROOT = _THIS_DIR.parents[1]  # plugin/src/claude_smart/ -> plugin/
_SOURCE_SCRIPT = _PLUGIN_ROOT / "bin" / "cs-cite"
_INSTALL_DIR = Path.home() / ".claude-smart" / "bin"
INSTALL_PATH = _INSTALL_DIR / "cs-cite"

_FINGERPRINT_LEN = 4

# Match a bare `cs-cite <ids>` invocation. Ids are rank tokens of the
# form `p<N>` (preference) or `s<N>` (skill) with an optional
# `-<fp>` fingerprint (1-4 alphanumeric chars), optionally
# `cs:`-prefixed (since bullets render as `[cs:p1-ab12]` and the model
# often copies the tag verbatim). The `(?i:...)` inline flags make the
# prefix, kind letter, and fingerprint case-insensitive so `CS:P1-AB12`
# is accepted — matching the `re.IGNORECASE` used by the standalone
# `cs-cite` script. Tokens may be comma- and/or whitespace-separated.
# Chained commands (&&, |, ;) and extra trailing tokens remain rejected
# by the anchored `\s*$` terminator so accidental mentions don't
# register as citations.
_ID_TOKEN = r"(?i:cs:)?(?i:[ps])\d+(?:-(?i:[a-z0-9]){1,4})?"
_ID_SEP = r"[,\s]+"
CITATION_CMD_RE = re.compile(
    rf"^\s*(?:[^\s]*/)?cs-cite\s+({_ID_TOKEN}(?:{_ID_SEP}{_ID_TOKEN})*)\s*$"
)
_CLEAN_ID_RE = re.compile(r"^(?i:cs:)?((?i:[ps])\d+(?:-(?i:[a-z0-9]){1,4})?)$")
_SPLIT_RE = re.compile(_ID_SEP)

CITATION_INSTRUCTION = (
    "_First, fully answer the user — citation does not change what or how "
    "you reply. Then, as a final step, consider whether to cite: if — and "
    "only if — an injected `[cs:…]` item materially changed your reply "
    "(different wording, action, or conclusion than you would have produced "
    "without it), call `cs-cite <id>` via the Bash tool. Ids come verbatim "
    "from the `[cs:…]` tags — keep the leading `p` (preference) or `s` "
    "(skill) and the `-<fp>` suffix, e.g. `cs-cite s1-ab12`. List "
    "multiple ids only when each shaped a different part of the answer, "
    "e.g. `cs-cite s1-ab12,p2-cd34`. Ids only, no prose, one Bash call. "
    "Default is to skip. If an item is merely on-topic, confirms what you "
    "already planned, or your reply would read the same without it, do not "
    "cite — end the turn normally with your reply. When unsure, skip. "
    "The `cs-cite` Bash call produces no stdout output. After it returns, "
    "emit exactly one short line as the final content of your assistant "
    "message, then stop: `✨ N claude-smart learning applied` when N=1, or "
    "`✨ N claude-smart learnings applied` when N>1, where N is the count "
    "of ids you passed. Do not add any other text, tool calls, or role "
    "markers after that line._"
)


def _fingerprint(real_id: Any) -> str:
    """Return the first ``_FINGERPRINT_LEN`` alphanumeric chars of ``real_id``.

    The fingerprint disambiguates rank ids across injections: two
    injections both producing ``s1`` for different skills will still
    yield distinct tags when their real ids have different prefixes.

    Args:
        real_id: The underlying ``user_playbook_id`` or ``profile_id``.
            Accepts anything ``str()`` handles (int, UUID, etc.).
            ``None`` yields an empty string.

    Returns:
        str: Up to 4 lowercase alphanumeric chars; empty when the real
            id has no alphanumeric characters or is ``None``.
    """
    if real_id is None:
        return ""
    return "".join(c for c in str(real_id).lower() if c.isalnum())[:_FINGERPRINT_LEN]


def rank_id(kind: str, rank: int, real_id: Any = None) -> str:
    """Return the citation id for a skill or preference item.

    Format is ``{letter}{rank}-{fingerprint}`` where ``letter`` is ``p``
    for preferences and ``s`` for skills, ``rank`` is the 1-based
    position within the current retrieval batch, and ``fingerprint`` is
    up to 4 alphanumeric chars derived from ``real_id``. The fingerprint
    is omitted when no real id is available (falling back to the rank
    form ``s1`` / ``p1``).

    Args:
        kind: ``"playbook"`` or ``"profile"``. Unknown values raise
            ``ValueError`` — callers never build registry entries for
            other kinds.
        rank: 1-based position within the retrieval batch.
        real_id: The underlying ``user_playbook_id`` or ``profile_id``
            used to derive the fingerprint suffix. Optional.

    Returns:
        str: ``p<rank>-<fp>`` for preferences, ``s<rank>-<fp>`` for
            skills. Suffix is omitted when the real id yields no
            alphanumeric fingerprint.

    Raises:
        ValueError: If ``kind`` is not ``"profile"`` or ``"playbook"``.
    """
    if kind == "profile":
        prefix = "p"
    elif kind == "playbook":
        prefix = "s"
    else:
        raise ValueError(f"unknown citation kind: {kind!r}")
    fp = _fingerprint(real_id)
    return f"{prefix}{rank}-{fp}" if fp else f"{prefix}{rank}"


def parse_citation_command(command: str) -> list[str]:
    """Extract citation ids from a ``cs-cite`` Bash command string.

    Returns an empty list when the command does not match the expected
    shape (chained commands, extra arguments, or anything other than a
    bare ``cs-cite <ids>`` invocation are rejected to avoid false
    positives from accidental mentions).

    Args:
        command: The raw ``input.command`` value from a Bash tool_use
            block.

    Returns:
        list[str]: Lowercase rank ids (e.g. ``"p1"``, ``"s3"``), in the
            order Claude cited them. Empty when the command does not
            match.
    """
    match = CITATION_CMD_RE.match(command or "")
    if not match:
        return []
    ids: list[str] = []
    for tok in _SPLIT_RE.split(match.group(1).strip()):
        if clean := _CLEAN_ID_RE.match(tok):
            ids.append(clean.group(1).lower())
    return ids


def ensure_installed() -> Path:
    """Idempotently install ``cs-cite`` into ``~/.claude-smart/bin/``.

    Called from SessionStart and from every PreToolUse / UserPromptSubmit
    inject, so we short-circuit when the target file already exists with
    the executable bit set — the steady-state path is one ``stat`` syscall
    instead of mkdir + copy + stat + chmod. Keying on filesystem state
    (rather than a module-level boolean) keeps test isolation working when
    tests monkeypatch ``INSTALL_PATH`` to a fresh tmpdir.

    Never raises — filesystem errors are logged at DEBUG and the caller
    proceeds with injection regardless (the citation feature degrades to
    silent if the script is unreachable).

    Returns:
        Path: Target path, whether or not install succeeded.
    """
    try:
        if INSTALL_PATH.is_file() and INSTALL_PATH.stat().st_mode & stat_.S_IXUSR:
            return INSTALL_PATH
        _INSTALL_DIR.mkdir(parents=True, exist_ok=True)
        if _SOURCE_SCRIPT.is_file():
            shutil.copy2(_SOURCE_SCRIPT, INSTALL_PATH)
            mode = INSTALL_PATH.stat().st_mode
            INSTALL_PATH.chmod(mode | stat_.S_IXUSR | stat_.S_IXGRP | stat_.S_IXOTH)
    except OSError as exc:
        _LOGGER.debug("cs-cite install failed: %s", exc)
    return INSTALL_PATH
