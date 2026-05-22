"""Stop hook — finalize the current assistant turn, publish to reflexio."""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

from claude_smart import (
    cs_cite,
    env_config,
    ids,
    internal_call,
    publish,
    runtime,
    state,
)

_LOGGER = logging.getLogger(__name__)


# Stop fires immediately after Claude Code logs the final assistant message,
# and at tight gaps (<~10 ms observed) the transcript write hasn't propagated
# before our read. Retry briefly when the scan returns empty — note the cost
# is paid on *every* tool-only turn too (we can't tell those apart from a
# flush race), so keep the total budget small: 100 ms worst case.
_TRANSCRIPT_RETRY_DELAYS_S = (0.03, 0.07)

# Plan-mode approve/reject flows never fire a hook — Claude Code writes the
# decision as a ``user`` / ``tool_result`` transcript entry whose text begins
# with one of these markers. Surface them as synthetic User turns so reflexio
# sees the correction signal (especially rejection-with-comment feedback).
_PLAN_APPROVAL_MARKER = "User has approved your plan"
_PLAN_REJECTION_MARKER = "The user doesn't want to proceed"
_PLAN_REJECTION_COMMENT_MARKER = "the user said:"

_EXIT_PLAN_MODE_TOOL = "ExitPlanMode"


def _read_transcript_entries(path: Path) -> list[dict[str, Any]]:
    """Parse the transcript JSONL once into a list of entries.

    Stop's scanners (assistant text and plan decisions) both
    need the same parsed view; reading once and passing the list around keeps
    the hook's wall-clock cost to a single ``read_text`` per fire even on
    multi-megabyte transcripts.

    Args:
        path (Path): Absolute path to the transcript JSONL.

    Returns:
        list[dict[str, Any]]: Parsed entries in chronological order. Empty
            on read failure; malformed lines are silently skipped.
    """
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        _LOGGER.debug("transcript read failed: %s", exc)
        return []
    entries: list[dict[str, Any]] = []
    for raw in lines:
        candidate = raw.strip()
        if not candidate:
            continue
        try:
            entries.append(json.loads(candidate))
        except json.JSONDecodeError:
            continue
    return entries


def _load_transcript_with_retry(path: Path) -> list[dict[str, Any]]:
    """Read the transcript, retrying briefly when the assistant text is empty.

    Stop fires immediately after Claude Code logs the final assistant
    message; at tight gaps the transcript write hasn't propagated. Reread
    a couple of times if the current-turn assistant text comes back empty.
    Total worst-case wait is ~100ms.
    """
    entries = _read_transcript_entries(path)
    if _scan_transcript_for_assistant_text(entries):
        return entries
    for delay in _TRANSCRIPT_RETRY_DELAYS_S:
        time.sleep(delay)
        entries = _read_transcript_entries(path)
        if _scan_transcript_for_assistant_text(entries):
            return entries
    return entries


def _current_turn_assistant_entries(
    entries: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return assistant entries for the in-progress turn, oldest first.

    Walks ``entries`` from the end backward, collecting every
    ``type: assistant`` entry until a real user message is reached (a user
    entry whose content is not purely ``tool_result`` blocks — those continue
    the assistant turn). Restores chronological order before returning.
    """
    out: list[dict[str, Any]] = []
    for entry in reversed(entries):
        entry_type = entry.get("type")
        if entry_type == "assistant":
            out.append(entry)
        elif _is_user_turn_boundary(entry):
            break
    out.reverse()
    return out


def _scan_transcript_for_assistant_text(entries: list[dict[str, Any]]) -> str:
    """Join every text block from the current-turn assistant entries."""
    parts: list[str] = []
    for entry in _current_turn_assistant_entries(entries):
        message = entry.get("message") or {}
        parts.extend(_extract_text_blocks(message.get("content")))
    return "\n\n".join(parts)


def _scan_transcript_for_user_text(entries: list[dict[str, Any]]) -> str:
    """Return the user text that opened the current transcript turn."""
    for entry in reversed(entries):
        if not _is_user_turn_boundary(entry):
            continue
        message = entry.get("message") or {}
        return "\n\n".join(_extract_text_blocks(message.get("content")))
    return ""


def _is_user_turn_boundary(entry: dict[str, Any]) -> bool:
    """True if ``entry`` is the user message that opened the current turn.

    Tool results are delivered as ``type: user`` entries whose content is a
    list of ``tool_result`` blocks — those continue the assistant turn and
    must not be treated as a boundary. A real user message has string
    content or contains at least one non-``tool_result`` block.
    """
    if entry.get("type") != "user":
        return False
    message = entry.get("message") or {}
    content = message.get("content")
    if isinstance(content, str):
        return True
    if isinstance(content, list):
        return any(
            isinstance(block, dict) and block.get("type") != "tool_result"
            for block in content
        )
    return False


def _extract_text_blocks(content: Any) -> list[str]:
    """Return assistant-visible text from a transcript content list.

    Picks up plain ``type: "text"`` blocks and the ``plan`` payload of
    ``ExitPlanMode`` tool_use blocks. Plan mode emits the plan as a
    tool_use argument rather than a text block, so without the second
    branch the plan is silently dropped from the published turn.
    """
    if isinstance(content, str):
        return [content]
    if not isinstance(content, list):
        return []
    out: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text":
            text = block.get("text")
            if isinstance(text, str) and text:
                out.append(text)
        elif btype == "tool_use" and block.get("name") == _EXIT_PLAN_MODE_TOOL:
            plan = (block.get("input") or {}).get("plan")
            if isinstance(plan, str) and plan:
                out.append(f"Plan:\n{plan}")
    return out


def _scan_transcript_for_plan_decisions(entries: list[dict[str, Any]]) -> list[str]:
    """Return plan-mode approval/rejection content strings for the current turn.

    Plan-mode decisions arrive as ``tool_result`` blocks on ``type: user``
    transcript entries (the ExitPlanMode tool's "output"). PostToolUse runs
    *before* the user decides, so the decision text never reaches the hook
    payload — the transcript is the only place it exists. Walks forward from
    the user message that opened the current turn so prior-turn decisions
    (already published) are not re-emitted.

    The walk tracks the most recent assistant ``tool_use`` name so that only
    ``tool_result`` blocks immediately following an ``ExitPlanMode`` call are
    treated as plan decisions — guards against false positives from other
    tools whose output happens to contain the marker text (e.g. a ``Bash``
    that echoes "User has approved your plan").

    Args:
        entries (list[dict[str, Any]]): Pre-parsed transcript entries.

    Returns:
        list[str]: Human-readable content strings, e.g. ``"Approved the plan."``
            or ``"Rejected the plan. Instead: <comment>"``, in transcript
            order. Empty when no decisions are found.
    """
    turn_start = 0
    for idx in range(len(entries) - 1, -1, -1):
        if _is_user_turn_boundary(entries[idx]):
            turn_start = idx
            break

    decisions: list[str] = []
    pending_tool_name: str | None = None
    for entry in entries[turn_start:]:
        message = entry.get("message") or {}
        content = message.get("content")
        if not isinstance(content, list):
            continue
        if entry.get("type") == "assistant":
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    pending_tool_name = block.get("name")
        elif entry.get("type") == "user":
            for block in content:
                if not isinstance(block, dict) or block.get("type") != "tool_result":
                    continue
                if pending_tool_name != _EXIT_PLAN_MODE_TOOL:
                    continue
                text = _tool_result_text(block.get("content"))
                decision = _parse_plan_decision(text)
                if decision:
                    decisions.append(decision)
                # Each tool_use → tool_result pair is consumed once.
                pending_tool_name = None
    return decisions


def _tool_result_text(content: Any) -> str:
    """Flatten a ``tool_result.content`` field into a searchable string.

    Claude Code emits tool_result content as either a bare string or a list
    of ``{type: "text", text: "…"}`` blocks depending on the tool; we accept
    both so the plan-decision markers match regardless of shape.
    """
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for item in content:
        if isinstance(item, str):
            parts.append(item)
        elif isinstance(item, dict):
            inner = item.get("text") or item.get("content")
            if isinstance(inner, str):
                parts.append(inner)
    return "\n".join(parts)


def _parse_plan_decision(text: str) -> str | None:
    """Map a plan-mode tool_result text to a User-record content string."""
    if not text:
        return None
    if _PLAN_APPROVAL_MARKER in text:
        return "Approved the plan."
    if _PLAN_REJECTION_MARKER in text:
        _, sep, tail = text.partition(_PLAN_REJECTION_COMMENT_MARKER)
        comment = tail.strip() if sep else ""
        return (
            f"Rejected the plan. Instead: {comment}"
            if comment
            else "Rejected the plan."
        )
    return None


def _has_unpublished_user_turn(session_id: str) -> bool:
    """True when the session buffer already has a user turn awaiting publish."""
    _, interactions = state.unpublished_slice(state.read_all(session_id))
    return any(item.get("role") == "User" for item in interactions)


def _resolve_cited_items(session_id: str, cited_ids: list[str]) -> list[dict[str, Any]]:
    """Map citation ids to ``{id, kind, title}`` entries via the session registry.

    Unknown ids (Claude hallucinations, or items injected in a newer
    session than this hook can see) are dropped. Duplicate ids within
    one turn collapse to a single entry — the user-facing badge row
    doesn't need the multiplicity.
    """
    if not cited_ids:
        return []
    registry = state.read_injected(session_id)
    seen: set[str] = set()
    resolved: list[dict[str, Any]] = []
    for cid in cited_ids:
        if cid in seen:
            continue
        entry = _registry_entry_for_citation(registry, cid)
        if not entry:
            continue
        seen.add(cid)
        item: dict[str, Any] = {
            "id": entry.get("id", cid),
            "kind": entry.get("kind", ""),
            "title": entry.get("title", ""),
        }
        real_id = entry.get("real_id")
        if real_id:
            item["real_id"] = real_id
        source_kind = entry.get("source_kind")
        if isinstance(source_kind, str) and source_kind:
            item["source_kind"] = source_kind
        resolved.append(item)
    return resolved


def _registry_entry_for_citation(
    registry: dict[str, dict[str, Any]], citation: str
) -> dict[str, Any] | None:
    """Resolve a legacy rank id or a dashboard-route citation token."""
    if not citation.startswith("route:"):
        return registry.get(citation)
    try:
        _, kind, source_kind, real_id = citation.split(":", 3)
    except ValueError:
        return None
    for entry in registry.values():
        if entry.get("kind") != kind:
            continue
        if str(entry.get("real_id") or "") != real_id:
            continue
        if kind == "playbook" and entry.get("source_kind") != source_kind:
            continue
        return entry
    return None


def handle(payload: dict[str, Any]) -> tuple[publish.PublishStatus, int] | None:
    """Drain the buffered assistant turn to reflexio.

    Args:
        payload (dict[str, Any]): Claude Code hook payload.

    Returns:
        tuple[PublishStatus, int] | None: The ``(status, count)`` tuple
            from ``publish.publish_unpublished`` so the dispatcher can
            include it in the forensic hook log. ``None`` is returned
            only when the handler short-circuits before publishing (no
            ``session_id`` in the payload, or Codex internal-prompt
            detection skipped the fire).
    """
    session_id = payload.get("session_id")
    if not session_id:
        return None

    # Always append an Assistant record, even when the turn emitted only
    # tool calls and no text. ``state.unpublished_slice`` folds any
    # buffered ``Assistant_tool`` records into this turn's ``tools_used``;
    # without this placeholder, those tools would be misattributed to the
    # next assistant turn.
    transcript_path = payload.get("transcript_path")
    project_id = ids.resolve_project_id(payload.get("cwd"))

    entries: list[dict[str, Any]] = []
    if transcript_path:
        path = Path(transcript_path)
        if path.is_file():
            entries = _load_transcript_with_retry(path)

    prompt = payload.get("prompt") or _scan_transcript_for_user_text(entries)
    if runtime.is_codex() and internal_call.is_codex_internal_prompt(prompt):
        return None

    last_assistant_message = payload.get("last_assistant_message")
    assistant_text = (
        last_assistant_message
        if runtime.is_codex()
        and isinstance(last_assistant_message, str)
        and last_assistant_message
        else _scan_transcript_for_assistant_text(entries)
    )
    if (
        runtime.is_codex()
        and (
            internal_call.is_codex_title_response(assistant_text)
            or internal_call.is_codex_suggestions_response(assistant_text)
        )
        and not prompt
        and not _has_unpublished_user_turn(session_id)
    ):
        return None
    if os.environ.get("CLAUDE_SMART_CITATIONS", "on") == "off":
        assistant_text = cs_cite.strip_marker_lines(assistant_text)
    text_cited_ids = cs_cite.parse_text_citations(assistant_text)
    cited_items = _resolve_cited_items(session_id, text_cited_ids)
    plan_decisions = _scan_transcript_for_plan_decisions(entries)

    now = int(time.time())
    if plan_decisions:
        for decision_text in plan_decisions:
            state.append(
                session_id,
                {
                    "ts": now,
                    "role": "User",
                    "content": decision_text,
                    "user_id": project_id,
                },
            )
    elif prompt and not _has_unpublished_user_turn(session_id):
        state.append(
            session_id,
            {
                "ts": now,
                "role": "User",
                "content": prompt,
                "user_id": project_id,
            },
        )

    record: dict[str, Any] = {
        "ts": now,
        "role": "Assistant",
        "content": assistant_text,
        "user_id": project_id,
    }
    if cited_items:
        record["cited_items"] = cited_items
    state.append(session_id, record)
    if env_config.env_truthy(env_config.CLAUDE_SMART_READ_ONLY_ENV):
        state.mark_all_published(session_id)
        return ("nothing", 0)
    return publish.publish_unpublished(
        session_id=session_id,
        project_id=project_id,
        force_extraction=False,
        skip_aggregation=False,
    )
