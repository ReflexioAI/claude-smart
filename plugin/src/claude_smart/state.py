"""Per-session JSONL buffer for interactions awaiting publish to reflexio.

Each Claude Code session gets one file at
``~/.claude-smart/sessions/{session_id}.jsonl``. Lines are one of:

- ``{"role": "User", ...}`` — a user turn (see InteractionData fields)
- ``{"role": "Assistant", ...}`` — a finalized assistant turn
- ``{"role": "Assistant_tool", ...}`` — a single tool invocation, attached
  to the next assistant turn at ``Stop`` time
- ``{"published_up_to": N}`` — high-water mark so Stop / SessionEnd don't
  re-publish rows already sent to reflexio

The buffer exists for offline resilience: when reflexio is unreachable,
Stop appends without publishing and the next successful hook drains.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Iterable

try:
    import fcntl  # POSIX only — Windows hooks fall back to append-without-lock.
except ImportError:  # pragma: no cover — non-POSIX platforms
    fcntl = None  # type: ignore[assignment]

_LOGGER = logging.getLogger(__name__)

_ENV_STATE_DIR = "CLAUDE_SMART_STATE_DIR"
_DEFAULT_STATE_DIR = Path.home() / ".claude-smart" / "sessions"

_TOOL_DATA_FIELD_MAX_LEN = 256


def _truncate_tool_data_field(value: Any) -> Any:
    """Truncate a single tool_data field value to ``_TOOL_DATA_FIELD_MAX_LEN``.

    Only *top-level string* values are shortened. Nested containers
    (dicts, lists) and non-string scalars pass through unchanged, even if
    the container holds overlong strings — extractor prompts built from
    this payload are bounded upstream by reflexio, and truncating a mid-
    structure string risks producing invalid JSON when the caller later
    serializes. The cap keeps long fields (``Edit.old_string`` /
    ``new_string`` diffs, multi-line ``Bash`` scripts) from inflating the
    extractor's input; short fields like file paths, URLs, and typical
    commands stay intact. The value is tuned for extractor-prompt budget
    predictability, not for preserving every character of a real
    command — fields over the cap are treated as diff-style content
    whose exact tail rarely changes what extraction learns.

    Args:
        value (Any): A field value from the redacted tool_input dict.

    Returns:
        Any: The value truncated to ``_TOOL_DATA_FIELD_MAX_LEN`` chars if it
            was an overlong string, otherwise the original value.
    """
    if isinstance(value, str) and len(value) > _TOOL_DATA_FIELD_MAX_LEN:
        return value[:_TOOL_DATA_FIELD_MAX_LEN]
    return value


def state_dir() -> Path:
    """Root directory for session JSONL files. Honours ``CLAUDE_SMART_STATE_DIR``."""
    override = os.environ.get(_ENV_STATE_DIR)
    return Path(override) if override else _DEFAULT_STATE_DIR


def session_path(session_id: str) -> Path:
    """Return the JSONL path for a given session id."""
    return state_dir() / f"{session_id}.jsonl"


def injected_path(session_id: str) -> Path:
    """Return the JSONL path for the per-session cs-cite registry."""
    return state_dir() / f"{session_id}.injected.jsonl"


def append_injected(session_id: str, entries: Iterable[dict[str, Any]]) -> None:
    """Append citation-registry entries to the per-session injected-items file.

    Each entry maps a short ``id`` (4-hex-char) back to the playbook or
    profile it came from so the Stop hook can resolve ids cited by
    Claude via ``cs-cite`` into human-readable titles for the dashboard.
    Silently no-ops when ``entries`` is empty.
    """
    records = list(entries)
    if not records:
        return
    path = injected_path(session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        if fcntl is not None:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            except OSError as exc:
                _LOGGER.debug("flock failed on %s: %s", path, exc)
        for rec in records:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


def read_injected(session_id: str) -> dict[str, dict[str, Any]]:
    """Return the per-session citation registry keyed by id.

    Later entries win when the same id was injected multiple times
    (identical content produces the same hash-derived id, so the extra
    record only refreshes metadata).
    """
    path = injected_path(session_id)
    if not path.exists():
        return {}
    registry: dict[str, dict[str, Any]] = {}
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError as exc:
                _LOGGER.warning("Skipping malformed injected line in %s: %s", path, exc)
                continue
            item_id = entry.get("id")
            if isinstance(item_id, str) and item_id:
                registry[item_id] = entry
    return registry


def append(session_id: str, record: dict[str, Any]) -> None:
    """Append one JSON record to the session buffer. Creates the dir if needed.

    Holds an exclusive ``flock`` on the buffer file across the write so
    concurrent hooks (e.g. parallel ``PostToolUse`` fires) cannot interleave
    JSON lines when a payload exceeds the buffered-writer's flush size.
    """
    path = session_path(session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, ensure_ascii=False) + "\n"
    with path.open("a", encoding="utf-8") as fh:
        if fcntl is not None:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            except OSError as exc:
                _LOGGER.debug("flock failed on %s: %s", path, exc)
        fh.write(line)


def read_all(session_id: str) -> list[dict[str, Any]]:
    """Return every record in the buffer as a list of dicts. Missing file → []."""
    path = session_path(session_id)
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError as exc:
                _LOGGER.warning("Skipping malformed buffer line in %s: %s", path, exc)
    return records


def unpublished_slice(
    records: Iterable[dict[str, Any]],
) -> tuple[int, list[dict[str, Any]]]:
    """Split records into (last-published index, unpublished turn records).

    Walks the records in order, tracking the most recent ``published_up_to``
    marker and collecting turn records (anything with a ``role``) that come
    after it. Tool records are folded into the closest following Assistant
    turn's ``tools_used``.

    Returns:
        tuple[int, list[dict]]: ``(published_up_to, interactions)``. The
            integer is the watermark after which all turns are unpublished;
            the list is formatted for ``InteractionData`` construction.
    """
    published = 0
    pending_tools: list[dict[str, Any]] = []
    turns: list[dict[str, Any]] = []
    for idx, rec in enumerate(records):
        if "published_up_to" in rec:
            published = rec["published_up_to"]
            pending_tools = []
            turns = []
            continue
        if idx < published:
            continue
        role = rec.get("role")
        if role == "Assistant_tool":
            tool_input = rec.get("tool_input") or {}
            tool_output = rec.get("tool_output") or ""
            tool_entry: dict[str, Any] = {
                "tool_name": rec.get("tool_name", ""),
                "status": rec.get("status", "success"),
            }
            tool_data: dict[str, Any] = {}
            if tool_input:
                tool_data["input"] = {
                    k: _truncate_tool_data_field(v) for k, v in tool_input.items()
                }
            if tool_output:
                tool_data["output"] = _truncate_tool_data_field(tool_output)
            if tool_data:
                tool_entry["tool_data"] = tool_data
            pending_tools.append(tool_entry)
            continue
        if role in {"User", "Assistant"}:
            # ``cited_items`` is local-only metadata for the dashboard's
            # "used" badge; reflexio's InteractionData has no slot for it.
            turn = {
                k: v for k, v in rec.items() if k not in {"role", "ts", "cited_items"}
            }
            turn["role"] = role
            if role == "Assistant" and pending_tools:
                turn["tools_used"] = pending_tools
                pending_tools = []
            turns.append(turn)
    return published, turns
