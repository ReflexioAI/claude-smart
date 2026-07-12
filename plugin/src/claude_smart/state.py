"""Per-session JSONL buffer for interactions awaiting publish to reflexio.

Each Claude Code session gets one file at
``~/.claude-smart/sessions/{session_id}.jsonl``. Lines are one of:

- ``{"role": "User", ...}`` — a user turn (see InteractionData fields)
- ``{"role": "Assistant", ...}`` — a finalized assistant turn
- ``{"role": "Assistant_tool", ...}`` — a single tool invocation, attached
  to the next assistant turn at ``Stop`` time
- ``{"published_up_to": N}`` — high-water mark so Stop / SessionEnd don't
  re-publish rows already sent to reflexio
- ``{"retrieved_learning_refs": [...]}`` — identity pairs shown to the agent,
  attached by event order to the next Assistant turn
- ``{"publish_attempt": {"start": N, "end": M}}`` — frozen retry boundary for
  one in-flight publish

The buffer exists for offline resilience: when reflexio is unreachable,
Stop appends without publishing and the next successful hook drains.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Iterable
from pathlib import Path
from typing import Any

try:
    import fcntl  # POSIX only — Windows hooks fall back to append-without-lock.
except ImportError:  # pragma: no cover — non-POSIX platforms
    fcntl = None  # type: ignore[assignment]

_LOGGER = logging.getLogger(__name__)

_ENV_STATE_DIR = "CLAUDE_SMART_STATE_DIR"
_DEFAULT_STATE_DIR = Path.home() / ".claude-smart" / "sessions"

_TOOL_DATA_FIELD_MAX_LEN = 256

_VALID_CITATION_KINDS = frozenset(
    {"playbook", "profile", "user_playbook", "agent_playbook"}
)
_VALID_RETRIEVED_PLAYBOOK_KINDS = frozenset({"user_playbook", "agent_playbook"})
_VALID_RETRIEVED_KINDS = _VALID_RETRIEVED_PLAYBOOK_KINDS | {"profile"}
_RETRIEVED_LEARNINGS_PUBLISH_CAP = 1000


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
    """Return the JSONL path for the per-session citation registry."""
    return state_dir() / f"{session_id}.injected.jsonl"


def _normalize_registry_ref(entry: dict[str, Any]) -> dict[str, str] | None:
    """Return the identity pair carried by one citation-registry entry."""

    learning_id = entry.get("real_id")
    kind = entry.get("kind")
    if not isinstance(learning_id, str) or not learning_id:
        return None
    if kind == "profile":
        wire_kind = "profile"
    elif (
        kind == "playbook"
        and entry.get("source_kind") in _VALID_RETRIEVED_PLAYBOOK_KINDS
    ):
        wire_kind = entry["source_kind"]
    else:
        return None
    return {"kind": wire_kind, "learning_id": learning_id}


def append_injected(session_id: str, entries: Iterable[dict[str, Any]]) -> None:
    """Record injected items for citations and the next Assistant publish.

    The citation sidecar keeps the display metadata used by Stop. The ordered
    session event stores identity pairs only, so publishing never has to join
    two files or infer event order from timestamps.
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
        for record in records:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")

    refs: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for record in records:
        ref = _normalize_registry_ref(record)
        if ref is None:
            continue
        key = (ref["kind"], ref["learning_id"])
        if key in seen:
            continue
        seen.add(key)
        refs.append(ref)
    if refs:
        append(session_id, {"retrieved_learning_refs": refs})


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


def mark_all_published(session_id: str) -> None:
    """Advance the watermark over every currently buffered record.

    Read-only mode still needs to retire any already-buffered interactions so
    they cannot publish later if learning is re-enabled.
    """
    append(session_id, {"published_up_to": len(read_all(session_id))})


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


def _to_wire_citations(cited_items: Any) -> list[dict[str, str]]:
    """Map local ``cited_items`` to the wire ``Citation`` shape.

    Local entries (from ``events.stop._resolve_cited_items``) carry
    ``{id, kind, title, real_id}``; reflexio's ``InteractionData.citations``
    wants ``{kind, real_id, tag, title}`` where ``tag`` is the rank id
    (``s1-301``-style) we already keep under ``id``. Entries without a
    ``real_id`` (unresolved injections) are dropped — the server can't
    join them back to a stored row.

    Args:
        cited_items (Any): The list-of-dicts blob attached to an Assistant
            turn record, or ``None`` when the turn cited nothing.

    Returns:
        list[dict[str, str]]: Citation dicts ready to be folded into an
            ``InteractionData`` payload. Empty when ``cited_items`` is
            missing, malformed, or contains nothing resolvable.
    """
    if not isinstance(cited_items, list):
        return []
    out: list[dict[str, str]] = []
    for item in cited_items:
        if not isinstance(item, dict):
            continue
        real_id = item.get("real_id")
        kind = item.get("kind")
        if not isinstance(real_id, str) or not real_id:
            continue
        wire_kind = kind
        if kind == "playbook":
            source_kind = item.get("source_kind")
            if source_kind in {"user_playbook", "agent_playbook"}:
                wire_kind = source_kind
        if wire_kind not in _VALID_CITATION_KINDS:
            continue
        tag = item.get("id")
        title = item.get("title")
        out.append(
            {
                "kind": wire_kind,
                "real_id": real_id,
                "tag": tag if isinstance(tag, str) else "",
                "title": title if isinstance(title, str) else "",
            }
        )
    return out


def published_record_offset(records: list[dict[str, Any]]) -> int:
    """Return the highest valid publish watermark in the session buffer."""

    limit = len(records)
    return max(
        (
            value
            for record in records
            if isinstance((value := record.get("published_up_to")), int)
            and 0 <= value <= limit
        ),
        default=0,
    )


def pending_publish_end(
    records: list[dict[str, Any]], published: int
) -> int | None:
    """Return the frozen end of the current publish attempt, if any."""

    for marker_index, record in enumerate(records[published:], start=published):
        attempt = record.get("publish_attempt")
        if not isinstance(attempt, dict):
            continue
        start = attempt.get("start")
        end = attempt.get("end")
        if (
            start == published
            and isinstance(end, int)
            and published < end <= marker_index
        ):
            _, interactions = unpublished_slice(
                records[:end], published_override=published
            )
            if interactions:
                return end
    return None


def _wire_retrieved_ref(value: Any) -> dict[str, str] | None:
    """Validate one identity pair read from the ordered session buffer."""

    if not isinstance(value, dict):
        return None
    kind = value.get("kind")
    learning_id = value.get("learning_id")
    if kind not in _VALID_RETRIEVED_KINDS:
        return None
    if not isinstance(learning_id, str) or not learning_id:
        return None
    return {"kind": kind, "learning_id": learning_id}


def safe_publish_end(records: list[dict[str, Any]], published: int) -> int:
    """Stop before identity refs that do not yet have an Assistant target."""

    unmatched_start: int | None = None
    for index, record in enumerate(records[published:], start=published):
        refs = record.get("retrieved_learning_refs")
        if (
            unmatched_start is None
            and isinstance(refs, list)
            and any(_wire_retrieved_ref(value) is not None for value in refs)
        ):
            unmatched_start = index
        if record.get("role") == "Assistant":
            unmatched_start = None
    return len(records) if unmatched_start is None else unmatched_start


def unpublished_slice(
    records: Iterable[dict[str, Any]],
    *,
    published_override: int | None = None,
) -> tuple[int, list[dict[str, Any]]]:
    """Return the current watermark and wire-ready unpublished interactions.

    Metadata records are interpreted by their numeric boundary rather than
    their physical position in the append-only file. This preserves turns that
    arrive while an earlier network request is in flight.
    """

    buffered = list(records)
    if published_override is None:
        published = published_record_offset(buffered)
    elif 0 <= published_override <= len(buffered):
        published = published_override
    else:
        raise ValueError("published_override is outside the supplied record range")
    pending_tools: list[dict[str, Any]] = []
    pending_refs: list[dict[str, str]] = []
    turns: list[dict[str, Any]] = []
    attached_total = skipped = truncated = 0

    for index, record in enumerate(buffered):
        if index < published:
            continue

        refs = record.get("retrieved_learning_refs")
        if "role" not in record and isinstance(refs, list):
            for value in refs:
                ref = _wire_retrieved_ref(value)
                if ref is None:
                    skipped += 1
                else:
                    pending_refs.append(ref)
            continue

        role = record.get("role")
        if role == "Assistant_tool":
            tool_input = record.get("tool_input") or {}
            tool_output = record.get("tool_output") or ""
            tool_entry: dict[str, Any] = {
                "tool_name": record.get("tool_name", ""),
                "status": record.get("status", "success"),
            }
            tool_data: dict[str, Any] = {}
            if tool_input:
                tool_data["input"] = {
                    key: _truncate_tool_data_field(value)
                    for key, value in tool_input.items()
                }
            if tool_output:
                tool_data["output"] = _truncate_tool_data_field(tool_output)
            if tool_data:
                tool_entry["tool_data"] = tool_data
            pending_tools.append(tool_entry)
            continue

        if role not in {"User", "Assistant"}:
            continue

        turn = {
            key: value
            for key, value in record.items()
            if key not in {"role", "ts", "cited_items"}
        }
        turn["role"] = role
        if role == "Assistant":
            citations = _to_wire_citations(record.get("cited_items"))
            if citations:
                turn["citations"] = citations
            if pending_tools:
                turn["tools_used"] = pending_tools
                pending_tools = []

            retrieved: list[dict[str, str]] = []
            seen: set[tuple[str, str]] = set()
            for ref in pending_refs:
                key = (ref["kind"], ref["learning_id"])
                if key in seen:
                    continue
                seen.add(key)
                if attached_total >= _RETRIEVED_LEARNINGS_PUBLISH_CAP:
                    truncated += 1
                    continue
                retrieved.append(ref)
                attached_total += 1
            pending_refs = []
            if retrieved:
                turn["retrieved_learnings"] = retrieved
        turns.append(turn)

    if skipped:
        _LOGGER.debug("Skipped %d malformed retrieved-learning refs", skipped)
    if truncated:
        _LOGGER.warning(
            "Dropped %d retrieved-learning refs at the publish request cap", truncated
        )
    return published, turns
