"""Support helpers for claude-smart citation tracking.

Context injected by UserPromptSubmit / PreToolUse tags each skill and
preference bullet with a rank-based id fingerprinted by the underlying
real id (``[cs:s1-1a2b]`` for the first skill whose
``user_playbook_id`` starts with ``1a2b``, ``[cs:p2-c3d4]`` for the
second preference). The injected instruction asks the assistant to end
impactful replies with a marker like::

    ✨ claude-smart rule applied: [git safety](http://localhost:3001/rules/s1-123)

The Stop hook later scans the assistant text for those markers and resolves
the ids against a per-session registry persisted at
``~/.claude-smart/sessions/<session_id>.injected.jsonl``.

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
- ``citation_instruction(mode)``: the trailer text appended to injected
  context so the assistant knows when and how to emit the citation marker.
  ``mode`` is read from the ``CLAUDE_SMART_CITATIONS`` env var by the
  caller; ``"off"`` disables the instruction, while all other values enable
  the compact marker instruction for backward compatibility.
- ``CITATION_INSTRUCTION``: the compact enabled instruction string, kept as
  a module-level constant for backward-compatible imports.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import unquote, urlparse

_FINGERPRINT_LEN = 4

_ID_TOKEN = r"(?i:cs:)?(?i:[ps])\d+(?:-(?i:[a-z0-9]){1,4})?"  # noqa: S105
_ID_SEP = r"[,\s]+"
_CLEAN_ID_RE = re.compile(r"^(?i:cs:)?((?i:[ps])\d+(?:-(?i:[a-z0-9]){1,4})?)$")
_SPLIT_RE = re.compile(_ID_SEP)
_TEXT_CITATION_LINE_RE = re.compile(
    r"(?im)^\s*✨\s+\d+\s+claude-smart learning(?:s)? applied\s+"
    r"\[cs:(?P<ids>[^\]]+)\]\s*$"
)
_APPLIED_LINK_LINE_RE = re.compile(
    r"(?im)^\s*✨\s+(?:Applied|claude-smart rules? applied):\s+(?P<body>.+?)\s*$"
)
_MARKDOWN_LINK_RE = re.compile(r"\[[^\]]+\]\((?P<url>[^)]+)\)")
_OSC8_URL_RE = re.compile(
    r"\x1b\]8;[^\x07\x1b]*;(?P<url>[^\x07\x1b]+)(?:\x07|\x1b\\)"
)
_RAW_DASHBOARD_URL_RE = re.compile(
    r"(?P<url>(?:https?://[^\s),\x1b\\]+)?/"
    r"(?:skills/(?:project|shared)/[^\s),\x1b\\]+|"
    r"preferences/(?:project/)?[^\s),\x1b\\]+|"
    r"rules/[^\s),\x1b\\]+))"
)

_COMPACT_INTRO = (
    "_If a listed `[cs:…]` item materially changes your answer, end with one "
    "final marker line. Skip the marker when no listed item changed the "
    "answer."
)

_MARKDOWN_MARKER_PARAGRAPH = (
    "Use human-readable linked titles, not raw ids. Format for one item: "
    "`✨ claude-smart rule applied: "
    "[verify process state](http://localhost:3001/rules/s1-123)`. "
    "For multiple items: "
    "`✨ claude-smart rules applied: "
    "[git safety](http://localhost:3001/rules/s1-123), "
    "[brief answer preference](http://localhost:3001/rules/p1-pref)`. "
    "Use the dashboard URL shown beside each cited item; do not invent URLs. "
    "Do not include `[cs:…]` ids in the marker line._"
)

_OSC8_EXAMPLE_ONE = (
    "✨ claude-smart rule applied: "
    "\x1b]8;;http://localhost:3001/rules/s1-123\x1b\\"
    "verify process state"
    "\x1b]8;;\x1b\\"
)
_OSC8_EXAMPLE_MULTI = (
    "✨ claude-smart rules applied: "
    "\x1b]8;;http://localhost:3001/rules/s1-123\x1b\\"
    "git safety"
    "\x1b]8;;\x1b\\"
    ", "
    "\x1b]8;;http://localhost:3001/rules/p1-pref\x1b\\"
    "brief answer preference"
    "\x1b]8;;\x1b\\"
)
_OSC8_MARKER_PARAGRAPH = (
    "Use human-readable OSC 8 terminal hyperlinks, not raw ids or visible "
    "URLs. Format for one item: "
    f"`{_OSC8_EXAMPLE_ONE}`. For multiple items: "
    f"`{_OSC8_EXAMPLE_MULTI}`. Use the dashboard URL shown beside each cited "
    "item; do not invent URLs. If OSC 8 is unavailable, use markdown links. "
    "Do not include `[cs:…]` ids in the marker line._"
)

CITATION_MODES = ("on", "auto", "marker-only", "off")
LINK_STYLES = ("markdown", "osc8")


def citation_instruction(mode: str, link_style: str = "markdown") -> str:
    """Return the citation prompt for ``mode``.

    Args:
        mode: ``"off"`` returns an empty string so no instruction is
            injected. Any other value, including legacy ``"auto"`` and
            ``"marker-only"``, returns the compact enabled instruction.
            Unknown values stay enabled — env-var typos must not break
            injection.
        link_style: ``"markdown"`` for ordinary markdown links, or ``"osc8"``
            for terminal-native hyperlinks. Unknown values fall back to
            ``"markdown"``.

    Returns:
        str: The instruction text, or ``""`` for ``"off"``.
    """
    if mode == "off":
        return ""
    marker_paragraph = (
        _OSC8_MARKER_PARAGRAPH if link_style == "osc8" else _MARKDOWN_MARKER_PARAGRAPH
    )
    return f"{_COMPACT_INTRO}\n\n{marker_paragraph}"


CITATION_INSTRUCTION = citation_instruction("on")


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


def parse_text_citations(text: str) -> list[str]:
    """Extract Codex text-only citation ids from a final learning marker line.

    Supports the legacy id marker::

        ✨ 1 claude-smart learning applied [cs:s1-ab12]

    and the newer human-readable dashboard-link marker::

        ✨ claude-smart rule applied: [git safety](http://localhost:3001/rules/s1-123)

    Ordinary references to injected ``[cs:...]`` ids or dashboard URLs inside
    the answer do not count as citations; they must appear on a final marker
    line. When multiple matching lines exist, the last one wins because the
    instruction requires the citation marker to be final.
    """
    old_matches = [
        (m.start(), "ids", m.group("ids"))
        for m in _TEXT_CITATION_LINE_RE.finditer(text or "")
    ]
    new_matches = [
        (m.start(), "links", m.group("body"))
        for m in _APPLIED_LINK_LINE_RE.finditer(text or "")
    ]
    matches = old_matches + new_matches
    if not matches:
        return []
    _, kind, value = max(matches, key=lambda item: item[0])
    if kind == "ids":
        return _parse_id_tokens(value)
    return _parse_dashboard_link_tokens(value)


def strip_marker_lines(text: str) -> str:
    """Remove any ``✨ N claude-smart learning(s) applied [cs:…]`` lines.

    Used by the Stop hook when ``CLAUDE_SMART_CITATIONS=off`` to scrub
    any marker the assistant emitted from a cached prompt fragment that
    still contained the citation instruction. Returns ``text`` unchanged
    when no marker line is present.

    Args:
        text: Assistant message text.

    Returns:
        str: ``text`` with marker lines removed, trailing blank lines
            trimmed. ``""`` when ``text`` is falsy.
    """
    if not text:
        return text or ""
    cleaned = _TEXT_CITATION_LINE_RE.sub("", text)
    cleaned = _APPLIED_LINK_LINE_RE.sub("", cleaned)
    return cleaned.rstrip("\n")


def _parse_id_tokens(raw_ids: str) -> list[str]:
    return [
        clean.group(1).lower()
        for tok in _SPLIT_RE.split(raw_ids.strip())
        if (clean := _CLEAN_ID_RE.match(tok))
    ]


def _parse_dashboard_link_tokens(raw: str) -> list[str]:
    tokens: list[str] = []
    seen: set[str] = set()
    matches = [
        (m.start(), m.group("url"))
        for regex in (_OSC8_URL_RE, _MARKDOWN_LINK_RE, _RAW_DASHBOARD_URL_RE)
        for m in regex.finditer(raw)
    ]
    for _, url in sorted(matches, key=lambda item: item[0]):
        if (token := dashboard_url_token(url)) and token not in seen:
            seen.add(token)
            tokens.append(token)
    return tokens


def dashboard_url_token(url: str) -> str:
    """Return an internal resolver token for a dashboard detail URL.

    The token is consumed by ``events.stop._resolve_cited_items``. It is not
    shown to users.
    """
    parsed = urlparse(url)
    path = parsed.path if parsed.scheme else url.split("?", 1)[0].split("#", 1)[0]
    parts = [unquote(part) for part in path.strip("/").split("/") if part]
    if len(parts) == 3 and parts[0] == "skills" and parts[1] in {"project", "shared"}:
        source_kind = "user_playbook" if parts[1] == "project" else "agent_playbook"
        return f"route:playbook:{source_kind}:{parts[2]}"
    if len(parts) == 2 and parts[0] == "preferences":
        return f"route:profile:profile:{parts[1]}"
    if len(parts) == 3 and parts[0] == "preferences" and parts[1] == "project":
        return f"route:profile:profile:{parts[2]}"
    if len(parts) == 2 and parts[0] == "rules":
        clean = _CLEAN_ID_RE.match(parts[1])
        return clean.group(1).lower() if clean else ""
    return ""
