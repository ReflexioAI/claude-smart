"""SessionEnd hook — flush any remaining interactions; extraction runs async.

When ``Stop`` never fires during a session (autonomous agentic loops
that don't reach ``stop_reason=end_turn``), the buffer at SessionEnd
time can contain orphan ``Assistant_tool`` records with no closing
``Assistant`` anchor. The ``state.unpublished_slice`` contract folds
tool records into the *next* Assistant turn's ``tools_used``; without
that anchor, every buffered tool call is silently dropped at publish
time and reflexio receives only the original User prompt.

This module synthesises a closing Assistant record from whatever
assistant text the Claude Code transcript still has, so the publish
carries the full tool record stream instead of just the User prompt.
A short placeholder string is used when the transcript is unreadable.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

from claude_smart import ids, publish, state
from claude_smart.events.stop import (
    _read_transcript_entries,
    _scan_transcript_for_assistant_text,
)

_LOGGER = logging.getLogger(__name__)

# Fallback Assistant content used when the transcript is missing,
# unreadable, or has no recoverable assistant text. The string is
# distinctive so a future audit can grep for it and quantify how often
# a session ended without any model response.
_PLACEHOLDER_ASSISTANT_TEXT = "<session ended without final assistant response>"


def handle(payload: dict[str, Any]) -> tuple[publish.PublishStatus, int] | None:
    """Flush any remaining buffered turns to reflexio.

    Args:
        payload (dict[str, Any]): Claude Code hook payload.

    Returns:
        tuple[PublishStatus, int] | None: The ``(status, count)`` tuple
            from ``publish.publish_unpublished`` so the dispatcher's
            forensic hook log captures the publish outcome. ``None``
            when the payload had no ``session_id``.
    """
    session_id = payload.get("session_id")
    if not session_id:
        return None
    project_id = ids.resolve_project_id(payload.get("cwd"))

    _maybe_synthesize_assistant_anchor(
        session_id=session_id,
        project_id=project_id,
        transcript_path=payload.get("transcript_path"),
    )

    # ``force_extraction=True`` makes the reflexio server run the
    # extractor synchronously for this publish, so by the time the HTTP
    # call returns the playbook (if any) is already committed. SessionEnd
    # is the final flush — the user/agent is already going away — so
    # adding a few extra seconds here trades nothing for "snapshot_playbooks()
    # reads after this point see the just-extracted rows." Stop hooks
    # (the per-turn flushes) stay async so they don't slow interactive
    # generation.
    return publish.publish_unpublished(
        session_id=session_id,
        project_id=project_id,
        force_extraction=True,
        skip_aggregation=False,
    )


def _maybe_synthesize_assistant_anchor(
    *,
    session_id: str,
    project_id: str,
    transcript_path: Any,
) -> None:
    """Append a closing Assistant record when the unpublished tail
    holds orphan ``Assistant_tool`` rows.

    Walks the post-watermark slice of the buffer and looks for a tail
    pattern of ``Assistant_tool`` records that aren't followed by an
    ``Assistant`` record. If that's the case, scans the Claude Code
    transcript for whatever final assistant text is recoverable and
    appends one synthetic ``Assistant`` record so
    ``state.unpublished_slice`` can fold the tools into its
    ``tools_used`` field at publish time.

    Args:
        session_id (str): Claude Code session id for the state buffer.
        project_id (str): Resolved project_id used as the synthetic
            record's ``user_id`` (matches Stop's convention).
        transcript_path (Any): The ``transcript_path`` value from the
            hook payload — accepted as ``Any`` because Claude Code may
            send a string, ``None``, or omit the key entirely.

    Returns:
        None
    """
    records = state.read_all(session_id)
    if not _tail_has_orphan_tools(records):
        return

    assistant_text = _recover_assistant_text(transcript_path)
    state.append(
        session_id,
        {
            "ts": int(time.time()),
            "role": "Assistant",
            "content": assistant_text,
            "user_id": project_id,
            "synthesised_by": "session_end_anchor",
        },
    )
    _LOGGER.info(
        "session_end: synthesised Assistant anchor for %s (len=%d)",
        session_id,
        len(assistant_text),
    )


def _tail_has_orphan_tools(records: list[dict[str, Any]]) -> bool:
    """Return True when the post-watermark tail ends with one or more
    ``Assistant_tool`` records with no closing ``Assistant`` record.

    Walks records from the latest ``published_up_to`` marker forward;
    tracks the most recent record role. The tail is considered "orphan"
    when at least one ``Assistant_tool`` was seen after the watermark
    and no ``Assistant`` record was seen *after* the last
    ``Assistant_tool``.

    Args:
        records (list[dict[str, Any]]): The full buffered record list
            as returned by ``state.read_all``.

    Returns:
        bool: True if a synthetic anchor would unblock publishing tool
            records that would otherwise be dropped.
    """
    # Find the most recent published_up_to watermark.
    published = 0
    for rec in records:
        if "published_up_to" in rec:
            published = rec["published_up_to"]

    tail = records[published:]
    saw_orphan_tool = False
    for rec in tail:
        role = rec.get("role")
        if role == "Assistant_tool":
            saw_orphan_tool = True
        elif role == "Assistant":
            saw_orphan_tool = False
    return saw_orphan_tool


def _recover_assistant_text(transcript_path: Any) -> str:
    """Return whatever assistant text the transcript still has, or a placeholder.

    Args:
        transcript_path (Any): Raw value from the hook payload.

    Returns:
        str: Concatenated current-turn assistant text from the
            transcript, or ``_PLACEHOLDER_ASSISTANT_TEXT`` when the
            transcript is unset, missing, or yields nothing.
    """
    if not isinstance(transcript_path, str) or not transcript_path:
        return _PLACEHOLDER_ASSISTANT_TEXT
    path = Path(transcript_path)
    if not path.is_file():
        return _PLACEHOLDER_ASSISTANT_TEXT
    try:
        entries = _read_transcript_entries(path)
    except OSError as exc:
        _LOGGER.debug("session_end transcript read failed: %s", exc)
        return _PLACEHOLDER_ASSISTANT_TEXT
    text = _scan_transcript_for_assistant_text(entries)
    return text or _PLACEHOLDER_ASSISTANT_TEXT
