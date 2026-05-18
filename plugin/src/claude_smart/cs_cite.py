"""Support helpers for claude-smart citation tracking.

Context injected by UserPromptSubmit / PreToolUse tags each skill and
preference bullet with a rank-based id fingerprinted by the underlying
real id (``[cs:s1-1a2b]`` for the first skill whose
``user_playbook_id`` starts with ``1a2b``, ``[cs:p2-c3d4]`` for the
second preference). The injected instruction asks the assistant to end
impactful replies with a marker like::

    ✨ 1 claude-smart learning applied [cs:s1-1a2b]

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
  caller; valid values are ``"auto"`` (default — counterfactual + marker),
  ``"marker-only"`` (suppress the counterfactual), and ``"off"`` (no
  instruction injected, no marker emitted).
- ``CITATION_INSTRUCTION``: the full ``"auto"`` instruction string, kept
  as a module-level constant for backward-compatible imports.
"""

from __future__ import annotations

import re
from typing import Any

_FINGERPRINT_LEN = 4

_ID_TOKEN = r"(?i:cs:)?(?i:[ps])\d+(?:-(?i:[a-z0-9]){1,4})?"
_ID_SEP = r"[,\s]+"
_CLEAN_ID_RE = re.compile(r"^(?i:cs:)?((?i:[ps])\d+(?:-(?i:[a-z0-9]){1,4})?)$")
_SPLIT_RE = re.compile(_ID_SEP)
_TEXT_CITATION_LINE_RE = re.compile(
    r"(?im)^\s*✨\s+\d+\s+claude-smart learning(?:s)? applied\s+"
    r"\[cs:(?P<ids>[^\]]+)\]\s*$"
)

_INTRO_AUTO = (
    "_First, fully answer the user — citation does not change what or how "
    "you reply. Then, as a final step, consider whether to cite: if — and "
    "only if — an injected `[cs:…]` item materially changed your reply "
    "(different wording, action, or conclusion than you would have produced "
    "without it), append a citation block at the very end of your message. "
    "Do not call a shell command or any other tool for citations. "
    "Ids come verbatim from the `[cs:…]` tags above — the leading letter "
    "is exactly `s` (skill) or `p` (preference); no other letter is valid. "
    "If an id you want to cite is not literally present in a `[cs:…]` tag "
    "in your context, skip the citation entirely rather than guess."
)

_INTRO_MARKER_ONLY = (
    "_First, fully answer the user — citation does not change what or how "
    "you reply. Then, as a final step, consider whether to cite: if — and "
    "only if — an injected `[cs:…]` item materially changed your reply "
    "(different wording, action, or conclusion than you would have produced "
    "without it), append the marker line below at the very end of your "
    "message. Do not call a shell command or any other tool for citations. "
    "Ids come verbatim from the `[cs:…]` tags above — the leading letter "
    "is exactly `s` (skill) or `p` (preference); no other letter is valid. "
    "If an id you want to cite is not literally present in a `[cs:…]` tag "
    "in your context, skip the citation entirely rather than guess."
)

_COUNTERFACTUAL_PARAGRAPH = (
    "The citation block is up to two lines: an optional counterfactual line "
    "followed by the marker line. Check your own prior assistant messages in "
    "this conversation — if you have NOT yet emitted a `✨ … claude-smart "
    "learning(s) applied` line earlier in this session, include the "
    "counterfactual line; otherwise skip it. The counterfactual is one short "
    "factual sentence contrasting your actual reply with the unlearned "
    "baseline. Refer to the skill by a short, human paraphrase (≤ 6 words) "
    "of what it asks for — taken from the bullet's content — wrapped in "
    "double quotes. Do not put the `[cs:…]` id in the counterfactual line; "
    'the id appears only in the marker line below. Example: `↳ Without '
    '"verify process state before suggesting kill" I would have told you '
    "to kill 88040 as a runaway fork.` Keep it factual, not promotional, "
    "and ≤ 30 words."
)

_MARKER_PARAGRAPH = (
    "End the message with exactly the marker line. Use this exact format "
    "for one id: `✨ 1 claude-smart learning applied [cs:s1-ab12]`. Use "
    "this exact format for multiple ids: "
    "`✨ 2 claude-smart learnings applied [cs:s1-ab12,p2-cd34]`, where the "
    "number is the count of ids in the brackets. The marker line MUST be "
    "the very last line of your message and end with `]` — nothing after "
    "it. Never emit a standalone wrapper like `✨s1-ab12✨` or "
    "`✨abc123✨`; those are not claude-smart citations and cannot be "
    "resolved."
)

_DEFAULT_SKIP_PARAGRAPH = (
    "Default is to skip the whole block. If an item is merely on-topic, "
    "confirms what you already planned, or your reply would read the same "
    "without it, do not cite — end the turn normally with your reply. When "
    "unsure, skip. Do not add any other text, tool calls, or role markers "
    "after the final marker line._"
)

CITATION_MODES = ("auto", "marker-only", "off")


def citation_instruction(mode: str) -> str:
    """Return the citation prompt for ``mode``.

    Args:
        mode: One of ``"auto"`` (full instruction), ``"marker-only"``
            (suppress the counterfactual paragraph), or ``"off"`` (return
            an empty string so no instruction is injected). Unknown values
            fall back to ``"auto"`` — env-var typos must not break
            injection.

    Returns:
        str: The instruction text, or ``""`` for ``"off"``.
    """
    if mode == "off":
        return ""
    if mode == "marker-only":
        return f"{_INTRO_MARKER_ONLY}\n\n{_MARKER_PARAGRAPH}\n\n{_DEFAULT_SKIP_PARAGRAPH}"
    return (
        f"{_INTRO_AUTO}\n\n{_COUNTERFACTUAL_PARAGRAPH}\n\n"
        f"{_MARKER_PARAGRAPH}\n\n{_DEFAULT_SKIP_PARAGRAPH}"
    )


CITATION_INSTRUCTION = citation_instruction("auto")


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

    The parser intentionally only accepts lines containing the visual
    ``claude-smart learning(s) applied`` marker, so ordinary references to
    injected ``[cs:...]`` ids inside an answer do not count as citations.
    When multiple matching lines exist, the last one wins because the
    instruction requires the citation marker to be final.
    """
    matches = list(_TEXT_CITATION_LINE_RE.finditer(text or ""))
    if not matches:
        return []
    return _parse_id_tokens(matches[-1].group("ids"))


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
    return cleaned.rstrip("\n")


def _parse_id_tokens(raw_ids: str) -> list[str]:
    ids: list[str] = []
    for tok in _SPLIT_RE.split(raw_ids.strip()):
        if clean := _CLEAN_ID_RE.match(tok):
            ids.append(clean.group(1).lower())
    return ids
